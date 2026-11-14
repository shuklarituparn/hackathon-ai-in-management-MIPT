"""V03: preparing the supplied formats (T04).

PDF/PNG/DOCX/XLSX become located evidence on the stored version: native text and OCR keep their boxes on
a stored render, scans are OCR'd even with a small native watermark, rotated scans record their turn, DOCX
keeps quotes, XLSX keeps raw formulas apart from derived values, and damaged files stay visible errors.
Tests that need Tesseract are marked ``ocr``.
"""

from __future__ import annotations

import io
import json
import zlib
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from sverka import contracts as c
from sverka import document_tools as dt
from sverka import fakes, ocr, preparation, readers
from sverka.storage import SqliteStore

ROOT = Path(__file__).resolve().parent.parent
CASE19 = ROOT / "examples" / "case19"
SCENARIOS = ROOT / "tests" / "fixtures" / "case19" / "scenarios"
ACT_174 = CASE19 / "05_Счет-акт_174_25-02-2026.pdf"
ACT_118 = CASE19 / "04_Акт_118_28-01-2026.pdf"  # image-only scan
KL_31 = CASE19 / "06_Служебная_записка_КЛ-31.docx"
PAYROLL_JAN = CASE19 / "02_01_Ведомость_ЗП_Январь_2026.xlsx"
SCAN_PNG = CASE19 / "12_Акт_связь_0426.png"
ROTATED = SCENARIOS / "rotated_scan" / ACT_174.name
NATIVE_PDFS = sorted(p for p in CASE19.glob("*.pdf") if p.name[:2] in ("05", "07", "09", "10", "13", "14"))
SCANNED_PDFS = sorted(p for p in CASE19.glob("*.pdf") if p.name[:2] in ("01", "04", "08", "15"))


@pytest.fixture
def store() -> fakes.InMemoryStore:
    return fakes.InMemoryStore()


@pytest.fixture
def ws(store) -> str:
    return store.create_workspace().workspace_id


def _upload(
    store, ws: str, path: Path | None = None, *, data: bytes | None = None, name: str | None = None, role="primary"
):
    data = path.read_bytes() if data is None else data
    return store.save_document(ws, filename=name or path.name, role=role, data=data)


def _prepare(store, ws, path=None, **kw) -> c.PreparedDocument:
    version = _upload(store, ws, path, **kw)
    return preparation.prepare_version(store, ws, version.version_id)


def _render(store, ws, render_id: str) -> Image.Image:
    _, data = store.read_artifact(ws, render_id)
    return Image.open(io.BytesIO(data))


def _ink(store, ws, block: c.EvidenceBlock) -> float:
    """Mean darkness (0..255) inside the block's box on its stored render."""
    box = block.locator.box
    crop = _render(store, ws, block.locator.render_id).convert("L").crop((box.x0, box.y0, box.x1, box.y1))
    return 255 - ImageStat.Stat(crop).mean[0]


def _no_tesseract(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "available", lambda: False)


def _block(prepared: c.PreparedDocument, needle: str, method: str | None = None) -> c.EvidenceBlock:
    found = [b for b in prepared.blocks if needle in b.text.replace("\u00a0", " ") and method in (None, b.method)]
    assert found, f"no block with {needle!r}: {[b.text for b in prepared.blocks]}"
    return found[0]


# ======================= native PDF =======================


@pytest.mark.parametrize("path", NATIVE_PDFS, ids=lambda p: p.name[:12])
def test_native_pdfs_keep_every_line_on_a_stored_render(store, ws, path):
    prepared = _prepare(store, ws, path)
    assert not prepared.errors and prepared.blocks
    [page] = prepared.pages
    assert page.transform == c.RenderTransform(rotation=0, dpi=preparation.RENDER_DPI)
    assert all(b.method == "native_text" and b.locator.render_id == page.render_id for b in prepared.blocks)
    content = readers.read_any(str(path))
    assert [b.text for b in prepared.blocks] == [f.text for f in content.fragments]  # nothing dropped
    for block in prepared.blocks:
        assert store.get_block(ws, block.block_id) == block  # saved, so read_evidence and the API resolve it


def test_native_amount_highlight_covers_the_printed_amount(store, ws):
    prepared = _prepare(store, ws, ACT_174)
    amount = _block(prepared, "318 298,10")
    assert amount.text == "К оплате по документу: 318 298,10 руб."
    assert 0.4 < amount.locator.box.x1 / amount.locator.image_width < 0.6
    assert _ink(store, ws, amount) > 20


def test_same_filename_versions_point_at_their_own_renders(store, ws):
    original = _upload(store, ws, ACT_174)
    replacement = store.save_document(
        ws,
        filename=ACT_174.name,
        role="primary",
        data=(SCENARIOS / "replacement_version" / "05_Счет-акт_174_25-02-2026_v2.pdf").read_bytes(),
        document_id=original.document_id,
    )
    first = preparation.prepare_version(store, ws, original.version_id)
    second = preparation.prepare_version(store, ws, replacement.version_id)
    assert not {b.block_id for b in first.blocks} & {b.block_id for b in second.blocks}
    for prepared in (first, second):
        for block in prepared.blocks:
            ref, _ = store.read_artifact(ws, block.locator.render_id)
            assert ref.version_id == prepared.version.version_id == block.version_id
    # The old version's evidence still resolves after the replacement.
    assert store.get_block(ws, first.blocks[0].block_id) == first.blocks[0]


# ======================= scans =======================


@pytest.mark.ocr
@pytest.mark.parametrize("path", SCANNED_PDFS, ids=lambda p: p.name[:12])
def test_scanned_pdfs_are_ocrd_on_their_render(store, ws, path):
    prepared = _prepare(store, ws, path)
    assert not prepared.errors and prepared.blocks
    [page] = prepared.pages
    assert page.transform.rotation == 0 and page.transform.dpi >= 300
    assert all(b.method == "ocr" and b.quality.engine == "tesseract" for b in prepared.blocks)
    assert any("OCR" in w for w in prepared.warnings)


@pytest.mark.ocr
def test_scanned_amount_highlight_covers_ink(store, ws):
    prepared = _prepare(store, ws, ACT_118)
    amount = _block(prepared, "245 000,00")
    assert _ink(store, ws, amount) > 20


@pytest.mark.ocr
def test_png_scan(store, ws):
    prepared = _prepare(store, ws, SCAN_PNG)
    amount = _block(prepared, "650 000,00")
    [page] = prepared.pages
    assert amount.locator.render_id == page.render_id and page.transform.dpi is None
    assert _ink(store, ws, amount) > 20


@pytest.mark.ocr
def test_rotated_scan_is_ocrd_upright_and_records_the_turn(store, ws):
    spec = json.loads((SCENARIOS / "rotated_scan" / "expected.json").read_text(encoding="utf-8"))
    expected = spec["expected_preparation"][ACT_174.name]
    prepared = _prepare(store, ws, ROTATED, name=ACT_174.name)
    assert not prepared.errors
    assert len(prepared.pages) == expected["pages"]
    [page] = prepared.pages
    assert page.transform.rotation == expected["transform"]["rotation"] == 90
    assert page.height > page.width  # the landscape scan is now an upright portrait page
    assert all(b.method == "ocr" for b in prepared.blocks)  # no text layer
    amount = _block(prepared, expected["amount_text"])
    assert "оплате" in amount.text and amount.locator.transform.rotation == 90
    assert _ink(store, ws, amount) > 20
    assert any("90°" in w for w in prepared.warnings)


def test_scan_without_tesseract_is_still_rendered_and_reported(store, ws, monkeypatch):
    _no_tesseract(monkeypatch)
    prepared = _prepare(store, ws, ACT_118)
    assert prepared.blocks == []
    [page] = prepared.pages
    assert _render(store, ws, page.render_id).size == (page.width, page.height)
    assert [e.code for e in prepared.errors] == ["unreadable"]
    assert "изображение" in prepared.errors[0].message


# ======================= scans with a small native watermark =======================


def _scan_with_watermark(scan: Path, text: str = "SCANNED COPY Scanner-01 14.11.2026") -> bytes:
    """A one-page PDF: the scan's image over the whole page plus a small native-text line at the bottom."""
    with Image.open(io.BytesIO(_first_image(scan))) as img:
        gray = img.convert("L")
        width, height = gray.size
        pixels = zlib.compress(gray.tobytes())
    import pdfplumber

    with pdfplumber.open(scan) as pdf:
        page_w, page_h = float(pdf.pages[0].width), float(pdf.pages[0].height)
    content = (f"q {page_w:.2f} 0 0 {page_h:.2f} 0 0 cm /Im1 Do Q BT /F1 9 Tf 24 18 Td ({text}) Tj ET").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] "
            "/Resources << /XObject << /Im1 4 0 R >> /Font << /F1 5 0 R >> >> /Contents 6 0 R >>"
        ).encode(),
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
            f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n"
        ).encode()
        + pixels
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _first_image(scan: Path) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(scan))
    try:
        for obj in pdf[0].get_objects():
            if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                buf = io.BytesIO()
                obj.get_bitmap(render=False).to_pil().save(buf, format="PNG")
                return buf.getvalue()
    finally:
        pdf.close()
    raise AssertionError("no image in the scan")


def test_watermark_fixture_fools_the_legacy_reader():
    """The legacy reader sees >= 3 native words and never OCRs the scan under them (silent data loss)."""
    import pdfplumber

    data = _scan_with_watermark(ACT_118)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        assert len(page.extract_words()) >= 3 and len(page.images) == 1


@pytest.mark.ocr
def test_scan_with_a_small_native_watermark_is_still_ocrd(store, ws):
    prepared = _prepare(store, ws, data=_scan_with_watermark(ACT_118), name="акт_118_водяной_знак.pdf")
    assert not prepared.errors
    [page] = prepared.pages
    amount = _block(prepared, "245 000,00")  # the scanned content is indexed
    assert amount.method == "ocr" and _ink(store, ws, amount) > 20
    watermark = _block(prepared, "SCANNED COPY", "native_text")
    assert watermark.locator.render_id == page.render_id
    assert watermark.locator.box.y0 > 0.9 * page.height and watermark.locator.box.x0 < 0.1 * page.width
    assert _ink(store, ws, watermark) > 20
    assert any("водяной знак" in w for w in prepared.warnings)


@pytest.mark.ocr
def test_watermark_on_a_rotated_scan_is_placed_on_the_upright_render(store, ws):
    prepared = _prepare(store, ws, data=_scan_with_watermark(ROTATED, "COPY COPY COPY"), name="rotated.pdf")
    [page] = prepared.pages
    assert page.transform.rotation == 90
    _block(prepared, "318 298,10")
    watermark = _block(prepared, "COPY COPY", "native_text")
    box = watermark.locator.box
    # Bottom-left of the landscape page turns into the top-left of the upright render, running downwards.
    assert box.x0 < 0.1 * page.width and box.y0 < 0.5 * page.height and box.y1 - box.y0 > box.x1 - box.x0
    assert _ink(store, ws, watermark) > 20


def test_watermarked_scan_without_tesseract_keeps_the_watermark_and_reports_the_scan(store, ws, monkeypatch):
    _no_tesseract(monkeypatch)
    prepared = _prepare(store, ws, data=_scan_with_watermark(ACT_118), name="акт_118_водяной_знак.pdf")
    [block] = prepared.blocks
    assert block.method == "native_text" and "SCANNED COPY" in block.text
    assert [e.code for e in prepared.errors] == ["unreadable"]
    assert any("водяной знак" in w for w in prepared.warnings)


# ======================= DOCX =======================


def test_docx_quotes_match_the_document(store, ws):
    import docx

    prepared = _prepare(store, ws, KL_31)
    assert not prepared.errors and not prepared.pages and prepared.warnings == []
    document = docx.Document(str(KL_31))
    for block in prepared.blocks:
        loc = block.locator
        if loc.paragraph is not None:
            assert document.paragraphs[loc.paragraph].text.strip() == loc.quote == block.text
        else:
            assert document.tables[loc.table].rows[loc.row].cells[loc.cell].text.strip() == loc.quote == block.text
    assert any(b.text == "270 000,00" or "270 000,00" in b.text for b in prepared.blocks)


def test_docx_literal_1000_stays_1000(store, ws, tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("Сумма: 1000")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Итого", "1000"
    path = tmp_path / "act.docx"
    document.save(path)
    prepared = _prepare(store, ws, path)
    assert [b.text for b in prepared.blocks] == ["Сумма: 1000", "Итого", "1000"]


def test_docx_text_outside_quotes_is_reported(store, ws, tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("Акт № 1")
    outer = document.add_table(rows=1, cols=1)
    outer.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = "Вложенная сумма 5 000,00"
    document.sections[0].header.paragraphs[0].text = "ООО Ромашка"
    path = tmp_path / "nested.docx"
    document.save(path)
    prepared = _prepare(store, ws, path)
    assert any("вне абзацев" in w for w in prepared.warnings)
    assert any("Колонтитулы" in w and "Ромашка" in w for w in prepared.warnings)


# ======================= XLSX =======================


def test_xlsx_keeps_raw_formula_cached_and_derived_values(store, ws):
    spec = json.loads((SCENARIOS / "uncached_sum" / "expected.json").read_text(encoding="utf-8"))
    cell = spec["expected_preparation"][PAYROLL_JAN.name]["cell"]
    prepared = _prepare(store, ws, PAYROLL_JAN)
    assert not prepared.errors and not prepared.pages
    [state] = prepared.formula_states
    assert state.model_dump(mode="json", exclude={"error", "kind"}) == cell
    total = next(b for b in prepared.blocks if b.locator.ref == cell["ref"])
    assert total.locator.derived_value == Decimal(cell["derived_value"]) and total.text == cell["formula"]
    assert prepared.warnings == []  # the legacy reader's float evaluation warning is gone


def test_xlsx_unresolved_formulas_are_listed_in_warnings(store, ws, tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["A2"] = 1000, "=[1]Свод!B2"
    path = tmp_path / "links.xlsx"
    wb.save(path)
    prepared = _prepare(store, ws, path)
    by_ref = {b.locator.ref: b.locator for b in prepared.blocks}
    assert by_ref["A1"].cached_value == "1000" and by_ref["A1"].evaluation == "literal"
    assert by_ref["A2"].evaluation == "unresolved" and by_ref["A2"].derived_value is None
    assert any("A2" in w for w in prepared.warnings)


def test_empty_xlsx_is_not_an_empty_success(store, ws, tmp_path):
    import openpyxl

    path = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(path)
    prepared = _prepare(store, ws, path)
    assert prepared.blocks == [] and [e.code for e in prepared.errors] == ["unreadable"]


# ======================= damaged and unsupported inputs =======================


@pytest.mark.parametrize("name", ["05_Счет-акт_174_25-02-2026.pdf", "16_Акт_пустой.pdf"])
def test_corrupt_inputs_fail_as_unreadable(store, ws, name):
    tools = dt.DocumentTools(store)
    version = _upload(store, ws, SCENARIOS / "corrupt_input" / name)
    result = tools.prepare_document(ws, version.version_id)
    assert not result.ok and result.error.code == "unreadable"
    assert result.error.details["errors"]
    assert tools.prepared(ws, version.version_id).blocks == []


def test_damaged_stored_bytes_are_unreadable(tmp_path):
    store = SqliteStore(tmp_path / "data")
    ws = store.create_workspace().workspace_id
    version = _upload(store, ws, ACT_174)
    blob = next(p for p in (tmp_path / "data" / "blobs").rglob("*") if p.is_file() and p.name == version.sha256)
    blob.write_bytes(b"%PDF tampered")
    result = dt.DocumentTools(store).prepare_document(ws, version.version_id)
    assert not result.ok and result.error.code == "unreadable"


@pytest.mark.parametrize(
    "name, data",
    [
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64),
        ("archive.zip", None),
        ("blob.bin", b"\x00\x01\x02 not a document"),
    ],
)
def test_unsupported_formats_say_so(store, ws, name, data):
    if data is None:
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("inner.txt", "x")
        data = buf.getvalue()
    prepared = _prepare(store, ws, data=data, name=name)
    assert prepared.blocks == [] and [e.code for e in prepared.errors] == ["unsupported"]
    result = dt.DocumentTools(store).prepare_document(ws, prepared.version.version_id)
    assert result.ok  # listed with its problem; there is nothing unreadable to retry


def test_context_text_file_is_kept_with_an_unsupported_note(store, ws):
    readme = CASE19 / "00_README_набор_данных.txt"
    prepared = _prepare(store, ws, readme, role="context")
    assert prepared.blocks == [] and [e.code for e in prepared.errors] == ["unsupported"]


# ======================= the whole supplied bundle =======================


def test_every_non_scanned_input_prepares_without_errors(store, ws):
    tools = dt.DocumentTools(store)
    for path in sorted(CASE19.iterdir()):
        if path in SCANNED_PDFS or path == SCAN_PNG or path.suffix == ".txt":
            continue
        version = _upload(store, ws, path)
        result = tools.prepare_document(ws, version.version_id)
        assert result.ok and result.data.blocks and not result.data.errors, path.name


@pytest.mark.ocr
def test_every_supplied_input_prepares_with_ocr(store, ws):
    tools = dt.DocumentTools(store)
    for path in sorted(CASE19.iterdir()):
        version = _upload(store, ws, path)
        result = tools.prepare_document(ws, version.version_id)
        assert result.ok, path.name
        if path.suffix != ".txt":
            assert result.data.blocks and not result.data.errors, path.name


def test_preparing_again_returns_the_same_evidence(store, ws):
    tools = dt.DocumentTools(store)
    version = _upload(store, ws, ACT_174)
    first = tools.prepare_document(ws, version.version_id).data
    second = tools.prepare_document(ws, version.version_id).data
    assert first == second


def test_unknown_or_foreign_versions_are_not_found(store, ws):
    tools = dt.DocumentTools(store)
    version = _upload(store, ws, ACT_174)
    other = store.create_workspace().workspace_id
    assert tools.prepare_document(other, version.version_id).error.code == "not_found"
    assert tools.prepare_document(ws, "ver_missing").error.code == "not_found"


# ======================= the T08 harness on real tools =======================


def test_harness_extracts_through_the_real_tools(store, ws, tmp_path):
    """The bounded harness (T08) drives these tools on real files: page, crop, evidence, validation."""
    import docx

    from sverka.harness import ExtractionHarness, HarnessLimits
    from sverka.harness_fakes import ScriptedProvider, respond, simple_validator, tool_call

    document = docx.Document()
    document.add_paragraph("Общепроизводственные затраты")
    document.add_paragraph("1 145 798,10")
    report_path = tmp_path / "report.docx"
    document.save(report_path)
    tools = dt.DocumentTools(store, validator=simple_validator)
    report = _upload(store, ws, report_path, role="report")
    act = _upload(store, ws, ACT_174)
    label, value = tools.prepared(ws, report.version_id).blocks
    prepared_act = tools.prepared(ws, act.version_id)
    amount = _block(prepared_act, "318 298,10")
    others = {b.text.split()[0]: b.block_id for b in prepared_act.blocks}

    def field(val, source, *ids, basis="observed"):
        return {"value": val, "source_text": source, "evidence_ids": list(ids), "basis": basis}

    report_proposal = {
        "kind": "report",
        "version_id": report.version_id,
        "row_label": "3 этап, 2026 г.",
        "indicators": [
            {
                "entry_id": "col15",
                "label": label.text,
                "column": 15,
                "label_evidence_ids": [label.block_id],
                "category": "production_overhead",
                "value": field("1145798.10", "1 145 798,10", value.block_id),
            }
        ],
    }
    money = field("318298.10", "318 298,10", amount.block_id)
    primary_proposal = {
        "kind": "primary",
        "version_id": act.version_id,
        "doc_type": field("Счет-акт", "СЧЕТ-АКТ", others["СЧЕТ-АКТ"]),
        "number": field("174", "№ 174", others["СЧЕТ-АКТ"]),
        "contracts": field(["24/03/15-01"], "24/03/15-01", others["Договор"]),
        "counterparty": field(None, None) | {"unresolved_reason": "not needed for this test"},
        "issue_date": field("2026-02-25", "25.02.2026", others["Дата"]),
        "service_period": field(None, None) | {"unresolved_reason": "not printed"},
        "amount": money,
        "currency": field("RUB", "руб.", amount.block_id),
        "unit": field("руб.", "руб.", amount.block_id),
        "allocations": [
            {"category": field("production_overhead", None, amount.block_id, basis="inferred"), "amount": money}
        ],
    }
    page = prepared_act.pages[0]
    box = amount.locator.box
    provider = ScriptedProvider(
        [
            respond(tool_call("validate_extraction", {"proposal": report_proposal})),
            respond(tool_call("inspect_page", {"version_id": act.version_id, "page": 1})),
            respond(
                tool_call(
                    "inspect_crop",
                    {"render_id": page.render_id, "box": {"x0": box.x0, "y0": box.y0, "x1": box.x1, "y1": box.y1}},
                )
            ),
            respond(tool_call("read_evidence", {"block_ids": [amount.block_id]})),
            respond(tool_call("validate_extraction", {"proposal": primary_proposal})),
        ]
    )
    scope = c.Scope(
        contract="24/03/15-01",
        report_row="3 этап, 2026 г.",
        date_from="2026-01-01",
        date_to="2026-05-31",
        confirmed=True,
    )
    run = store.create_run(ws, c.StartRunRequest(version_ids=[report.version_id, act.version_id], scope=scope))
    result = ExtractionHarness(provider=provider, tools=tools, store=store, limits=HarnessLimits()).extract(run)

    assert result.status == "completed", result.failure
    [primary] = result.primaries
    assert primary.amount.value == Decimal("318298.10") and primary.amount.evidence_ids == [amount.block_id]
    tool_messages = [
        json.loads(e["message"]["text"])
        for e in result.trace
        if e["event"] == "message" and e["message"]["role"] == "tool"
    ]
    assert tool_messages and all(m["ok"] for m in tool_messages), tool_messages
    assert result.replay.tool_versions["evidence_tools"] == dt.TOOLS_VERSION


# ======================= review regressions =======================


def test_vertically_merged_cells_do_not_hide_missing_docx_text(store, ws, tmp_path):
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "Объединенная ячейка с длинным текстом сорок символов"
    table.cell(0, 1).add_table(rows=1, cols=1).cell(0, 0).text = "Сумма 5 000,00"
    path = tmp_path / "merged.docx"
    document.save(path)
    prepared = _prepare(store, ws, path)
    assert any("вне абзацев" in w for w in prepared.warnings)


def test_large_pages_are_rendered_within_the_pixel_cap():
    class Page:
        width, height = 2384.0, 3370.0  # A0 in points

    assert preparation._capped_dpi(Page, 300) * 3370 / 72 <= preparation.MAX_RENDER_SIDE
    assert preparation._ocr_render_dpi(Page, 300) * 3370 / 72 <= preparation.MAX_RENDER_SIDE
    Page.width, Page.height = 595.0, 842.0  # A4 keeps the requested resolution
    assert preparation._capped_dpi(Page, 300) == 300


def test_missing_ocr_is_retryable_and_not_cached(store, ws, monkeypatch):
    tools = dt.DocumentTools(store)
    version = _upload(store, ws, ACT_118)
    calls = []
    real = preparation.prepare_version
    monkeypatch.setattr(preparation, "prepare_version", lambda *a, **k: calls.append(1) or real(*a, **k))
    _no_tesseract(monkeypatch)
    first = tools.prepare_document(ws, version.version_id).data
    assert first.errors and all(e.retryable for e in first.errors)
    tools.prepare_document(ws, version.version_id)
    assert len(calls) == 2  # prepared again: installing Tesseract can fix it


def test_the_preparation_cache_is_bounded(store, ws, monkeypatch):
    monkeypatch.setattr(dt, "MAX_CACHED", 1)
    tools = dt.DocumentTools(store)
    first, second = _upload(store, ws, ACT_174), _upload(store, ws, KL_31)
    tools.prepare_document(ws, first.version_id)
    tools.prepare_document(ws, second.version_id)
    assert list(tools._cache) == [(ws, second.version_id)]


def test_bad_arguments_and_internal_errors_are_told_apart(store, ws, monkeypatch):
    tools = dt.DocumentTools(store)
    version = _upload(store, ws, ACT_174)
    page = tools.prepare_document(ws, version.version_id).data.pages[0]
    bad = tools.inspect_crop(ws, page.render_id, {"x0": 10, "y0": 10, "x1": 5, "y1": 20})
    assert bad.error.code == "invalid_reference"

    def broken(*args, **kwargs):
        c.Box(x0=5, y0=5, x1=1, y1=1)  # an internal model error, not the caller's

    monkeypatch.setattr(dt.ev, "render_png", broken)
    internal = tools.inspect_crop(ws, page.render_id, c.Box(x0=0, y0=0, x1=10, y1=10))
    assert internal.error.code == "unreadable" and "inspect_crop failed" in internal.error.message


def test_orientation_is_checked_when_most_lines_are_noise():
    lines = [("СЧЕТ-АКТ № 174", (0, 0, 1, 1), 95.0)] + [(">", (0, 0, 1, 1), 70.0)] * 4
    assert ocr._good_share(lines) < 0.4
    assert ocr._good_share([("Стоимость услуг", (0, 0, 1, 1), 94.0)]) == 1.0
