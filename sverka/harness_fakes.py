"""Deterministic test doubles for the harness (T08). Not used by the production runtime.

* ``ScriptedProvider``: a ``ModelProvider`` that replays prepared responses or calls a handler.
* ``FakeEvidenceTools``: in-memory ``EvidenceTools`` over ``fakes.InMemoryStore``.
* ``simple_validator``: a stand-in for Eva's ``validate_extraction`` (T06). It accepts any parseable value
  whose evidence exists in the prepared document and does not compare the value with the evidence text.
  Never treat its output as real validation.
* ``seed_example``: stores a document built from the published contract examples
  (tests/fixtures/contracts/evidence_blocks.json) with fresh, store-issued IDs.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import contracts as c
from .fakes import InMemoryStore, load_example
from .model_adapter import ModelMessage, ModelResponse, ProviderError, ToolCall, ToolSpec

# 1x1 transparent PNG: enough for the harness, which moves bytes and never decodes them.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# ======================= provider =======================

Step = ModelResponse | Exception | Callable[[Sequence[ModelMessage], Sequence[ToolSpec]], Any]


def tool_call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> ToolCall:
    """A tool request. A ``dict`` is encoded as JSON; a ``str`` is passed through, valid or not."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return ToolCall(id=call_id or c.new_id("msg").replace("msg_", "call_"), name=name, arguments=raw)


def respond(*calls: ToolCall, text: str = "", finish_reason: str | None = None) -> ModelResponse:
    """An assistant turn with the given tool calls and/or text."""
    reason = finish_reason or ("tool_calls" if calls else "stop")
    return ModelResponse(
        ModelMessage("assistant", text, tool_calls=tuple(calls)), finish_reason=reason, model="scripted-1"
    )


@dataclass
class ProviderCall:
    messages: list[ModelMessage]
    tools: list[ToolSpec]
    timeout: float


class ScriptedProvider:
    """Replays ``script`` in order, or asks ``handler`` for every turn.

    A step is a ``ModelResponse``, an exception to raise, or a callable ``(messages, tools)`` returning either.
    Every call is recorded in ``calls`` with the exact messages the harness sent.
    """

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: Sequence[Step] = (), *, handler: Callable[..., Any] | None = None, secret: str = ""):
        self._script = list(script)
        self._handler = handler
        self._secret = secret
        self.calls: list[ProviderCall] = []

    def describe(self) -> dict[str, str | int | float | bool | None]:
        return {"provider": self.name, "model": self.model, "api_key_set": bool(self._secret)}

    def secret_values(self) -> tuple[str, ...]:
        return (self._secret,) if self._secret else ()

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[ToolSpec], *, timeout: float) -> ModelResponse:
        self.calls.append(ProviderCall(list(messages), list(tools), timeout))
        if self._handler is not None:
            step: Any = self._handler(messages, tools)
        elif self._script:
            step = self._script.pop(0)
        else:
            raise ProviderError("provider_error", "ScriptedProvider has no more steps")
        if callable(step):
            step = step(messages, tools)
        if isinstance(step, Exception):
            raise step
        return step


class FakeClock:
    """Manual monotonic clock: ``clock()`` reads it, ``clock.advance(s)`` moves it. Usable as ``sleep`` too."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ======================= validator stand-in =======================


def _period(value: Any) -> c.Period:
    start, end = str(value).split("/")
    return c.Period(start=start, end=end)


def _observe(
    cls: Any, path: str, proposed: c.ProposedField, known: set[str], parse: Callable[[Any], Any], asks: list
) -> Any:
    def unresolved(reason: str, *, ask: bool) -> Any:
        if ask:
            asks.append(
                c.ReinspectionRequest(
                    field=path, reason=reason, block_ids=[e for e in proposed.evidence_ids if e in known]
                )
            )
        return cls(
            state="unresolved",
            source_text=proposed.source_text,
            evidence_ids=[e for e in proposed.evidence_ids if e in known],
            reason=reason,
        )

    if proposed.value is None:
        return unresolved(proposed.unresolved_reason or "no value proposed", ask=False)
    if not proposed.evidence_ids or any(e not in known for e in proposed.evidence_ids):
        return unresolved("the value has no evidence in this document", ask=True)
    try:
        value = parse(proposed.value)
        return cls(
            state="accepted",
            value=value,
            source_text=proposed.source_text,
            evidence_ids=list(proposed.evidence_ids),
            basis=proposed.basis,
        )
    except (ValueError, TypeError):
        return unresolved("the value is not in canonical form", ask=True)


def simple_validator(
    proposal: c.ReportProposal | c.PrimaryProposal, prepared: c.PreparedDocument
) -> c.ValidationOutcome:
    """Fake ``validate_extraction``: evidence must exist and the value must parse. Nothing more is checked."""
    known = {b.block_id for b in prepared.blocks}
    asks: list[c.ReinspectionRequest] = []
    if isinstance(proposal, c.ReportProposal):
        extraction: Any = c.ReportExtraction(
            version_id=proposal.version_id,
            row_label=proposal.row_label,
            headers=[
                c.HeaderEntry(
                    entry_id=h.entry_id,
                    label=h.label,
                    label_evidence_ids=h.label_evidence_ids,
                    observation=_observe(c.TextObservation, f"headers.{i}.value", h.value, known, str, asks),
                )
                for i, h in enumerate(proposal.headers)
            ],
            indicators=[
                c.IndicatorEntry(
                    entry_id=ind.entry_id,
                    label=ind.label,
                    column=ind.column,
                    unit=ind.unit,
                    label_evidence_ids=ind.label_evidence_ids,
                    category=ind.category,
                    components=ind.components,
                    observation=_observe(c.DecimalObservation, f"indicators.{i}.value", ind.value, known, str, asks),
                )
                for i, ind in enumerate(proposal.indicators)
            ],
        )
    else:

        def text(name: str) -> Any:
            return _observe(c.TextObservation, name, getattr(proposal, name), known, str, asks)

        extraction = c.PrimaryExtraction(
            version_id=proposal.version_id,
            doc_type=text("doc_type"),
            number=text("number"),
            contracts=_observe(c.TextListObservation, "contracts", proposal.contracts, known, list, asks),
            counterparty=text("counterparty"),
            issue_date=_observe(c.DateObservation, "issue_date", proposal.issue_date, known, str, asks),
            service_period=_observe(
                c.PeriodObservation, "service_period", proposal.service_period, known, _period, asks
            ),
            amount=_observe(c.DecimalObservation, "amount", proposal.amount, known, str, asks),
            currency=text("currency"),
            unit=text("unit"),
            allocations=[
                c.AllocationLine(
                    line_label=line.line_label,
                    category=_observe(
                        c.CategoryObservation, f"allocations.{i}.category", line.category, known, str, asks
                    ),
                    possible_categories=line.possible_categories,
                    amount=_observe(c.DecimalObservation, f"allocations.{i}.amount", line.amount, known, str, asks),
                )
                for i, line in enumerate(proposal.allocations)
            ],
            possible_categories=proposal.possible_categories,
        )
    return c.ValidationOutcome(extraction=extraction, reinspection=asks)


# ======================= evidence tools =======================

Validator = Callable[[Any, c.PreparedDocument], Any]


class FakeEvidenceTools:
    """In-memory ``interfaces.EvidenceTools``. ``calls`` records every invocation as ``(tool name, arguments)``.

    ``failures[name]`` forces a tool to fail: a ``ToolError`` is returned as a failed ``ToolResult``, an
    exception is raised. ``validator`` may return a ``ValidationOutcome`` or a ``ToolResult``.
    """

    tool_versions = {"evidence_tools": "fake-1"}

    def __init__(self, store: InMemoryStore, validator: Validator = simple_validator) -> None:
        self.store = store
        self.validator = validator
        self.prepared: dict[str, c.PreparedDocument] = {}
        self.failures: dict[str, c.ToolError | Exception] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.reocr_text = "re-read text"

    def add(self, prepared: c.PreparedDocument) -> None:
        self.prepared[prepared.version.version_id] = prepared

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _enter(self, name: str, *args: Any) -> c.ToolResult | None:
        self.calls.append((name, args))
        failure = self.failures.get(name)
        if isinstance(failure, Exception):
            raise failure
        return c.error_result(failure) if failure is not None else None

    def _document(self, workspace_id: str, version_id: str) -> c.PreparedDocument:
        self.store.get_version(workspace_id, version_id)  # workspace scoping: foreign IDs are not found
        try:
            return self.prepared[version_id]
        except KeyError:
            raise c.ToolFailure("unreadable", f"version {version_id} could not be prepared") from None

    def prepare_document(self, workspace_id: str, version_id: str) -> c.ToolResult:
        try:
            return self._enter("prepare_document", version_id) or c.ok_result(self._document(workspace_id, version_id))
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def read_evidence(self, workspace_id: str, block_ids: Sequence[str]) -> c.ToolResult:
        try:
            forced = self._enter("read_evidence", tuple(block_ids))
            return forced or c.ok_result([self.store.get_block(workspace_id, b) for b in block_ids])
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def inspect_page(self, workspace_id: str, version_id: str, page: int) -> c.ToolResult:
        try:
            forced = self._enter("inspect_page", version_id, page)
            if forced:
                return forced
            for image in self._document(workspace_id, version_id).pages:
                if image.page == page:
                    return c.ok_result(image)
            raise c.ToolFailure("not_found", f"page {page} not found")
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def inspect_crop(self, workspace_id: str, render_id: str, box: c.Box) -> c.ToolResult:
        try:
            forced = self._enter("inspect_crop", render_id, box)
            if forced:
                return forced
            source, _ = self.store.read_artifact(workspace_id, render_id)
            crop = self.store.save_artifact(
                workspace_id, source.version_id, kind="crop", media_type="image/png", data=PNG_BYTES
            )
            return c.ok_result(crop)
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def reocr_region(self, workspace_id: str, render_id: str, box: c.Box) -> c.ToolResult:
        try:
            forced = self._enter("reocr_region", render_id, box)
            if forced:
                return forced
            source, _ = self.store.read_artifact(workspace_id, render_id)
            page = next(p for p in self.prepared[source.version_id].pages if p.render_id == render_id)
            block = c.EvidenceBlock(
                version_id=source.version_id,
                block_id=c.new_id("blk"),
                text=self.reocr_text,
                method="reocr",
                locator=c.PageRegionLocator(
                    page=page.page, render_id=render_id, box=box, image_width=page.width, image_height=page.height
                ),
            )
            self.store.save_blocks(workspace_id, [block])
            return c.ok_result([block])
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def read_cells(self, workspace_id: str, version_id: str, sheet: str, ref: str) -> c.ToolResult:
        try:
            forced = self._enter("read_cells", version_id, sheet, ref)
            if forced:
                return forced
            cells = [
                b
                for b in self._document(workspace_id, version_id).blocks
                if b.locator.kind == "cell" and b.locator.sheet == sheet and (":" in ref or b.locator.ref == ref)
            ]
            if not cells:
                raise c.ToolFailure("not_found", f"no cells at {sheet}!{ref}")
            return c.ok_result(cells)
        except c.ToolFailure as exc:
            return c.error_result(exc)

    def validate_extraction(self, workspace_id: str, proposal: Any, prepared: c.PreparedDocument) -> c.ToolResult:
        forced = self._enter("validate_extraction", proposal, prepared)
        if forced:
            return forced
        outcome = self.validator(proposal, prepared)
        return outcome if isinstance(outcome, c.ToolResult) else c.ok_result(outcome)


# ======================= fixtures =======================


@dataclass
class SeededDocument:
    version: c.DocumentVersion
    prepared: c.PreparedDocument
    ids: dict[str, str] = field(default_factory=dict)  # example block ID -> stored block ID
    render_id: str | None = None

    @property
    def version_id(self) -> str:
        return self.version.version_id


def seed_example(
    store: InMemoryStore,
    tools: FakeEvidenceTools | None,
    workspace_id: str,
    source: str,
    *,
    role: str,
    filename: str,
    data: bytes | None = None,
    render: bytes = PNG_BYTES,
) -> SeededDocument:
    """Store a document whose evidence is the published example blocks of ``source`` (e.g. ``ver_sa174_v1``).

    The store issues the version, artifact and block IDs, so several documents can be seeded from one example.
    """
    version = store.save_document(
        workspace_id, filename=filename, role=role, data=data or f"{filename}:{c.new_id('doc')}".encode()
    )
    examples = [b for b in load_example("evidence_blocks") if b.version_id == source]
    if not examples:
        raise KeyError(f"no example blocks for {source}")
    pages: list[c.PageImage] = []
    render_id: str | None = None
    blocks: list[c.EvidenceBlock] = []
    ids: dict[str, str] = {}
    for example in examples:
        locator = example.locator
        if locator.kind == "page_region":
            if render_id is None:
                ref = store.save_artifact(
                    workspace_id, version.version_id, kind="page_render", media_type="image/png", data=render
                )
                render_id = ref.artifact_id
                pages.append(
                    c.PageImage(
                        page=locator.page,
                        render_id=render_id,
                        width=locator.image_width,
                        height=locator.image_height,
                        transform=locator.transform,
                    )
                )
            locator = locator.model_copy(update={"render_id": render_id})
        ids[example.block_id] = c.new_id("blk")
        blocks.append(
            example.model_copy(
                update={"version_id": version.version_id, "block_id": ids[example.block_id], "locator": locator}
            )
        )
    store.save_blocks(workspace_id, blocks)
    prepared = c.PreparedDocument(version=version, blocks=blocks, pages=pages)
    if tools is not None:
        tools.add(prepared)
    return SeededDocument(version=version, prepared=prepared, ids=ids, render_id=render_id)


def _field(doc: SeededDocument, value: Any, source_text: str | None, *evidence: str, basis: str = "observed") -> dict:
    return {
        "value": value,
        "source_text": source_text,
        "evidence_ids": [doc.ids[e] for e in evidence],
        "basis": basis,
    }


def example_primary_proposal(doc: SeededDocument) -> dict[str, Any]:
    """A complete ``PrimaryProposal`` payload for a document seeded from ``ver_sa174_v1`` (Счет-акт № 174)."""
    amount = _field(doc, "318298.10", "318 298,10", "blk_sa174_amount")
    return {
        "kind": "primary",
        "version_id": doc.version_id,
        "doc_type": _field(doc, "Счет-акт", "СЧЕТ-АКТ", "blk_sa174_number"),
        "number": _field(doc, "174", "№ 174", "blk_sa174_number"),
        "contracts": _field(doc, ["24/03/15-01"], "Договор 24/03/15-01", "blk_sa174_contract"),
        "counterparty": _field(doc, "ООО «ИнжКонтур Бета»", "ООО «ИнжКонтур Бета»", "blk_sa174_vendor"),
        "issue_date": _field(doc, "2026-02-25", "25.02.2026", "blk_sa174_date"),
        "service_period": _field(doc, "2026-02-25/2026-02-25", None, "blk_sa174_date", basis="inferred"),
        "amount": amount,
        "currency": _field(doc, "RUB", "руб.", "blk_sa174_amount"),
        "unit": _field(doc, "руб.", "руб.", "blk_sa174_amount"),
        "allocations": [
            {
                "line_label": None,
                "category": _field(
                    doc, "production_overhead", None, "blk_sa174_desc", "blk_sa174_purpose", basis="inferred"
                ),
                "possible_categories": [],
                "amount": amount,
            }
        ],
        "possible_categories": [],
    }


def example_report_proposal(doc: SeededDocument) -> dict[str, Any]:
    """A ``ReportProposal`` payload for a document seeded from ``ver_kfz_v1``: column 15 only."""
    return {
        "kind": "report",
        "version_id": doc.version_id,
        "row_label": "3 этап, 2026 г.",
        "headers": [],
        "indicators": [
            {
                "entry_id": "col15",
                "label": "Общепроизводственные затраты (Статья калькуляции 0800)",
                "column": 15,
                "unit": "руб.",
                "label_evidence_ids": [doc.ids["blk_kfz_h15"]],
                "category": "production_overhead",
                "components": [],
                "value": _field(doc, "1145798.10", "1 145 798,10", "blk_kfz_c15"),
            }
        ],
    }


EXPECTED_AMOUNT = Decimal("318298.10")  # what example_primary_proposal states, for assertions
