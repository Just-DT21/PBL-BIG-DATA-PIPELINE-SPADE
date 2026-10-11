"""Unit tests: content detection, storage containment, secrets and metric maths.

These run without touching the worker subprocess, so they are the fast layer of
the suite. Everything they assert is a property the web layer relies on.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from spade import db, filing, security, storage
from spade.pipeline import metrics
from spade.pipeline.backends import BackendUnavailable, backend_catalog, get_backend
from spade.pipeline.engines import engine_for, supported_formats

FORMAT_FOR_SAMPLE = {
    "csv": "csv",
    "tsv": "csv",
    "json": "json",
    "txt": "txt",
    "docx": "docx",
    "pdf": "pdf",
    "xlsx": "xlsx",
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", sorted(FORMAT_FOR_SAMPLE.items()))
def test_detection_matches_real_files(sample_files, name, expected):
    path = sample_files[name]
    detection = filing.detect(path, filename=path.name)
    assert detection.format == expected
    assert detection.mime == filing.FORMAT_MIME[expected]
    assert detection.reason


def test_legacy_xls_is_detected_when_the_writer_is_available(sample_files):
    pytest.importorskip("xlwt")
    path = sample_files.get("xls")
    assert path is not None, "build_all should produce a legacy workbook"
    detection = filing.detect(path, filename=path.name)
    assert detection.format == "xls"
    assert detection.container["sheets"] >= 1


def test_content_wins_over_a_misleading_extension(sample_files, clean_workdir):
    renamed = clean_workdir / "actually_a_table.txt"
    renamed.write_bytes(sample_files["csv"].read_bytes())
    detection = filing.detect(renamed, filename=renamed.name)
    assert detection.format == "csv"
    assert detection.declared_format == "txt"
    assert any("TXT" in warning for warning in detection.warnings)


def test_binary_upload_is_rejected(clean_workdir):
    target = clean_workdir / "payload.exe"
    target.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + bytes(range(1, 200)))
    with pytest.raises(filing.UnsupportedFile):
        filing.detect(target, filename=target.name)


def test_empty_upload_is_rejected(clean_workdir):
    target = clean_workdir / "empty.csv"
    target.write_bytes(b"")
    with pytest.raises(filing.MalformedFile):
        filing.detect(target, filename=target.name)


def test_truncated_pdf_is_rejected(sample_files, clean_workdir):
    target = clean_workdir / "truncated.pdf"
    target.write_bytes(sample_files["pdf"].read_bytes()[:400])
    with pytest.raises(filing.MalformedFile) as info:
        filing.detect(target, filename=target.name)
    assert "pdf" == info.value.detected


def test_zip_that_is_not_a_document_is_rejected(clean_workdir):
    target = clean_workdir / "bundle.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("readme.txt", "not a word document")
    with pytest.raises(filing.UnsupportedFile):
        filing.detect(target, filename=target.name)


def test_compression_bomb_is_refused_before_expansion(clean_workdir):
    target = clean_workdir / "bomb.docx"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("word/document.xml", b"\x00" * (4 * 1024 * 1024))
    with pytest.raises(filing.UnsupportedFile) as info:
        filing.detect(target, filename=target.name)
    assert "ratio" in str(info.value)


def test_json_shape_and_depth_are_reported(sample_files):
    detection = filing.detect(sample_files["json"], filename="sample_records.json")
    assert detection.format == "json"
    assert detection.container["shape"] in ("array", "array of objects")
    assert detection.container["depth"] >= 1


# ---------------------------------------------------------------------------
# Storage containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/absolute/path/report.pdf",
        "C:\\Windows\\notepad.exe",
        "name\x00with-null.txt",
    ],
)
def test_display_names_cannot_carry_a_path(hostile):
    cleaned = storage.safe_display_name(hostile)
    assert "/" not in cleaned and "\\" not in cleaned and "\x00" not in cleaned
    assert cleaned == Path(cleaned).name


@pytest.mark.parametrize("bad_id", ["", "usr_short", "usr_" + "z" * 32, "../usr_" + "a" * 32])
def test_storage_rejects_malformed_user_ids(bad_id):
    with pytest.raises(storage.StorageError):
        storage.user_root(bad_id)


def test_a_user_cannot_resolve_another_users_file(tmp_path):
    owner = "usr_" + "a" * 32
    intruder = "usr_" + "b" * 32
    secret = storage.file_dir(owner) / "fl_secret.csv"
    secret.write_bytes(b"a,b\n1,2\n")

    assert storage.resolve_owned(str(secret), owner) == secret.resolve()
    with pytest.raises(storage.StorageError):
        storage.resolve_owned(str(secret), intruder)


def test_paths_outside_the_storage_root_are_refused(tmp_path):
    escaped = tmp_path / "outside.txt"
    escaped.write_text("not ours")
    with pytest.raises(storage.StorageError):
        storage.resolve_owned(str(escaped), "usr_" + "c" * 32)


# ---------------------------------------------------------------------------
# Passwords and sessions
# ---------------------------------------------------------------------------


def test_passwords_are_salted_and_verified():
    first_hash, first_salt, iterations = security.hash_password("correct-horse-1")
    second_hash, second_salt, _ = security.hash_password("correct-horse-1")
    assert first_hash != second_hash, "a random salt must make hashes unique"
    assert first_salt != second_salt
    assert iterations > 0
    assert security.verify_password("correct-horse-1", first_hash, first_salt)
    assert not security.verify_password("wrong-password", first_hash, first_salt)


@pytest.mark.parametrize("email", ["no-at-sign", "a@b", "@example.com", "spaces in@example.com"])
def test_invalid_emails_are_refused(email):
    with pytest.raises(ValueError):
        security.validate_email(email)


def test_short_passwords_are_refused():
    with pytest.raises(ValueError):
        security.validate_password("short")


def test_session_tokens_are_never_stored_in_plaintext(app):
    """The database row must not be a usable session token."""
    with db.session() as conn:
        user_id = db.new_id("usr")
        conn.execute(
            "INSERT INTO users (id, email, pw_hash, pw_salt, iterations, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, f"token-{user_id}@example.com", "hash", "salt", 14, db.now()),
        )
        token = security.create_session(conn, user_id)
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE user_id=?", (user_id,)
        ).fetchone()
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    assert row is not None
    assert row["token_hash"] != token
    assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


def test_reduction_uses_the_documented_formula():
    # ((Baseline - Optimized) / Baseline) * 100, for lower-is-better metrics only.
    assert metrics.reduction_pct(200, 150) == pytest.approx(25.0)
    assert metrics.reduction_pct(100, 100) == pytest.approx(0.0)
    assert metrics.reduction_pct(100, 130) == pytest.approx(-30.0), "a regression is negative, not clamped"


@pytest.mark.parametrize("baseline,optimized", [(0, 10), (None, 10), (10, None)])
def test_reduction_is_undefined_rather_than_invented(baseline, optimized):
    assert metrics.reduction_pct(baseline, optimized) is None


def test_throughput_is_reported_separately_from_reduction():
    baseline = metrics.throughput(1000, 2.0)
    optimized = metrics.throughput(1000, 1.0)
    assert baseline == pytest.approx(500.0)
    assert optimized == pytest.approx(1000.0)
    assert metrics.throughput_improvement_pct(1000, 2.0, 1000, 1.0) == pytest.approx(100.0)
    assert metrics.throughput_improvement_pct(1000, 1.0, 1000, 2.0) == pytest.approx(-50.0)
    assert metrics.throughput_improvement_pct(1000, 0.0, 1000, 1.0) is None


# ---------------------------------------------------------------------------
# Engine and backend registry
# ---------------------------------------------------------------------------


def test_every_supported_format_has_an_engine():
    assert set(supported_formats()) == {"csv", "xlsx", "xls", "docx", "pdf", "json", "txt"}
    for fmt in supported_formats():
        engine = engine_for(fmt)
        assert engine.name
        assert engine.baseline_backend != engine.optimized_backend


def test_declared_backends_refuse_to_run():
    catalog = backend_catalog()
    assert set(catalog["implemented"]) == {"local-rowwise", "local-vectorized"}
    assert set(catalog["declared"]) == {"spark", "flink"}
    by_slug = {spec["slug"]: spec for spec in catalog["backends"]}
    for slug in catalog["declared"]:
        # A declared backend has no runtime wiring and refuses to be used.
        with pytest.raises(BackendUnavailable):
            get_backend(slug)
        assert by_slug[slug]["implemented"] is False
        assert by_slug[slug]["status"] == "declared"
        assert by_slug[slug]["reason_unavailable"]
        assert by_slug[slug]["contract"], "a declared adapter must document its contract"
    for slug in catalog["implemented"]:
        assert get_backend(slug).implemented is True
