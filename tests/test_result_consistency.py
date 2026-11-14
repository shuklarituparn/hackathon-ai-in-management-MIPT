"""Regression cases for contradictory or double-counted reconciliation conclusions."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sverka.contracts import ReconciliationResult
from sverka.fakes import load_example


@pytest.fixture
def result_payload():
    return load_example("result").model_dump(mode="json")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "verified", "diff": "0.00", "explained_by": [], "unexplained_diff": "0.00"},
        {"diff": "25000.00", "unexplained_diff": "12500.00"},
    ],
)
def test_grand_total_must_match_its_indicator(result_payload, changes):
    result_payload["conclusion"]["grand_total"].update(changes)
    with pytest.raises(ValidationError, match="grand.total.*match"):
        ReconciliationResult.model_validate(result_payload)


def _remove_total_coverage(payload):
    total = next(i for i in payload["indicators"] if i["entry_id"] == "col17")
    total.update(
        coverage="partial",
        observed_sum="10466077.40",
        expected_value=None,
        diff=None,
        status="insufficient_evidence",
        problem="missing_coverage",
    )
    payload["conclusion"]["incomplete_indicators"].append("col17")


def test_partial_total_cannot_retain_a_confirmed_conclusion(result_payload):
    _remove_total_coverage(result_payload)
    with pytest.raises(ValidationError, match="grand.total.*match"):
        ReconciliationResult.model_validate(result_payload)


def test_partial_total_keeps_the_independent_overhead_discrepancy(result_payload):
    _remove_total_coverage(result_payload)
    result_payload["conclusion"]["grand_total"].update(
        status="insufficient_evidence", diff=None, explained_by=[], unexplained_diff=None
    )
    result = ReconciliationResult.model_validate(result_payload)
    assert result.conclusion.grand_total.diff is None
    assert result.conclusion.component_discrepancies[0].diff == Decimal("12500.00")


def test_repeated_explanation_cannot_create_an_unexplained_difference(result_payload):
    result_payload["conclusion"]["grand_total"].update(explained_by=["col15", "col15"], unexplained_diff="-12500.00")
    with pytest.raises(ValidationError, match="duplicate.*explained_by"):
        ReconciliationResult.model_validate(result_payload)


def test_repeated_component_discrepancy_is_rejected(result_payload):
    discrepancies = result_payload["conclusion"]["component_discrepancies"]
    discrepancies.append(discrepancies[0].copy())
    with pytest.raises(ValidationError, match="duplicate.*component"):
        ReconciliationResult.model_validate(result_payload)


def test_unknown_grand_total_cannot_claim_a_difference(result_payload):
    result_payload["conclusion"]["grand_total"].update(entry_id=None, status="insufficient_evidence")
    with pytest.raises(ValidationError, match="insufficient.*grand.total"):
        ReconciliationResult.model_validate(result_payload)


def test_layout_without_a_grand_total_can_remain_unverified(result_payload):
    result_payload["conclusion"]["grand_total"].update(
        entry_id=None, status="insufficient_evidence", diff=None, explained_by=[], unexplained_diff=None
    )
    result = ReconciliationResult.model_validate(result_payload)
    assert result.conclusion.grand_total.entry_id is None
    assert result.conclusion.component_discrepancies[0].diff == Decimal("12500.00")
