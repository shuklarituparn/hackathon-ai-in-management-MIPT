"""Versioned evidence (T03): adapt legacy reader output to the v1 evidence contracts.

``readers.read_any`` returns a ``models.DocContent`` of fragments. This module turns it into a
``contracts.PreparedDocument`` whose ``EvidenceBlock``s point at one stored ``DocumentVersion``:

- PDF/PNG/JPEG: ``PageRegionLocator`` with a pixel box on a stored page render. Native PDF text
  arrives in PDF points (72 per inch) of the displayed page, as pdfplumber reports it, and is scaled
  by ``transform.dpi / 72``; the render must be that displayed page drawn at ``transform.dpi``
  (``page.to_image(resolution=dpi)``). OCR boxes are already pixels of the OCR image
  (``DocContent.page_images[page]``), so that exact image must be the render.
- DOCX: ``DocxLocator`` with the exact quote. Paragraphs are numbered from 0 among the body's
  paragraphs; a table row becomes one block per non-empty cell (``table``/``row``/``cell`` from 0,
  where ``cell`` counts the row's distinct cells, so a merged cell counts once).
- XLSX: one ``CellLocator`` per non-empty cell, read from the workbook itself because the reader
  replaces formulas with values. A formula keeps its text and cached value; ``formulas`` derives its value
  separately with Decimal (``derived``), or leaves it ``unresolved``/``error`` with a reason (T04).

Reader errors become ``PreparedDocument.errors``, so an unreadable file never looks like an empty
successful preparation. Versions and render artifacts come from the store; this module mints
neither.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections.abc import Mapping
from typing import Literal

from sverka import contracts as c
from sverka import formulas as fx
from sverka import models as m
from sverka.readers import _cell_str

PDF_POINTS_PER_INCH = 72

_PAGE_TYPES = ("pdf", "scan", "image")
_METHODS: dict[str, c.ExtractionMethod] = {
    "pdf": "native_text",
    "scan": "ocr",
    "image": "ocr",
    "xlsx": "spreadsheet",
    "docx": "docx",
}
# A cell or range inside a formula, optionally on another sheet: B2, $C$6:$C$7, 'Лист 1'!B2, Data!A1:B3.
_FORMULA_REF = re.compile(
    r"(?<![A-Za-z0-9_.])(?:(?:'(?P<quoted>[^']+)'|(?P<sheet>[A-Za-z0-9_.А-Яа-яЁё]+))!)?"
    r"(?P<ref>\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?)(?![A-Za-z0-9_(])"
)


def _method_from_fragment(frag: m.Fragment) -> c.ExtractionMethod:
    """Map a ``models.Fragment`` to one of the contract's ``ExtractionMethod`` literals."""
    t = (frag.loc or {}).get("type")
    if t not in _METHODS:
        raise ValueError(f"no contract extraction method for fragment type {t!r}")
    return _METHODS[t]


def _locator_fingerprint(locator: c.Locator) -> str:
    """Stable, order-independent fingerprint of a contract locator."""
    data = locator.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_block_id(version_id: str, locator: c.Locator, text: str, method: c.ExtractionMethod) -> c.BlockId:
    """Stable ``blk_`` ID derived from (version, locator, method, text).

    The same bytes prepared again give the same IDs; another version never shares them.
    """
    h = hashlib.sha256()
    for part in (version_id, _locator_fingerprint(locator), method, text):
        h.update(part.encode("utf-8"))
        h.update(b"|")
    return f"blk_{h.hexdigest()[:60]}"


# ======================= page regions =======================


def _edges(lo: float, hi: float, limit: int, axis: str) -> tuple[int, int]:
    """Round a span outward to whole pixels and clip it to ``[0, limit]``.

    Text can run past the page edge (act 174 has a line ending at 607 pt on a 595 pt page), so the
    visible part is kept. A span entirely off the render means a wrong coordinate space and fails.
    """
    a, b = max(0, math.floor(lo)), min(limit, math.ceil(hi))
    if b <= a:
        if lo >= limit or hi <= 0:
            raise ValueError(f"box lies outside the render ({axis} {lo:.1f}..{hi:.1f} of {limit} px)")
        b = a + 1 if a < limit else a  # a zero-width line still gets a visible one-pixel box
        a = b - 1
    return a, b


def clip_box(x0: float, y0: float, x1: float, y1: float, width: int, height: int) -> c.Box:
    """Pixel box rounded outward and clipped to a ``width`` x ``height`` render (see ``_edges``)."""
    left, right = _edges(x0, x1, width, "x")
    top, bottom = _edges(y0, y1, height, "y")
    return c.Box(x0=left, y0=top, x1=right, y1=bottom)


def _unrotated_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    return (height, width) if rotation in (90, 270) else (width, height)


def rotate_box(
    box: tuple[float, float, float, float], size: tuple[float, float], rotation: int
) -> tuple[float, float, float, float]:
    """Map a box on an image of ``size`` (width, height) onto that image turned clockwise by ``rotation``."""
    x0, y0, x1, y1 = box
    w, h = size
    if rotation == 90:
        return h - y1, x0, h - y0, x1
    if rotation == 180:
        return w - x1, h - y1, w - x0, h - y0
    if rotation == 270:
        return y0, w - x1, y1, w - x0
    if rotation == 0:
        return box
    raise ValueError(f"rotation must be 0, 90, 180 or 270, got {rotation}")


def _pixel_box(frag_type: str, loc: dict, page_image: c.PageImage) -> c.Box:
    """Convert a fragment bbox into pixels of ``page_image``, rounding outward and clipping to the render."""
    raw = loc.get("bbox")
    if not raw or len(raw) != 4:
        raise ValueError(f"a page-region fragment needs a 4-number bbox, got {raw!r}")
    x0, y0, x1, y1 = (float(v) for v in raw)
    if x1 < x0 or y1 < y0:
        raise ValueError(f"inverted bbox {raw!r}")
    if frag_type == "pdf":
        dpi = page_image.transform.dpi
        if dpi is None:
            raise ValueError(
                f"page {page_image.page}: native PDF text is in points; the render's transform.dpi is needed "
                "to convert it to pixels"
            )
        k = dpi / PDF_POINTS_PER_INCH
        x0, y0, x1, y1 = x0 * k, y0 * k, x1 * k, y1 * k
        rotation = page_image.transform.rotation
        if rotation:
            # Points are on the displayed page; the render shows that page turned clockwise by ``rotation``.
            if page_image.transform.crop is not None:
                raise ValueError(f"page {page_image.page}: native text cannot be placed on a rotated crop")
            x0, y0, x1, y1 = rotate_box(
                (x0, y0, x1, y1), _unrotated_size(page_image.width, page_image.height, rotation), rotation
            )
    crop = page_image.transform.crop
    if crop is not None:
        x0, y0, x1, y1 = x0 - crop.x0, y0 - crop.y0, x1 - crop.x0, y1 - crop.y0
    return clip_box(x0, y0, x1, y1, page_image.width, page_image.height)


def _check_ocr_render(content: m.DocContent, page: int, page_image: c.PageImage) -> None:
    """OCR boxes are pixels of the OCR image, so the stored render must have exactly its size."""
    img = content.page_images.get(page)
    size = getattr(img, "size", None)
    if size is None or page_image.transform.crop is not None:
        return
    if tuple(size) != (page_image.width, page_image.height):
        raise ValueError(
            f"page {page}: the render is {page_image.width}x{page_image.height} px but the OCR boxes are on a "
            f"{size[0]}x{size[1]} px image; save DocContent.page_images[{page}] as the render"
        )


def locator_from_fragment(
    frag: m.Fragment, *, page_image: c.PageImage | None = None, cell: int | None = None
) -> c.Locator:
    """Contract locator for one fragment (for a DOCX table row, one of its cells).

    Spreadsheet cells need the workbook; use ``spreadsheet_blocks``.
    """
    loc = frag.loc or {}
    t = loc.get("type")

    if t in _PAGE_TYPES:
        if page_image is None:
            raise ValueError(f"{t!r} locator needs a stored PageImage (render_id, width, height)")
        page = int(loc.get("page", 1))
        if page_image.page != page:
            raise ValueError(f"the render is for page {page_image.page}, not page {page}")
        return c.PageRegionLocator(
            page=page,
            render_id=page_image.render_id,
            box=_pixel_box(t, loc, page_image),
            image_width=page_image.width,
            image_height=page_image.height,
            transform=page_image.transform,
        )

    if t == "docx":
        # The reader numbers paragraphs, tables and rows from 1; the contract counts from 0.
        if "table" in loc:
            cells = frag.cells or []
            if cell is None or not 0 <= cell < len(cells):
                raise ValueError(f"a DOCX table row has {len(cells)} cells; pass a cell index")
            return c.DocxLocator(
                quote=str(cells[cell]).strip(),
                table=int(loc["table"]) - 1,
                row=int(loc["row"]) - 1,
                cell=cell,
            )
        return c.DocxLocator(quote=frag.text, paragraph=int(loc["paragraph"]) - 1)

    if t == "xlsx":
        raise ValueError("spreadsheet cells keep their formulas only in the workbook; use spreadsheet_blocks")
    raise ValueError(f"no contract locator for fragment type {t!r}")


def _block(version_id: str, text: str, method: c.ExtractionMethod, locator: c.Locator, quality=None):
    return c.EvidenceBlock(
        version_id=version_id,
        block_id=make_block_id(version_id, locator, text, method),
        text=text,
        method=method,
        locator=locator,
        quality=quality,
    )


def blocks_from_fragment(
    frag: m.Fragment, version_id: c.VersionId, *, page_image: c.PageImage | None = None
) -> list[c.EvidenceBlock]:
    """Adapt one non-spreadsheet fragment: one block, or one per non-empty cell of a DOCX table row."""
    loc = frag.loc or {}
    method = _method_from_fragment(frag)
    if loc.get("type") == "docx" and "table" in loc:
        out = []
        for i, value in enumerate(frag.cells or []):
            text = str(value).strip()
            if text:
                out.append(_block(version_id, text, method, locator_from_fragment(frag, cell=i)))
        return out
    quality = None
    if method == "ocr":
        conf = None if frag.conf is None else min(100.0, max(0.0, float(frag.conf)))
        quality = c.OcrQuality(engine="tesseract", confidence=conf)
    locator = locator_from_fragment(frag, page_image=page_image)
    return [_block(version_id, frag.text, method, locator, quality)]


# ======================= spreadsheets =======================


def _formula_inputs(formula: str) -> list[str]:
    out: list[str] = []
    for mt in _FORMULA_REF.finditer(formula):
        ref = mt["ref"].replace("$", "")
        sheet = mt["quoted"] or mt["sheet"]
        name = f"{sheet}!{ref}" if sheet else ref
        if name not in out:
            out.append(name)
    return out


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def spreadsheet_evidence(
    data: bytes | str, version_id: c.VersionId
) -> tuple[list[c.EvidenceBlock], list[c.FormulaState], list[str]]:
    """Blocks, formula states and warnings for every non-empty cell of every sheet.

    A formula keeps its original text and the value cached in the file (if any); ``formulas`` derives its
    value separately with ``Decimal`` (``derived``), or records why it cannot (``unresolved``/``error``, with
    the reason in ``FormulaState.error``). The block text is what the file shows: the cached value, or the
    formula itself when the file stores none. A cached value that differs from the derived one is kept and
    reported as a warning; the last digits of a binary float (``0.30000000000000004``) are not a difference.
    """
    import openpyxl

    source = io.BytesIO(data) if isinstance(data, bytes) else data
    formulas = openpyxl.load_workbook(source, data_only=False)
    if isinstance(source, io.BytesIO):
        source.seek(0)
    values = openpyxl.load_workbook(source, data_only=True)
    evaluator = fx.WorkbookEvaluator(formulas)
    blocks: list[c.EvidenceBlock] = []
    states: list[c.FormulaState] = []
    warnings: list[str] = []
    for ws in formulas.worksheets:
        wv = values[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                raw = cell.value
                if _is_blank(raw):
                    continue
                if fx.is_formula(raw):
                    formula = fx.formula_text(raw)
                    cached = wv[cell.coordinate].value
                    cached_text = None if _is_blank(cached) else _cell_str(cached)
                    result = evaluator.evaluate(ws.title, cell.coordinate)
                    state = dict(
                        sheet=ws.title,
                        ref=cell.coordinate,
                        formula=formula,
                        cached_value=cached_text,
                        derived_value=result.value,
                        evaluation=result.state,
                        inputs=_formula_inputs(formula),
                    )
                    text = cached_text if cached_text is not None else formula
                    states.append(c.FormulaState(**state, error=result.reason))
                    if (
                        result.state == "derived"
                        and isinstance(cached, (int, float))
                        and not isinstance(cached, bool)
                        and not fx.agrees_with_cached(cached, result.value)
                    ):
                        warnings.append(
                            f"Лист «{ws.title}», {cell.coordinate}: сохраненное в файле значение {cached_text} "
                            f"не совпадает с пересчитанным {format(result.value, 'f')} по формуле {formula}"
                        )
                else:
                    text = _cell_str(raw)
                    if not text:
                        continue
                    state = dict(sheet=ws.title, ref=cell.coordinate, cached_value=text, evaluation="literal")
                blocks.append(_block(version_id, text, "spreadsheet", c.CellLocator(**state)))
    return blocks, states, warnings


def spreadsheet_blocks(
    data: bytes | str, version_id: c.VersionId
) -> tuple[list[c.EvidenceBlock], list[c.FormulaState]]:
    """One block per non-empty cell of every sheet, read from the workbook bytes or path, and the formula
    states. See ``spreadsheet_evidence``, which also returns warnings."""
    blocks, states, _ = spreadsheet_evidence(data, version_id)
    return blocks, states


# ======================= documents =======================


def _errors(content: m.DocContent, skipped: set[str]) -> list[c.ToolError]:
    errors = [c.ToolError(code="unreadable", message=e) for e in content.errors if str(e).strip()]
    for t in sorted(skipped):
        errors.append(
            c.ToolError(
                code="unsupported",
                message=f"Фрагменты типа «{t}» не имеют локатора в контракте v1 и не стали доказательствами.",
            )
        )
    return errors


def _prepare(
    content: m.DocContent, version_id: str, page_images: Mapping[int, c.PageImage] | None, data: bytes | None
) -> tuple[list[c.EvidenceBlock], list[c.FormulaState], set[str]]:
    page_images = dict(page_images or {})
    for page, image in page_images.items():
        if image.page != page:
            raise ValueError(f"page_images[{page}] is the render of page {image.page}")
    blocks: list[c.EvidenceBlock] = []
    states: list[c.FormulaState] = []
    skipped: set[str] = set()
    spreadsheet_done = False
    for frag in content.fragments:
        loc = frag.loc or {}
        t = loc.get("type")
        if t == "xlsx":
            if not spreadsheet_done:
                cells, states = spreadsheet_blocks(data if data is not None else content.path, version_id)
                blocks.extend(cells)
                spreadsheet_done = True
            continue
        if t in _PAGE_TYPES:
            page = int(loc.get("page", 1))
            page_image = page_images.get(page)
            if page_image is None:
                raise ValueError(
                    f"no stored render for page {page}; save the page_render artifact and pass its PageImage "
                    "in page_images"
                )
            if t != "pdf":
                _check_ocr_render(content, page, page_image)
            blocks.extend(blocks_from_fragment(frag, version_id, page_image=page_image))
        elif t in _METHODS:
            blocks.extend(blocks_from_fragment(frag, version_id))
        else:
            skipped.add(str(t))
    return blocks, states, skipped


def blocks_from_content(
    content: m.DocContent,
    version_id: c.VersionId,
    *,
    page_images: Mapping[int, c.PageImage] | None = None,
    data: bytes | None = None,
) -> list[c.EvidenceBlock]:
    """Every evidence block of a ``DocContent``.

    Page-region fragments require ``page_images[page]``: the caller has already saved the
    ``page_render`` artifact (``save_page_render``). A missing render is an error, not a fallback.
    Spreadsheet cells are read from ``data`` (the version's bytes) or, failing that, ``content.path``.
    """
    return _prepare(content, version_id, page_images, data)[0]


def build_prepared_document(
    content: m.DocContent,
    version: c.DocumentVersion,
    *,
    page_images: Mapping[int, c.PageImage] | None = None,
    data: bytes | None = None,
) -> c.PreparedDocument:
    """Adapt a ``models.DocContent`` into a contract ``PreparedDocument`` for ``version``.

    ``version`` must come from the store (``save_document``); ``page_images`` maps each 1-based page
    with page-region fragments to its stored render. Reader errors are kept as ``errors``.
    """
    blocks, states, skipped = _prepare(content, version.version_id, page_images, data)
    return c.PreparedDocument(
        version=version,
        blocks=blocks,
        pages=[page_images[p] for p in sorted(page_images or {})],
        formula_states=states,
        warnings=list(content.warnings),
        errors=_errors(content, skipped),
    )


def save_page_render(
    store,
    workspace_id: str,
    version_id: str,
    page: int,
    image,
    *,
    dpi: int | None = None,
    rotation: Literal[0, 90, 180, 270] = 0,
) -> c.PageImage:
    """Save a PIL image as the ``page_render`` artifact of one page and describe it as a ``PageImage``.

    For native PDF text pass the pdfplumber render and its ``dpi``. For OCR pass
    ``DocContent.page_images[page]`` unchanged, since the OCR boxes are pixels of that image.
    """
    transform = c.RenderTransform(rotation=rotation, dpi=dpi)
    data = render_png(image, {"page": page, "transform": transform.model_dump(mode="json")})
    art = store.save_artifact(workspace_id, version_id, kind="page_render", media_type="image/png", data=data)
    return c.PageImage(
        page=page, render_id=art.artifact_id, width=image.width, height=image.height, transform=transform
    )


RENDER_INFO_KEY = "sverka-render"


def render_png(image, info: dict) -> bytes:
    """PNG bytes of a render with its description (page, transform, crop origin) in a text chunk.

    The description lets inspection tools place a crop or a re-OCR box back on its page from the stored
    artifact alone, also after a restart. Only renders this application saves are read this way.
    """
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text(RENDER_INFO_KEY, json.dumps(info, sort_keys=True, ensure_ascii=False))
    buf = io.BytesIO()
    image.save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()


def read_render_info(data: bytes) -> tuple[dict | None, tuple[int, int]]:
    """The description stored by ``render_png`` (or None) and the image size in pixels."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        raw = getattr(img, "text", {}).get(RENDER_INFO_KEY)
        size = img.size
    try:
        info = json.loads(raw) if raw else None
    except ValueError:
        info = None
    return (info if isinstance(info, dict) else None), size


__all__ = [
    "PDF_POINTS_PER_INCH",
    "RENDER_INFO_KEY",
    "blocks_from_content",
    "blocks_from_fragment",
    "build_prepared_document",
    "clip_box",
    "locator_from_fragment",
    "make_block_id",
    "read_render_info",
    "render_png",
    "rotate_box",
    "save_page_render",
    "spreadsheet_blocks",
    "spreadsheet_evidence",
]
