"""T10 reconciliation tests on typed case19 fixtures: V01, V02, V05, V06, V07 and the D01-D06 regressions."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
import recon_fixtures as f

from sverka import contracts as c
from sverka.reconciliation import reconcile

D = Decimal
V, X, U = "verified", "discrepancy", "insufficient_evidence"

# docs/planning/05-validation.md, baseline expected results: entry -> (status, complete expected value or None)
ORACLE = {
    "col3": (U, None),
    "col4": (V, "0.00"),
    "col5": (V, "5558474.83"),
    "col6": (U, None),
    "col7": (V, "5054470.26"),
    "col8": (U, None),
    "col9": (U, None),
    "col10": (V, "1671359.96"),
    "col11": (U, None),
    "col12": (U, None),
    "col13": (U, None),
    "col14": (U, None),
    "col15": (X, "1133298.10"),
    "col16": (V, "3204744.51"),
    "col17": (X, "11567877.40"),
}


def run(primaries=None, **kwargs) -> c.ReconciliationResult:
    result = reconcile(*f.bundle(primaries, **kwargs))
    c.ReconciliationResult.model_validate(result.model_dump(mode="json"))  # survives a JSON round trip
    return result


def ind(result, entry_id) -> c.IndicatorResult:
    return next(i for i in result.indicators if i.entry_id == entry_id)


def extra(key, extraction=None, **kw) -> f.Item:
    return f.Item(key, "primary", extraction, **kw)


def prod(key, amount, **kw) -> f.Item:
    kw.setdefault("issue", date(2026, 3, 15))
    return extra(
        key, f.primary(key, doc_type="Акт", number=key.upper(), amount=amount, lines=[(f.PROD, amount, (), None)], **kw)
    )


# ======================= V01: complete baseline =======================


@pytest.fixture(scope="module")
def baseline():
    return run()


@pytest.mark.parametrize("entry_id", sorted(ORACLE, key=lambda e: int(e[3:])))
def test_v01_every_indicator_matches_the_oracle(baseline, entry_id):
    status, expected = ORACLE[entry_id]
    result = ind(baseline, entry_id)
    reported = next(v for e, *_, v in f.INDICATORS if e == entry_id)
    assert result.status == status
    assert result.reported_value == D(reported)
    assert result.expected_value == (D(expected) if expected else None)
    if status == U:
        assert result.diff is None and result.problem


def test_v01_overhead_discrepancy_has_four_sources(baseline):
    overhead = ind(baseline, "col15")
    assert sorted(x.amount for x in overhead.contributions) == [
        D("245000.00"),
        D("270000.00"),
        D("300000.00"),
        D("318298.10"),
    ]
    assert overhead.diff == D("12500.00") and overhead.problem == "amount_mismatch"
    assert all(x.evidence_ids for x in overhead.contributions)
    assert "по дате документа" in overhead.explanation  # issue-date rule is recorded (policy 2)


def test_v01_total_is_the_same_error_not_a_second_one(baseline):
    total = ind(baseline, "col17")
    assert total.diff == D("12500.00")
    conclusion = baseline.conclusion
    assert [d.entry_id for d in conclusion.component_discrepancies] == ["col15"]
    assert conclusion.grand_total.explained_by == ["col15"] and conclusion.grand_total.unexplained_diff == D("0.00")
    assert sum(d.diff for d in conclusion.component_discrepancies) == D("12500.00")  # not 25 000
    assert conclusion.incomplete_indicators == ["col3", "col6", "col8", "col9", "col11", "col12", "col13", "col14"]


def test_v01_exclusions_with_reasons(baseline):
    reasons = {e.version_id: (e.reason, e.duplicate_of) for e in baseline.exclusions}
    assert reasons == {
        "ver_adm27copy": ("duplicate", "ver_adm27"),
        "ver_org0603": ("outside_period", None),
        "ver_adm75": ("other_contract", None),
        "ver_readme": ("context_only", None),
    }
    admin = ind(baseline, "col16")
    assert {e.version_id for e in admin.exclusions} == {"ver_adm27copy", "ver_org0603", "ver_adm75"}
    assert len(admin.contributions) == 5 and baseline.unresolved == []


def test_v01_zero_is_verified_only_in_closed_scope(baseline):
    materials = ind(baseline, "col4")
    assert materials.status == V and materials.observed_sum == D("0.00") and materials.contributions == []


def test_v01_payroll_subtotals_are_not_counted_twice(baseline):
    payroll = ind(baseline, "col5")
    assert len(payroll.contributions) == 10  # 0201 and 0202 per month; the five ИТОГО lines are not counted
    assert "Итоговые строки" in payroll.explanation
    assert ind(baseline, "col7").status == V
    for split in ("col6", "col8", "col9"):  # 0202 combines additional payroll and leave reserve
        assert any(g.code == "combined_line_not_split" for g in ind(baseline, split).gaps)


def test_v01_unsupported_details_are_insufficient_not_errors(baseline):
    for entry_id in ("col3", "col11", "col12", "col13", "col14"):
        result = ind(baseline, entry_id)
        assert result.status == U and result.problem == "no_supporting_document"


# ======================= V02: March payroll missing =======================


def test_v02_missing_march_keeps_partial_sums_and_the_known_error():
    result = run(f.without("pay03"))
    payroll = ind(result, "col5")
    assert payroll.status == U and payroll.coverage == "partial" and payroll.problem == "missing_coverage"
    assert payroll.observed_sum == D("4456674.83") and payroll.expected_value is None and payroll.diff is None
    assert any(g.code == "missing_months" and "март 2026" in g.message for g in payroll.gaps)
    total = ind(result, "col17")
    assert total.status == U and total.expected_value is None and total.diff is None
    assert total.observed_sum == D("10466077.40")
    assert ind(result, "col15").diff == D("12500.00")
    conclusion = result.conclusion
    assert [d.entry_id for d in conclusion.component_discrepancies] == ["col15"]
    assert conclusion.grand_total.status == U and conclusion.grand_total.diff is None
    assert "Подтвержденные расхождения" in conclusion.summary
    assert ind(result, "col10").status == V  # March contributions are still there


def test_v02_adding_the_statement_back_restores_verification():
    assert ind(run(f.without("pay03")), "col5").status == U
    assert ind(run(), "col5").status == V


# ======================= V05: observations are preserved =======================


def test_v05_typed_350_is_kept_and_arithmetic_is_a_finding():
    result = run(report_overrides={"col13": "350.00"})
    assert ind(result, "col13").reported_value == D("350.00")  # never "repaired" to 550 or anything else
    subtotal = ind(result, "col11")
    assert subtotal.reported_value == D("1578126.37")
    assert any(g.code == "report_arithmetic" for g in subtotal.gaps)
    assert not any(g.code == "report_arithmetic" for g in ind(run(), "col11").gaps)


def test_v05_literal_1000_stays_1000():
    material = extra(
        "mat",
        f.primary(
            "mat",
            doc_type="Накладная",
            number="M-1",
            issue=date(2026, 2, 2),
            amount="1000.00",
            lines=[("materials", "1000.00", (), None)],
        ),
    )
    result = run(f.plus(material), report_overrides={"col4": "1000"})
    materials = ind(result, "col4")
    dumped = materials.model_dump(mode="json")
    assert dumped["reported_value"] == "1000" and dumped["expected_value"] == "1000.00"
    assert materials.status == V and materials.diff == 0


def test_v05_unreadable_report_value_is_not_zero():
    result = run(report_overrides={"col16": None})
    admin = ind(result, "col16")
    assert admin.reported_value is None and admin.status == U and admin.problem == "unreadable_report_value"
    assert admin.expected_value == D("3204744.51") and admin.diff is None


# ======================= V06: acceptance and coverage before equality =======================


def test_v06_unaccepted_category_is_not_counted_even_when_it_would_match():
    guess = extra(
        "guess",
        f.primary(
            "guess",
            doc_type="Акт",
            number="G-1",
            issue=date(2026, 3, 3),
            amount="12500.00",
            lines=[(None, "12500.00", (f.PROD,), None)],
        ),
    )
    overhead = ind(run(f.plus(guess)), "col15")
    assert overhead.status == U and overhead.problem == "unresolved_interpretation"
    assert overhead.observed_sum == D("1133298.10")  # the matching 12 500 is not added


def test_v06_one_matching_month_is_incomplete():
    only_january = [i for i in f.items() if not i.key.startswith("pay") or i.key == "pay01"]
    payroll = ind(run(only_january, report_overrides={"col5": "1110000.00"}), "col5")
    assert payroll.observed_sum == D("1110000.00")
    assert payroll.status == U and payroll.problem == "missing_coverage"


def test_v06_unknown_relevance_blocks_zero_and_every_category():
    unreadable = extra("scan", None, preparation="failed")
    result = run(f.plus(unreadable))
    for entry_id in ("col4", "col15", "col16", "col17"):
        assert ind(result, entry_id).status == U
    assert ind(result, "col4").problem == "unreadable_input"
    assert result.unresolved[0].version_id == "ver_scan"


def test_v06_possible_materials_makes_zero_unverified():
    maybe = extra(
        "maybe",
        f.primary(
            "maybe",
            doc_type="Накладная",
            number="N-7",
            issue=date(2026, 4, 4),
            amount="5000.00",
            lines=[(None, "5000.00", ("materials", f.PROD), None)],
        ),
    )
    result = run(f.plus(maybe))
    assert ind(result, "col4").status == U
    assert ind(result, "col16").status == V  # not plausible for administrative overhead


def test_v06_cross_contract_lookalike_does_not_suppress_the_valid_document():
    twin = extra(
        "sa174other",
        f.overhead(
            "sa174other",
            "Счет-акт",
            "174",
            date(2026, 2, 25),
            "318298.10",
            f.PROD,
            "ООО «ИнжКонтур Бета»",
            contract="24/03/16-02",
        ),
    )
    result = run(f.plus(twin))
    assert ind(result, "col15").observed_sum == D("1133298.10")
    assert {"ver_sa174"} <= {x.version_id for x in ind(result, "col15").contributions}
    assert next(e for e in result.exclusions if e.version_id == "ver_sa174other").reason == "other_contract"


def test_v06_same_document_in_other_bytes_counts_once():
    scan = extra(
        "act118scan",
        f.overhead(
            "act118scan",
            "Акт",
            "118",
            date(2026, 1, 28),
            "245000.00",
            f.PROD,
            "ООО «ЛабСервис Альфа»",
            period=f.month(1),
        ),
    )
    result = run(f.plus(scan))
    assert ind(result, "col15").observed_sum == D("1133298.10")
    duplicate = next(e for e in result.exclusions if e.version_id == "ver_act118scan")
    assert (duplicate.reason, duplicate.duplicate_of) == ("duplicate", "ver_act118")


@pytest.mark.parametrize("copy_extracted", [True, False])
def test_v06_discrepancy_equal_to_an_identical_copy_is_named_a_duplicate(copy_extracted):
    # The report counted the ADM-27 copy as well: administrative overhead and the total are 487 500 too high.
    items = (
        f.items() if copy_extracted else [replace(i, extraction=None) if i.key == "adm27copy" else i for i in f.items()]
    )
    result = run(items, report_overrides={"col16": "3692244.51", "col17": "12067877.40"})
    copy = next(e for e in result.exclusions if e.version_id == "ver_adm27copy")
    assert copy.amount == D("487500.00") and "adm27copy.pdf" in copy.label
    admin = ind(result, "col16")
    assert (admin.status, admin.problem, admin.diff) == (X, "duplicate", D("487500.00"))
    assert "ver_adm27copy" in {e.version_id for e in admin.exclusions}


def test_v06_reused_number_with_separate_stated_periods_is_not_a_conflict():
    # Every monthly payroll statement is numbered "1"; their stated periods do not overlap.
    items = [
        replace(i, extraction=f.payroll(int(i.key[3:]), number="1", key=i.key)) if i.key.startswith("pay") else i
        for i in f.items()
    ]
    result = run(items)
    assert ind(result, "col5").status == V and ind(result, "col7").status == V
    assert result.unresolved == []


@pytest.mark.parametrize("contract", ["№ 24/03/15-01 от 09.04.2024", "договор 24/03/15-01, этап 3"])
def test_v06_contract_written_with_extra_text_is_still_this_contract(contract):
    act = f.overhead("sa174", "Счет-акт", "174", date(2026, 2, 25), "318298.10", f.PROD, "ООО «ИнжКонтур Бета»")
    act = act.model_copy(update={"contracts": f.acc(c.TextListObservation, [contract], "sa174", "contract")})
    result = run([replace(i, extraction=act) if i.key == "sa174" else i for i in f.items()])
    overhead = ind(result, "col15")
    assert overhead.observed_sum == D("1133298.10") and len(overhead.contributions) == 4
    assert "ver_sa174" not in {e.version_id for e in result.exclusions}


def test_v06_contract_number_inside_a_longer_one_is_unresolved_not_excluded():
    act = f.overhead("addendum", "Акт", "A-1", date(2026, 3, 1), "12500.00", f.PROD, "ООО «Тест»")
    act = act.model_copy(
        update={"contracts": f.acc(c.TextListObservation, ["24/03/15-01-доп"], "addendum", "contract")}
    )
    result = run(f.plus(extra("addendum", act)))
    assert ind(result, "col15").status == U
    assert "ver_addendum" in {u.version_id for u in result.unresolved}
    assert "ver_addendum" not in {e.version_id for e in result.exclusions}


def test_v06_duplicate_is_found_when_the_copy_writes_the_contract_differently():
    copy = f.overhead(
        "act118b", "Акт", "118", date(2026, 1, 28), "245000.00", f.PROD, "ООО «ЛабСервис Альфа»", period=f.month(1)
    )
    copy = copy.model_copy(update={"contracts": f.acc(c.TextListObservation, ["№ 24/03/15-01"], "act118b", "contract")})
    result = run(f.plus(extra("act118b", copy)))
    assert ind(result, "col15").observed_sum == D("1133298.10")
    assert next(e for e in result.exclusions if e.version_id == "ver_act118b").reason == "duplicate"


def test_v06_same_number_from_another_counterparty_is_a_different_document():
    other = extra(
        "act118b",
        f.overhead("act118b", "Акт", "118", date(2026, 1, 28), "12500.00", f.PROD, "ООО «Другая»"),
    )
    overhead = ind(run(f.plus(other)), "col15")
    assert overhead.status == V and overhead.expected_value == D("1145798.10")


# ======================= V07: periods, allocations, units, versions =======================


def test_v07_explicit_period_beats_the_issue_date():
    late = prod("late", "12500.00", issue=date(2026, 6, 2), period=f.month(5))
    assert ind(run(f.plus(late)), "col15").status == V


def test_v07_issue_date_rule_excludes_outside_scope():
    june = prod("june", "12500.00", issue=date(2026, 6, 1))
    result = run(f.plus(june))
    assert next(e for e in result.exclusions if e.version_id == "ver_june").reason == "outside_period"
    assert ind(result, "col15").diff == D("12500.00")


def test_v07_partial_overlap_is_unresolved_not_prorated():
    straddle = prod("straddle", "12500.00", issue=None, period=(date(2026, 5, 15), date(2026, 6, 15)))
    overhead = ind(run(f.plus(straddle)), "col15")
    assert overhead.status == U and overhead.observed_sum == D("1133298.10")
    assert "частично" in overhead.gaps[0].message


def test_v07_period_unknown_blocks_its_categories():
    undated = prod("undated", "12500.00", issue=None)
    result = run(f.plus(undated))
    assert ind(result, "col15").status == U and ind(result, "col16").status == V


def test_v07_allocations_split_and_unallocated_remainder():
    split = extra(
        "split",
        f.primary(
            "split",
            doc_type="Акт",
            number="S-1",
            issue=date(2026, 3, 1),
            amount="12500.00",
            lines=[(f.PROD, "10000.00", (), None), (f.ADMIN, "2500.00", (), None)],
        ),
    )
    result = run(f.plus(split), report_overrides={"col16": "3207244.51"})
    assert ind(result, "col15").observed_sum == D("1143298.10")
    assert ind(result, "col16").status == V

    partly = extra(
        "partly",
        f.primary(
            "partly",
            doc_type="Акт",
            number="P-1",
            issue=date(2026, 3, 1),
            amount="12500.00",
            lines=[(f.PROD, "10000.00", (), None)],
            possible=[f.ADMIN],
        ),
    )
    result = run(f.plus(partly))
    assert ind(result, "col15").observed_sum == D("1143298.10")
    assert ind(result, "col16").status == U  # the unallocated 2 500 may be administrative
    assert any("не отнесены" in u.reason for u in result.unresolved)


def test_v07_equal_lines_of_one_category_are_all_counted():
    # 200 000 = 100 000 + 100 000 by coincidence: none of these ordinary lines is a subtotal.
    lines = [(f.ADMIN, "200000.00", (), "Услуга А"), (f.ADMIN, "100000.00", (), "Услуга Б")]
    lines.append((f.ADMIN, "100000.00", (), "Услуга В"))
    act = extra(
        "svc3",
        f.primary("svc3", doc_type="Акт", number="X-1", issue=date(2026, 3, 10), amount="400000.00", lines=lines),
    )
    result = run(f.plus(act), report_overrides={"col16": "3604744.51", "col17": "11980377.40"})
    assert ind(result, "col16").status == V and result.unresolved == []
    assert ind(result, "col7").status == V and ind(result, "col4").status == V  # nothing else is blocked


def test_v07_unlabelled_parent_line_equal_to_its_details_is_not_counted_twice():
    main, rest = f.PAYROLL[1]
    total = str(D(main) + D(rest))
    lines = [("payroll_account", total, (), "Начислено по счету 211")]
    lines += [("payroll_main", main, (), "0201"), ("payroll_additional", rest, (), "0202")]
    statement = f.primary(
        "pay01", doc_type="Расчетная ведомость", number="ВЕД-ЗП-2026-01", amount=total, period=f.month(1), lines=lines
    )
    items = [replace(i, extraction=statement) if i.key == "pay01" else i for i in f.items()]
    result = run(items)
    assert ind(result, "col5").status == V and ind(result, "col7").status == V
    assert result.unresolved == []


def test_v07_units_are_scaled_to_rubles():
    thousands = prod("thousands", "12.5", unit="тыс. руб.")
    overhead = ind(run(f.plus(thousands)), "col15")
    assert overhead.status == V and D("12500.00") in [x.amount for x in overhead.contributions]


@pytest.mark.parametrize(
    ("unit", "amount"), [("руб. коп.", "12500.00"), ("в рублях и копейках", "12500.00"), ("коп.", "1250000")]
)
def test_v07_kopecks_scale_only_when_the_unit_is_kopecks(unit, amount):
    overhead = ind(run(f.plus(prod("rubkop", amount, unit=unit))), "col15")
    assert overhead.status == V and D("12500.00") in [x.amount for x in overhead.contributions]


@pytest.mark.parametrize(
    ("unit", "currency", "amount"),
    [
        ("в рублях", None, "12500.00"),
        ("руб. РФ", "RUB", "12500.00"),
        ("руб., с НДС", "RUB", "12500.00"),
        ("руб. (без НДС)", None, "12500.00"),
        ("в тыс. руб.", None, "12.5"),
        ("тысяч рублей", "RUB", "12.5"),
        ("руб.", "рубль РФ", "12500.00"),
        ("руб.", "RUB (₽)", "12500.00"),
    ],
)
def test_v07_common_ruble_wordings_are_recognized(unit, currency, amount):
    overhead = ind(run(f.plus(prod("wording", amount, unit=unit, currency=currency))), "col15")
    assert overhead.status == V and D("12500.00") in [x.amount for x in overhead.contributions]


def test_v07_foreign_currency_stays_unresolved():
    dollars = prod("usd", "12500.00", unit="USD", currency="USD")
    assert ind(run(f.plus(dollars)), "col15").status == U


def test_v07_signs_are_kept():
    credit = prod("credit", "-12500.00")
    overhead = ind(run(f.plus(credit)), "col15")
    assert overhead.observed_sum == D("1120798.10") and overhead.diff == D("25000.00")


def test_v07_rounding_is_half_up():
    tiny = prod("tiny", "2.665")
    assert D("2.67") in [x.amount for x in ind(run(f.plus(tiny)), "col15").contributions]


def test_v07_tolerance_is_configurable():
    within = ind(run(report_overrides={"col16": "3204744.52"}), "col16")
    beyond = ind(run(report_overrides={"col16": "3204744.53"}), "col16")
    assert within.status == V and beyond.status == X
    strict = reconcile(*f.bundle(report_overrides={"col16": "3204744.52"}), tolerance=D("0.00"))
    assert ind(strict, "col16").status == X


def test_v07_conflicting_versions_stay_unresolved():
    other_amount = extra(
        "adm27v2",
        f.overhead("adm27v2", "Акт", "ADM-27", date(2026, 2, 27), "500000.00", f.ADMIN, "ООО «ДокКонтур Эпсилон»"),
    )
    result = run(f.plus(other_amount))
    admin = ind(result, "col16")
    assert admin.status == U and admin.problem == "conflicting_versions"
    assert {u.version_id for u in result.unresolved} == {"ver_adm27", "ver_adm27v2"}


def test_v07_two_statements_for_one_month_are_not_both_counted():
    second = extra("pay01b", f.payroll(1, number="ВЕД-ЗП-2026-01-КОРР", key="pay01b"))
    payroll = ind(run(f.plus(second)), "col5")
    assert payroll.status == U and payroll.problem == "conflicting_versions"


def test_v07_replaced_version_is_superseded():
    newer = extra(
        "act118v2",
        f.overhead(
            "act118v2", "Акт", "118", date(2026, 1, 28), "245000.00", f.PROD, "ООО «ЛабСервис Альфа»", period=f.month(1)
        ),
        predecessor="ver_act118",
    )
    result = run(f.plus(newer))
    old = next(e for e in result.exclusions if e.version_id == "ver_act118")
    assert (old.reason, old.duplicate_of) == ("duplicate", "ver_act118v2")
    assert ind(result, "col15").observed_sum == D("1133298.10")


# ======================= conclusion and robustness =======================


def test_dependent_discrepancies_are_not_double_counted():
    result = run(
        report_overrides={"col7": "5055470.26", "col6": "5249019.04", "col5": "5559474.83", "col17": "11581377.40"}
    )
    listed = [d.entry_id for d in result.conclusion.component_discrepancies]
    assert listed == ["col7", "col15"]  # col5 is the same 1 000 as col7
    grand = result.conclusion.grand_total
    assert grand.diff == D("13500.00") and grand.explained_by == ["col7", "col15"] and grand.unexplained_diff == 0


@pytest.mark.parametrize("row", [f.ROW, "3 этап 2026 г", "3 ЭТАП, 2026 года"])
def test_report_row_spelling_differences_are_not_a_mismatch(row):
    result = run(row=row)
    assert "выбрана" not in result.conclusion.summary
    assert [i.status for i in result.indicators] == [ORACLE[i.entry_id][0] for i in result.indicators]


def test_report_without_printed_units_is_compared_in_rubles_and_says_so():
    # The supplied report prints no unit on its money columns; only column 3 names person-months.
    scope, report, primaries, manifest = f.bundle()
    bare = [e if e.entry_id == "col3" else e.model_copy(update={"unit": None}) for e in report.indicators]
    result = reconcile(scope, report.model_copy(update={"indicators": bare}), primaries, manifest)
    assert [i.status for i in result.indicators] == [ORACLE[i.entry_id][0] for i in result.indicators]
    for i in result.indicators:
        assumed = any(g.code == "report_unit_assumed" for g in i.gaps)
        assert assumed == (i.entry_id != "col3") and (i.unit == "руб.") == assumed
    assert ind(result, "col17").diff == D("12500.00")
    assert result.conclusion.grand_total.explained_by == ["col15"]


def test_report_row_mismatch_blocks_every_comparison():
    result = run(row="2 этап, 2025 г.")
    assert "выбрана" in result.conclusion.summary
    for i in result.indicators:
        assert i.status == U and i.diff is None
        assert any(g.code == "report_row_mismatch" for g in i.gaps)
    assert result.conclusion.component_discrepancies == []
    assert result.conclusion.grand_total.status == U
    assert "строка отчета не совпадает" in result.conclusion.summary
    admin = ind(result, "col16")  # the documents are still summed and shown
    assert admin.expected_value == D("3204744.51") and admin.problem == "unresolved_interpretation"


def test_extraction_for_an_unlisted_version_is_rejected():
    scope, report, primaries, manifest = f.bundle()
    stray = f.overhead("stray", "Акт", "1", date(2026, 3, 1), "1.00", f.PROD, None)
    with pytest.raises(ValueError, match="manifest"):
        reconcile(scope, report, [*primaries, stray], manifest)


def test_reconcile_is_pure():
    scope, report, primaries, manifest = f.bundle()
    before = [p.model_dump() for p in primaries], report.model_dump(), manifest.model_dump()
    assert reconcile(scope, report, primaries, manifest) == reconcile(scope, report, primaries, manifest)
    assert before == ([p.model_dump() for p in primaries], report.model_dump(), manifest.model_dump())


@pytest.mark.parametrize("case", sorted(f.USE_CASES))
def test_published_use_case_results_are_current(case):
    path = f"{f.RESULTS_DIR}/{case}_result.json"
    with open(path, encoding="utf-8") as fh:
        published = fh.read()
    assert published == f.use_case_json(case), "regenerate: python -m tests.recon_fixtures"
    c.ReconciliationResult.model_validate_json(published)
