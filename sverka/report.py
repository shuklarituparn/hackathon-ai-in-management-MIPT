"""Разбор итогового отчета: карточка учета фактических затрат (КФЗ).

Скан или PDF: находим линии таблицы, режем на ячейки, каждую числовую ячейку читаем несколькими
способами и голосуем. Затем проверяем арифметику отчета (итог = сумма частей) и по ней
исправляем или помечаем сомнительные ячейки.
Отчет в XLSX/DOCX: читаем таблицу напрямую.
"""
from __future__ import annotations

import itertools
import re
from collections import Counter
from decimal import Decimal

import numpy as np
from PIL import Image

from . import config, ocr
from .models import DocContent, Report, ReportCell, ReportColumn, ReportRow
from .money import MONTH_YEAR_RE, month_num, month_range, parse_amount, q
from .requisites import find_contracts

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

# Стандартная форма КФЗ на 17 граф.
KFZ17 = [
    ReportColumn(3, "labor_intensity", "Трудоемкость (человеко-месяц)", "info", None, None, "чел.-мес."),
    ReportColumn(4, "materials", "Материальные затраты", "article", "materials", None),
    ReportColumn(5, "labor", "Затраты на оплату труда - всего (0200)", "article", "labor", "0200"),
    ReportColumn(6, "labor_211", "в т.ч. заработная плата, счет 211 - всего", "sub"),
    ReportColumn(7, "labor_main", "в т.ч. основная заработная плата", "sub"),
    ReportColumn(8, "labor_add", "в т.ч. дополнительная заработная плата", "sub"),
    ReportColumn(9, "labor_reserve", "в т.ч. резерв отпусков", "sub"),
    ReportColumn(10, "insurance", "Страховые взносы - всего (0300)", "article", "insurance", "0300"),
    ReportColumn(11, "ins_213", "в т.ч. взносы на заработную плату, счет 213 - всего", "sub"),
    ReportColumn(12, "ins_main", "в т.ч. взносы на основную заработную плату", "sub"),
    ReportColumn(13, "ins_add", "в т.ч. взносы на дополнительную заработную плату", "sub"),
    ReportColumn(14, "ins_reserve", "в т.ч. взносы на резерв отпусков", "sub"),
    ReportColumn(15, "prod_overhead", "Общепроизводственные затраты (0800)", "article", "prod_overhead", "0800"),
    ReportColumn(16, "gen_overhead", "Общехозяйственные затраты (0900)", "article", "gen_overhead", "0900"),
    ReportColumn(17, "total", "Итого", "total"),
]
# Арифметика формы: графа = сумма граф.
IDENT17 = [(5, [6, 9]), (6, [7, 8]), (10, [11, 14]), (11, [12, 13]), (17, [4, 5, 10, 15, 16])]

CODE_KIND = {"0100": "materials", "0200": "labor", "0300": "insurance",
             "0800": "prod_overhead", "0900": "gen_overhead"}
HEADER_KIND = [("labor", r"оплат\w*\s+труда"), ("insurance", r"страхов\w+\s+взнос"),
               ("prod_overhead", r"общепроизводствен"), ("gen_overhead", r"общехозяйствен"),
               ("materials", r"материальн"), ("travel", r"командиров"),
               ("contractors", r"сторонн|соисполнител"), ("other_direct", r"прочи\w+\s+прям")]
KIND_TITLE = {"labor": "Затраты на оплату труда", "insurance": "Страховые взносы",
              "prod_overhead": "Общепроизводственные затраты", "gen_overhead": "Общехозяйственные затраты",
              "materials": "Материальные затраты", "travel": "Командировочные расходы",
              "contractors": "Работы сторонних организаций", "other_direct": "Прочие прямые затраты"}
# Пары цифр, которые OCR путает чаще всего.
CONFUSABLE = {"3": "58", "5": "36", "8": "360", "6": "58", "0": "8", "1": "7", "7": "1", "9": "4", "4": "9"}


# ======================= вход =======================
def parse_report(dc: DocContent) -> Report:
    rep = Report(file=dc.name)
    try:
        if dc.fmt in ("xlsx", "docx") and any(f.cells for f in dc.fragments):
            _from_table_doc(dc, rep)
        else:
            _from_image_doc(dc, rep)
    except ocr.OcrUnavailable as e:
        rep.errors.append(str(e))
    except Exception as e:
        rep.errors.append(f"Не удалось разобрать отчет: {type(e).__name__}: {e}")
    if rep.rows:
        _classify_rows(rep)
        _verify_arithmetic(rep)
    elif not rep.errors:
        rep.errors.append("В отчете не найдена таблица с показателями")
    _header_meta(rep)
    return rep


# ======================= изображение / PDF =======================
def _page_image(dc: DocContent):
    """Изображение первой страницы отчета и слова текстового слоя (если есть)."""
    import pdfplumber

    if dc.fmt == "image":
        with Image.open(dc.path) as im:
            return im.convert("L"), None, 1.0
    with pdfplumber.open(dc.path) as pdf:
        best = None
        for page in pdf.pages:
            w = page.extract_words(x_tolerance=1.5, y_tolerance=2)
            w = w if len(w) >= 20 else None
            dpi = config.OCR_DPI
            if w is None and len(page.images) == 1:
                # Страница-скан: растеризуем в родном разрешении картинки, без лишней интерполяции.
                im0 = page.images[0]
                if im0.get("srcsize") and (im0["x1"] - im0["x0"]) >= 0.9 * page.width:
                    dpi = int(min(400, max(150, round(im0["srcsize"][0] / (page.width / 72.0)))))
            img = page.to_image(resolution=dpi).original.convert("L")
            if best is None:
                best = (img, w, dpi / 72.0)
            if _detect_grid(np.array(img)) is not None:
                return img, w, dpi / 72.0
        return best if best else (None, None, 1.0)


def _peaks(proj, thr, gap=6):
    idx = np.where(proj > thr)[0]
    out = []
    if len(idx) == 0:
        return out
    s = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > gap:
            out.append((s + prev) // 2)
            s = i
        prev = i
    out.append((s + prev) // 2)
    return out


def _detect_grid(gray: np.ndarray):
    """Сетка таблицы: (границы граф xs, строки [(y0, y1)], маска вертикалей, все вертикали)."""
    H, W = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, W // 25), 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, H // 70))))
    ys = _peaks(hl.sum(axis=1) / 255, W * 0.3)
    if len(ys) < 3:
        return None
    bottom = ys[-1]
    band_top = ys[-3] if len(ys) >= 4 else ys[-2]
    cols_with_h = np.where(hl[band_top:bottom + 1].sum(axis=0) > 0)[0]
    if len(cols_with_h) == 0:
        return None
    x_left, x_right = int(cols_with_h[0]), int(cols_with_h[-1])
    band = vl[band_top:bottom]
    xs = [x for x in _peaks(band.sum(axis=0) / 255, band.shape[0] * 0.8, gap=3)
          if x_left + 8 < x < x_right - 8]
    # Внешние границы таблицы берем по концам горизонтальных линий: край скана часто сливается с рамкой.
    xs = [x_left] + xs + [x_right]
    if len(xs) < 4:
        return None
    all_x = [x for x in _peaks(vl[ys[0]:bottom].sum(axis=0) / 255, 10, gap=3) if x_left + 8 < x < x_right - 8]
    all_x = [x_left] + all_x + [x_right]

    def has_line(x, y):
        if x in (x_left, x_right):
            return True
        return bool(vl[max(0, y - 2): y + 3, max(0, x - 4): x + 5].any())

    # Идем снизу вверх, пока во всех строках есть полный набор вертикалей.
    rows = []
    for i in range(len(ys) - 1, 0, -1):
        y0, y1 = ys[i - 1], ys[i]
        if y1 - y0 < 8:
            continue
        probes = [y0 + (y1 - y0) * k // 4 for k in (1, 2, 3)]
        if all(any(has_line(x, y) for y in probes) for x in xs):
            rows.insert(0, (y0, y1))
        else:
            break
    if not rows:
        return None
    return {"xs": xs, "rows": rows, "ys": ys, "vl": vl, "all_x": all_x, "has_line": has_line}


def _deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Исправляет небольшой наклон скана по длинным горизонтальным линиям таблицы."""
    H, W = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    lines = cv2.HoughLinesP(bw, 1, np.pi / 1800, threshold=max(100, W // 8), minLineLength=W // 4, maxLineGap=6)
    if lines is None:
        return gray, 0.0
    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) < 12:
            angles.append(a)
    if len(angles) < 3:
        return gray, 0.0
    ang = float(np.median(angles))
    if abs(ang) < 0.12:
        return gray, 0.0
    M = cv2.getRotationMatrix2D((W / 2, H / 2), ang, 1.0)
    return cv2.warpAffine(gray, M, (W, H), flags=cv2.INTER_CUBIC, borderValue=255), ang


def _cell_text_from_words(words, scale, x0, y0, x1, y1) -> str:
    got = [w for w in words if x0 <= (w["x0"] + w["x1"]) / 2 * scale <= x1 and y0 <= (w["top"] + w["bottom"]) / 2 * scale <= y1]
    got.sort(key=lambda w: (round(w["top"]), w["x0"]))
    return " ".join(w["text"] for w in got)


def _from_image_doc(dc: DocContent, rep: Report) -> None:
    if cv2 is None:
        rep.errors.append("OpenCV не установлен: разбор скана отчета недоступен")
        return
    img, words, scale = _page_image(dc)
    if img is None:
        rep.errors.append("Отчет не содержит страниц")
        return
    img = img.convert("L")
    gray = np.array(img)
    grid = None
    if words is not None:
        grid = _detect_grid(gray)
    else:
        # Скан: убираем наклон; если таблица не найдена, пробуем повороты на 90, 270 и 180 градусов.
        for rot in (0, 90):
            g = gray if rot == 0 else np.ascontiguousarray(np.rot90(gray, k=1))
            g, ang = _deskew(g)
            cand = _detect_grid(g)
            if cand is None or len(cand["xs"]) < 6:
                continue
            # Перевернутая таблица: высокие строки шапки оказываются внизу, а не вверху.
            gaps = np.diff(cand["ys"])
            if len(gaps) >= 4 and np.median(gaps[-2:]) > 1.5 * np.median(gaps[:2]):
                g2 = np.ascontiguousarray(np.rot90(g, k=2))
                cand2 = _detect_grid(g2)
                if cand2 is not None:
                    g, cand, rot = g2, cand2, rot + 180
            grid, gray = cand, g
            if rot or abs(ang) >= 0.12:
                rep.warnings.append(f"Скан отчета выровнен: поворот {rot} град., наклон {ang:.1f} град.")
            break
        img = Image.fromarray(gray)
    rep.image = img
    rep.source = "text" if words is not None else "ocr"
    if grid is None:
        rep.errors.append("В отчете не найдена сетка таблицы")
        rep.header_text = dc.text
        return
    xs, rows = grid["xs"], grid["rows"]
    rep.meta["_table_bbox"] = [xs[0], grid["ys"][0], xs[-1], grid["ys"][-1]]
    ncell = len(xs) - 1
    # Базовое увеличение ячейки: приводим высоту строки примерно к 60 px (цифры около 35 px),
    # на таком размере Tesseract читает жирные цифры без ошибок.
    row_h = float(np.min([y1 - y0 for y0, y1 in rows[-3:]]))
    base_scale = float(min(8.0, max(1.5, 60.0 / max(row_h, 1.0))))
    mg = max(2, int(round(row_h * 0.18)))

    def crop(r, j0, j1):
        y0, y1 = rows[r]
        return gray[y0 + mg: y1 - mg + 1, xs[j0] + mg: xs[j1] - mg], [xs[j0], y0, xs[j1], y1]

    def cell_str(r, j, numeric=True):
        c, box = crop(r, j, j + 1)
        if words is not None:
            return [_cell_text_from_words(words, scale, box[0], box[1], box[2], box[3])], box
        if numeric:
            return ocr.ocr_number_cell(c, base_scale), box
        return [ocr.ocr_text_cell(c, base_scale)], box

    # --- строка нумерации граф (1, 2, 3, ...) ---
    num_row, offset = None, None
    for r in range(len(rows)):
        vals = []
        for j in range(ncell):
            if words is not None:
                t = cell_str(r, j)[0][0]
            else:
                c, _ = crop(r, j, j + 1)
                t = ocr.ocr_digits_fast(c, base_scale)
            m = re.fullmatch(r"\D{0,2}(\d{1,2})\D{0,2}", t.strip())
            vals.append(int(m.group(1)) if m else None)
        offs = Counter(v - j for j, v in enumerate(vals) if v is not None)
        if offs:
            off, cnt = offs.most_common(1)[0]
            if cnt >= max(3, 0.6 * ncell):
                num_row, offset = r, off
                break
    if num_row is None:
        # Нумерации нет: считаем, что числовые графы прижаты к правому краю формы на 17 граф.
        offset = 17 - (ncell - 1)
        data_rows = [r for r in range(len(rows)) if _numeric_share(cell_str, r, ncell) >= 0.5]
        rep.warnings.append("Строка нумерации граф не найдена, номера граф назначены по положению")
    else:
        data_rows = list(range(num_row + 1, len(rows)))
    if not data_rows:
        rep.errors.append("В таблице отчета не найдены строки с данными")
        return
    first_num_cell = next((j for j in range(ncell) if j + offset >= 3), 1)
    max_col = (ncell - 1) + offset

    # --- схема граф ---
    hdr_top = grid["ys"][0]
    hdr_bottom = rows[num_row][0] if num_row is not None else rows[data_rows[0]][0]
    header_words = _header_words(gray, words, scale, hdr_top, hdr_bottom, 45.0 / max(row_h, 1.0))
    rep.columns, rep.schema = _build_schema(header_words, grid, xs, offset, first_num_cell, ncell, max_col)

    # --- данные ---
    for r in data_rows:
        lab_c, lab_box = crop(r, 0, first_num_cell)
        if words is not None:
            label = _cell_text_from_words(words, scale, *lab_box)
        else:
            label = _best_label([ocr.ocr_text_cell(lab_c, base_scale * k) for k in (1.5, 1.0)])
        label = re.sub(r"\s*\|\s*", " ", label).strip(" |:;.,")
        row = ReportRow(label=label, bbox=[xs[0], rows[r][0], xs[-1], rows[r][1]])
        for j in range(first_num_cell, ncell):
            variants, box = cell_str(r, j)
            col = j + offset
            cands = _candidates(variants)
            cell = ReportCell(col=col, value=cands[0][0] if cands else None,
                              raw=variants[0] if variants else "", variants=variants, bbox=box)
            cell._cands = cands  # type: ignore[attr-defined]
            if not cands:
                cell.reliable, cell.note = False, "ячейка не распознана"
            elif words is None and cands[0][1] < max(2, 0.6 * len(variants)):
                cell.reliable, cell.note = False, "прочтения ячейки расходятся"
            row.cells[col] = cell
        rep.rows.append(row)
    # Текст над таблицей (шапка карточки).
    top = gray[0: max(20, grid["ys"][0] - 5), :]
    if words is not None:
        rep.header_text = dc.text
    else:
        try:
            kt = float(min(3.0, max(1.0, 22.0 / max(row_h, 1.0))))
            top = cv2.resize(top, None, fx=kt, fy=kt, interpolation=cv2.INTER_CUBIC)
            rep.header_text = "\n".join(t for t, _, c in ocr.ocr_lines(Image.fromarray(top), psm=4) if c > 30)
        except Exception:
            rep.header_text = ""


def _best_label(variants: list[str]) -> str:
    """Из нескольких прочтений подписи строки берем то, где меньше посторонних символов."""
    def junk(t):
        letters = re.findall(r"[A-Za-zА-Яа-яЁё]", t)
        lat = len(re.findall(r"[A-Za-z]", t)) / max(1, len(letters))
        return len(re.findall(r"[^0-9A-Za-zА-Яа-яЁё\s.,:()\-/]", t)) + lat - 0.01 * len(t)
    variants = [v for v in variants if v.strip()]
    return min(variants, key=junk) if variants else ""


def _numeric_share(cell_str, r, ncell) -> float:
    ok = 0
    for j in range(1, ncell):
        v, _ = cell_str(r, j)
        if _candidates(v):
            ok += 1
    return ok / max(1, ncell - 1)


def _candidates(variants) -> list[tuple[Decimal, int]]:
    """Варианты прочтения -> значения с числом голосов. Запятая могла потеряться: тогда 2 знака после нее."""
    votes: Counter = Counter()
    for v in variants:
        t = re.sub(r"[\s ]", "", v or "").replace("−", "-")
        neg = t.startswith("-")
        t = t.lstrip("-").strip(".,")
        if not t or not re.fullmatch(r"[\d.,]+", t):
            continue
        m = re.fullmatch(r"(\d[\d.,]*?)[.,](\d{2})", t)
        if m:
            val = Decimal(re.sub(r"[.,]", "", m.group(1)) + "." + m.group(2))
        elif re.fullmatch(r"\d{3,}", t):
            val = Decimal(t[:-2] + "." + t[-2:])
        elif re.fullmatch(r"\d{1,2}", t):
            val = Decimal(t)
        else:
            continue
        votes[q(-val if neg else val)] += 1
    return votes.most_common()


def _header_words(gray, words, scale, y0, y1, k=2.0):
    """Слова шапки таблицы с координатами в пикселях изображения."""
    out = []
    if y1 - y0 < 10:
        return out
    if words is not None:
        for w in words:
            cy = (w["top"] + w["bottom"]) / 2 * scale
            if y0 <= cy <= y1:
                out.append((w["text"], (w["x0"] + w["x1"]) / 2 * scale, cy))
        return out
    k = float(min(4.0, max(1.0, k)))
    band = cv2.resize(gray[y0:y1, :], None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    try:
        for t, b, c in ocr.ocr_lines(Image.fromarray(band), psm=11):
            if c < 25:
                continue
            out.append((t, (b[0] + b[2]) / 2 / k, y0 + (b[1] + b[3]) / 2 / k))
    except ocr.OcrUnavailable:
        raise
    except Exception:
        pass
    return out


def _build_schema(header_words, grid, xs, offset, first_num_cell, ncell, max_col):
    """Определяет назначение граф по шапке: коды статей калькуляции и ключевые слова."""
    all_x, has_line = grid["all_x"], grid["has_line"]

    def cell_of(x):
        for j in range(ncell):
            if xs[j] <= x < xs[j + 1]:
                return j
        return None

    def group(x, y):
        """Диапазон граф, который накрывает ячейка шапки с точкой (x, y)."""
        left = max([v for v in all_x if v <= x and has_line(v, int(y))], default=xs[0])
        right = min([v for v in all_x if v > x and has_line(v, int(y))], default=xs[-1])
        js = [j for j in range(ncell) if xs[j] >= left - 6 and xs[j + 1] <= right + 6]
        return js

    detected: dict[int, ReportColumn] = {}
    for text, x, y in header_words:
        low = text.lower().replace("ё", "е")
        for m in re.finditer(r"(?<!\d)([01]\d00)(?!\d)", text):
            js = group(x, y)
            if not js:
                continue
            col = js[0] + offset
            code = m.group(1)
            detected[col] = ReportColumn(col, f"art_{code}", "", "article", CODE_KIND.get(code), code)
        for kind, rx in HEADER_KIND:
            if re.search(rx, low):
                js = group(x, y)
                if js:
                    col = js[0] + offset
                    c = detected.setdefault(col, ReportColumn(col, kind, "", "article", kind, None))
                    if c.kind is None or c.kind == kind:
                        c.kind = kind
        if re.search(r"трудо[её]мкост", low):
            j = cell_of(x)
            if j is not None:
                detected.setdefault(j + offset, ReportColumn(j + offset, "labor_intensity",
                                                             "Трудоемкость (человеко-месяц)", "info", None, None,
                                                             "чел.-мес."))
    art = {c: v for c, v in detected.items() if v.role == "article"}
    if max_col == 17:
        ref = {c.num: c for c in KFZ17}
        conflict = [c for c, v in art.items() if v.code and ref.get(c) and ref[c].code and ref[c].code != v.code]
        missing = [v.code for c, v in art.items() if v.code and (c not in ref or ref[c].role != "article")]
        # Одно несовпадение кода считаем ошибкой распознавания шапки, а не другой формой отчета.
        if len(conflict) + len(missing) <= 1:
            return [ReportColumn(**vars(c)) for c in KFZ17], "КФЗ, 17 граф"
    # Нестандартная форма: строим схему по найденным кодам статей.
    cols = []
    for j in range(first_num_cell, ncell):
        col = j + offset
        d = detected.get(col)
        if d is not None:
            if d.role == "article":
                d.title = KIND_TITLE.get(d.kind, "Статья затрат") + (f" ({d.code})" if d.code else "")
                d.key = d.kind or d.key
            cols.append(d)
        elif col == max_col:
            cols.append(ReportColumn(col, "total", "Итого", "total"))
        else:
            cols.append(ReportColumn(col, f"col_{col}", f"Графа {col}", "sub"))
    return cols, f"нестандартная форма, граф: {max_col}"


# ======================= отчет в XLSX / DOCX =======================
def _from_table_doc(dc: DocContent, rep: Report) -> None:
    rep.source = "text"
    rows = [f for f in dc.fragments if f.cells]
    rep.header_text = "\n".join(f.text for f in dc.fragments[:25])

    def colkey(fr, i):
        return re.sub(r"\d", "", fr.refs[i]) if fr.refs else i

    num_i, num_map = None, {}
    for i, fr in enumerate(rows):
        ints = [(colkey(fr, k), int(float(str(c)))) for k, c in enumerate(fr.cells)
                if re.fullmatch(r"\s*\d{1,2}(?:\.0)?\s*", str(c))]
        seq = [v for _, v in ints]
        if len(seq) >= 5 and all(b - a == 1 for a, b in zip(seq, seq[1:])):
            num_i, num_map = i, dict(ints)
            break
    if num_i is None:
        rep.errors.append("В отчете не найдена строка нумерации граф")
        return
    max_col = max(num_map.values())
    hdr_text = " ".join(f.text for f in rows[:num_i]).lower()
    if max_col == 17:
        rep.columns, rep.schema = [ReportColumn(**vars(c)) for c in KFZ17], "КФЗ, 17 граф"
    else:
        rep.columns, rep.schema = _schema_from_table_header(rows[:num_i], num_map, max_col), f"нестандартная форма, граф: {max_col}"
    del hdr_text
    first_num = min(c.num for c in rep.columns)
    for fr in rows[num_i + 1:]:
        if fr.loc.get("table") != rows[num_i].loc.get("table") or fr.loc.get("sheet") != rows[num_i].loc.get("sheet"):
            break
        label_parts, row = [], ReportRow(label="")
        for k, c in enumerate(fr.cells):
            col = num_map.get(colkey(fr, k))
            if col is None or col < first_num:
                if not isinstance(c, (int, float)):
                    label_parts.append(str(c))
                continue
            val = parse_amount(c)
            row.cells[col] = ReportCell(col=col, value=val, raw=str(c), variants=[str(c)],
                                        reliable=val is not None)
            row.cells[col]._cands = [(val, 5)] if val is not None else []  # type: ignore[attr-defined]
        row.label = " ".join(label_parts).strip(" :;")
        if row.cells:
            row.loc = fr.where()  # type: ignore[attr-defined]
            rep.rows.append(row)


def _schema_from_table_header(hdr_rows, num_map, max_col):
    found = {}
    for fr in hdr_rows:
        for k, c in enumerate(fr.cells):
            key = re.sub(r"\d", "", fr.refs[k]) if fr.refs else k
            col = num_map.get(key)
            if col is None:
                continue
            low = str(c).lower()
            code = re.search(r"(?<!\d)([01]\d00)(?!\d)", low)
            kind = next((kd for kd, rx in HEADER_KIND if re.search(rx, low)), None)
            if code or kind:
                kind = kind or CODE_KIND.get(code.group(1))
                found.setdefault(col, ReportColumn(col, kind or f"art_{code.group(1)}",
                                                   KIND_TITLE.get(kind, "Статья затрат") +
                                                   (f" ({code.group(1)})" if code else ""),
                                                   "article", kind, code.group(1) if code else None))
            elif re.search(r"трудо[её]мкост", low):
                found.setdefault(col, ReportColumn(col, "labor_intensity", "Трудоемкость", "info", None, None, "чел.-мес."))
    cols = []
    for col in sorted(set(num_map.values())):
        if col in found:
            cols.append(found[col])
        elif col == max_col:
            cols.append(ReportColumn(col, "total", "Итого", "total"))
        elif col >= 3:
            cols.append(ReportColumn(col, f"col_{col}", f"Графа {col}", "sub"))
    return cols


# ======================= строки и арифметика =======================
def _classify_rows(rep: Report) -> None:
    import difflib
    lat2cyr = str.maketrans("ABCEHKMOPTXYaceopxyI", "АВСЕНКМОРТХУасеорхуГ")
    for row in rep.rows:
        low = row.label.translate(lat2cyr).lower()
        first = re.split(r"[\s,.:]+", low.strip())[0] if low.strip() else ""
        if re.search(r"всего|итого", low) or max(difflib.SequenceMatcher(None, first, w).ratio()
                                                 for w in ("всего", "итого")) >= 0.75:
            row.kind = "total"
        elif "справочн" in low:
            row.kind = "reference"


def _identities(rep: Report) -> list[tuple[tuple, list[tuple], str]]:
    """Контрольные соотношения: (левая ячейка, [слагаемые], описание)."""
    idents = []
    cols = {c.num for c in rep.columns}
    if rep.schema.startswith("КФЗ, 17"):
        row_idents = IDENT17
    else:
        arts = [c.num for c in rep.columns if c.role == "article"]
        tot = [c.num for c in rep.columns if c.role == "total"]
        row_idents = [(tot[0], arts)] if tot and len(arts) >= 2 else []
    for ri, row in enumerate(rep.rows):
        for lhs, rhs in row_idents:
            if lhs in cols and all(c in cols for c in rhs):
                idents.append(((ri, lhs), [(ri, c) for c in rhs],
                               f"«{row.label}»: гр.{lhs} = " + " + ".join(f"гр.{c}" for c in rhs)))
    # Итоговая строка = сумма строк данных перед ней (без справочных).
    start = 0
    for ti, row in enumerate(rep.rows):
        if row.kind != "total":
            continue
        parts = [i for i in range(start, ti) if rep.rows[i].kind == "data"]
        start = ti + 1
        if not parts:
            continue
        ok = total = 0
        for c in cols:
            vals = [rep.rows[i].val(c) for i in parts] + [row.val(c)]
            if any(v is None for v in vals):
                continue
            total += 1
            ok += sum(vals[:-1]) == vals[-1]
        if total and ok >= 0.6 * total:
            for c in sorted(cols):
                idents.append(((ti, c), [(i, c) for i in parts],
                               f"гр.{c}: «{row.label}» = сумма строк " + ", ".join(f"«{rep.rows[i].label}»" for i in parts)))
    return idents


def _verify_arithmetic(rep: Report) -> None:
    idents = _identities(rep)
    cell = lambda k: rep.rows[k[0]].cells.get(k[1])

    def value(k, override=None):
        if override and k in override:
            return override[k]
        c = cell(k)
        return c.value if c else None

    def failing(override=None):
        bad = []
        for lhs, rhs, desc in idents:
            vals = [value(k, override) for k in [lhs] + rhs]
            if any(v is None for v in vals):
                continue
            if vals[0] != sum(vals[1:]):
                bad.append((lhs, rhs, desc))
        return bad

    bad = failing()
    for _ in range(6):
        if not bad:
            break
        involved = sorted({k for lhs, rhs, _ in bad for k in [lhs] + rhs if cell(k) is not None})
        fixed = False
        # 1) Альтернативные прочтения из голосования: перебираем варианты ячеек одного соотношения
        #    и принимаем набор, который уменьшает общее число несходящихся сумм.
        for lhs, rhs, _d in bad:
            local = [k for k in [lhs] + rhs if cell(k) is not None]
            opts = [[v for v, _ in getattr(cell(k), "_cands", [])[:4]] or [value(k)] for k in local]
            space = 1
            for o in opts:
                space *= len(o)
            if not 1 < space <= 5000:
                continue
            cur = len(failing())
            best = None
            for combo in itertools.product(*opts):
                ov = dict(zip(local, combo))
                nbad = len(failing(ov))
                changed = sum(1 for k in local if ov[k] != value(k))
                if nbad < cur and (best is None or (nbad, changed) < best[:2]):
                    best = (nbad, changed, ov)
            if best is not None:
                for k, v in best[2].items():
                    c = cell(k)
                    if v != c.value:
                        c.note = f"выбрано альтернативное прочтение по контрольной сумме (было {c.value})"
                        c.value, c.repaired, c.reliable = v, True, True
                fixed = True
        # 2) Ячейка не распознана или прочтения расходятся: восстанавливаем из соотношений.
        # 3) Отличие в одной цифре из пары, которую путает OCR.
        if not fixed:
            for k in involved:
                c = cell(k)
                rel = [(lhs, rhs) for lhs, rhs, _ in idents if k == lhs or k in rhs]
                solved = set()
                for lhs, rhs in rel:
                    others = [value(x) for x in [lhs] + rhs if x != k]
                    if any(v is None for v in others):
                        continue
                    if k == lhs:
                        solved.add(sum(others))
                    else:
                        solved.add(value(lhs) - sum(value(x) for x in rhs if x != k))
                if len(solved) != 1:
                    continue
                new = solved.pop()
                if new < 0 or len(failing({k: new})) >= len(bad):
                    continue
                weak = c.value is None or not c.reliable
                if weak or (_one_digit_confusion(c.value, new) and len(rel) >= 2):
                    c.note = (f"значение восстановлено по контрольным суммам отчета "
                              f"(распознано: {c.raw or 'нет'})")
                    c.value, c.repaired, c.reliable = q(new), True, True
                    fixed = True
                    break
        new_bad = failing()
        if not fixed or len(new_bad) >= len(bad):
            bad = new_bad
            break
        bad = new_bad
    bad_keys = {k for lhs, rhs, _ in bad for k in [lhs] + rhs}
    for lhs, rhs, desc in idents:
        vals = [value(k) for k in [lhs] + rhs]
        if any(v is None for v in vals):
            rep.checks.append({"check": desc, "status": "НЕПРОВЕРЕНО", "left": None, "right": None,
                               "row": lhs[0], "col": lhs[1], "note": "часть ячеек не распознана"})
            continue
        ok = vals[0] == sum(vals[1:])
        rep.checks.append({"check": desc, "status": "ОК" if ok else "НАРУШЕНО", "left": vals[0],
                           "right": sum(vals[1:]), "row": lhs[0], "col": lhs[1],
                           "note": "" if ok else "арифметика отчета не сходится: ошибка в отчете или ошибка распознавания"})
    # Ячейка, подтвержденная хотя бы одной сходящейся контрольной суммой, считается надежной,
    # даже если другая сумма с ее участием не сошлась из-за соседней ячейки.
    confirmed = set()
    for lhs, rhs, _ in idents:
        if (lhs, rhs, _) not in bad and all(value(k) is not None for k in [lhs] + rhs):
            confirmed.update([lhs] + rhs)
    for k in bad_keys - confirmed:
        c = cell(k)
        if c is not None and rep.source == "ocr":
            c.reliable = False
            c.note = c.note or "ячейка участвует в несходящейся контрольной сумме"
    for k in confirmed:
        c = cell(k)
        if c is not None and not c.reliable and c.value is not None:
            c.reliable, c.note = True, ""
    repaired = sum(1 for r in rep.rows for c in r.cells.values() if c.repaired)
    if repaired:
        rep.warnings.append(f"Ячеек отчета, исправленных по контрольным суммам: {repaired}")


def _one_digit_confusion(a: Decimal | None, b: Decimal) -> bool:
    if a is None:
        return False
    sa, sb = f"{a:.2f}".replace(".", ""), f"{b:.2f}".replace(".", "")
    if len(sa) != len(sb):
        return False
    diff = [(x, y) for x, y in zip(sa, sb) if x != y]
    return len(diff) == 1 and diff[0][1] in CONFUSABLE.get(diff[0][0], "")


# ======================= шапка карточки =======================
def _header_meta(rep: Report) -> None:
    text = rep.header_text or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines[:6]:
        if re.search(r"карточк|отч[её]т|кфз", l, re.I):
            rep.title = l
            break
    found = find_contracts(text)
    if found:
        rep.contract = found[0][0]
    months = []
    for l in lines[:8]:
        ms = list(MONTH_YEAR_RE.finditer(l))
        if len(ms) >= 2 and not re.search(r"[«\"']\d", l):
            months = ms[:2]
            rep.period_text = l
            break
    if months:
        a, b = months
        ma, mb = month_num(a.group(1)), month_num(b.group(1))
        if ma and mb:
            rep.period = (month_range(int(a.group(2)), ma)[0], month_range(int(b.group(2)), mb)[1])
    money = r"(\d{1,3}(?:[ \u00a0]\d{3})+[.,]\d{2})"
    for name, rx in (("Получено средств", r"получено\s+средств.{0,40}?" + money),
                     ("Сметная стоимость", r"с[мы]етная\s+стоимость.{0,60}?" + money)):
        m = re.search(rx, text, re.I)
        if m:
            v = parse_amount(m.group(1))
            if v is not None:
                rep.meta[name] = v
