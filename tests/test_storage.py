"""T02 storage tests (V10). Shared behaviour runs against both the SQLite store and the in-memory fake."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sverka import contracts as c
from sverka import storage
from sverka.fakes import InMemoryStore, load_example
from sverka.interfaces import ActiveRunConflict, DocumentStore, RunStore
from sverka.storage import SCHEMA_VERSION, SqliteStore

SCOPE = c.Scope(
    contract="24/03/15-01", report_row="3 этап, 2026 г.", date_from="2026-01-01", date_to="2026-05-31", confirmed=True
)


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteStore(tmp_path / "data")
        yield s
        s.close()


def _docx_block(version_id, block_id="blk_one", text="one"):
    return c.EvidenceBlock(
        version_id=version_id,
        block_id=block_id,
        text=text,
        method="docx",
        locator={"kind": "docx", "quote": text, "paragraph": 0},
    )


def _region_block(version_id, render_id, block_id="blk_region"):
    box = {"x0": 1, "y0": 1, "x1": 10, "y1": 10}
    locator = {
        "kind": "page_region",
        "page": 1,
        "render_id": render_id,
        "box": box,
        "image_width": 100,
        "image_height": 100,
    }
    return c.EvidenceBlock(version_id=version_id, block_id=block_id, text="245 000,00", method="ocr", locator=locator)


def _start(store, ws, version):
    return store.create_run(ws, c.StartRunRequest(version_ids=[version.version_id], scope=SCOPE))


def _failure_code(excinfo):
    return excinfo.value.error.code


# ======================= shared behaviour =======================


def test_implements_protocols(store):
    assert isinstance(store, DocumentStore) and isinstance(store, RunStore)


def test_same_name_uploads_are_separate_versions(store):
    ws = store.create_workspace().workspace_id
    first = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"first")
    second = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"second")
    assert first.version_id != second.version_id and first.document_id != second.document_id
    assert store.read_original(ws, first.version_id) == b"first"
    assert store.read_original(ws, second.version_id) == b"second"
    assert [v.version_id for v in store.list_versions(ws)] == [first.version_id, second.version_id]


def test_replacement_keeps_old_version_and_its_evidence(store):
    ws = store.create_workspace().workspace_id
    v1 = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"old")
    store.save_blocks(ws, [_docx_block(v1.version_id, text="old")])
    v2 = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"new", document_id=v1.document_id)
    v3 = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"newer", document_id=v1.document_id)
    assert (v2.document_id, v2.predecessor_version_id) == (v1.document_id, v1.version_id)
    assert v3.predecessor_version_id == v2.version_id
    old_block = store.get_block(ws, "blk_one")
    assert old_block.version_id == v1.version_id and old_block.text == "old"
    assert store.read_original(ws, v1.version_id) == b"old"


def test_identical_bytes_are_two_versions_with_one_hash(store):
    ws = store.create_workspace().workspace_id
    a = store.save_document(ws, filename="ADM-27.pdf", role="primary", data=b"same")
    b = store.save_document(ws, filename="ADM-27 копия.pdf", role="primary", data=b"same")
    assert a.version_id != b.version_id and a.sha256 == b.sha256


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("../../sverka.db", "sverka.db"),
        ("..\\..\\windows\\evil.pdf", "evil.pdf"),
        ("/etc/passwd", "passwd"),
        ("", "file"),
        ("..", "file"),
        ("Й.pdf", "Й.pdf"),  # macOS-style decomposed Cyrillic becomes NFC
    ],
)
def test_filenames_are_metadata_only(store, filename, expected):
    ws = store.create_workspace().workspace_id
    v = store.save_document(ws, filename=filename, role="context", data=b"payload")
    assert v.original_filename == expected
    assert store.read_original(ws, v.version_id) == b"payload"


def test_foreign_ids_are_not_found(store):
    ws, other = store.create_workspace().workspace_id, store.create_workspace().workspace_id
    v = store.save_document(ws, filename="a.pdf", role="primary", data=b"a")
    store.save_blocks(ws, [_docx_block(v.version_id)])
    run = _start(store, ws, v)
    calls = [
        lambda: store.get_version(other, v.version_id),
        lambda: store.read_original(other, v.version_id),
        lambda: store.read_artifact(other, v.original_artifact_id),
        lambda: store.get_block(other, "blk_one"),
        lambda: store.get_run(other, run.run_id),
        lambda: store.list_versions("ws_missing"),
        lambda: store.save_document(other, filename="b.pdf", role="primary", data=b"b", document_id=v.document_id),
        lambda: store.save_artifact(other, v.version_id, kind="page_render", media_type="image/png", data=b"png"),
    ]
    for call in calls:
        with pytest.raises(c.ToolFailure) as excinfo:
            call()
        assert _failure_code(excinfo) == "not_found"


def test_blocks_are_immutable_and_scoped(store):
    ws, other = store.create_workspace().workspace_id, store.create_workspace().workspace_id
    v1 = store.save_document(ws, filename="a.pdf", role="primary", data=b"a")
    v2 = store.save_document(ws, filename="b.pdf", role="primary", data=b"b")
    render = store.save_artifact(ws, v1.version_id, kind="page_render", media_type="image/png", data=b"png")
    store.save_blocks(ws, [_docx_block(v1.version_id), _region_block(v1.version_id, render.artifact_id)])
    store.save_blocks(ws, [_docx_block(v1.version_id)])  # identical re-save is harmless
    bad = [
        (ws, _docx_block(v1.version_id, text="changed")),  # same ID, other content
        (ws, _docx_block("ver_unknown", block_id="blk_two")),
        (other, _docx_block(v1.version_id, block_id="blk_three")),  # version from another workspace
        (ws, _region_block(v2.version_id, render.artifact_id, block_id="blk_four")),  # render of another version
    ]
    for workspace, block in bad:
        with pytest.raises(c.ToolFailure) as excinfo:
            store.save_blocks(workspace, [block])
        assert _failure_code(excinfo) == "invalid_reference"
    assert store.get_block(ws, "blk_one").text == "one"


def test_one_active_check_per_workspace(store):
    ws, other = store.create_workspace().workspace_id, store.create_workspace().workspace_id
    v = store.save_document(ws, filename="r.pdf", role="report", data=b"r")
    w = store.save_document(other, filename="r.pdf", role="report", data=b"r")
    run = _start(store, ws, v)
    with pytest.raises(ActiveRunConflict):
        _start(store, ws, v)
    _start(store, other, w)  # another workspace is independent
    store.save_run(run.model_copy(update={"status": "running", "stage": "prepare"}))
    with pytest.raises(ActiveRunConflict):
        _start(store, ws, v)
    store.save_run(run.model_copy(update={"status": "completed", "stage": "save", "result": load_example("result")}))
    follow_up = store.create_run(
        ws, c.StartRunRequest(version_ids=[v.version_id], scope=SCOPE, predecessor_run_id=run.run_id)
    )
    assert follow_up.predecessor_run_id == run.run_id
    assert [r.run_id for r in store.list_runs(ws)] == [run.run_id, follow_up.run_id]


def test_finished_runs_are_final_and_inputs_fixed(store):
    ws = store.create_workspace().workspace_id
    v = store.save_document(ws, filename="r.pdf", role="report", data=b"r")
    w = store.save_document(ws, filename="p.pdf", role="primary", data=b"p")
    run = _start(store, ws, v)
    with pytest.raises(c.ToolFailure) as excinfo:  # inputs cannot change after start
        store.save_run(run.model_copy(update={"input_version_ids": [w.version_id]}))
    assert _failure_code(excinfo) == "invalid_reference"
    store.save_run(run.model_copy(update={"status": "cancelled"}))
    late = run.model_copy(update={"status": "completed", "stage": "save", "result": load_example("result")})
    with pytest.raises(c.ToolFailure):
        store.save_run(late)
    assert store.get_run(ws, run.run_id).status == "cancelled"
    with pytest.raises(c.ToolFailure) as excinfo:
        store.create_run(ws, c.StartRunRequest(version_ids=["ver_unknown"], scope=SCOPE))
    assert _failure_code(excinfo) == "invalid_reference"
    with pytest.raises(c.ToolFailure) as excinfo:
        store.create_run(
            ws, c.StartRunRequest(version_ids=[v.version_id], scope=SCOPE, predecessor_run_id="run_unknown")
        )
    assert _failure_code(excinfo) == "not_found"


def test_messages_keep_order_and_scope(store):
    from datetime import UTC, datetime

    ws, other = store.create_workspace().workspace_id, store.create_workspace().workspace_id
    v = store.save_document(other, filename="r.pdf", role="report", data=b"r")
    foreign_run = _start(store, other, v)
    now = datetime.now(UTC)

    def msg(i, run_id=None):
        return c.ChatMessage(
            workspace_id=ws, message_id=f"msg_{i}", run_id=run_id, role="user", text=str(i), created_at=now
        )

    for i in range(3):
        store.append_message(msg(i))
    assert [m.text for m in store.list_messages(ws)] == ["0", "1", "2"]
    with pytest.raises(c.ToolFailure) as excinfo:
        store.append_message(msg(9, run_id=foreign_run.run_id))
    assert _failure_code(excinfo) == "not_found"
    with pytest.raises(c.ToolFailure):
        store.append_message(msg(0))  # duplicate ID
    assert store.list_messages(other) == []


# ======================= SQLite only: restart, integrity, crashes =======================


def test_restart_preserves_data_and_interrupts_unfinished_runs(tmp_path):
    data = tmp_path / "data"
    s = SqliteStore(data)
    ws, done_ws = s.create_workspace().workspace_id, s.create_workspace().workspace_id
    v = s.save_document(ws, filename="Карточка.pdf", role="report", data=b"report")
    s.save_blocks(ws, [_docx_block(v.version_id)])
    running = _start(s, ws, v)
    s.save_run(running.model_copy(update={"status": "running", "stage": "extract"}))
    d = s.save_document(done_ws, filename="r.pdf", role="report", data=b"r")
    done = _start(s, done_ws, d)
    s.save_run(done.model_copy(update={"status": "completed", "stage": "save", "result": load_example("result")}))
    s.close()

    s = SqliteStore(data)
    assert s.recovered_runs == [running.run_id]
    interrupted = s.get_run(ws, running.run_id)
    assert interrupted.status == "interrupted" and interrupted.stage == "extract"
    assert interrupted.issues[-1].code == "interrupted_by_restart"
    assert s.get_run(done_ws, done.run_id).result == load_example("result")
    assert s.read_original(ws, v.version_id) == b"report" and s.get_block(ws, "blk_one").text == "one"
    with pytest.raises(c.ToolFailure):  # the old worker's late completion is rejected
        s.save_run(
            running.model_copy(update={"status": "completed", "stage": "save", "result": load_example("result")})
        )
    _start(s, ws, v)  # the workspace is free again
    s.close()


def test_schema_is_versioned(tmp_path):
    s = SqliteStore(tmp_path)
    s.close()
    with sqlite3.connect(tmp_path / "sverka.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        SqliteStore(tmp_path)


def test_crash_before_metadata_commit_stores_no_version(tmp_path, monkeypatch):
    s = SqliteStore(tmp_path)
    ws = s.create_workspace().workspace_id

    def crash(*args, **kwargs):
        raise OSError("simulated crash")

    with monkeypatch.context() as m:
        m.setattr(c, "DocumentVersion", crash)
        with pytest.raises(OSError):
            s.save_document(ws, filename="a.pdf", role="primary", data=b"a")
    assert s.list_versions(ws) == []
    with sqlite3.connect(tmp_path / "sverka.db") as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0  # rolled back with the version
    assert s.read_original(ws, s.save_document(ws, filename="a.pdf", role="primary", data=b"a").version_id) == b"a"
    s.close()


def test_failed_run_save_keeps_previous_state(tmp_path, monkeypatch):
    s = SqliteStore(tmp_path)
    ws = s.create_workspace().workspace_id
    run = _start(s, ws, s.save_document(ws, filename="r.pdf", role="report", data=b"r"))

    def crash(current, new):
        raise RuntimeError("simulated crash inside the transaction")

    monkeypatch.setattr(storage, "check_run_update", crash)
    with pytest.raises(RuntimeError):
        s.save_run(run.model_copy(update={"status": "running"}))
    assert s.get_run(ws, run.run_id).status == "queued"
    s.close()


def test_partial_and_damaged_blob_files(tmp_path):
    s = SqliteStore(tmp_path)
    ws = s.create_workspace().workspace_id
    v = s.save_document(ws, filename="a.pdf", role="primary", data=b"complete file")
    blob = tmp_path / "blobs" / v.sha256[:2] / v.sha256
    stray = blob.parent / f".{v.sha256}.deadbeef.tmp"
    stray.write_bytes(b"half")
    s.close()

    s = SqliteStore(tmp_path)
    assert not stray.exists()  # leftovers of interrupted writes are removed on start
    blob.write_bytes(b"complete")  # truncated on disk
    with pytest.raises(c.ToolFailure) as excinfo:
        s.read_original(ws, v.version_id)
    assert _failure_code(excinfo) == "unreadable"
    s.save_document(ws, filename="again.pdf", role="primary", data=b"complete file")  # rewrite repairs the blob
    assert s.read_original(ws, v.version_id) == b"complete file"
    blob.unlink()
    with pytest.raises(c.ToolFailure) as excinfo:
        s.read_original(ws, v.version_id)
    assert _failure_code(excinfo) == "unreadable"
    s.close()


def test_concurrent_starts_allow_one_active_check(tmp_path):
    s = SqliteStore(tmp_path)
    ws = s.create_workspace().workspace_id
    v = s.save_document(ws, filename="r.pdf", role="report", data=b"r")

    def attempt(_):
        try:
            return _start(s, ws, v).run_id
        except ActiveRunConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        started = [r for r in pool.map(attempt, range(16)) if r]
    assert len(started) == 1
    s.close()


def test_page_region_blocks_must_point_at_a_render(store):
    ws = store.create_workspace().workspace_id
    v = store.save_document(ws, filename="Акт.pdf", role="primary", data=b"%PDF")
    with pytest.raises(c.ToolFailure) as excinfo:  # the original PDF has no pixel coordinates to draw on
        store.save_blocks(ws, [_region_block(v.version_id, v.original_artifact_id)])
    assert _failure_code(excinfo) == "invalid_reference"
    for kind in ("page_render", "crop"):
        art = store.save_artifact(ws, v.version_id, kind=kind, media_type="image/png", data=kind.encode())
        store.save_blocks(ws, [_region_block(v.version_id, art.artifact_id, block_id=f"blk_{kind}")])


def test_second_process_does_not_interrupt_live_checks(tmp_path):
    server = SqliteStore(tmp_path)
    ws = server.create_workspace().workspace_id
    run = _start(server, ws, server.save_document(ws, filename="r.pdf", role="report", data=b"r"))
    server.save_run(run.model_copy(update={"status": "running", "stage": "extract"}))

    cli = SqliteStore(tmp_path)  # e.g. a CLI command or a second worker on the same /data
    assert not cli.is_owner and cli.recovered_runs == []
    assert cli.get_run(ws, run.run_id).status == "running"
    cli.close()

    server.close()  # the owner stops (or crashes): the next start recovers
    restarted = SqliteStore(tmp_path)
    assert restarted.is_owner and restarted.recovered_runs == [run.run_id]
    restarted.close()
