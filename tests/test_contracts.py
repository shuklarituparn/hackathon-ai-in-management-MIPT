"""T01 contract tests: proposed vs accepted fields, decimal strings, null vs zero, locators and invariants."""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from sverka import contracts as c
from sverka.fakes import EXAMPLES, EXAMPLES_DIR, InMemoryStore, fake_reconcile, load_example
from sverka.interfaces import ActiveRunConflict, DocumentStore, RunStore

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
SCOPE = c.Scope(
    contract="24/03/15-01", report_row="3 этап, 2026 г.", date_from="2026-01-01", date_to="2026-05-31", confirmed=True
)


def _read(name):
    with open(os.path.join(EXAMPLES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# ---------- published examples and schema ----------


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_examples_load_and_are_canonical(name):
    filename, typ = EXAMPLES[name]
    obj = load_example(name)
    assert TypeAdapter(typ).dump_python(obj, mode="json") == _read(filename)


def test_published_schema_is_current():
    expected = json.loads(json.dumps(c.json_schema(), sort_keys=True))
    assert _read("schema.json") == expected, "run: python -m sverka.contracts > tests/fixtures/contracts/schema.json"


def test_example_result_matches_oracle_overhead():
    result = load_example("result")
    overhead = next(i for i in result.indicators if i.entry_id == "col15")
    assert sum(x.amount for x in overhead.contributions) == Decimal("1133298.10")
    assert overhead.diff == Decimal("12500.00")
    total = result.conclusion.grand_total
    assert total.explained_by == ["col15"] and total.unexplained_diff == Decimal("0.00")


# ---------- decimal strings ----------


@pytest.mark.parametrize("text", ["0", "0.00", "1000", "1145798.10", "-12500.00", "34.14"])
def test_decimal_strings_round_trip_exactly(text):
    obs = c.DecimalObservation(state="accepted", value=text, evidence_ids=["blk_a"], basis="observed")
    assert obs.value == Decimal(text)
    assert obs.model_dump(mode="json")["value"] == text


def test_literal_1000_stays_1000():
    obs = c.DecimalObservation.model_validate_json(
        '{"state":"accepted","value":"1000","evidence_ids":["blk_a"],"basis":"observed"}'
    )
    assert obs.value == Decimal("1000") and obs.model_dump(mode="json")["value"] == "1000"


@pytest.mark.parametrize(
    "raw",
    [
        "1000",
        "1000.5",
        "true",
        '"1 145 798,10"',
        '"1145798,10"',
        '"1e3"',
        '"NaN"',
        '""',
        '" 1"',
        '"01"',
        '"+5"',
        '"1."',
    ],
)
def test_non_canonical_amounts_are_rejected(raw):
    with pytest.raises(ValidationError):
        c.DecimalObservation.model_validate_json(
            f'{{"state":"accepted","value":{raw},"evidence_ids":["blk_a"],"basis":"observed"}}'
        )


def test_python_float_and_nan_are_rejected():
    for bad in (1000.5, 1000, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValidationError):
            c.Contribution(version_id="ver_a", label="x", amount=bad, category="materials", evidence_ids=["blk_a"])


# ---------- null versus zero; proposed versus accepted ----------


def test_zero_is_an_accepted_value_and_null_is_unresolved():
    zero = c.DecimalObservation(state="accepted", value="0.00", evidence_ids=["blk_a"], basis="observed")
    assert zero.value == Decimal("0") and zero.value is not None
    unresolved = load_example("unresolved_field")
    assert unresolved.state == "unresolved" and unresolved.value is None and unresolved.reason


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "unresolved", "value": "0.00", "reason": "unreadable"},  # unresolved must be null, not zero
        {"state": "unresolved", "value": None},  # needs a reason
        {"state": "accepted", "value": None, "evidence_ids": ["blk_a"], "basis": "observed"},
        {"state": "accepted", "value": "5.00", "basis": "observed"},  # needs evidence
        {"state": "accepted", "value": "5.00", "evidence_ids": ["blk_a"]},  # needs basis
        {"value": "5.00", "evidence_ids": ["blk_a"], "basis": "observed"},  # state is required
        {"state": "maybe", "value": "5.00", "evidence_ids": ["blk_a"], "basis": "observed"},
    ],
)
def test_invalid_observations(payload):
    with pytest.raises(ValidationError):
        c.DecimalObservation.model_validate(payload)


def test_proposal_has_no_acceptance_flag():
    assert not {"state", "accepted"} & set(c.ProposedField.model_fields)
    c.ProposedField(value="1145798.10", source_text="1 145 798,10", evidence_ids=["blk_a"])
    for extra in ({"state": "accepted"}, {"accepted": True}):
        with pytest.raises(ValidationError):
            c.ProposedField.model_validate({"value": "1", "evidence_ids": ["blk_a"], **extra})
    with pytest.raises(ValidationError):
        c.ProposedField(value=None)  # an empty proposal must say why


def test_validated_extraction_is_not_a_proposal():
    data = _read("primary.json")
    TypeAdapter(c.ExtractionResult).validate_python(data)
    with pytest.raises(ValidationError):
        TypeAdapter(c.ExtractionProposal).validate_python(data)


# ---------- evidence locators ----------


def _region(**kw):
    base = dict(
        kind="page_region",
        page=1,
        render_id="art_p1",
        box={"x0": 10, "y0": 10, "x1": 50, "y1": 30},
        image_width=100,
        image_height=100,
    )
    return {**base, **kw}


def test_region_helper_is_valid():
    TypeAdapter(c.Locator).validate_python(_region())  # so the invalid cases below fail for their stated reason


@pytest.mark.parametrize(
    "locator",
    [
        _region(page=0),
        _region(box={"x0": 10, "y0": 10, "x1": 150, "y1": 30}),  # outside the render
        _region(box={"x0": 50, "y0": 10, "x1": 10, "y1": 30}),  # inverted
        _region(box={"x0": 10, "y0": 10, "x1": 10, "y1": 30}),  # empty
        _region(render_id="ver_p1"),  # wrong ID kind
        _region(transform={"rotation": 45}),
        {"kind": "cell", "sheet": "S", "ref": "C", "evaluation": "literal"},
        {"kind": "cell", "sheet": "S", "ref": "c6", "evaluation": "literal"},
        {"kind": "cell", "sheet": "S", "ref": "C6", "formula": "=1", "evaluation": "literal"},
        {"kind": "cell", "sheet": "S", "ref": "C8", "formula": "=SUM(C6:C7)", "evaluation": "derived"},
        {
            "kind": "cell",
            "sheet": "S",
            "ref": "C8",
            "formula": "=X()",
            "derived_value": "0",
            "evaluation": "unresolved",
        },
        {"kind": "cell", "sheet": "S", "ref": "C8", "evaluation": "derived", "derived_value": "1"},  # no formula
        {"kind": "docx", "quote": "703 244,51"},
        {"kind": "docx", "quote": "703 244,51", "paragraph": 1, "table": 0, "row": 1, "cell": 1},
        {"kind": "docx", "quote": "703 244,51", "table": 0, "row": 1},
        {"kind": "docx", "quote": "", "paragraph": 1},
        {"kind": "pdf", "page": 1},
    ],
)
def test_invalid_locators_are_rejected(locator):
    with pytest.raises(ValidationError):
        TypeAdapter(c.Locator).validate_python(locator)


def test_valid_locators_cover_all_formats():
    kinds = {b.locator.kind for b in load_example("evidence_blocks")}
    assert kinds == {"page_region", "cell", "docx"}
    formula = next(b for b in load_example("evidence_blocks") if b.block_id == "blk_pay01_c8").locator
    assert formula.cached_value is None and formula.derived_value == Decimal("1110000") and formula.inputs


def test_method_must_fit_locator():
    with pytest.raises(ValidationError):
        c.EvidenceBlock(
            version_id="ver_a",
            block_id="blk_a",
            text="1",
            method="ocr",
            locator={"kind": "cell", "sheet": "S", "ref": "A1", "evaluation": "literal"},
        )


def _version(vid="ver_a"):
    return c.DocumentVersion(
        workspace_id="ws_a",
        document_id="doc_a",
        version_id=vid,
        sha256="0" * 64,
        size_bytes=1,
        role="primary",
        original_filename="a.pdf",
        format="pdf",
        original_artifact_id="art_orig",
        created_at=NOW,
    )


def test_prepared_document_checks_versions_and_renders():
    page = c.PageImage(page=1, render_id="art_p1", width=100, height=100)
    block = c.EvidenceBlock(version_id="ver_a", block_id="blk_a", text="x", method="native_text", locator=_region())
    c.PreparedDocument(version=_version(), blocks=[block], pages=[page])
    with pytest.raises(ValidationError):  # block from another version
        c.PreparedDocument(version=_version("ver_b"), blocks=[block], pages=[page])
    with pytest.raises(ValidationError):  # duplicate block IDs
        c.PreparedDocument(version=_version(), blocks=[block, block], pages=[page])
    with pytest.raises(ValidationError):  # render not listed
        c.PreparedDocument(version=_version(), blocks=[block], pages=[])


# ---------- reconciliation invariants ----------


def _indicator(**kw):
    base = dict(
        entry_id="col5",
        label="Оплата труда",
        reported_value="5558474.83",
        coverage="complete",
        expected_value="5558474.83",
        observed_sum="5558474.83",
        diff="0.00",
        status="verified",
        explanation="ok",
    )
    return {**base, **kw}


def test_partial_coverage_keeps_expected_and_diff_null():
    partial = c.IndicatorResult.model_validate(
        _indicator(
            coverage="partial",
            observed_sum="4456674.83",
            expected_value=None,
            diff=None,
            status="insufficient_evidence",
            problem="missing_coverage",
        )
    )
    assert partial.observed_sum == Decimal("4456674.83") and partial.expected_value is None


@pytest.mark.parametrize(
    "change",
    [
        dict(coverage="partial"),  # expected_value with partial coverage
        dict(diff="1.00"),  # diff != reported - expected
        dict(status="insufficient_evidence", problem="missing_coverage"),  # insufficient with a diff
        dict(problem="amount_mismatch"),  # verified with a problem
        dict(status="discrepancy", reported_value="5558475.83", diff="1.00", problem="missing_coverage"),
        dict(status="discrepancy", reported_value="5558475.83", diff="1.00"),  # discrepancy without a problem
        dict(reported_value=None, diff=None),  # verified without a diff
        dict(observed_sum=5558474.83),  # float
    ],
)
def test_indicator_invariants(change):
    with pytest.raises(ValidationError):
        c.IndicatorResult.model_validate(_indicator(**change))


def test_result_invariants():
    good = load_example("result").model_dump(mode="json")

    def broken(mutate):
        data = json.loads(json.dumps(good))
        mutate(data)
        with pytest.raises(ValidationError):
            c.ReconciliationResult.model_validate(data)

    def verified_over_tolerance(d):
        col16 = next(i for i in d["indicators"] if i["entry_id"] == "col16")
        col16.update(reported_value="3204744.61", diff="0.10")

    broken(verified_over_tolerance)
    broken(lambda d: d["conclusion"]["grand_total"].update(unexplained_diff="12500.00"))
    broken(lambda d: d["conclusion"]["component_discrepancies"][0].update(diff="25000.00"))
    broken(lambda d: d["conclusion"].update(incomplete_indicators=["col15"]))
    broken(lambda d: d["indicators"].append(d["indicators"][0]))


# ---------- tool results, runs and chat ----------


def test_tool_result_envelope():
    failed = c.error_result(c.ToolFailure("timeout", "OCR took too long", retryable=True))
    assert not failed.ok and failed.data is None and failed.error.code == "timeout"
    assert c.ok_result({"x": 1}).ok
    for payload in (
        {"ok": True, "error": {"code": "timeout", "message": "t"}},
        {"ok": False},
        {"ok": False, "data": {}, "error": {"code": "timeout", "message": "t"}},
        {"ok": False, "error": {"code": "crashed", "message": "t"}},
    ):
        with pytest.raises(ValidationError):
            c.ToolResult[dict].model_validate(payload)


def _run(**kw):
    base = dict(
        workspace_id="ws_a",
        run_id="run_a",
        input_version_ids=["ver_a"],
        scope=SCOPE,
        status="running",
        stage="extract",
        created_at=NOW,
        updated_at=NOW,
    )
    return {**base, **kw}


@pytest.mark.parametrize(
    "change",
    [
        dict(status="completed"),  # no result
        dict(scope={**SCOPE.model_dump(mode="json"), "confirmed": False}),
        dict(status="failed"),  # no failure
        dict(status="cancelled", failure={"code": "timeout", "message": "t"}),
        dict(input_version_ids=[]),
    ],
)
def test_run_invariants(change):
    with pytest.raises(ValidationError):
        c.Run.model_validate(_run(**change))


def test_completed_run_with_result():
    run = c.Run.model_validate(_run(status="completed", stage="save", result=load_example("result")))
    assert run.result.conclusion.grand_total.diff == Decimal("12500.00")


def test_chat_cards_are_typed_and_tied_to_the_run():
    result = load_example("result")
    card = c.IndicatorCard(run_id="run_a", indicator=result.indicators[0])
    c.ChatMessage(
        workspace_id="ws_a",
        message_id="msg_a",
        run_id="run_a",
        role="assistant",
        text="...",
        cards=[card],
        created_at=NOW,
    )
    with pytest.raises(ValidationError):
        c.ChatMessage(
            workspace_id="ws_a",
            message_id="msg_a",
            run_id="run_b",
            role="assistant",
            text="...",
            cards=[card],
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        c.ChatMessage(
            workspace_id="ws_a",
            message_id="msg_a",
            run_id="run_a",
            role="user",
            text="...",
            cards=[card],
            created_at=NOW,
        )


def test_ids_are_typed():
    assert c.new_id("ver").startswith("ver_")
    with pytest.raises(ValueError):
        c.new_id("xyz")
    with pytest.raises(ValidationError):
        c.Exclusion(version_id="blk_a", label="x", reason="duplicate", explanation="x")


# ---------- fakes ----------


def test_in_memory_store_versions_and_scoping():
    store = InMemoryStore()
    assert isinstance(store, DocumentStore) and isinstance(store, RunStore)
    ws, other = store.create_workspace(), store.create_workspace()
    first = store.save_document(ws.workspace_id, filename="Акт.pdf", role="primary", data=b"one")
    second = store.save_document(ws.workspace_id, filename="Акт.pdf", role="primary", data=b"two")
    assert first.version_id != second.version_id and first.document_id != second.document_id
    assert store.read_original(ws.workspace_id, first.version_id) == b"one"
    replaced = store.save_document(
        ws.workspace_id, filename="Акт.pdf", role="primary", data=b"three", document_id=first.document_id
    )
    assert replaced.predecessor_version_id == first.version_id
    assert store.read_original(ws.workspace_id, first.version_id) == b"one"  # old version still readable

    sneaky = store.save_document(ws.workspace_id, filename="../../etc/passwd", role="context", data=b"x")
    assert sneaky.original_filename == "passwd" and sneaky.format == "other"

    with pytest.raises(c.ToolFailure) as err:
        store.get_version(other.workspace_id, first.version_id)
    assert err.value.error.code == "not_found"

    block = c.EvidenceBlock(
        version_id=first.version_id,
        block_id="blk_one",
        text="one",
        method="docx",
        locator={"kind": "docx", "quote": "one", "paragraph": 0},
    )
    store.save_blocks(ws.workspace_id, [block])
    assert store.get_block(ws.workspace_id, "blk_one") == block
    with pytest.raises(c.ToolFailure) as err:
        store.get_block(other.workspace_id, "blk_one")
    assert err.value.error.code == "not_found"
    with pytest.raises(c.ToolFailure) as err:
        store.save_blocks(other.workspace_id, [block])  # version belongs to another workspace
    assert err.value.error.code == "invalid_reference"


def test_in_memory_runs_allow_one_active_check_and_ignore_late_completion():
    store = InMemoryStore()
    ws = store.create_workspace().workspace_id
    v = store.save_document(ws, filename="r.pdf", role="report", data=b"r")
    request = c.StartRunRequest(version_ids=[v.version_id], scope=SCOPE)
    run = store.create_run(ws, request)
    with pytest.raises(ActiveRunConflict):
        store.create_run(ws, request)
    store.save_run(run.model_copy(update={"status": "cancelled"}))
    late = c.Run.model_validate(run.model_dump() | {"status": "completed", "result": load_example("result")})
    with pytest.raises(c.ToolFailure):
        store.save_run(late)
    assert store.get_run(ws, run.run_id).status == "cancelled"
    with pytest.raises(c.ToolFailure):
        store.create_run(ws, c.StartRunRequest(version_ids=["ver_unknown"], scope=SCOPE))


def test_fake_reconcile_returns_example_for_scope():
    report = c.ReportExtraction(
        version_id="ver_a", row_label="3 этап, 2026 г.", indicators=[load_example("report_field")]
    )
    manifest = c.BundleManifest(workspace_id="ws_a", entries=[])
    scope = SCOPE.model_copy(update={"report_row": "другая строка"})
    assert fake_reconcile(scope, report, [load_example("primary")], manifest).scope == scope


# ---------- skeleton routes ----------


def test_health_reports_configuration_without_credentials(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from sverka import config
    from sverka.web import app

    monkeypatch.setattr(config, "LLM_URL", "http://user:secret-token@llm.example/v1")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    client = TestClient(app)
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok"
    assert body["contracts"] == c.CONTRACT_VERSION and body["llm_configured"] is True
    assert body["data_dir_writable"] is True
    assert "secret" not in response.text and "llm.example" not in response.text
    page = client.get("/app")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
