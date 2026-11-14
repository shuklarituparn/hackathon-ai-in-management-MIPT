"""Reconcile validated facts (T10): ``reconcile(scope, report, primaries, manifest) -> ReconciliationResult``.

A pure function of validated extractions: no parsing, OCR, model calls or storage. Policies are numbered as in
docs/architecture/README.md#reconciliation-policies:

1. Source report observations are never mutated; monetary result values are normalized to RUB with a note
   retaining the original number and unit. Arithmetic inconsistencies in the report become findings.
2. An explicit service period wins; otherwise the issue-date rule (period = issue date) applies and is recorded.
   A period that only partly overlaps the scope stays unresolved; nothing is prorated.
3. Identical bytes count once. Number + date + amount is only a duplicate candidate: contract, counterparty and
   document type must agree too. Same identity with different amount or date is a conflicting version and stays
   unresolved, unless both documents state service periods that do not overlap (monthly statements that reuse
   a number are different documents).
4. Only accepted amount + accepted category + eligibility (contract, period, currency) is counted.
5. Payroll and contributions need every scope month covered by its own statement, even if only one survived;
   a statement's subtotal line is not counted next to its detail lines. Two documents for the same month under
   one monthly root (for example a separate bonus statement) are not both counted: that root stays unverified.

If the report row that was read is not the row the user confirmed, no reported value is compared: every
indicator is insufficient evidence with a ``report_row_mismatch`` gap, while document sums are still shown.
6. Coverage is about the supplied bundle. A category is complete only when no unreadable input, unresolved
   interpretation, conflicting version or unsplit line could affect it.
7. Zero is verified only in a closed scope: no accepted or unresolved document could belong to the category.
   A document of unknown relevance blocks every category.
8. ``observed_sum`` may be partial. Without complete coverage ``expected_value`` and ``diff`` are null,
   including the grand total; independent component discrepancies are kept in the conclusion.
9. Amounts are normalized to RUB (units such as "тыс. руб." are scaled, other currencies stay unresolved),
   signs are kept, rounding is half-up to 0.01 and equality uses the configured tolerance. A monetary report
   column that prints no unit is compared in rubles, and a ``report_unit_assumed`` gap says so.

A contract reference matches the scope when it names the contract number as a whole ("№ 24/03/15-01 от
09.04.2024"); a number that only appears inside a longer one stays unresolved instead of being excluded.

The category hierarchy comes from the report itself: an indicator's ``components`` make their categories
children of its category. A line accepted at a parent category with ``possible_categories`` naming finer ones
(e.g. "additional payroll and leave reserve") counts for the parent but leaves those finer indicators unverified.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from . import config
from . import contracts as c
from .money import MONTH_NAMES, fmt, fmt_date, month_range, months_between, q

MONTHLY_ROOTS = ("payroll_total", "contributions_total")
UNRELATED = "unrelated"
ZERO = Decimal("0.00")
_SUBSET_LIMIT = 16
_DASHES = re.compile(r"[‐-―−]")
_TOTAL_LABEL = re.compile(r"\b(итог\w*|всего|total)\b", re.I)
# Unit and currency words. A unit is read word by word, so "в рублях", "руб. РФ" and "тысяч рублей" are
# recognized; any other word (USD, kg, чел.-мес.) makes the unit unsupported.
_RUB_WORDS = {"rub", "rur", "₽", "р", "руб", "рубль", "рубля", "рубли", "рублей", "рублях", "ruble", "rubles"}
_RUB_WORDS |= {"rouble", "roubles"}
_KOPECK_WORDS = {"коп", "копейка", "копейки", "копеек", "копейках", "kopeck", "kopecks"}
_THOUSAND_WORDS = {"тыс", "тысяча", "тысячи", "тысяч", "тысячах", "thousand", "thousands"}
_MILLION_WORDS = {"млн", "миллион", "миллиона", "миллионов", "миллионах", "million", "millions"}
_NEUTRAL_WORDS = {"в", "и", "рф", "российский", "российских", "российского", "российская", "федерации"}
_VAT_SUFFIX = re.compile(r"(?:\s*[,;]?\s*\(\s*(?:с|без)\s+ндс\s*\)|(?:\s*[,;]\s*|\s+)(?:с|без)\s+ндс)\s*$")
WORKFORCE = "workforce_intensity"  # the one nonmonetary indicator of the supplied form

# Order in which an insufficient-evidence indicator reports its main problem.
_PROBLEM_ORDER = (
    "unreadable_report_value",
    "conflicting_versions",
    "unreadable_input",
    "unresolved_interpretation",
    "missing_coverage",
    "no_supporting_document",
)
_PROBLEM_TEXT = {
    "unreadable_report_value": "значение в отчете не прочитано",
    "conflicting_versions": "в наборе есть конфликтующие версии документов",
    "unreadable_input": "часть документов не прочитана",
    "unresolved_interpretation": "не установлены категория, период, договор или сумма части документов",
    "missing_coverage": "документы покрывают не весь период",
    "no_supporting_document": "нет отдельного подтверждающего документа",
}


# ======================= helpers =======================


def _norm(text: str | None) -> str:
    text = _DASHES.sub("-", (text or "").lower().replace("№", ""))
    return re.sub(r"\s+", "", text)


def _accepted(obs) -> bool:
    return obs is not None and obs.state == "accepted"


def _dedupe(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _full_months(period: c.Period) -> frozenset[tuple[int, int]]:
    out = set()
    for y, m in months_between(period.start, period.end):
        start, end = month_range(y, m)
        if period.start <= start and period.end >= end:
            out.add((y, m))
    return frozenset(out)


def _month_text(months: Iterable[tuple[int, int]]) -> str:
    return ", ".join(f"{MONTH_NAMES[m]} {y}" for y, m in sorted(months))


def _signed(d: Decimal) -> str:
    return ("+" if d > 0 else "-" if d < 0 else "") + fmt(abs(d))


# ======================= category hierarchy =======================


class _Tree:
    """Category hierarchy derived from the report's indicator components."""

    def __init__(self, report: c.ReportExtraction) -> None:
        self.entries = {e.entry_id: e for e in report.indicators}
        self.children: dict[str, set[str]] = defaultdict(set)
        self.entry_children: dict[str, list[str]] = {e.entry_id: list(e.components) for e in report.indicators}
        for e in report.indicators:
            for comp in e.components:
                child = self.entries[comp].category
                if e.category and child and child != e.category:
                    self.children[e.category].add(child)
        self.categories = {e.category for e in report.indicators if e.category}
        has_parent = {ch for kids in self.children.values() for ch in kids}
        self.roots = self.categories - has_parent
        self._sub: dict[str, frozenset[str]] = {}

    def known(self, category: str | None) -> bool:
        return category in self.categories

    def subtree(self, category: str) -> frozenset[str]:
        if category not in self._sub:
            seen, stack = set(), [category]
            while stack:
                cur = stack.pop()
                if cur not in seen:
                    seen.add(cur)
                    stack.extend(self.children.get(cur, ()))
            self._sub[category] = frozenset(seen)
        return self._sub[category]

    def could_affect(self, possible: frozenset[str], x: str) -> bool:
        """Whether an amount whose category is one of ``possible`` (empty = unknown) could belong to ``x``."""
        if not possible:
            return True
        sub = self.subtree(x)
        for p in possible:
            if p == UNRELATED:
                continue
            if not self.known(p):
                if x in self.roots:  # a category this report has no column for still breaks the totals
                    return True
                continue
            if p in sub or x in self.subtree(p):
                return True
        return False

    def entry_descendants(self, entry_id: str) -> set[str]:
        seen, stack = set(), list(self.entry_children.get(entry_id, ()))
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.entry_children.get(cur, ()))
        return seen


# ======================= documents =======================


@dataclass
class _Line:
    version_id: str
    label: str
    category: str | None  # accepted category; None when unresolved
    possible: frozenset[str]  # finer or plausible categories; empty means unknown
    amount: Decimal | None  # RUB after normalization; None when unresolved
    evidence_ids: list[str]
    months: frozenset[tuple[int, int]]
    accepted: bool
    reason: str = ""
    marked_total: bool = False  # the document labels this line as a total (ИТОГО, Всего)


@dataclass
class _Doc:
    version: c.DocumentVersion
    extraction: c.PrimaryExtraction | None
    label: str
    status: str = "counted"  # counted | excluded | unresolved
    lines: list[_Line] = field(default_factory=list)
    exclusion: c.Exclusion | None = None
    problem: str | None = None  # for unresolved documents
    reason: str = ""
    possible: frozenset[str] = frozenset()
    amount: Decimal | None = None
    period: c.Period | None = None
    findings: list[c.Finding] = field(default_factory=list)

    @property
    def vid(self) -> str:
        return self.version.version_id

    def categories(self) -> frozenset[str]:
        cats = set(self.possible)
        for line in self.lines:
            if not line.accepted and not line.possible:
                return frozenset()  # unknown is a wildcard, not an empty contribution to the union
            cats |= line.possible
            if line.category:
                cats.add(line.category)
        if self.extraction is not None:
            cats |= set(self.extraction.possible_categories)
            for a in self.extraction.allocations:
                if not _accepted(a.category) and not (a.possible_categories or self.extraction.possible_categories):
                    return frozenset()
                cats |= set(a.possible_categories)
                if _accepted(a.category):
                    cats.add(a.category.value)
        return frozenset(cats)


def _label(ext: c.PrimaryExtraction | None, version: c.DocumentVersion) -> str:
    if ext is None or not _accepted(ext.number):
        return version.original_filename
    parts = [ext.doc_type.value if _accepted(ext.doc_type) else "Документ", f"№ {ext.number.value}"]
    if _accepted(ext.issue_date):
        parts.append(f"от {fmt_date(ext.issue_date.value)}")
    return " ".join(parts)


def _period(ext: c.PrimaryExtraction) -> tuple[c.Period | None, c.Finding | None]:
    """Policy 2: explicit service period, else the issue-date rule."""
    sp = ext.service_period
    if _accepted(sp):
        if sp.basis == "inferred":
            note = c.Finding(
                code="period_inferred",
                message="Период оказания услуг выведен валидатором, а не указан в документе.",
                evidence_ids=sp.evidence_ids,
                version_ids=[ext.version_id],
            )
            return sp.value, note
        return sp.value, None
    if _accepted(ext.issue_date):
        d = ext.issue_date.value
        note = c.Finding(
            code="period_from_issue_date",
            message=f"Период не указан; по правилу даты документа принят {fmt_date(d)}.",
            evidence_ids=ext.issue_date.evidence_ids,
            version_ids=[ext.version_id],
        )
        return c.Period(start=d, end=d), note
    return None, None


def _unit_words(text: str) -> list[str]:
    """Read the whole unit; only a recognized VAT suffix and the redundant ``(₽)`` alias are allowed.

    Unknown suffixes, symbols and divisors stay unsupported instead of silently changing the currency or
    scale. Parenthesized scales need an explicit interpretation, unlike ordinary ``тыс. руб.`` units.
    """
    text = text.lower().replace("ё", "е").strip()
    text = _VAT_SUFFIX.sub("", text)
    text = re.sub(r"\s*\(\s*₽\s*\)\s*$", " ₽", text)
    if not re.fullmatch(r"[a-zа-я₽.\s]+", text):
        return []
    return [w for w in re.findall(r"[a-zа-я₽]+", text) if w not in _NEUTRAL_WORDS]


def _is_rub(text: str) -> bool:
    words = _unit_words(text)
    return bool(words) and all(w in _RUB_WORDS for w in words)


def _money_scale(unit: str | None, *, implicit_rub: bool = False) -> Decimal | None:
    """Factor from ``unit`` to RUB. Unknown currencies, dimensions or extra words never default to RUB."""
    if unit is None:
        return Decimal(1) if implicit_rub else None
    words = _unit_words(unit)
    known = _RUB_WORDS | _KOPECK_WORDS | _THOUSAND_WORDS | _MILLION_WORDS
    if not words or any(w not in known for w in words):
        return None
    rub = any(w in _RUB_WORDS for w in words)
    kopecks = any(w in _KOPECK_WORDS for w in words)
    multiplier = [w for w in words if w in _THOUSAND_WORDS | _MILLION_WORDS]
    if multiplier:
        if len(multiplier) > 1 or kopecks or not (rub or implicit_rub):
            return None
        return Decimal(1000) if multiplier[0] in _THOUSAND_WORDS else Decimal(1_000_000)
    return Decimal(1) if rub else Decimal("0.01")  # "руб. коп." is rubles; kopecks alone are hundredths


def _report_scale(entry: c.IndicatorEntry) -> tuple[Decimal | None, bool]:
    """Factor from a report value to RUB, and whether RUB was assumed.

    Money columns of a cost form often print no unit (the supplied report does not), so a monetary indicator
    without a unit is compared in rubles and the assumption is recorded. A unit that is printed but not
    supported (kg, USD) still blocks the comparison."""
    if entry.unit is None and entry.category != WORKFORCE:
        return Decimal(1), True
    return _money_scale(entry.unit), False


def _scale(ext: c.PrimaryExtraction) -> tuple[Decimal | None, str]:
    """Policy 9: accepted currency and unit must consistently identify a convertible RUB amount."""
    currency = ext.currency.value if _accepted(ext.currency) else None
    if currency is not None and not _is_rub(currency):
        return None, f"валюта суммы не поддерживается ({currency})"
    unit = ext.unit.value if _accepted(ext.unit) else None
    factor = _money_scale(unit, implicit_rub=currency is not None)
    if factor is None:
        return None, f"единица суммы не установлена или не поддерживается ({unit or 'не указана'})"
    return factor, ""


def _doc_amount(ext: c.PrimaryExtraction | None) -> Decimal | None:
    """The document's accepted total in RUB, or None when it is unknown or not convertible."""
    if ext is None or not _accepted(ext.amount):
        return None
    factor, _ = _scale(ext)
    return q(ext.amount.value * factor) if factor is not None else None


def _sums_into(line: _Line, total: _Line, tree: _Tree) -> bool:
    """Whether ``line`` can be one of the lines that ``total`` adds up (policy 5).

    A line labelled as a total (ИТОГО, Всего) sums lines of its own or finer categories. An unlabelled line only
    sums strictly finer categories that it could cover, so equal ordinary lines are never mistaken for subtotals.
    """
    if line.category not in tree.subtree(total.category):
        return False
    if total.marked_total:
        return True
    if line.category == total.category:
        return False
    return not total.possible or any(line.category in tree.subtree(p) for p in total.possible if tree.known(p))


def _lines(ext: c.PrimaryExtraction, factor: Decimal, months, tree: _Tree, tol: Decimal, doc: _Doc) -> None:
    raw: list[_Line] = []
    doc_possible = frozenset(ext.possible_categories)
    for a in ext.allocations:
        cat = a.category.value if _accepted(a.category) else None
        amount = q(a.amount.value * factor) if _accepted(a.amount) else None
        evidence = _dedupe([*a.amount.evidence_ids, *a.category.evidence_ids])
        label = doc.label + (f" — {a.line_label}" if a.line_label else "")
        possible = frozenset(a.possible_categories) | (doc_possible if cat is None else frozenset())
        if cat is not None and amount is not None and (cat == UNRELATED or tree.known(cat)):
            marked = bool(a.line_label and _TOTAL_LABEL.search(a.line_label))
            raw.append(
                _Line(doc.vid, label, cat, frozenset(a.possible_categories), amount, evidence, months, True, "", marked)
            )
        else:
            if cat is None:
                reason = "категория части документа не принята"
            elif amount is None:
                reason = "сумма части документа не принята"
                possible = frozenset({cat})
            else:
                reason = f"категория «{cat}» отсутствует в отчете"
                possible = frozenset({cat})
            raw.append(_Line(doc.vid, label, None, possible, amount, evidence, months, False, reason))

    # Policy 5: a subtotal line (e.g. ИТОГО) equal to the lines under it is not counted again. Ordinary lines of
    # one category that happen to add up (200 000 = 100 000 + 100 000) are all counted; see _sums_into.
    dropped: set[int] = set()
    accepted = sorted(
        (ln for ln in raw if ln.accepted and ln.category != UNRELATED),
        key=lambda ln: (len(tree.subtree(ln.category)), abs(ln.amount)),
    )  # finer categories first: remove an inner subtotal before evaluating its parent
    for line in accepted:
        under = [o for o in accepted if o is not line and id(o) not in dropped and _sums_into(o, line, tree)]
        if under and abs(sum(o.amount for o in under) - line.amount) <= tol:
            dropped.add(id(line))
            doc.findings.append(
                c.Finding(
                    code="subtotal_not_counted",
                    message=f"«{line.label}»: итоговая строка {fmt(line.amount)} не учитывается повторно.",
                    evidence_ids=line.evidence_ids,
                    version_ids=[doc.vid],
                )
            )
    lines = [ln for ln in raw if id(ln) not in dropped]

    # The document amount must be fully allocated; any remainder has unknown category.
    if doc.amount is not None:
        allocated = sum((ln.amount for ln in lines if ln.amount is not None), ZERO)
        remainder = q(doc.amount - allocated)
        if abs(remainder) > tol:
            lines.append(
                _Line(
                    doc.vid,
                    doc.label,
                    None,
                    doc_possible,
                    remainder,
                    list(ext.amount.evidence_ids),
                    months,
                    False,
                    f"{fmt(remainder)} руб. из суммы документа не отнесены к статье",
                )
            )
    elif not lines:
        lines.append(_Line(doc.vid, doc.label, None, doc_possible, None, [], months, False, "сумма не принята"))
    doc.lines = lines


def _exclude(doc: _Doc, reason: str, explanation: str, duplicate_of: str | None = None) -> _Doc:
    ext = doc.extraction
    evidence = []
    if ext is not None:
        source = {
            "other_contract": ext.contracts,
            "outside_period": ext.service_period if _accepted(ext.service_period) else ext.issue_date,
        }.get(reason, ext.amount)
        evidence = list(source.evidence_ids) if source is not None else []
    doc.status = "excluded"
    doc.exclusion = c.Exclusion(
        version_id=doc.vid,
        label=doc.label,
        reason=reason,
        explanation=explanation,
        amount=doc.amount,
        duplicate_of=duplicate_of,
        evidence_ids=evidence,
    )
    return doc


def _unresolve(doc: _Doc, problem: str, reason: str) -> _Doc:
    doc.status, doc.problem, doc.reason = "unresolved", problem, reason
    doc.possible = doc.categories()
    return doc


def _assess(entry: c.ManifestEntry, ext: c.PrimaryExtraction | None, scope: c.Scope, tree: _Tree, tol) -> _Doc:
    doc = _Doc(version=entry.version, extraction=ext, label=_label(ext, entry.version))
    if ext is None:
        problem = "unreadable_input" if entry.preparation == "failed" else "unresolved_interpretation"
        return _unresolve(doc, problem, "документ не прочитан" if entry.preparation == "failed" else "нет извлечения")
    failed = "unreadable_input" if entry.preparation == "failed" else "unresolved_interpretation"
    doc.amount = _doc_amount(ext)

    if not _accepted(ext.contracts):
        return _unresolve(doc, failed, "договор не установлен")
    matches = {_contract_match(x, scope.contract) for x in ext.contracts.value}
    if "ours" not in matches:
        named = ", ".join(ext.contracts.value)
        if "partial" in matches:  # e.g. "24/03/15-01-доп": related to the contract, but not clearly the same
            return _unresolve(doc, "unresolved_interpretation", f"договор «{named}» не удалось сопоставить")
        return _exclude(doc, "other_contract", f"Договор {named}, а не {scope.contract}.")

    period, note = _period(ext)
    if period is None:
        return _unresolve(doc, failed, "период не установлен")
    doc.period = period
    if note is not None:
        doc.findings.append(note)
    span = f"{fmt_date(period.start)}–{fmt_date(period.end)}"
    scope_span = f"{fmt_date(scope.date_from)}–{fmt_date(scope.date_to)}"
    if period.end < scope.date_from or period.start > scope.date_to:
        return _exclude(doc, "outside_period", f"Период {span} вне проверяемого периода {scope_span}.")
    if period.start < scope.date_from or period.end > scope.date_to:
        return _unresolve(doc, "unresolved_interpretation", f"период {span} лишь частично входит в {scope_span}")

    factor, why = _scale(ext)
    if factor is None:
        return _unresolve(doc, "unresolved_interpretation", why)
    _lines(ext, factor, _full_months(period), tree, tol, doc)
    counted = [ln for ln in doc.lines if ln.accepted and ln.category != UNRELATED]
    if counted:
        return doc
    if all(ln.accepted for ln in doc.lines):  # every line accepted as unrelated
        return _exclude(doc, "unrelated", "Документ принят как не относящийся к статьям отчета.")
    return _unresolve(doc, failed, "; ".join(dict.fromkeys(ln.reason for ln in doc.lines if not ln.accepted)))


def _contract_match(text: str, contract: str) -> str:
    """How a contract reference relates to ``contract``: "ours" when it names that number as a whole
    ("№ 24/03/15-01 от 09.04.2024", "договор 24/03/15-01, этап 3"), "partial" when the number only appears
    inside a longer one ("24/03/15-01-доп"), otherwise "other"."""
    needle = _norm(contract)
    hay = _DASHES.sub("-", (text or "").lower().replace("№", " "))
    if not needle:
        return "other"
    # Tolerate spaces inside the number without joining its surrounding prose. A separated suffix
    # ("...-01 - доп") still extends the identifier, so it cannot count as an exact match.
    identifier = r"\s*".join(re.escape(char) for char in needle)
    for match in re.finditer(rf"(?<![\w/-]){identifier}(?![\w/-]|\s*[-/]\s*[\w/-])", hay):
        prefix = hay[: match.start()].rstrip()
        if prefix.endswith("/"):
            continue  # e.g. "99 / 24 / 03 / 15 - 01" is a longer identifier
        if prefix.endswith("-") and not re.search(r"\b(?:договор|контракт)(?:а|у|ом)?\s+-\s+$", hay[: match.start()]):
            continue  # a prose introducer may use a dash; other prefixes stay ambiguous
        return "ours"
    return "partial" if needle in _norm(text) else "other"


def _same_identity(a: c.PrimaryExtraction, b: c.PrimaryExtraction, contract: str) -> bool:
    if not (_accepted(a.number) and _accepted(b.number)) or _norm(a.number.value) != _norm(b.number.value):
        return False
    if not (_accepted(a.contracts) and _accepted(b.contracts)):
        return False
    # Both candidates must name the known scope contract. Comparing full printed references to one
    # another loses matches when either reference includes a date, a prefix or a stage description.
    if not all(
        any(_contract_match(reference, contract) == "ours" for reference in ext.contracts.value) for ext in (a, b)
    ):
        return False
    for field_name in ("doc_type", "counterparty"):
        fa, fb = getattr(a, field_name), getattr(b, field_name)
        if _accepted(fa) and _accepted(fb) and _norm(fa.value) != _norm(fb.value):
            return False
    return True


def _separate_periods(a: c.PrimaryExtraction, b: c.PrimaryExtraction) -> bool:
    """Both documents state (not infer) their service periods, and the periods do not overlap."""
    pa, pb = a.service_period, b.service_period
    if not (_accepted(pa) and _accepted(pb)) or pa.basis != "observed" or pb.basis != "observed":
        return False
    return pa.value.end < pb.value.start or pb.value.end < pa.value.start


def _identity_duplicates(docs: list[_Doc], contract: str) -> None:
    """Policy 3 beyond identical bytes, for two counted documents with the same identity (number, contract,
    type, counterparty): same date and amount is one document twice; anything else is a conflicting version,
    unless both state service periods that do not overlap. Evaluate all pairs before changing eligibility;
    a conflict also blocks the copies connected to that identity. Previously unresolved documents already
    block their categories and are not considered duplicate candidates here."""
    eligible = [d for d in docs if d.status == "counted" and d.extraction is not None]
    neighbours: dict[str, set[str]] = defaultdict(set)
    conflicts: set[str] = set()
    duplicates: list[tuple[_Doc, _Doc]] = []
    for a, b in itertools.combinations(eligible, 2):
        if not _same_identity(a.extraction, b.extraction, contract):
            continue
        if _separate_periods(a.extraction, b.extraction):
            continue  # e.g. monthly statements numbered "1" every month
        neighbours[a.vid].add(b.vid)
        neighbours[b.vid].add(a.vid)
        da, db = a.extraction.issue_date, b.extraction.issue_date
        same_date = _accepted(da) and _accepted(db) and da.value == db.value
        if same_date and a.amount is not None and a.amount == b.amount:
            duplicates.append((a, b))
        else:
            conflicts.update((a.vid, b.vid))

    pending = list(conflicts)
    while pending:
        for vid in neighbours[pending.pop()] - conflicts:
            conflicts.add(vid)
            pending.append(vid)
    for doc in eligible:
        if doc.vid in conflicts:
            text = f"конфликтующие версии документа № {doc.extraction.number.value} (разные дата или сумма)"
            _unresolve(doc, "conflicting_versions", text)
    for a, b in duplicates:
        if a.status == "counted" and b.status == "counted":
            _exclude(b, "duplicate", f"Повтор документа «{a.label}»: тот же номер, дата, договор и сумма.", a.vid)


def _assess_bundle(scope, primaries, manifest, tree, tol) -> tuple[list[_Doc], list[c.Exclusion]]:
    by_vid = {p.version_id: p for p in primaries}
    listed = {e.version.version_id for e in manifest.entries}
    unknown = set(by_vid) - listed
    if unknown:
        raise ValueError(f"extractions for versions missing from the manifest: {sorted(unknown)}")
    newer = {e.version.predecessor_version_id: e.version.version_id for e in manifest.entries}
    newer.pop(None, None)
    docs: list[_Doc] = []
    context: list[c.Exclusion] = []
    first_by_hash: dict[str, _Doc] = {}
    for entry in manifest.entries:
        v = entry.version
        if v.role == "report":
            continue
        ext = by_vid.get(v.version_id)
        if v.role == "context":
            context.append(
                c.Exclusion(
                    version_id=v.version_id,
                    label=v.original_filename,
                    reason="context_only",
                    explanation="Справочный документ набора, не первичный документ.",
                )
            )
            continue
        # Excluded documents keep their amount, so a discrepancy equal to them can be named (see _classify).
        doc = _Doc(version=v, extraction=ext, label=_label(ext, v), amount=_doc_amount(ext))
        if v.version_id in newer:
            docs.append(_exclude(doc, "duplicate", "Заменен более новой версией документа.", newer[v.version_id]))
            continue
        if v.sha256 in first_by_hash:
            original = first_by_hash[v.sha256]
            if doc.amount is None:  # identical bytes: the copy says what the original says
                doc.amount = original.amount
            if ext is None:
                doc.possible = original.categories()
            if doc.label != v.original_filename:  # the copy and the original share number and date
                doc.label = f"{doc.label} (файл {v.original_filename})"
            explanation = f"Файл побайтно совпадает с «{original.label}» (файл {original.version.original_filename})."
            docs.append(_exclude(doc, "duplicate", explanation, original.vid))
            continue
        assessed = _assess(entry, ext, scope, tree, tol)
        first_by_hash[v.sha256] = assessed
        docs.append(assessed)
    _identity_duplicates(docs, scope.contract)
    return docs, context


# ======================= indicators =======================


@dataclass
class _Blocker:
    problem: str
    finding: c.Finding


def _blockers(x: str, docs: list[_Doc], tree: _Tree) -> list[_Blocker]:
    out: list[_Blocker] = []
    for d in docs:
        if d.status == "unresolved" and tree.could_affect(d.possible, x):
            out.append(
                _Blocker(d.problem, c.Finding(code=d.problem, message=f"«{d.label}»: {d.reason}.", version_ids=[d.vid]))
            )
        if d.status != "counted":
            continue
        for ln in d.lines:
            if not ln.accepted and tree.could_affect(ln.possible, x):
                finding = c.Finding(
                    code="unresolved_allocation",
                    message=f"«{ln.label}»: {ln.reason}.",
                    evidence_ids=ln.evidence_ids,
                    version_ids=[d.vid],
                )
                out.append(_Blocker("unresolved_interpretation", finding))
            elif (
                ln.accepted
                and ln.category != UNRELATED
                and ln.category != x
                and x in tree.subtree(ln.category)
                and tree.could_affect(ln.possible, x)
            ):
                finding = c.Finding(
                    code="combined_line_not_split",
                    message=f"«{ln.label}»: {fmt(ln.amount)} руб. указаны одной строкой без разбивки по составу.",
                    evidence_ids=ln.evidence_ids,
                    version_ids=[d.vid],
                )
                out.append(_Blocker("no_supporting_document", finding))
    return out


def _monthly(x, docs, tree, scope_months, monthly_roots) -> tuple[list[c.Finding], list[_Blocker]]:
    gaps: list[c.Finding] = []
    blockers: list[_Blocker] = []
    for root in monthly_roots:
        if not tree.known(root):
            continue
        if x in tree.subtree(root):
            focus = x
        elif root in tree.subtree(x):
            focus = root
        else:
            continue
        covered: set[tuple[int, int]] = set()
        by_month: dict[tuple[int, int], set[str]] = defaultdict(set)
        for d in docs:
            if d.status != "counted":
                continue
            for ln in d.lines:
                if ln.accepted and ln.category in tree.subtree(root):
                    for month in ln.months:
                        by_month[month].add(d.vid)
                    if ln.category in tree.subtree(focus):
                        covered |= ln.months
        missing = [m for m in scope_months if m not in covered]
        if missing:
            gaps.append(
                c.Finding(code="missing_months", message=f"Нет ежемесячных документов за: {_month_text(missing)}.")
            )
        for month, vids in sorted(by_month.items()):
            if len(vids) > 1 and month in scope_months:
                finding = c.Finding(
                    code="month_covered_twice",
                    message=f"За {_month_text([month])} несколько документов одного вида; учесть оба нельзя.",
                    version_ids=sorted(vids),
                )
                blockers.append(_Blocker("conflicting_versions", finding))
    return gaps, blockers


def _unsupported(x, docs, tree) -> list[c.IndicatorEntry]:
    """Nonzero child indicators with no source of their own and no undivided parent line covering them."""
    sub = tree.subtree(x)
    lines = [ln for d in docs if d.status == "counted" for ln in d.lines if ln.accepted and ln.category != UNRELATED]
    out = []
    for entry in tree.entries.values():
        d = entry.category
        if d is None or d == x or d not in sub:
            continue
        value = entry.observation.value if _accepted(entry.observation) else None
        if value is not None and value == 0:
            continue
        own = any(ln.category in tree.subtree(d) for ln in lines)
        parent_line = any(
            ln.category in sub
            and ln.category != d
            and d in tree.subtree(ln.category)
            and tree.could_affect(ln.possible, d)
            for ln in lines
        )
        if not own and not parent_line:
            out.append(entry)
    return out


def _subset(items: Sequence[tuple[object, Decimal]], target: Decimal, tol: Decimal):
    items = [it for it in items if it[1] is not None][:_SUBSET_LIMIT]
    for size in range(1, len(items) + 1):
        for combo in itertools.combinations(items, size):
            if abs(sum(amount for _, amount in combo) - target) <= tol:
                return [obj for obj, _ in combo]
    return None


def _classify(diff, contributions, exclusions, tol) -> tuple[str, str]:
    """Problem category and hint for a confirmed discrepancy."""
    if diff > 0:
        hit = _subset(
            [(e, e.amount) for e in exclusions if e.reason in ("duplicate", "outside_period", "other_contract")],
            diff,
            tol,
        )
        if hit:
            reasons = {e.reason for e in hit}
            problem = {"duplicate": "duplicate", "outside_period": "wrong_period", "other_contract": "wrong_contract"}
            names = ", ".join(f"«{e.label}» ({fmt(e.amount)})" for e in hit)
            hint = f" Разница равна сумме исключенных документов: {names}; вероятно, они ошибочно включены в отчет."
            return (problem[reasons.pop()] if len(reasons) == 1 else "amount_mismatch"), hint
    else:
        hit = _subset([(x, x.amount) for x in contributions], -diff, tol)
        if hit:
            names = ", ".join(f"«{x.label}» ({fmt(x.amount)})" for x in hit)
            return "omitted_document", f" Разница равна сумме документов: {names}; вероятно, они не включены в отчет."
    return "amount_mismatch", " Ни один документ набора не объясняет разницу."


def _arithmetic(entry: c.IndicatorEntry, tree: _Tree, tol: Decimal) -> list[c.Finding]:
    """Policy 1: report arithmetic is checked and reported, never repaired."""
    if not entry.components or not _accepted(entry.observation):
        return []
    parts = [tree.entries[comp] for comp in entry.components]
    if not all(_accepted(p.observation) for p in parts):
        return []
    factor = _report_scale(entry)[0]
    scales = [_report_scale(p)[0] for p in parts]
    if factor is None or any(scale is None for scale in scales):
        return []  # unknown or nonmonetary dimensions cannot be added as if they were RUB
    total = sum((p.observation.value * scale for p, scale in zip(parts, scales)), ZERO)
    reported = entry.observation.value * factor
    if abs(reported - total) <= tol:
        return []
    names = " + ".join(f"гр. {p.column or p.entry_id}" for p in parts)
    evidence = _dedupe([*entry.observation.evidence_ids, *(i for p in parts for i in p.observation.evidence_ids)])
    message = f"В отчете {fmt(reported)}, а {names} = {fmt(total)}; значения отчета не исправляются."
    return [c.Finding(code="report_arithmetic", message=message, evidence_ids=evidence)]


def _notes(contributions: list[c.Contribution], docs: list[_Doc]) -> str:
    """Policy 2 and 5 are recorded in the explanation: which rules shaped the counted sources."""
    vids = {x.version_id for x in contributions}
    codes = [f.code for d in docs if d.vid in vids for f in d.findings]
    notes = []
    by_date = len(
        {d.vid for d in docs if d.vid in vids and any(f.code == "period_from_issue_date" for f in d.findings)}
    )
    if by_date:
        notes.append(
            f" Для {by_date} {'источника' if by_date == 1 else 'источников'} период определен по дате документа."
        )
    if "subtotal_not_counted" in codes:
        notes.append(" Итоговые строки документов не учитывались повторно.")
    return "".join(notes)


def _indicator(entry, docs, tree, scope_months, monthly_roots, tol) -> c.IndicatorResult:
    raw_value = entry.observation.value if _accepted(entry.observation) else None
    factor, assumed = _report_scale(entry)
    # Result amounts, tolerance and conclusion use RUB; the source observation is never mutated.
    converted = factor is not None and factor != 1
    rv = raw_value * factor if raw_value is not None and converted else raw_value
    conversion_note = ""
    conversion_findings = []
    if assumed:
        conversion_findings = [
            c.Finding(
                code="report_unit_assumed",
                message="Единица графы в отчете не указана; значение сравнивается в рублях.",
                evidence_ids=_dedupe([*entry.observation.evidence_ids, *entry.label_evidence_ids]),
            )
        ]
    if converted and raw_value is not None:
        conversion_note = (
            f" В отчете {format(raw_value, 'f')} {entry.unit}; для сравнения приведено к {format(rv, 'f')} руб."
        )
        conversion_findings = [
            c.Finding(
                code="report_unit_normalized",
                message=conversion_note.strip(),
                evidence_ids=_dedupe([*entry.observation.evidence_ids, *entry.label_evidence_ids]),
            )
        ]
    base = dict(
        entry_id=entry.entry_id,
        column=entry.column,
        label=entry.label,
        unit="руб." if converted or assumed else entry.unit,
        category=entry.category,
        reported_value=rv,
        reported_evidence_ids=list(entry.observation.evidence_ids) if rv is not None else [],
    )
    arithmetic = _arithmetic(entry, tree, tol)
    x = entry.category
    if x is None:
        explanation = "Показатель не сопоставлен ни с одной категорией расходов."
        if rv is None:
            problem = "unreadable_report_value"
        else:
            problem = "unresolved_interpretation"
        return c.IndicatorResult(
            **base,
            coverage="none",
            status="insufficient_evidence",
            problem=problem,
            gaps=arithmetic + conversion_findings,
            explanation=explanation + conversion_note,
        )

    sub = tree.subtree(x)
    contributions = [
        c.Contribution(
            version_id=d.vid, label=ln.label, amount=ln.amount, category=ln.category, evidence_ids=ln.evidence_ids
        )
        for d in docs
        if d.status == "counted"
        for ln in d.lines
        if ln.accepted and ln.category in sub
    ]
    exclusions = [
        e.exclusion for e in docs if e.status == "excluded" and e.categories() and tree.could_affect(e.categories(), x)
    ]
    blockers = _blockers(x, docs, tree)
    month_gaps, month_blockers = _monthly(x, docs, tree, scope_months, monthly_roots)
    blockers += month_blockers
    unsupported = _unsupported(x, docs, tree)

    gaps = [b.finding for b in blockers] + month_gaps
    problems = {b.problem for b in blockers}
    if factor is None and x != WORKFORCE:
        problems.add("unresolved_interpretation")
        gaps.append(
            c.Finding(
                code="unsupported_report_unit",
                message=f"Единица показателя не установлена или не поддерживается: {entry.unit or 'не указана'}.",
                evidence_ids=_dedupe([*entry.observation.evidence_ids, *entry.label_evidence_ids]),
            )
        )
    if month_gaps:
        problems.add("missing_coverage" if contributions else "no_supporting_document")
    if unsupported:
        names = ", ".join(f"гр. {e.column or e.entry_id}" for e in unsupported)
        gaps.append(c.Finding(code="unsupported_component", message=f"Нет подтверждающих документов по: {names}."))
        problems.add("no_supporting_document")

    observed = q(sum((x_.amount for x_ in contributions), ZERO)) if contributions else None
    complete = not problems
    if complete and not contributions:
        if rv is not None and rv == 0:
            observed = ZERO  # Policy 7: closed scope, nothing could belong here
        else:
            complete = False
            problems.add("no_supporting_document")
            gaps.append(c.Finding(code="no_documents", message="В наборе нет документов по этой статье."))
    if rv is None:
        problems.add("unreadable_report_value")
    gaps += arithmetic + conversion_findings

    expected = observed if complete else None
    notes = _notes(contributions, docs) + conversion_note
    n = len(contributions)
    docs_text = f"{n} {'источник' if n == 1 else 'источника' if 2 <= n <= 4 else 'источников'}"
    if complete and rv is not None:
        diff = rv - expected
        if abs(diff) <= tol:
            if contributions:
                explanation = f"Сумма {docs_text}: {fmt(expected)} руб., совпадает с отчетом.{notes}"
            else:
                explanation = (
                    "В закрытом наборе нет принятых или неразрешенных расходов этой статьи; нулевое значение подтверждено."
                    + conversion_note
                )
            return c.IndicatorResult(
                **base,
                observed_sum=observed,
                expected_value=expected,
                diff=diff,
                coverage="complete",
                status="verified",
                contributions=contributions,
                exclusions=exclusions,
                gaps=gaps,
                explanation=explanation,
            )
        problem, hint = _classify(diff, contributions, exclusions, tol)
        direction = "завышен" if diff > 0 else "занижен"
        explanation = (
            f"По документам ({docs_text}) {fmt(expected)} руб., в отчете {fmt(rv)} руб.: отчет {direction} на "
            f"{fmt(abs(diff))} руб.{hint}{notes}"
        )
        return c.IndicatorResult(
            **base,
            observed_sum=observed,
            expected_value=expected,
            diff=diff,
            coverage="complete",
            status="discrepancy",
            problem=problem,
            contributions=contributions,
            exclusions=exclusions,
            gaps=gaps,
            explanation=explanation,
        )

    problem = next(p for p in _PROBLEM_ORDER if p in problems)
    coverage = "complete" if complete else ("partial" if contributions else "none")
    if observed is not None and not complete:
        known = f" Учтено по документам (частично): {fmt(observed)} руб."
    elif complete:
        known = f" По документам: {fmt(expected)} руб."
    else:
        known = ""
    explanation = f"Недостаточно данных: {_PROBLEM_TEXT[problem]}.{known}{conversion_note}"
    return c.IndicatorResult(
        **base,
        observed_sum=observed,
        expected_value=expected,
        coverage=coverage,
        status="insufficient_evidence",
        problem=problem,
        contributions=contributions,
        exclusions=exclusions,
        gaps=gaps,
        explanation=explanation,
    )


# ======================= conclusion =======================


def _grand_total_entry(report: c.ReportExtraction, tree: _Tree) -> str | None:
    components = {comp for e in report.indicators for comp in e.components}
    roots = [e for e in report.indicators if e.components and e.entry_id not in components]
    if not roots:
        return None
    return max(roots, key=lambda e: len(tree.entry_descendants(e.entry_id))).entry_id


def _conclusion(results: list[c.IndicatorResult], report, tree: _Tree, row_note: str, tol: Decimal) -> c.Conclusion:
    by_id = {r.entry_id: r for r in results}
    total_id = _grand_total_entry(report, tree)
    discrepant = {r.entry_id for r in results if r.status == "discrepancy" and r.entry_id != total_id}

    def maximal(ids: set[str]) -> set[str]:
        return {i for i in ids if not any(i in tree.entry_descendants(j) for j in ids if j != i)}

    components = []
    for eid in sorted(discrepant, key=lambda i: (by_id[i].column or 0, i)):
        below = maximal(tree.entry_descendants(eid) & discrepant)
        if below and abs(sum((by_id[i].diff for i in below), ZERO) - by_id[eid].diff) <= tol:
            continue  # fully explained by more specific discrepancies
        r = by_id[eid]
        components.append(
            c.ComponentDiscrepancy(
                entry_id=eid,
                diff=r.diff,
                problem=r.problem,
                explanation=r.explanation,
                evidence_ids=r.reported_evidence_ids,
            )
        )
    listed = {d.entry_id for d in components}
    incomplete = [r.entry_id for r in results if r.status == "insufficient_evidence"]

    if total_id is None:
        grand = c.GrandTotal(entry_id=None, status="insufficient_evidence")
        total_text = "В отчете нет итоговой графы."
    else:
        total = by_id[total_id]
        if total.status == "insufficient_evidence":
            grand = c.GrandTotal(entry_id=total_id, status="insufficient_evidence")
            why = "строка отчета не совпадает с выбранной" if row_note else _PROBLEM_TEXT[total.problem]
            total_text = f"Итог отчета не подтвержден: {why}."
        else:
            within = maximal(listed & tree.entry_descendants(total_id))
            explained = sorted(within, key=lambda i: (by_id[i].column or 0, i))
            unexplained = total.diff - sum((by_id[i].diff for i in explained), ZERO)
            grand = c.GrandTotal(
                entry_id=total_id,
                status=total.status,
                diff=total.diff,
                explained_by=explained,
                unexplained_diff=unexplained,
            )
            if total.status == "verified":
                total_text = "Итог отчета подтвержден документами набора."
            else:
                direction = "завышен" if total.diff > 0 else "занижен"
                total_text = f"Итог отчета {direction} на {fmt(abs(total.diff))} руб."
                if explained:
                    names = ", ".join(f"{by_id[i].label} ({_signed(by_id[i].diff)})" for i in explained)
                    total_text += f" Это то же расхождение, что по статьям: {names}."
                if unexplained != 0:
                    total_text += f" Не объяснено статьями: {_signed(unexplained)} руб."

    counts = {s: sum(r.status == s for r in results) for s in ("verified", "discrepancy", "insufficient_evidence")}
    summary = [
        f"Показателей: {len(results)}; подтверждено: {counts['verified']}, расхождений: {counts['discrepancy']}, "
        f"недостаточно данных: {counts['insufficient_evidence']}.",
        total_text,
    ]
    if components and (total_id is None or by_id[total_id].status == "insufficient_evidence"):
        names = ", ".join(f"{by_id[d.entry_id].label} ({_signed(d.diff)})" for d in components)
        summary.append(f"Подтвержденные расхождения по статьям: {names}.")
    if row_note:
        summary.append(row_note)
    return c.Conclusion(
        component_discrepancies=components,
        incomplete_indicators=incomplete,
        grand_total=grand,
        summary=" ".join(summary),
    )


# ======================= entry point =======================

_ROW_NOISE = {"г", "гг", "год", "года", "году"}


def _row_tokens(text: str) -> list[str]:
    """Words of a report row label, ignoring case, punctuation and the year suffix ("2026 г." = "2026 года")."""
    words = re.findall(r"\w+", (text or "").lower().replace("ё", "е"))
    return [w for w in words if w not in _ROW_NOISE]


def _row_blocked(result: c.IndicatorResult, mismatch: c.Finding) -> c.IndicatorResult:
    """The report row that was read is not the confirmed one, so its value is not compared with the documents."""
    update: dict = {"gaps": [*result.gaps, mismatch]}
    if result.status != "insufficient_evidence":
        known = f" По документам: {fmt(result.expected_value)} руб." if result.expected_value is not None else ""
        conversion = " ".join(g.message for g in result.gaps if g.code == "report_unit_normalized")
        update |= {
            "status": "insufficient_evidence",
            "problem": "unresolved_interpretation",
            "diff": None,
            "explanation": f"Недостаточно данных: {mismatch.message}; значение отчета не сравнивается.{known} {conversion}".rstrip(),
        }
    return c.IndicatorResult.model_validate({**result.model_dump(), **update})


def reconcile(
    scope: c.Scope,
    report: c.ReportExtraction,
    primaries: Sequence[c.PrimaryExtraction],
    manifest: c.BundleManifest,
    *,
    tolerance: Decimal | None = None,
    monthly_roots: Sequence[str] = MONTHLY_ROOTS,
) -> c.ReconciliationResult:
    """Calculate every report indicator from validated primary facts, independently of the report's own values."""
    tol = config.TOLERANCE if tolerance is None else tolerance
    tree = _Tree(report)
    docs, context = _assess_bundle(scope, primaries, manifest, tree, tol)
    scope_months = months_between(scope.date_from, scope.date_to)
    doc_exclusions = [d.exclusion for d in docs if d.status == "excluded"]
    results = [_indicator(e, docs, tree, scope_months, monthly_roots, tol) for e in report.indicators]

    row_note = ""
    if _row_tokens(report.row_label) != _row_tokens(scope.report_row):
        row_note = f"Внимание: прочитана строка «{report.row_label}», а выбрана «{scope.report_row}»."
        mismatch = c.Finding(
            code="report_row_mismatch",
            message=f"прочитана строка отчета «{report.row_label}», а выбрана «{scope.report_row}»",
        )
        results = [_row_blocked(r, mismatch) for r in results]

    unresolved = [
        c.UnresolvedDocument(version_id=d.vid, label=d.label, reason=d.reason, possible_categories=sorted(d.possible))
        for d in docs
        if d.status == "unresolved"
    ]
    for d in docs:
        if d.status == "counted" and any(not ln.accepted for ln in d.lines):
            reasons = "; ".join(dict.fromkeys(ln.reason for ln in d.lines if not ln.accepted))
            possible = sorted({p for ln in d.lines if not ln.accepted for p in ln.possible})
            unresolved.append(
                c.UnresolvedDocument(version_id=d.vid, label=d.label, reason=reasons, possible_categories=possible)
            )

    return c.ReconciliationResult(
        scope=scope,
        tolerance=tol,
        indicators=results,
        exclusions=doc_exclusions + context,
        unresolved=unresolved,
        conclusion=_conclusion(results, report, tree, row_note, tol),
    )
