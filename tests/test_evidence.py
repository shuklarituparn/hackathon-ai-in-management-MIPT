"""Tests for sverka.evidence (T03): versioned evidence blocks on the v1 contracts."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sverka import contracts as c
from sverka import evidence as ev
from sverka import fakes
from sverka import models as m
from sverka import readers

ROOT = Path(__file__).resolve().parent.parent
CASE19 = ROOT / "examples" / "case19"
ACT_174 = CASE19 / "05_Счет-акт_174_25-02-2026.pdf"
KL_31 = CASE19 / "06_Служебная_записка_КЛ-31.docx"
PAYROLL_JAN = CASE19 / "02_01_Ведомость_ЗП_Январь_2026.xlsx"
SCAN_PNG = CASE19 / "12_Акт_связь_0426.png"


@pytest.fixture
def store() -> fakes.InMemoryStore:
    return fakes.InMemoryStore()


@pytest.fixture
def workspace_id(store: fakes.InMemoryStore) -> str:
    return store.create_workspace().workspace_id


def _version(store, ws_id: str, content: bytes = b"test", filename: str = "report.pdf", role="report"):
    return store.save_document(workspace_id=ws_id, filename=filename, role=role, data=content)


def _page_image(store, ws_id: str, version_id: str, page: int = 1, dpi: int | None = 72) -> c.PageImage:
    art = store.save_artifact(
        workspace_id=ws_id, version_id=version_id, kind="page_render", media_type="image/png", data=b"fake png"
    )
    return c.PageImage(
        page=page, render_id=art.artifact_id, width=800, height=1000, transform=c.RenderTransform(dpi=dpi)
    )


def _pdf_content(page: int = 1, bbox=(10, 20, 300, 60)) -> m.DocContent:
    return m.DocContent(
        path="/nonexistent/report.pdf",
        name="report.pdf",
        fmt="pdf",
        fragments=[m.Fragment(text="Итого 1000", loc={"type": "pdf", "page": page, "bbox": list(bbox)})],
    )


def _docx_content() -> m.DocContent:
    # Shaped like readers._read_docx output: 1-based paragraph/table/row numbers, one fragment per row.
    return m.DocContent(
        path="/nonexistent/contract.docx",
        name="contract.docx",
        fmt="docx",
        fragments=[
            m.Fragment(text="Первый абзац", loc={"type": "docx", "paragraph": 1}),
            m.Fragment(
                text="Статья | | 1000",
                loc={"type": "docx", "table": 1, "row": 2},
                cells=["Статья", "", "1000"],
            ),
        ],
    )


def _real_pdf(store, ws_id: str, path: Path, dpi: int = 150):
    import pdfplumber

    data = path.read_bytes()
    version = _version(store, ws_id, data, path.name, role="primary")
    content = readers.read_any(str(path))
    pages = {}
    with pdfplumber.open(path) as pdf:
        for pno in sorted({f.loc["page"] for f in content.fragments}):
            image = pdf.pages[pno - 1].to_image(resolution=dpi).original
            pages[pno] = ev.save_page_render(store, ws_id, version.version_id, pno, image, dpi=dpi)
    return version, content, pages


def _ink(store, ws_id: str, block: c.EvidenceBlock) -> float:
    """Mean darkness (0..255) inside the block's box on its stored render."""
    from io import BytesIO

    from PIL import Image, ImageStat

    loc = block.locator
    _, data = store.read_artifact(ws_id, loc.render_id)
    with Image.open(BytesIO(data)) as img:
        crop = img.convert("L").crop((loc.box.x0, loc.box.y0, loc.box.x1, loc.box.y1))
        return 255 - ImageStat.Stat(crop).mean[0]


# ======================= contract shape, IDs, versions =======================


def test_prepared_document_round_trips_through_json(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    prepared = ev.build_prepared_document(_pdf_content(), version, page_images={1: page_image})
    assert c.PreparedDocument.model_validate_json(prepared.model_dump_json()) == prepared


def test_block_ids_are_contract_shaped_and_stable(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    prepared = ev.build_prepared_document(_pdf_content(), version, page_images={1: page_image})
    for b in prepared.blocks:
        assert b.block_id.startswith("blk_") and len(b.block_id) == 64
    again = ev.build_prepared_document(_pdf_content(), version, page_images={1: page_image})
    assert [b.block_id for b in prepared.blocks] == [b.block_id for b in again.blocks]


def test_equal_filenames_different_versions_produce_distinct_block_ids(store, workspace_id):
    v1 = _version(store, workspace_id, content=b"version A", filename="report.pdf")
    v2 = _version(store, workspace_id, content=b"version B", filename="report.pdf")
    prep1 = ev.build_prepared_document(
        _pdf_content(), v1, page_images={1: _page_image(store, workspace_id, v1.version_id)}
    )
    prep2 = ev.build_prepared_document(
        _pdf_content(), v2, page_images={1: _page_image(store, workspace_id, v2.version_id)}
    )
    assert prep1.blocks[0].block_id != prep2.blocks[0].block_id
    assert prep1.blocks[0].version_id == v1.version_id != v2.version_id == prep2.blocks[0].version_id


def test_store_round_trips_prepared_blocks(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    prepared = ev.build_prepared_document(_pdf_content(), version, page_images={1: page_image})
    store.save_blocks(workspace_id, prepared.blocks)
    for b in prepared.blocks:
        assert store.get_block(workspace_id, b.block_id) == b


def test_store_rejects_blocks_for_unknown_version(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    [block] = ev.blocks_from_fragment(_pdf_content().fragments[0], "ver_fake", page_image=page_image)
    with pytest.raises(c.ToolFailure, match="names unknown version"):
        store.save_blocks(workspace_id, [block])


def test_prepared_document_rejects_foreign_version_block(store, workspace_id):
    v1 = _version(store, workspace_id)
    v2 = _version(store, workspace_id, content=b"other")
    page_image = _page_image(store, workspace_id, v1.version_id)
    [block] = ev.blocks_from_fragment(_pdf_content().fragments[0], v2.version_id, page_image=page_image)
    with pytest.raises(ValueError, match="belongs to version"):
        c.PreparedDocument(version=v1, blocks=[block], pages=[page_image])


def test_methods_match_contract_literals():
    def method(t):
        return ev._method_from_fragment(m.Fragment(text="x", loc={"type": t}))

    assert [method(t) for t in ("pdf", "scan", "xlsx", "docx")] == ["native_text", "ocr", "spreadsheet", "docx"]
    with pytest.raises(ValueError):
        method("txt")


# ======================= page regions =======================


def test_native_pdf_points_are_scaled_to_render_pixels(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id, dpi=144)  # 2 px per point
    [block] = ev.blocks_from_content(
        _pdf_content(bbox=(10.2, 20.6, 300.1, 60.4)), version.version_id, page_images={1: page_image}
    )
    box = block.locator.box
    # Scaled by 144/72 and rounded outward, never truncated inward.
    assert (box.x0, box.y0, box.x1, box.y1) == (20, 41, 601, 121)
    assert block.method == "native_text" and block.locator.render_id == page_image.render_id


def test_native_pdf_text_without_render_dpi_fails(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id, dpi=None)
    with pytest.raises(ValueError, match="transform.dpi"):
        ev.blocks_from_content(_pdf_content(), version.version_id, page_images={1: page_image})


def test_box_outside_the_render_fails(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id, dpi=300)  # 800 px wide at 300 dpi
    with pytest.raises(ValueError, match="outside the render"):
        ev.blocks_from_content(_pdf_content(bbox=(200, 20, 300, 60)), version.version_id, page_images={1: page_image})


def test_text_running_past_the_page_edge_is_clipped(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)  # 800x1000 at 72 dpi
    [block] = ev.blocks_from_content(
        _pdf_content(bbox=(700, 20, 812.5, 60)), version.version_id, page_images={1: page_image}
    )
    assert (block.locator.box.x0, block.locator.box.x1) == (700, 800)


def test_thin_box_stays_valid(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    [block] = ev.blocks_from_content(
        _pdf_content(bbox=(10.4, 20.0, 10.4, 20.0)), version.version_id, page_images={1: page_image}
    )
    assert block.locator.box.x1 > block.locator.box.x0 and block.locator.box.y1 > block.locator.box.y0


def test_inverted_box_fails(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id)
    with pytest.raises(ValueError):
        ev.blocks_from_content(_pdf_content(bbox=(300, 60, 10, 20)), version.version_id, page_images={1: page_image})


def test_missing_render_for_named_page_fails(store, workspace_id):
    version = _version(store, workspace_id)
    with pytest.raises(ValueError, match="no stored render for page 1"):
        ev.build_prepared_document(_pdf_content(), version, page_images={})


def test_render_for_another_page_fails(store, workspace_id):
    version = _version(store, workspace_id)
    page_image = _page_image(store, workspace_id, version.version_id, page=2)
    with pytest.raises(ValueError, match="render of page 2"):
        ev.build_prepared_document(_pdf_content(), version, page_images={1: page_image})


def test_ocr_render_must_be_the_ocr_image(store, workspace_id):
    from PIL import Image

    version = _version(store, workspace_id, filename="scan.png")
    content = m.DocContent(
        path="/nonexistent/scan.png",
        name="scan.png",
        fmt="image",
        fragments=[m.Fragment(text="Итого 1000", loc={"type": "scan", "page": 1, "bbox": [5, 5, 50, 20]}, conf=91.5)],
        page_images={1: Image.new("L", (400, 300), 255)},
    )
    wrong = _page_image(store, workspace_id, version.version_id)  # 800x1000, not the OCR image
    with pytest.raises(ValueError, match="OCR boxes are on a 400x300"):
        ev.build_prepared_document(content, version, page_images={1: wrong})
    right = ev.save_page_render(store, workspace_id, version.version_id, 1, content.page_images[1])
    [block] = ev.build_prepared_document(content, version, page_images={1: right}).blocks
    assert block.method == "ocr" and block.quality == c.OcrQuality(engine="tesseract", confidence=91.5)
    assert (block.locator.box.x0, block.locator.box.y1) == (5, 20)


def test_real_native_pdf_highlight_covers_the_printed_amount(store, workspace_id):
    version, content, pages = _real_pdf(store, workspace_id, ACT_174, dpi=150)
    prepared = ev.build_prepared_document(content, version, page_images=pages)
    [amount] = [b for b in prepared.blocks if "318 298,10" in b.text and "оплате" in b.text]
    loc = amount.locator
    # The line ends at about half the page width (the review bug put it at 12%).
    assert 0.4 < loc.box.x1 / loc.image_width < 0.6
    assert loc.transform.dpi == 150
    assert _ink(store, workspace_id, amount) > 20  # the box covers printed text, not blank margin
    store.save_blocks(workspace_id, prepared.blocks)
    assert store.get_block(workspace_id, amount.block_id) == amount


def test_real_rotated_native_pdf_keeps_boxes_on_the_displayed_page(store, workspace_id, tmp_path):
    import pypdfium2 as pdfium

    rotated = tmp_path / "act174_rotated.pdf"
    pdf = pdfium.PdfDocument(str(ACT_174))
    pdf[0].set_rotation(90)
    pdf.save(str(rotated))
    pdf.close()
    version, content, pages = _real_pdf(store, workspace_id, rotated, dpi=100)
    prepared = ev.build_prepared_document(content, version, page_images=pages)
    # The legacy reader groups rotated words into lines poorly (T04); a single word is enough here.
    [word] = [b for b in prepared.blocks if b.text == "оплате"]
    loc = word.locator
    assert loc.image_width > loc.image_height  # landscape render of the turned page
    assert loc.box.y1 - loc.box.y0 > loc.box.x1 - loc.box.x0  # the word now runs down the page
    assert _ink(store, workspace_id, word) > 20


@pytest.mark.ocr
def test_real_png_scan_blocks_point_at_the_ocr_image(store, workspace_id):
    version = _version(store, workspace_id, SCAN_PNG.read_bytes(), SCAN_PNG.name, role="primary")
    content = readers.read_any(str(SCAN_PNG))
    assert not content.errors and content.page_images
    pages = {
        p: ev.save_page_render(store, workspace_id, version.version_id, p, img)
        for p, img in content.page_images.items()
    }
    prepared = ev.build_prepared_document(content, version, page_images=pages)
    assert prepared.blocks and all(b.method == "ocr" for b in prepared.blocks)
    assert all(b.quality.engine == "tesseract" for b in prepared.blocks)
    store.save_blocks(workspace_id, prepared.blocks)


# ======================= DOCX =======================


def test_docx_paragraph_and_table_cells_are_0based_quotes(store, workspace_id):
    version = _version(store, workspace_id, filename="contract.docx")
    para, first, third = ev.build_prepared_document(_docx_content(), version).blocks
    assert (para.locator.paragraph, para.locator.quote) == (0, "Первый абзац")
    assert (first.locator.table, first.locator.row, first.locator.cell, first.locator.quote) == (0, 1, 0, "Статья")
    assert (third.locator.cell, third.text) == (2, "1000")  # the empty middle cell has no block


def test_real_docx_with_a_table_maps_to_the_document(store, workspace_id):
    import docx

    version = _version(store, workspace_id, KL_31.read_bytes(), KL_31.name, role="primary")
    content = readers.read_any(str(KL_31))
    prepared = ev.build_prepared_document(content, version)
    document = docx.Document(str(KL_31))
    cells = [b.locator for b in prepared.blocks if b.locator.table is not None]
    paragraphs = [b.locator for b in prepared.blocks if b.locator.paragraph is not None]
    assert cells and paragraphs
    for loc in cells:
        assert document.tables[loc.table].rows[loc.row].cells[loc.cell].text.strip() == loc.quote
    for loc in paragraphs:
        assert document.paragraphs[loc.paragraph].text.strip() == loc.quote
    assert any("318 298,10" in loc.quote or "245 000" in loc.quote or loc.quote[:1].isdigit() for loc in cells)
    store.save_blocks(workspace_id, prepared.blocks)


# ======================= XLSX =======================


def test_real_xlsx_keeps_formulas_and_never_calls_them_literal(store, workspace_id):
    import openpyxl

    data = PAYROLL_JAN.read_bytes()
    version = _version(store, workspace_id, data, PAYROLL_JAN.name, role="primary")
    content = readers.read_any(str(PAYROLL_JAN))
    prepared = ev.build_prepared_document(content, version, data=data)
    by_ref = {(b.locator.sheet, b.locator.ref): b for b in prepared.blocks}

    wb = openpyxl.load_workbook(PAYROLL_JAN, data_only=False)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    loc = by_ref[(ws.title, cell.coordinate)].locator
                    assert loc.formula == cell.value and loc.evaluation != "literal"

    total = next(b for b in prepared.blocks if b.locator.formula == "=SUM(C6:C7)")
    loc = total.locator
    # The file stores no result for this SUM: no cached value is invented; the derived value is ours (T04),
    # kept apart from the cache, and the block text is still what the file holds.
    assert loc.cached_value is None and loc.evaluation == "derived"
    assert loc.derived_value == Decimal("1110000.00")
    assert loc.inputs == ["C6:C7"] and total.text == "=SUM(C6:C7)"
    assert any(s.ref == loc.ref and s.formula == loc.formula for s in prepared.formula_states)

    literal = by_ref[(loc.sheet, "C6")].locator
    assert literal.evaluation == "literal" and literal.formula is None and literal.cached_value
    store.save_blocks(workspace_id, prepared.blocks)


def test_formula_inputs_keep_sheets_and_ignore_function_names():
    assert ev._formula_inputs("=SUM($C$6:$C$7)+'Лист 2'!B2-Data!A1*2") == ["C6:C7", "Лист 2!B2", "Data!A1"]
    assert ev._formula_inputs("=LOG10(B2)+ROUND(C3,2)") == ["B2", "C3"]


def test_xlsx_fragments_without_a_formula_cell_are_literal(store, workspace_id, tmp_path):
    import openpyxl

    path = tmp_path / "ledger.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Январь"
    wb.active["A2"], wb.active["B2"] = "Итого", 1000
    wb.save(path)
    version = _version(store, workspace_id, path.read_bytes(), "ledger.xlsx")
    prepared = ev.build_prepared_document(readers.read_any(str(path)), version)
    loc = {b.locator.ref: b.locator for b in prepared.blocks}["B2"]
    assert (loc.evaluation, loc.cached_value, loc.formula) == ("literal", "1000", None)
    assert prepared.formula_states == []


# ======================= reader errors =======================


def test_reader_errors_are_kept_not_turned_into_an_empty_success(store, workspace_id, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a pdf")
    version = _version(store, workspace_id, broken.read_bytes(), broken.name, role="primary")
    content = readers.read_any(str(broken))
    assert content.errors
    prepared = ev.build_prepared_document(content, version)
    assert prepared.blocks == []
    assert [e.code for e in prepared.errors] == ["unreadable"] * len(content.errors)
    assert [e.message for e in prepared.errors] == content.errors


def test_text_lines_without_a_contract_locator_are_reported_not_crashed(store, workspace_id):
    readme = CASE19 / "00_README_набор_данных.txt"
    version = _version(store, workspace_id, readme.read_bytes(), readme.name, role="context")
    prepared = ev.build_prepared_document(readers.read_any(str(readme)), version)
    assert prepared.blocks == []
    assert [e.code for e in prepared.errors] == ["unsupported"]


# ======================= rotated renders (T04) =======================


def test_rotate_box_turns_clockwise():
    # A 100x50 image turned clockwise by 90 becomes 50x100; its top-left corner goes to the top-right.
    assert ev.rotate_box((0, 0, 10, 5), (100, 50), 90) == (45, 0, 50, 10)
    assert ev.rotate_box((0, 0, 10, 5), (100, 50), 180) == (90, 45, 100, 50)
    assert ev.rotate_box((0, 0, 10, 5), (100, 50), 270) == (0, 90, 5, 100)
    assert ev.rotate_box((1, 2, 3, 4), (100, 50), 0) == (1, 2, 3, 4)


def test_native_text_is_placed_on_a_rotated_render(store, workspace_id):
    version = _version(store, workspace_id)
    art = store.save_artifact(workspace_id, version.version_id, kind="page_render", media_type="image/png", data=b"r")
    # An 800x1000 px page at 72 dpi, rendered turned clockwise by 90: 1000x800 px.
    page_image = c.PageImage(
        page=1, render_id=art.artifact_id, width=1000, height=800, transform=c.RenderTransform(dpi=72, rotation=90)
    )
    [block] = ev.blocks_from_content(
        _pdf_content(bbox=(10, 20, 300, 60)), version.version_id, page_images={1: page_image}
    )
    box = block.locator.box
    assert (box.x0, box.y0, box.x1, box.y1) == (940, 10, 980, 300)


# ======================= inspection tools (T05) =======================


@pytest.fixture
def tools(store):
    from sverka.document_tools import DocumentTools

    return DocumentTools(store)


def _prepared(store, tools, ws_id: str, path: Path, name: str | None = None):
    version = _version(store, ws_id, path.read_bytes(), name or path.name, role="primary")
    result = tools.prepare_document(ws_id, version.version_id)
    assert result.ok, result.error
    return result.data


def _png(store, ws_id: str, artifact_id: str):
    from io import BytesIO

    from PIL import Image

    ref, data = store.read_artifact(ws_id, artifact_id)
    assert ref.media_type == "image/png"
    return Image.open(BytesIO(data))


def _amount(prepared: c.PreparedDocument) -> c.EvidenceBlock:
    return next(b for b in prepared.blocks if "318 298,10" in b.text)


def test_tools_implement_the_evidence_tools_interface(tools):
    from sverka.interfaces import EvidenceTools

    assert isinstance(tools, EvidenceTools)
    assert tools.tool_versions["evidence_tools"]


def test_read_evidence_returns_stored_blocks_and_rejects_unknown_ones(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, ACT_174)
    ids = [b.block_id for b in prepared.blocks[:3]]
    result = tools.read_evidence(workspace_id, ids)
    assert result.ok and result.data == prepared.blocks[:3]
    missing = tools.read_evidence(workspace_id, [ids[0], "blk_missing"])
    assert not missing.ok and missing.error.code == "not_found" and missing.error.details["missing"] == ["blk_missing"]
    other = store.create_workspace().workspace_id
    assert tools.read_evidence(other, ids).error.code == "not_found"  # another workspace cannot read them


def test_inspect_page_returns_the_listed_render(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, ACT_174)
    result = tools.inspect_page(workspace_id, prepared.version.version_id, 1)
    assert result.ok and result.data == prepared.pages[0]
    assert _png(store, workspace_id, result.data.render_id).size == (result.data.width, result.data.height)
    assert tools.inspect_page(workspace_id, prepared.version.version_id, 2).error.code == "not_found"
    other = store.create_workspace().workspace_id
    assert tools.inspect_page(other, prepared.version.version_id, 1).error.code == "not_found"


def test_inspect_page_of_a_spreadsheet_is_unsupported(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, PAYROLL_JAN)
    assert tools.inspect_page(workspace_id, prepared.version.version_id, 1).error.code == "unsupported"


def test_source_click_crop_shows_the_amount_on_the_right_version(store, workspace_id, tools):
    """V03: the crop behind a claim comes from the cited version's own render and shows ink where the text is."""
    from PIL import ImageStat

    prepared = _prepared(store, tools, workspace_id, ACT_174)
    replacement = _prepared(
        store,
        tools,
        workspace_id,
        ROOT / "tests/fixtures/case19/scenarios/replacement_version/05_Счет-акт_174_25-02-2026_v2.pdf",
        name=ACT_174.name,
    )
    for doc in (prepared, replacement):
        amount = _amount(doc)
        result = tools.inspect_crop(workspace_id, amount.locator.render_id, amount.locator.box)
        assert result.ok
        crop = result.data
        assert crop.kind == "crop" and crop.version_id == doc.version.version_id == amount.version_id
        image = _png(store, workspace_id, crop.artifact_id)
        box = amount.locator.box
        assert image.size == (box.x1 - box.x0, box.y1 - box.y0)
        assert 255 - ImageStat.Stat(image.convert("L")).mean[0] > 20


def test_inspect_crop_rejects_bad_regions_and_foreign_renders(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, ACT_174)
    page = prepared.pages[0]
    outside = c.Box(x0=0, y0=0, x1=page.width + 1, y1=10)
    assert tools.inspect_crop(workspace_id, page.render_id, outside).error.code == "invalid_reference"
    original = prepared.version.original_artifact_id
    inside = c.Box(x0=0, y0=0, x1=10, y1=10)
    assert tools.inspect_crop(workspace_id, original, inside).error.code == "invalid_reference"
    other = store.create_workspace().workspace_id
    assert tools.inspect_crop(other, page.render_id, inside).error.code == "not_found"
    assert tools.inspect_crop(workspace_id, "art_missing", inside).error.code == "not_found"


def test_a_crop_of_a_crop_maps_back_to_the_page(store, workspace_id, tools):
    from PIL import ImageChops

    prepared = _prepared(store, tools, workspace_id, ACT_174)
    page = prepared.pages[0]
    first = tools.inspect_crop(workspace_id, page.render_id, c.Box(x0=100, y0=400, x1=700, y1=600)).data
    second = tools.inspect_crop(workspace_id, first.artifact_id, c.Box(x0=50, y0=100, x1=250, y1=140)).data
    direct = tools.inspect_crop(workspace_id, page.render_id, c.Box(x0=150, y0=500, x1=350, y1=540)).data
    a, b = _png(store, workspace_id, second.artifact_id), _png(store, workspace_id, direct.artifact_id)
    assert ImageChops.difference(a.convert("L"), b.convert("L")).getbbox() is None
    info, _ = ev.read_render_info(store.read_artifact(workspace_id, second.artifact_id)[1])
    assert info["source_render_id"] == page.render_id
    assert info["transform"]["crop"] == {"x0": 150, "y0": 500, "x1": 350, "y1": 540}


@pytest.mark.ocr
def test_reocr_adds_evidence_without_erasing_the_old_reading(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, ACT_174)
    amount = _amount(prepared)
    result = tools.reocr_region(workspace_id, amount.locator.render_id, amount.locator.box)
    assert result.ok
    [block] = result.data
    assert block.method == "reocr" and block.derived_from == amount.block_id
    assert "318 298,10" in block.text
    assert block.locator.render_id == amount.locator.render_id
    assert _overlap(block.locator.box, amount.locator.box) > 0.8
    assert store.get_block(workspace_id, amount.block_id) == amount  # the old reading stays
    assert store.get_block(workspace_id, block.block_id) == block
    again = tools.reocr_region(workspace_id, amount.locator.render_id, amount.locator.box)
    assert [b.block_id for b in again.data] == [block.block_id]  # repeatable, not duplicated


@pytest.mark.ocr
def test_reocr_through_a_crop_lands_on_the_page_render(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, ACT_174)
    amount = _amount(prepared)
    box = amount.locator.box
    crop = tools.inspect_crop(
        workspace_id, amount.locator.render_id, c.Box(x0=box.x0 - 20, y0=box.y0 - 20, x1=box.x1 + 20, y1=box.y1 + 20)
    ).data
    result = tools.reocr_region(
        workspace_id, crop.artifact_id, c.Box(x0=20, y0=20, x1=box.x1 - box.x0 + 20, y1=box.y1 - box.y0 + 20)
    )
    [block] = result.data
    assert block.locator.render_id == amount.locator.render_id
    assert _overlap(block.locator.box, box) > 0.8 and "318 298,10" in block.text


@pytest.mark.ocr
def test_source_click_on_a_rotated_scan_shows_the_amount(store, workspace_id, tools):
    rotated = ROOT / "tests" / "fixtures" / "case19" / "scenarios" / "rotated_scan" / ACT_174.name
    prepared = _prepared(store, tools, workspace_id, rotated)
    amount = _amount(prepared)
    assert amount.locator.transform.rotation == 90
    assert _ink(store, workspace_id, amount) > 20
    result = tools.reocr_region(workspace_id, amount.locator.render_id, amount.locator.box)
    assert any("318 298,10" in b.text for b in result.data)


def test_reocr_without_tesseract_is_unsupported(store, workspace_id, tools, monkeypatch):
    from sverka import ocr

    prepared = _prepared(store, tools, workspace_id, ACT_174)
    monkeypatch.setattr(ocr, "available", lambda: False)
    amount = _amount(prepared)
    result = tools.reocr_region(workspace_id, amount.locator.render_id, amount.locator.box)
    assert not result.ok and result.error.code == "unsupported"


def test_reocr_of_an_overlapping_region_keeps_the_first_reading(store, workspace_id, tools, monkeypatch):
    """A line read again through a wider region has the same text and box, so the same block ID. Tesseract
    reports another confidence there; the call must succeed and return the reading already stored."""
    from sverka import document_tools as dt
    from sverka import ocr

    prepared = _prepared(store, tools, workspace_id, ACT_174)
    amount = _amount(prepared)
    box, render_id = amount.locator.box, amount.locator.render_id
    width, height, pad = box.x1 - box.x0, box.y1 - box.y0, dt.REOCR_PAD
    readings = iter([(0, 91.0), (10, 84.5)])  # how far the region was widened, and the confidence read there

    def read(region, psm=6, lang=None):
        grow, conf = next(readings)
        k = region.width // (width + 2 * (pad + grow))  # reocr_region upscales small regions
        left = top = (pad + grow) * k
        return [("318 298,10", (left, top, left + width * k, top + height * k), conf)]

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_lines", read)
    [first] = tools.reocr_region(workspace_id, render_id, box).data
    assert first.quality.confidence == 91.0
    wider = c.Box(x0=box.x0 - 10, y0=box.y0 - 10, x1=box.x1 + 10, y1=box.y1 + 10)
    result = tools.reocr_region(workspace_id, render_id, wider)
    assert result.ok, result.error
    assert result.data == [first]
    assert store.get_block(workspace_id, first.block_id) == first


def test_crops_resolve_after_a_restart(tmp_path):
    """Render descriptions live in the stored PNGs, so a new process can crop and place boxes."""
    from sverka.document_tools import DocumentTools
    from sverka.storage import SqliteStore

    store = SqliteStore(tmp_path / "data")
    ws_id = store.create_workspace().workspace_id
    prepared = _prepared(store, DocumentTools(store), ws_id, ACT_174)
    amount = _amount(prepared)
    restarted = DocumentTools(SqliteStore(tmp_path / "data"))
    crop = restarted.inspect_crop(ws_id, amount.locator.render_id, amount.locator.box)
    assert crop.ok
    again = restarted.inspect_crop(ws_id, crop.data.artifact_id, c.Box(x0=0, y0=0, x1=10, y1=10))
    assert again.ok
    assert restarted.read_evidence(ws_id, [amount.block_id]).data == [amount]


def test_read_cells_keeps_formula_lineage(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, PAYROLL_JAN)
    vid = prepared.version.version_id
    result = tools.read_cells(workspace_id, vid, "Ведомость", "C6:C8")
    assert result.ok
    assert [(b.locator.ref, b.text, b.locator.evaluation) for b in result.data] == [
        ("C6", "1010000", "literal"),
        ("C7", "100000", "literal"),
        ("C8", "=SUM(C6:C7)", "derived"),
    ]
    total = result.data[2].locator
    assert (total.formula, total.cached_value, total.derived_value, total.inputs) == (
        "=SUM(C6:C7)",
        None,
        Decimal("1110000.00"),
        ["C6:C7"],
    )
    [single] = tools.read_cells(workspace_id, vid, "ведомость", "$C$8").data  # sheet names ignore case
    assert single == result.data[2]
    assert all(store.get_block(workspace_id, b.block_id) == b for b in result.data)


def test_read_cells_rejects_bad_requests(store, workspace_id, tools):
    prepared = _prepared(store, tools, workspace_id, PAYROLL_JAN)
    vid = prepared.version.version_id
    empty = tools.read_cells(workspace_id, vid, "Ведомость", "Z90")
    assert empty.ok and empty.data == [] and empty.warnings
    assert tools.read_cells(workspace_id, vid, "Нет листа", "A1").error.code == "not_found"
    assert tools.read_cells(workspace_id, vid, "Ведомость", "A1:Z100").error.code == "invalid_reference"
    assert tools.read_cells(workspace_id, vid, "Ведомость", "not a ref").error.code == "invalid_reference"
    other = store.create_workspace().workspace_id
    assert tools.read_cells(other, vid, "Ведомость", "C8").error.code == "not_found"
    pdf = _prepared(store, tools, workspace_id, ACT_174)
    assert tools.read_cells(workspace_id, pdf.version.version_id, "Лист1", "A1").error.code == "unsupported"


def test_validate_extraction_delegates_to_the_configured_validator(store, workspace_id):
    from sverka.document_tools import DocumentTools

    calls = []

    def validator(proposal, prepared):
        calls.append((proposal, prepared.version.version_id))
        return c.ToolResult(ok=False, error=c.ToolError(code="invalid_reference", message="checked"))

    store_tools = DocumentTools(store, validator=validator)
    prepared = _prepared(store, store_tools, workspace_id, ACT_174)
    result = store_tools.validate_extraction(workspace_id, "proposal", prepared)
    assert result.error.message == "checked" and calls == [("proposal", prepared.version.version_id)]
    assert DocumentTools(store).validate_extraction(workspace_id, "proposal", prepared).error.code == "unsupported"


def _overlap(a: c.Box, b: c.Box) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0))
