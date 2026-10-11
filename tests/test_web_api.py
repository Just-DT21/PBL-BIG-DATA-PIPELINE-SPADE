"""Authentication, authorization, limits and deletion over the real HTTP API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import helpers
from spade import config


def test_health_reports_formats_and_windows(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["supported_formats"]) == {"csv", "xlsx", "xls", "docx", "pdf", "json", "txt"}
    assert body["backends"]["declared"] == ["spark", "flink"]


def test_api_requires_a_session(client):
    for method, path in [
        ("get", "/api/files"),
        ("get", "/api/jobs"),
        ("get", "/api/overview"),
        ("get", "/api/settings"),
        ("get", "/api/auth/me"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/jobs", json={"file_id": "fl_missing"}).status_code == 401


def test_register_login_logout_cycle(client):
    email = helpers.register(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    # A wrong password must not hand out a session.
    bad = client.post("/api/auth/login", json={"email": email, "password": "not-the-password"})
    assert bad.status_code == 401
    assert client.get("/api/auth/me").status_code == 401

    good = client.post("/api/auth/login", json={"email": email, "password": "correct-horse-1"})
    assert good.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_duplicate_registration_is_refused(app):
    fresh = TestClient(app)
    email = helpers.register(fresh, email="duplicate@example.com")
    duplicate = TestClient(app).post(
        "/api/auth/register", json={"email": email, "password": "correct-horse-1"}
    )
    assert duplicate.status_code in (400, 409, 422)


def test_weak_registration_input_is_rejected(client):
    assert client.post("/api/auth/register", json={"email": "nope", "password": "correct-horse-1"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "ok@example.com", "password": "short"}).status_code == 422


def test_cross_origin_state_change_is_refused(client, sample_files):
    helpers.register(client)
    with open(sample_files["csv"], "rb") as handle:
        response = client.post(
            "/api/files",
            files={"file": ("notes.csv", handle, "text/csv")},
            headers={"origin": "http://attacker.example"},
        )
    assert response.status_code == 403


def test_files_are_private_to_their_owner(app, sample_files):
    owner = TestClient(app)
    intruder = TestClient(app)
    helpers.register(owner, email="owner@example.com")
    helpers.register(intruder, email="intruder@example.com")

    created = helpers.upload_ok(owner, sample_files["csv"])
    file_id = created["id"]
    analysis = helpers.wait_for_analysis(owner, file_id)
    assert analysis["analysis_ready"]
    job = helpers.optimize(owner, file_id)
    assert job["status"] == "succeeded"
    artifact_id = job["artifacts"][0]["id"]

    # The intruder sees none of it.
    assert intruder.get(f"/api/files/{file_id}").status_code == 404
    assert intruder.get(f"/api/files/{file_id}/download").status_code == 404
    assert intruder.get(f"/api/jobs/{job['id']}").status_code == 404
    assert intruder.get(f"/api/jobs/{job['id']}/artifacts/{artifact_id}").status_code == 404
    assert intruder.delete(f"/api/files/{file_id}").status_code == 404
    assert intruder.post("/api/jobs", json={"file_id": file_id}).status_code == 404
    assert intruder.get("/api/files").json()["total"] == 0


def test_unsupported_and_malformed_uploads_are_rejected_with_a_reason(client, clean_workdir):
    helpers.register(client)

    binary = clean_workdir / "installer.exe"
    binary.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + bytes(range(1, 200)))
    response = helpers.upload(client, binary)
    assert response.status_code == 415
    assert "binary" in response.json()["detail"].lower() or "supported" in response.json()["detail"].lower()

    empty = clean_workdir / "empty.csv"
    empty.write_bytes(b"")
    empty_response = helpers.upload(client, empty)
    assert empty_response.status_code in (400, 415)
    assert "empty" in empty_response.json()["detail"].lower()

    truncated = clean_workdir / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.7\n" + b"x" * 400)
    assert helpers.upload(client, truncated).status_code == 415


def test_upload_limit_is_enforced(client, clean_workdir):
    helpers.register(client)
    current = client.get("/api/settings").json()
    small = 4096
    updated = client.put("/api/settings", json={"max_upload_bytes": small})
    assert updated.status_code == 200
    assert updated.json()["settings"]["max_upload_bytes"] == small

    payload = clean_workdir / "big.csv"
    payload.write_bytes(b"a,b\n" + b"1,2\n" * 4000)  # ~24 KB
    response = helpers.upload(client, payload)
    assert response.status_code == 413
    assert "limit" in response.json()["detail"].lower()

    # The ceiling can be raised again, but never past the hard limit.
    assert client.put("/api/settings", json={"max_upload_bytes": config.MAX_UPLOAD_BYTES}).status_code == 200
    assert client.put("/api/settings", json={"max_upload_bytes": config.MAX_UPLOAD_BYTES + 1}).status_code == 422
    assert client.put("/api/settings", json={"chunk_rows": 1}).status_code == 422
    assert client.get("/api/settings").json()["settings"]["max_upload_bytes"] == config.MAX_UPLOAD_BYTES


def test_upload_delete_removes_files_and_results(client, sample_files):
    helpers.register(client)
    created = helpers.upload_ok(client, sample_files["csv"])
    file_id = created["id"]
    assert helpers.wait_for_analysis(client, file_id)["analysis_ready"]
    job = helpers.optimize(client, file_id)
    assert job["status"] == "succeeded"

    response = client.delete(f"/api/files/{file_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/api/files/{file_id}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert helpers.download(client, job["id"], job["artifacts"][0]["id"]).status_code == 404


def test_job_deletion_is_independent_of_the_upload(client, sample_files):
    helpers.register(client)
    created = helpers.upload_ok(client, sample_files["csv"])
    helpers.wait_for_analysis(client, created["id"])
    job = helpers.optimize(client, created["id"])

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    # The upload itself survives, so the teacher can run another experiment.
    assert client.get(f"/api/files/{created['id']}").status_code == 200


def test_overview_and_history_reflect_real_runs(client, sample_files):
    helpers.register(client)
    created = helpers.upload_ok(client, sample_files["csv"])
    helpers.wait_for_analysis(client, created["id"])
    job = helpers.optimize(client, created["id"])

    overview = client.get("/api/overview").json()
    assert overview["files"]["total"] >= 1
    assert overview["jobs"].get("succeeded", 0) >= 1
    assert any(entry["id"] == job["id"] for entry in overview["recent_jobs"])
    assert overview["activity"], "the audit trail should record the run"

    history = client.get("/api/jobs").json()["jobs"]
    assert any(entry["id"] == job["id"] for entry in history)
    assert all(entry["kind"] == "optimize" for entry in history)
    assert client.get("/api/jobs?include_analysis=true").status_code == 200


def test_settings_persist_per_user(app, sample_files):
    first = TestClient(app)
    second = TestClient(app)
    helpers.register(first, email="settings-a@example.com")
    helpers.register(second, email="settings-b@example.com")

    assert first.put("/api/settings", json={"chunk_rows": 4000}).status_code == 200
    assert first.get("/api/settings").json()["settings"]["chunk_rows"] == 4000
    # A different account keeps the default.
    assert second.get("/api/settings").json()["settings"]["chunk_rows"] == config.USER_SETTINGS["chunk_rows"][0]


def test_responses_declare_what_the_browser_may_cache(client):
    """Personalised API data must never be reused from the browser cache."""
    assert client.get("/api/health").headers.get("cache-control") == "no-store"
    helpers.register(client)
    for path in ("/api/files", "/api/overview", "/api/nami/benchmarks", "/api/nami/policies"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-store", path

    # The interface shell is revalidated rather than cached blindly, and the
    # static assets still carry validators so the revalidation is cheap.
    shell = client.get("/")
    assert shell.headers.get("cache-control") == "no-cache"
    asset = client.get("/static/js/lab.js")
    assert asset.status_code == 200
    assert asset.headers.get("cache-control") == "no-cache"
    assert asset.headers.get("etag")


def test_job_for_an_unknown_file_is_refused(client):
    helpers.register(client)
    response = helpers.create_job(client, "fl_does_not_exist")
    assert response.status_code == 404
    assert response.json()["detail"]
