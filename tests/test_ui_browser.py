"""T09 browser checks (V09): the /app chat and evidence interface, driven in a real browser over the real API.

The server is ``sverka.web.app`` with an isolated store. Most checks use labelled synthetic reconciliation
fixtures and seeded evidence. Integration checks run the real preparation, extraction validation, reconciliation
and follow-up services against native case19 documents, replacing only the model with offline scripted readers.
No test calls a remote model.

Requires Playwright and a Chromium browser (installed Chrome/Edge, or ``playwright install chromium``); skipped
otherwise. Set ``SVERKA_TEST_BROWSER_CHANNEL`` to pick a channel and ``SVERKA_UI_SCREENSHOTS`` to a directory to save
screenshots of the checked states.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")

import uvicorn  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from sverka import api, config  # noqa: E402
from sverka import contracts as c  # noqa: E402
from sverka.storage import SqliteStore  # noqa: E402
from sverka.web import app as web_app  # noqa: E402

expect = sync_api.expect
pytestmark = pytest.mark.browser

FIXTURES = Path(__file__).parent / "fixtures" / "reconciliation"
SCOPE = {
    "contract": "24/03/15-01",
    "report_row": "3 этап, 2026 г.",
    "date_from": "2026-01-01",
    "date_to": "2026-05-31",
}
NB = " "
REPORT_RENDER = (2632, 1860)
ACT_RENDER = (1240, 1754)
ROTATED_RENDER = (1754, 1240)


# ======================= server, browser and test services =======================


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(web_app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        requested = os.environ.get("SVERKA_TEST_BROWSER_CHANNEL")
        channels = [requested] if requested else ["chrome", "msedge", None]
        failures = []
        for channel in channels:
            try:
                instance = pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch()
                break
            except Exception as exc:  # browser not installed in this environment
                failures.append(f"{channel or 'bundled chromium'}: {type(exc).__name__}")
        else:
            pytest.skip("no browser available for Playwright (" + "; ".join(failures) + ")")
        yield instance
        instance.close()


class ScriptedLauncher:
    """Applies test-chosen outcomes to started runs in order. Without a plan a run stays queued, like a busy
    run service, so tests can drive its progress through the store."""

    def __init__(self, store):
        self.store, self.plan, self.started, self.cancelled = store, [], [], []

    def start(self, run):
        self.started.append(run.run_id)
        if self.plan:
            self.plan.pop(0)(self.store, run)

    def cancel(self, run):
        self.cancelled.append(run.run_id)


class StubResponder(api.SavedResultResponder):
    """The saved-result stub, plus a crash on demand to exercise the API's failed-reply message."""

    def respond(self, message):
        if "СБОЙ" in message.text:
            raise RuntimeError("model adapter crashed")
        return super().respond(message)


def _now():
    return datetime.now(UTC)


def completed(run, fixture, version_map=None, edit=None):
    """``run`` completed with a fixture result. ``version_map`` points the fixture's readable version IDs at the
    versions uploaded in the test workspace; ``edit`` rewrites the JSON text (for hostile-text checks)."""
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    for fake, real in (version_map or {}).items():
        text = text.replace(f'"{fake}"', f'"{real}"')
    if edit:
        text = edit(text)
    result = c.ReconciliationResult.model_validate_json(text).model_copy(update={"scope": run.scope})
    n = len(run.input_version_ids)
    progress = [c.StageProgress(stage=s, done=n, total=n) for s in ("prepare", "extract", "validate")]
    progress += [c.StageProgress(stage=s, done=1, total=1) for s in ("reconcile", "save")]
    update = {"status": "completed", "stage": "save", "result": result, "progress": progress, "updated_at": _now()}
    return run.model_copy(update=update)


def completes_with(fixture, version_map=None):
    return lambda store, run: store.save_run(completed(run, fixture, version_map))


def fails_with(code, message):
    failure = c.RunFailure(code=code, message=message)
    return lambda store, run: store.save_run(
        run.model_copy(update={"status": "failed", "failure": failure, "updated_at": _now()})
    )


class Env:
    def __init__(self, base, store, services, launcher, page, errors):
        self.base, self.store, self.services, self.launcher = base, store, services, launcher
        self.page, self.errors = page, errors

    def goto(self, ws=None):
        page = self.page
        page.goto(self.base + "/app" + ("#" + ws if ws else ""))
        if ws:
            expect(page.locator(f'#ws-history [data-workspace-id="{ws}"]')).to_have_attribute("aria-current", "page")
            expect(page.locator("#ws-status .ws-title")).not_to_be_empty()
        return page

    def shot(self, name, **kwargs):
        target = os.environ.get("SVERKA_UI_SCREENSHOTS")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(target) / f"{name}.png"), **kwargs)


@pytest.fixture
def env(server, browser, tmp_path):
    store = SqliteStore(tmp_path / "data")
    launcher = ScriptedLauncher(store)
    services = api.Services(store=store, launcher=launcher, responder=StubResponder(store))
    web_app.state.services = services
    # Reduced motion: the pencil and highlighter animations are cosmetic and would only add timing noise.
    context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU", reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(f"page error: {exc}"))
    page.on("dialog", lambda dialog: (errors.append(f"dialog: {dialog.message}"), dialog.dismiss()))
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console: {msg.text}")
            if msg.type == "error" and "Failed to load resource" not in msg.text
            else None
        ),
    )
    yield Env(server, store, services, launcher, page, errors)
    context.close()
    web_app.state.services = None
    store.close()
    assert errors == []


# ======================= seeded data =======================


def png(size, box=None):
    """A page render of ``size`` with a filled red rectangle exactly at ``box`` (x0, y0, x1, y1)."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline=(160, 160, 160), width=4)
    if box:
        draw.rectangle([box[0], box[1], box[2] - 1, box[3] - 1], fill=(255, 120, 120))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def seed_region(
    store, ws, version_id, block_id, box, size, text, *, method="ocr", rotation=0, crop=None, dpi=150, quality=None
):
    render = store.save_artifact(ws, version_id, kind="page_render", media_type="image/png", data=png(size, box))
    locator = {
        "kind": "page_region",
        "page": 1,
        "render_id": render.artifact_id,
        "box": dict(zip(("x0", "y0", "x1", "y1"), box)),
        "image_width": size[0],
        "image_height": size[1],
        "transform": {"rotation": rotation, "dpi": dpi, "crop": crop},
    }
    store.save_blocks(
        ws,
        [
            c.EvidenceBlock(
                version_id=version_id, block_id=block_id, text=text, method=method, locator=locator, quality=quality
            )
        ],
    )
    return render


def seed_workspace(store):
    """Real uploaded versions standing in for part of case 19, with the fixture's readable version IDs mapped."""
    ws = store.create_workspace().workspace_id
    files = {
        "report": ("01_Карточка_учета_затрат_этап_3.pdf", "report"),
        "ver_act118": ("04_Акт_118_28-01-2026.pdf", "primary"),
        "ver_sa174": ("05_Счет-акт_174_25-02-2026.pdf", "primary"),
        "ver_kl31": ("06_Служебная_записка_КЛ-31.docx", "primary"),
        "ver_upd77": ("07_УПД_0426-77.pdf", "primary"),
        "ver_pay01": ("02_01_Ведомость_ЗП_Январь_2026.xlsx", "primary"),
        "ver_it0326": ("11_Справка_ИТ_03-26.docx", "primary"),
        "ver_readme": ("00_README_набор_данных.txt", "context"),
    }
    versions = {
        key: store.save_document(ws, filename=name, role=role, data=f"{key}-bytes".encode())
        for key, (name, role) in files.items()
    }
    return ws, versions


def version_map(versions):
    return {key: v.version_id for key, v in versions.items() if key.startswith("ver_")}


def seed_evidence(store, ws, versions):
    report, sa174 = versions["report"].version_id, versions["ver_sa174"].version_id
    renders = {
        "report": seed_region(
            store,
            ws,
            report,
            "blk_kfz_col15",
            (2050, 842, 2175, 868),
            REPORT_RENDER,
            "1 145 798,10",
            dpi=300,
            quality={"engine": "tesseract", "confidence": 91.53846153846153},  # a Tesseract line average
        ),
        "sa174": seed_region(
            store,
            ws,
            sa174,
            "blk_sa174_line0",
            (142, 509, 605, 531),
            ACT_RENDER,
            "К оплате по документу: 318 298,10 руб.",
            method="native_text",
        ),
        # A scan stored already rotated: the box is in the rotated render's space and must not be rotated again.
        "upd77": seed_region(
            store,
            ws,
            versions["ver_upd77"].version_id,
            "blk_upd77_line0",
            (1100, 820, 1500, 870),
            ROTATED_RENDER,
            "Всего к оплате 300 000,00",
            rotation=90,
        ),
    }
    pay01, kl31, it0326 = (versions[k].version_id for k in ("ver_pay01", "ver_kl31", "ver_it0326"))
    store.save_blocks(
        ws,
        [
            c.EvidenceBlock(
                version_id=pay01,
                block_id="blk_pay01_line0",
                text="=SUM(C6:C7)",
                method="spreadsheet",
                locator={
                    "kind": "cell",
                    "sheet": "Ведомость",
                    "ref": "C8",
                    "formula": "=SUM(C6:C7)",
                    "cached_value": None,
                    "derived_value": "1010000.00",
                    "evaluation": "derived",
                    "inputs": ["C6", "C7"],
                },
            ),
            c.EvidenceBlock(
                version_id=pay01,
                block_id="blk_pay01_line1",
                text="=C6*Ставки!B2",
                method="spreadsheet",
                locator={
                    "kind": "cell",
                    "sheet": "Ведомость",
                    "ref": "C9",
                    "formula": "=C6*Ставки!B2",
                    "cached_value": "100000",
                    "evaluation": "unresolved",
                    "inputs": ["C6", "Ставки!B2"],
                },
            ),
            c.EvidenceBlock(
                version_id=kl31,
                block_id="blk_kl31_line0",
                text="Сумма расходов: 270 000,00 руб.",
                method="docx",
                locator={"kind": "docx", "quote": "Сумма расходов: 270 000,00 руб.", "paragraph": 3},
            ),
            c.EvidenceBlock(
                version_id=it0326,
                block_id="blk_it0326_line0",
                text="703 244,51",
                method="docx",
                locator={"kind": "docx", "quote": "703 244,51", "table": 0, "row": 1, "cell": 1},
            ),
        ],
    )
    return renders


def seed_run(store, ws, versions, fixture="baseline_result.json", edit=None):
    request = c.StartRunRequest(
        version_ids=[v.version_id for v in versions.values()], scope={**SCOPE, "confirmed": True}
    )
    run = store.create_run(ws, request)
    store.save_run(completed(run, fixture, version_map(versions), edit))
    return run.run_id


def queued_run(store, ws, versions):
    request = c.StartRunRequest(version_ids=[versions["report"].version_id], scope={**SCOPE, "confirmed": True})
    return store.create_run(ws, request).run_id


# ======================= page helpers =======================


def run_block(page, run_id):
    return page.locator(f'.run-block[data-run-id="{run_id}"]')


def chat_messages(page):
    """Saved chat messages, excluding the assistant's result and completion blocks."""
    return page.locator(".msg[data-message-id]")


def assert_no_internal_details(page):
    """Internal identity may remain in URLs/data attributes, never readable or accessible copy."""
    copy = page.evaluate(
        """() => [document.body.innerText, document.querySelector('#announcer').textContent,
          ...Array.from(document.querySelectorAll('*')).flatMap(el =>
            ['title', 'aria-label', 'aria-description', 'aria-valuetext', 'alt', 'placeholder']
              .map(name => el.getAttribute(name) || ''))].filter(Boolean).join('\\n')"""
    )
    patterns = [
        r"\b(?:ws|doc|ver|blk|art|run|msg)_[A-Za-z0-9_-]+",
        r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b|\b[0-9]{40,64}\b",
        r"\b(?:SHA-?256|RuntimeError|provider_error|date_from|run_id|workspace_id|SVERKA_[A-Z0-9_]+)\b",
    ]
    for pattern in patterns:
        leaked = re.search(pattern, copy, re.IGNORECASE)
        assert leaked is None, f"Internal detail appears in readable or accessible copy: {leaked.group()}"


def row(page, run_id, entry):
    return run_block(page, run_id).locator(f'tr.ind-row[data-entry="{entry}"]')


def cell(page, run_id, entry, column):
    return row(page, run_id, entry).locator(f"td.c-{column}")


def expand(page, run_id, entry):
    toggle = row(page, run_id, entry).locator("button.ind-toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    detail = run_block(page, run_id).locator(f'tr.ind-row[data-entry="{entry}"] + tr.ind-detail')
    expect(detail).to_be_visible()
    return detail


def open_setup(page):
    """Unfold «Документы и параметры»; it folds once a reconciliation has runs."""
    toggle = page.locator("#setup-toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(page.locator("#setup-panel")).to_be_visible()


def confirm_scope(page):
    open_setup(page)
    page.check("#scope-confirm")


def fill_scope(page, confirm=True):
    open_setup(page)
    page.fill("#scope-contract", SCOPE["contract"])
    page.fill("#scope-row", SCOPE["report_row"])
    page.fill("#scope-from", SCOPE["date_from"])
    page.fill("#scope-to", SCOPE["date_to"])
    if confirm:
        page.check("#scope-confirm")


def open_evidence(page, scope, block_id):
    scope.locator(f'button.ev-btn[data-block-id="{block_id}"]').first.click()
    panel = page.locator("#evidence-body")
    expect(panel).to_have_attribute("data-block", block_id)
    expect(panel).to_have_attribute("data-state", re.compile(r"^(ready|error)$"))
    return panel


def pixel(page, x, y):
    data = page.screenshot(clip={"x": x, "y": y, "width": 1, "height": 1})
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0))


def assert_highlight(page, box, size):
    """The highlight sits on the box scaled by the displayed image, and over the red rectangle drawn there."""
    page.wait_for_function(
        "() => { const i = document.querySelector('#evidence-body img.page-img');"
        " return i && i.complete && i.naturalWidth > 0; }"
    )
    img = page.locator("#evidence-body img.page-img")
    mark = page.locator("#evidence-body .page-mark")
    mark.scroll_into_view_if_needed()
    ib, mb = img.bounding_box(), mark.bounding_box()
    sx, sy = ib["width"] / size[0], ib["height"] / size[1]
    assert abs(sx - sy) < 0.002, "displayed image keeps the render's aspect ratio"
    assert abs(mb["x"] - (ib["x"] + box[0] * sx)) <= 1.5
    assert abs(mb["y"] - (ib["y"] + box[1] * sy)) <= 1.5
    assert abs(mb["width"] - (box[2] - box[0]) * sx) <= 1.5
    assert abs(mb["height"] - (box[3] - box[1]) * sy) <= 1.5
    inside = pixel(page, mb["x"] + mb["width"] / 2, mb["y"] + mb["height"] / 2)
    assert inside[0] > 200 and inside[1] < 185 and inside[2] < 140, f"highlight is over the drawn box: {inside}"
    outside = pixel(page, mb["x"] + mb["width"] / 2, mb["y"] - 12)
    assert min(outside) > 235, f"above the highlight is blank page: {outside}"
    return ib


def fonts_ready(page):
    """Layout checks measure the bundled fonts, not the platform fallback shown while they load."""
    loaded = page.evaluate(
        "async () => { await document.fonts.ready; return document.fonts.check('600 15px \"Golos Text\"'); }"
    )
    assert loaded, "the bundled Golos Text font loads from /static/fonts"


def baseline_checks(page, run_id):
    rows = run_block(page, run_id).locator("tr.ind-row")
    expect(rows).to_have_count(15)
    entries = [f"col{n}" for n in range(3, 18)]
    assert [r.get_attribute("data-entry") for r in rows.all()] == entries
    statuses = {e: row(page, run_id, e).get_attribute("data-status") for e in entries}
    assert [e for e, s in statuses.items() if s == "verified"] == ["col4", "col5", "col7", "col10", "col16"]
    assert [e for e, s in statuses.items() if s == "discrepancy"] == ["col15", "col17"]
    assert sum(s == "insufficient_evidence" for s in statuses.values()) == 8
    for entry in entries:
        expect(row(page, run_id, entry).locator(".c-status .badge")).not_to_be_empty()

    # A verified zero is a value; an unknown expectation is not zero.
    expect(cell(page, run_id, "col4", "expected").locator(".val")).to_have_attribute("data-value", "0.00")
    expect(cell(page, run_id, "col4", "expected")).to_have_text("0,00")
    expect(row(page, run_id, "col4").locator(".ind-unit")).to_have_text("руб.")
    expect(cell(page, run_id, "col3", "expected").locator(".val-null")).to_have_text("неизвестно")
    expect(cell(page, run_id, "col3", "diff").locator(".val-null")).to_have_text("неизвестно")
    expect(cell(page, run_id, "col3", "reported")).to_have_text("34,14")
    expect(row(page, run_id, "col3").locator(".ind-unit")).to_have_text("чел.-мес.")
    expect(cell(page, run_id, "col6", "observed")).to_contain_text("частичная сумма")
    expect(cell(page, run_id, "col6", "expected").locator(".val-null")).to_be_visible()

    # One underlying +12 500 error: shown on column 15 and on the total, explained, never added up.
    for entry in ("col15", "col17"):
        expect(cell(page, run_id, entry, "diff").locator(".val")).to_have_attribute("data-value", "12500.00")
        expect(cell(page, run_id, entry, "diff")).to_have_text(f"+12{NB}500,00")
    expect(cell(page, run_id, "col17", "reported")).to_have_text(f"11{NB}580{NB}377,40")
    conclusion = run_block(page, run_id).locator(".result > .conclusion")
    expect(conclusion.locator(".components li")).to_have_count(1)
    expect(conclusion.locator(".components li")).to_have_attribute("data-entry", "col15")
    expect(conclusion.locator(".g-diff .val")).to_have_attribute("data-value", "12500.00")
    expect(conclusion.locator(".g-diff")).to_have_text(f"+12{NB}500,00{NB}руб.")
    expect(conclusion.locator(".g-explained")).to_contain_text("Гр. 15")
    expect(conclusion.locator(".g-unexplained .val")).to_have_attribute("data-value", "0.00")
    expect(conclusion.locator(".grand")).to_contain_text("не складываются")
    text = run_block(page, run_id).inner_text()
    assert "25" + NB + "000" not in text and "25000" not in text
    fonts_ready(page)
    table = run_block(page, run_id).locator(".result > .table-wrap")
    assert table.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), "all columns visible without sideways scroll"
    if page.viewport_size["width"] >= 1440:
        display = row(page, run_id, "col15").evaluate("el => getComputedStyle(el).display")
        assert display == "table-row", "desktop shows the indicators as a table"
    exclusions = run_block(page, run_id).locator(".result > .exclusions li")
    expect(exclusions).to_have_count(4)
    expect(run_block(page, run_id).locator(".result > .exclusions")).to_contain_text("дубликат")
    expect(run_block(page, run_id).locator(".result")).to_contain_text("Нерешенных документов нет")


# ======================= tests =======================


def test_upload_confirm_run_results_and_refresh_restore(env):
    page = env.goto()
    expect(page.locator("#ws-status")).to_contain_text("Сверка не выбрана")
    expect(page.locator("#run-start")).to_be_disabled()
    page.click("#ws-new")
    expect(page).to_have_url(re.compile(r"/app#ws_[A-Za-z0-9_-]+$"))
    ws = page.url.rsplit("#", 1)[1]
    expect(page.locator("#ws-status .ws-title")).not_to_be_empty()
    env.shot("12-new-reconciliation")

    report_png = png(REPORT_RENDER)
    page.set_input_files(
        "#up-report", files=[{"name": "01_Карточка.png", "mimeType": "image/png", "buffer": report_png}]
    )
    page.click('form[data-role="report"] button[type="submit"]')
    expect(page.locator('form[data-role="report"] .upload-status')).to_contain_text("Загружено 1 файл")

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("набор/05_Счет-акт_174.pdf", b"%PDF-sa174")
        zf.writestr("набор/02_01_Ведомость.xlsx", b"xlsx-jan")
    page.set_input_files(
        "#up-primary", files=[{"name": "набор.zip", "mimeType": "application/zip", "buffer": bundle.getvalue()}]
    )
    page.click('form[data-role="primary"] button[type="submit"]')
    expect(page.locator('form[data-role="primary"] .upload-status')).to_contain_text("Загружено 2 файла")
    # Same file name, different content: a separate document, not a replacement.
    page.set_input_files(
        "#up-primary", files=[{"name": "05_Счет-акт_174.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-other"}]
    )
    page.click('form[data-role="primary"] button[type="submit"]')
    expect(page.locator('form[data-role="primary"] .upload-status')).to_contain_text("Загружено 1 файл")
    page.set_input_files(
        "#up-context", files=[{"name": "00_README.txt", "mimeType": "text/plain", "buffer": b"readme"}]
    )
    page.click('form[data-role="context"] button[type="submit"]')
    expect(page.locator('form[data-role="context"] .upload-status')).to_contain_text("Загружено 1 файл")
    page.set_input_files("#up-primary", files=[{"name": "bad.zip", "mimeType": "application/zip", "buffer": b"junk"}])
    page.click('form[data-role="primary"] button[type="submit"]')
    expect(page.locator('form[data-role="primary"] .upload-status')).to_contain_text("Не загружено")
    expect(page.locator('form[data-role="primary"] .upload-status')).to_contain_text("bad.zip")

    versions = env.store.list_versions(ws)
    assert [v.role for v in versions] == ["report", "primary", "primary", "primary", "context"]
    report = versions[0]
    same_name = [v for v in versions if v.original_filename == "05_Счет-акт_174.pdf"]
    assert len(same_name) == 2 and same_name[0].document_id != same_name[1].document_id
    docs = page.locator("#doc-list")
    expect(docs.locator("li.doc")).to_have_count(5)
    expect(docs.locator(".note")).to_have_count(2)  # both same-name documents are flagged as distinct
    for v in versions:
        version = docs.locator(f'.version[data-version-id="{v.version_id}"]')
        expect(version).to_contain_text(v.original_filename)
        expect(version).to_contain_text("версия 1 из 1")
    expect(docs.locator('input[data-role="report"]:checked')).to_have_count(1)
    # Leave the second same-name upload out of the check; identity is the version ID, not the file name.
    docs.locator(f'input[data-version-id="{same_name[1].version_id}"]').uncheck()

    expect(page.locator("#run-blockers")).to_contain_text("Укажите договор")
    fill_scope(page, confirm=False)
    expect(page.locator("#run-start")).to_be_disabled()
    expect(page.locator("#run-blockers")).to_contain_text("Подтвердите параметры")
    page.check("#scope-confirm")
    expect(page.locator("#run-start")).to_be_enabled()
    page.fill("#scope-to", "2026-05-30")  # any edit withdraws the confirmation
    expect(page.locator("#scope-confirm")).not_to_be_checked()
    expect(page.locator("#run-start")).to_be_disabled()
    page.fill("#scope-from", "2026-06-01")
    page.check("#scope-confirm")
    expect(page.locator("#run-blockers")).to_contain_text("Начало периода позже")
    fill_scope(page)
    expect(page.locator("#run-start")).to_be_enabled()

    env.launcher.plan.append(completes_with("baseline_result.json"))
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/runs") else None)
    page.locator("#run-start").dblclick()
    block = page.locator(".run-block")
    expect(block).to_have_count(1)
    expect(block.locator(".run-head .badge").first).to_have_text(re.compile("Завершена"))
    assert len(posts) == 1, "double click starts one check"
    sent = json.loads(posts[0].post_data)
    assert sent["scope"] == {**SCOPE, "coverage_basis": "supplied_bundle", "confirmed": True}
    expected_ids = [report.version_id] + [v.version_id for v in versions[1:] if v.version_id != same_name[1].version_id]
    assert sent["version_ids"] == expected_ids and sent["predecessor_run_id"] is None
    run_id = block.get_attribute("data-run-id")
    baseline_checks(page, run_id)
    expect(page.locator("#followup-run")).to_have_value(run_id)
    expect(page.locator("#run-start")).to_be_disabled()  # the next check needs a fresh confirmation
    assert env.store.list_messages(ws) == [], "completion commentary does not create a saved user conversation"
    expect(chat_messages(page)).to_have_count(0)
    expect(block.locator(".run-completion")).to_have_count(1)
    assert_no_internal_details(page)
    env.shot("01-baseline-result")

    page.reload()
    expect(page).to_have_url(f"{env.base}/app#{ws}")
    expect(page.locator("#doc-list li.doc")).to_have_count(5)
    expect(page.locator(f'#doc-list input[data-version-id="{report.version_id}"]')).to_be_checked()
    expect(page.locator(f'#doc-list input[data-version-id="{same_name[1].version_id}"]')).not_to_be_checked()
    expect(page.locator("#scope-contract")).to_have_value(SCOPE["contract"])
    expect(page.locator("#scope-confirm")).not_to_be_checked()
    expect(page.locator(".run-block")).to_have_count(1)
    expect(page.locator("#followup-run")).to_have_value(run_id)
    baseline_checks(page, run_id)


def test_evidence_regions_cells_docx_missing_and_old_version(env):
    store = env.store
    ws, versions = seed_workspace(store)
    renders = seed_evidence(store, ws, versions)
    run_id = seed_run(store, ws, versions)
    page = env.goto(ws)
    baseline_checks(page, run_id)

    detail = expand(page, run_id, "col15")
    expect(detail.locator(".contributions tbody tr")).to_have_count(4)
    expect(detail.locator(".breakdown .terms")).to_have_text(
        f"245{NB}000,00 + 318{NB}298,10 + 270{NB}000,00 + 300{NB}000,00"
    )
    expect(detail.locator(".breakdown > .val")).to_have_attribute("data-value", "1133298.10")

    panel = open_evidence(page, detail, "blk_kfz_col15")
    expect(panel.locator(".ev-doc")).to_have_attribute("data-version-id", versions["report"].version_id)
    expect(panel.locator("h3.fname")).to_have_text(versions["report"].original_filename)
    expect(panel.locator("figcaption")).to_contain_text("Страница 1")
    expect(panel.locator(".ev-text .quote")).to_have_text("1 145 798,10")
    expect(panel.locator(".ev-text h4")).to_have_text("Распознанный текст")
    expect(panel).to_contain_text("уверенность 92%")
    expect(panel).not_to_contain_text("91.53")
    assert_highlight(page, (2050, 842, 2175, 868), REPORT_RENDER)

    sa174 = versions["ver_sa174"]
    panel = open_evidence(page, detail, "blk_sa174_line0")
    expect(panel.locator(".ev-doc")).to_have_attribute("data-version-id", sa174.version_id)
    expect(panel.locator("h3.fname")).to_have_text(sa174.original_filename)
    expect(panel.locator(".ev-text h4")).to_have_text("Текст фрагмента")  # native PDF text was not recognized
    expect(panel.locator("img.page-img")).to_have_attribute(
        "src", f"/api/workspaces/{ws}/artifacts/{renders['sa174'].artifact_id}"
    )
    expect(panel.locator(".original a")).to_have_attribute(
        "href", f"/api/workspaces/{ws}/artifacts/{sa174.original_artifact_id}"
    )
    expect(panel).not_to_contain_text("указывает на версию")  # the contribution and the block name the same version
    wide = assert_highlight(page, (142, 509, 605, 531), ACT_RENDER)
    env.shot("02-evidence-page-region")
    page.set_viewport_size({"width": 1100, "height": 900})  # evidence becomes a drawer of another width
    expect(page.locator("#evidence")).to_be_visible()
    narrow = assert_highlight(page, (142, 509, 605, 531), ACT_RENDER)
    assert abs(narrow["width"] - wide["width"]) > 20, "the check covered two display sizes"
    page.locator("#evidence-body .ev-region button[aria-pressed]").click()  # 100% zoom
    zoomed = assert_highlight(page, (142, 509, 605, 531), ACT_RENDER)
    assert abs(zoomed["width"] - ACT_RENDER[0]) <= 1.5
    page.click("#evidence-close")
    expect(page.locator("#evidence")).to_be_hidden()
    page.set_viewport_size({"width": 1440, "height": 900})

    panel = open_evidence(page, detail, "blk_upd77_line0")
    expect(panel.locator("figcaption")).to_contain_text("повернута на 90°")
    assert_highlight(page, (1100, 820, 1500, 870), ROTATED_RENDER)

    panel = open_evidence(page, detail, "blk_kl31_line0")
    expect(panel.locator(".docx-quote")).to_have_text("Сумма расходов: 270 000,00 руб.")
    expect(panel.locator(".docx-address")).to_contain_text("Абзац 4")
    expect(panel.locator(".original a")).to_have_text("Скачать исходный файл")
    expect(panel.locator("img")).to_have_count(0)

    panel = open_evidence(page, expand(page, run_id, "col16"), "blk_it0326_line0")
    expect(panel.locator(".docx-quote")).to_have_text("703 244,51")
    expect(panel.locator(".docx-address")).to_contain_text("Таблица 1, строка 2, ячейка 2")
    env.shot("03-evidence-docx")

    panel = open_evidence(page, expand(page, run_id, "col7"), "blk_pay01_line0")
    expect(panel.locator(".cell-sheet")).to_have_text("Ведомость")
    expect(panel.locator(".cell-ref")).to_have_text("C8")
    expect(panel.locator(".cell-formula")).to_have_text("=SUM(C6:C7)")
    expect(panel.locator(".cell-cached")).to_have_text("не сохранено в файле")
    expect(panel.locator(".cell-derived .val")).to_have_attribute("data-value", "1010000.00")
    expect(panel.locator(".cell-evaluation")).to_have_attribute("data-evaluation", "derived")
    expect(panel.locator(".cell-inputs")).to_have_text("C6 C7")
    env.shot("04-evidence-cell")
    panel = open_evidence(page, expand(page, run_id, "col5"), "blk_pay01_line1")
    expect(panel.locator(".cell-cached")).to_have_text("100000")
    expect(panel.locator(".cell-derived")).to_have_text("не вычислялось")
    expect(panel.locator(".cell-evaluation")).to_have_attribute("data-evaluation", "unresolved")
    expect(panel.locator(".cell-inputs")).to_contain_text("Ставки!B2")

    # Evidence that was never stored stays missing; nothing else is substituted.
    panel = open_evidence(page, expand(page, run_id, "col3"), "blk_kfz_col3")
    expect(panel.locator('[data-evidence-error="not_found"]')).to_contain_text("не найден")
    expect(panel.locator("img, .ev-doc")).to_have_count(0)
    panel = open_evidence(page, expand(page, run_id, "col15"), "blk_act118_line0")
    expect(panel.locator('[data-evidence-error="not_found"]')).to_be_visible()

    # Replace the act through the UI, then reopen the old result's evidence: it stays on the old version.
    open_setup(page)
    doc = page.locator(f'#doc-list li.doc[data-document-id="{sa174.document_id}"]')
    doc.get_by_role("button", name="Загрузить новую версию").click()
    doc.locator('input[type="file"]').set_input_files(
        files=[{"name": "05_Счет-акт_174_исправленный.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-v2"}]
    )
    doc.get_by_role("button", name="Загрузить как новую версию").click()
    expect(doc.locator(".version.superseded")).to_have_count(1)
    v2 = env.store.list_versions(ws)[-1]
    assert v2.predecessor_version_id == sa174.version_id and v2.document_id == sa174.document_id
    expect(doc.locator(f'input[data-version-id="{v2.version_id}"]')).to_be_checked()
    panel = open_evidence(page, expand(page, run_id, "col15"), "blk_sa174_line0")
    expect(panel.locator(".ev-doc")).to_have_attribute("data-version-id", sa174.version_id)
    expect(panel.locator(".ev-doc")).to_have_attribute("data-newer-version-id", v2.version_id)
    expect(panel.locator(".ev-meta")).to_contain_text("версия 1 из 2")
    expect(panel.locator(".ev-newer")).to_contain_text("Есть более новая версия этого документа")
    expect(panel.locator("img.page-img")).to_have_attribute(
        "src", f"/api/workspaces/{ws}/artifacts/{renders['sa174'].artifact_id}"
    )
    expect(panel.locator(".original a")).to_have_attribute(
        "href", f"/api/workspaces/{ws}/artifacts/{sa174.original_artifact_id}"
    )
    assert_highlight(page, (142, 509, 605, 531), ACT_RENDER)
    assert_no_internal_details(page)
    env.shot("05-evidence-old-version")


def test_evidence_new_tab_retains_highlight_zoom_crop_and_saved_version(env):
    store = env.store
    ws, versions = seed_workspace(store)
    renders = seed_evidence(store, ws, versions)
    original = versions["ver_sa174"]
    cropped_box, cropped_size = (120, 230, 490, 260), (900, 600)
    cropped = seed_region(
        store,
        ws,
        original.version_id,
        "blk_rotated_crop",
        cropped_box,
        cropped_size,
        "На повернутом фрагменте: 987 654,32 руб.",
        rotation=90,
        crop={"x0": 210, "y0": 350, "x1": 1110, "y1": 950},
    )
    run_id = seed_run(store, ws, versions)
    message = c.ChatMessage(
        workspace_id=ws,
        message_id=c.new_id("msg"),
        run_id=run_id,
        role="assistant",
        text="Дополнительный фрагмент того же документа.",
        evidence_ids=["blk_rotated_crop"],
        created_at=_now(),
    )
    store.append_message(message)
    newer = store.save_document(
        ws,
        filename="05_Счет-акт_174_новая_версия.pdf",
        role="primary",
        data=b"replacement-not-the-evidence-render",
        document_id=original.document_id,
    )
    before = [
        [item.model_dump_json() for item in getter(ws)]
        for getter in (store.list_versions, store.list_runs, store.list_messages)
    ]
    page = env.goto(ws)
    requests = []
    page.context.on("request", lambda request: requests.append(request))
    cases = [
        ("blk_sa174_line0", original, renders["sa174"], (142, 509, 605, 531), ACT_RENDER, "318 298,10"),
        (
            "blk_upd77_line0",
            versions["ver_upd77"],
            renders["upd77"],
            (1100, 820, 1500, 870),
            ROTATED_RENDER,
            "300 000,00",
        ),
        ("blk_rotated_crop", original, cropped, cropped_box, cropped_size, "987 654,32"),
    ]
    for index, (block_id, version, render, box, size, excerpt) in enumerate(cases):
        source = chat_messages(page) if block_id == "blk_rotated_crop" else expand(page, run_id, "col15")
        panel = open_evidence(page, source, block_id)
        original_href = panel.locator(".original a").get_attribute("href")
        link = panel.get_by_role("link", name="Открыть страницу в новой вкладке")
        viewer_path = f"/static/evidence.html#workspace={ws}&block={block_id}&version={version.version_id}"
        expect(link).to_have_attribute("href", viewer_path)
        with page.expect_popup() as opened:
            link.click()
        popup = opened.value
        expect(popup.locator("#evidence-body")).to_have_attribute("data-state", "ready")
        expect(popup.locator("h1.fname")).to_have_text(version.original_filename)
        expect(popup.locator(".ev-doc")).to_have_attribute("data-version-id", version.version_id)
        expect(popup.locator(".ev-doc")).not_to_have_attribute("data-version-id", newer.version_id)
        expect(popup.locator("figcaption")).to_contain_text("Страница 1")
        expect(popup.locator(".ev-text .quote")).to_contain_text(excerpt)
        expect(popup.locator(".page-img")).to_have_attribute(
            "src", f"/api/workspaces/{ws}/artifacts/{render.artifact_id}"
        )
        expect(popup.locator(".original a")).to_have_attribute("href", original_href)
        assert original_href == f"/api/workspaces/{ws}/artifacts/{version.original_artifact_id}"
        if index:
            expect(popup.locator("figcaption")).to_contain_text("повернута на 90°")
        if block_id == "blk_rotated_crop":
            expect(popup.locator("figcaption")).to_contain_text("часть страницы")
        popup.wait_for_function(
            """() => {
                const image = document.querySelector('.page-img');
                const mark = document.querySelector('.page-mark').getBoundingClientRect();
                const scroller = document.querySelector('.page-scroll').getBoundingClientRect();
                return image.complete && image.naturalWidth > 0
                    && mark.top >= scroller.top && mark.bottom <= scroller.bottom;
            }"""
        )
        expect(popup.locator("h1.fname")).to_be_in_viewport()
        assert popup.locator("h1.fname").bounding_box()["y"] >= 0
        if index == 0:
            popup.wait_for_function(
                """() => {
                    const container = document.querySelector('.page-scroll');
                    const scroller = container.getBoundingClientRect();
                    const mark = document.querySelector('.page-mark').getBoundingClientRect();
                    return container.scrollTop > 0
                        && Math.abs((mark.top + mark.bottom) / 2 - (scroller.top + scroller.bottom) / 2) < 3;
                }"""
            )
        fitted = assert_highlight(popup, box, size)
        assert fitted["width"] <= popup.viewport_size["width"]
        for zoom in ("100", "150", "200"):
            popup.select_option("#page-zoom", zoom)
            displayed = assert_highlight(popup, box, size)
            assert abs(displayed["width"] - size[0] * int(zoom) / 100) <= 1.5
        popup.select_option("#page-zoom", "fit")
        popup.set_viewport_size({"width": 900, "height": 800})
        assert_highlight(popup, box, size)
        assert_no_internal_details(popup)
        if target := os.environ.get("SVERKA_UI_SCREENSHOTS"):
            Path(target).mkdir(parents=True, exist_ok=True)
            popup.screenshot(path=str(Path(target) / f"10-evidence-tab-{index + 1}.png"), full_page=True)
        assert all(request.method == "GET" for request in requests if request.frame.page == popup)
        popup.close()
    after = [
        [item.model_dump_json() for item in getter(ws)]
        for getter in (store.list_versions, store.list_runs, store.list_messages)
    ]
    assert after == before, "opening and zooming evidence never rewrites documents, runs or chat"


def test_evidence_new_tab_rejects_missing_wrong_version_and_unsafe_links(env):
    store = env.store
    ws, versions = seed_workspace(store)
    seed_evidence(store, ws, versions)
    version = versions["ver_sa174"]
    page = env.page
    endpoint = f"/api/workspaces/{ws}/evidence/blk_sa174_line0"
    view = page.request.get(env.base + endpoint).json()
    viewer = env.base + f"/static/evidence.html#workspace={ws}&block=blk_sa174_line0&version={version.version_id}"

    def open_viewer(url=viewer):
        page.goto("about:blank")
        page.goto(url)

    # The original-file and image URLs are both constrained to scoped, same-origin artifact paths.
    hostile_urls = [
        "javascript:window.__evidence_xss=1",
        "//attacker.invalid/collect",
        f"/api/workspaces/{ws}/artifacts/../evidence/blk_sa174_line0",
        "/api/workspaces/ws_other/artifacts/art_other",
    ]
    for field in ("original_url", "artifact_url"):
        for url in hostile_urls:
            damaged = {**view, field: url}
            page.route(
                "**" + endpoint,
                lambda route, request, damaged=damaged: route.fulfill(json=damaged),
            )
            open_viewer()
            expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
            expect(page.locator("#evidence-body [role='alert']")).to_be_visible()
            expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
            assert page.evaluate("() => window.__evidence_xss") is None
            assert_no_internal_details(page)
            page.unroute("**" + endpoint)

    malformed_identity = [
        {
            **view,
            "original_url": f"/api/workspaces/{ws}/artifacts/{versions['report'].original_artifact_id}",
        },
        {**view, "artifact_url": view["original_url"]},
        {**view, "version": {**view["version"], "workspace_id": "ws_other"}},
    ]
    for damaged in malformed_identity:
        page.route("**" + endpoint, lambda route, request, damaged=damaged: route.fulfill(json=damaged))
        open_viewer()
        expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
        expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
        assert_no_internal_details(page)
        page.unroute("**" + endpoint)

    wrong = {**view, "version": {**view["version"], "version_id": versions["report"].version_id}}
    page.route("**" + endpoint, lambda route: route.fulfill(json=wrong))
    open_viewer()
    expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
    expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
    assert_no_internal_details(page)
    page.unroute("**" + endpoint)

    open_viewer(viewer.replace(version.version_id, versions["report"].version_id))
    expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
    expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
    assert_no_internal_details(page)

    open_viewer(viewer.replace("blk_sa174_line0", "blk_missing"))
    expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
    expect(page.locator("#evidence-body")).to_contain_text("не найден")
    expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
    assert_no_internal_details(page)

    open_viewer(env.base + "/static/evidence.html#workspace=//attacker.invalid&block=blk_missing&version=ver_missing")
    expect(page.locator("#evidence-body")).to_have_attribute("data-state", "error")
    expect(page.locator(".page-img, .page-mark, .original a")).to_have_count(0)
    assert_no_internal_details(page)


def test_evidence_new_tab_image_failures_keep_excerpt_and_original_available(env):
    store = env.store
    ws, versions = seed_workspace(store)
    renders = seed_evidence(store, ws, versions)
    version = versions["ver_sa174"]
    page = env.page
    endpoint = f"/api/workspaces/{ws}/evidence/blk_sa174_line0"
    view = page.request.get(env.base + endpoint).json()
    viewer = env.base + f"/static/evidence.html#workspace={ws}&block=blk_sa174_line0&version={version.version_id}"
    original_url = f"/api/workspaces/{ws}/artifacts/{version.original_artifact_id}"
    artifact_url = f"/api/workspaces/{ws}/artifacts/{renders['sa174'].artifact_id}"

    def open_viewer():
        page.goto("about:blank")
        page.goto(viewer)

    page.route("**" + endpoint, lambda route: route.fulfill(json={**view, "artifact_url": None}))
    open_viewer()
    expect(page.locator("#evidence-body")).to_contain_text("не сохранено")
    expect(page.locator(".page-mark")).to_have_count(0)
    expect(page.locator(".ev-text .quote")).to_contain_text("318 298,10")
    expect(page.locator(".original a")).to_have_attribute("href", original_url)
    page.unroute("**" + endpoint)

    page.route("**" + artifact_url, lambda route: route.fulfill(status=404, body="missing"))
    open_viewer()
    expect(page.locator("#evidence-body")).to_contain_text("не загрузилось")
    expect(page.locator(".page-mark")).to_be_hidden()
    expect(page.locator(".ev-text .quote")).to_contain_text("318 298,10")
    expect(page.locator(".original a")).to_have_attribute("href", original_url)
    page.unroute("**" + artifact_url)

    page.route(
        "**" + artifact_url,
        lambda route: route.fulfill(content_type="image/png", body=png((400, 200))),
    )
    open_viewer()
    expect(page.locator("#evidence-body")).to_contain_text(re.compile("размер", re.IGNORECASE))
    expect(page.locator(".page-mark")).to_be_hidden()
    expect(page.locator(".ev-text .quote")).to_contain_text("318 298,10")
    expect(page.locator(".original a")).to_have_attribute("href", original_url)
    assert_no_internal_details(page)


def test_missing_march_keeps_partial_values_unknown(env):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = seed_run(store, ws, versions, fixture="missing_march_result.json")
    page = env.goto(ws)
    rows = run_block(page, run_id).locator("tr.ind-row")
    expect(rows).to_have_count(15)
    for entry, observed in (("col5", "4456674.83"), ("col17", "10466077.40")):
        expect(cell(page, run_id, entry, "observed").locator(".val")).to_have_attribute("data-value", observed)
        expect(cell(page, run_id, entry, "observed")).to_contain_text("частичная сумма")
        expect(cell(page, run_id, entry, "expected").locator(".val-null")).to_have_text("неизвестно")
        expect(cell(page, run_id, entry, "diff").locator(".val-null")).to_have_text("неизвестно")
        expect(row(page, run_id, entry)).to_have_attribute("data-status", "insufficient_evidence")
    expect(cell(page, run_id, "col5", "observed")).to_contain_text(f"4{NB}456{NB}674,83")
    expect(cell(page, run_id, "col15", "diff").locator(".val")).to_have_attribute("data-value", "12500.00")
    conclusion = run_block(page, run_id).locator(".result > .conclusion")
    expect(conclusion.locator(".components li")).to_have_count(1)
    expect(conclusion.locator(".components li")).to_contain_text(f"+12{NB}500,00")
    expect(conclusion.locator(".grand .badge")).to_have_text(re.compile("Недостаточно данных"))
    expect(conclusion.locator(".g-diff .val-null")).to_have_text("неизвестно")
    expect(conclusion.locator(".g-unexplained .val-null")).to_have_text("не определена")
    expect(conclusion.locator(".grand")).to_contain_text("Итог не подтвержден")
    expect(conclusion.locator(".incomplete")).to_contain_text("Гр. 5")
    expect(conclusion.locator(".incomplete")).to_contain_text("Гр. 17")
    detail = expand(page, run_id, "col5")
    expect(detail).to_contain_text("это не полное ожидаемое значение")
    expect(detail.locator(".gaps li")).to_have_count(1)
    expect(detail.locator(".contributions tbody tr")).to_have_count(8)
    env.shot("06-missing-march")


def test_completion_commentary_once_and_report_collapse(env):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = queued_run(store, ws, versions)
    page = env.goto(ws)
    block = run_block(page, run_id)
    expect(block.locator(".run-head")).to_contain_text("В очереди")
    expect(block.locator(".run-completion")).to_have_count(0)

    # A real polling transition adds one assistant commentary for the saved result.
    store.save_run(completed(store.get_run(ws, run_id), "baseline_result.json", version_map(versions)))
    commentary = block.locator(".run-completion")
    expect(commentary).to_have_count(1)
    expect(commentary).to_contain_text("Проверка №1 завершена.")
    expect(commentary).to_contain_text("Показателей: 15. Подтверждено: 5; расхождений: 2; недостаточно данных: 8.")
    commentary.get_by_role("button", name="Объяснить расхождения").click()
    expect(page.locator("#composer-text")).to_have_value(
        "Объясни найденные расхождения и покажи подтверждающие документы."
    )
    expect(page.locator("#composer-send")).to_have_accessible_name("Отправить вопрос")
    expect(page.locator("#composer-send svg")).to_have_count(1)
    textarea, send = page.locator("#composer-text").bounding_box(), page.locator("#composer-send").bounding_box()
    assert send["x"] >= textarea["x"] + textarea["width"] - 1
    assert abs((send["y"] + send["height"] / 2) - (textarea["y"] + textarea["height"] / 2)) < 35
    page.wait_for_timeout(1800)
    expect(commentary).to_have_count(1)
    assert store.list_messages(ws) == []

    report = block.locator(".result")
    toggle = report.locator(".report-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(row(page, run_id, "col15")).to_be_visible()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_contain_text("Показать отчет: 15 показателей")
    expect(row(page, run_id, "col15")).to_be_hidden()
    expect(report.locator(".verdict")).to_be_visible()
    expect(commentary).to_be_visible()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(row(page, run_id, "col15")).to_be_visible()
    expect(cell(page, run_id, "col15", "diff").locator(".val")).to_have_attribute("data-value", "12500.00")

    # Restoration derives the same commentary without posting a duplicate chat message.
    page.reload()
    expect(block.locator(".run-completion")).to_have_count(1)
    expect(block.locator(".run-completion")).to_contain_text("Проверка №1 завершена.")
    expect(chat_messages(page)).to_have_count(0)
    assert store.list_messages(ws) == []
    assert_no_internal_details(page)


def test_assistant_markdown_is_readable_and_safe(env):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = seed_run(store, ws, versions)
    message = c.ChatMessage(
        workspace_id=ws,
        message_id=c.new_id("msg"),
        run_id=run_id,
        role="assistant",
        text=(
            "## Вывод\n\n**Суммы** *нужно уточнить*.\n\n- Первый пункт\n- Второй пункт\n\n"
            '<img src=x onerror="window.__markdown_xss=1">\n\n'
            "[опасная ссылка](javascript:window.__markdown_xss=2)"
        ),
        created_at=_now(),
    )
    store.append_message(message)
    page = env.goto(ws)
    text = page.locator(f'.msg[data-message-id="{message.message_id}"] .msg-text')
    expect(text.locator("h2, h3, h4")).to_have_text("Вывод")
    expect(text.locator("strong")).to_have_text("Суммы")
    expect(text.locator("em")).to_have_text("нужно уточнить")
    expect(text.locator("ul li")).to_have_text(["Первый пункт", "Второй пункт"])
    expect(text).to_contain_text('<img src=x onerror="window.__markdown_xss=1">')
    expect(text.locator("img, script, iframe, a[href^='javascript:']")).to_have_count(0)
    assert page.evaluate("() => window.__markdown_xss") is None


def test_legacy_assistant_json_is_readable_without_rewriting_saved_messages(env):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = seed_run(store, ws, versions)
    precise = "0.1234567890123456789012345678901234567890"
    label = next(item.label for item in store.get_run(ws, run_id).result.indicators if item.entry_id == "col15")
    answer = (
        f"Для col15: amount_mismatch. Разница 12 500,00 руб. Точное значение {precise}. "
        "Дополнительный расход 9876543.21 руб."
    )
    envelope = json.dumps(
        {
            "text": answer,
            "entry_ids": ["col15"],
            "evidence_ids": ["blk_sa174_line0"],
        },
        ensure_ascii=False,
    )
    texts = [
        envelope,
        answer + "\n\nЧерновой анализ перед ответом.\n\n" + envelope,
        "<think>Внутренняя заметка col15 amount_mismatch</think>\n" + envelope,
        '{"text": ' + json.dumps(answer, ensure_ascii=False) + ', "entry_ids": [',
    ]
    for text in texts:
        store.append_message(
            c.ChatMessage(
                workspace_id=ws,
                message_id=c.new_id("msg"),
                run_id=run_id,
                role="assistant",
                text=text,
                created_at=_now(),
            )
        )
    before = [message.model_dump_json() for message in store.list_messages(ws)]
    page = env.goto(ws)
    for _ in range(2):
        messages = chat_messages(page)
        expect(messages).to_have_count(len(texts))
        for reply in messages.all():
            prose = reply.locator(".msg-text")
            expect(prose).to_contain_text(label)
            expect(prose).to_contain_text("суммы не совпадают")
            expect(prose).to_contain_text("12 500,00")
            expect(prose).to_contain_text(precise)
            expect(prose).not_to_contain_text(re.compile(r"col15|amount_mismatch|entry_ids|evidence_ids|\{\s*\"text\""))
            expect(prose).not_to_contain_text("Внутренняя заметка")
            expect(prose).not_to_contain_text("Черновой анализ")
            expect(prose.locator("think, script, iframe")).to_have_count(0)
            expect(prose).to_contain_text("9876543.21")
            assert prose.inner_text().count("Разница 12 500,00 руб.") == 1
        assert [message.model_dump_json() for message in store.list_messages(ws)] == before
        page.reload()


def test_backend_messages_and_failures_keep_internal_details_private(env):
    store = env.store
    ws, versions = seed_workspace(store)
    seed_evidence(store, ws, versions)
    version = versions["ver_sa174"]
    # A contribution's label is also used as the evidence button's accessible name.
    raw_label = f"Акт {version.version_id} SHA256 {version.sha256}"
    run_id = seed_run(
        store,
        ws,
        versions,
        edit=lambda text: text.replace("Акт № 118 от 28.01.2026", raw_label),
    )
    identifiers = " ".join(
        [ws, run_id, version.version_id, version.document_id, version.original_artifact_id, "blk_sa174_line0"]
    )
    for role, text in (
        ("assistant", f"Документы проверены: {identifiers}. SHA256: {version.sha256}. Коммит a1b2c3d."),
        ("system", f"Ответ не получен (RuntimeError): run_id={run_id}, provider_error, {version.sha256}."),
    ):
        store.append_message(
            c.ChatMessage(
                workspace_id=ws,
                message_id=c.new_id("msg"),
                run_id=run_id,
                role=role,
                text=text,
                evidence_ids=["blk_sa174_line0"],
                created_at=_now(),
            )
        )
    failed_id = queued_run(store, ws, versions)
    failed_run = store.get_run(ws, failed_id)
    store.save_run(
        failed_run.model_copy(
            update={
                "status": "failed",
                "failure": c.RunFailure(
                    code="provider_error",
                    message=f"RuntimeError provider_error: workspace_id={ws}, run_id={failed_id}, SHA256 {version.sha256}",
                ),
                "updated_at": _now(),
            }
        )
    )
    page = env.goto(ws)
    expect(chat_messages(page)).to_have_count(2)
    expect(run_block(page, failed_id).locator(".alert-bad")).to_be_visible()
    detail = expand(page, run_id, "col15")
    expect(detail.locator('button.ev-btn[data-block-id="blk_act118_line0"]')).to_have_attribute(
        "aria-label", re.compile("Акт")
    )
    reply = chat_messages(page).filter(has=page.locator(".msg-text", has_text="Документы проверены"))
    open_evidence(page, reply, "blk_sa174_line0")
    expect(page.locator("#evidence-body h3.fname")).to_have_text(version.original_filename)
    assert_no_internal_details(page)
    page.reload()
    expect(chat_messages(page)).to_have_count(2)
    assert_no_internal_details(page)


def test_evidence_preserves_large_cell_references_and_precise_decimal_text(env):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = seed_run(store, ws, versions)
    decimal = "0.1234567890123456789012345678901234567890"
    decimal_comma = decimal.replace(".", ",")
    formula = "=SUM(A100000:A100001)"
    source_text = f"Исходное значение {decimal}; в документе {decimal_comma}; формула {formula}"
    store.save_blocks(
        ws,
        [
            c.EvidenceBlock(
                version_id=versions["ver_pay01"].version_id,
                block_id="blk_high_rows",
                text=source_text,
                method="spreadsheet",
                locator={
                    "kind": "cell",
                    "sheet": "Расчет",
                    "ref": "A100002",
                    "formula": formula,
                    "cached_value": decimal,
                    "derived_value": decimal,
                    "evaluation": "derived",
                    "inputs": ["A100000", "A100001"],
                },
            )
        ],
    )
    store.append_message(
        c.ChatMessage(
            workspace_id=ws,
            message_id=c.new_id("msg"),
            run_id=run_id,
            role="assistant",
            text=f"Исходное значение {decimal}; в документе {decimal_comma}; формула `{formula}`.",
            evidence_ids=["blk_high_rows"],
            created_at=_now(),
        )
    )
    page = env.goto(ws)
    reply = chat_messages(page).last
    expect(reply.locator(".msg-text")).to_contain_text(decimal)
    expect(reply.locator(".msg-text")).to_contain_text(decimal_comma)
    expect(reply.locator(".msg-text code")).to_have_text(formula)
    panel = open_evidence(page, reply, "blk_high_rows")
    expect(panel.locator(".cell-ref")).to_have_text("A100002")
    expect(panel.locator(".cell-formula")).to_have_text(formula)
    expect(panel.locator(".cell-cached")).to_have_text(decimal)
    expect(panel.locator(".cell-derived .val")).to_have_attribute("data-value", decimal)
    expect(panel.locator(".cell-derived .val")).to_have_text(decimal_comma)
    expect(panel.locator(".cell-inputs")).to_have_text("A100000 A100001")
    expect(panel.locator(".cell-text")).to_have_text(source_text)


def test_lost_message_response_recovers_a_delayed_reply_without_reposting(env, monkeypatch):
    store = env.store
    ws, versions = seed_workspace(store)
    run_id = seed_run(store, ws, versions)
    monkeypatch.setattr(env.services.responder, "respond", lambda _message: [])
    page = env.goto(ws)
    posts = []
    page.on(
        "request",
        lambda request: (
            posts.append(request) if request.method == "POST" and request.url.endswith("/messages") else None
        ),
    )

    def lose_saved_response(route):
        if route.request.method == "POST":
            route.fetch()
            route.abort()
        else:
            route.continue_()

    page.route("**/api/workspaces/*/messages", lose_saved_response)
    page.fill("#composer-text", "Когда появится ответ?")
    page.click("#composer-send")
    expect(chat_messages(page)).to_have_count(1)
    expect(page.locator(".msg.pending")).to_have_count(0)
    assert [message.text for message in store.list_messages(ws)] == ["Когда появится ответ?"]

    # A second draft survives Enter and a send-button click while the first answer is awaited.
    page.fill("#composer-text", "Второй вопрос")
    expect(page.locator("#composer-send")).to_be_disabled()
    page.keyboard.press("Enter")
    send = page.locator("#composer-send").bounding_box()
    page.mouse.click(send["x"] + send["width"] / 2, send["y"] + send["height"] / 2)
    expect(page.locator("#composer-text")).to_have_value("Второй вопрос")
    assert len(posts) == 1

    # RunService can save its own conclusion after this user message; it is not the user's answer.
    result = store.get_run(ws, run_id).result
    store.append_message(
        c.ChatMessage(
            workspace_id=ws,
            message_id=c.new_id("msg"),
            run_id=run_id,
            role="assistant",
            text=result.conclusion.summary,
            cards=[c.ConclusionCard(run_id=run_id, conclusion=result.conclusion)],
            created_at=_now(),
        )
    )
    expect(chat_messages(page)).to_have_count(2)
    expect(page.locator(".msg-user .note")).to_contain_text("Ревизор готовит ответ")
    expect(page.locator("#composer-send")).to_be_disabled()
    expect(page.locator("#composer-text")).to_have_value("Второй вопрос")
    assert len(posts) == 1

    # The saved user message is already reconciled when the assistant's reply arrives later.
    store.append_message(
        c.ChatMessage(
            workspace_id=ws,
            message_id=c.new_id("msg"),
            run_id=run_id,
            role="assistant",
            text="Ответ появился после восстановления связи.",
            created_at=_now(),
        )
    )
    expect(chat_messages(page)).to_have_count(3)
    expect(chat_messages(page).last).to_contain_text("Ответ появился после восстановления связи.")
    expect(page.locator(".msg-user .note")).to_have_count(0)
    expect(page.locator("#composer-send")).to_be_enabled()
    expect(page.locator("#composer-text")).to_have_value("Второй вопрос")
    page.wait_for_timeout(1800)
    expect(chat_messages(page)).to_have_count(3)
    assert len(posts) == 1
    assert len(store.list_messages(ws)) == 3
    page.unroute("**/api/workspaces/*/messages")
    page.reload()
    expect(chat_messages(page)).to_have_count(3)


def test_progress_cancel_conflict_and_failure(env):
    store = env.store
    ws, versions = seed_workspace(store)
    page = env.goto(ws)
    fill_scope(page)
    page.click("#run-start")
    block = page.locator(".run-block").first
    expect(block.locator(".run-head")).to_contain_text("В очереди")
    expect(block.locator(".run-head")).to_contain_text("Активная проверка")
    run_id = block.get_attribute("data-run-id")
    confirm_scope(page)
    expect(page.locator("#run-start")).to_be_disabled()
    expect(page.locator("#run-blockers")).to_contain_text("Уже выполняется проверка №1")

    progress = [
        c.StageProgress(stage="prepare", done=8, total=8, failed=1),
        c.StageProgress(stage="extract", done=3, total=7),
    ]
    run = store.get_run(ws, run_id)
    store.save_run(
        run.model_copy(update={"status": "running", "stage": "extract", "progress": progress, "updated_at": _now()})
    )
    prepare = block.locator('li[data-stage="prepare"]')
    extract = block.locator('li[data-stage="extract"]')
    expect(prepare.locator(".stage")).to_have_text("Подготовка документов")
    expect(prepare.locator(".stage-count")).to_have_text("8 из 8, с ошибкой 1")
    expect(extract.locator(".stage")).to_have_text("Извлечение данных")
    expect(extract.locator(".stage-count")).to_have_text("3 из 7")
    expect(extract).to_have_attribute("data-state", "current")
    expect(block).to_contain_text("Сейчас: извлекаю данные из документов.")
    assert "%" not in block.inner_text()
    env.shot("07-run-progress")

    # A failed poll is reported and retried; the run keeps its last known state meanwhile.
    page.route(f"**/runs/{run_id}", lambda route: route.fulfill(status=503, body="unavailable"))
    expect(block.locator(".run-progress")).to_contain_text("Не удалось обновить состояние")
    expect(block.locator('li[data-stage="extract"] .stage-count')).to_have_text("3 из 7")
    page.unroute(f"**/runs/{run_id}")
    expect(block.locator(".run-progress")).not_to_contain_text("Не удалось обновить состояние", timeout=8000)

    block.get_by_role("button", name="Отменить проверку").click()
    expect(block.locator(".run-head")).to_contain_text("Отменена")
    assert env.launcher.cancelled == [run_id]
    polls = []
    page.on("request", lambda r: polls.append(r.url) if r.url.endswith(f"/runs/{run_id}") else None)
    page.wait_for_timeout(3500)
    assert polls == [], "a finished run is no longer polled"

    # Another client starts a check meanwhile: this page still thinks the workspace is idle.
    other = queued_run(store, ws, versions)
    confirm_scope(page)
    page.click("#run-start")
    expect(page.locator("#run-start-status")).to_contain_text("уже выполняется другая проверка")
    other_block = run_block(page, other)
    expect(other_block.locator(".run-head")).to_contain_text("Активная проверка")
    assert len(store.list_runs(ws)) == 2
    other_block.get_by_role("button", name="Отменить проверку").click()
    expect(other_block.locator(".run-head")).to_contain_text("Отменена")

    env.launcher.plan.append(fails_with("provider_error", "Провайдер вернул 503."))
    confirm_scope(page)
    page.click("#run-start")
    failed = page.locator(".run-block").nth(2)
    expect(failed.locator(".run-head")).to_contain_text("Не выполнена")
    expect(failed.locator(".alert-bad")).to_contain_text("ошибка внешнего сервиса модели")
    expect(failed.locator(".ind-table")).to_have_count(0)
    assert_no_internal_details(page)


def test_followups_bound_to_selected_run_without_duplicates(env):
    store = env.store
    ws, versions = seed_workspace(store)
    baseline = seed_run(store, ws, versions)
    active = queued_run(store, ws, versions)
    page = env.goto(ws)
    expect(run_block(page, active).locator(".run-head")).to_contain_text("Активная проверка")
    page.select_option("#followup-run", baseline)
    expect(run_block(page, baseline).locator(".sel-slot")).to_contain_text("Вопросы задаются по этой проверке")

    # The active check finishes while the user looks at the historical one: the follow-up target stays put.
    store.save_run(completed(store.get_run(ws, active), "missing_march_result.json", version_map(versions)))
    expect(run_block(page, active).locator(".run-head")).to_contain_text("Завершена")
    expect(page.locator("#followup-run")).to_have_value(baseline)
    expect(run_block(page, active).locator("tr.ind-row")).to_have_count(15)

    posted = []
    page.on(
        "request",
        lambda r: (
            posted.append(json.loads(r.post_data)) if r.method == "POST" and r.url.endswith("/messages") else None
        ),
    )
    page.fill("#composer-text", "Почему расходятся общепроизводственные затраты?")
    page.keyboard.press("Enter")
    expect(chat_messages(page)).to_have_count(2)
    assert posted == [{"text": "Почему расходятся общепроизводственные затраты?", "run_id": baseline}]
    saved = store.list_messages(ws)
    assert [m.role for m in saved] == ["user", "assistant"]
    assert [chat_messages(page).nth(i).get_attribute("data-message-id") for i in range(2)] == [
        m.message_id for m in saved
    ]
    reply = page.locator(f'.msg[data-message-id="{saved[1].message_id}"]')
    cards = reply.locator("details.msg-cards")
    expect(cards).not_to_have_attribute("open", "")
    expect(cards.locator("summary")).to_contain_text("15 показателей")
    cards.locator("summary").click()
    expect(cards.locator("tr.ind-row")).to_have_count(15)
    expect(cards.locator('tr.ind-row[data-entry="col15"] td.c-diff .val')).to_have_attribute("data-value", "12500.00")
    expect(run_block(page, baseline).locator("tr.ind-row")).to_have_count(15)
    page.wait_for_timeout(1800)
    expect(chat_messages(page)).to_have_count(2)
    expect(page.locator(".ind-table tr.ind-row")).to_have_count(45)  # two runs plus one reply's cards, once each
    env.shot("08-followup")

    # The request reaches the server but the response is lost: the saved message is found, not posted again.
    def lose_response(route):
        route.fetch()
        route.abort()

    page.route(
        "**/api/workspaces/*/messages",
        lambda route: lose_response(route) if route.request.method == "POST" else route.continue_(),
    )
    page.fill("#composer-text", "Вопрос с потерянным ответом")
    page.click("#composer-send")
    expect(page.locator(".msg-user", has_text="Вопрос с потерянным ответом")).to_have_count(1)
    expect(chat_messages(page)).to_have_count(4)
    expect(page.locator(".msg.pending")).to_have_count(0)
    page.unroute("**/api/workspaces/*/messages")
    assert [m.text for m in store.list_messages(ws) if m.role == "user"].count("Вопрос с потерянным ответом") == 1

    # The request never arrives: the message stays pending with a retry that posts it once.
    page.route(
        "**/api/workspaces/*/messages",
        lambda route: route.abort() if route.request.method == "POST" else route.continue_(),
    )
    page.fill("#composer-text", "Вопрос без связи")
    page.click("#composer-send")
    failed = page.locator('.msg.pending[data-pending="failed"]')
    expect(failed).to_contain_text("Не отправлено")
    assert not any(m.text == "Вопрос без связи" for m in store.list_messages(ws))
    page.unroute("**/api/workspaces/*/messages")
    failed.get_by_role("button", name="Повторить").click()
    expect(page.locator(".msg.pending")).to_have_count(0)
    expect(page.locator(".msg-user", has_text="Вопрос без связи")).to_have_count(1)
    assert [m.text for m in store.list_messages(ws)].count("Вопрос без связи") == 1

    # The model side fails: the API saves a system notice; the user's message is shown once.
    page.fill("#composer-text", "СБОЙ модели")
    page.click("#composer-send")
    expect(page.locator(".msg-system")).to_contain_text("Не удалось получить ответ")
    expect(page.locator(".msg-system")).not_to_contain_text("RuntimeError")
    expect(page.locator(".msg-user", has_text="СБОЙ модели")).to_have_count(1)

    total = len(store.list_messages(ws))
    page.reload()
    expect(chat_messages(page)).to_have_count(total)
    expect(page.locator("#followup-run")).to_have_value(baseline)
    ids = [chat_messages(page).nth(i).get_attribute("data-message-id") for i in range(total)]
    assert ids == [m.message_id for m in store.list_messages(ws)] and len(set(ids)) == total
    assert_no_internal_details(page)


def test_hostile_text_is_inert_and_errors_are_explained(env, monkeypatch):
    store = env.store
    ws = store.create_workspace().workspace_id
    report = store.save_document(ws, filename='<img src=x onerror="window.__xss=1">.pdf', role="report", data=b"r")
    primary = store.save_document(ws, filename="<b title=x>акт.pdf", role="primary", data=b"p")
    seed_region(
        store, ws, primary.version_id, "blk_kfz_col15", (10, 10, 60, 30), (200, 100), "<script>window.__xss=4</script>"
    )
    versions = {"report": report, "ver_sa174": primary}
    hostile = "<img src=y onerror=window.__xss=3>"
    run_id = seed_run(store, ws, versions, edit=lambda text: text.replace("Акт № 118 от 28.01.2026", hostile))
    store.append_message(
        c.ChatMessage(
            workspace_id=ws,
            message_id=c.new_id("msg"),
            run_id=run_id,
            role="user",
            text="<script>window.__xss=2</script><b>жирный</b>",
            created_at=_now(),
        )
    )
    page = env.goto(ws)
    expect(page.locator("#doc-list")).to_contain_text('<img src=x onerror="window.__xss=1">.pdf')
    expect(chat_messages(page).locator(".msg-text")).to_have_text("<script>window.__xss=2</script><b>жирный</b>")
    detail = expand(page, run_id, "col15")
    expect(detail.locator(".contributions")).to_contain_text(hostile)
    panel = open_evidence(page, detail, "blk_kfz_col15")
    expect(panel.locator(".ev-text .quote")).to_have_text("<script>window.__xss=4</script>")
    expect(panel.locator("h3.fname")).to_have_text("<b title=x>акт.pdf")
    assert page.evaluate("() => window.__xss") is None
    assert page.locator('img[src="x"], img[src="y"], .msg-text b, .msg-text script, #doc-list b').count() == 0

    # Upload limits come back as readable errors.
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    open_setup(page)
    page.set_input_files(
        "#up-primary", files=[{"name": "big.pdf", "mimeType": "application/pdf", "buffer": b"x" * (2 << 20)}]
    )
    page.click('form[data-role="primary"] button[type="submit"]')
    expect(page.locator('form[data-role="primary"] .upload-status')).to_contain_text("Слишком большой файл")

    # Validation errors identify the form field using its Russian label.
    detail_body = {
        "detail": [{"loc": ["body", "scope", "date_from"], "msg": "Input should be a valid date", "type": "date"}]
    }
    page.route(
        "**/api/workspaces/*/runs",
        lambda route: (
            route.fulfill(status=422, content_type="application/json", body=json.dumps(detail_body))
            if route.request.method == "POST"
            else route.continue_()
        ),
    )
    page.check(f'#doc-list input[data-version-id="{primary.version_id}"]')
    fill_scope(page)
    page.click("#run-start")
    expect(page.locator("#run-start-status")).to_contain_text("Проверьте поле «Начало периода»")
    assert_no_internal_details(page)
    page.unroute("**/api/workspaces/*/runs")

    # A malformed response is reported, not rendered as an empty result.
    page.route(
        "**/api/workspaces/*/runs",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"oops": 1}'),
    )
    page.reload()
    expect(page.locator("#ws-status .alert-bad")).to_contain_text(
        re.compile("Не удалось|не удалось|некорректный ответ")
    )
    page.unroute("**/api/workspaces/*/runs")
    page.route(
        "**/api/workspaces/*/messages",
        lambda route: route.fulfill(status=502, content_type="text/html", body="<html>Bad gateway</html>"),
    )
    page.reload()
    expect(page.locator("#ws-status .alert-bad")).to_contain_text("Сервер недоступен")
    page.unroute("**/api/workspaces/*/messages")


def test_inaccessible_workspace_is_not_replaced(env):
    page = env.goto()
    created = []
    page.on(
        "request", lambda r: created.append(r.url) if r.method == "POST" and r.url.endswith("/api/workspaces") else None
    )
    page.evaluate(
        "() => { localStorage.setItem('sverka.workspace', 'ws_gone123'); localStorage.setItem('sverka.recentWorkspaces', JSON.stringify(['ws_gone123', 'ws_older456'])); }"
    )
    page.reload()
    alert = page.locator("#ws-status .alert-bad")
    expect(alert).to_contain_text("Не найдено")
    assert_no_internal_details(page)
    alert.get_by_role("button", name="Повторить").click()
    expect(page.locator("#ws-status .alert-bad")).to_contain_text("Не найдено")
    expect(page.locator('#ws-history [data-workspace-id="ws_older456"]')).to_have_count(1)
    assert page.evaluate("() => localStorage.getItem('sverka.workspace')") == "ws_gone123"
    assert created == []
    expect(page.locator("#run-start")).to_be_disabled()
    expect(page.locator("#composer-send")).to_be_disabled()


def test_switching_workspace_stops_the_old_ones_polling(env):
    store = env.store
    first, versions = seed_workspace(store)
    active = queued_run(store, first, versions)
    second, _ = seed_workspace(store)
    page = env.goto(first)
    expect(run_block(page, active).locator(".run-head")).to_contain_text("В очереди")
    stale = []
    page.on("request", lambda r: stale.append(r.url) if f"/workspaces/{first}/" in r.url else None)
    page.evaluate("id => { window.location.hash = id; }", second)
    expect(page).to_have_url(f"{env.base}/app#{second}")
    expect(page.locator(f'#ws-history [data-workspace-id="{second}"]')).to_have_attribute("aria-current", "page")
    expect(page.locator(".run-block")).to_have_count(0)
    expect(page.locator("#doc-list li.doc")).to_have_count(len(versions))
    page.wait_for_timeout(3500)
    assert stale == [], "no request for the previous workspace after switching"
    expect(page.locator(f'#ws-history [data-workspace-id="{first}"]')).to_have_count(1)


def test_saved_list_resizable_columns_and_evidence_on_demand(env):
    store = env.store
    first, versions = seed_workspace(store)
    seed_evidence(store, first, versions)
    run_id = seed_run(store, first, versions)
    second, _ = seed_workspace(store)
    page = env.goto(second)
    expect(page.locator("#setup-panel")).to_be_visible()  # no runs yet: documents and parameters come first
    expect(page.locator("#evidence")).to_be_hidden()
    expect(page.locator("#evidence-toggle")).to_be_hidden()

    # A link opens a saved reconciliation; the list shows a readable title and the last run's status.
    page.goto(f"{env.base}/app#{first}")
    expect(page.locator(f'#ws-history [data-workspace-id="{first}"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#setup-panel")).to_be_hidden()  # it has results: the conversation takes the screen
    item = page.locator(f'#ws-history [data-workspace-id="{first}"]')
    expect(item.locator(".hist-title")).to_have_text("Договор 24/03/15-01, 3 этап, 2026 г.")
    expect(item).to_have_attribute("aria-current", "page")
    expect(item.locator(".hist-meta")).to_contain_text("Завершена")
    expect(page.locator(f'#ws-history [data-workspace-id="{second}"]')).to_have_count(1)
    expect(page.locator("#ws-status .ws-title")).to_have_text("Договор 24/03/15-01, 3 этап, 2026 г.")

    # The evidence column exists only while a fragment is shown, and can be hidden and brought back.
    chat_alone = page.locator(".chat").bounding_box()["width"]
    open_evidence(page, expand(page, run_id, "col15"), "blk_sa174_line0")
    expect(page.locator("#evidence")).to_be_visible()
    assert page.locator(".chat").bounding_box()["width"] < chat_alone - 300
    page.click("#evidence-close")
    expect(page.locator("#evidence")).to_be_hidden()
    toggle = page.locator("#evidence-toggle")
    expect(toggle).to_have_text("Показать фрагмент")
    toggle.click()
    expect(page.locator("#evidence")).to_be_visible()
    expect(page.locator("#evidence-body .ev-doc")).to_have_attribute(
        "data-version-id", versions["ver_sa174"].version_id
    )
    expect(page.locator("#evidence-body h3.fname")).to_have_text(versions["ver_sa174"].original_filename)

    # Both dividers resize their column by pointer and keyboard; widths survive a reload.
    def drag(selector, dx):
        box = page.locator(selector).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + dx, y, steps=6)
        page.mouse.up()

    side = page.locator("#sidebar").bounding_box()["width"]
    drag("#side-splitter", 120)
    wider = page.locator("#sidebar").bounding_box()["width"]
    assert abs(wider - (side + 120)) <= 3
    page.locator("#side-splitter").focus()
    page.keyboard.press("ArrowLeft")
    assert abs(page.locator("#sidebar").bounding_box()["width"] - (wider - 16)) <= 2
    expect(page.locator("#side-splitter")).to_have_attribute("aria-valuenow", str(round(wider - 16)))
    evidence = page.locator("#evidence").bounding_box()["width"]
    drag("#ev-splitter", -80)
    assert abs(page.locator("#evidence").bounding_box()["width"] - (evidence + 80)) <= 3
    page.reload()
    expect(page.locator(f'#ws-history [data-workspace-id="{first}"]')).to_have_attribute("aria-current", "page")
    assert abs(page.locator("#sidebar").bounding_box()["width"] - (wider - 16)) <= 2
    expect(page.locator("#evidence")).to_be_hidden()

    # Rename, hide and show the list, switch through it.
    page.locator('[data-fkey="rename"]').click()
    page.fill("#ws-rename", "Этап 3: основная сверка")
    page.keyboard.press("Enter")
    expect(item.locator(".hist-title")).to_have_text("Этап 3: основная сверка")
    expect(page.locator("#ws-status .ws-title")).to_have_text("Этап 3: основная сверка")
    page.click("#side-hide")
    expect(page.locator("#sidebar")).to_be_hidden()
    page.click("#side-show")
    expect(page.locator("#sidebar")).to_be_visible()
    page.locator(f'#ws-history [data-workspace-id="{second}"]').click()
    expect(page.locator(f'#ws-history [data-workspace-id="{second}"]')).to_have_attribute("aria-current", "page")
    expect(page).to_have_url(f"{env.base}/app#{second}")
    env.shot("10-saved-list")


def test_fixture_mode_result_is_labelled_and_its_evidence_missing(env):
    store = env.store
    env.services.launcher = api.FixtureRunLauncher(store)
    ws, _ = seed_workspace(store)
    page = env.goto(ws)
    fill_scope(page)
    page.click("#run-start")
    block = page.locator(".run-block").first
    expect(block.locator(".run-head")).to_contain_text("Завершена")
    expect(block.locator(".run-head .synthetic")).to_have_text("Пример результата")
    run_id = block.get_attribute("data-run-id")
    panel = open_evidence(page, expand(page, run_id, "col15"), "blk_kfz_c15")
    expect(panel.locator('[data-evidence-error="not_found"]')).to_contain_text("не найден")
    expect(panel).to_contain_text("В синтетическом примере")


def test_small_screen_layout_and_evidence_drawer(env):
    store = env.store
    ws, versions = seed_workspace(store)
    seed_evidence(store, ws, versions)
    run_id = seed_run(store, ws, versions)
    page = env.page
    page.set_viewport_size({"width": 390, "height": 844})
    env.goto(ws)
    expect(run_block(page, run_id).locator("tr.ind-row")).to_have_count(15)
    fonts_ready(page)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    expect(page.locator("#evidence")).to_be_hidden()
    detail = expand(page, run_id, "col15")
    button = detail.locator('button.ev-btn[data-block-id="blk_sa174_line0"]')
    button.click()
    drawer = page.locator("#evidence")
    expect(drawer).to_be_visible()
    assert drawer.bounding_box()["width"] >= 389
    assert_highlight(page, (142, 509, 605, 531), ACT_RENDER)
    env.shot("09-small-screen-evidence")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(button).to_be_focused()

    # On a phone the saved list is a drawer behind the menu button.
    expect(page.locator("#sidebar")).to_be_hidden()
    page.click("#side-show")
    expect(page.locator("#sidebar")).to_be_visible()
    expect(page.locator(f'#ws-history [data-workspace-id="{ws}"]')).to_be_visible()
    env.shot("11-small-screen-list")
    page.keyboard.press("Escape")
    expect(page.locator("#sidebar")).to_be_hidden()


# ======================= real service integration, offline model =======================


def upload_native_documents(env, files=None):
    """Upload actual native inputs through the UI, with the same synthetic report as T11's offline tests."""
    from test_live_flow import native_bundle

    ws = env.store.create_workspace().workspace_id
    page = env.goto(ws)
    files = native_bundle() if files is None else files
    for role in ("report", "primary", "context"):
        uploads = [
            {"name": name, "mimeType": "application/octet-stream", "buffer": data}
            for name, file_role, data in files
            if file_role == role
        ]
        if uploads:
            page.set_input_files("#up-" + role, files=uploads)
            page.click(f'form[data-role="{role}"] button[type="submit"]')
            expect(page.locator(f'form[data-role="{role}"] .upload-status')).to_contain_text(
                f"Загружено {len(uploads)}"
            )
    return ws


def install_reader_service(env, ws, before_model=lambda: None):
    from test_live_flow import FAST, ReaderModel

    from sverka.document_tools import DocumentTools
    from sverka.harness_fakes import ScriptedProvider
    from sverka.run_service import RunService

    tools = DocumentTools(env.store)
    reader = ReaderModel(lambda version_id: tools.prepared(ws, version_id))

    def read(messages, specs):
        before_model()
        return reader(messages, specs)

    provider = ScriptedProvider(handler=read)
    service = RunService(env.store, tools=tools, provider_factory=lambda: provider, limits=FAST)
    env.services.launcher = service
    return service, provider


def test_real_service_result_delayed_outcome_followup_and_refresh(env, monkeypatch):
    from sverka.extraction import ExtractionInstructions
    from sverka.harness import FollowUpResponder
    from sverka.harness_fakes import ScriptedProvider, respond, tool_call
    from sverka.model_adapter import ProviderError

    ws = upload_native_documents(env)
    store, page = env.store, env.page
    model_entered, release_model = threading.Event(), threading.Event()
    outcome_ready, release_outcome = threading.Event(), threading.Event()

    def pause_model():
        model_entered.set()
        assert release_model.wait(45), "test did not release the offline model"

    service, extraction_provider = install_reader_service(env, ws, pause_model)
    append = store.append_message

    def append_delayed_outcome(message):
        if message.cards and message.cards[0].kind == "conclusion" and not outcome_ready.is_set():
            outcome_ready.set()
            assert release_outcome.wait(45), "test did not release the saved outcome"
        append(message)

    monkeypatch.setattr(store, "append_message", append_delayed_outcome)
    try:
        fill_scope(page)
        page.click("#run-start")
        block = page.locator(".run-block").first
        expect(block).to_have_count(1)
        run_id = block.get_attribute("data-run-id")
        assert model_entered.wait(30)
        expect(block.locator(".activity-title")).to_contain_text("извлекаю данные из документов")
        expect(block.locator('li[data-stage="prepare"] .stage-count')).to_have_text("19 из 19")
        expect(block.locator('li[data-stage="extract"]')).to_have_attribute("data-state", "current")
        assert store.get_run(ws, run_id).status == "running"
        release_model.set()

        expect(block.locator(".run-head")).to_contain_text("Завершена", timeout=45000)
        expect(block.locator("tr.ind-row")).to_have_count(15)
        expect(block.locator(".run-completion")).to_have_count(1)
        assert outcome_ready.is_set() and store.list_messages(ws) == []
        # The terminal run is visible before its real RunService outcome message has been saved.
        recovery_reads = []
        page.on(
            "request",
            lambda request: (
                recovery_reads.append(request.url)
                if request.method == "GET" and request.url.endswith("/messages")
                else None
            ),
        )
        page.wait_for_timeout(1800)
        assert recovery_reads, "terminal-message recovery must retry while the saved outcome is delayed"
        expect(chat_messages(page)).to_have_count(0)
        release_outcome.set()
        expect(chat_messages(page)).to_have_count(1, timeout=10000)
        assert service.wait(run_id, timeout=10)

        run = store.get_run(ws, run_id)
        assert run.result is not None and run.report is not None and len(run.primaries) == 18
        assert run.replay.tool_versions["extraction"] == "1" and run.replay.tool_versions["run_service"] == "1"
        assert {p.stage: (p.done, p.total, p.failed) for p in run.progress} == {
            "prepare": (19, 19, 0),
            "extract": (19, 19, 0),
            "validate": (19, 19, 0),
            "reconcile": (1, 1, 0),
            "save": (1, 1, 0),
        }
        expect(block.locator(".synthetic")).to_have_count(0)
        for indicator in run.result.indicators:
            expect(row(page, run_id, indicator.entry_id)).to_have_attribute("data-status", indicator.status)
            for column, field in (
                ("reported", "reported_value"),
                ("observed", "observed_sum"),
                ("expected", "expected_value"),
                ("diff", "diff"),
            ):
                value = getattr(indicator, field)
                shown = cell(page, run_id, indicator.entry_id, column)
                if value is None:
                    expect(shown.locator(".val-null")).to_have_count(1)
                else:
                    expect(shown.locator(".val")).to_have_attribute("data-value", format(value, "f"))
        payroll = next(indicator for indicator in run.result.indicators if indicator.entry_id == "col5")
        assert payroll.status == "verified" and format(payroll.expected_value, "f") == "5558474.83"
        evidence_id = payroll.reported_evidence_ids[0]
        evidence = store.get_block(ws, evidence_id)
        panel = open_evidence(page, expand(page, run_id, "col5"), evidence_id)
        expect(panel.locator(".ev-doc")).to_have_attribute("data-version-id", evidence.version_id)
        expect(panel.locator(".cell-ref")).to_have_text(evidence.locator.ref)
        expect(panel.locator(".cell-text")).to_have_text("5558474.83")

        [outcome] = store.list_messages(ws)
        assert outcome.text == run.result.conclusion.summary and outcome.cards[0].kind == "conclusion"
        expect(chat_messages(page)).to_have_attribute("data-message-id", outcome.message_id)
        followup_provider = ScriptedProvider(
            [
                respond(tool_call("read_evidence", {"block_ids": [evidence_id]})),
                respond(
                    text=json.dumps(
                        {
                            "kind": "answer",
                            "text": "Оплата труда подтверждена документами из сохраненной проверки.",
                            "entry_ids": ["col5"],
                            "evidence_ids": [evidence_id],
                            "include_conclusion": True,
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        env.services.responder = FollowUpResponder(
            provider=followup_provider, runs=store, documents=store, instructions=ExtractionInstructions()
        )
        page.fill("#composer-text", "Какие документы подтверждают оплату труда?")
        page.click("#composer-send")
        expect(chat_messages(page)).to_have_count(3)
        saved = store.list_messages(ws)
        reply = page.locator(f'.msg[data-message-id="{saved[-1].message_id}"]')
        expect(reply.locator(".msg-text")).to_have_text(
            "Оплата труда подтверждена документами из сохраненной проверки."
        )
        assert len(extraction_provider.calls) > 18 and len(followup_provider.calls) == 2
        assert [tool.name for tool in followup_provider.calls[0].tools] == ["read_evidence"]
        assert json.loads(followup_provider.calls[1].messages[-1].text)["ok"] is True
        assert saved[-1].cards[-1].indicator == payroll and saved[-1].evidence_ids == [evidence_id]
        reply.locator(".msg-cards summary").click()
        expect(reply.locator('tr.ind-row[data-entry="col5"] .c-expected .val')).to_have_attribute(
            "data-value", "5558474.83"
        )
        # The actual follow-up service keeps the saved cards when its model cannot answer.
        unavailable = ScriptedProvider(
            [ProviderError("provider_error", f"RuntimeError SVERKA_PROVIDER_API_KEY run_id={run_id}")]
        )
        env.services.responder.provider = unavailable
        page.fill("#composer-text", "Объясните результат еще раз")
        page.click("#composer-send")
        expect(chat_messages(page)).to_have_count(5)
        saved = store.list_messages(ws)
        expect(chat_messages(page).last.locator(".msg-text")).to_have_text(
            "Комментарий ассистента недоступен. Ниже сохраненный результат проверки."
        )
        assert len(unavailable.calls) == 1
        assert saved[-1].text == "Комментарий ассистента недоступен. Ниже сохраненный результат проверки."
        assert len([card for card in saved[-1].cards if card.kind == "indicator"]) == 15
        expect(chat_messages(page).last.locator(".msg-cards summary")).to_contain_text("15 показателей")
        assert store.get_run(ws, run_id) == run
        assert_no_internal_details(page)
        page.reload()
        expect(chat_messages(page)).to_have_count(5)
        assert chat_messages(page).evaluate_all("nodes => nodes.map(node => node.dataset.messageId)") == [
            message.message_id for message in saved
        ]
        expect(run_block(page, run_id).locator(".run-completion")).to_have_count(1)
        expect(run_block(page, run_id).locator("tr.ind-row")).to_have_count(15)
        env.shot("13-real-service-result")
    finally:
        release_model.set()
        release_outcome.set()
        for run in store.list_runs(ws):
            assert service.wait(run.run_id, timeout=30)


def test_real_service_missing_credentials_is_readable_and_persisted(env, monkeypatch):
    from test_live_flow import FAST, native_bundle

    from sverka.harness import FollowUpResponder
    from sverka.harness_fakes import ScriptedProvider
    from sverka.run_service import RunService, provider_from_env

    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv("SVERKA_PROVIDER_" + name, raising=False)
    ws = upload_native_documents(env, native_bundle()[:2])
    store, page = env.store, env.page
    service = RunService(store, provider_factory=provider_from_env, limits=FAST)
    env.services.launcher = service
    provider = ScriptedProvider()
    env.services.responder = FollowUpResponder(provider=provider, runs=store, documents=store)
    try:
        fill_scope(page)
        page.click("#run-start")
        block = page.locator(".run-block").first
        expect(block.locator(".run-head")).to_contain_text("Не выполнена")
        run_id = block.get_attribute("data-run-id")
        assert service.wait(run_id, timeout=10)
        run = store.get_run(ws, run_id)
        assert run.failure.code == "missing_credentials" and run.result is None
        expect(block.locator(".alert-bad")).to_contain_text("не настроен доступ к модели")
        expect(block.locator(".run-completion, tr.ind-row")).to_have_count(0)
        expect(chat_messages(page)).to_have_count(1, timeout=10000)
        [outcome] = store.list_messages(ws)
        assert outcome.cards[0].kind == "run_status" and outcome.cards[0].failure.code == "missing_credentials"
        page.fill("#composer-text", "Почему проверка не выполнена?")
        page.click("#composer-send")
        expect(chat_messages(page)).to_have_count(3)
        assert provider.calls == []
        assert_no_internal_details(page)
        page.reload()
        expect(chat_messages(page)).to_have_count(3)
        expect(run_block(page, run_id).locator(".run-head")).to_contain_text("Не выполнена")
        expect(page.locator("#doc-list li.doc")).to_have_count(2)
        assert_no_internal_details(page)
        env.shot("14-real-service-missing-credentials")
    finally:
        for run in store.list_runs(ws):
            assert service.wait(run.run_id, timeout=30)


def test_real_service_cancellation_discards_a_late_offline_model_response(env):
    from test_live_flow import native_bundle

    ws = upload_native_documents(env, native_bundle()[:2])
    entered, release = threading.Event(), threading.Event()

    def pause_model():
        entered.set()
        assert release.wait(45), "test did not release the cancelled offline model"

    service, provider = install_reader_service(env, ws, pause_model)
    store, page = env.store, env.page
    try:
        fill_scope(page)
        page.click("#run-start")
        block = page.locator(".run-block").first
        expect(block).to_have_count(1)
        run_id = block.get_attribute("data-run-id")
        assert entered.wait(30)
        expect(block.locator(".activity-title")).to_contain_text("извлекаю данные из документов")
        block.get_by_role("button", name="Отменить проверку").click()
        expect(block.locator(".run-head")).to_contain_text("Отменена")
        release.set()
        assert service.wait(run_id, timeout=30)
        cancelled = store.get_run(ws, run_id)
        assert cancelled.status == "cancelled" and cancelled.result is None and cancelled.report is None
        assert len(provider.calls) == 1 and store.list_messages(ws) == []
        expect(block.locator(".run-completion, tr.ind-row")).to_have_count(0)
        page.reload()
        expect(run_block(page, run_id).locator(".run-head")).to_contain_text("Отменена")
        expect(chat_messages(page)).to_have_count(0)
    finally:
        release.set()
        for run in store.list_runs(ws):
            assert service.wait(run.run_id, timeout=30)
