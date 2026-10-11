# SPADE Universal Data Optimizer

A self-hosted web platform where a teacher or client uploads their own PDF, Excel,
CSV, Word, JSON or text file and gets back a **measured** before-and-after
comparison: what was found, what was changed, how much smaller the file is, how
long each execution path took, and whether the output still contains the same
data as the input.

Everything reported by the application is measured in the process that did the
work. Where a number cannot be measured reliably, SPADE says so instead of
printing an estimate.

---

## 1. Quick start

```bash
cd "PBL IDEA NAYANTIKA"

python -m venv .venv                     # already present in this checkout
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
#   (Linux/macOS: source .venv/bin/activate && pip install ...)

.venv/Scripts/python.exe run.py          # http://127.0.0.1:8000
```

Options: `--host 0.0.0.0 --port 8080 --reload`. The server binds to loopback by
default; a private deployment is not exposed unless you ask for it.

Then open <http://127.0.0.1:8000>, choose **Create account**, and upload a file.
Sample files to try are produced by:

```bash
.venv/Scripts/python.exe scripts/make_samples.py     # writes samples/
```

### Tests

```bash
.venv/Scripts/python.exe -m pytest            # 79 tests, ~70 s
.venv/Scripts/python.exe scripts/api_smoke.py # in-process HTTP journey, 8 formats
.venv/Scripts/python.exe scripts/live_e2e.py  # the same journey against a *running* server
```

The suite uploads real files of every format, waits for the real analysis job,
runs the real optimization job **in the isolated worker subprocess**, and checks
the downloaded artifacts. It does not call an engine directly to fake a result.

### Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPADE_DATA_DIR` | `./data` | storage root for the database and private files |
| `SPADE_MAX_UPLOAD_BYTES` | 512 MiB | hard ceiling; users may only lower their own limit |
| `SPADE_MAX_PROCESSING_SECONDS` | 300 | hard ceiling for the per-job timeout |
| `SPADE_SESSION_SECONDS` | 14 days | session lifetime |
| `SPADE_MAX_ANALYSIS_ROWS` | 5,000,000 | hard ceiling for the analysis scan limit |
| `SPADE_MAX_CONCURRENT_JOBS` | 2 | worker processes run at once |

---

## 2. What works today

### Upload and detection

* Drag-and-drop **and** file browser, with progress, size, detected format and
  processing status.
* The **content** decides the format, never the file name: a CSV renamed to
  `.txt` is analysed as CSV and the mismatch is reported as a warning.
* Validated containers before any parser sees them: PDF signature + page tree,
  OOXML parts, OLE2 legacy workbook, JSON parse (with a nesting-depth limit),
  delimiter sniffing for text. Unsupported or malformed uploads are refused with
  a readable reason (`415`), never a stack trace.
* Guarded against zip bombs (entry count, expansion ratio, total uncompressed
  size, CRC check) and against oversized uploads (`413`) with streaming
  abort, so a huge file cannot exhaust memory.
* Nothing uploaded is ever executed. Uploaded content is only ever opened by the
  isolated worker process.

### Automatic analysis

| Format | What is measured |
| --- | --- |
| CSV / TSV | rows, columns, cells, inferred column types, missing values, duplicates, blank and ragged rows, whitespace and numeric-as-text cells, distinct counts, estimated in-memory size |
| XLSX / XLS | the above per sheet, plus sheet names, selected sheet, container parts |
| PDF | pages, pages with a text layer, extracted characters per page, scanned/OCR-needed detection, embedded images, image bytes and dimensions, recompressible JPEG payload, metadata, annotations, outline entries |
| DOCX | paragraphs, tables, words, empty paragraphs, package parts, embedded media, orphaned media parts, core properties |
| JSON | validity, shape (object / array / array of objects), nesting depth, record count, repeated records, empty fields |
| TXT | lines, blank lines, repeated lines, characters, encoding, CRLF/LF/lone-CR counts, trailing whitespace, tabs, longest line |

The analysis is shown **before** anything is transformed, together with the
operation plan, each operation's rationale, and the estimated effect.

### Operation plan (nothing destructive is automatic)

| Format | Selected by default | Offered, off by default | Requires explicit approval |
| --- | --- | --- | --- |
| CSV/TSV/XLSX/XLS | `compact_output`, `drop_exact_duplicates`, `trim_whitespace`, `canonicalize_numbers` | `drop_empty_rows`, `drop_empty_columns` | `drop_duplicates_on_key`, `drop_columns`, `normalize_headers` |
| PDF | `compress_content_streams`, `dedupe_objects` | – | `recompress_images`, `strip_metadata` |
| DOCX | `repack_max_compression` | – | `remove_unused_media`, `strip_metadata`, `remove_empty_paragraphs` |
| JSON | `normalize_formatting`, `drop_duplicate_records` | – | `drop_empty_fields`, `select_fields` |
| TXT | `normalize_line_endings`, `collapse_blank_lines` | `strip_trailing_whitespace`, `strip_bom` | `drop_duplicate_lines` |

Defaults are derived from the measured profile, so an operation whose finding is
zero is not pre-selected. Anything marked "changes data" is never applied
automatically and every operation can be toggled off in the interface.

### The pipeline

Seven separate, individually timed stages:
`ingest → validate → profile → plan → execute → verify → report`.

Two real execution paths run over the same resolved plan:

* **baseline** — `local-rowwise`: row-at-a-time pure Python that materialises the
  whole table. The honest reference implementation.
* **optimized** — `local-vectorized`: chunked, column-oriented pandas/Numpy
  execution with a bounded deduplication state, per-column normalisation caches
  and streaming output, so peak memory does not scale with file size.

Both paths consume the same plan, so they can differ only in *how* they compute,
never in *what* they compute. CSV/TSV and tabular files are read in bounded
chunks (`chunk_rows`, 1,000–1,000,000, default 50,000).

Apache **Spark** and **Flink** adapters are declared in the API and the UI with
their contract and requirements, and they **refuse to run**
(`BackendUnavailable`). No job is ever attributed to them. See
`spade/pipeline/backends.py`.

### Verification (before anything is downloadable)

| Format | Checks |
| --- | --- |
| CSV/Excel | `output_written`, `output_parses`, `columns_match_plan`, `no_invented_records`, `records_retained`, `baseline_matches_optimized`, `idempotent`, plus `ragged_rows_rectified` when the input was not rectangular |
| PDF | `page_count_preserved`, `text_preserved`, `images_preserved`, `baseline_equivalent` |
| DOCX | `package_valid`, `text_preserved`, `document_part_unchanged`, `no_unexpected_part_loss`, `media_preserved`, `reopens_in_word_library`, `baseline_equivalent` |
| JSON | `output_parses`, `duplicates_removed_as_declared`, `baseline_matches_optimized` |
| TXT | `output_readable`, `line_endings_normalised`, `line_content_matches_plan`, `baseline_matches_optimized`, `no_content_invented` |

A failed verification is reported as a failure and the run is not presented as a
success. `exact_comparison` is `false` when the row-level comparison was capped
(200,000 rows per file), so a sampled comparison is never called exact.

### Performance comparison

`Reduction (%) = ((Baseline − Optimized) / Baseline) × 100`, used **only** for
lower-is-better metrics (file size, wall clock, CPU time, peak Python heap).
Throughput is higher-is-better and is computed and labelled separately.

* Reported only when measurable: below 50 ms both paths are dominated by process
  and file-system overhead, so no time percentage is claimed; peak-heap
  differences below 1 MB are labelled interpreter overhead, not a result.
* `peak_python_heap_bytes` is a `tracemalloc` peak and excludes memory held
  outside the Python allocator; `peak_rss_bytes` is reported from `psutil`
  (absent, with a reason, when psutil is not installed).
* Regressions are shown as regressions, with the metric and the percentage sign
  kept. Trade-offs are spelled out (for example: a smaller PDF can cost CPU time).

Real example from this environment (live server, 8,699-row CSV, run through the
interface): 543.99 KB → 468.29 KB (**13.92 % smaller**), 1.623 s → 1.344 s
(**17.17 % faster**), throughput 5,361 → 6,473 records/s (+20.73 %), peak Python
heap 11.51 MB → 7.83 MB, verification **passed**, 699 redundant records removed.
A TXT sample reports a 0.59 % size change with no time claim at all.

One finding worth stating plainly, because the platform reports it instead of
hiding it: **the row-wise baseline is often faster on small files.** With ~1,500
rows the pure-Python path usually wins (pandas start-up and per-chunk overhead
dominate), and the vectorized path starts winning from a few thousand rows upward
(the 8,699-row run above is 17 % faster optimized). Which one wins is measured
per file, and a slower result is published as a regression with its percentage,
never smoothed into a success story. The size reduction is independent of that
and holds at every size.

### Results, downloads and housekeeping

* Optimized output **plus** a JSON report and a Markdown report, with the
  transformations applied, warnings, findings, both measurement paths and the
  environment (Python, platform, library versions).
* The original upload is preserved byte for byte and stays downloadable.
* When no operation is selected, or none of them improves the file, SPADE
  returns the **unchanged original** (byte-identical) and makes no size claim.
* Delete an upload (and its runs) or a single run from the history view.
* Dashboard, recent activity, and a processing history with per-run status.

### Security posture

* Server-side sessions: scrypt-hashed passwords with per-user salt; opaque
  256-bit session tokens in HTTP-only `SameSite=Lax` cookies; only the SHA-256 of
  a token is stored.
* Every file, job and artifact lookup is scoped by the authenticated user id in
  SQL, so one account cannot read another's data (covered by tests).
* Same-origin guard on every state-changing request, on top of the cookie policy.
* Per-user storage directories with random file names, resolved and asserted
  inside the storage root on every access; path traversal is refused and file
  names are sanitised before use in `Content-Disposition`.
* Processing happens in a separate interpreter with a minimal environment, a hard
  timeout, an optional address-space cap, and a bounded output log. A crashing or
  hanging parser takes down only that process.
* Logs and the audit trail record identifiers and outcomes, never document
  contents. **Nothing is ever sent to an external service**, and there is no AI
  integration in this build.

---

## 3. Honest limitations

* **No OCR.** A scanned PDF is detected and reported as having no text layer;
  SPADE will not claim a text-preserving compression for it.
* **Legacy `.xls` output is upgraded to `.xlsx`.** No maintained pure-Python
  library writes the legacy format, so the container is upgraded and the values
  are carried across unchanged; the analysis note says so.
* **PDF image recompression is lossy** and is never automatic. It is offered only
  when JPEG payloads are present, and dimensions plus image count are verified
  afterwards. No OCR, no image upscaling, no font subsetting.
* **Deselecting `compact_output` does not restore the original byte layout.**
  Both execution paths write through one re-encoding writer, so the container is
  always normalised (line endings, minimal quoting). Deselecting the operation
  removes the *claim*, not the re-encoding.
* **Text deduplication is opt-in** because repeated lines are frequently
  meaningful; the count is reported instead.
* **Measurement scope.** Wall clock and CPU time come from the worker process;
  peak heap is a Python-heap measure (a lower bound on true memory); RSS needs
  `psutil`. Small files produce no time percentage by design.
* **Single-node storage.** SQLite plus local files; no object storage, no
  horizontal scale-out, and the documented Spark/Flink adapters are not wired up.
* **Accounts are basic:** no email verification, no password reset, no 2FA, no
  admin role, no per-account quotas beyond the size ceiling. Sessions are
  server-side and revocable by signing out.
* **Not a data warehouse.** The pipeline is single-machine and bounded by the
  per-job timeout; a 30,000-row CSV is comfortable, a multi-gigabyte export is
  not the target of this build.
* **The `AI Command Center` page** (`/command-center`, linked in the sidebar) is a
  self-contained static demo of an agent control-room UI. It makes no API calls,
  stores nothing on the server, and is labelled as simulated in the page itself.
  It is not part of the data pipeline.

---

## 4. Layout

```
run.py                      launcher (host/port/reload)
spade/
  config.py                 limits, storage paths, environment sampling
  db.py                     SQLite schema and helpers
  security.py               scrypt passwords, sessions, same-origin guard
  storage.py                per-user storage with path containment
  filing.py                 content-based detection and container validation
  worker.py                 isolated worker entry point
  jobs.py                   supervisor: subprocess, timeout, artifacts
  samples.py                deterministic sample generators (real formats)
  main.py                   FastAPI app: auth, files, jobs, artifacts, settings
  pipeline/
    spec.py                 stages, operations, contexts
    metrics.py              Meter (wall/CPU/heap/RSS), reduction and throughput maths
    backends.py             implemented + declared execution backends
    runner.py               staged runner, comparison, outcome classification, reports
    report.py               Markdown rendering of the same result document
    engines/                tabular.py, pdf.py, word.py, structured.py, text.py
static/                     dashboard (index.html, css, js modules)
tests/                      pytest suite (detection, security, API, formats, edge cases)
scripts/                    api_smoke.py, live_e2e.py, make_samples.py, probes
data/                       runtime storage (database, private uploads)
```

---

## 5. API summary

All routes are under `/api` and require a session except `health`, `register`
and `login`. Mutating routes also require a same-origin request.

```
GET    /api/health                  GET  /api/engines          GET  /api/overview
POST   /api/auth/register           POST /api/auth/login       POST /api/auth/logout
GET    /api/auth/me                 GET  /api/settings         PUT  /api/settings
POST   /api/files                   GET  /api/files            GET  /api/files/{id}
GET    /api/files/{id}/download     DELETE /api/files/{id}
POST   /api/jobs                    GET  /api/jobs             GET  /api/jobs/{id}
DELETE /api/jobs/{id}               GET  /api/jobs/{id}/artifacts/{artifact_id}
```

Interactive docs: <http://127.0.0.1:8000/api/docs>

---

## 6. Future integrations

* **Spark / Flink adapters.** The contract is documented in
  `spade/pipeline/backends.py`: implement `run(ctx, operations, variant) ->
  ExecOutcome`, reproduce the local semantics exactly, and report per-stage
  timings plus partition/shuffle (Spark) or watermark/checkpoint (Flink) figures
  in `ExecOutcome.extra`. Jobs must keep reporting measured record counts.
* **OCR** (Tesseract) for scanned PDFs, reported as a distinct, opt-in operation.
* **Password reset / email verification**, and an administrator view for quotas.
* **Object storage** (S3-compatible) instead of local directories, for multi-node
  deployment; the storage layer is already the only place that touches paths.
* **Scheduled and streaming ingestion** so a folder or a Kafka topic can be
  watched and optimised as files arrive.
* **Bulk export** of a whole run history as a zip or a CSV of measurements.
* **More formats**: ODS, Parquet and Avro are natural next engines; the registry
  in `spade/pipeline/engines/__init__.py` is the only place that needs a new
  entry.
