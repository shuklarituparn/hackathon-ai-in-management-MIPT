"""The complete check (T11): uploads -> preparation -> harness -> validation -> reconciliation -> saved result.

Everything is real except the model: storage (SQLite), preparation and inspection tools, the harness loop, the
T06 validator and reconciliation. The model is ``case19_reader.ReaderModel``, a mechanical reader that submits
what the supplied files print and cites their real evidence. Tests on the scanned bundle need Tesseract
(``ocr``). The ``live`` test at the end runs the same baseline with the configured provider; a scripted model
does not replace it.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from case19_reader import ROW_LABEL, ReaderModel
from case19_scenarios import build_scenario, load_manifest, load_scenario
from fastapi import FastAPI
from fastapi.testclient import TestClient
from recon_fixtures import INDICATORS

from sverka import api, cli, config
from sverka import contracts as c
from sverka.harness import HarnessLimits
from sverka.harness_fakes import ScriptedProvider, respond
from sverka.model_adapter import ProviderError
from sverka.run_service import RunService, check_limits, provider_from_env, provider_problem
from sverka.storage import SqliteStore

ROOT = Path(__file__).resolve().parent.parent
CASE19 = ROOT / "examples" / "case19"
SCOPE = c.Scope(
    contract="24/03/15-01", report_row=ROW_LABEL, date_from=date(2026, 1, 1), date_to=date(2026, 5, 31), confirmed=True
)
FAST = HarnessLimits(retry_backoff_seconds=0, provider_retries=1)


# ======================= helpers =======================


def synthetic_report() -> bytes:
    """The supplied report's row as a spreadsheet, so the flow can run without OCR. Synthetic test input."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "КФЗ"
    sheet.append(["КАРТОЧКА УЧЕТА ФАКТИЧЕСКИХ ЗАТРАТ №3", "Договор 24/03/15-01"])
    sheet.append(["Этап", "", *[label for _, _, label, *_ in INDICATORS]])
    sheet.append([ROW_LABEL, "", *[float(value) for *_, value in INDICATORS]])
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()


def native_bundle() -> list[tuple[str, str, bytes]]:
    """(name, role, bytes): the synthetic report, the 18 supplied primaries that need no OCR, the README."""
    files = [("01_Карточка_КФЗ.xlsx", "report", synthetic_report())]
    for entry in load_manifest()["primaries"]:
        if not entry["scan"]:
            files.append((entry["file"], "primary", (CASE19 / entry["file"]).read_bytes()))
    files.append(("00_README_набор_данных.txt", "context", (CASE19 / "00_README_набор_данных.txt").read_bytes()))
    return files


class Flow:
    """A store with uploaded files and a run service whose model is the mechanical reader."""

    def __init__(self, tmp_path, files, *, model=None, limits: HarnessLimits = FAST, **reader):
        self.data_dir = tmp_path / "data"
        self.store = SqliteStore(self.data_dir)
        self.workspace_id = self.store.create_workspace().workspace_id
        self.versions = {
            name: self.store.save_document(self.workspace_id, filename=name, role=role, data=data)
            for name, role, data in files
        }
        self.reader = ReaderModel(
            lambda version_id: self.service.tools.prepared(self.workspace_id, version_id), **reader
        )
        self.provider = ScriptedProvider(handler=model or self.reader)
        self.service = RunService(
            self.store, provider_factory=lambda: self.provider, limits=limits, trace_dir=str(self.data_dir / "traces")
        )

    def create_run(self, names=None) -> c.Run:
        ids = [v.version_id for name, v in self.versions.items() if names is None or name in names]
        return self.store.create_run(self.workspace_id, c.StartRunRequest(version_ids=ids, scope=SCOPE))

    def run(self, names=None) -> c.Run:
        return self.service.execute(self.create_run(names))

    def client(self) -> TestClient:
        app = FastAPI()
        services = api.Services(store=self.store, launcher=self.service, responder=api.SavedResultResponder(self.store))
        api.install(app, services)
        return TestClient(app)

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def flow(tmp_path):
    made: list[Flow] = []

    def make(files=None, **kw) -> Flow:
        made.append(Flow(tmp_path, native_bundle() if files is None else files, **kw))
        return made[-1]

    yield make
    for f in made:
        f.close()


def scenario_files(name: str, tmp_path) -> list[tuple[str, str, bytes]]:
    return [(u.path.name, u.role, u.path.read_bytes()) for u in build_scenario(name, tmp_path / name)]


def by_entry(run: c.Run) -> dict[str, c.IndicatorResult]:
    return {i.entry_id: i for i in run.result.indicators}


def check_oracle(run: c.Run, scenario: str) -> None:
    """Compare a completed run with a scenario's expected statuses, values and exclusions."""
    expected = load_scenario(scenario)["expected"]
    indicators = by_entry(run)
    counts = {s: sum(1 for i in indicators.values() if i.status == s) for s in expected["status_counts"]}
    assert counts == expected["status_counts"]
    for entry_id, want in expected["indicators"].items():
        got = indicators[entry_id]
        assert (got.status, got.problem, got.coverage) == (want["status"], want["problem"], want["coverage"]), entry_id
        for name in ("observed_sum", "expected_value", "diff"):
            value = getattr(got, name)
            assert (None if value is None else format(value, "f")) == want[name], (entry_id, name)
    # The README files are listed as context_only; the oracle names the excluded primaries.
    excluded = sorted(e.reason for e in run.result.exclusions if e.reason != "context_only")
    assert excluded == sorted(e["reason"] for e in expected["exclusions"])


# ======================= the flow without OCR =======================


def test_a_check_runs_from_uploads_to_a_saved_result(flow):
    f = flow()
    run = f.run()
    assert run.status == "completed" and run.stage == "save" and run.failure is None
    stages = {p.stage: (p.done, p.total, p.failed) for p in run.progress}
    assert stages == {
        "prepare": (19, 19, 0),
        "extract": (19, 19, 0),
        "validate": (19, 19, 0),
        "reconcile": (1, 1, 0),
        "save": (1, 1, 0),
    }
    assert len(run.primaries) == 18 and len(run.report.indicators) == 15
    indicators = by_entry(run)
    for entry_id, value in (("col5", "5558474.83"), ("col7", "5054470.26"), ("col10", "1671359.96")):
        assert indicators[entry_id].status == "verified" and indicators[entry_id].expected_value == Decimal(value)
    assert indicators["col17"].reported_value == Decimal("11580377.40")
    # Every number shown comes with evidence that resolves in this workspace and shows that number.
    payroll = indicators["col5"]
    assert len(payroll.contributions) == 10
    for contribution in payroll.contributions:
        block = f.store.get_block(f.workspace_id, contribution.evidence_ids[0])
        assert block.version_id == contribution.version_id
    reported = f.store.get_block(f.workspace_id, payroll.reported_evidence_ids[0])
    assert reported.version_id == f.versions["01_Карточка_КФЗ.xlsx"].version_id and reported.text == "5558474.83"
    # The replay manifest names what ran, without credentials.
    assert run.replay.provider == "scripted" and run.replay.tool_versions["extraction"] == "1"
    assert set(run.replay.input_sha256) == {v.version_id for v in f.versions.values()}
    assert run.replay.settings["reconciliation.tolerance"] == "0.01"
    # The conversation holds the outcome as typed cards.
    [message] = f.store.list_messages(f.workspace_id)
    assert message.role == "assistant" and message.run_id == run.run_id
    assert message.text == run.result.conclusion.summary and [card.kind for card in message.cards] == ["conclusion"]
    trace = (f.data_dir / "traces" / f.workspace_id / f"{run.run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(trace[0])["event"] == "run_started" and json.loads(trace[-1])["event"] == "run_finished"


def test_the_saved_run_and_its_evidence_survive_a_restart(flow, tmp_path):
    f = flow()
    run = f.run()
    evidence = by_entry(run)["col5"].contributions[0].evidence_ids[0]
    f.store.close()
    reopened = SqliteStore(f.data_dir)
    try:
        again = reopened.get_run(f.workspace_id, run.run_id)
        assert again == run and again.status == "completed"
        assert reopened.get_block(f.workspace_id, evidence).block_id == evidence
        assert [m.run_id for m in reopened.list_messages(f.workspace_id)] == [run.run_id]
    finally:
        reopened.close()
        f.store = SqliteStore(f.data_dir)  # the fixture closes this one


def test_the_api_starts_a_check_in_the_background_and_shows_its_progress(flow):
    f = flow()
    client = f.client()
    body = {"version_ids": [v.version_id for v in f.versions.values()], "scope": SCOPE.model_dump(mode="json")}
    started = client.post(f"/api/workspaces/{f.workspace_id}/runs", json=body)
    assert started.status_code == 202 and started.json()["status"] in ("queued", "running", "completed")
    run_id = started.json()["run_id"]
    assert f.service.wait(run_id, timeout=120)
    run = client.get(f"/api/workspaces/{f.workspace_id}/runs/{run_id}").json()
    assert run["status"] == "completed" and run["result"]["conclusion"]["summary"]
    assert {p["stage"] for p in run["progress"]} == {"prepare", "extract", "validate", "reconcile", "save"}
    evidence_id = run["result"]["indicators"][2]["contributions"][0]["evidence_ids"][0]
    view = client.get(f"/api/workspaces/{f.workspace_id}/evidence/{evidence_id}")
    assert view.status_code == 200 and view.json()["block"]["block_id"] == evidence_id
    # A question about the saved run gets the saved cards (the stub responder of this test client).
    reply = client.post(f"/api/workspaces/{f.workspace_id}/messages", json={"text": "Что с ФОТ?", "run_id": run_id})
    assert reply.status_code == 201 and reply.json()[-1]["cards"]


# ======================= missing evidence is not a failure =======================


def test_a_document_the_model_cannot_read_leaves_the_check_completed_and_unverified(flow):
    silent = "02_03_Ведомость_ЗП_Март_2026.xlsx"
    from case19_reader import expected_primary

    f = flow(expected=lambda name: None if name == silent else expected_primary(name))
    run = f.run()
    assert run.status == "completed" and run.failure is None
    payroll = by_entry(run)["col5"]
    assert payroll.status == "insufficient_evidence" and payroll.expected_value is None and payroll.diff is None
    [open_doc] = [u for u in run.result.unresolved if u.version_id == f.versions[silent].version_id]
    assert open_doc.label == silent
    # Policy 7: a document of unknown relevance blocks every category, so nothing is verified by coincidence.
    assert {i.status for i in run.result.indicators} == {"insufficient_evidence"}
    assert by_entry(run)["col10"].problem == "unresolved_interpretation"


def test_a_value_the_report_does_not_print_is_never_accepted(flow):
    # The reader claims the overhead the documents add up to; the report prints another number.
    f = flow(report_overrides={"col15": "1133298.10"})
    run = f.run()
    assert run.status == "completed"
    overhead = next(e for e in run.report.indicators if e.entry_id == "col15")
    assert overhead.observation.state == "unresolved" and overhead.observation.value is None
    result = by_entry(run)["col15"]
    assert result.status == "insufficient_evidence" and result.problem == "unreadable_report_value"
    assert result.reported_value is None
    assert any(i.code == "reinspection_incomplete" for i in run.issues)


def test_corrupt_documents_stay_visible(flow, tmp_path):
    corrupt = ROOT / "tests" / "fixtures" / "case19" / "scenarios" / "corrupt_input"
    files = native_bundle()
    files = [x for x in files if x[0] != "05_Счет-акт_174_25-02-2026.pdf"]
    files.append(
        ("05_Счет-акт_174_25-02-2026.pdf", "primary", (corrupt / "05_Счет-акт_174_25-02-2026.pdf").read_bytes())
    )
    files.append(("16_Акт_пустой.pdf", "primary", (corrupt / "16_Акт_пустой.pdf").read_bytes()))
    f = flow(files)
    run = f.run()
    assert run.status == "completed"
    broken = {f.versions[n].version_id for n in ("05_Счет-акт_174_25-02-2026.pdf", "16_Акт_пустой.pdf")}
    assert {v for i in run.issues if i.code == "document_unreadable" for v in i.version_ids} == broken
    assert broken <= {u.version_id for u in run.result.unresolved}
    stages = {p.stage: (p.done, p.failed) for p in run.progress}
    assert stages["prepare"] == (18, 2) and stages["validate"] == (18, 2)
    # An unreadable accounting document may belong anywhere: nothing is verified, not even a zero.
    assert all(i.status == "insufficient_evidence" for i in run.result.indicators)
    assert by_entry(run)["col4"].problem == "unreadable_input"


# ======================= failures are explicit =======================


def test_missing_credentials_fail_the_check_at_once(flow, monkeypatch):
    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"SVERKA_PROVIDER_{name}", raising=False)
    f = flow()
    f.service.provider_factory = provider_from_env
    run = f.run()
    assert run.status == "failed" and run.result is None and run.failure.code == "missing_credentials"
    assert "SVERKA_PROVIDER_API_KEY" in run.failure.message and "документы не проверялись" in run.failure.message
    assert f.provider.calls == []
    assert f.store.list_versions(f.workspace_id)  # the uploads stay
    [message] = f.store.list_messages(f.workspace_id)
    assert [card.kind for card in message.cards] == ["run_status"] and message.cards[
        0
    ].failure.code == "missing_credentials"


def test_an_invalid_provider_setting_is_reported_without_its_value(flow, monkeypatch):
    monkeypatch.setenv("SVERKA_PROVIDER_BASE_URL", "ftp://user:secret-token@example.invalid")
    assert "SVERKA_PROVIDER_BASE_URL" in provider_problem() and "secret-token" not in provider_problem()
    f = flow()
    f.service.provider_factory = provider_from_env
    run = f.run()
    assert run.failure.code == "missing_credentials" and "secret-token" not in run.failure.message


@pytest.mark.parametrize("code", ["timeout", "provider_error"])
def test_a_provider_that_never_answers_fails_the_check_with_its_reason(flow, code):
    def unavailable(messages, tools):
        return ProviderError(code, "the endpoint did not answer", retryable=True)

    f = flow(model=unavailable)
    run = f.run()
    assert run.status == "failed" and run.result is None and run.failure.code == code
    assert "the endpoint did not answer" in run.failure.message
    assert len(f.provider.calls) == 2  # one retry, then the report is given up and the run stops
    assert {p.stage: p.failed for p in run.progress}["extract"] == 1


def test_a_model_that_never_uses_the_tools_fails_the_check(flow):
    f = flow(model=lambda messages, tools: respond(text="Все показатели подтверждены."))
    run = f.run()
    assert run.status == "failed" and run.failure.code == "provider_error" and run.result is None
    assert "the report could not be extracted" in run.failure.message


def test_a_check_without_a_report_is_invalid_input(flow):
    f = flow()
    run = f.run([n for n in f.versions if not n.startswith("01_")])
    assert run.status == "failed" and run.failure.code == "invalid_input"
    assert "exactly one report" in run.failure.message and f.provider.calls == []


def test_a_reconciliation_defect_fails_the_run_instead_of_completing_it(flow):
    f = flow()

    def broken(*args):
        raise RuntimeError("boom")

    f.service.reconcile = broken
    run = f.run()
    assert run.status == "failed" and run.failure.code == "internal_error" and run.result is None
    assert {p.stage: p.failed for p in run.progress}["reconcile"] == 1


def test_a_report_without_indicators_is_not_a_completed_check(flow):
    from case19_reader import ReaderModel
    from sverka.harness_fakes import tool_call

    def empty_report(messages, tools):
        version_id = next(m.text for m in messages if m.role == "user").split("version_id=")[1].split(",")[0]
        proposal = {"kind": "report", "version_id": version_id, "row_label": ROW_LABEL, "headers": [], "indicators": []}
        return respond(tool_call("validate_extraction", {"proposal": proposal}))

    f = flow()
    reader = f.reader
    assert isinstance(reader, ReaderModel)
    f.provider = ScriptedProvider(
        handler=lambda m, t: empty_report(m, t) if "role=report" in m[1].text else reader(m, t)
    )
    run = f.run()
    assert run.status == "failed" and run.result is None and run.failure.code == "invalid_input"
    assert "ни одного показателя" in run.failure.message
    assert any(i.code == "no_indicators" for i in run.issues)


# ======================= cancellation =======================


def test_cancel_stops_the_check_and_a_late_result_is_dropped(flow):
    f = flow()
    entered, release = threading.Event(), threading.Event()

    def slow(messages, tools):
        entered.set()
        assert release.wait(30)
        return f.reader(messages, tools)

    f.provider = ScriptedProvider(handler=slow)
    client = f.client()
    body = {"version_ids": [v.version_id for v in f.versions.values()], "scope": SCOPE.model_dump(mode="json")}
    run_id = client.post(f"/api/workspaces/{f.workspace_id}/runs", json=body).json()["run_id"]
    assert entered.wait(60)  # the model call for the report is in flight
    cancelled = client.post(f"/api/workspaces/{f.workspace_id}/runs/{run_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    release.set()  # the model answers after the cancellation
    assert f.service.wait(run_id, timeout=60)
    run = f.store.get_run(f.workspace_id, run_id)
    assert run.status == "cancelled" and run.result is None and run.failure is None and run.report is None
    assert f.store.list_messages(f.workspace_id) == []  # nothing was announced
    assert len(f.provider.calls) == 1  # no further document was sent to the model
    # The workspace is free again: a new check can start.
    assert f.run().status == "completed"


def test_a_run_cancelled_before_it_starts_does_nothing(flow):
    f = flow()
    run = f.create_run()
    f.store.save_run(run.model_copy(update={"status": "cancelled"}))
    assert f.service.execute(run).status == "cancelled" and f.provider.calls == []


# ======================= wiring =======================


def test_live_mode_wires_the_run_service_and_the_follow_up_responder(tmp_path, monkeypatch):
    from sverka.harness import FollowUpResponder

    monkeypatch.setattr(config, "RUN_MODE", "live")
    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"SVERKA_PROVIDER_{name}", raising=False)
    services = api.default_services(str(tmp_path / "data"))
    try:
        assert isinstance(services.launcher, RunService) and isinstance(services.responder, FollowUpResponder)
        assert services.launcher.trace_dir == str(tmp_path / "data" / "traces")
        app = FastAPI()
        api.install(app, services)
        client = TestClient(app)
        workspace_id = client.post("/api/workspaces").json()["workspace_id"]
        upload = client.post(
            f"/api/workspaces/{workspace_id}/documents",
            files=[("files", ("01_КФЗ.xlsx", synthetic_report(), "application/octet-stream"))],
            data={"role": "report"},
        )
        body = {"version_ids": [upload.json()[0]["version_id"]], "scope": SCOPE.model_dump(mode="json")}
        run_id = client.post(f"/api/workspaces/{workspace_id}/runs", json=body).json()["run_id"]
        assert services.launcher.wait(run_id, timeout=60)
        run = client.get(f"/api/workspaces/{workspace_id}/runs/{run_id}").json()
        assert run["status"] == "failed" and run["failure"]["code"] == "missing_credentials"
    finally:
        services.store.close()


def test_other_run_modes_keep_their_launchers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_MODE", "disabled")
    services = api.default_services(str(tmp_path / "a"))
    assert isinstance(services.launcher, api.DisabledRunLauncher)
    services.store.close()
    monkeypatch.setattr(config, "RUN_MODE", "fixture")
    services = api.default_services(str(tmp_path / "b"))
    assert isinstance(services.launcher, api.FixtureRunLauncher)
    services.store.close()


def test_a_check_gives_one_document_more_turns_than_the_harness_default():
    """The scanned report used every turn of the harness default before its first proposal (release-check.md)."""
    base, limits = HarnessLimits(), check_limits({})
    assert (limits.doc_max_requests, limits.doc_max_tool_calls) == (20, 40)
    assert (limits.run_max_requests, limits.run_max_tool_calls) == (base.run_max_requests, base.run_max_tool_calls)
    assert limits.shortfall(23) is None
    overridden = check_limits({"SVERKA_HARNESS_DOC_MAX_REQUESTS": "9", "SVERKA_HARNESS_DOC_MAX_TOOL_CALLS": " "})
    assert (overridden.doc_max_requests, overridden.doc_max_tool_calls) == (9, 40)


def test_health_reports_the_provider_state_without_secrets(monkeypatch):
    from sverka.web import app

    for name in tuple(os.environ):
        if name.startswith("SVERKA_PROVIDER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SVERKA_PROVIDER_BASE_URL", "https://api.example.invalid/v1")
    monkeypatch.setenv("SVERKA_PROVIDER_MODEL", "some-model")
    monkeypatch.setenv("SVERKA_PROVIDER_API_KEY", "sk-very-secret-value")
    health = TestClient(app).get("/health")
    assert health.json()["provider"] == {
        "configured": True,
        "name": "openai-compatible",
        "model": "some-model",
        "missing": [],
    }
    assert "sk-very-secret-value" not in health.text and "api.example.invalid" not in health.text
    monkeypatch.delenv("SVERKA_PROVIDER_API_KEY")
    assert TestClient(app).get("/health").json()["provider"]["missing"] == ["SVERKA_PROVIDER_API_KEY"]


def test_the_command_line_check_uses_the_same_service(tmp_path, monkeypatch, capsys):
    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"SVERKA_PROVIDER_{name}", raising=False)
    folder = tmp_path / "bundle"
    folder.mkdir()
    for name, _, data in native_bundle()[:3]:
        (folder / name).write_bytes(data)
    args = ["check", str(folder), "--contract", "24/03/15-01", "--row", ROW_LABEL]
    args += ["--period-from", "01.01.2026", "--period-to", "31.05.2026", "--quiet"]
    args += ["--out", str(tmp_path / "out"), "--data", str(tmp_path / "data")]
    assert cli.main(args) == 2  # no provider is configured: the check is not performed, and says so
    assert "ПРОВЕРКА НЕ ВЫПОЛНЕНА" in capsys.readouterr().err
    saved = c.Run.model_validate_json((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert (
        saved.status == "failed" and saved.failure.code == "missing_credentials" and len(saved.input_version_ids) == 3
    )
    (folder / "02_Карточка_копия.xlsx").write_bytes(b"x")
    assert cli.main(args) == 2 and "--report" in capsys.readouterr().err  # two candidates: the report must be named


def test_the_command_line_reads_archives_like_the_upload_route(tmp_path, monkeypatch, capsys):
    import zipfile

    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"SVERKA_PROVIDER_{name}", raising=False)
    common = ["--contract", "24/03/15-01", "--row", ROW_LABEL, "--period-from", "01.01.2026"]
    common += ["--period-to", "31.05.2026", "--quiet", "--out", str(tmp_path / "out"), "--data", str(tmp_path / "data")]
    # A Windows archive stores Russian names in cp866 without the UTF-8 flag. Python always writes UTF-8
    # names, so the archive is written with ASCII names of the same length and the names are patched in.
    windows = tmp_path / "windows.zip"
    names = {}
    with zipfile.ZipFile(windows, "w") as z:
        for index, (name, _, data) in enumerate(native_bundle()[:3]):
            raw = name.encode("cp866")
            names[(f"file{index}".ljust(len(raw), "_")).encode("ascii")] = raw
            z.writestr(f"file{index}".ljust(len(raw), "_"), data)
    packed = windows.read_bytes()
    for placeholder, raw in names.items():
        assert packed.count(placeholder) == 2  # local header and central directory
        packed = packed.replace(placeholder, raw)
    windows.write_bytes(packed)
    assert cli.main(["check", str(windows), *common]) == 2  # the report is found by its name; no provider
    saved = c.Run.model_validate_json((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert saved.failure.code == "missing_credentials" and len(saved.input_version_ids) == 3
    capsys.readouterr()
    # A damaged archive is "not performed" (2), never the exit code of a finished check with findings (1).
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(windows.read_bytes()[:-40])
    assert cli.main(["check", str(damaged), *common]) == 2
    assert "ОШИБКА" in capsys.readouterr().err
    assert cli.main(["check", str(tmp_path / "missing"), *common]) == 2


def test_command_line_roles():
    names = ["00_README.txt", "01_Карточка_учета.pdf", "04_Акт_118.pdf"]
    assert cli._roles(names, None, []) == {
        "00_README.txt": "context",
        "01_Карточка_учета.pdf": "report",
        "04_Акт_118.pdf": "primary",
    }
    assert cli._roles(names, "04_Акт_118.pdf", ["01_Карточка_учета.pdf"])["00_README.txt"] == "primary"
    with pytest.raises(ValueError):
        cli._roles(names, "нет_такого.pdf", [])


# ======================= the supplied bundle (needs Tesseract) =======================


@pytest.mark.ocr
def test_baseline_bundle_gives_the_expected_result(flow, tmp_path):
    """V01 through the whole flow: 1 scanned report, 22 primaries, the README as context."""
    f = flow(scenario_files("baseline", tmp_path))
    started = time.monotonic()
    run = f.run()
    assert run.status == "completed", run.failure
    check_oracle(run, "baseline")
    assert len(f.reader.documents) == 23 and len(run.primaries) == 22
    indicators = by_entry(run)
    for entry_id, _, _, _, _, _, value in INDICATORS:
        assert indicators[entry_id].reported_value == Decimal(value), entry_id
    overhead = indicators["col15"]
    assert sorted(x.amount for x in overhead.contributions) == sorted(
        Decimal(x) for x in ("245000.00", "318298.10", "270000.00", "300000.00")
    )
    for contribution in overhead.contributions:  # the cited fragment shows the counted amount
        block = f.store.get_block(f.workspace_id, contribution.evidence_ids[0])
        digits = "".join(ch for ch in block.text if ch.isdigit())
        assert format(contribution.amount, "f").replace(".", "") in digits, block.text
    reported = f.store.get_block(f.workspace_id, overhead.reported_evidence_ids[0])
    assert reported.method == "reocr" and "1145 798,10" in reported.text.replace("1 145", "1145")
    total = run.result.conclusion.grand_total
    assert (
        total.diff == Decimal("12500.00")
        and total.explained_by == ["col15"]
        and total.unexplained_diff == Decimal("0.00")
    )
    names = {v.version_id: n for n, v in f.versions.items()}
    assert {names[e.version_id]: e.reason for e in run.result.exclusions if e.reason != "context_only"} == {
        "10_Акт_ADM-27_копия.pdf": "duplicate",
        "14_Акт_ОРГ-0603.pdf": "outside_period",
        "15_Акт_ADM-75_15-05-2026.pdf": "other_contract",
    }
    assert run.result.unresolved == [] and time.monotonic() - started < 300


@pytest.mark.ocr
def test_missing_march_keeps_payroll_partial_and_the_overhead_discrepancy(flow, tmp_path):
    """V02 through the whole flow."""
    f = flow(scenario_files("missing_march", tmp_path))
    run = f.run()
    assert run.status == "completed", run.failure
    check_oracle(run, "missing_march")
    conclusion = run.result.conclusion
    assert [d.entry_id for d in conclusion.component_discrepancies] == ["col15"]
    assert conclusion.grand_total.status == "insufficient_evidence" and conclusion.grand_total.diff is None


@pytest.mark.ocr
def test_corrupt_inputs_block_every_indicator_on_the_supplied_bundle(flow, tmp_path):
    f = flow(scenario_files("corrupt_input", tmp_path))
    run = f.run()
    assert run.status == "completed", run.failure
    check_oracle(run, "corrupt_input")
    assert sum(1 for i in run.issues if i.code == "document_unreadable") == 2


# ======================= the real provider =======================


@pytest.mark.live
@pytest.mark.ocr
def test_live_baseline_with_the_configured_provider(tmp_path):
    """V01 with the real model. Needs ``SVERKA_PROVIDER_*`` and Tesseract; skipped without them.

    A skip is not a pass: record the result of this test in ``docs/release-check.md`` before release."""
    problem = provider_problem()
    if problem is not None:
        pytest.skip(f"the model provider is not configured: {problem}")
    store = SqliteStore(tmp_path / "data")
    try:
        workspace_id = store.create_workspace().workspace_id
        versions = [
            store.save_document(workspace_id, filename=name, role=role, data=data)
            for name, role, data in scenario_files("baseline", tmp_path)
        ]
        request = c.StartRunRequest(version_ids=[v.version_id for v in versions], scope=SCOPE)
        service = RunService(store, trace_dir=str(tmp_path / "data" / "traces"))
        run = service.execute(store.create_run(workspace_id, request))
        assert run.status == "completed", run.failure
        check_oracle(run, "baseline")
    finally:
        store.close()
