"""T08 live check: real provider connectivity, tool use and image input on a known evidence fixture.

Excluded from ordinary CI by the ``live`` marker. Needs ``SVERKA_PROVIDER_BASE_URL``, ``SVERKA_PROVIDER_MODEL``
and ``SVERKA_PROVIDER_API_KEY`` (see docs/harness.md). Without them the tests are skipped, which is not a pass.

    python -m pytest -q -m live tests/test_provider_live.py

The evidence is the published example of Счет-акт № 174 (tests/fixtures/contracts/evidence_blocks.json) with a
real 150 dpi render of the PDF page, served by the in-memory fake tools. Validation is the fake validator, so
this proves the provider/tool loop, not extraction quality.
"""

import io
import json
import os
from decimal import Decimal

import pytest

from sverka import contracts as c
from sverka.fakes import InMemoryStore
from sverka.harness import ExtractionHarness, HarnessLimits
from sverka.harness_fakes import FakeEvidenceTools, seed_example
from sverka.model_adapter import ImagePart, ModelMessage, OpenAICompatibleProvider, ProviderConfig, ToolSpec

pytestmark = pytest.mark.live

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACT_174 = os.path.join(ROOT, "examples", "case19", "05_Счет-акт_174_25-02-2026.pdf")
SCOPE = c.Scope(
    contract="24/03/15-01", report_row="3 этап, 2026 г.", date_from="2026-01-01", date_to="2026-05-31", confirmed=True
)


@pytest.fixture(scope="module")
def provider() -> OpenAICompatibleProvider:
    config = ProviderConfig.from_env()
    if config.missing():
        pytest.skip("live provider is not configured: set " + ", ".join(config.missing()))
    return OpenAICompatibleProvider(config)


@pytest.fixture(scope="module")
def page_png() -> bytes:
    """Page 1 of the real act at 150 dpi: the render the example boxes were measured on (1240x1754)."""
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(ACT_174) as pdf:
        image = pdf.pages[0].to_image(resolution=150).original
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def test_live_tool_use_extracts_the_known_fixture(provider, page_png):
    store = InMemoryStore()
    ws = store.create_workspace().workspace_id
    tools = FakeEvidenceTools(store)
    report = seed_example(store, tools, ws, "ver_kfz_v1", role="report", filename="report.pdf")
    act = seed_example(store, tools, ws, "ver_sa174_v1", role="primary", filename="act.pdf", render=page_png)
    run = store.create_run(ws, c.StartRunRequest(version_ids=[report.version_id, act.version_id], scope=SCOPE))
    harness = ExtractionHarness(
        provider=provider, tools=tools, store=store, limits=HarnessLimits(run_max_requests=24, run_max_seconds=600)
    )

    result = harness.extract(run)

    summary = [(d.version.role, d.status, d.stop_reason, d.failure) for d in result.documents]
    assert result.status == "completed", (result.failure, summary)
    assert [d.status for d in result.documents] == ["validated", "validated"], summary
    amount = result.primaries[0].amount
    assert amount.state == "accepted" and amount.value == Decimal("318298.10")
    assert amount.evidence_ids == [act.ids["blk_sa174_amount"]]
    assert result.report.indicators[0].observation.value == Decimal("1145798.10")
    # The model really called tools through the provider, and the calls were paired by ID.
    calls = [e for e in result.trace if e["event"] == "tool_call"]
    assert {e["name"] for e in calls} >= {"validate_extraction"}
    assert all(e["call_id"] for e in calls)
    assert provider.config.api_key not in json.dumps(result.trace, ensure_ascii=False)
    print("live usage:", result.usage, "model:", provider.model)


def test_live_image_input_is_read_by_the_model(provider, page_png):
    tool = ToolSpec(
        "report_amount",
        "Report the total amount payable printed on the page.",
        {
            "type": "object",
            "properties": {"amount": {"type": "string", "description": 'Digits with a dot, e.g. "1234.50"'}},
            "required": ["amount"],
            "additionalProperties": False,
        },
    )
    messages = [
        ModelMessage("system", "Read the page image and call report_amount once. Do not answer in prose."),
        ModelMessage("user", "What is the amount payable on this page?", images=(ImagePart("image/png", page_png),)),
    ]
    response = provider.complete(messages, [tool], timeout=120)
    calls = response.message.tool_calls
    assert calls and calls[0].name == "report_amount" and calls[0].id, response
    amount = json.loads(calls[0].arguments)["amount"].replace(" ", "").replace(",", ".")
    assert Decimal(amount) == Decimal("318298.10")
