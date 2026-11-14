"""Evidence tools over the document store: preparation (T04) and inspection (T05).

``DocumentTools(store)`` implements ``interfaces.EvidenceTools`` for the harness and the API:

* ``prepare_document`` prepares a stored version (``preparation.prepare_version``) and keeps the result in a
  bounded per-process cache, so the same ``PreparedDocument`` comes back afterwards. A preparation whose error
  is retryable (OCR missing) is not cached. A file with nothing to show (no blocks, no page renders) and an
  ``unreadable`` problem fails with ``unreadable``, so it is listed as unreadable rather than handed to the
  model as an empty document.
* ``read_evidence`` returns stored blocks; any unknown or foreign ID fails the whole request.
* ``inspect_page`` returns the page render listed in the prepared document.
* ``inspect_crop`` saves the exact pixels of a box on a page render (or on an earlier crop) as a ``crop``
  artifact, unscaled, so the crop's own pixels map back to the page by an offset.
* ``reocr_region`` runs Tesseract again on a box and adds ``reocr`` blocks on the full page render. Earlier
  readings stay; a new block names the block it overlaps most in ``derived_from``. A line read again with the
  same text and box, also through an overlapping region, is returned as first stored.
* ``read_cells`` returns the spreadsheet blocks of a cell or range with formula, cached value, derived value or
  error state and input-cell lineage.
* ``validate_extraction`` delegates to the injected T06 validator; without one it fails with ``unsupported``.

Renders carry their page, transform and crop origin in a PNG text chunk (``evidence.render_png``), so crops
and re-OCR boxes resolve from the stored artifacts alone, also after a restart. Every ID is resolved inside
its workspace; an ID from another workspace is ``not_found``. Tools never raise: failures are typed
``ToolResult`` errors.
"""

from __future__ import annotations

import copy
import functools
import io
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from . import contracts as c
from . import evidence as ev
from . import ocr, preparation

TOOLS_VERSION = "1.0.0"
MAX_CELLS = 500  # read_cells range limit
MAX_CACHED = 128  # prepared documents kept per process (least recently used are dropped)
REOCR_PAD = 6  # pixels added around a re-OCR box so glyphs on its edge are whole

__all__ = ["MAX_CELLS", "TOOLS_VERSION", "DocumentTools", "Validator"]

Validator = Callable[[Any, c.PreparedDocument], Any]
_BOX = TypeAdapter(c.Box)
_CELL_REF = TypeAdapter(c.CellRef)


def _argument(adapter: TypeAdapter, value: Any, name: str) -> Any:
    """Validate a tool argument; a bad argument is ``invalid_reference``, unlike an internal model error."""
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise c.ToolFailure("invalid_reference", f"invalid {name}: {exc.errors()[0]['msg']}") from None


def _tool(fn):
    """Run a tool body; typed failures and unexpected exceptions become failed ``ToolResult``s."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> c.ToolResult:
        try:
            return fn(*args, **kwargs)
        except c.ToolFailure as exc:
            return c.error_result(exc)
        except Exception as exc:  # a broken tool is a failed tool, never an empty success
            message = f"{fn.__name__} failed: {type(exc).__name__}: {exc}"
            return c.error_result(c.ToolError(code="unreadable", message=message[:500]))

    return wrapper


@dataclass(frozen=True)
class _Render:
    """A page render or crop resolved to its full-page render."""

    ref: c.ArtifactRef
    page: int
    base_id: str  # the page render all coordinates go back to
    base_size: tuple[int, int]
    transform: c.RenderTransform  # of the page render
    origin: tuple[int, int]  # top-left of this render on the page render
    size: tuple[int, int]  # this render's own pixel size


class DocumentTools:
    """``interfaces.EvidenceTools`` backed by a ``DocumentStore`` (``SqliteStore`` or ``fakes.InMemoryStore``)."""

    tool_versions = {"evidence_tools": TOOLS_VERSION}

    def __init__(
        self,
        store,
        *,
        validator: Validator | None = None,
        render_dpi: int | None = None,
        ocr_dpi: int | None = None,
    ) -> None:
        self.store = store
        self.validator = validator
        self.render_dpi = render_dpi
        self.ocr_dpi = ocr_dpi
        self._cache: OrderedDict[tuple[str, str], c.PreparedDocument] = OrderedDict()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._sheets: dict[tuple[str, str], list[str]] = {}
        self._guard = threading.Lock()

    def with_validator(self, validator: Validator | None) -> DocumentTools:
        """These tools with another validator, sharing the store and the cache of prepared documents.

        A check uses its own validator (it remembers that check's report), while preparations are reused
        between checks of one process.
        """
        clone = copy.copy(self)
        clone.validator = validator
        return clone

    # ======================= preparation (T04) =======================

    def prepared(self, workspace_id: str, version_id: str) -> c.PreparedDocument:
        """A copy of the prepared version, prepared on first use. Raises ``ToolFailure`` for unknown/foreign
        versions."""
        return self._prepared(workspace_id, version_id).model_copy(deep=True)

    def _prepared(self, workspace_id: str, version_id: str) -> c.PreparedDocument:
        """The cached prepared version itself; callers only read it.

        A preparation with a retryable error (OCR missing) is not cached, so a later call prepares again.
        """
        key = (workspace_id, version_id)
        with self._guard:
            prepared = self._cache.get(key)
            if prepared is not None:
                self._cache.move_to_end(key)
                return prepared
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                prepared = self._cache.get(key)
            if prepared is None:
                prepared = preparation.prepare_version(
                    self.store, workspace_id, version_id, render_dpi=self.render_dpi, ocr_dpi=self.ocr_dpi
                )
                if not any(e.retryable for e in prepared.errors):
                    with self._guard:
                        self._cache[key] = prepared
                        while len(self._cache) > MAX_CACHED:
                            old, _ = self._cache.popitem(last=False)
                            self._locks.pop(old, None)
                            self._sheets.pop(old, None)
        return prepared

    @_tool
    def prepare_document(self, workspace_id: str, version_id: str) -> c.ToolResult:
        prepared = self.prepared(workspace_id, version_id)
        unreadable = [e for e in prepared.errors if e.code == "unreadable"]
        if unreadable and not prepared.blocks and not prepared.pages:
            message = "; ".join(e.message for e in prepared.errors)
            details = {"errors": [e.model_dump(mode="json") for e in prepared.errors]}
            return c.error_result(
                c.ToolError(code="unreadable", message=message, details=details), warnings=prepared.warnings
            )
        return c.ok_result(prepared)

    # ======================= inspection (T05) =======================

    @_tool
    def read_evidence(self, workspace_id: str, block_ids: Sequence[str]) -> c.ToolResult:
        blocks: list[c.EvidenceBlock] = []
        missing: list[str] = []
        for block_id in block_ids:
            try:
                blocks.append(self.store.get_block(workspace_id, block_id))
            except c.ToolFailure as exc:
                if exc.error.code not in ("not_found", "invalid_reference"):
                    raise
                missing.append(block_id)
        if missing:
            raise c.ToolFailure(
                "not_found",
                f"evidence not found in this workspace: {', '.join(missing[:10])}",
                details={"missing": missing},
            )
        return c.ok_result(blocks, warnings=[] if blocks else ["no block IDs were given"])

    @_tool
    def inspect_page(self, workspace_id: str, version_id: str, page: int) -> c.ToolResult:
        prepared = self._prepared(workspace_id, version_id)
        for image in prepared.pages:
            if image.page == page:
                return c.ok_result(image.model_copy(deep=True))
        if not prepared.pages:
            if any(e.code == "unreadable" for e in prepared.errors):
                raise c.ToolFailure("unreadable", "the document could not be read, so it has no page images")
            raise c.ToolFailure(
                "unsupported",
                f"a {prepared.version.format} document has no page images; use read_evidence or read_cells",
            )
        listed = ", ".join(str(p.page) for p in prepared.pages)
        raise c.ToolFailure("not_found", f"page {page} not found; the document has page(s) {listed}")

    @_tool
    def inspect_crop(self, workspace_id: str, render_id: str, box: c.Box) -> c.ToolResult:
        from PIL import Image

        render = self._render(workspace_id, render_id)
        box = _argument(_BOX, box, "box")
        self._inside(render, box)
        page_box = (
            box.x0 + render.origin[0],
            box.y0 + render.origin[1],
            box.x1 + render.origin[0],
            box.y1 + render.origin[1],
        )
        _, data = self.store.read_artifact(workspace_id, render.base_id)
        with Image.open(io.BytesIO(data)) as img:
            crop = img.crop(page_box)
            crop.load()
        transform = render.transform.model_copy(
            update={"crop": c.Box(x0=page_box[0], y0=page_box[1], x1=page_box[2], y1=page_box[3])}
        )
        info = {"page": render.page, "transform": transform.model_dump(mode="json"), "source_render_id": render.base_id}
        ref = self.store.save_artifact(
            workspace_id, render.ref.version_id, kind="crop", media_type="image/png", data=ev.render_png(crop, info)
        )
        return c.ok_result(ref)

    @_tool
    def reocr_region(self, workspace_id: str, render_id: str, box: c.Box) -> c.ToolResult:
        from PIL import Image

        render = self._render(workspace_id, render_id)
        box = _argument(_BOX, box, "box")
        self._inside(render, box)
        if not ocr.available():
            raise c.ToolFailure("unsupported", "Tesseract is not installed, so the region cannot be read again")
        ox, oy = render.origin
        bw, bh = render.base_size
        x0, y0 = max(0, box.x0 + ox - REOCR_PAD), max(0, box.y0 + oy - REOCR_PAD)
        x1, y1 = min(bw, box.x1 + ox + REOCR_PAD), min(bh, box.y1 + oy + REOCR_PAD)
        _, data = self.store.read_artifact(workspace_id, render.base_id)
        with Image.open(io.BytesIO(data)) as img:
            region = img.convert("L").crop((x0, y0, x1, y1))
        scale = 3 if region.height < 60 else 2 if region.height < 400 else 1
        if scale > 1:
            region = region.resize((region.width * scale, region.height * scale), Image.LANCZOS)
        lines = ocr.ocr_lines(region, psm=6)
        if not lines and region.height <= 120 * scale:
            lines = ocr.ocr_lines(region, psm=7)
        version_id = render.ref.version_id
        earlier = self._blocks_on(workspace_id, version_id, render.base_id)
        blocks: list[c.EvidenceBlock] = []
        for text, (lx0, ly0, lx1, ly1), conf in lines:
            text = text.strip()
            if not text:
                continue
            try:
                pixel = ev.clip_box(x0 + lx0 / scale, y0 + ly0 / scale, x0 + lx1 / scale, y0 + ly1 / scale, bw, bh)
            except ValueError:  # entirely outside the render
                continue
            locator = c.PageRegionLocator(
                page=render.page,
                render_id=render.base_id,
                box=pixel,
                image_width=bw,
                image_height=bh,
                transform=render.transform,
            )
            block_id = ev.make_block_id(version_id, locator, text, "reocr")
            source = _most_overlapping(pixel, earlier, exclude=block_id)
            blocks.append(
                c.EvidenceBlock(
                    version_id=version_id,
                    block_id=block_id,
                    text=text,
                    method="reocr",
                    locator=locator,
                    quality=c.OcrQuality(engine="tesseract", confidence=min(100.0, max(0.0, float(conf)))),
                    derived_from=source,
                )
            )
        # A line read again with the same text and box has the same block ID, whichever region it was read
        # through. Its first stored reading stays: another confidence must not fail the call.
        blocks = [self._stored_or(workspace_id, b) for b in {b.block_id: b for b in blocks}.values()]
        self.store.save_blocks(workspace_id, blocks)
        return c.ok_result(blocks, warnings=[] if blocks else ["no text was recognized in the region"])

    @_tool
    def read_cells(self, workspace_id: str, version_id: str, sheet: str, ref: str) -> c.ToolResult:
        from openpyxl.utils import range_boundaries

        cell_ref = _argument(_CELL_REF, ref, "cell reference")
        prepared = self._prepared(workspace_id, version_id)
        if prepared.version.format != "xlsx" and not any(b.locator.kind == "cell" for b in prepared.blocks):
            raise c.ToolFailure(
                "unsupported", f"a {prepared.version.format} document has no cells; use read_evidence or inspect_page"
            )
        if any(e.code == "unreadable" for e in prepared.errors) and not prepared.blocks:
            raise c.ToolFailure("unreadable", "the spreadsheet could not be read")
        title = self._sheet_title(workspace_id, version_id, sheet)
        min_col, min_row, max_col, max_row = range_boundaries(cell_ref.replace("$", ""))
        count = (max_col - min_col + 1) * (max_row - min_row + 1)
        if count > MAX_CELLS:
            raise c.ToolFailure(
                "invalid_reference", f"range {ref} has {count} cells; read at most {MAX_CELLS} at a time"
            )
        found = []
        for block in prepared.blocks:
            loc = block.locator
            if loc.kind != "cell" or loc.sheet != title:
                continue
            col, row = _cell_position(loc.ref)
            if min_col <= col <= max_col and min_row <= row <= max_row:
                found.append(((row, col), block))
        found.sort(key=lambda item: item[0])
        blocks = [b.model_copy(deep=True) for _, b in found]
        warnings = [] if blocks else [f"cells {title}!{cell_ref} are empty"]
        return c.ok_result(blocks, warnings=warnings)

    # ======================= validation (T06) =======================

    @_tool
    def validate_extraction(self, workspace_id: str, proposal: Any, prepared: c.PreparedDocument) -> c.ToolResult:
        if self.validator is None:
            raise c.ToolFailure("unsupported", "validate_extraction is not connected: no T06 validator was configured")
        if prepared.version.workspace_id != workspace_id:
            raise c.ToolFailure("not_found", f"version {prepared.version.version_id} not found")
        outcome = self.validator(proposal, prepared)
        return outcome if isinstance(outcome, c.ToolResult) else c.ok_result(outcome)

    # ======================= helpers =======================

    def _render(self, workspace_id: str, render_id: str) -> _Render:
        ref, data = self.store.read_artifact(workspace_id, render_id)
        if ref.kind not in ("page_render", "crop"):
            raise c.ToolFailure("invalid_reference", f"artifact {render_id} is not a page render or crop")
        info, size = ev.read_render_info(data)
        if info is None:
            raise c.ToolFailure("unsupported", f"render {render_id} carries no page description")
        transform = c.RenderTransform.model_validate(info.get("transform") or {})
        page = int(info.get("page", 1))
        if ref.kind == "page_render":
            return _Render(ref, page, render_id, size, transform, (0, 0), size)
        base_id = info.get("source_render_id")
        base_ref, base_data = self.store.read_artifact(workspace_id, str(base_id))
        if base_ref.kind != "page_render" or base_ref.version_id != ref.version_id or transform.crop is None:
            raise c.ToolFailure("invalid_reference", f"crop {render_id} does not point at a page render of its version")
        base_info, base_size = ev.read_render_info(base_data)
        base_transform = c.RenderTransform.model_validate((base_info or {}).get("transform") or {})
        origin = (transform.crop.x0, transform.crop.y0)
        return _Render(ref, page, base_ref.artifact_id, base_size, base_transform, origin, size)

    @staticmethod
    def _inside(render: _Render, box: c.Box) -> None:
        width, height = render.size
        if box.x1 > width or box.y1 > height:
            raise c.ToolFailure(
                "invalid_reference", f"box lies outside render {render.ref.artifact_id} ({width}x{height} px)"
            )

    def _stored_or(self, workspace_id: str, block: c.EvidenceBlock) -> c.EvidenceBlock:
        """The block already stored under this ID, or ``block`` itself when there is none."""
        try:
            return self.store.get_block(workspace_id, block.block_id)
        except c.ToolFailure as exc:
            if exc.error.code != "not_found":
                raise
            return block

    def _blocks_on(self, workspace_id: str, version_id: str, render_id: str) -> list[c.EvidenceBlock]:
        prepared = self._cache.get((workspace_id, version_id))
        if prepared is None:
            return []
        return [b for b in prepared.blocks if b.locator.kind == "page_region" and b.locator.render_id == render_id]

    def _sheet_title(self, workspace_id: str, version_id: str, sheet: str) -> str:
        import openpyxl

        key = (workspace_id, version_id)
        names = self._sheets.get(key)
        if names is None:  # read once per version: blocks do not list sheets without non-empty cells
            data = self.store.read_original(workspace_id, version_id)
            names = openpyxl.load_workbook(io.BytesIO(data), read_only=True).sheetnames
            with self._guard:
                self._sheets[key] = names
        if sheet in names:
            return sheet
        for name in names:
            if name.lower() == sheet.lower():
                return name
        raise c.ToolFailure("not_found", f"sheet {sheet!r} not found; sheets: {', '.join(names)}")


def _cell_position(ref: str) -> tuple[int, int]:
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    letters, row = coordinate_from_string(ref.split(":", 1)[0].replace("$", ""))
    return column_index_from_string(letters), row


def _most_overlapping(box: c.Box, blocks: Sequence[c.EvidenceBlock], *, exclude: str) -> str | None:
    best, best_area = None, 0
    for block in blocks:
        if block.block_id == exclude:
            continue
        other = block.locator.box
        w = min(box.x1, other.x1) - max(box.x0, other.x0)
        h = min(box.y1, other.y1) - max(box.y0, other.y0)
        if w > 0 and h > 0 and w * h > best_area:
            best, best_area = block.block_id, w * h
    return best
