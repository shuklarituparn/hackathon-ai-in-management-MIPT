"""PR23 regressions for unknown relevance and complete identity conflict groups."""

from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest

from sverka import contracts as c
from sverka.reconciliation import reconcile
from tests import recon_fixtures as f


def _run(extractions: list[c.PrimaryExtraction]) -> c.ReconciliationResult:
    items = [f.Item(ext.version_id.removeprefix("ver_"), "primary", ext) for ext in extractions]
    scope, report, primaries, manifest = f.bundle(items, report_overrides={"col4": "0", "col15": "0", "col16": "0"})
    report_data = report.model_dump()
    report_data["indicators"] = [
        entry for entry in report_data["indicators"] if entry["entry_id"] in {"col4", "col15", "col16"}
    ]
    report = c.ReportExtraction.model_validate(report_data)
    result = reconcile(scope, report, primaries, manifest)
    return c.ReconciliationResult.model_validate(result.model_dump(mode="json"))


def _primary(key: str, category: str, amount: str) -> c.PrimaryExtraction:
    return f.primary(
        key,
        doc_type="Act",
        number="1",
        issue=date(2026, 1, 10),
        counterparty="Supplier",
        amount=amount,
        lines=[(category, amount, (), None)],
    )


@pytest.mark.parametrize(
    ("line_possible", "document_possible", "blocked"),
    [
        ((), (), {"materials", f.PROD, f.ADMIN}),
        ((f.PROD,), (), {f.PROD}),
        ((), (f.PROD,), {f.PROD}),
        ((f.ADMIN,), (), {f.PROD, f.ADMIN}),
    ],
    ids=["globally-unknown", "line-constraint", "document-constraint", "separate-category-constraint"],
)
def test_unknown_contract_preserves_each_allocations_possible_categories(line_possible, document_possible, blocked):
    extraction = f.primary(
        "mixed",
        doc_type="Act",
        number="1",
        issue=date(2026, 1, 10),
        contract=None,
        counterparty="Supplier",
        amount="200",
        lines=[(f.PROD, "100", (), None), (None, "100", line_possible, None)],
        possible=document_possible,
    )

    result = _run([extraction])

    assert [entry.version_id for entry in result.unresolved] == ["ver_mixed"]
    assert not any(entry.version_id == "ver_mixed" for entry in result.exclusions)
    for indicator in result.indicators:
        assert indicator.contributions == []
        if indicator.category in blocked:
            assert indicator.status == "insufficient_evidence"
            assert indicator.problem == "unresolved_interpretation"
            assert indicator.expected_value is None and indicator.diff is None
        else:
            assert indicator.status == "verified"
            assert indicator.expected_value == Decimal("0.00") and indicator.diff == 0


def _assert_whole_group_unresolved(result: c.ReconciliationResult) -> None:
    versions = {"ver_a", "ver_b", "ver_c"}
    assert {entry.version_id for entry in result.unresolved} == versions
    assert not versions.intersection(entry.version_id for entry in result.exclusions)
    for indicator in result.indicators:
        assert indicator.contributions == []
        if indicator.category in {f.PROD, f.ADMIN}:
            assert indicator.status == "insufficient_evidence"
            assert indicator.problem == "conflicting_versions"
            assert indicator.expected_value is None and indicator.diff is None
        else:
            assert indicator.status == "verified"
            assert indicator.expected_value == Decimal("0.00")


@pytest.mark.parametrize("order", list(permutations("abc")), ids=lambda order: "".join(order))
def test_three_conflicting_identity_versions_all_remain_unresolved(order):
    versions = {
        "a": _primary("a", f.PROD, "100"),
        "b": _primary("b", f.PROD, "200"),
        "c": _primary("c", f.ADMIN, "300"),
    }

    _assert_whole_group_unresolved(_run([versions[key] for key in order]))


@pytest.mark.parametrize("order", list(permutations("abc")), ids=lambda order: "".join(order))
def test_identity_duplicate_pair_cannot_hide_a_third_conflicting_version(order):
    versions = {
        "a": _primary("a", f.PROD, "100"),
        "b": _primary("b", f.PROD, "100"),
        "c": _primary("c", f.ADMIN, "300"),
    }

    _assert_whole_group_unresolved(_run([versions[key] for key in order]))
