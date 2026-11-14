"""A mechanical reader of the supplied case19 documents: a test double for the model.

Test-only. It writes the proposal a careful reader would submit for a prepared document: every value comes
from ``tests/fixtures/case19/manifest.json`` (what the file prints) and cites the real evidence block that shows
it. It decides nothing: acceptance is the validator's, and a value the evidence does not show is still proposed,
so the validator has to reject it. Never used by the application.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from case19_scenarios import load_manifest
from recon_fixtures import INDICATORS

from sverka import contracts as c
from sverka.harness_fakes import respond, tool_call
from sverka.money import MONTH_NAMES, fmt

ROW_LABEL = "3 этап, 2026 г."
WORKFORCE_UNIT = "человеко-месяц"  # as the report's column heading prints it
MISSING = "не указано в документе"


def expected_primary(filename: str) -> dict[str, Any] | None:
    """What ``filename`` prints, from the case19 manifest; None for a file that is not part of the bundle."""
    return next((p["expected"] for p in load_manifest()["primaries"] if p["file"] == filename), None)


UNREAD = {
    "doc_type": None,
    "number": None,
    "contracts": None,
    "counterparty": None,
    "issue_date": None,
    "service_period": None,
    "amount": None,
    "allocations": [],
}


def _text_block(prepared: c.PreparedDocument, needle: str) -> c.EvidenceBlock | None:
    needle = needle.casefold()
    return next((b for b in prepared.blocks if needle in b.text.casefold()), None)


def amount_block(prepared: c.PreparedDocument, amount: Decimal) -> c.EvidenceBlock | None:
    """The block that shows ``amount``: printed text, a literal cell, or a formula cell's cached/derived value."""
    for block in prepared.blocks:
        loc = block.locator
        if loc.kind == "cell":
            values = [loc.derived_value] if loc.formula is not None else []
            for raw in (loc.cached_value, None if loc.formula is not None else block.text):
                try:
                    values.append(Decimal(raw.replace(" ", "").replace(",", ".")) if raw else None)
                except InvalidOperation:
                    continue
            if amount in [v for v in values if v is not None]:
                return block
        elif fmt(amount) in block.text.replace(" ", " "):
            return block
    return None


def field(value: Any, block: c.EvidenceBlock | None, *, basis: str = "observed", source: str | None = None) -> dict:
    if value is None or block is None:
        return {"value": None, "unresolved_reason": MISSING}
    return {
        "value": value,
        "source_text": source if source is not None else block.text,
        "evidence_ids": [block.block_id],
        "basis": basis,
    }


def _decimal_field(prepared: c.PreparedDocument, raw: str | None) -> dict:
    if raw is None:
        return field(None, None)
    block = amount_block(prepared, Decimal(raw))
    derived = block is not None and block.locator.kind == "cell" and block.locator.formula is not None
    return field(raw, block, basis="inferred" if derived else "observed")


def primary_proposal(prepared: c.PreparedDocument, expected: dict[str, Any]) -> dict[str, Any]:
    """A ``PrimaryProposal`` payload for ``prepared`` stating ``expected`` (one manifest entry's ``expected``)."""
    title = prepared.blocks[0] if prepared.blocks else None
    doc_type = expected["doc_type"]
    type_block = _text_block(prepared, doc_type) if doc_type else None
    period = expected.get("service_period")
    period_block = None
    if period:
        start = date.fromisoformat(period["from"])
        period_block = _text_block(prepared, f"{MONTH_NAMES[start.month]} {start.year}")
    issue = expected.get("issue_date")
    issue_block = _text_block(prepared, date.fromisoformat(issue).strftime("%d.%m.%Y")) if issue else None
    contracts = expected.get("contracts") or []
    money = _text_block(prepared, "руб")
    lines = []
    for line in expected.get("allocations", []):
        amount = _decimal_field(prepared, line["amount"])
        evidence = list(amount.get("evidence_ids", []))
        code = (line.get("line_label") or "").split(" ")[0]
        label = _text_block(prepared, code) if code else None
        if label is not None and label.block_id not in evidence:
            evidence.append(label.block_id)
        category = {"value": line["category"], "evidence_ids": evidence, "basis": "inferred"}
        if not evidence or line["category"] is None:
            category = {"value": None, "unresolved_reason": "категория не установлена"}
        lines.append(
            {
                "line_label": line.get("line_label"),
                "category": category,
                "possible_categories": line.get("possible_categories", []),
                "amount": amount,
            }
        )
    return {
        "kind": "primary",
        "version_id": prepared.version.version_id,
        "doc_type": field(doc_type, type_block or title),
        "number": field(expected["number"], _text_block(prepared, expected["number"] or "\0")),
        "contracts": field(contracts or None, _text_block(prepared, contracts[0]) if contracts else None),
        "counterparty": field(
            expected.get("counterparty"), _text_block(prepared, expected.get("counterparty") or "\0")
        ),
        "issue_date": field(issue, issue_block),
        "service_period": field(f"{period['from']}/{period['to']}" if period else None, period_block),
        "amount": _decimal_field(prepared, expected.get("amount")),
        "currency": field("RUB", money),
        "unit": field("руб.", money),
        "allocations": lines,
        "possible_categories": expected.get("possible_categories", []),
    }


def report_proposal(
    prepared: c.PreparedDocument, blocks: list[c.EvidenceBlock] | None = None, *, overrides: dict | None = None
) -> dict[str, Any]:
    """A ``ReportProposal`` payload with the 15 indicators of the supplied report.

    Each value cites the first of ``blocks`` (default: the document's blocks) that prints it; a value no block
    prints is still proposed, citing the row's first block. ``overrides`` maps entry_id to another value.
    """
    blocks = list(prepared.blocks if blocks is None else blocks)
    overrides = overrides or {}
    indicators = []
    for entry_id, column, label, _, category, components, value in INDICATORS:
        value = overrides.get(entry_id, value)
        entry: dict[str, Any] = {
            "entry_id": entry_id,
            "label": label,
            "column": column,
            "unit": WORKFORCE_UNIT if category == "workforce_intensity" else None,  # money columns print none
            "category": category,
            "components": components,
        }
        if value is None:
            entry["value"] = {"value": None, "unresolved_reason": "ячейка не прочитана"}
        else:
            printed = fmt(Decimal(value))
            shown = next((b for b in blocks if _prints(b, Decimal(value), printed)), blocks[0] if blocks else None)
            entry["value"] = field(value, shown, source=printed)
        indicators.append(entry)
    return {
        "kind": "report",
        "version_id": prepared.version.version_id,
        "row_label": ROW_LABEL,
        "headers": [],
        "indicators": indicators,
    }


def _prints(block: Any, value: Decimal, printed: str) -> bool:
    """Whether ``block`` shows ``value``: a cell holding that number, or text with the digits of ``printed``
    in order (OCR varies spaces and the decimal mark)."""
    if getattr(getattr(block, "locator", None), "kind", None) == "cell":
        try:
            return Decimal(block.text) == value
        except InvalidOperation:
            return False
    digits = "".join(ch for ch in printed if ch.isdigit())
    return digits in "".join(ch for ch in block.text if ch.isdigit())


class ReaderModel:
    """Handler for ``harness_fakes.ScriptedProvider``: the mechanical reader as a model.

    For the report it re-OCRs the requested row when the page is a scan, then submits the 15 indicators citing
    the blocks that came back. For a primary it submits what the manifest says the file prints; a file the
    manifest does not know is submitted with every field unread. ``prepared(version_id)`` returns the prepared
    document (the run's own ``DocumentTools``), ``expected`` overrides what a file is said to print.
    """

    def __init__(self, prepared, *, expected=expected_primary, report_overrides: dict | None = None) -> None:
        self.prepared = prepared
        self.expected = expected
        self.report_overrides = report_overrides
        self.documents: list[str] = []

    def __call__(self, messages, tools):
        request = next(m.text for m in messages if m.role == "user")
        version_id, role = re.search(r"version_id=(ver_\w+), role=(\w+)", request).groups()
        turns = sum(1 for m in messages if m.role == "assistant")
        if turns == 0:
            self.documents.append(version_id)
        prepared = self.prepared(version_id)
        if role != "report":
            expected = self.expected(prepared.version.original_filename) or UNREAD
            return respond(tool_call("validate_extraction", {"proposal": primary_proposal(prepared, expected)}))
        scanned = any(b.method == "ocr" for b in prepared.blocks)
        if scanned and turns == 0:
            row = next(b for b in prepared.blocks if "2026 г.:" in b.text)
            box, width = row.locator.box, row.locator.image_width
            band = {"x0": max(0, box.x0 - 10), "y0": box.y0 - 2, "x1": min(width, box.x1 + 10), "y1": box.y1 + 3}
            return respond(tool_call("reocr_region", {"render_id": row.locator.render_id, "box": band}))
        blocks = None
        if scanned:
            result = json.loads(next(m.text for m in reversed(messages) if m.role == "tool"))
            blocks = [SimpleNamespace(block_id=b["block_id"], text=b["text"]) for b in result.get("data", [])]
        proposal = report_proposal(prepared, blocks, overrides=self.report_overrides)
        return respond(tool_call("validate_extraction", {"proposal": proposal}))
