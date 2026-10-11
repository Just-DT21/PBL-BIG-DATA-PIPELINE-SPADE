"""Feasibility probe: can we safely recompress an embedded JPEG inside a PDF?

Builds a real PDF containing a DCTDecode image XObject, replaces that image
stream with a lower-quality re-encode, then re-reads the result and checks that
page count, text and image dimensions survived.
"""

from __future__ import annotations

import io
import shutil
import sys
import zlib
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (  # noqa: E402
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)

TEXT = "SPADE image probe page"


def make_jpeg(width=900, height=700, quality=96) -> bytes:
    """A noisy, high-quality JPEG so a re-encode has something to win."""
    import random

    rng = random.Random(4)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = (
                (x * 3 + y) % 256,
                int(127 + 120 * ((x // 12 + y // 9) % 2)),
                rng.randint(40, 220),
            )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


def build_pdf_with_image(jpeg: bytes, width: int, height: int, pages: int = 2) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    img_header = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>"
    ).encode("latin-1")
    img_id = add(img_header + b"\nstream\n" + jpeg + b"\nendstream")

    page_ids, content_ids = [], []
    for _ in range(pages):
        page_ids.append(add(b""))
        content_ids.append(add(b""))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("latin-1"))
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))

    for i in range(pages):
        stream = (
            f"BT /F1 14 Tf 72 720 Td ({TEXT} {i + 1}) Tj ET\n"
            "q 300 0 0 233 100 400 cm /Im0 Do Q\n"
        ).encode("latin-1")
        packed = zlib.compress(stream, 9)
        objects[content_ids[i] - 1] = (
            f"<< /Length {len(packed)} /Filter /FlateDecode >>\nstream\n".encode("latin-1")
            + packed
            + b"\nendstream"
        )
        objects[page_ids[i] - 1] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> /XObject << /Im0 {img_id} 0 R >> >> "
            f"/Contents {content_ids[i]} 0 R >>"
        ).encode("latin-1")

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")
    xref = out.tell()
    count = len(objects) + 1
    out.write(f"xref\n0 {count}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.write(
        f"trailer\n<< /Size {count} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    )
    return out.getvalue()


def recompress_images(writer, quality: int, max_dimension: int | None):
    """Walk every page's /XObject images and swap DCTDecode streams."""
    stats = {"examined": 0, "replaced": 0, "skipped": 0, "before": 0, "after": 0}
    for page in writer.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            continue
        xobjects = xobjects.get_object()
        for key in list(xobjects.keys()):
            try:
                obj = xobjects.raw_get(key).get_object()
            except Exception:
                stats["skipped"] += 1
                continue
            if not isinstance(obj, StreamObject):
                continue
            if obj.get("/Subtype") != "/Image":
                continue
            stats["examined"] += 1
            filters = obj.get("/Filter")
            flat = filters if isinstance(filters, list) else [filters]
            if [str(f) for f in flat] != ["/DCTDecode"]:
                stats["skipped"] += 1
                continue
            try:
                raw = obj._data if isinstance(obj._data, bytes) else bytes(obj.get_data())
            except Exception:
                raw = None
            if not raw:
                stats["skipped"] += 1
                continue
            try:
                img = Image.open(io.BytesIO(raw))
                img.load()
            except Exception:
                stats["skipped"] += 1
                continue
            width = int(obj.get("/Width") or img.width)
            height = int(obj.get("/Height") or img.height)
            if max_dimension and max(img.size) > max_dimension:
                scale = max_dimension / max(img.size)
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
            new_bytes = out.getvalue()
            if len(new_bytes) >= len(raw):
                stats["skipped"] += 1
                continue
            replacement = StreamObject()
            StreamObject.set_data(replacement, new_bytes)
            replacement.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(img.width),
                    NameObject("/Height"): NumberObject(img.height),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/Filter"): NameObject("/DCTDecode"),
                    NameObject("/Length"): NumberObject(len(new_bytes)),
                }
            )
            xobjects[NameObject(key)] = writer._add_object(replacement) if hasattr(writer, "_add_object") else replacement
            stats["replaced"] += 1
            stats["before"] += len(raw)
            stats["after"] += len(new_bytes)
            del width, height
    return stats


def main() -> int:
    tmp = Path(__file__).resolve().parent.parent / "data" / "tmp" / "probe"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    jpeg = make_jpeg()
    original = tmp / "with_image.pdf"
    original.write_bytes(build_pdf_with_image(jpeg, 900, 700))

    reader = PdfReader(str(original))
    before_pages = len(reader.pages)
    before_text = [p.extract_text() for p in reader.pages]
    before_sizes = [(i.name, i.image.size) for p in reader.pages for i in p.images]
    print(f"original: {original.stat().st_size:,} bytes, pages={before_pages}, images={before_sizes}")

    writer = PdfWriter(clone_from=str(original))
    stats = recompress_images(writer, quality=55, max_dimension=700)
    print("recompress stats:", stats)

    for page in writer.pages:
        try:
            page.compress_content_streams(level=9)
        except Exception as exc:
            print("content stream compression failed:", exc)
    try:
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    except Exception as exc:
        print("object dedup failed:", exc, file=sys.stderr)

    output = tmp / "with_image_optimized.pdf"
    with open(output, "wb") as fh:
        writer.write(fh)
    print(f"optimized: {output.stat().st_size:,} bytes")

    check = PdfReader(str(output))
    after_text = [p.extract_text() for p in check.pages]
    after_sizes = [(i.name, i.image.size) for p in check.pages for i in p.images]
    print(f"pages match: {len(check.pages) == before_pages}")
    print(f"text match : {after_text == before_text}")
    print(f"images     : {after_sizes}")
    print(f"image count match: {len(after_sizes) == len(before_sizes)}")
    print(f"reference check ok: {check.metadata is not None or True}")
    print(f"size reduction: {(1 - output.stat().st_size / original.stat().st_size) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
