"""Service interfaces (T01) that let each stream build against fakes before the real parts exist.

Owners: storage/runs and reconcile - Aadi (T02, T10); evidence tools - Eva (T03-T06);
model provider - Guillermo (T08). In-memory fakes are in ``sverka.fakes``.

Methods raise ``contracts.ToolFailure`` with a typed code (``not_found``, ``invalid_reference``, ...).
An ID from another workspace is ``not_found``: callers cannot probe other workspaces.
Evidence tools return ``ToolResult`` instead of raising, because their output goes back to the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .contracts import (
    ArtifactKind,
    ArtifactRef,
    Box,
    BundleManifest,
    ChatMessage,
    DocumentRole,
    DocumentVersion,
    EvidenceBlock,
    ExtractionProposal,
    PageImage,
    PreparedDocument,
    PrimaryExtraction,
    ReconciliationResult,
    ReportExtraction,
    Run,
    Scope,
    StartRunRequest,
    ToolResult,
    ValidationOutcome,
    Workspace,
)
from .model_adapter import ModelMessage, ModelResponse, ToolSpec


class ActiveRunConflict(Exception):
    """Raised by RunStore.create_run when the workspace already has a queued or running check."""


@runtime_checkable
class DocumentStore(Protocol):
    """Immutable originals, artifacts and evidence blocks, all scoped to a workspace."""

    def create_workspace(self) -> Workspace: ...

    def get_workspace(self, workspace_id: str) -> Workspace: ...

    def save_document(
        self, workspace_id: str, *, filename: str, role: DocumentRole, data: bytes, document_id: str | None = None
    ) -> DocumentVersion:
        """Store bytes as a new version. Pass ``document_id`` to add a replacement version of an existing
        document; the filename is metadata only and never decides where bytes are stored."""
        ...

    def get_version(self, workspace_id: str, version_id: str) -> DocumentVersion: ...

    def list_versions(self, workspace_id: str) -> list[DocumentVersion]: ...

    def read_original(self, workspace_id: str, version_id: str) -> bytes: ...

    def save_artifact(
        self, workspace_id: str, version_id: str, *, kind: ArtifactKind, media_type: str, data: bytes
    ) -> ArtifactRef: ...

    def read_artifact(self, workspace_id: str, artifact_id: str) -> tuple[ArtifactRef, bytes]: ...

    def save_blocks(self, workspace_id: str, blocks: Sequence[EvidenceBlock]) -> None:
        """Add blocks. Existing blocks are never overwritten (re-OCR adds new blocks)."""
        ...

    def get_block(self, workspace_id: str, block_id: str) -> EvidenceBlock: ...


@runtime_checkable
class RunStore(Protocol):
    """Runs and conversation. ``save_run`` replaces the stored run atomically."""

    def create_run(self, workspace_id: str, request: StartRunRequest) -> Run: ...

    def get_run(self, workspace_id: str, run_id: str) -> Run: ...

    def list_runs(self, workspace_id: str) -> list[Run]: ...

    def save_run(self, run: Run) -> None:
        """Replace the run atomically. Finished runs are final and identity fields (inputs, scope) never change;
        both raise ToolFailure('invalid_reference'), so a late completion after cancel is rejected."""
        ...

    def list_messages(self, workspace_id: str) -> list[ChatMessage]: ...

    def append_message(self, message: ChatMessage) -> None: ...


@runtime_checkable
class EvidenceTools(Protocol):
    """Document tools exposed to the harness (Eva, T03-T06). Signatures are v1; changes need review."""

    def prepare_document(self, workspace_id: str, version_id: str) -> ToolResult[PreparedDocument]: ...

    def read_evidence(self, workspace_id: str, block_ids: Sequence[str]) -> ToolResult[list[EvidenceBlock]]: ...

    def inspect_page(self, workspace_id: str, version_id: str, page: int) -> ToolResult[PageImage]: ...

    def inspect_crop(self, workspace_id: str, render_id: str, box: Box) -> ToolResult[ArtifactRef]: ...

    def reocr_region(self, workspace_id: str, render_id: str, box: Box) -> ToolResult[list[EvidenceBlock]]: ...

    def read_cells(
        self, workspace_id: str, version_id: str, sheet: str, ref: str
    ) -> ToolResult[list[EvidenceBlock]]: ...

    def validate_extraction(
        self, workspace_id: str, proposal: ExtractionProposal, prepared: PreparedDocument
    ) -> ToolResult[ValidationOutcome]:
        """The only place that assigns accepted/unresolved."""
        ...


@runtime_checkable
class ModelProvider(Protocol):
    """One configured model endpoint (T08). Guillermo owns this interface; it is not a shared data contract.

    Messages, tool calls and responses are the normalized types of ``sverka.model_adapter``. ``complete``
    makes one attempt within ``timeout`` seconds and raises ``model_adapter.ProviderError`` on failure; the
    harness owns retries and budgets. ``describe`` returns settings that are safe to store: no credentials.
    """

    name: str
    model: str

    def complete(
        self, messages: Sequence[ModelMessage], tools: Sequence[ToolSpec], *, timeout: float
    ) -> ModelResponse: ...

    def describe(self) -> dict[str, str | int | float | bool | None]: ...


class Reconcile(Protocol):
    """Signature of reconciliation.reconcile (T10): a pure function of validated facts."""

    def __call__(
        self, scope: Scope, report: ReportExtraction, primaries: Sequence[PrimaryExtraction], manifest: BundleManifest
    ) -> ReconciliationResult: ...
