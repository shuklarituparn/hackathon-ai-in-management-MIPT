"""PR25 regressions: complete money units and scoped contract identity."""

from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest

from sverka import contracts as c
from sverka.reconciliation import reconcile
from tests import recon_fixtures as f
from tests.test_reconciliation_money_regressions import entry, indicator, run_document

D = Decimal


@pytest.mark.parametrize("source", ["primary", "report"])
@pytest.mark.parametrize(
    ("unit", "amount"),
    [
        ("руб., с НДС", "1000"),
        ("руб. (без НДС)", "1000"),
        ("RUB (₽)", "1000"),
        ("в рублях и копейках", "1000"),
        ("коп.", "100000"),
        ("тысяч рублей", "1"),
        ("млн. руб.", "0.001"),
    ],
)
def test_complete_supported_units_keep_their_ruble_scale(source, unit, amount):
    result, _ = run_document(
        amount if source == "primary" else "1000",
        [
            entry(
                "material",
                "materials",
                amount if source == "report" else "1000",
                unit=unit if source == "report" else "руб.",
            )
        ],
        unit=unit if source == "primary" else "руб.",
    )

    material = indicator(result, "material")
    assert material.status == "verified"
    assert material.reported_value == material.observed_sum == material.expected_value == D("1000.00")
    assert material.diff == 0
    assert [contribution.amount for contribution in material.contributions] == [D("1000.00")]
    assert result.unresolved == []


@pytest.mark.parametrize("source", ["primary", "report"])
@pytest.mark.parametrize(
    "unit",
    [
        "руб., USD",
        "руб. (USD)",
        "руб. (тыс. руб.)",
        "тыс. (руб.)",
        "руб./1000",
        "руб. (1000)",
        "руб. 1000",
        "руб. €",
        "руб. / м²",
        "руб., неизвестно",
    ],
)
def test_unsupported_unit_suffix_cannot_be_discarded(source, unit):
    result, _ = run_document(
        "1000",
        [entry("material", "materials", "1000", unit=unit if source == "report" else "руб.")],
        unit=unit if source == "primary" else "руб.",
    )

    material = indicator(result, "material")
    assert material.status == "insufficient_evidence"
    assert material.problem == "unresolved_interpretation"
    assert material.expected_value is None and material.diff is None
    if source == "primary":
        assert material.observed_sum is None and material.contributions == []
        assert [document.version_id for document in result.unresolved] == ["ver_money"]
    else:
        assert material.observed_sum == D("1000.00")
        assert [contribution.amount for contribution in material.contributions] == [D("1000.00")]
        assert any(gap.code == "unsupported_report_unit" for gap in material.gaps)


@pytest.mark.parametrize("currency", ["RUB, USD", "RUB (USD)"])
@pytest.mark.parametrize("unit", ["руб.", None])
def test_currency_suffix_cannot_supply_implicit_rubles(currency, unit):
    result, _ = run_document("1000", [entry("material", "materials", "1000")], unit=unit, currency=currency)

    material = indicator(result, "material")
    assert material.status == "insufficient_evidence"
    assert material.problem == "unresolved_interpretation"
    assert material.contributions == [] and material.observed_sum is None
    assert material.expected_value is None and material.diff is None
    assert [document.version_id for document in result.unresolved] == ["ver_money"]


def test_currency_symbol_annotation_still_identifies_rubles():
    result, _ = run_document("1000", [entry("material", "materials", "1000")], currency="RUB (₽)")
    material = indicator(result, "material")
    assert material.status == "verified" and material.expected_value == D("1000.00")
    assert material.diff == 0 and result.unresolved == []


def _act(key: str, contract: str, *, amount="245000.00", category=f.PROD, period=None) -> c.PrimaryExtraction:
    return f.primary(
        key,
        doc_type="Акт",
        number="118",
        issue=date(2026, 1, 28),
        period=period or f.month(1),
        counterparty="ООО «ЛабСервис Альфа»",
        contract=contract,
        amount=amount,
        lines=[(category, amount, (), None)],
    )


def _run_acts(extractions, *, reported="245000.00", with_admin=False, scope_contract=f.CONTRACT):
    items = [f.Item(ext.version_id.removeprefix("ver_"), "primary", ext) for ext in extractions]
    scope, _, primaries, manifest = f.bundle(items)
    scope = scope.model_copy(update={"contract": scope_contract})
    # Different bytes force the semantic identity check rather than the SHA deduplicator.
    hashes = [item.version.sha256 for item in manifest.entries if item.version.role == "primary"]
    assert len(set(hashes)) == len(hashes)
    indicators = [entry("production", f.PROD, reported)]
    if with_admin:
        indicators.append(entry("admin", f.ADMIN, "300000"))
    report = c.ReportExtraction(version_id="ver_kfz", row_label=f.ROW, indicators=indicators)
    result = reconcile(scope, report, primaries, manifest, monthly_roots=())
    return c.ReconciliationResult.model_validate(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "contract",
    [
        "24 / 03 / 15 - 01",
        "№\u00a024\u00a0/\u00a003\u00a0/\u00a015\u00a0–\u00a001 от 09.04.2024",
        "договор 24 /03/ 15 − 01, этап 3",
        "по договору — 24 / 03 / 15 - 01 от 09.04.2024",
    ],
)
def test_contract_spacing_and_unicode_separators_preserve_the_identifier(contract):
    result = _run_acts([_act("act118", contract)])
    production = indicator(result, "production")
    assert production.status == "verified"
    assert production.observed_sum == production.expected_value == D("245000.00")
    assert production.diff == 0
    assert [contribution.version_id for contribution in production.contributions] == ["ver_act118"]
    assert result.unresolved == []


@pytest.mark.parametrize(
    "contract",
    [
        "24/03/15-01-доп",
        "24 / 03 / 15 - 01-доп",
        "24 / 03 / 15 - 01 - доп",
        "99 / 24 / 03 / 15 - 01",
        "ABC / 24 / 03 / 15 - 01",
        "99 - 24 / 03 / 15 - 01",
        "ABC - 24 / 03 / 15 - 01",
        "prefix24/03/15-01",
        "prefix24 / 03 / 15 - 01",
    ],
)
def test_longer_contract_identifier_stays_unresolved_instead_of_counted(contract):
    result = _run_acts([_act("act118", contract)])
    production = indicator(result, "production")
    assert production.status == "insufficient_evidence"
    assert production.problem == "unresolved_interpretation"
    assert production.contributions == [] and production.observed_sum is None
    assert production.expected_value is None and production.diff is None
    assert [document.version_id for document in result.unresolved] == ["ver_act118"]
    assert "ver_act118" not in {exclusion.version_id for exclusion in result.exclusions}


DECORATED = ("№ 24/03/15-01 от 09.04.2024", "договор 24/03/15-01, этап 3")


@pytest.mark.parametrize("references", [(DECORATED[0], DECORATED[0]), DECORATED], ids=["same-text", "different-text"])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["original-first", "copy-first"])
def test_decorated_contract_references_share_the_scoped_duplicate_identity(references, order):
    versions = [_act("act118", references[0]), _act("act118scan", references[1])]
    result = _run_acts([versions[index] for index in order])

    production = indicator(result, "production")
    assert production.status == "verified"
    assert production.observed_sum == production.expected_value == D("245000.00")
    assert production.diff == 0 and len(production.contributions) == 1
    kept = production.contributions[0].version_id
    duplicates = [exclusion for exclusion in result.exclusions if exclusion.reason == "duplicate"]
    assert len(duplicates) == 1
    assert {kept, duplicates[0].version_id} == {"ver_act118", "ver_act118scan"}
    assert duplicates[0].duplicate_of == kept and duplicates[0].amount == D("245000.00")
    assert result.unresolved == []


@pytest.mark.parametrize("order", list(permutations("abc")), ids=lambda order: "".join(order))
def test_decorated_duplicate_pair_cannot_hide_a_conflicting_third_version(order):
    versions = {
        "a": _act("a", DECORATED[0]),
        "b": _act("b", DECORATED[1]),
        "c": _act("c", "контракт № 24/03/15-01 от 09.04.2024", amount="300000", category=f.ADMIN),
    }
    result = _run_acts([versions[key] for key in order], with_admin=True)

    assert {document.version_id for document in result.unresolved} == {"ver_a", "ver_b", "ver_c"}
    assert not {"ver_a", "ver_b", "ver_c"}.intersection(exclusion.version_id for exclusion in result.exclusions)
    for item in result.indicators:
        assert item.status == "insufficient_evidence" and item.problem == "conflicting_versions"
        assert item.contributions == [] and item.observed_sum is None
        assert item.expected_value is None and item.diff is None


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_decorated_references_with_distinct_stated_periods_are_separate_documents(order):
    versions = [_act("jan", DECORATED[0], period=f.month(1)), _act("feb", DECORATED[1], period=f.month(2))]
    result = _run_acts([versions[index] for index in order], reported="490000")

    production = indicator(result, "production")
    assert production.status == "verified"
    assert production.observed_sum == production.expected_value == D("490000.00")
    assert production.diff == 0
    assert {contribution.version_id for contribution in production.contributions} == {"ver_jan", "ver_feb"}
    assert not any(exclusion.reason == "duplicate" for exclusion in result.exclusions)
    assert result.unresolved == []
