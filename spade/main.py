"""SPADE web application.

Security posture
----------------
* Every API route except registration, sign-in and health requires a session.
* Every file, job and artifact query is scoped by the authenticated user id in
  SQL, so ownership is enforced in the data layer rather than the template.
* Uploaded bytes are stored under a random name inside the owner's directory;
  the original name is only ever used as a display string.
* Documents are parsed exclusively by the isolated worker process, never by the
  web process. Nothing uploaded is ever executed.
* Requests that would change state must be same-origin, on top of SameSite=Lax
  cookies.
* Logs record identifiers and outcomes, never document contents.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, filing, jobs, security, storage
from .nami import policy as nami_policy
from .nami import support as nami_support
from .nami.benchmark import LIMITATIONS as NAMI_LIMITATIONS
from .nami.benchmark import METHODOLOGY as NAMI_METHODOLOGY
from .nami.benchmark import NAMI_SYSTEM, REFERENCE_SYSTEM
from .pipeline.backends import backend_catalog
from .pipeline.engines import supported_formats
from .pipeline.metrics import environment, measurement_scope

DEMO_LIMIT_NOTE = (
    "Uploads are stored privately under your own directory. Nothing is sent to an external service."
)

#: A benchmark runs both systems several times in one worker, so its supervisor
#: timeout is derived from the per-run limit and then capped here.
NAMI_MAX_BENCHMARK_SECONDS = int(os.environ.get("SPADE_NAMI_MAX_BENCHMARK_SECONDS", 900))
NAMI_MAX_TRIALS = 7


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    with db.session() as conn:
        security.purge_expired_sessions(conn)
    yield
    jobs.shutdown()


app = FastAPI(
    title="SPADE Universal Data Optimizer",
    version="1.0.0",
    description="Private analyser and optimiser for PDF, Excel, CSV, Word, JSON and text files.",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

api = APIRouter(prefix="/api")

User = Depends(security.get_current_user)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class JobRequest(BaseModel):
    file_id: str
    operations: list[str] | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class BenchmarkRequest(BaseModel):
    """A NAMI benchmark request. ``operations`` defaults to the recommended plan."""

    file_id: str
    operations: list[str] | None = None
    trials: int = 3
    warmup: bool = True
    force_policy: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class PreflightRequest(BaseModel):
    file_id: str
    operations: list[str] | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class SettingsPayload(BaseModel):
    max_upload_bytes: int | None = None
    chunk_rows: int | None = None
    processing_timeout_seconds: int | None = None
    max_analysis_rows: int | None = None
    retain_originals: int | None = None


# ---------------------------------------------------------------------------
# Health and metadata
# ---------------------------------------------------------------------------


@api.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "supported_formats": supported_formats(),
        "backends": backend_catalog(),
        "environment": environment(),
    }


@api.get("/engines")
def engines() -> dict[str, Any]:
    """Which formats and execution engines exist, and which are only declared."""
    return {
        "formats": supported_formats(),
        "format_labels": {key: filing.FORMAT_LABELS[key] for key in sorted(filing.FORMAT_LABELS)},
        "extensions": sorted({ext.upper() for ext in filing.EXT_ALIASES}),
        "backends": backend_catalog(),
        "nami": {
            **nami_support.catalog(),
            "policies": [spec.id for spec in nami_policy.POLICY_CATALOG],
            "max_trials": NAMI_MAX_TRIALS,
            "max_benchmark_seconds": NAMI_MAX_BENCHMARK_SECONDS,
        },
        "measurement_scope": measurement_scope(),
        "limits": config.hard_limits(),
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@api.post("/auth/register")
def register(payload: Credentials, request: Request, response: Response) -> dict[str, Any]:
    security.enforce_same_origin(request)
    try:
        email = security.validate_email(payload.email)
        security.validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    password_hash, salt, iterations = security.hash_password(payload.password)
    user_id = db.new_id("usr")
    with db.session() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail="An account with that email already exists.")
        conn.execute(
            "INSERT INTO users (id, email, pw_hash, pw_salt, iterations, is_active, created_at)"
            " VALUES (?,?,?,?,?,1,?)",
            (user_id, email, password_hash, salt, iterations, db.now()),
        )
        db.audit(conn, user_id, "register", "account created")
        token = security.create_session(conn, user_id)
    response.status_code = 201
    security.set_session_cookie(response, token)
    return {"user": {"id": user_id, "email": email}, "created": True}


@api.post("/auth/login")
def login(payload: Credentials, request: Request, response: Response) -> dict[str, Any]:
    security.enforce_same_origin(request)
    email = (payload.email or "").strip().lower()
    security.check_throttle(request, email)
    with db.session() as conn:
        row = conn.execute(
            "SELECT id, email, pw_hash, pw_salt, is_active FROM users WHERE email=?", (email,)
        ).fetchone()
        if row is None or not security.verify_password(payload.password, row["pw_hash"], row["pw_salt"]):
            security.record_failure(request, email)
            db.audit(conn, row["id"] if row else None, "login_failed", "invalid credentials")
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        if not row["is_active"]:
            raise HTTPException(status_code=403, detail="This account is disabled.")
        token = security.create_session(conn, row["id"])
        db.audit(conn, row["id"], "login", "session started")
    security.clear_failures(request, email)
    security.set_session_cookie(response, token)
    return {"user": {"id": row["id"], "email": row["email"]}}


@api.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    user: security.CurrentUser = User,
) -> dict[str, Any]:
    security.enforce_same_origin(request)
    token = request.cookies.get(config.SESSION_COOKIE)
    with db.session() as conn:
        if token:
            security.destroy_session(conn, token)
        db.audit(conn, user.id, "logout", "session ended")
    security.clear_session_cookie(response)
    return {"signed_out": True}


@api.get("/auth/me")
def me(user: security.CurrentUser = User) -> dict[str, Any]:
    with db.session() as conn:
        settings = db.all_user_settings(conn, user.id)
    return {"user": {"id": user.id, "email": user.email}, "settings": settings}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@api.get("/settings")
def read_settings(user: security.CurrentUser = User) -> dict[str, Any]:
    with db.session() as conn:
        settings = db.all_user_settings(conn, user.id)
        usage = storage.disk_usage(user.id)
    return {
        "settings": settings,
        "hard_limits": config.hard_limits(),
        "bounds": {
            key: {"default": default, "min": low, "max": high}
            for key, (default, low, high) in config.USER_SETTINGS.items()
        },
        "disk_usage_bytes": usage,
        "notes": [DEMO_LIMIT_NOTE],
    }


@api.put("/settings")
def write_settings(
    payload: SettingsPayload,
    request: Request,
    user: security.CurrentUser = User,
) -> dict[str, Any]:
    security.enforce_same_origin(request)
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        with db.session() as conn:
            settings = db.set_user_settings(conn, user.id, values)
            db.audit(conn, user.id, "settings_updated", ", ".join(sorted(values)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"settings": settings}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _max_upload_for(user_id: str) -> int:
    with db.session() as conn:
        return db.get_user_setting(conn, user_id, "max_upload_bytes")


def _settings_for(user_id: str) -> dict[str, int]:
    with db.session() as conn:
        return db.all_user_settings(conn, user_id)


@api.post("/files")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    user: security.CurrentUser = User,
) -> JSONResponse:
    security.enforce_same_origin(request)
    limit = _max_upload_for(user.id)

    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > limit * 1.05 + 4096:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That upload is larger than your configured limit of {limit:,} bytes. "
                "Raise the limit in Settings or upload a smaller file."
            ),
        )

    original_name = storage.safe_display_name(file.filename or "upload")
    declared = filing.declared_format(original_name)
    suffix = Path(original_name).suffix.lstrip(".")[:8] or "bin"
    file_id = db.new_id("fl")

    try:
        stored = storage.save_upload(user.id, file_id, suffix, file.file, limit)
    except storage.UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except storage.StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()

    try:
        detection = filing.detect(stored.path, filename=original_name, size_bytes=stored.size_bytes)
    except (filing.UnsupportedFile, filing.MalformedFile) as exc:
        storage.delete_path(stored.path)
        with db.session() as conn:
            db.audit(conn, user.id, "upload_rejected", f"format={getattr(exc, 'detected', 'unknown')}")
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    with db.session() as conn:
        conn.execute(
            "INSERT INTO files (id, user_id, original_name, stored_path, declared_ext, detected_format,"
            " content_type, size_bytes, sha256, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                file_id,
                user.id,
                original_name,
                str(stored.path.resolve().relative_to(config.DATA_DIR)).replace("\\", "/"),
                declared or "",
                detection.format,
                detection.mime,
                stored.size_bytes,
                stored.sha256,
                "queued",
                db.now(),
            ),
        )
        db.audit(conn, user.id, "upload", f"file={file_id} format={detection.format} bytes={stored.size_bytes}")

    _start_analysis(user.id, file_id, stored.path, original_name)
    return JSONResponse(status_code=201, content=_file_payload(file_id, user.id))


def _start_analysis(user_id: str, file_id: str, path: Path, original_name: str) -> None:
    """Profile the document in the isolated worker and attach the result."""
    settings = _settings_for(user_id)
    analysis_job_id = db.new_id("job")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, file_id, kind, status, options_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (analysis_job_id, user_id, file_id, "analyze", "queued", "{}", db.now()),
        )

    def task() -> None:
        with db.session() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND user_id=?",
                (db.now(), analysis_job_id, user_id),
            )
        result = jobs.run_worker(
            user_id=user_id,
            job_id=analysis_job_id,
            input_path=path,
            original_name=original_name,
            options={},
            settings=settings,
            mode="analyze",
        )
        with db.session() as conn:
            if result.get("ok"):
                # The operation catalogue travels with the analysis so the interface
                # can show the plan without a second round trip.
                analysis = dict(result.get("analysis") or {})
                analysis["operations"] = result.get("operations") or {}
                conn.execute(
                    "UPDATE files SET status='analyzed', analysis_json=?, error=NULL WHERE id=? AND user_id=?",
                    (json.dumps(analysis, default=str), file_id, user_id),
                )
                conn.execute(
                    "UPDATE jobs SET status='succeeded', result_json=?, finished_at=? WHERE id=? AND user_id=?",
                    (json.dumps(result, default=str), db.now(), analysis_job_id, user_id),
                )
            else:
                message = str(result.get("error") or "Analysis failed.")
                conn.execute(
                    "UPDATE files SET status='analysis_failed', error=? WHERE id=? AND user_id=?",
                    (message, file_id, user_id),
                )
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, result_json=?, finished_at=?"
                    " WHERE id=? AND user_id=?",
                    (
                        message,
                        json.dumps(result, default=str),
                        db.now(),
                        analysis_job_id,
                        user_id,
                    ),
                )
        # Analysis scratch data is not a deliverable, so the job directory is dropped.
        try:
            storage.purge_job(user_id, analysis_job_id)
        except storage.StorageError:  # pragma: no cover
            pass

    jobs.submit_job(analysis_job_id, task)


def _file_payload(file_id: str, user_id: str) -> dict[str, Any]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM files WHERE id=? AND user_id=?", (file_id, user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File not found.")
        analysis = json.loads(row["analysis_json"]) if row["analysis_json"] else None
    return {
        "id": row["id"],
        "original_name": row["original_name"],
        "format": row["detected_format"],
        "declared_ext": row["declared_ext"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "analysis": analysis,
        "analysis_ready": bool(analysis),
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@api.get("/files")
def list_files(user: security.CurrentUser = User, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, original_name, detected_format, declared_ext, size_bytes, status, created_at, error"
            " FROM files WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (user.id, limit),
        ).fetchall()
        counts = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(size_bytes),0) AS bytes FROM files"
            " WHERE user_id=? AND deleted_at IS NULL",
            (user.id,),
        ).fetchone()
    return {
        "files": [dict(row) for row in rows],
        "total": counts["total"],
        "total_bytes": counts["bytes"],
    }


@api.get("/files/{file_id}")
def get_file(file_id: str, user: security.CurrentUser = User) -> dict[str, Any]:
    return _file_payload(file_id, user.id)


@api.get("/files/{file_id}/download")
def download_original(file_id: str, user: security.CurrentUser = User) -> FileResponse:
    with db.session() as conn:
        row = conn.execute(
            "SELECT original_name, stored_path FROM files WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (file_id, user.id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    path = storage.resolve_owned(str(config.DATA_DIR / row["stored_path"]), user.id)
    return FileResponse(path, filename=storage.safe_display_name(row["original_name"]))


@api.delete("/files/{file_id}")
def delete_file(
    file_id: str, request: Request, user: security.CurrentUser = User
) -> dict[str, Any]:
    security.enforce_same_origin(request)
    with db.session() as conn:
        row = conn.execute(
            "SELECT stored_path FROM files WHERE id=? AND user_id=?", (file_id, user.id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File not found.")
        job_rows = conn.execute("SELECT id FROM jobs WHERE file_id=? AND user_id=?", (file_id, user.id)).fetchall()
        conn.execute("DELETE FROM files WHERE id=? AND user_id=?", (file_id, user.id))
        db.audit(conn, user.id, "file_deleted", f"file={file_id}")

    try:
        storage.delete_path(config.DATA_DIR / row["stored_path"])
    except storage.StorageError:  # pragma: no cover - storage already gone
        pass
    for job_row in job_rows:
        try:
            storage.purge_job(user.id, job_row["id"])
        except storage.StorageError:  # pragma: no cover
            pass
    return {"deleted": True, "jobs_deleted": len(job_rows)}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@api.post("/jobs")
def create_job(payload: JobRequest, request: Request, user: security.CurrentUser = User) -> JSONResponse:
    security.enforce_same_origin(request)
    with db.session() as conn:
        file_row = conn.execute(
            "SELECT id, original_name, stored_path, detected_format, size_bytes FROM files"
            " WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (payload.file_id, user.id),
        ).fetchone()
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    if file_row["size_bytes"] == 0:
        raise HTTPException(status_code=422, detail="The file is empty and cannot be processed.")

    options = dict(payload.options or {})
    if payload.operations is not None:
        options["operations"] = payload.operations
        options["approved"] = True

    job_id = db.new_id("job")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, file_id, kind, status, options_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                user.id,
                payload.file_id,
                "optimize",
                "queued",
                json.dumps(options, default=str),
                db.now(),
            ),
        )
        db.audit(conn, user.id, "job_created", f"job={job_id} file={payload.file_id}")

    input_path = storage.resolve_owned(str(config.DATA_DIR / file_row["stored_path"]), user.id)
    original_name = file_row["original_name"]
    settings = _settings_for(user.id)

    def task() -> None:
        with db.session() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND user_id=?",
                (db.now(), job_id, user.id),
            )
        result = jobs.run_worker(
            user_id=user.id,
            job_id=job_id,
            input_path=input_path,
            original_name=original_name,
            options=options,
            settings=settings,
            mode="optimize",
        )
        with db.session() as conn:
            if result.get("ok"):
                jobs.register_artifacts(conn, user.id, job_id, result)
                conn.execute(
                    "UPDATE jobs SET status='succeeded', result_json=?, finished_at=? WHERE id=? AND user_id=?",
                    (json.dumps(result, default=str), db.now(), job_id, user.id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, result_json=?, finished_at=?"
                    " WHERE id=? AND user_id=?",
                    (
                        str(result.get("error") or "Processing failed."),
                        json.dumps(result, default=str),
                        db.now(),
                        job_id,
                        user.id,
                    ),
                )

    jobs.submit_job(job_id, task)
    return JSONResponse(status_code=202, content={"job": _job_payload(job_id, user.id, include_result=False)})


def _job_payload(job_id: str, user_id: str, include_result: bool = True) -> dict[str, Any]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT j.*, f.original_name AS file_name, f.detected_format AS file_format,"
            " f.size_bytes AS input_bytes FROM jobs j JOIN files f ON f.id = j.file_id"
            " WHERE j.id=? AND j.user_id=?",
            (job_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        artifacts = conn.execute(
            "SELECT id, kind, label, filename, size_bytes, created_at FROM artifacts"
            " WHERE job_id=? AND user_id=? ORDER BY created_at",
            (job_id, user_id),
        ).fetchall()
    payload: dict[str, Any] = {
        "id": row["id"],
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_format": row["file_format"],
        "kind": row["kind"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "options": json.loads(row["options_json"] or "{}"),
        "artifacts": [dict(a) for a in artifacts],
    }
    if include_result:
        payload["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    return payload


@api.get("/jobs")
def list_jobs(
    user: security.CurrentUser = User,
    limit: int = 50,
    kind: str | None = None,
    include_analysis: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    query = (
        "SELECT j.id, j.file_id, j.kind, j.status, j.error, j.created_at, j.started_at, j.finished_at,"
        " f.original_name AS file_name, f.detected_format AS file_format"
        " FROM jobs j JOIN files f ON f.id = j.file_id WHERE j.user_id=?"
    )
    params: list[Any] = [user.id]
    if kind:
        query += " AND j.kind=?"
        params.append(kind)
    elif not include_analysis:
        query += " AND j.kind='optimize'"
    query += " ORDER BY j.created_at DESC LIMIT ?"
    params.append(limit)
    with db.session() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"jobs": [dict(row) for row in rows]}


@api.get("/jobs/{job_id}")
def get_job(job_id: str, user: security.CurrentUser = User) -> dict[str, Any]:
    return {"job": _job_payload(job_id, user.id)}


@api.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request, user: security.CurrentUser = User) -> dict[str, Any]:
    security.enforce_same_origin(request)
    with db.session() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE id=? AND user_id=?", (job_id, user.id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        conn.execute("DELETE FROM jobs WHERE id=? AND user_id=?", (job_id, user.id))
        db.audit(conn, user.id, "job_deleted", f"job={job_id}")
    try:
        storage.purge_job(user.id, job_id)
    except storage.StorageError:  # pragma: no cover
        pass
    return {"deleted": True}


@api.get("/jobs/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    job_id: str, artifact_id: str, user: security.CurrentUser = User
) -> FileResponse:
    with db.session() as conn:
        row = conn.execute(
            "SELECT filename, stored_path FROM artifacts WHERE id=? AND job_id=? AND user_id=?",
            (artifact_id, job_id, user.id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    path = storage.resolve_owned(str(config.DATA_DIR / row["stored_path"]), user.id)
    return FileResponse(path, filename=storage.safe_display_name(row["filename"]))


# ---------------------------------------------------------------------------
# NAMI Optimizer: pipeline intelligence lab
# ---------------------------------------------------------------------------


def _owned_file(file_id: str, user_id: str) -> Any:
    with db.session() as conn:
        row = conn.execute(
            "SELECT id, original_name, stored_path, detected_format, size_bytes FROM files"
            " WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (file_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return row


def _require_eligible(fmt: str) -> dict[str, Any]:
    gate = nami_support.eligibility(fmt)
    if not gate["eligible"]:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "format_not_benchmarkable",
                "message": gate["reason"],
                "eligibility": gate,
            },
        )
    return gate


def _benchmark_timeout(user_timeout: int, trials: int) -> int:
    """A benchmark runs both systems twice per trial, plus a warm-up pair."""
    derived = max(user_timeout, user_timeout * (2 * trials + 2))
    return min(NAMI_MAX_BENCHMARK_SECONDS, derived)


@api.get("/nami/policies")
def nami_policies(user: security.CurrentUser = User) -> dict[str, Any]:
    """The policy catalogue, the thresholds and the method, as the code defines them."""
    catalogue = nami_policy.policy_catalog()
    return {
        **catalogue,
        "reference_system": REFERENCE_SYSTEM,
        "nami_system": NAMI_SYSTEM,
        "support": nami_support.catalog(),
        "methodology": NAMI_METHODOLOGY,
        "limitations": NAMI_LIMITATIONS,
        "measurement_scope": measurement_scope(),
        "defaults": {
            "trials": 3,
            "warmup": True,
            "max_trials": NAMI_MAX_TRIALS,
            "max_benchmark_seconds": NAMI_MAX_BENCHMARK_SECONDS,
        },
    }


@api.post("/nami/preflight")
def nami_preflight(
    payload: PreflightRequest, request: Request, user: security.CurrentUser = User
) -> JSONResponse:
    """Profile a workload and choose a policy without transforming anything."""
    security.enforce_same_origin(request)
    file_row = _owned_file(payload.file_id, user.id)
    if file_row["size_bytes"] == 0:
        raise HTTPException(status_code=422, detail="The file is empty and cannot be profiled.")
    _require_eligible(file_row["detected_format"])

    options: dict[str, Any] = {**(payload.options or {})}
    if payload.operations is not None:
        options["operations"] = payload.operations
        options["approved"] = True

    job_id = db.new_id("job")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, file_id, kind, status, options_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (job_id, user.id, payload.file_id, "nami_preflight", "queued", json.dumps(options, default=str), db.now()),
        )
        db.audit(conn, user.id, "nami_preflight", f"job={job_id} file={payload.file_id}")

    input_path = storage.resolve_owned(str(config.DATA_DIR / file_row["stored_path"]), user.id)
    original_name = file_row["original_name"]
    settings = _settings_for(user.id)

    def task() -> None:
        with db.session() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND user_id=?",
                (db.now(), job_id, user.id),
            )
        result = jobs.run_worker(
            user_id=user.id,
            job_id=job_id,
            input_path=input_path,
            original_name=original_name,
            options=options,
            settings=settings,
            mode="preflight",
            pipeline="nami",
        )
        with db.session() as conn:
            if result.get("ok"):
                profile = result.get("workload_profile") or {}
                conn.execute(
                    "UPDATE jobs SET status='succeeded', result_json=?, finished_at=? WHERE id=? AND user_id=?",
                    (json.dumps(result, default=str), db.now(), job_id, user.id),
                )
                conn.execute(
                    "UPDATE files SET nami_profile_json=?, nami_policy_id=? WHERE id=? AND user_id=?",
                    (json.dumps(profile, default=str), (result.get("policy") or {}).get("id"), payload.file_id, user.id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, result_json=?, finished_at=?"
                    " WHERE id=? AND user_id=?",
                    (
                        str(result.get("error") or "Profiling failed."),
                        json.dumps(result, default=str),
                        db.now(),
                        job_id,
                        user.id,
                    ),
                )
        try:
            storage.purge_job(user.id, job_id)
        except storage.StorageError:  # pragma: no cover
            pass

    jobs.submit_job(job_id, task)
    return JSONResponse(status_code=202, content={"job": _job_payload(job_id, user.id, include_result=False)})


@api.post("/nami/benchmarks")
def create_benchmark(
    payload: BenchmarkRequest, request: Request, user: security.CurrentUser = User
) -> JSONResponse:
    """Run the interleaved SPADE-vs-NAMI comparison for one file."""
    security.enforce_same_origin(request)
    if not 1 <= payload.trials <= NAMI_MAX_TRIALS:
        raise HTTPException(
            status_code=422,
            detail=f"Trials must be between 1 and {NAMI_MAX_TRIALS}.",
        )
    if payload.force_policy and payload.force_policy not in nami_policy.POLICIES_BY_ID:
        raise HTTPException(status_code=422, detail=f"Unknown policy {payload.force_policy!r}.")

    file_row = _owned_file(payload.file_id, user.id)
    if file_row["size_bytes"] == 0:
        raise HTTPException(status_code=422, detail="The file is empty and cannot be benchmarked.")
    _require_eligible(file_row["detected_format"])

    settings = _settings_for(user.id)
    timeout = _benchmark_timeout(int(settings.get("processing_timeout_seconds", 60)), payload.trials)
    options: dict[str, Any] = {
        "trials": payload.trials,
        "warmup": payload.warmup,
        "timeout_seconds": timeout,
        "_sha256": None,
        **(payload.options or {}),
    }
    options.pop("_sha256", None)
    if payload.operations is not None:
        options["operations"] = payload.operations
        options["approved"] = True
    if payload.force_policy:
        options["force_policy"] = payload.force_policy
    options["trials"] = payload.trials
    options["warmup"] = payload.warmup
    options["timeout_seconds"] = timeout

    with db.session() as conn:
        sha = conn.execute("SELECT sha256 FROM files WHERE id=? AND user_id=?", (payload.file_id, user.id)).fetchone()
    options["_sha256"] = sha["sha256"] if sha else None

    benchmark_id = db.new_id("bmk")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO benchmarks (id, user_id, file_id, status, trials_requested, force_policy,"
            " operations_json, options_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                benchmark_id,
                user.id,
                payload.file_id,
                "queued",
                payload.trials,
                payload.force_policy or "",
                json.dumps(payload.operations or [], default=str),
                json.dumps(options, default=str),
                db.now(),
            ),
        )
        db.audit(
            conn,
            user.id,
            "nami_benchmark_created",
            f"benchmark={benchmark_id} file={payload.file_id} trials={payload.trials}",
        )

    input_path = storage.resolve_owned(str(config.DATA_DIR / file_row["stored_path"]), user.id)
    original_name = file_row["original_name"]
    output_dir = storage.benchmark_output_dir(user.id, benchmark_id)

    def task() -> None:
        with db.session() as conn:
            conn.execute(
                "UPDATE benchmarks SET status='running', started_at=? WHERE id=? AND user_id=?",
                (db.now(), benchmark_id, user.id),
            )
        result = jobs.run_worker(
            user_id=user.id,
            job_id=benchmark_id,
            input_path=input_path,
            original_name=original_name,
            options=options,
            settings=settings,
            mode="benchmark",
            pipeline="nami",
            timeout_seconds=timeout,
            output_dir=output_dir,
        )
        with db.session() as conn:
            if result.get("ok"):
                _register_benchmark_artifacts(conn, user.id, benchmark_id, result)
                trials = result.get("trials") or {}
                conn.execute(
                    "UPDATE benchmarks SET status='succeeded', result_json=?, profile_json=?,"
                    " policy_id=?, trials_completed=?, finished_at=? WHERE id=? AND user_id=?",
                    (
                        json.dumps(result, default=str),
                        json.dumps(result.get("workload_profile") or {}, default=str),
                        (result.get("policy") or {}).get("id"),
                        int(trials.get("completed") or 0),
                        db.now(),
                        benchmark_id,
                        user.id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE benchmarks SET status='failed', error=?, result_json=?, finished_at=?"
                    " WHERE id=? AND user_id=?",
                    (
                        str(result.get("error") or "The benchmark failed."),
                        json.dumps(result, default=str),
                        db.now(),
                        benchmark_id,
                        user.id,
                    ),
                )

    jobs.submit_job(benchmark_id, task)
    return JSONResponse(status_code=202, content={"benchmark": _benchmark_payload(benchmark_id, user.id, include_result=False)})


def _register_benchmark_artifacts(conn, user_id: str, benchmark_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Persist artifact rows for a finished benchmark, inside the storage root only."""
    registered: list[dict[str, Any]] = []
    for artifact in result.get("artifacts", []):
        raw_path = Path(artifact["path"])
        try:
            relative = raw_path.resolve().relative_to(config.DATA_DIR)
        except ValueError:
            continue
        if not raw_path.is_file():
            continue
        artifact_id = db.new_id("art")
        conn.execute(
            "INSERT INTO benchmark_artifacts (id, benchmark_id, user_id, kind, label, filename, stored_path,"
            " size_bytes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                benchmark_id,
                user_id,
                artifact.get("kind", "output"),
                artifact.get("label", artifact.get("filename", "artifact")),
                storage.safe_display_name(artifact.get("filename", "artifact")),
                str(relative).replace("\\", "/"),
                int(artifact.get("size_bytes") or raw_path.stat().st_size),
                db.now(),
            ),
        )
        registered.append({**artifact, "id": artifact_id})
    return registered


def _benchmark_payload(benchmark_id: str, user_id: str, include_result: bool = True) -> dict[str, Any]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT b.*, f.original_name AS file_name, f.detected_format AS file_format,"
            " f.size_bytes AS input_bytes FROM benchmarks b JOIN files f ON f.id = b.file_id"
            " WHERE b.id=? AND b.user_id=?",
            (benchmark_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Benchmark not found.")
        artifacts = conn.execute(
            "SELECT id, kind, label, filename, size_bytes, created_at FROM benchmark_artifacts"
            " WHERE benchmark_id=? AND user_id=? ORDER BY created_at",
            (benchmark_id, user_id),
        ).fetchall()
    payload: dict[str, Any] = {
        "id": row["id"],
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_format": row["file_format"],
        "status": row["status"],
        "error": row["error"],
        "error_kind": None,
        "trials_requested": row["trials_requested"],
        "trials_completed": row["trials_completed"],
        "policy_id": row["policy_id"],
        "force_policy": row["force_policy"] or None,
        "operations": json.loads(row["operations_json"] or "[]"),
        "options": json.loads(row["options_json"] or "{}"),
        "profile": json.loads(row["profile_json"]) if row["profile_json"] else None,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "artifacts": [dict(a) for a in artifacts],
    }
    if include_result:
        payload["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        if payload["result"] and not payload["result"].get("ok"):
            payload["error_kind"] = payload["result"].get("error_kind")
    return payload


@api.get("/nami/benchmarks")
def list_benchmarks(user: security.CurrentUser = User, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    with db.session() as conn:
        rows = conn.execute(
            "SELECT b.id, b.file_id, b.status, b.error, b.trials_requested, b.trials_completed, b.policy_id,"
            " b.force_policy, b.created_at, b.started_at, b.finished_at, b.profile_json, b.result_json,"
            " f.original_name AS file_name, f.detected_format AS file_format, f.size_bytes AS input_bytes"
            " FROM benchmarks b JOIN files f ON f.id = b.file_id WHERE b.user_id=?"
            " ORDER BY b.created_at DESC LIMIT ?",
            (user.id, limit),
        ).fetchall()
    return {"benchmarks": [_benchmark_row(row) for row in rows]}


def _benchmark_row(row) -> dict[str, Any]:
    """A history row plus the headline measured figures, when the run finished."""
    summary: dict[str, Any] = {}
    if row["status"] == "succeeded" and row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            result = {}
        comparison = result.get("comparison") or {}
        summary = {
            "end_to_end_reduction_pct": (comparison.get("end_to_end") or {}).get("reduction_pct"),
            "execution_reduction_pct": (comparison.get("execution_only") or {}).get("reduction_pct"),
            "file_size_reduction_pct": (comparison.get("file_size") or {}).get("reduction_pct"),
            "correctness": (result.get("correctness") or {}).get("status"),
            "records_processed": (comparison.get("records") or {}).get("input"),
            "within_noise": (comparison.get("end_to_end") or {}).get("within_noise"),
        }
    return {
        "id": row["id"],
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_format": row["file_format"],
        "input_bytes": row["input_bytes"],
        "status": row["status"],
        "error": row["error"],
        "trials_requested": row["trials_requested"],
        "trials_completed": row["trials_completed"],
        "policy_id": row["policy_id"],
        "force_policy": row["force_policy"] or None,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "summary": summary,
    }


@api.get("/nami/benchmarks/{benchmark_id}")
def get_benchmark(benchmark_id: str, user: security.CurrentUser = User) -> dict[str, Any]:
    return {"benchmark": _benchmark_payload(benchmark_id, user.id)}


@api.delete("/nami/benchmarks/{benchmark_id}")
def delete_benchmark(
    benchmark_id: str, request: Request, user: security.CurrentUser = User
) -> dict[str, Any]:
    security.enforce_same_origin(request)
    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM benchmarks WHERE id=? AND user_id=?", (benchmark_id, user.id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Benchmark not found.")
        conn.execute("DELETE FROM benchmarks WHERE id=? AND user_id=?", (benchmark_id, user.id))
        db.audit(conn, user.id, "nami_benchmark_deleted", f"benchmark={benchmark_id}")
    try:
        storage.purge_benchmark(user.id, benchmark_id)
    except storage.StorageError:  # pragma: no cover
        pass
    return {"deleted": True}


@api.get("/nami/benchmarks/{benchmark_id}/artifacts/{artifact_id}")
def download_benchmark_artifact(
    benchmark_id: str, artifact_id: str, user: security.CurrentUser = User
) -> FileResponse:
    with db.session() as conn:
        row = conn.execute(
            "SELECT filename, stored_path FROM benchmark_artifacts"
            " WHERE id=? AND benchmark_id=? AND user_id=?",
            (artifact_id, benchmark_id, user.id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    path = storage.resolve_owned(str(config.DATA_DIR / row["stored_path"]), user.id)
    return FileResponse(path, filename=storage.safe_display_name(row["filename"]))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@api.get("/overview")
def overview(user: security.CurrentUser = User) -> dict[str, Any]:
    with db.session() as conn:
        files = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(size_bytes),0) AS bytes FROM files"
            " WHERE user_id=? AND deleted_at IS NULL",
            (user.id,),
        ).fetchone()
        job_counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE user_id=? AND kind='optimize' GROUP BY status",
            (user.id,),
        ).fetchall()
        recent = conn.execute(
            "SELECT j.id, j.status, j.created_at, j.result_json, f.original_name AS file_name,"
            " f.detected_format AS file_format FROM jobs j JOIN files f ON f.id=j.file_id"
            " WHERE j.user_id=? AND j.kind='optimize' ORDER BY j.created_at DESC LIMIT 8",
            (user.id,),
        ).fetchall()
        audits = conn.execute(
            "SELECT action, detail, created_at FROM audit WHERE user_id=? ORDER BY created_at DESC LIMIT 12",
            (user.id,),
        ).fetchall()

    saved_bytes = 0
    reduction_samples: list[float] = []
    for row in recent:
        if not row["result_json"]:
            continue
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            continue
        size = (result.get("comparison") or {}).get("file_size") or {}
        delta = size.get("delta_bytes") or 0
        if delta > 0:
            saved_bytes += delta
        reduction = size.get("reduction_pct")
        if isinstance(reduction, (int, float)) and reduction > 0:
            reduction_samples.append(float(reduction))

    return {
        "files": {"total": files["total"], "bytes": files["bytes"]},
        "jobs": {row["status"]: row["n"] for row in job_counts},
        "saved_bytes_recent": saved_bytes,
        "average_reduction_pct_recent": round(sum(reduction_samples) / len(reduction_samples), 2)
        if reduction_samples
        else None,
        "recent_jobs": [
            {
                "id": row["id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "file_name": row["file_name"],
                "file_format": row["file_format"],
                "result": _recent_summary(row["result_json"]),
            }
            for row in recent
        ],
        "activity": [dict(row) for row in audits],
        "supported_formats": supported_formats(),
    }


def _recent_summary(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None
    comparison = result.get("comparison") or {}
    return {
        "outcome": (result.get("outcome") or {}).get("kind"),
        "format": (result.get("file") or {}).get("format"),
        "size": comparison.get("file_size"),
        "time": comparison.get("execution_time"),
        "verification": (result.get("verification") or {}).get("status"),
    }


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Keep the browser honest about what it may reuse.

    * Static assets are marked ``no-cache`` so they are revalidated instead of
      being served from a heuristic cache (they still carry ETag/Last-Modified,
      so the revalidation is a cheap 304).
    * API responses are marked ``no-store``: they are personalised, they change
      as jobs finish, and a cached list would show a user files or runs that no
      longer exist.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Vary", "Cookie")
    elif path.startswith("/static/") or path == "/":
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.include_router(api)

if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    index_path = config.STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="The interface has not been built.")
    return FileResponse(index_path, media_type="text/html")


@app.get("/command-center")
def command_center() -> FileResponse:
    """The AI Command Center demo, a static companion page to this app.

    It is a self-contained client-side page: it renders sample data and keeps its
    tasks and preferences in the browser, and it makes no API calls at all.
    """
    page = config.STATIC_DIR / "command-center.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="The interface has not been built.")
    return FileResponse(page, media_type="text/html")
