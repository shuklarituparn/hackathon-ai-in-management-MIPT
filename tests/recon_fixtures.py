"""Typed case19 fixtures for reconciliation tests. Synthetic, not recorded live extractions.

Amounts, numbers, dates and counterparties come from examples/case19. A field the document does not print
(an issue date on a statement, a service period on most acts) is unresolved. This is the single source of
typed case19 values: tests/fixtures/case19/manifest.json mirrors it, and test_case19_fixtures.py checks
both against each other and against the spreadsheets. Test-only: never pass these objects, or the oracle,
to the agent as evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from sverka import contracts as c

CONTRACT = "24/03/15-01"
ROW = "3 этап, 2026 г."
SCOPE = c.Scope(
    contract=CONTRACT, report_row=ROW, date_from=date(2026, 1, 1), date_to=date(2026, 5, 31), confirmed=True
)
WORKSPACE = "ws_case19"
NOW = datetime(2026, 11, 13, tzinfo=UTC)

# entry_id, column, label, unit, category, components, reported value (the supplied report, row "3 этап, 2026 г.")
INDICATORS = [
    ("col3", 3, "Трудоемкость (человеко-месяц)", "чел.-мес.", "workforce_intensity", [], "34.14"),
    ("col4", 4, "Материальные затраты", "руб.", "materials", [], "0.00"),
    ("col5", 5, "Затраты на оплату труда — всего (0200)", "руб.", "payroll_total", ["col6", "col9"], "5558474.83"),
    ("col6", 6, "в т.ч. заработная плата, счет 211", "руб.", "payroll_account", ["col7", "col8"], "5248019.04"),
    ("col7", 7, "основная заработная плата", "руб.", "payroll_main", [], "5054470.26"),
    ("col8", 8, "дополнительная заработная плата", "руб.", "payroll_additional", [], "193548.78"),
    ("col9", 9, "резерв отпусков", "руб.", "leave_reserve", [], "310455.79"),
    ("col10", 10, "Страховые взносы — всего (0300)", "руб.", "contributions_total", ["col11", "col14"], "1671359.96"),
    (
        "col11",
        11,
        "взносы на заработную плату, счет 213",
        "руб.",
        "contributions_account",
        ["col12", "col13"],
        "1578126.37",
    ),
    ("col12", 12, "взносы на основную заработную плату", "руб.", "contributions_main", [], "1526450.09"),
    ("col13", 13, "взносы на дополнительную заработную плату", "руб.", "contributions_additional", [], "51676.28"),
    ("col14", 14, "взносы на резерв отпусков", "руб.", "contributions_reserve", [], "93233.59"),
    ("col15", 15, "Общепроизводственные затраты (0800)", "руб.", "production_overhead", [], "1145798.10"),
    ("col16", 16, "Общехозяйственные затраты (0900)", "руб.", "administrative_overhead", [], "3204744.51"),
    ("col17", 17, "Итого", "руб.", "total", ["col4", "col5", "col10", "col15", "col16"], "11580377.40"),
]

# Month -> (main payroll 0201, additional payroll and leave reserve 0202).
PAYROLL = {
    1: ("1010000.00", "100000.00"),
    2: ("1015500.00", "101500.00"),
    3: ("1002300.00", "99500.00"),
    4: ("1018750.00", "102000.00"),
    5: ("1007920.26", "101004.57"),
}
# Month -> accrued contributions 0300 (one line, no split).
CONTRIBUTIONS = {1: "333000.00", 2: "335100.00", 3: "330540.00", 4: "337225.00", 5: "335494.96"}
CONTRIBUTION_SPLIT = ["contributions_main", "contributions_additional", "contributions_reserve"]
MONTHS_RU = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май"}


def _ev(key: str, name: str) -> list[str]:
    return [f"blk_{key}_{name}"]


def acc(cls, value, key: str, name: str, basis: str = "observed"):
    return cls(state="accepted", value=value, evidence_ids=_ev(key, name), basis=basis)


def unresolved(cls, reason: str = "не указано в документе"):
    return cls(state="unresolved", reason=reason)


def line(key, category, amount, possible=(), label=None, *, n=0) -> c.AllocationLine:
    cat = (
        acc(c.CategoryObservation, category, key, f"cat{n}", "inferred")
        if category
        else unresolved(c.CategoryObservation, "категория не установлена")
    )
    amt = (
        acc(c.DecimalObservation, Decimal(amount), key, f"line{n}")
        if amount is not None
        else unresolved(c.DecimalObservation)
    )
    return c.AllocationLine(line_label=label, category=cat, possible_categories=list(possible), amount=amt)


def primary(
    key: str,
    *,
    doc_type: str | None,
    number: str | None,
    amount: str | None,
    lines: list[tuple] | None = None,
    issue: date | None = None,
    period: tuple[date, date] | None = None,
    contract: str | None = CONTRACT,
    counterparty: str | None = None,
    unit: str | None = "руб.",
    currency: str | None = "RUB",
    possible: list[str] = (),
) -> c.PrimaryExtraction:
    """``lines``: (category or None, amount or None, possible categories, line label)."""
    vid = f"ver_{key}"
    text = lambda value, name: acc(c.TextObservation, value, key, name) if value else unresolved(c.TextObservation)  # noqa: E731
    return c.PrimaryExtraction(
        version_id=vid,
        doc_type=text(doc_type, "type"),
        number=text(number, "number"),
        contracts=acc(c.TextListObservation, [contract], key, "contract")
        if contract
        else unresolved(c.TextListObservation),
        counterparty=text(counterparty, "counterparty"),
        issue_date=acc(c.DateObservation, issue, key, "date") if issue else unresolved(c.DateObservation),
        service_period=acc(c.PeriodObservation, c.Period(start=period[0], end=period[1]), key, "period")
        if period
        else unresolved(c.PeriodObservation, "период в документе не указан"),
        amount=acc(c.DecimalObservation, Decimal(amount), key, "amount")
        if amount is not None
        else unresolved(c.DecimalObservation, "сумма не прочитана"),
        currency=text(currency, "currency"),
        unit=text(unit, "unit"),
        allocations=[line(key, *ln, n=i) for i, ln in enumerate(lines or [])],
        possible_categories=list(possible),
    )


def month(m: int) -> tuple[date, date]:
    start = date(2026, m, 1)
    end = date(2026, m + 1, 1) if m < 12 else date(2027, 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def payroll(m: int, *, number: str | None = None, key: str | None = None) -> c.PrimaryExtraction:
    main, rest = PAYROLL[m]
    total = str(Decimal(main) + Decimal(rest))
    return primary(
        key or f"pay{m:02d}",
        doc_type="Расчетная ведомость",
        number=number or f"ВЕД-ЗП-2026-{m:02d}",
        amount=total,
        period=month(m),
        lines=[
            ("payroll_main", main, (), "0201 Основная заработная плата"),
            ("payroll_total", rest, ("payroll_additional", "leave_reserve"), "0202 Дополнительная зарплата и резерв"),
            ("payroll_total", total, (), "ИТОГО"),  # subtotal: must not be counted again
        ],
    )


def contributions(m: int) -> c.PrimaryExtraction:
    amount = CONTRIBUTIONS[m]
    return primary(
        f"con{m:02d}",
        doc_type="Расчет страховых взносов",
        number=f"ВЗН-2026-{m:02d}",
        amount=amount,
        period=month(m),
        lines=[("contributions_total", amount, CONTRIBUTION_SPLIT, "0300 Начислено взносов")],
    )


def overhead(key, doc_type, number, issue, amount, category, counterparty, *, period=None, contract=CONTRACT):
    return primary(
        key,
        doc_type=doc_type,
        number=number,
        issue=issue,
        period=period,
        amount=amount,
        contract=contract,
        counterparty=counterparty,
        lines=[(category, amount, (), None)],
    )


PROD, ADMIN = "production_overhead", "administrative_overhead"


def overheads() -> list[c.PrimaryExtraction]:
    return [
        overhead(
            "act118", "Акт", "118", date(2026, 1, 28), "245000.00", PROD, "ООО «ЛабСервис Альфа»", period=month(1)
        ),
        overhead("sa174", "Счет-акт", "174", date(2026, 2, 25), "318298.10", PROD, "ООО «ИнжКонтур Бета»"),
        overhead("kl31", "Служебная записка", "КЛ-31", date(2026, 4, 10), "270000.00", PROD, None),
        overhead("upd77", "УПД", "0426-77", date(2026, 5, 22), "300000.00", PROD, "ООО «СпецАренда Гамма»"),
        overhead("ia15", "Акт", "ИА-15", date(2026, 1, 31), "620000.00", ADMIN, "ООО «Аналитика Дельта»"),
        overhead("adm27", "Акт", "ADM-27", date(2026, 2, 27), "487500.00", ADMIN, "ООО «ДокКонтур Эпсилон»"),
        overhead("adm27copy", "Акт", "ADM-27", date(2026, 2, 27), "487500.00", ADMIN, "ООО «ДокКонтур Эпсилон»"),
        overhead("it0326", "Справка о расходах ИТ", "03-26", None, "703244.51", ADMIN, None, period=month(3)),
        overhead("sv0426", "Акт", "СВ-0426", date(2026, 4, 29), "650000.00", ADMIN, "ООО «КомЛинк Дзета»"),
        overhead("upr55", "УПД", "УПР-55", date(2026, 5, 29), "744000.00", ADMIN, "ООО «ОргКонтур Эта»"),
        overhead(
            "org0603", "Акт", "ОРГ-0603", date(2026, 6, 3), "165000.00", ADMIN, "ООО «ОргКонтур Эта»", period=month(6)
        ),
        overhead(
            "adm75",
            "Акт",
            "ADM-75",
            date(2026, 5, 15),
            "98000.00",
            ADMIN,
            "ООО «ДокКонтур Эпсилон»",
            contract="24/03/16-02",
        ),
    ]


def report(overrides: dict[str, str | None] | None = None, row: str = ROW) -> c.ReportExtraction:
    """The supplied report. ``overrides`` maps entry_id to a typed value, or None for an unreadable cell."""
    overrides = overrides or {}
    indicators = []
    for entry_id, column, label, unit, category, components, value in INDICATORS:
        value = overrides.get(entry_id, value)
        obs = (
            acc(c.DecimalObservation, Decimal(value), "kfz", entry_id)
            if value is not None
            else unresolved(c.DecimalObservation, "ячейка не прочитана")
        )
        indicators.append(
            c.IndicatorEntry(
                entry_id=entry_id,
                label=label,
                column=column,
                unit=unit,
                category=category,
                components=components,
                observation=obs,
            )
        )
    return c.ReportExtraction(version_id="ver_kfz", row_label=row, indicators=indicators)


@dataclass
class Item:
    """One supplied file: its version, its validated extraction (None if there is none) and preparation state."""

    key: str
    role: str
    extraction: c.PrimaryExtraction | None
    sha: str | None = None
    preparation: str = "prepared"
    predecessor: str | None = None

    def version(self) -> c.DocumentVersion:
        return c.DocumentVersion(
            workspace_id=WORKSPACE,
            document_id=f"doc_{self.key}",
            version_id=f"ver_{self.key}",
            predecessor_version_id=self.predecessor,
            sha256=self.sha or hashlib.sha256(self.key.encode()).hexdigest(),
            size_bytes=1,
            role=self.role,
            original_filename=f"{self.key}.pdf",
            format="pdf",
            original_artifact_id=f"art_{self.key}",
            created_at=NOW,
        )


def items() -> list[Item]:
    """The complete baseline bundle: 22 primaries. The ADM-27 copy has the same bytes as ADM-27."""
    out = [Item(f"pay{m:02d}", "primary", payroll(m)) for m in PAYROLL]
    out += [Item(f"con{m:02d}", "primary", contributions(m)) for m in CONTRIBUTIONS]
    out += [Item(p.version_id[4:], "primary", p) for p in overheads()]
    same = hashlib.sha256(b"adm27").hexdigest()
    return [replace(i, sha=same) if i.key in ("adm27", "adm27copy") else i for i in out]


def bundle(primaries: list[Item] | None = None, *, report_overrides=None, row: str = ROW, scope: c.Scope = SCOPE):
    """(scope, report, primaries, manifest) for ``primaries`` plus the report and the README context file."""
    primaries = items() if primaries is None else primaries
    entries = [Item("kfz", "report", None), *primaries, Item("readme", "context", None)]
    manifest = c.BundleManifest(
        workspace_id=WORKSPACE,
        entries=[c.ManifestEntry(version=i.version(), preparation=i.preparation) for i in entries],
    )
    extractions = [i.extraction for i in primaries if i.extraction is not None]
    return scope, report(report_overrides, row), extractions, manifest


def without(*keys: str) -> list[Item]:
    return [i for i in items() if i.key not in keys]


def plus(*extra: Item) -> list[Item]:
    return [*items(), *extra]


# Published reconciliation results for the two use cases (tests/fixtures/reconciliation/).
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "reconciliation")
USE_CASES = {"baseline": (), "missing_march": ("pay03",)}


def use_case_json(name: str) -> str:
    from sverka.reconciliation import reconcile

    result = reconcile(*bundle(without(*USE_CASES[name])))
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    for case in USE_CASES:
        target = os.path.join(RESULTS_DIR, f"{case}_result.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(use_case_json(case))
        print(target)
