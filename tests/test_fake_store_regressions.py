"""Regression checks for detached store snapshots and immutable evidence batches.

Each check runs against the in-memory fake and the SQLite store, so both keep the same guarantees.
"""

import pytest

from sverka import contracts as c
from sverka.fakes import InMemoryStore, load_example
from sverka.storage import SqliteStore


@pytest.fixture(params=["memory", "sqlite"])
def stored_run(request, tmp_path):
    store = InMemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "data")
    workspace = store.create_workspace().workspace_id
    version = store.save_document(workspace, filename="report.pdf", role="report", data=b"report")
    start = c.StartRunRequest(version_ids=[version.version_id], scope=load_example("result").scope)
    run = store.create_run(workspace, start)
    yield store, workspace, version, start, run
    if isinstance(store, SqliteStore):
        store.close()


def _block(version, block_id="blk_test", text="original"):
    return c.EvidenceBlock(
        version_id=version.version_id,
        block_id=block_id,
        text=text,
        method="docx",
        locator=c.DocxLocator(quote=text, paragraph=0),
    )


def test_run_reads_and_saves_are_detached(stored_run):
    store, workspace, _, _, created = stored_run
    created.status = "running"
    assert store.get_run(workspace, created.run_id).status == "queued"
    store.save_run(created)
    created.scope.report_row = "changed after save"
    assert store.get_run(workspace, created.run_id).scope.report_row != created.scope.report_row

    read = store.get_run(workspace, created.run_id)
    read.status = "cancelled"
    assert store.get_run(workspace, created.run_id).status == "running"
    store.save_run(read)
    assert store.get_run(workspace, created.run_id).status == "cancelled"


def test_terminal_guard_cannot_be_bypassed_by_mutating_a_read(stored_run):
    store, workspace, _, _, run = stored_run
    run.status = "cancelled"
    store.save_run(run)
    read = store.get_run(workspace, run.run_id)
    read.status = "running"
    with pytest.raises(c.ToolFailure, match="already cancelled"):
        store.save_run(read)
    assert store.get_run(workspace, run.run_id).status == "cancelled"


def test_run_creation_snapshots_nested_request_data(stored_run):
    store, workspace, version, request, run = stored_run
    request.scope.report_row = "caller changed scope"
    request.version_ids.clear()
    run.scope.contract = "caller changed returned scope"
    run.input_version_ids.clear()
    saved = store.get_run(workspace, run.run_id)
    assert saved.scope.report_row != request.scope.report_row
    assert saved.scope.contract != run.scope.contract
    assert saved.input_version_ids == [version.version_id]


def test_versions_and_artifact_references_are_detached(stored_run):
    store, workspace, version, _, _ = stored_run
    version.original_filename = "changed.pdf"
    assert store.get_version(workspace, version.version_id).original_filename == "report.pdf"
    fetched = store.get_version(workspace, version.version_id)
    fetched.sha256 = "0" * 64
    listed = store.list_versions(workspace)
    listed[0].original_filename = "changed again.pdf"
    assert store.get_version(workspace, version.version_id).sha256 != fetched.sha256
    assert store.get_version(workspace, version.version_id).original_filename == "report.pdf"

    artifact = store.save_artifact(workspace, version.version_id, kind="crop", media_type="image/png", data=b"crop")
    artifact.media_type = "text/html"
    fetched_ref, data = store.read_artifact(workspace, artifact.artifact_id)
    assert data == b"crop" and fetched_ref.media_type == "image/png"
    fetched_ref.sha256 = "0" * 64
    assert store.read_artifact(workspace, artifact.artifact_id)[1] == b"crop"


def test_saved_and_returned_evidence_are_deep_snapshots(stored_run):
    store, workspace, version, _, _ = stored_run
    block = _block(version)
    store.save_blocks(workspace, [block])
    block.text = "changed after save"
    block.locator.quote = "changed nested value"
    fetched = store.get_block(workspace, block.block_id)
    assert fetched.text == "original" and fetched.locator.quote == "original"
    fetched.locator.quote = "changed after read"
    assert store.get_block(workspace, block.block_id).locator.quote == "original"


def test_conflicting_ids_in_batch_reject_every_write(stored_run):
    store, workspace, version, _, _ = stored_run
    original = _block(version)
    independent = _block(version, block_id="blk_independent")
    conflicting = _block(version, text="different")
    with pytest.raises(c.ToolFailure, match="already exists"):
        store.save_blocks(workspace, [independent, original, conflicting])
    for block in (original, independent):
        with pytest.raises(c.ToolFailure):
            store.get_block(workspace, block.block_id)


def test_identical_block_repeats_are_idempotent(stored_run):
    store, workspace, version, _, _ = stored_run
    block = _block(version)
    store.save_blocks(workspace, [block, block.model_copy(deep=True)])
    store.save_blocks(workspace, [block.model_copy(deep=True)])
    assert store.get_block(workspace, block.block_id) == block


def test_messages_are_detached_on_append_and_read(stored_run):
    store, workspace, _, _, run = stored_run
    message = c.ChatMessage(
        workspace_id=workspace,
        message_id=c.new_id("msg"),
        run_id=run.run_id,
        role="assistant",
        text="checking",
        cards=[c.RunStatusCard(run_id=run.run_id, status="running")],
        created_at=run.created_at,
    )
    store.append_message(message)
    message.cards[0].status = "failed"
    message.text = "changed"
    fetched = store.list_messages(workspace)
    assert fetched[0].text == "checking" and fetched[0].cards[0].status == "running"
    fetched[0].cards.clear()
    fetched.clear()
    assert len(store.list_messages(workspace)[0].cards) == 1
