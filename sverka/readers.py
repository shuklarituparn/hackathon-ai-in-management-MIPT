"""Чтение файлов разных форматов в единое представление DocContent.

Формат определяется по содержимому (сигнатуре), а не только по расширению.
Любая ошибка чтения не роняет процесс: она записывается в DocContent.errors.
"""
from __future__ import annotations

import ast
import io
import operator
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from PIL import Image

from . import config, ocr
from .models import DocContent, Fragment

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
LEGACY_EXT = {".doc", ".xls", ".rtf", ".odt", ".ods"}


def sniff(path: str) -> str:
    """Определяет формат по сигнатуре файла."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return "broken-" + (ext.lstrip(".") or "zip")
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        return "zip"
    if head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*") \
            or head[:2] == b"BM" or head[:4] == b"GIF8" or head[:4] == b"RIFF":
        return "image"
    if head.startswith(b"\xd0\xcf\x11\xe0") or ext in LEGACY_EXT:
        return "legacy"
    if ext in (".txt", ".csv", ".md", ".json", ".xml"):
        return "txt"
    if ext == ".pdf":
        return "broken-pdf"
    if ext in (".docx", ".xlsx"):
        return "broken-" + ext.lstrip(".")
    if ext in IMAGE_EXT:
        return "image"
    return "unknown"


def read_any(path: str) -> DocContent:
    dc = DocContent(path=path, name=os.path.basename(path))
    kind = sniff(path)
    try:
        if kind == "pdf":
            _read_pdf(dc)
        elif kind == "docx":
            _read_docx(dc)
        elif kind == "xlsx":
            _read_xlsx(dc)
        elif kind == "image":
            _read_image(dc)
        elif kind == "txt":
            _read_txt(dc)
        elif kind == "legacy":
            _read_legacy(dc)
        elif kind == "empty":
            dc.errors.append("Файл пустой (0 байт)")
        elif kind.startswith("broken"):
            dc.errors.append("Файл поврежден: не удалось открыть как " + kind.split("-", 1)[1].upper())
        elif kind == "unreadable":
            dc.errors.append("Файл недоступен для чтения")
        else:
            dc.errors.append("Формат файла не поддерживается")
    except ocr.OcrUnavailable as e:
        dc.errors.append(str(e))
    except Exception as e:  # поврежденный файл, неожиданный формат и т.п.
        dc.errors.append(f"Ошибка чтения файла: {type(e).__name__}: {e}")
    if not dc.errors and not dc.fragments:
        dc.errors.append("В файле не найден текст")
    return dc


# ---------------- PDF ----------------
def _read_pdf(dc: DocContent) -> None:
    import pdfplumber

    dc.fmt = "pdf"
    confs = []
    with pdfplumber.open(dc.path) as pdf:
        if not pdf.pages:
            dc.errors.append("PDF не содержит страниц")
            return
        for pno, page in enumerate(pdf.pages, 1):
            try:
                words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            except Exception:
                words = []
            if len(words) >= 3:
                for text, bbox in _group_words(words):
                    dc.fragments.append(Fragment(text, {"type": "pdf", "page": pno, "bbox": bbox}))
                continue
            # Текстового слоя нет: страница является сканом.
            img = page.to_image(resolution=config.OCR_DPI).original
            lines, img, conf = ocr.ocr_page(img)
            dc.ocr, dc.fmt = True, "pdf-scan"
            dc.page_images[pno] = img
            confs.append(conf)
            for text, bbox, c in lines:
                dc.fragments.append(Fragment(text, {"type": "scan", "page": pno, "bbox": bbox}, conf=c))
    if confs:
        dc.warnings.append(f"Документ распознан через OCR, средняя уверенность {sum(confs) / len(confs):.0f}%")


def _group_words(words):
    """Слова -> строки по вертикальной координате."""
    words = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines, cur, top = [], [], None
    for w in words:
        if top is None or abs(w["top"] - top) <= 3:
            cur.append(w)
            top = w["top"] if top is None else top
        else:
            lines.append(cur)
            cur, top = [w], w["top"]
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in ln)
        bbox = [min(w["x0"] for w in ln), min(w["top"] for w in ln),
                max(w["x1"] for w in ln), max(w["bottom"] for w in ln)]
        out.append((text, [round(v, 1) for v in bbox]))
    return out


# ---------------- изображения ----------------
def _read_image(dc: DocContent) -> None:
    dc.fmt = "image"
    with Image.open(dc.path) as im:
        frames = getattr(im, "n_frames", 1)
        confs = []
        for i in range(frames):
            if frames > 1:
                im.seek(i)
            lines, img, conf = ocr.ocr_page(im.copy())
            dc.page_images[i + 1] = img
            confs.append(conf)
            for text, bbox, c in lines:
                dc.fragments.append(Fragment(text, {"type": "scan", "page": i + 1, "bbox": bbox}, conf=c))
    dc.ocr = True
    if confs:
        dc.warnings.append(f"Документ распознан через OCR, средняя уверенность {sum(confs) / len(confs):.0f}%")


# ---------------- DOCX ----------------
def _read_docx(dc: DocContent) -> None:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    dc.fmt = "docx"
    d = docx.Document(dc.path)
    pi = ti = 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            pi += 1
            text = Paragraph(child, d).text.strip()
            if text:
                dc.fragments.append(Fragment(text, {"type": "docx", "paragraph": pi}))
        elif tag == "tbl":
            ti += 1
            for ri, row in enumerate(Table(child, d).rows, 1):
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:  # объединенные ячейки повторяются
                        continue
                    seen.add(id(c._tc))
                    cells.append(c.text.strip())
                if any(cells):
                    dc.fragments.append(Fragment(" | ".join(cells), {"type": "docx", "table": ti, "row": ri},
                                                 cells=cells))
    if len(dc.text) < 30:
        _ocr_docx_images(dc)


def _ocr_docx_images(dc: DocContent) -> None:
    """DOCX без текста, но с картинками: распознаем вложенные изображения."""
    with zipfile.ZipFile(dc.path) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        for i, n in enumerate(sorted(media), 1):
            try:
                img = Image.open(io.BytesIO(z.read(n)))
                lines, img, _, rotation = ocr.ocr_page_oriented(img)
            except ocr.OcrUnavailable:
                raise
            except Exception:
                continue
            dc.ocr = True
            dc.page_images[i] = img
            dc.page_rotations[i] = rotation
            for text, bbox, c in lines:
                dc.fragments.append(Fragment(text, {"type": "scan", "page": i, "bbox": bbox}, conf=c))


# ---------------- XLSX ----------------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Pow: operator.pow}
_REF = r"\$?([A-Z]{1,3})\$?(\d+)"


def _eval_formula(ws, formula: str, depth: int = 0):
    """Вычисляет простые формулы (SUM, +, -, *, /, ROUND), если Excel не сохранил значение."""
    from openpyxl.utils import range_boundaries

    if depth > 20:
        return None
    expr = formula.lstrip("=").replace(";", ",")

    def cell_val(ref: str):
        v = ws[ref.replace("$", "")].value
        if isinstance(v, str) and v.startswith("="):
            v = _eval_formula(ws, v, depth + 1)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    def rng(m):
        c1, r1, c2, r2 = range_boundaries(m.group(0).replace("$", ""))
        vals = []
        for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
            for c in row:
                v = c.value
                if isinstance(v, str) and v.startswith("="):
                    v = _eval_formula(ws, v, depth + 1)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
        return "[" + ",".join(repr(v) for v in vals) + "]"

    expr = re.sub(_REF + ":" + _REF, rng, expr)
    expr = re.sub(_REF, lambda m: repr(cell_val(m.group(0))), expr)
    expr = re.sub(r"(?i)\bСУММ\b", "SUM", expr)
    expr = re.sub(r"(?i)\bОКРУГЛ\b", "ROUND", expr)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.List):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id.upper()
            args = [ev(a) for a in node.args]
            flat = []
            for a in args:
                flat.extend(a if isinstance(a, list) else [a])
            if fn == "SUM":
                return sum(flat)
            if fn == "ROUND":
                return round(flat[0], int(flat[1]) if len(flat) > 1 else 0)
            if fn == "ABS":
                return abs(flat[0])
        raise ValueError("unsupported")

    try:
        return ev(ast.parse(expr, mode="eval"))
    except Exception:
        return None


def _read_xlsx(dc: DocContent) -> None:
    import openpyxl
    from openpyxl.utils import get_column_letter

    dc.fmt = "xlsx"
    wb_f = openpyxl.load_workbook(dc.path, data_only=False)
    try:
        wb_v = openpyxl.load_workbook(dc.path, data_only=True)
    except Exception:
        wb_v = None
    computed = 0
    for ws in wb_f.worksheets:
        wv = wb_v[ws.title] if wb_v is not None and ws.title in wb_v.sheetnames else None
        for row in ws.iter_rows():
            cells, refs = [], []
            for c in row:
                v = c.value
                if isinstance(v, str) and v.startswith("="):
                    cached = wv[c.coordinate].value if wv is not None else None
                    if cached is None:
                        # Excel не сохранил результат формулы (файл создан программно): считаем сами.
                        cached = _eval_formula(ws, v)
                        if cached is not None:
                            computed += 1
                    v = cached
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if isinstance(v, float):
                    v = round(v, 6)
                cells.append(v)
                refs.append(c.coordinate)
            if not cells:
                continue
            r = row[0].row
            rng = f"{refs[0]}:{refs[-1]}" if len(refs) > 1 else refs[0]
            text = " | ".join(_cell_str(v) for v in cells)
            dc.fragments.append(Fragment(text, {"type": "xlsx", "sheet": ws.title, "row": r, "range": rng},
                                         cells=cells, refs=refs))
    if computed:
        dc.warnings.append(f"Значения {computed} формул не сохранены в файле и вычислены системой")


def _cell_str(v) -> str:
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v - round(v, 2)) < 1e-9 else repr(v)
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m.%Y")
    return str(v).strip()


# ---------------- TXT и старые форматы ----------------
def _read_txt(dc: DocContent) -> None:
    dc.fmt = "txt"
    raw = open(dc.path, "rb").read()
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            dc.fragments.append(Fragment(line.strip(), {"type": "txt", "line": i}))


def _read_legacy(dc: DocContent) -> None:
    """DOC/XLS/RTF/ODT: конвертируем через LibreOffice, если он установлен."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        dc.errors.append("Старый формат (DOC/XLS/RTF) не поддерживается без LibreOffice: "
                         "пересохраните файл в DOCX/XLSX")
        return
    ext = os.path.splitext(dc.path)[1].lower()
    target = "xlsx" if ext in (".xls", ".ods") else "docx"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([soffice, "--headless", "--convert-to", target, "--outdir", tmp, dc.path],
                       capture_output=True, timeout=120, check=False)
        out = [f for f in os.listdir(tmp) if f.endswith("." + target)]
        if not out:
            dc.errors.append("Не удалось конвертировать файл старого формата")
            return
        tmp_dc = DocContent(path=os.path.join(tmp, out[0]), name=dc.name)
        (_read_xlsx if target == "xlsx" else _read_docx)(tmp_dc)
        dc.fragments, dc.fmt, dc.warnings = tmp_dc.fragments, tmp_dc.fmt, tmp_dc.warnings
        dc.warnings.append("Файл старого формата конвертирован через LibreOffice")
