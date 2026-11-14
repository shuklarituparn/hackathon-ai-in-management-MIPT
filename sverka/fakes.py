"""In-memory fakes and example loaders for development and tests (T01).

``InMemoryStore`` implements ``interfaces.DocumentStore`` and ``interfaces.RunStore`` with the same
scoping and error rules the real SQLite store (T02) must follow. ``load_example`` returns the published
example payloads from tests/fixtures/contracts/ as typed objects. Not used by the production runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter

from . import contracts as c
from .interfaces import ActiveRunConflict
from .storage import RENDER_KINDS, check_run_update, clean_filename, document_format, media_type_for

__all__ = [
    "EXAMPLES",
    "EXAMPLES_DIR",
    "InMemoryStore",
    "document_format",
    "dump_json",
    "fake_reconcile",
    "load_example",
]

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "contracts"
)

# Example name -> (file, type). These are the five T01 example payloads.
EXAMPLES: dict[str, tuple[str, Any]] = {
    "report_field": ("report_field.json", c.IndicatorEntry),
    "primary": ("primary.json", c.PrimaryExtraction),
    "unresolved_field": ("unresolved_field.json", c.DecimalObservation),
    "result": ("result.json", c.ReconciliationResult),
    "evidence_blocks": ("evidence_blocks.json", list[c.EvidenceBlock]),
}


def load_example(name: str):
    """Load one published example payload as its contract type, e.g. ``load_example("primary")``."""
    filename, typ = EXAMPLES[name]
    with open(os.path.join(EXAMPLES_DIR, filename), encoding="utf-8") as f:
        return TypeAdapter(typ).validate_json(f.read())


def fake_reconcile(
    scope: c.Scope, report: c.ReportExtraction, primaries: Sequence[c.PrimaryExtraction], manifest: c.BundleManifest
) -> c.ReconciliationResult:
    """Stand-in for reconciliation.reconcile until T10 lands: returns the example result for ``scope``."""
    return load_example("result").model_copy(update={"scope": scope})


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InMemoryStore:
    """Fake for DocumentStore and RunStore. Bytes are content-addressed; versions are never overwritten."""

    def __init__(self) -> None:
        self._workspaces: dict[str, c.Workspace] = {}
        self._blobs: dict[str, bytes] = {}
        self._versions: dict[tuple[str, str], c.DocumentVersion] = {}
        self._artifacts: dict[tuple[str, str], c.ArtifactRef] = {}
        self._blocks: dict[tuple[str, str], c.EvidenceBlock] = {}
        self._runs: dict[tuple[str, str], c.Run] = {}
        self._messages: dict[str, list[c.ChatMessage]] = {}

    # ---------- workspaces ----------
    def create_workspace(self) -> c.Workspace:
        ws = c.Workspace(workspace_id=c.new_id("ws"), created_at=_now())
        self._workspaces[ws.workspace_id] = ws
        self._messages[ws.workspace_id] = []
        return ws.model_copy(deep=True)

    def _ws(self, workspace_id: str) -> None:
        if workspace_id not in self._workspaces:
            raise c.ToolFailure("not_found", f"workspace {workspace_id} not found")

    def get_workspace(self, workspace_id: str) -> c.Workspace:
        self._ws(workspace_id)
        return self._workspaces[workspace_id].model_copy(deep=True)

    # ---------- documents ----------
    def save_document(
        self, workspace_id: str, *, filename: str, role: str, data: bytes, document_id: str | None = None
    ) -> c.DocumentVersion:
        self._ws(workspace_id)
        predecessor = None
        if document_id is not None:
            previous = [v for (w, _), v in self._versions.items() if w == workspace_id and v.document_id == document_id]
            if not previous:
                raise c.ToolFailure("not_found", f"document {document_id} not found")
            predecessor = previous[-1].version_id  # latest by insertion order
        name = clean_filename(filename)
        version_id = c.new_id("ver")
        artifact = self._put_artifact(
            workspace_id, version_id, kind="original", data=data, media_type=media_type_for(name)
        )
        version = c.DocumentVersion(
            workspace_id=workspace_id,
            document_id=document_id or c.new_id("doc"),
            version_id=version_id,
            predecessor_version_id=predecessor,
            sha256=_sha(data),
            size_bytes=len(data),
            role=role,
            original_filename=name,
            format=document_format(name),
            original_artifact_id=artifact.artifact_id,
            created_at=_now(),
        )
        self._versions[(workspace_id, version_id)] = version
        return version.model_copy(deep=True)

    def get_version(self, workspace_id: str, version_id: str) -> c.DocumentVersion:
        self._ws(workspace_id)
        try:
            return self._versions[(workspace_id, version_id)].model_copy(deep=True)
        except KeyError:
            raise c.ToolFailure("not_found", f"version {version_id} not found") from None

    def list_versions(self, workspace_id: str) -> list[c.DocumentVersion]:
        self._ws(workspace_id)
        # Insertion order (timestamps can tie); detached copies.
        return [v.model_copy(deep=True) for (w, _), v in self._versions.items() if w == workspace_id]

    def read_original(self, workspace_id: str, version_id: str) -> bytes:
        version = self.get_version(workspace_id, version_id)
        return self.read_artifact(workspace_id, version.original_artifact_id)[1]

    # ---------- artifacts ----------
    def _put_artifact(
        self, workspace_id: str, version_id: str, *, kind: str, media_type: str, data: bytes
    ) -> c.ArtifactRef:
        sha = _sha(data)
        self._blobs.setdefault(sha, bytes(data))
        ref = c.ArtifactRef(
            workspace_id=workspace_id,
            artifact_id=c.new_id("art"),
            version_id=version_id,
            kind=kind,
            media_type=media_type,
            sha256=sha,
            size_bytes=len(data),
        )
        self._artifacts[(workspace_id, ref.artifact_id)] = ref
        return ref.model_copy(deep=True)

    def save_artifact(
        self, workspace_id: str, version_id: str, *, kind: str, media_type: str, data: bytes
    ) -> c.ArtifactRef:
        self.get_version(workspace_id, version_id)
        return self._put_artifact(workspace_id, version_id, kind=kind, media_type=media_type, data=data)

    def read_artifact(self, workspace_id: str, artifact_id: str) -> tuple[c.ArtifactRef, bytes]:
        self._ws(workspace_id)
        try:
            ref = self._artifacts[(workspace_id, artifact_id)]
        except KeyError:
            raise c.ToolFailure("not_found", f"artifact {artifact_id} not found") from None
        return ref.model_copy(deep=True), self._blobs[ref.sha256]

    # ---------- evidence ----------
    def save_blocks(self, workspace_id: str, blocks: Sequence[c.EvidenceBlock]) -> None:
        self._ws(workspace_id)
        pending: dict[tuple[str, str], c.EvidenceBlock] = {}
        for b in blocks:
            if (workspace_id, b.version_id) not in self._versions:
                raise c.ToolFailure("invalid_reference", f"block {b.block_id} names unknown version {b.version_id}")
            if b.locator.kind == "page_region":
                ref = self._artifacts.get((workspace_id, b.locator.render_id))
                if ref is None or ref.version_id != b.version_id or ref.kind not in RENDER_KINDS:
                    raise c.ToolFailure(
                        "invalid_reference",
                        f"block {b.block_id} must name a page render or crop of version {b.version_id}",
                    )
            key = (workspace_id, b.block_id)
            existing = pending.get(key, self._blocks.get(key))
            if existing is not None and existing != b:
                raise c.ToolFailure("invalid_reference", f"block {b.block_id} already exists with other content")
            pending[key] = b.model_copy(deep=True)
        self._blocks.update(pending)

    def get_block(self, workspace_id: str, block_id: str) -> c.EvidenceBlock:
        self._ws(workspace_id)
        try:
            return self._blocks[(workspace_id, block_id)].model_copy(deep=True)
        except KeyError:
            raise c.ToolFailure("not_found", f"evidence {block_id} not found") from None

    # ---------- runs ----------
    def create_run(self, workspace_id: str, request: c.StartRunRequest) -> c.Run:
        self._ws(workspace_id)
        for vid in request.version_ids:
            if (workspace_id, vid) not in self._versions:
                raise c.ToolFailure("invalid_reference", f"version {vid} is not in workspace {workspace_id}")
        if request.predecessor_run_id is not None:
            self.get_run(workspace_id, request.predecessor_run_id)
        if any(w == workspace_id and r.status in ("queued", "running") for (w, _), r in self._runs.items()):
            raise ActiveRunConflict(f"workspace {workspace_id} already has an active check")
        now = _now()
        run = c.Run(
            workspace_id=workspace_id,
            run_id=c.new_id("run"),
            predecessor_run_id=request.predecessor_run_id,
            input_version_ids=list(request.version_ids),
            scope=request.scope.model_copy(deep=True),
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._runs[(workspace_id, run.run_id)] = run
        return run.model_copy(deep=True)

    def get_run(self, workspace_id: str, run_id: str) -> c.Run:
        self._ws(workspace_id)
        try:
            return self._runs[(workspace_id, run_id)].model_copy(deep=True)
        except KeyError:
            raise c.ToolFailure("not_found", f"run {run_id} not found") from None

    def list_runs(self, workspace_id: str) -> list[c.Run]:
        self._ws(workspace_id)
        return [r.model_copy(deep=True) for (w, _), r in self._runs.items() if w == workspace_id]

    def save_run(self, run: c.Run) -> None:
        run = c.Run.model_validate(run.model_dump())  # a fresh, validated copy detached from the caller's object
        check_run_update(self.get_run(run.workspace_id, run.run_id), run)
        self._runs[(run.workspace_id, run.run_id)] = run

    # ---------- conversation ----------
    def list_messages(self, workspace_id: str) -> list[c.ChatMessage]:
        self._ws(workspace_id)
        return [m.model_copy(deep=True) for m in self._messages[workspace_id]]

    def append_message(self, message: c.ChatMessage) -> None:
        self._ws(message.workspace_id)
        if message.run_id is not None:
            self.get_run(message.workspace_id, message.run_id)
        if any(m.message_id == message.message_id for msgs in self._messages.values() for m in msgs):
            raise c.ToolFailure("invalid_reference", f"message {message.message_id} already exists")
        self._messages[message.workspace_id].append(message.model_copy(deep=True))


def dump_json(obj: Any) -> str:
    """Canonical JSON for contract objects (decimal strings, ISO dates, UTF-8 kept readable)."""
    data = (
        obj.model_dump(mode="json")
        if hasattr(obj, "model_dump")
        else TypeAdapter(type(obj)).dump_python(obj, mode="json")
    )
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
