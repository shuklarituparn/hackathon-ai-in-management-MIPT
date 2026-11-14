"""Shared v1 contracts (T01): data schemas, statuses, tool errors and evidence locators.

Reference: docs/architecture/README.md#contracts. Example payloads live in tests/fixtures/contracts/;
the published JSON Schema is tests/fixtures/contracts/schema.json (regenerate with
``python -m sverka.contracts > tests/fixtures/contracts/schema.json``).

Rules that every producer and consumer relies on:

* Money and other measured quantities are decimal strings in JSON (``"1145798.10"``) and ``Decimal`` in
  Python. Floats and JSON numbers are rejected; the string keeps its scale, so ``"1000"`` stays ``1000``.
* Dates are ISO ``YYYY-MM-DD``; timestamps are timezone-aware ISO 8601.
* Zero is a value. ``null``/``None`` means unresolved or unknown, never zero.
* ``ProposedField`` is what the model says. Only ``validate_extraction`` turns it into a ``FieldObservation``
  with ``state`` ``accepted`` or ``unresolved``; a proposal has no acceptance flag.
* Tools return ``ToolResult``: ``ok``, ``data``, ``warnings`` and, on failure, a typed ``ToolError``.
  Errors never become empty successful extractions.

Breaking changes to this module need explicit review from the affected consumers (Eva, Guillermo).
Update the schema file and examples in the same change.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)
from pydantic.json_schema import models_json_schema

CONTRACT_VERSION = "1.0.0"

# ======================= primitives =======================

DECIMAL_PATTERN = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
_DECIMAL_RE = re.compile(DECIMAL_PATTERN)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("decimal must be finite")
        return value
    if isinstance(value, str):
        if not _DECIMAL_RE.fullmatch(value):
            raise ValueError(
                "decimal string must look like '1145798.10' or '-12500.00' (no spaces, no comma, no exponent)"
            )
        return Decimal(value)
    raise ValueError(
        f"expected a decimal string, got {type(value).__name__}; JSON numbers and floats are not allowed for amounts"
    )


def _dump_decimal(value: Decimal) -> str:
    return format(value, "f")


DecimalStr = Annotated[
    Decimal,
    PlainValidator(_to_decimal),
    PlainSerializer(_dump_decimal, return_type=str, when_used="json"),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": DECIMAL_PATTERN,
            "description": 'Decimal as a string, e.g. "1145798.10". Never a JSON number.',
        }
    ),
]

ISO_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def _to_date(value: Any) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if isinstance(value, str) and re.fullmatch(ISO_DATE_PATTERN, value):
        return date.fromisoformat(value)
    raise ValueError("expected an ISO date string YYYY-MM-DD")


IsoDate = Annotated[
    date,
    PlainValidator(_to_date),
    PlainSerializer(lambda d: d.isoformat(), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date", "pattern": ISO_DATE_PATTERN}),
]


def _id_type(prefix: str):
    return Annotated[str, StringConstraints(pattern=rf"^{prefix}_[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$")]


WorkspaceId = _id_type("ws")
DocumentId = _id_type("doc")
VersionId = _id_type("ver")
BlockId = _id_type("blk")
ArtifactId = _id_type("art")
RunId = _id_type("run")
MessageId = _id_type("msg")

ID_PREFIXES = {
    "workspace": "ws",
    "document": "doc",
    "version": "ver",
    "block": "blk",
    "artifact": "art",
    "run": "run",
    "message": "msg",
}


def new_id(prefix: str) -> str:
    """Return a fresh opaque ID such as ``ver_3f2a...``. ``prefix`` is one of ``ID_PREFIXES`` values."""
    if prefix not in ID_PREFIXES.values():
        raise ValueError(f"unknown ID prefix: {prefix}")
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# Report inventory entry, e.g. "col15" or "contract".
EntryId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
# Cost category code. Open vocabulary so other report layouts can define their own; see KNOWN_CATEGORIES.
CategoryCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

# Categories used by the supplied 17-column report. The report's IndicatorEntry.components define the
# hierarchy (payroll_total = payroll_account + leave_reserve, ...). "unrelated" lets the validator accept
# that a document supports no indicator, which closes it for zero checks.
KNOWN_CATEGORIES = (
    "workforce_intensity",
    "materials",
    "payroll_total",
    "payroll_account",
    "payroll_main",
    "payroll_additional",
    "leave_reserve",
    "contributions_total",
    "contributions_account",
    "contributions_main",
    "contributions_additional",
    "contributions_reserve",
    "production_overhead",
    "administrative_overhead",
    "total",
    "unrelated",
)

# ======================= enumerations =======================

DocumentRole = Literal["report", "primary", "context"]
DocumentFormat = Literal["pdf", "png", "jpeg", "tiff", "docx", "xlsx", "txt", "other"]
ExtractionMethod = Literal["native_text", "ocr", "reocr", "vision", "spreadsheet", "docx"]
ToolErrorCode = Literal["unreadable", "unsupported", "not_found", "invalid_reference", "timeout"]
FieldState = Literal["accepted", "unresolved"]
Basis = Literal["observed", "inferred"]
# literal: plain value, no formula. derived: formula evaluated by our code. unresolved: formula could not be
# evaluated (unknown dependency or unsupported function). error: the formula evaluates to an error.
EvaluationState = Literal["literal", "derived", "unresolved", "error"]
RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
RunStage = Literal["prepare", "extract", "validate", "reconcile", "save"]
RunFailureCode = Literal[
    "missing_credentials", "provider_error", "timeout", "budget_exhausted", "invalid_input", "internal_error"
]
Coverage = Literal["complete", "partial", "none"]
IndicatorStatus = Literal["verified", "discrepancy", "insufficient_evidence"]
DISCREPANCY_PROBLEMS = (
    "amount_mismatch",
    "duplicate",
    "wrong_period",
    "wrong_contract",
    "omitted_document",
    "report_arithmetic",
)
INSUFFICIENT_PROBLEMS = (
    "missing_coverage",
    "no_supporting_document",
    "unreadable_input",
    "unresolved_interpretation",
    "conflicting_versions",
    "unreadable_report_value",
)
ProblemCategory = Literal[
    "amount_mismatch",
    "duplicate",
    "wrong_period",
    "wrong_contract",
    "omitted_document",
    "report_arithmetic",
    "missing_coverage",
    "no_supporting_document",
    "unreadable_input",
    "unresolved_interpretation",
    "conflicting_versions",
    "unreadable_report_value",
]
ExclusionReason = Literal["duplicate", "outside_period", "other_contract", "unrelated", "context_only"]
ArtifactKind = Literal["original", "page_render", "crop"]
InspectionTool = Literal["read_evidence", "inspect_page", "inspect_crop", "reocr_region", "read_cells"]


class Contract(BaseModel):
    """Base for all contract models: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")


# ======================= errors =======================


class ToolError(Contract):
    code: ToolErrorCode
    message: NonEmptyStr
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolFailure(Exception):
    """Raise inside tools/stores; convert to ``ToolResult`` with ``error_result(exc.error)`` at the boundary."""

    def __init__(
        self, code: ToolErrorCode, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None
    ):
        super().__init__(f"{code}: {message}")
        self.error = ToolError(code=code, message=message, retryable=retryable, details=details or {})


T = TypeVar("T")


class ToolResult(Contract, Generic[T]):
    ok: bool
    data: T | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ToolError | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.ok and self.error is not None:
            raise ValueError("a successful result cannot carry an error")
        if not self.ok and (self.error is None or self.data is not None):
            raise ValueError("a failed result needs an error and no data")
        return self


def ok_result(data: Any, warnings: list[str] | None = None) -> ToolResult:
    return ToolResult(ok=True, data=data, warnings=warnings or [])


def error_result(error: ToolError | ToolFailure, warnings: list[str] | None = None) -> ToolResult:
    if isinstance(error, ToolFailure):
        error = error.error
    return ToolResult(ok=False, error=error, warnings=warnings or [])


class Finding(Contract):
    """A machine-readable observation about a field, document or indicator."""

    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    message: NonEmptyStr
    evidence_ids: list[BlockId] = Field(default_factory=list)
    version_ids: list[VersionId] = Field(default_factory=list)


# ======================= scope and documents =======================


class Period(Contract):
    """Inclusive date range."""

    start: IsoDate
    end: IsoDate

    @model_validator(mode="after")
    def _order(self):
        if self.start > self.end:
            raise ValueError("period start is after its end")
        return self


class Scope(Contract):
    """What is being checked. The user confirms it before a run starts."""

    contract: NonEmptyStr
    report_row: NonEmptyStr
    date_from: IsoDate
    date_to: IsoDate
    coverage_basis: Literal["supplied_bundle"] = "supplied_bundle"
    confirmed: bool = False

    @model_validator(mode="after")
    def _order(self):
        if self.date_from > self.date_to:
            raise ValueError("scope date_from is after date_to")
        return self


class Workspace(Contract):
    workspace_id: WorkspaceId
    created_at: AwareDatetime


class DocumentVersion(Contract):
    """One immutable uploaded file. A replacement upload is a new version of the same document_id."""

    workspace_id: WorkspaceId
    document_id: DocumentId
    version_id: VersionId
    predecessor_version_id: VersionId | None = None
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0)]
    role: DocumentRole
    original_filename: NonEmptyStr
    format: DocumentFormat
    original_artifact_id: ArtifactId
    created_at: AwareDatetime


class ArtifactRef(Contract):
    """A stored file: the original, a page render or a crop. Always scoped to one document version."""

    workspace_id: WorkspaceId
    artifact_id: ArtifactId
    version_id: VersionId
    kind: ArtifactKind
    media_type: NonEmptyStr
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0)]


# ======================= evidence locators =======================


class Box(Contract):
    """Pixel box in the coordinate space of one stored render: left, top, right, bottom."""

    x0: Annotated[int, Field(ge=0)]
    y0: Annotated[int, Field(ge=0)]
    x1: Annotated[int, Field(ge=0)]
    y1: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _order(self):
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("box must have x0 < x1 and y0 < y1")
        return self


class RenderTransform(Contract):
    """How a stored render relates to the original page.

    ``rotation`` is the clockwise rotation applied to the original page; ``crop`` is the region of the rotated
    full-page render (at ``dpi``) shown by this render, or null for the whole page.
    """

    rotation: Literal[0, 90, 180, 270] = 0
    dpi: Annotated[int, Field(gt=0)] | None = None
    crop: Box | None = None


class PageRegionLocator(Contract):
    """PDF/PNG/JPEG evidence: a 1-based page and a pixel box on a stored render of that page."""

    kind: Literal["page_region"] = "page_region"
    page: Annotated[int, Field(ge=1)]
    render_id: ArtifactId
    box: Box
    image_width: Annotated[int, Field(gt=0)]
    image_height: Annotated[int, Field(gt=0)]
    transform: RenderTransform = Field(default_factory=RenderTransform)

    @model_validator(mode="after")
    def _inside(self):
        if self.box.x1 > self.image_width or self.box.y1 > self.image_height:
            raise ValueError("box lies outside the render dimensions")
        return self


CELL_REF_PATTERN = r"^\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(:\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?$"
CellRef = Annotated[str, StringConstraints(pattern=CELL_REF_PATTERN)]
# Input-cell lineage may name another sheet: "B2" or "Sheet 1!B2".
QualifiedCellRef = Annotated[
    str,
    StringConstraints(
        pattern=r"^([^!]+!)?\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}"
        r"(:\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?$"
    ),
]


class CellValueState(Contract):
    """Spreadsheet cell or range with its formula state kept separate from any value we derive."""

    sheet: NonEmptyStr
    ref: CellRef
    formula: str | None = None  # original formula text, e.g. "=SUM(C6:C7)"
    cached_value: str | None = None  # value stored in the file (the literal, or the formula's cached result)
    derived_value: DecimalStr | None = None  # value our code computed from inputs; never copied from cache
    evaluation: EvaluationState
    inputs: list[QualifiedCellRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _states(self):
        if self.evaluation == "literal":
            if self.formula is not None or self.derived_value is not None or self.inputs:
                raise ValueError("a literal cell has no formula, derived value or inputs")
        elif self.formula is None:
            raise ValueError(f"evaluation '{self.evaluation}' requires the original formula")
        if self.evaluation == "derived" and self.derived_value is None:
            raise ValueError("evaluation 'derived' requires derived_value")
        if self.evaluation in ("unresolved", "error") and self.derived_value is not None:
            raise ValueError(f"evaluation '{self.evaluation}' cannot have a derived_value")
        return self


class CellLocator(CellValueState):
    """XLSX evidence."""

    kind: Literal["cell"] = "cell"


class FormulaState(CellValueState):
    """Formula state recorded during preparation, including errors from evaluation."""

    error: str | None = None


class DocxLocator(Contract):
    """DOCX evidence: the exact quote plus a 0-based paragraph index or table/row/cell address."""

    kind: Literal["docx"] = "docx"
    quote: NonEmptyStr
    paragraph: Annotated[int, Field(ge=0)] | None = None
    table: Annotated[int, Field(ge=0)] | None = None
    row: Annotated[int, Field(ge=0)] | None = None
    cell: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _address(self):
        table_parts = (self.table, self.row, self.cell)
        has_table = all(p is not None for p in table_parts)
        if any(p is not None for p in table_parts) and not has_table:
            raise ValueError("a table address needs table, row and cell")
        if (self.paragraph is None) == (not has_table):
            raise ValueError("give exactly one address: paragraph, or table/row/cell")
        return self


Locator = Annotated[PageRegionLocator | CellLocator | DocxLocator, Field(discriminator="kind")]

_METHODS_BY_LOCATOR = {
    "page_region": {"native_text", "ocr", "reocr", "vision"},
    "cell": {"spreadsheet"},
    "docx": {"docx"},
}


class OcrQuality(Contract):
    engine: NonEmptyStr
    confidence: Annotated[float, Field(ge=0, le=100)] | None = None
    language: str | None = None


class EvidenceBlock(Contract):
    """A located fragment of one document version. Coordinates come from document tools, not the model."""

    version_id: VersionId
    block_id: BlockId
    text: str
    method: ExtractionMethod
    locator: Locator
    quality: OcrQuality | None = None
    derived_from: BlockId | None = None  # e.g. a re-OCR block points at the block it re-read

    @model_validator(mode="after")
    def _method_matches_locator(self):
        if self.method not in _METHODS_BY_LOCATOR[self.locator.kind]:
            raise ValueError(f"method '{self.method}' does not fit a '{self.locator.kind}' locator")
        if self.derived_from == self.block_id:
            raise ValueError("a block cannot be derived from itself")
        return self


class PageImage(Contract):
    page: Annotated[int, Field(ge=1)]
    render_id: ArtifactId
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    transform: RenderTransform = Field(default_factory=RenderTransform)


class PreparedDocument(Contract):
    """Output of prepare_document: what the document says, before any interpretation."""

    version: DocumentVersion
    blocks: list[EvidenceBlock] = Field(default_factory=list)
    pages: list[PageImage] = Field(default_factory=list)
    formula_states: list[FormulaState] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self):
        vid = self.version.version_id
        ids = [b.block_id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate block_id in prepared document")
        renders = {p.render_id: p for p in self.pages}
        if len(renders) != len(self.pages):
            raise ValueError("duplicate render_id in pages")
        for b in self.blocks:
            if b.version_id != vid:
                raise ValueError(f"block {b.block_id} belongs to version {b.version_id}, not {vid}")
            loc = b.locator
            if loc.kind == "page_region":
                page = renders.get(loc.render_id)
                if page is None or page.page != loc.page:
                    raise ValueError(
                        f"block {b.block_id} points at render {loc.render_id} not listed for page {loc.page}"
                    )
        return self


# ======================= extraction: proposals (model output) =======================


class ProposedField(Contract):
    """What the model proposes for one field. There is deliberately no acceptance flag.

    ``value`` is canonical text: decimals as "1145798.10", dates as "2026-01-31", periods as
    "2026-01-01/2026-01-31", lists (contracts) as a JSON list of strings. ``source_text`` is the exact
    transcription as printed ("1 145 798,10") so the validator can compare it with the evidence.
    """

    value: str | list[str] | None
    source_text: str | None = None
    evidence_ids: list[BlockId] = Field(default_factory=list)
    basis: Basis = "observed"
    unresolved_reason: str | None = None

    @model_validator(mode="after")
    def _reason(self):
        if self.value is None and not self.unresolved_reason:
            raise ValueError("a proposal without a value needs unresolved_reason")
        return self


class HeaderProposal(Contract):
    entry_id: EntryId
    label: NonEmptyStr
    label_evidence_ids: list[BlockId] = Field(default_factory=list)
    value: ProposedField


class IndicatorProposal(Contract):
    entry_id: EntryId
    label: NonEmptyStr
    column: Annotated[int, Field(ge=1)] | None = None
    unit: str | None = None
    label_evidence_ids: list[BlockId] = Field(default_factory=list)
    category: CategoryCode | None = None
    components: list[EntryId] = Field(default_factory=list)
    value: ProposedField


class ReportProposal(Contract):
    kind: Literal["report"] = "report"
    version_id: VersionId
    row_label: NonEmptyStr
    headers: list[HeaderProposal] = Field(default_factory=list)
    indicators: list[IndicatorProposal]


class AllocationProposal(Contract):
    line_label: str | None = None
    category: ProposedField
    possible_categories: list[CategoryCode] = Field(default_factory=list)
    amount: ProposedField


class PrimaryProposal(Contract):
    kind: Literal["primary"] = "primary"
    version_id: VersionId
    doc_type: ProposedField
    number: ProposedField
    contracts: ProposedField
    counterparty: ProposedField
    issue_date: ProposedField
    service_period: ProposedField
    amount: ProposedField
    currency: ProposedField
    unit: ProposedField
    allocations: list[AllocationProposal] = Field(default_factory=list)
    possible_categories: list[CategoryCode] = Field(default_factory=list)


ExtractionProposal = Annotated[ReportProposal | PrimaryProposal, Field(discriminator="kind")]

# ======================= extraction: validated observations =======================


class FieldObservation(Contract, Generic[T]):
    """Validator output for one field. Use the concrete subclasses below in schemas.

    accepted: ``value`` is set and backed by ``evidence_ids``. unresolved: ``value`` is null and ``reason``
    says why. Zero is an accepted value; null is never zero.
    """

    state: FieldState
    value: T | None = None
    source_text: str | None = None
    evidence_ids: list[BlockId] = Field(default_factory=list)
    basis: Basis | None = None
    findings: list[Finding] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _state(self):
        if self.state == "accepted":
            if self.value is None:
                raise ValueError("an accepted observation needs a value")
            if not self.evidence_ids:
                raise ValueError("an accepted observation needs evidence_ids")
            if self.basis is None:
                raise ValueError("an accepted observation needs basis 'observed' or 'inferred'")
        else:
            if self.value is not None:
                raise ValueError("an unresolved observation must have value null")
            if not self.reason:
                raise ValueError("an unresolved observation needs a reason")
        return self


class DecimalObservation(FieldObservation[DecimalStr]):
    pass


class TextObservation(FieldObservation[str]):
    pass


class TextListObservation(FieldObservation[list[str]]):
    pass


class DateObservation(FieldObservation[IsoDate]):
    pass


class PeriodObservation(FieldObservation[Period]):
    pass


class CategoryObservation(FieldObservation[CategoryCode]):
    pass


class HeaderEntry(Contract):
    entry_id: EntryId
    label: NonEmptyStr
    label_evidence_ids: list[BlockId] = Field(default_factory=list)
    observation: TextObservation


class IndicatorEntry(Contract):
    """One numeric report indicator: its printed label/unit, meaning and validated value."""

    entry_id: EntryId
    label: NonEmptyStr
    column: Annotated[int, Field(ge=1)] | None = None
    unit: str | None = None
    label_evidence_ids: list[BlockId] = Field(default_factory=list)
    category: CategoryCode | None = None
    components: list[EntryId] = Field(default_factory=list)  # entries whose sum the report says this equals
    observation: DecimalObservation

    @model_validator(mode="after")
    def _no_self(self):
        if self.entry_id in self.components:
            raise ValueError("an indicator cannot be its own component")
        return self


class ReportExtraction(Contract):
    """Header/indicator inventory with one observation per entry, including unreadable ones."""

    kind: Literal["report"] = "report"
    version_id: VersionId
    row_label: NonEmptyStr
    headers: list[HeaderEntry] = Field(default_factory=list)
    indicators: list[IndicatorEntry]
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _entries(self):
        ids = [e.entry_id for e in self.headers] + [e.entry_id for e in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate entry_id in report inventory")
        indicator_ids = {e.entry_id for e in self.indicators}
        for e in self.indicators:
            missing = set(e.components) - indicator_ids
            if missing:
                raise ValueError(f"{e.entry_id} names unknown components: {sorted(missing)}")
        return self


class AllocationLine(Contract):
    """Part of a primary document's amount assigned to a category.

    ``category`` is the most specific category the evidence supports. When a line covers several finer
    categories that cannot be split (e.g. "additional payroll and leave reserve"), accept the parent category
    and list the finer ones in ``possible_categories``.
    """

    line_label: str | None = None
    category: CategoryObservation
    possible_categories: list[CategoryCode] = Field(default_factory=list)
    amount: DecimalObservation


class PrimaryExtraction(Contract):
    kind: Literal["primary"] = "primary"
    version_id: VersionId
    doc_type: TextObservation
    number: TextObservation
    contracts: TextListObservation
    counterparty: TextObservation
    issue_date: DateObservation
    service_period: PeriodObservation
    amount: DecimalObservation
    currency: TextObservation
    unit: TextObservation
    allocations: list[AllocationLine] = Field(default_factory=list)
    possible_categories: list[CategoryCode] = Field(default_factory=list)
    conflicts: list[Finding] = Field(default_factory=list)


ExtractionResult = Annotated[ReportExtraction | PrimaryExtraction, Field(discriminator="kind")]


class ReinspectionRequest(Contract):
    """Validator's request for the harness to look again before a field is accepted."""

    field: NonEmptyStr  # dotted path, e.g. "amount" or "allocations.0.category"
    reason: NonEmptyStr
    block_ids: list[BlockId] = Field(default_factory=list)
    suggested_tool: InspectionTool | None = None


class ValidationOutcome(Contract):
    """Data returned by validate_extraction."""

    extraction: ExtractionResult
    reinspection: list[ReinspectionRequest] = Field(default_factory=list)


# ======================= reconciliation =======================


class ManifestEntry(Contract):
    version: DocumentVersion
    preparation: Literal["prepared", "partial", "failed"]
    errors: list[ToolError] = Field(default_factory=list)


class BundleManifest(Contract):
    """Every supplied document, including unreadable ones, so coverage can be judged."""

    workspace_id: WorkspaceId
    entries: list[ManifestEntry]

    @model_validator(mode="after")
    def _consistent(self):
        ids = [e.version.version_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate version in manifest")
        if any(e.version.workspace_id != self.workspace_id for e in self.entries):
            raise ValueError("manifest mixes workspaces")
        return self


class Contribution(Contract):
    """An accepted amount from one source counted towards an indicator."""

    version_id: VersionId
    label: NonEmptyStr  # human-readable source, e.g. "Счет-акт № 174 от 25.02.2026"
    amount: DecimalStr
    category: CategoryCode
    evidence_ids: Annotated[list[BlockId], Field(min_length=1)]


class Exclusion(Contract):
    """A supplied document deliberately not counted, with the reason."""

    version_id: VersionId
    label: NonEmptyStr
    reason: ExclusionReason
    explanation: NonEmptyStr
    amount: DecimalStr | None = None
    duplicate_of: VersionId | None = None
    evidence_ids: list[BlockId] = Field(default_factory=list)


class UnresolvedDocument(Contract):
    """A document whose relevance/amount could not be accepted; it blocks every plausible category."""

    version_id: VersionId
    label: NonEmptyStr
    reason: NonEmptyStr
    possible_categories: list[CategoryCode] = Field(default_factory=list)
    evidence_ids: list[BlockId] = Field(default_factory=list)


class IndicatorResult(Contract):
    """Result for one report indicator.

    ``observed_sum`` may be partial. ``expected_value`` is the complete expectation and is null unless
    coverage is complete; ``diff`` = reported - expected and is null unless both are known.
    """

    entry_id: EntryId
    column: Annotated[int, Field(ge=1)] | None = None
    label: NonEmptyStr
    unit: str | None = None
    category: CategoryCode | None = None
    reported_value: DecimalStr | None = None
    reported_evidence_ids: list[BlockId] = Field(default_factory=list)
    observed_sum: DecimalStr | None = None
    expected_value: DecimalStr | None = None
    diff: DecimalStr | None = None
    coverage: Coverage
    status: IndicatorStatus
    problem: ProblemCategory | None = None
    contributions: list[Contribution] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    gaps: list[Finding] = Field(default_factory=list)
    explanation: NonEmptyStr

    @model_validator(mode="after")
    def _policy(self):
        if self.coverage != "complete" and self.expected_value is not None:
            raise ValueError("expected_value must be null unless coverage is complete")
        if self.diff is not None:
            if self.expected_value is None or self.reported_value is None:
                raise ValueError("diff needs both reported_value and expected_value")
            if self.diff != self.reported_value - self.expected_value:
                raise ValueError("diff must equal reported_value - expected_value")
        if self.status == "insufficient_evidence":
            if self.diff is not None:
                raise ValueError("insufficient_evidence cannot carry a diff")
            if self.problem not in INSUFFICIENT_PROBLEMS:
                raise ValueError(f"insufficient_evidence needs one of {INSUFFICIENT_PROBLEMS}")
        else:
            if self.diff is None:
                raise ValueError(f"status '{self.status}' needs a diff")
            if self.status == "verified" and self.problem is not None:
                raise ValueError("a verified indicator has no problem category")
            if self.status == "discrepancy" and self.problem not in DISCREPANCY_PROBLEMS:
                raise ValueError(f"discrepancy needs one of {DISCREPANCY_PROBLEMS}")
        return self


class ComponentDiscrepancy(Contract):
    entry_id: EntryId
    diff: DecimalStr
    problem: ProblemCategory
    explanation: NonEmptyStr
    evidence_ids: list[BlockId] = Field(default_factory=list)


class GrandTotal(Contract):
    """Grand-total support. ``explained_by`` lists component discrepancies already counted, so the same
    error is not reported twice."""

    entry_id: EntryId | None = None
    status: IndicatorStatus
    diff: DecimalStr | None = None
    explained_by: list[EntryId] = Field(default_factory=list)
    unexplained_diff: DecimalStr | None = None


class Conclusion(Contract):
    component_discrepancies: list[ComponentDiscrepancy] = Field(default_factory=list)
    incomplete_indicators: list[EntryId] = Field(default_factory=list)
    grand_total: GrandTotal
    summary: NonEmptyStr


class ReconciliationResult(Contract):
    """Return value of reconcile(scope, report, primaries, manifest)."""

    scope: Scope
    tolerance: DecimalStr
    indicators: list[IndicatorResult]
    exclusions: list[Exclusion] = Field(default_factory=list)
    unresolved: list[UnresolvedDocument] = Field(default_factory=list)
    conclusion: Conclusion

    @model_validator(mode="after")
    def _consistent(self):
        by_id = {i.entry_id: i for i in self.indicators}
        if len(by_id) != len(self.indicators):
            raise ValueError("duplicate indicator entry_id")
        for i in self.indicators:
            if i.status == "verified" and abs(i.diff) > self.tolerance:
                raise ValueError(f"{i.entry_id}: verified but |diff| exceeds tolerance")
            if i.status == "discrepancy" and abs(i.diff) <= self.tolerance:
                raise ValueError(f"{i.entry_id}: discrepancy within tolerance")
        c = self.conclusion
        component_ids = [d.entry_id for d in c.component_discrepancies]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("duplicate component discrepancy entry_id")
        for d in c.component_discrepancies:
            ind = by_id.get(d.entry_id)
            if ind is None or ind.status != "discrepancy" or ind.diff != d.diff:
                raise ValueError(f"component discrepancy {d.entry_id} does not match its indicator")
        for eid in c.incomplete_indicators:
            if eid not in by_id or by_id[eid].status != "insufficient_evidence":
                raise ValueError(f"incomplete indicator {eid} is not an insufficient_evidence indicator")
        g = c.grand_total
        if g.status == "insufficient_evidence" and (g.diff is not None or g.unexplained_diff is not None):
            raise ValueError("insufficient evidence for grand total requires null diff and unexplained_diff")
        if g.entry_id is not None:
            total_indicator = by_id.get(g.entry_id)
            if total_indicator is None:
                raise ValueError(f"grand total names unknown indicator {g.entry_id}")
            if g.status != total_indicator.status or g.diff != total_indicator.diff:
                raise ValueError("grand total status and diff must match its indicator")
        if len(g.explained_by) != len(set(g.explained_by)):
            raise ValueError("duplicate entry_id in grand_total.explained_by")
        listed = {d.entry_id: d.diff for d in c.component_discrepancies}
        if any(e not in listed for e in g.explained_by):
            raise ValueError("grand_total.explained_by must name listed component discrepancies")
        if g.unexplained_diff is not None:
            if g.diff is None:
                raise ValueError("unexplained_diff needs a grand-total diff")
            if g.unexplained_diff != g.diff - sum((listed[e] for e in g.explained_by), Decimal(0)):
                raise ValueError("unexplained_diff must equal diff minus explained component diffs")
        return self


# ======================= runs and chat =======================


class StageProgress(Contract):
    stage: RunStage
    done: Annotated[int, Field(ge=0)] = 0
    total: Annotated[int, Field(ge=0)] = 0
    failed: Annotated[int, Field(ge=0)] = 0


class RunFailure(Contract):
    code: RunFailureCode
    message: NonEmptyStr


class ReplayManifest(Contract):
    """What is needed to explain or replay a run. Never contains secrets."""

    input_sha256: dict[VersionId, Sha256]
    app_version: NonEmptyStr
    contract_version: NonEmptyStr = CONTRACT_VERSION
    tool_versions: dict[str, str] = Field(default_factory=dict)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class Run(Contract):
    workspace_id: WorkspaceId
    run_id: RunId
    predecessor_run_id: RunId | None = None
    input_version_ids: Annotated[list[VersionId], Field(min_length=1)]
    scope: Scope
    status: RunStatus
    stage: RunStage | None = None
    progress: list[StageProgress] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    report: ReportExtraction | None = None
    primaries: list[PrimaryExtraction] = Field(default_factory=list)
    result: ReconciliationResult | None = None
    issues: list[Finding] = Field(default_factory=list)
    failure: RunFailure | None = None
    replay: ReplayManifest | None = None

    @model_validator(mode="after")
    def _states(self):
        if not self.scope.confirmed:
            raise ValueError("a run needs a confirmed scope")
        if self.status == "completed":
            if self.result is None or self.failure is not None:
                raise ValueError("a completed run has a result and no failure")
        elif self.result is not None:
            raise ValueError(f"a {self.status} run cannot have a result")
        if self.status == "failed" and self.failure is None:
            raise ValueError("a failed run needs a failure")
        if self.status in ("queued", "running", "cancelled", "interrupted") and self.failure is not None:
            raise ValueError(f"a {self.status} run has no failure; use status 'failed'")
        return self


class StartRunRequest(Contract):
    version_ids: Annotated[list[VersionId], Field(min_length=1)]
    scope: Scope
    predecessor_run_id: RunId | None = None


class IndicatorCard(Contract):
    kind: Literal["indicator"] = "indicator"
    run_id: RunId
    indicator: IndicatorResult


class ConclusionCard(Contract):
    kind: Literal["conclusion"] = "conclusion"
    run_id: RunId
    conclusion: Conclusion


class RunStatusCard(Contract):
    kind: Literal["run_status"] = "run_status"
    run_id: RunId
    status: RunStatus
    stage: RunStage | None = None
    failure: RunFailure | None = None


ResultCard = Annotated[IndicatorCard | ConclusionCard | RunStatusCard, Field(discriminator="kind")]


class ChatMessage(Contract):
    """A conversation turn. Numbers shown to the user come from typed cards, never from parsing ``text``."""

    workspace_id: WorkspaceId
    message_id: MessageId
    run_id: RunId | None = None
    role: Literal["user", "assistant", "system"]
    text: str
    cards: list[ResultCard] = Field(default_factory=list)
    evidence_ids: list[BlockId] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _cards(self):
        if self.role == "user" and self.cards:
            raise ValueError("user messages carry no result cards")
        if any(c.run_id != self.run_id for c in self.cards):
            raise ValueError("every card must belong to the message's run")
        return self


class PostMessageRequest(Contract):
    text: NonEmptyStr
    run_id: RunId | None = None


class EvidenceView(Contract):
    """GET /api/workspaces/{wid}/evidence/{eid}."""

    block: EvidenceBlock
    version: DocumentVersion
    artifact_url: str | None = None
    original_url: NonEmptyStr


# ======================= published schema =======================

PUBLISHED_MODELS: tuple[type[BaseModel], ...] = (
    Scope,
    Workspace,
    DocumentVersion,
    ArtifactRef,
    EvidenceBlock,
    PreparedDocument,
    ToolError,
    ToolResult[Any],
    ProposedField,
    ReportProposal,
    PrimaryProposal,
    ReportExtraction,
    PrimaryExtraction,
    ValidationOutcome,
    BundleManifest,
    ReconciliationResult,
    Run,
    StartRunRequest,
    ChatMessage,
    PostMessageRequest,
    EvidenceView,
)


def json_schema() -> dict[str, Any]:
    """JSON Schema for every published model, in one document."""
    _, schema = models_json_schema(
        [(m, "validation") for m in PUBLISHED_MODELS], title=f"Sverka contracts v{CONTRACT_VERSION}"
    )
    schema["$comment"] = "Generated by python -m sverka.contracts; do not edit by hand."
    return schema


if __name__ == "__main__":
    print(json.dumps(json_schema(), ensure_ascii=False, indent=2, sort_keys=True))
