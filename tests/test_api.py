"""T02 API tests (V10): uploads and archives, versions, evidence, artifacts, runs, cancel, conversation, restart."""

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from sverka import api, config
from sverka import contracts as c
from sverka.storage import SqliteStore

SCOPE = {
    "contract": "24/03/15-01",
    "report_row": "3 этап, 2026 г.",
    "date_from": "2026-01-01",
    "date_to": "2026-05-31",
    "confirmed": True,
}


class QueuedLauncher:
    """Leaves runs queued, like a busy run service, and records cancellations."""

    def __init__(self, store):
        self.store, self.cancelled = store, []

    def start(self, run):
        pass

    def cancel(self, run):
        self.cancelled.append(run.run_id)


class CrashingLauncher:
    def __init__(self, store):
        self.store = store

    def start(self, run):
        raise RuntimeError("worker unavailable")

    def cancel(self, run):
        pass


def make_client(data_dir, launcher=api.DisabledRunLauncher):
    store = SqliteStore(data_dir)
    services = api.Services(store=store, launcher=launcher(store), responder=api.SavedResultResponder(store))
    app = FastAPI()
    api.install(app, services)
    return TestClient(app), services


@pytest.fixture
def client(tmp_path):
    cl, services = make_client(tmp_path / "data")
    yield cl
    services.store.close()


def _ws(cl):
    r = cl.post("/api/workspaces")
    assert r.status_code == 201
    return r.json()["workspace_id"]


def _upload(cl, ws, files, role="primary", **data):
    return cl.post(f"/api/workspaces/{ws}/documents", files=[("files", f) for f in files], data={"role": role, **data})


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _error(response):
    return response.json()["error"]["code"]


# ======================= uploads =======================


def test_same_name_uploads_become_separate_versions(client):
    ws = _ws(client)
    first = _upload(client, ws, [("Акт.pdf", b"%PDF-first", "application/pdf")])
    second = _upload(client, ws, [("Акт.pdf", b"%PDF-second", "application/pdf")])
    assert first.status_code == second.status_code == 201
    a, b = first.json()[0], second.json()[0]
    assert a["version_id"] != b["version_id"] and a["original_filename"] == b["original_filename"] == "Акт.pdf"
    listed = client.get(f"/api/workspaces/{ws}/documents").json()
    assert [v["version_id"] for v in listed] == [a["version_id"], b["version_id"]]
    assert client.get(f"/api/workspaces/{ws}/artifacts/{a['original_artifact_id']}").content == b"%PDF-first"


def test_replacement_upload(client):
    ws, other = _ws(client), _ws(client)
    v1 = _upload(client, ws, [("Ведомость.xlsx", b"v1", "application/octet-stream")]).json()[0]
    v2 = _upload(client, ws, [("Ведомость.xlsx", b"v2", "application/octet-stream")], document_id=v1["document_id"])
    assert v2.status_code == 201 and v2.json()[0]["predecessor_version_id"] == v1["version_id"]
    two = _upload(
        client,
        ws,
        [("a.pdf", b"a", "application/pdf"), ("b.pdf", b"b", "application/pdf")],
        document_id=v1["document_id"],
    )
    assert two.status_code == 400
    foreign = _upload(client, other, [("x.xlsx", b"x", "application/octet-stream")], document_id=v1["document_id"])
    assert foreign.status_code == 404


def test_role_is_required_and_validated(client):
    ws = _ws(client)
    assert _upload(client, ws, [("a.pdf", b"a", "application/pdf")], role="boss").status_code == 422
    assert client.post(f"/api/workspaces/{ws}/documents", files=[("files", ("a.pdf", b"a"))]).status_code == 422


def test_zip_upload_unpacks_files_and_skips_system_entries(client):
    ws = _ws(client)
    archive = _zip(
        [
            ("bundle/01_Карточка.pdf", b"report"),
            ("bundle/sub/02_Акт.pdf", b"act"),
            ("../../evil.pdf", b"evil"),
            ("__MACOSX/bundle/._01_Карточка.pdf", b"junk"),
            ("bundle/.DS_Store", b"junk"),
            ("bundle/~$lock.xlsx", b"junk"),
            ("bundle/empty/", b""),
        ]
    )
    r = _upload(client, ws, [("набор.zip", archive, "application/zip")])
    assert r.status_code == 201
    assert sorted(v["original_filename"] for v in r.json()) == ["01_Карточка.pdf", "02_Акт.pdf", "evil.pdf"]
    evil = next(v for v in r.json() if v["original_filename"] == "evil.pdf")
    assert client.get(f"/api/workspaces/{ws}/artifacts/{evil['original_artifact_id']}").content == b"evil"


def test_windows_cp866_member_names_are_decoded():
    info = zipfile.ZipInfo("Акт.pdf".encode("cp866").decode("cp437"))
    info.flag_bits = 0  # not flagged UTF-8, as written by Windows archivers
    assert api._member_name(info) == "Акт.pdf"


def test_invalid_archives_store_nothing(client):
    ws = _ws(client)
    bad = _upload(client, ws, [("набор.zip", b"PK\x03\x04 not really a zip", "application/zip")])
    assert bad.status_code == 400 and _error(bad) == "unreadable"
    only_junk = _upload(client, ws, [("набор.zip", _zip([("__MACOSX/x", b"x")]), "application/zip")])
    assert only_junk.status_code == 400
    mixed = _upload(client, ws, [("ok.pdf", b"ok", "application/pdf"), ("bad.zip", b"garbage", "application/zip")])
    assert mixed.status_code == 400
    assert client.get(f"/api/workspaces/{ws}/documents").json() == []


def test_oversized_uploads_and_archives_are_rejected(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    big = _upload(client, ws, [("big.pdf", b"x" * (3 << 20), "application/pdf")])
    assert big.status_code == 413
    monkeypatch.setattr(config, "MAX_ARCHIVE_MB", 1)
    bomb = _zip([("zeros.bin", b"\0" * (2 << 20))])  # compresses to a few KB, unpacks past the limit
    assert len(bomb) < 1 << 20
    assert _upload(client, ws, [("bomb.zip", bomb, "application/zip")]).status_code == 413
    monkeypatch.setattr(config, "MAX_ARCHIVE_FILES", 2)
    many = _zip([(f"{i}.pdf", b"x") for i in range(3)])
    assert _upload(client, ws, [("many.zip", many, "application/zip")]).status_code == 413
    assert client.get(f"/api/workspaces/{ws}/documents").json() == []


def test_streaming_limit_applies_without_declared_length(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    upload = UploadFile(file=io.BytesIO(b"x" * (2 << 20)), filename="big.pdf")
    with pytest.raises(api.ApiError) as excinfo:
        api._read_uploads([upload])
    assert excinfo.value.status == 413


# ======================= artifacts and evidence =======================


def test_artifacts_are_workspace_scoped_and_served_safely(client):
    ws, other = _ws(client), _ws(client)
    pdf = _upload(client, ws, [("Акт 118.pdf", b"%PDF-1.7", "application/pdf")]).json()[0]
    html = _upload(client, ws, [("page.html", b"<script>alert(1)</script>", "text/html")]).json()[0]
    r = client.get(f"/api/workspaces/{ws}/artifacts/{pdf['original_artifact_id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert (
        r.headers["content-disposition"].startswith("inline;")
        and "%D0%90%D0%BA%D1%82" in r.headers["content-disposition"]
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get(f"/api/workspaces/{ws}/artifacts/{html['original_artifact_id']}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert (
        r.headers["content-disposition"].startswith("attachment;") and r.headers["content-security-policy"] == "sandbox"
    )
    for path in (
        f"/api/workspaces/{other}/artifacts/{pdf['original_artifact_id']}",  # foreign workspace
        f"/api/workspaces/{ws}/artifacts/art_missing",
        f"/api/workspaces/{ws}/artifacts/..%2F..%2Fsverka.db",
        f"/api/workspaces/{ws}/artifacts/{pdf['sha256']}",  # content hashes are not artifact IDs
        f"/api/workspaces/..%2F/artifacts/{pdf['original_artifact_id']}",
    ):
        assert client.get(path).status_code == 404, path


def test_evidence_resolves_to_its_own_version_after_replacement(tmp_path):
    cl, services = make_client(tmp_path / "data")
    store = services.store
    ws, other = _ws(cl), _ws(cl)
    v1 = _upload(cl, ws, [("Акт.png", b"old-scan", "image/png")]).json()[0]
    render = store.save_artifact(ws, v1["version_id"], kind="page_render", media_type="image/png", data=b"old-render")
    locator = {
        "kind": "page_region",
        "page": 1,
        "render_id": render.artifact_id,
        "box": {"x0": 10, "y0": 20, "x1": 110, "y1": 40},
        "image_width": 1654,
        "image_height": 2339,
    }
    block = c.EvidenceBlock(
        version_id=v1["version_id"], block_id="blk_amount", text="650 000,00", method="ocr", locator=locator
    )
    store.save_blocks(ws, [block])
    v2 = _upload(cl, ws, [("Акт.png", b"new-scan", "image/png")], document_id=v1["document_id"]).json()[0]
    assert v2["predecessor_version_id"] == v1["version_id"]

    view = cl.get(f"/api/workspaces/{ws}/evidence/blk_amount")
    assert view.status_code == 200
    body = view.json()
    assert body["block"]["version_id"] == body["version"]["version_id"] == v1["version_id"]
    assert cl.get(body["artifact_url"]).content == b"old-render"
    assert cl.get(body["original_url"]).content == b"old-scan"
    assert cl.get(f"/api/workspaces/{other}/evidence/blk_amount").status_code == 404
    assert cl.get(f"/api/workspaces/{ws}/evidence/blk_missing").status_code == 404
    store.close()


# ======================= runs =======================


def test_disabled_run_service_fails_clearly_and_frees_the_workspace(client):
    ws = _ws(client)
    v = _upload(client, ws, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    first = client.post(f"/api/workspaces/{ws}/runs", json={"version_ids": [v["version_id"]], "scope": SCOPE})
    assert first.status_code == 202
    run = first.json()
    assert run["status"] == "failed" and run["failure"]["code"] == "internal_error" and run["result"] is None
    second = client.post(f"/api/workspaces/{ws}/runs", json={"version_ids": [v["version_id"]], "scope": SCOPE})
    assert second.status_code == 202


def test_run_requests_are_validated(client):
    ws = _ws(client)
    v = _upload(client, ws, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    unconfirmed = client.post(
        f"/api/workspaces/{ws}/runs", json={"version_ids": [v["version_id"]], "scope": {**SCOPE, "confirmed": False}}
    )
    assert unconfirmed.status_code == 422
    unknown = client.post(f"/api/workspaces/{ws}/runs", json={"version_ids": ["ver_unknown"], "scope": SCOPE})
    assert unknown.status_code == 400 and _error(unknown) == "invalid_reference"
    assert client.post(f"/api/workspaces/{ws}/runs", json={"version_ids": [], "scope": SCOPE}).status_code == 422
    assert client.get(f"/api/workspaces/{ws}/runs/run_missing").status_code == 404


def test_one_active_check_cancel_and_late_completion(tmp_path):
    cl, services = make_client(tmp_path / "data", launcher=QueuedLauncher)
    ws = _ws(cl)
    v = _upload(cl, ws, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    body = {"version_ids": [v["version_id"]], "scope": SCOPE}
    run = cl.post(f"/api/workspaces/{ws}/runs", json=body).json()
    assert run["status"] == "queued"
    busy = cl.post(f"/api/workspaces/{ws}/runs", json=body)
    assert busy.status_code == 409

    cancelled = cl.post(f"/api/workspaces/{ws}/runs/{run['run_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert cl.post(f"/api/workspaces/{ws}/runs/{run['run_id']}/cancel").json()["status"] == "cancelled"
    assert services.launcher.cancelled == [run["run_id"]]  # notified once

    from sverka.fakes import load_example

    stale = services.store.get_run(ws, run["run_id"]).model_copy(update={"status": "running"})
    late = stale.model_copy(update={"status": "completed", "stage": "save", "result": load_example("result")})
    with pytest.raises(c.ToolFailure):
        services.store.save_run(late)  # the worker's result after cancellation is ignored
    assert cl.get(f"/api/workspaces/{ws}/runs/{run['run_id']}").json()["status"] == "cancelled"
    assert cl.post(f"/api/workspaces/{ws}/runs", json=body).status_code == 202
    services.store.close()


def test_launcher_crash_is_recorded_as_failed(tmp_path):
    cl, services = make_client(tmp_path / "data", launcher=CrashingLauncher)
    ws = _ws(cl)
    v = _upload(cl, ws, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    body = {"version_ids": [v["version_id"]], "scope": SCOPE}
    run = cl.post(f"/api/workspaces/{ws}/runs", json=body).json()
    assert run["status"] == "failed" and "RuntimeError" in run["failure"]["message"]
    assert cl.post(f"/api/workspaces/{ws}/runs", json=body).status_code == 202
    services.store.close()


def test_fixture_mode_is_labelled_and_reaches_chat_as_typed_cards(tmp_path):
    cl, services = make_client(tmp_path / "data", launcher=api.FixtureRunLauncher)
    ws = _ws(cl)
    v = _upload(cl, ws, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    run = cl.post(f"/api/workspaces/{ws}/runs", json={"version_ids": [v["version_id"]], "scope": SCOPE}).json()
    assert run["status"] == "completed"
    assert any(i["code"] == "fixture_result" for i in run["issues"])
    assert run["replay"]["settings"] == {"run_mode": "fixture"}
    col15 = next(i for i in run["result"]["indicators"] if i["entry_id"] == "col15")
    assert col15["diff"] == "12500.00"  # decimal string, not a number

    messages = cl.post(f"/api/workspaces/{ws}/messages", json={"text": "Что с накладными?", "run_id": run["run_id"]})
    assert messages.status_code == 201
    user, reply = messages.json()
    assert user["role"] == "user" and reply["role"] == "assistant" and "синтетический" in reply["text"]
    kinds = [card["kind"] for card in reply["cards"]]
    assert kinds[:2] == ["run_status", "conclusion"] and kinds.count("indicator") == len(run["result"]["indicators"])
    services.store.close()


def test_messages_validate_run_scope(client):
    ws, other = _ws(client), _ws(client)
    v = _upload(client, other, [("r.pdf", b"r", "application/pdf")], role="report").json()[0]
    foreign = client.post(f"/api/workspaces/{other}/runs", json={"version_ids": [v["version_id"]], "scope": SCOPE})
    r = client.post(f"/api/workspaces/{ws}/messages", json={"text": "hi", "run_id": foreign.json()["run_id"]})
    assert r.status_code == 404
    assert client.post(f"/api/workspaces/{ws}/messages", json={"text": ""}).status_code == 422
    assert client.get(f"/api/workspaces/{ws}/messages").json() == []


# ======================= refresh and restart =======================


def test_state_survives_restart_and_unfinished_runs_are_interrupted(tmp_path):
    data = tmp_path / "data"
    cl, services = make_client(data, launcher=QueuedLauncher)
    ws = _ws(cl)
    v = _upload(cl, ws, [("Карточка.pdf", b"report", "application/pdf")], role="report").json()[0]
    run = cl.post(f"/api/workspaces/{ws}/runs", json={"version_ids": [v["version_id"]], "scope": SCOPE}).json()
    cl.post(f"/api/workspaces/{ws}/messages", json={"text": "Проверь отчет", "run_id": run["run_id"]})
    before = cl.get(f"/api/workspaces/{ws}/messages").json()
    services.store.close()

    cl, services = make_client(data)  # process restart
    assert cl.get(f"/api/workspaces/{ws}/messages").json() == before
    assert cl.get(f"/api/workspaces/{ws}/documents").json()[0]["version_id"] == v["version_id"]
    restored = cl.get(f"/api/workspaces/{ws}/runs").json()
    assert [r["status"] for r in restored] == ["interrupted"]
    assert cl.get(f"/api/workspaces/{ws}/artifacts/{v['original_artifact_id']}").content == b"report"
    services.store.close()


def test_web_app_serves_the_api(tmp_path, monkeypatch):
    from sverka.web import app

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app.state, "services", None, raising=False)
    cl = TestClient(app)
    assert cl.post("/api/workspaces").status_code == 201
    assert (tmp_path / "sverka.db").exists()
    assert cl.get("/health").json()["run_mode"] == config.RUN_MODE
    app.state.services.store.close()


class BrokenResponder:
    def respond(self, message):
        raise RuntimeError("model adapter crashed")


def test_failed_reply_is_recorded_not_lost(tmp_path):
    store = SqliteStore(tmp_path / "data")
    services = api.Services(store=store, launcher=api.DisabledRunLauncher(store), responder=BrokenResponder())
    app = FastAPI()
    api.install(app, services)
    cl = TestClient(app)
    ws = _ws(cl)
    r = cl.post(f"/api/workspaces/{ws}/messages", json={"text": "Почему расхождение?"})
    assert r.status_code == 201
    user, notice = r.json()
    assert user["role"] == "user" and notice["role"] == "system" and "RuntimeError" in notice["text"]
    assert [m["role"] for m in cl.get(f"/api/workspaces/{ws}/messages").json()] == ["user", "system"]
    store.close()
