"""Money normalization and nested-subtotal regressions from the PR 23 review."""

from datetime import date
from decimal import Decimal
import re

import pytest
import recon_fixtures as f

from sverka import contracts as c
from sverka.reconciliation import reconcile

D = Decimal


def entry(
    entry_id: str,
    category: str,
    value: str,
    *,
    unit: str = "руб.",
    components: tuple[str, ...] = (),
) -> c.IndicatorEntry:
    return c.IndicatorEntry(
        entry_id=entry_id,
        label=entry_id,
        category=category,
        unit=unit,
        components=list(components),
        observation=f.acc(c.DecimalObservation, D(value), "kfz", entry_id),
    )


def run_document(
    amount: str,
    indicators: list[c.IndicatorEntry],
    *,
    lines: list[tuple] | None = None,
    unit: str = "руб.",
    currency: str = "RUB",
    tolerance: Decimal = D("0.01"),
) -> tuple[c.ReconciliationResult, c.ReportExtraction]:
    primary = f.primary(
        "money",
        doc_type="Акт",
        number="M-1",
        issue=date(2026, 3, 1),
        amount=amount,
        lines=lines if lines is not None else [("materials", amount, (), None)],
        unit=unit,
        currency=currency,
    )
    scope, _, primaries, manifest = f.bundle([f.Item("money", "primary", primary)])
    report = c.ReportExtraction(version_id="ver_kfz", row_label=f.ROW, indicators=indicators)
    original = report.model_dump(mode="json")
    result = reconcile(scope, report, primaries, manifest, tolerance=tolerance, monthly_roots=())
    assert report.model_dump(mode="json") == original
    c.ReconciliationResult.model_validate(result.model_dump(mode="json"))
    return result, report


def indicator(result: c.ReconciliationResult, entry_id: str) -> c.IndicatorResult:
    return next(item for item in result.indicators if item.entry_id == entry_id)


@pytest.mark.parametrize(("unit", "currency"), [("GBP", "GBP"), ("руб.", "USD")])
def test_foreign_or_conflicting_currency_cannot_verify_a_ruble_report(unit, currency):
    result, _ = run_document("1000", [entry("material", "materials", "1000")], unit=unit, currency=currency)

    material = indicator(result, "material")
    assert material.status == "insufficient_evidence"
    assert material.problem == "unresolved_interpretation"
    assert material.contributions == []
    assert material.expected_value is None and material.diff is None
    assert [document.version_id for document in result.unresolved] == ["ver_money"]


@pytest.mark.parametrize("unit", ["GBP", "kg", "тыс. USD"])
def test_ruble_currency_does_not_override_an_unsupported_primary_unit(unit):
    result, _ = run_document("1000", [entry("material", "materials", "1000")], unit=unit)
    material = indicator(result, "material")
    assert material.status == "insufficient_evidence"
    assert material.expected_value is None and material.contributions == []


@pytest.mark.parametrize("unit", ["kg", "USD", "руб. и USD"])
def test_an_unknown_report_unit_cannot_verify_even_zero(unit):
    result, _ = run_document("0", [entry("material", "materials", "0", unit=unit)])
    material = indicator(result, "material")
    assert material.status == "insufficient_evidence"
    assert material.problem == "unresolved_interpretation"
    assert material.expected_value is None and material.diff is None
    assert any(gap.code == "unsupported_report_unit" for gap in material.gaps)


@pytest.mark.parametrize(
    ("unit", "amount"),
    [("RUB", "1000"), ("коп.", "100000"), ("тыс. руб.", "1"), ("млн. руб.", "0.001")],
)
def test_supported_primary_units_still_convert_to_rubles(unit, amount):
    result, _ = run_document(amount, [entry("material", "materials", "1000")], unit=unit)

    material = indicator(result, "material")
    assert material.status == "verified"
    assert material.expected_value == D("1000.00") and material.diff == 0
    assert [contribution.amount for contribution in material.contributions] == [D("1000.00")]
    assert result.unresolved == []


def test_report_thousands_are_not_compared_as_literal_rubles():
    result, report = run_document("1000", [entry("material", "materials", "1000", unit="тыс. руб.")])

    material = indicator(result, "material")
    assert material.status == "discrepancy"
    assert material.unit == "руб."
    assert material.reported_value == D("1000000")
    assert material.expected_value == D("1000.00")
    assert material.diff == D("999000.00")
    assert report.indicators[0].observation.value == D("1000")
    assert report.indicators[0].unit == "тыс. руб."
    assert re.search(r"\b1\s?000(?:[,.]0+)?\s+тыс\. руб\.", material.explanation)


@pytest.mark.parametrize(
    ("unit", "value"),
    [("коп.", "100000"), ("тыс. руб.", "1"), ("млн. руб.", "0.001")],
)
def test_matching_scaled_report_keeps_the_source_observation(unit, value):
    result, report = run_document("1000", [entry("material", "materials", value, unit=unit)])

    material = indicator(result, "material")
    assert material.status == "verified"
    assert material.unit == "руб."
    assert material.reported_value == material.expected_value == D("1000.00")
    assert material.diff == 0
    assert report.indicators[0].observation.value == D(value)
    assert report.indicators[0].unit == unit
    assert unit in material.explanation


def test_report_row_mismatch_keeps_the_source_unit_conversion_note():
    primary = f.primary(
        "money",
        doc_type="Акт",
        number="M-1",
        issue=date(2026, 3, 1),
        amount="1000",
        lines=[("materials", "1000", (), None)],
    )
    scope, _, primaries, manifest = f.bundle([f.Item("money", "primary", primary)])
    report = c.ReportExtraction(
        version_id="ver_kfz",
        row_label="another row",
        indicators=[entry("material", "materials", "1", unit="тыс. руб.")],
    )
    material = indicator(reconcile(scope, report, primaries, manifest), "material")
    assert material.status == "insufficient_evidence" and material.diff is None
    assert material.reported_value == D("1000")
    assert "1 тыс. руб." in material.explanation


@pytest.mark.parametrize(
    ("reported", "status", "difference"),
    [("1.00001", "verified", "0.01"), ("1.00002", "discrepancy", "0.02")],
)
def test_report_unit_conversion_uses_the_global_ruble_tolerance(reported, status, difference):
    result, _ = run_document("1000", [entry("material", "materials", reported, unit="тыс. руб.")])

    material = indicator(result, "material")
    assert material.status == status
    assert material.diff == D(difference)
    assert result.tolerance == D("0.01")


@pytest.mark.parametrize(
    ("total_value", "arithmetic_gap"),
    [("2", False), ("1000.001", True), ("2.00001", False), ("2.00002", True)],
)
def test_report_arithmetic_compares_mixed_units_in_rubles(total_value, arithmetic_gap):
    result, _ = run_document(
        "2000",
        [
            entry("total", "total", total_value, unit="тыс. руб.", components=("material", "admin")),
            entry("material", "materials", "1000"),
            entry("admin", "administrative_overhead", "0.001", unit="млн. руб."),
        ],
        lines=[("materials", "1000", (), None), ("administrative_overhead", "1000", (), None)],
    )

    total = indicator(result, "total")
    assert any(gap.code == "report_arithmetic" for gap in total.gaps) is arithmetic_gap
    assert total.reported_value == D(total_value) * 1000
    assert total.expected_value == D("2000.00")
    if not arithmetic_gap:
        assert all(item.status == "verified" for item in result.indicators)
    else:
        assert total.status == "discrepancy"
        assert total.diff == D(total_value) * 1000 - D("2000.00")


@pytest.mark.parametrize(
    ("total", "account", "main", "additional", "reserve", "reported"),
    [
        ("100", "80", "70", "10", "20", "110"),
        ("-100", "-80", "-70", "-10", "-20", "-90"),
        ("20", "0", "10", "-10", "20", "30"),
    ],
    ids=["positive", "negative", "zero-subtotal"],
)
def test_nested_primary_subtotals_do_not_create_an_unallocated_remainder(
    total, account, main, additional, reserve, reported
):
    result, _ = run_document(
        total,
        [
            entry("total", "total", reported, components=("account", "reserve")),
            entry("account", "payroll_account", account, components=("main", "additional")),
            entry("main", "payroll_main", main),
            entry("additional", "payroll_additional", additional),
            entry("reserve", "leave_reserve", reserve),
        ],
        lines=[
            ("total", total, (), "ИТОГО"),
            ("payroll_account", account, (), "ИТОГО счет 211"),
            ("payroll_main", main, (), "Основная"),
            ("payroll_additional", additional, (), "Дополнительная"),
            ("leave_reserve", reserve, (), "Резерв"),
        ],
    )

    grand_total = indicator(result, "total")
    assert grand_total.status == "discrepancy"
    assert grand_total.expected_value == D(total)
    assert grand_total.diff == D("10.00")
    assert sorted(item.amount for item in grand_total.contributions) == sorted(map(D, (main, additional, reserve)))
    assert all(
        indicator(result, entry_id).status == "verified" for entry_id in ("account", "main", "additional", "reserve")
    )
    assert result.unresolved == []
    assert result.conclusion.grand_total.diff == D("10.00")
