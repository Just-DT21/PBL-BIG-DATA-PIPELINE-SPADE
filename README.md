# SPADE Universal Data Optimizer

A self-hosted web platform where a teacher or client uploads their own PDF, Excel,
CSV, Word, JSON or text file and gets back a **measured** before-and-after
comparison: what was found, what was changed, how much smaller the file is, how
long each execution path took, and whether the output still contains the same
data as the input.

It also contains **NAMI Optimizer**, an adaptive, memory-aware execution pipeline
that is benchmarked against the SPADE reference path on the same input, with the
same resolved plan, and with every output verified row by row against the
reference output.

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
.venv/Scripts/python.exe -m pytest            # 122 tests, ~2 min
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
| `SPADE_NAMI_MEMORY_BUDGET_MIB` | 512 | NAMI's dedupe-state budget; above it the partitioned spool policy is chosen |
| `SPADE_NAMI_MAX_BENCHMARK_SECONDS` | 900 | supervisor ceiling for one multi-trial benchmark |

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

## 3. NAMI Optimizer — the benchmark lab

**NAMI** stands for *Native Adaptive Memory-aware Intelligence*. It is not a
relabelled copy of the SPADE path: it profiles the workload, chooses an execution
strategy from the measured profile, and then has to prove its output is the
output SPADE was asked to produce.

### 3.1 The two systems

| | A · SPADE reference | B · NAMI Optimizer |
| --- | --- | --- |
| Backend | `local-rowwise` | selected policy (below) |
| Strategy | row-at-a-time pure Python, whole table materialised, one dict per row | profiled, then vectorized / chunked / spooled |
| Role | honest baseline | candidate |

### 3.2 Adaptive policy engine

The decision is a pure function of the measured profile and the resolved plan, and
every threshold is documented, configurable per request, and echoed back inside
the result so a reviewer can reproduce the choice.

| Policy | Chosen when | Memory behaviour |
| --- | --- | --- |
| `nami-p1-inmemory-vectorized` | rows ≤ `inmemory_max_rows` (25 000) **and** estimated table ≤ `inmemory_max_bytes` (64 MiB) | holds the whole table, bounded by those two ceilings |
| `nami-p2-chunked-streaming` | above the in-memory ceilings, and the dedupe state fits `memory_budget_bytes`, or no operation needs cross-row state | chunk size + one 128-bit key per distinct row |
| `nami-p3-partitioned-spool` | the plan needs global dedupe state **and** it exceeds the budget | digests spilled to partition files; one partition + a one-byte-per-row survivor array |
| `nami-p4-streaming-python` | pandas cannot be imported | bounded blocks, standard library only, reported as a fallback |

`spade/nami/executors.py` implements all four, and each one applies the identical
transformation contract: same reader, same row normalisation, same trim /
canonicalisation rules, same "keep the first occurrence" order, same column
projection, and the *same container writer* as the SPADE paths. A byte difference
can therefore only come from the transformation itself.

### 3.3 What the benchmark measures

* **One file, one resolved plan, two systems, one process.** The plan is resolved
  once, before any measurement.
* **Interleaved trials.** Trial *n* alternates which system runs first, so a warm
  page cache cannot systematically favour either one.
* **Discarded warm-up pair** whenever more than one trial is requested, and the
  result says whether a warm-up ran.
* **Medians as the headline**, with mean, standard deviation, minimum and maximum
  across trials, plus the individual trial samples in `benchmark_trials.csv`.
* **Two timing figures.** *End-to-end* is the honest cost of being adaptive (it
  includes NAMI's profiling pass and the policy decision); *execution-only*
  compares the transformation work like for like. Both are labelled.
* **Resolution and noise guards.** Below 50 ms per trial no time percentage is
  reported, and a difference smaller than the combined trial-to-trial spread is
  reported as *within measurement noise*, not as an improvement.
* `Reduction (%) = ((Baseline - Optimized) / Baseline) × 100`, used only where
  lower is better; throughput is higher-is-better and computed separately.

### 3.4 Correctness validation (every run)

`spade/nami/equivalence.py` re-reads both artifacts from disk and checks: both
exist, headers match, row counts match, the row streams are identical **in value
and order** (streaming SHA-256 over canonical rows), the schema matches the
resolved plan, the row accounting closes (`in − blank − duplicate = out`), and a
bounded window of output rows exists in the input. A failed check is reported as
a failure; nothing is swallowed, and a failed run keeps its error in the history.

### 3.5 Interface

**NAMI Optimizer** in the sidebar (*Pipeline Intelligence Lab*) gives you: the
SPADE-vs-NAMI comparison header, dataset selection, automatic workload profiling
with a schema preview, benchmark configuration (dataset, policy override,
trials, warm-up), an explicit *Profile & plan* action, a *Run comparison* action
with live status polling, side-by-side benchmark cards, five before/after charts,
the reduction table, the correctness checks, the policy explanation with the
thresholds in force, downloadable artifacts, benchmark history, and a methodology
and limitations panel. Every value on the page comes from a backend response.

### 3.6 Scope, stated plainly

The benchmark engine covers **tabular workloads** (CSV/TSV, XLSX, XLS). PDF,
Word, JSON and text are still analysed and optimised by the standard SPADE
pipeline and are not benchmarked here; `spade/nami/support.py` is the one place
that states that boundary and the API returns it. No cluster, executor, partition
or Kafka resource is involved anywhere, and none is claimed.

### 3.7 Reproducing a run

```bash
.venv/Scripts/python.exe -m pytest tests/test_nami.py -q      # 42 tests
.venv/Scripts/python.exe scripts/probe_nami.py 4000           # every policy vs the reference, no API
```

`probe_nami.py` prints the measured comparison and the correctness verdict for
the automatic policy and for each forced policy, which is the fastest way to see
an equivalence defect while editing.

---

## 4. Honest limitations

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
* **Adaptivity has a real cost.** NAMI scans the input once to profile the
  workload before transforming it, so on a few thousand rows its *end-to-end*
  time is often worse than SPADE's fixed path even when its *execution-only* time
  is clearly better. That is why the lab publishes both figures, why a warm-up
  pair is discarded, and why a difference smaller than the trial spread is
  reported as noise rather than as a result.
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
* **The `AI Command Center` page** (`/command-center`, reachable by URL but no longer
  in the sidebar) is a
  self-contained static demo of an agent control-room UI. It makes no API calls,
  stores nothing on the server, and is labelled as simulated in the page itself.
  It is not part of the data pipeline.

---

## 5. Layout

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
  nami/
    profile.py              workload profiler (rows, types, missing, duplicates, estimates)
    policy.py               documented adaptive policy engine and thresholds
    executors.py            P1 in-memory, P2 chunked, P3 partitioned spool, P4 streaming
    equivalence.py          output-equivalence and row-accounting verification
    benchmark.py            interleaved multi-trial harness, aggregation, comparison
    runner.py               isolated-worker entry point (preflight and benchmark)
    report.py               Markdown benchmark report
    support.py              the formats NAMI does and does not cover, and why
  pipeline/
    spec.py                 stages, operations, contexts
    metrics.py              Meter (wall/CPU/heap/RSS), reduction and throughput maths
    backends.py             implemented + declared execution backends
    runner.py               staged runner, comparison, outcome classification, reports
    report.py               Markdown rendering of the same result document
    engines/                tabular.py, pdf.py, word.py, structured.py, text.py
static/                     dashboard (index.html, css, js modules)
                            js/lab.js is the NAMI Optimizer lab view
tests/                      pytest suite (detection, security, API, formats, edge cases)
scripts/                    api_smoke.py, live_e2e.py, make_samples.py, probes
data/                       runtime storage (database, private uploads)
```

---

## 6. API summary

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

# NAMI Optimizer
GET    /api/nami/policies                    POST /api/nami/preflight
POST   /api/nami/benchmarks                  GET  /api/nami/benchmarks
GET    /api/nami/benchmarks/{id}             DELETE /api/nami/benchmarks/{id}
GET    /api/nami/benchmarks/{id}/artifacts/{artifact_id}
```

Interactive docs: <http://127.0.0.1:8000/api/docs>

---

## 7. Future integrations

* **NAMI policies for non-tabular formats.** The benchmark engine is deliberately
  scoped to record tables today (`spade/nami/support.py` says so and the API
  returns it). Extending the policy model to JSON record streams would mean
  defining a second profiling contract for the structured engine, which is a
  real piece of work rather than a label change.
* **A distributed NAMI executor.** The policy engine currently selects *local*
  strategies. A `spark` or `flink` strategy would slot in beside the existing
  ones, and the benchmark already measures stage timings, so the comparison would
  extend without changing the interface.
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

---

## 8. Deploying

The deployment target is Render, with the start command unchanged:

```
python run.py --host 0.0.0.0 --port $PORT
```

Notes for a real deployment:

* **Storage must be writable and persistent.** `SPADE_DATA_DIR` defaults to
  `./data`; on Render, point it at a mounted disk (for example
  `SPADE_DATA_DIR=/var/data`) or every upload and the database are lost on the
  next deploy. `spade/config.py` creates the directories it needs at startup, and
  it never assumes a path it has not been given.
* **Nothing else is required.** No external service, no API key, no queue: the
  web process stores files privately under the account that uploaded them and the
  document parsing happens in an isolated worker subprocess.
* **Schema changes are migrations.** `db.init_db()` runs `CREATE TABLE IF NOT
  EXISTS` and then applies explicit `ALTER TABLE` migrations recorded in
  `spade/db.py` (`MIGRATIONS`), so an existing database keeps its data across an
  upgrade.
* **Do not commit private data.** `.gitignore` excludes `data/`, `logs/`, `.env`
  and the virtual environment. Uploaded documents, the SQLite database and job
  artifacts must never enter the repository.

### Pushing the change

This working copy is not a git checkout (there is no `.git` directory here), so
publish it by copying the tracked files into your clone of the project repository
and committing there:

```bash
# from your clone of PBL-BIG-DATA-PIPELINE-SPADE
git status                                    # confirm a clean starting point
git switch -c nami-optimizer                  # keep main intact while you review
# copy this working copy over the clone, excluding data/, logs/, .venv/,
# .pytest_cache/ and .freebuff/ (see .gitignore)
git add -A                                    # .gitignore keeps the private data out
git status --short                            # verify nothing private is staged
git commit -m "Add NAMI Optimizer benchmark lab: adaptive policy engine, equivalence verification, multi-trial comparison"
git push -u origin nami-optimizer             # then open a pull request
```

### Verifying the deployment

1. Open <https://pbl-big-data-pipeline-spade.onrender.com/> and sign in.
2. Confirm the standard pipeline still works: upload `samples/sample_records.csv`,
   wait for the analysis, run the optimisation, download the output.
3. Open **NAMI Optimizer** in the sidebar.
4. Pick the uploaded file. Profiling runs automatically and shows the policy it
   chose, with the reasons and the thresholds in force.
5. Set trials to 1 and un-tick the warm-up for a first quick run, then
   **Run comparison**.
6. Check the result: the reduction table (execution-only and end-to-end), the
   correctness panel (all rows identical to the reference output), the policy
   explanation, and the downloads (optimised output, reference artifact,
   benchmark report JSON/Markdown/CSV).
7. Re-run with 3 trials and the warm-up enabled for the numbers you would quote.
   A Render instance is slower and more contended than a laptop, so expect wider
   trial spreads there; the page reports a difference smaller than that spread as
   *within measurement noise* rather than as an improvement.
