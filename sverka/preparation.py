"""Prepare stored document versions (T04): what each document says, before any interpretation.

``prepare_version(store, workspace_id, version_id)`` reads one stored version and returns a
``contracts.PreparedDocument``. Its page renders and evidence blocks are saved to the store, so block IDs
resolve through ``read_evidence`` and the API, and ``inspect_crop``/``reocr_region`` can work from the stored
renders alone.

* **PDF.** Every native word is kept, grouped into lines, as ``native_text`` blocks on a render at
  ``render_dpi``. A page is also OCR'd when it has no text layer, or when a large image carries only a few
  native words (a scan with a small watermark or stamp): the native words would otherwise hide the scanned
  content. An OCR'd page is rendered at ``ocr_dpi`` or more, turned upright when Tesseract detects its
  orientation, and that upright image is the page render (``transform.rotation`` records the clockwise turn
  and ``transform.dpi`` the resolution). Native words on such a page are placed on the same render.
* **PNG/JPEG/TIFF.** Each frame is OCR'd the same way; the upright OCR image is the render.
* **DOCX.** Paragraph and table-cell quotes (``DocxLocator``), no page rendering. Text the quotes cannot
  address (text boxes, nested tables, content controls, headers and footers) is reported as a warning.
* **XLSX.** One block per non-empty cell with raw formula, cached value and our separately derived value or
  error (``evidence.spreadsheet_evidence``, ``formulas``).

Nothing is repaired or interpreted here: a printed ``1000`` stays ``1000``, report values are not fixed, and
no category is chosen. Read problems are kept as ``errors``: a damaged, empty or unsupported file never looks
like an empty successful preparation. Without Tesseract a scanned page is still rendered, so it can be
inspected as an image, and the missing OCR is an ``unreadable`` error.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from collections.abc import Sequence

from . import config, ocr, readers
from . import contracts as c
from . import evidence as ev
from . import models as m

RENDER_DPI = config.RENDER_DPI
# OCR'd pages are rendered so the long side has at least this many pixels (ocr.normalize_page would
# otherwise upscale the image and the render would no longer have a whole-number dpi).
OCR_MIN_LONG_SIDE = 2200
# A page whose images cover at least this share of it and which has at most this many native words is a scan
# (with a watermark, stamp or scanner footer) and is OCR'd as well.
SCAN_IMAGE_SHARE = 0.5
WATERMARK_MAX_WORDS = 40
# Renders never exceed this many pixels on their long side (large-format drawings would otherwise become
# 100+ megapixel images).
MAX_RENDER_SIDE = 7000
OCR_MISSING = "страница доступна только как изображение"
UNSUPPORTED_KINDS = {
    "legacy": "Старый формат (DOC/XLS/RTF/ODT) не поддерживается: пересохраните файл в DOCX, XLSX или PDF",
    "zip": "Архив внутри набора не поддерживается: загрузите его содержимое отдельными файлами",
    "unknown": "Формат файла не поддерживается: нужны PDF, PNG, JPEG, TIFF, DOCX или XLSX",
}

__all__ = [
    "OCR_MIN_LONG_SIDE",
    "RENDER_DPI",
    "SCAN_IMAGE_SHARE",
    "WATERMARK_MAX_WORDS",
    "prepare_bytes",
    "prepare_version",
]


def prepare_version(
    store, workspace_id: str, version_id: str, *, render_dpi: int | None = None, ocr_dpi: int | None = None
) -> c.PreparedDocument:
    """Prepare one stored version and save its renders and blocks.

    Raises ``ToolFailure`` for an unknown or foreign version (``not_found``) and for stored bytes that fail
    their hash check (``unreadable``). Everything that goes wrong while reading the file itself is kept in
    the returned document's ``errors``.
    """
    version = store.get_version(workspace_id, version_id)
    data = store.read_original(workspace_id, version_id)
    prepared = prepare_bytes(store, version, data, render_dpi=render_dpi, ocr_dpi=ocr_dpi)
    store.save_blocks(workspace_id, prepared.blocks)
    return prepared


def prepare_bytes(
    store, version: c.DocumentVersion, data: bytes, *, render_dpi: int | None = None, ocr_dpi: int | None = None
) -> c.PreparedDocument:
    """Prepare ``data`` (the bytes of ``version``); renders are saved to ``store``, blocks are not."""
    render_dpi = render_dpi or RENDER_DPI
    ocr_dpi = ocr_dpi or config.OCR_DPI
    with tempfile.TemporaryDirectory(prefix="sverka-prepare-") as tmp:
        path = os.path.join(tmp, "document" + _suffix(version.original_filename))
        with open(path, "wb") as f:
            f.write(data)
        kind = readers.sniff(path)
        if kind == "xlsx":
            return _prepare_xlsx(version, data)
        content = m.DocContent(path=path, name=version.original_filename)
        pages: dict[int, c.PageImage] = {}
        try:
            if kind == "pdf":
                _read_pdf(store, version, content, pages, render_dpi, ocr_dpi)
            elif kind == "image":
                _read_image(store, version, content, pages)
            elif kind in UNSUPPORTED_KINDS:
                return c.PreparedDocument(
                    version=version, errors=[c.ToolError(code="unsupported", message=UNSUPPORTED_KINDS[kind])]
                )
            else:  # DOCX and text; empty or damaged files come back with reader errors
                content = readers.read_any(path)
                content.name = version.original_filename
                _save_reader_images(store, version, content, pages)
                if kind == "docx" and not content.errors:
                    content.warnings.extend(_docx_coverage_warnings(path))
        except Exception as exc:  # a damaged file is an error, never an empty success
            content.errors.append(f"Ошибка чтения файла: {type(exc).__name__}: {exc}")
        try:
            prepared = ev.build_prepared_document(content, version, page_images=pages, data=data)
        except ValueError as exc:  # coordinates that do not fit the render: report, never misplace
            prepared = c.PreparedDocument(
                version=version,
                pages=[pages[p] for p in sorted(pages)],
                warnings=list(content.warnings),
                errors=[c.ToolError(code="unreadable", message=f"Не удалось разместить фрагменты на странице: {exc}")],
            )
    return _mark_retryable(_require_content(prepared))


# ======================= helpers =======================


def _mark_retryable(prepared: c.PreparedDocument) -> c.PreparedDocument:
    """Missing OCR is an installation problem, not a property of the file: preparing again may succeed."""
    for i, error in enumerate(prepared.errors):
        if OCR_MISSING in error.message or "Tesseract" in error.message:
            prepared.errors[i] = error.model_copy(update={"retryable": True})
    return prepared


def _suffix(filename: str) -> str:
    suffix = os.path.splitext(filename)[1].lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""


def _require_content(prepared: c.PreparedDocument) -> c.PreparedDocument:
    """A document with no evidence and no recorded problem would look like an empty success."""
    if prepared.blocks or prepared.errors:
        return prepared
    message = "В файле не найден текст" + (" (страницы доступны как изображения)" if prepared.pages else "")
    prepared.errors.append(c.ToolError(code="unreadable", message=message))
    return prepared


def _ocr_warning(confs: Sequence[float]) -> str:
    return f"Документ распознан через OCR, средняя уверенность {sum(confs) / len(confs):.0f}%"


# ======================= PDF =======================


def _image_share(page) -> float:
    """Share of the page covered by its images (clipped to the page, capped at 1)."""
    area = float(page.width) * float(page.height)
    if area <= 0:
        return 0.0
    covered = 0.0
    for im in page.images:
        x0, x1 = max(0.0, float(im["x0"])), min(float(page.width), float(im["x1"]))
        y0, y1 = max(0.0, float(im["top"])), min(float(page.height), float(im["bottom"]))
        if x1 > x0 and y1 > y0:
            covered += (x1 - x0) * (y1 - y0)
    return min(1.0, covered / area)


def _capped_dpi(page, dpi: int) -> int:
    long_side = max(float(page.width), float(page.height))
    if long_side <= 0:
        return dpi
    return max(1, min(dpi, math.floor(MAX_RENDER_SIDE * 72 / long_side)))


def _ocr_render_dpi(page, ocr_dpi: int) -> int:
    long_side = max(float(page.width), float(page.height))
    if long_side <= 0:
        return ocr_dpi
    return _capped_dpi(page, max(ocr_dpi, math.ceil((OCR_MIN_LONG_SIDE + 2) * 72 / long_side)))


def _read_pdf(store, version, content: m.DocContent, pages: dict, render_dpi: int, ocr_dpi: int) -> None:
    import pdfplumber

    content.fmt = "pdf"
    confs: list[float] = []
    with pdfplumber.open(content.path) as pdf:
        if not pdf.pages:
            content.errors.append("PDF не содержит страниц")
            return
        for pno, page in enumerate(pdf.pages, 1):
            try:
                words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            except Exception as exc:
                words = []
                content.warnings.append(f"Стр. {pno}: текстовый слой не прочитан ({type(exc).__name__})")
            native = [
                m.Fragment(text, {"type": "pdf", "page": pno, "bbox": bbox})
                for text, bbox in readers._group_words(words)
            ]
            share = _image_share(page)
            scanned = len(words) < 3 or (share >= SCAN_IMAGE_SHARE and len(words) <= WATERMARK_MAX_WORDS)
            if not scanned:
                dpi = _capped_dpi(page, render_dpi)
                image = page.to_image(resolution=dpi).original
                pages[pno] = ev.save_page_render(store, version.workspace_id, version.version_id, pno, image, dpi=dpi)
                content.fragments.extend(native)
                continue
            if words:
                content.warnings.append(
                    f"Стр. {pno}: скан с текстовым слоем из {len(words)} слов (водяной знак или штамп); "
                    "страница распознана OCR, текстовый слой сохранен отдельно"
                )
            dpi = _ocr_render_dpi(page, ocr_dpi)
            image = page.to_image(resolution=dpi).original
            try:
                lines, upright, conf, rotation = ocr.ocr_page_oriented(image)
            except ocr.OcrUnavailable as exc:
                gray = image.convert("L")
                pages[pno] = ev.save_page_render(store, version.workspace_id, version.version_id, pno, gray, dpi=dpi)
                content.page_images[pno] = gray
                content.fragments.extend(native)
                content.errors.append(f"Стр. {pno}: {exc}; {OCR_MISSING}")
                continue
            base = upright.size if rotation in (0, 180) else upright.size[::-1]
            if base != image.size:
                # normalize_page rescaled the page, so the render has no whole-number dpi for native words.
                content.warnings.append(
                    f"Стр. {pno}: изображение для OCR изменило размер; текстовый слой страницы не размещен на нем"
                )
                native = []
            content.ocr = True
            content.page_images[pno] = upright
            content.page_rotations[pno] = rotation
            pages[pno] = ev.save_page_render(
                store, version.workspace_id, version.version_id, pno, upright, dpi=dpi, rotation=rotation
            )
            if rotation:
                content.warnings.append(
                    f"Стр. {pno}: страница повернута на {rotation}° по часовой стрелке для распознавания"
                )
            confs.append(conf)
            for text, bbox, line_conf in lines:
                content.fragments.append(m.Fragment(text, {"type": "scan", "page": pno, "bbox": bbox}, conf=line_conf))
            content.fragments.extend(native)
    if confs:
        content.warnings.append(_ocr_warning(confs))


# ======================= images =======================


def _read_image(store, version, content: m.DocContent, pages: dict) -> None:
    from PIL import Image, ImageOps

    content.fmt = "image"
    confs: list[float] = []
    with Image.open(content.path) as im:
        frames = getattr(im, "n_frames", 1)
        for i in range(frames):
            if frames > 1:
                im.seek(i)
            frame = ImageOps.exif_transpose(im.copy())  # the page as a viewer shows it
            pno = i + 1
            try:
                lines, upright, conf, rotation = ocr.ocr_page_oriented(frame)
            except ocr.OcrUnavailable as exc:
                gray = frame.convert("L")
                pages[pno] = ev.save_page_render(store, version.workspace_id, version.version_id, pno, gray)
                content.page_images[pno] = gray
                content.errors.append(f"Стр. {pno}: {exc}; {OCR_MISSING}")
                continue
            content.ocr = True
            content.page_images[pno] = upright
            content.page_rotations[pno] = rotation
            pages[pno] = ev.save_page_render(
                store, version.workspace_id, version.version_id, pno, upright, rotation=rotation
            )
            if rotation:
                content.warnings.append(
                    f"Стр. {pno}: изображение повернуто на {rotation}° по часовой стрелке для распознавания"
                )
            confs.append(conf)
            for text, bbox, line_conf in lines:
                content.fragments.append(m.Fragment(text, {"type": "scan", "page": pno, "bbox": bbox}, conf=line_conf))
    if confs:
        content.warnings.append(_ocr_warning(confs))


def _save_reader_images(store, version, content: m.DocContent, pages: dict) -> None:
    """Images the reader OCR'd inside another format (pictures in a DOCX) become page renders."""
    for pno, image in sorted(content.page_images.items()):
        rotation = content.page_rotations.get(pno, 0)
        pages[pno] = ev.save_page_render(store, version.workspace_id, version.version_id, pno, image, rotation=rotation)


# ======================= DOCX =======================


def _docx_coverage_warnings(path: str) -> list[str]:
    """Report DOCX text that paragraph/table-cell quotes cannot address, so it is not lost silently.

    Addressable text is what the reader quotes: runs of the body's paragraphs and of the paragraphs of its
    tables' cells, each cell counted once. Everything else under the body (text boxes, nested tables,
    content controls, tracked insertions) is counted as missing.
    """
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(path)
    body = document.element.body

    def chars(nodes) -> int:
        return sum(len(re.sub(r"\s+", "", t.text or "")) for t in nodes)

    total = chars(body.iter(qn("w:t")))
    paragraphs = body.xpath("./w:p | ./w:tbl/w:tr/w:tc/w:p")
    covered = sum(chars(p.xpath("./w:r/w:t | ./w:hyperlink/w:r/w:t")) for p in paragraphs)
    warnings = []
    missing = total - covered
    if missing > 0:
        warnings.append(
            f"В DOCX около {missing} символов текста вне абзацев и ячеек таблиц (надписи, вложенные таблицы, "
            "элементы управления); они не стали доказательствами — проверьте документ визуально"
        )
    seen: set[int] = set()
    headers: list[str] = []
    for section in document.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            try:
                if part.is_linked_to_previous or id(part._element) in seen:
                    continue
                seen.add(id(part._element))
                headers.extend(p.text.strip() for p in part.paragraphs if p.text.strip())
            except Exception:
                continue
    if headers:
        sample = "; ".join(headers)[:120]
        warnings.append(f"Колонтитулы DOCX не входят в доказательства: «{sample}»")
    return warnings


# ======================= XLSX =======================


def _prepare_xlsx(version: c.DocumentVersion, data: bytes) -> c.PreparedDocument:
    try:
        blocks, states, warnings = ev.spreadsheet_evidence(data, version.version_id)
    except Exception as exc:
        return _require_content(
            c.PreparedDocument(
                version=version,
                errors=[c.ToolError(code="unreadable", message=f"Файл XLSX не прочитан: {type(exc).__name__}: {exc}")],
            )
        )
    unresolved = [s for s in states if s.evaluation in ("unresolved", "error")]
    if unresolved:
        warnings.append(
            f"Не вычислено формул: {len(unresolved)} (" + ", ".join(f"{s.sheet}!{s.ref}" for s in unresolved[:5]) + ")"
        )
    return _require_content(
        c.PreparedDocument(version=version, blocks=blocks, formula_states=states, warnings=warnings)
    )
