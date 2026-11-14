"""V04: spreadsheet formula states (T04).

Raw formulas, cached values, derived values and errors stay separate; derived values use Decimal; an unknown
dependency stays unknown; ROUND rounds half away from zero; literal 1000 stays 1000.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from sverka import contracts as c
from sverka import evidence as ev
from sverka import formulas as fx

ROOT = Path(__file__).resolve().parent.parent
CASE19 = ROOT / "examples" / "case19"
UNCACHED = ROOT / "tests" / "fixtures" / "case19" / "scenarios" / "uncached_sum" / "expected.json"
VERSION = "ver_formulas"


def _workbook(cells: dict[str, object], *, title: str = "Лист 1", extra: dict[str, dict] | None = None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for ref, value in cells.items():
        ws[ref] = value
    for name, more in (extra or {}).items():
        other = wb.create_sheet(name)
        for ref, value in more.items():
            other[ref] = value
    return wb


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _states(wb) -> dict[str, c.FormulaState]:
    _, states, _ = ev.spreadsheet_evidence(_bytes(wb), VERSION)
    return {s.ref: s for s in states}


def _locators(wb) -> dict[str, c.CellLocator]:
    blocks, _, _ = ev.spreadsheet_evidence(_bytes(wb), VERSION)
    return {b.locator.ref: b.locator for b in blocks}


# ======================= the supplied payroll statements =======================


@pytest.mark.parametrize("month", ["01", "02", "03", "04", "05"])
def test_uncached_payroll_sum_is_derived_with_decimal(month):
    [path] = CASE19.glob(f"02_{month}_*.xlsx")
    blocks, states, warnings = ev.spreadsheet_evidence(path.read_bytes(), VERSION)
    [state] = states
    ws = openpyxl.load_workbook(path)["Ведомость"]
    expected = sum(Decimal(str(ws[r].value)) for r in ("C6", "C7"))
    assert (state.ref, state.formula, state.cached_value) == ("C8", "=SUM(C6:C7)", None)
    assert state.evaluation == "derived" and state.derived_value == expected and state.error is None
    assert state.inputs == ["C6:C7"]
    [block] = [b for b in blocks if b.locator.ref == "C8"]
    assert block.text == "=SUM(C6:C7)"  # the file shows the formula; the derived value is ours
    assert block.locator.derived_value == expected
    assert warnings == []


def test_uncached_sum_matches_the_scenario_oracle_exactly():
    import json

    spec = json.loads(UNCACHED.read_text(encoding="utf-8"))["expected_preparation"]
    [(name, prep)] = spec.items()
    _, states, _ = ev.spreadsheet_evidence((CASE19 / name).read_bytes(), VERSION)
    [state] = [s for s in states if s.ref == prep["cell"]["ref"]]
    assert state.model_dump(mode="json", exclude={"error", "kind"}) == prep["cell"]


# ======================= values and literals =======================


def test_literal_1000_stays_1000_and_is_not_a_formula():
    locs = _locators(_workbook({"A1": 1000, "A2": "1000", "A3": "=A1"}))
    assert (locs["A1"].evaluation, locs["A1"].cached_value, locs["A1"].formula) == ("literal", "1000", None)
    assert locs["A2"].cached_value == "1000"
    assert locs["A3"].derived_value == Decimal("1000") and format(locs["A3"].derived_value, "f") == "1000"


def test_decimal_arithmetic_has_no_float_noise():
    states = _states(_workbook({"A1": 0.1, "A2": 0.2, "B1": "=A1+A2", "B2": "=SUM(A1:A2)*3"}))
    assert states["B1"].derived_value == Decimal("0.3")
    assert states["B2"].derived_value == Decimal("0.9")


def test_round_is_half_away_from_zero():
    states = _states(
        _workbook({"A1": "=ROUND(2.5,0)", "A2": "=ROUND(-2.5,0)", "A3": "=ROUND(1.005,2)", "A4": "=ОКРУГЛ(1234.5;-2)"})
    )
    assert [states[r].derived_value for r in ("A1", "A2", "A3", "A4")] == [
        Decimal("3"),
        Decimal("-3"),
        Decimal("1.01"),  # Decimal sees 1.005 exactly; a float would give 1.0
        Decimal("1200"),
    ]


def test_number_format_pads_but_never_rounds():
    wb = _workbook({"A1": 1010000, "A2": 100000, "A3": 0.125, "B1": "=SUM(A1:A2)", "B2": "=A3"})
    wb.active["B1"].number_format = "#,##0.00"
    wb.active["B2"].number_format = "#,##0.00"
    states = _states(wb)
    assert format(states["B1"].derived_value, "f") == "1110000.00"
    assert format(states["B2"].derived_value, "f") == "0.125"  # shown as 0,13 but the value is kept


def test_sum_skips_blank_text_and_booleans_like_excel():
    states = _states(_workbook({"A1": 5, "A2": "примечание", "A4": True, "A5": 7, "B1": "=SUM(A1:A5)"}))
    assert states["B1"].derived_value == Decimal("12")


def test_cross_sheet_and_chained_formulas():
    wb = _workbook(
        {"A1": "='Данные 2'!B2+Data!C3", "A2": "=A1*2", "A3": "=СУММ(A1;A2)"},
        extra={"Данные 2": {"B2": 100}, "Data": {"C3": 50}},
    )
    states = _states(wb)
    assert [states[r].derived_value for r in ("A1", "A2", "A3")] == [Decimal("150"), Decimal("300"), Decimal("450")]
    assert states["A1"].inputs == ["Данные 2!B2", "Data!C3"]


# ======================= unknown stays unknown =======================


@pytest.mark.parametrize(
    "formula, reason",
    [
        ("=[1]Sheet1!A1", "another workbook"),
        ("=Missing!A1+1", "unknown sheet"),
        ("=VLOOKUP(1,B1:B2,1)", "VLOOKUP is not supported"),
        ("=Ставка*2", "defined name"),
        ("=B9+1", "empty"),  # Excel would read 0; a lost input is not zero
        ("=SUM(B:B)", "whole-row or whole-column"),
        ('=B1&"x"', "concatenation"),
        ("=IF(B1>0,1,2)", "IF is not supported"),
    ],
)
def test_unknown_dependency_stays_unresolved(formula, reason):
    state = _states(_workbook({"B1": 10, "A1": formula}))["A1"]
    assert state.evaluation == "unresolved" and state.derived_value is None
    assert reason in state.error
    assert state.formula == formula  # the raw formula is kept


def test_a_formula_depending_on_an_unresolved_one_is_unresolved():
    states = _states(_workbook({"A1": "=Missing!A1", "A2": "=A1+1", "A3": "=SUM(A1:A2)"}))
    assert {states[r].evaluation for r in ("A1", "A2", "A3")} == {"unresolved"}
    assert "depends on" in states["A2"].error


def test_circular_references_are_unresolved_not_zero():
    states = _states(_workbook({"A1": "=A2+1", "A2": "=A1+1"}))
    assert states["A1"].evaluation == states["A2"].evaluation == "unresolved"
    assert "circular" in states["A1"].error


# ======================= errors =======================


@pytest.mark.parametrize(
    "cells, code",
    [
        ({"A1": 5, "B1": "=A1/0"}, "#DIV/0!"),
        ({"A1": "текст", "B1": "=A1+1"}, "#VALUE!"),
        ({"A1": "#N/A", "B1": "=SUM(A1:A2)"}, "#N/A"),
        ({"A1": 5, "B1": "=A1+#REF!"}, "#REF!"),
    ],
)
def test_spreadsheet_errors_are_errors_without_a_value(cells, code):
    state = _states(_workbook(cells))["B1"]
    assert state.evaluation == "error" and state.derived_value is None
    assert state.error.startswith(code)
    c.FormulaState.model_validate(state.model_dump())


def test_cached_value_is_kept_apart_and_a_mismatch_is_reported(tmp_path):
    # openpyxl cannot write cached results, so build the cached value into the sheet XML directly.
    wb = _workbook({"A1": 10, "A2": 20, "A3": "=SUM(A1:A2)"})
    path = tmp_path / "cached.xlsx"
    wb.save(path)
    _set_cached_value(path, "A3", "999")
    blocks, states, warnings = ev.spreadsheet_evidence(path.read_bytes(), VERSION)
    [state] = states
    assert (state.cached_value, state.derived_value, state.evaluation) == ("999", Decimal("30"), "derived")
    [block] = [b for b in blocks if b.locator.ref == "A3"]
    assert block.text == "999"  # what the file shows
    assert len(warnings) == 1 and "999" in warnings[0] and "30" in warnings[0]


@pytest.mark.parametrize(
    "cells, ref, cached, derived",
    [
        ({"A1": 0.1, "A2": 0.2, "A3": "=A1+A2"}, "A3", repr(0.1 + 0.2), "0.3"),
        (
            {"A1": 0.07, "A2": 789.01, "A3": 1234.56, "A4": "=SUM(A1:A3)"},
            "A4",
            repr(0.07 + 789.01 + 1234.56),
            "2023.64",
        ),
        ({"A1": 100, "A2": "=A1/3"}, "A2", repr(100 / 3), None),
        (
            {"A1": 1234567890.12, "A2": 1234567890.02, "A3": "=A1-A2"},
            "A3",
            repr(1234567890.12 - 1234567890.02),
            "0.10",
        ),
    ],
)
def test_binary_float_noise_in_a_cached_value_is_not_a_mismatch(tmp_path, cells, ref, cached, derived):
    # Excel stores what binary doubles give (0.1 + 0.2 as 0.30000000000000004); the exact result agrees with it.
    assert Decimal(cached) != Decimal(derived or "33.33")  # the stored digits do differ from the exact result
    path = tmp_path / "noise.xlsx"
    _workbook(cells).save(path)
    _set_cached_value(path, ref, cached)
    _, [state], warnings = ev.spreadsheet_evidence(path.read_bytes(), VERSION)
    assert state.evaluation == "derived" and warnings == []
    if derived is not None:
        assert state.derived_value == Decimal(derived)  # the derived value stays exact


def test_a_cached_value_one_kopeck_off_is_still_a_mismatch(tmp_path):
    path = tmp_path / "kopeck.xlsx"
    _workbook({"A1": 0.07, "A2": 789.01, "A3": 1234.56, "A4": "=SUM(A1:A3)"}).save(path)
    _set_cached_value(path, "A4", "2023.65")
    _, [state], warnings = ev.spreadsheet_evidence(path.read_bytes(), VERSION)
    assert state.derived_value == Decimal("2023.64")
    assert len(warnings) == 1 and "2023.65" in warnings[0] and "2023.64" in warnings[0]


def _set_cached_value(path: Path, ref: str, value: str) -> None:
    import re
    import zipfile

    with zipfile.ZipFile(path) as z:
        items = {n: z.read(n) for n in z.namelist()}
    name = "xl/worksheets/sheet1.xml"
    xml = items[name].decode("utf-8")
    xml, n = re.subn(rf'(<c r="{ref}"[^>]*>\s*<f>[^<]*</f>)\s*(<v>\s*</v>|<v/>)?', rf"\1<v>{value}</v>", xml)
    assert n == 1
    items[name] = xml.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n_, data in items.items():
            z.writestr(n_, data)


def test_evaluator_memoizes_and_handles_long_running_totals():
    cells: dict[str, object] = {f"A{i}": 1 for i in range(1, 201)}
    cells["B1"] = "=A1"
    cells.update({f"B{i}": f"=B{i - 1}+A{i}" for i in range(2, 201)})
    states = _states(_workbook(cells))
    assert states["B200"].derived_value == Decimal("200")


def test_display_scale():
    assert fx.display_scale("#,##0.00") == 2
    assert fx.display_scale("#,##0.00;[Red](#,##0.00);-") == 2
    assert fx.display_scale("0") == 0
    assert fx.display_scale("General") is None
    assert fx.display_scale("0.0%") is None
    assert fx.display_scale('#,##0.000 "руб."') == 3


def test_a_formula_that_only_shows_text_or_a_boolean_is_unresolved_not_an_error():
    states = _states(_workbook({"A1": "Итого", "A2": True, "A3": "1000", "B1": "=A1", "B2": "=A2", "B3": "=A3"}))
    assert states["B1"].evaluation == "unresolved" and "text" in states["B1"].error
    assert states["B2"].evaluation == "unresolved" and "boolean" in states["B2"].error
    assert states["B3"].derived_value == Decimal("1000")  # numeric text still reads as its number
