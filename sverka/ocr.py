"""Обертка над Tesseract: распознавание страниц и отдельных ячеек."""
from __future__ import annotations

import re

import numpy as np
from PIL import Image

from . import config

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None
try:
    import pytesseract
    from pytesseract import Output
except Exception:  # pragma: no cover
    pytesseract = None


class OcrUnavailable(RuntimeError):
    pass


def _cfg(extra: str = "") -> str:
    c = extra
    if config.TESSDATA_DIR:
        c += f' --tessdata-dir "{config.TESSDATA_DIR}"'
    return c.strip()


def available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _langs() -> str:
    """Оставляем только установленные языки, чтобы не падать без rus."""
    try:
        have = set(pytesseract.get_languages(config=_cfg()))
    except Exception:
        return config.OCR_LANG
    want = [l for l in config.OCR_LANG.split("+") if l in have]
    return "+".join(want) or "eng"


def to_gray(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("L"))


def normalize_page(img: Image.Image, min_side: int = 2200) -> Image.Image:
    """Приводим скан к рабочему размеру: мелкие изображения увеличиваем."""
    img = img.convert("L")
    long_side = max(img.size)
    if long_side < min_side:
        k = min_side / long_side
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    return img


def fix_orientation(img: Image.Image) -> tuple[Image.Image, int]:
    """Поворот на 90/180/270 по данным OSD. При любой ошибке оставляем как есть."""
    try:
        osd = pytesseract.image_to_osd(img, config=_cfg("--psm 0"))
        rot = int(re.search(r"Rotate:\s*(\d+)", osd).group(1))
        conf = float(re.search(r"Orientation confidence:\s*([\d.]+)", osd).group(1))
        if rot in (90, 180, 270) and conf >= 2.0:
            return img.rotate(-rot, expand=True, fillcolor=255), rot
    except Exception:
        pass
    return img, 0


def ocr_lines(img: Image.Image, psm: int = 6, lang: str | None = None):
    """Распознает страницу. Возвращает строки: (текст, bbox, уверенность)."""
    if not available():
        raise OcrUnavailable("Tesseract не установлен, распознавание сканов недоступно")
    data = pytesseract.image_to_data(img, lang=lang or _langs(), config=_cfg(f"--psm {psm}"),
                                     output_type=Output.DICT)
    lines: dict = {}
    for i, txt in enumerate(data["text"]):
        txt = txt.strip()
        if not txt:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        e = lines.setdefault(key, {"words": [], "bbox": [x, y, x + w, y + h], "confs": []})
        e["words"].append((x, txt))
        b = e["bbox"]
        e["bbox"] = [min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h)]
        if conf >= 0:
            e["confs"].append(conf)
    out = []
    for key in sorted(lines, key=lambda k: (lines[k]["bbox"][1], lines[k]["bbox"][0])):
        e = lines[key]
        text = " ".join(t for _, t in sorted(e["words"]))
        conf = sum(e["confs"]) / len(e["confs"]) if e["confs"] else 0.0
        out.append((text, e["bbox"], conf))
    return out


def ocr_page(img: Image.Image):
    """OCR страницы с автоповоротом. Возвращает (строки, изображение, средняя уверенность)."""
    lines, img, conf, _ = ocr_page_oriented(img)
    return lines, img, conf


def ocr_page_oriented(img: Image.Image):
    """Как ocr_page, но также возвращает поворот по часовой стрелке (0/90/180/270), примененный к странице."""
    img = normalize_page(img)
    lines = ocr_lines(img, psm=6)
    conf = _mean_conf(lines)
    applied = 0
    # Ориентацию проверяем и тогда, когда средняя уверенность приемлема, но большинство строк — мусор:
    # несколько уверенно прочитанных строк (водяной знак, штамп) на повернутом скане поднимают среднее.
    # Поворот принимается, только если чтение стало увереннее.
    if conf < 55 or _cyr_share(lines) < 0.3 or _good_share(lines) < 0.4:
        rotated, rot = fix_orientation(img)
        if rot:
            l2 = ocr_lines(rotated, psm=6)
            if _mean_conf(l2) > conf:
                img, lines, conf, applied = rotated, l2, _mean_conf(l2), rot
    if conf < 40:
        l3 = ocr_lines(img, psm=3)
        if _mean_conf(l3) > conf:
            lines, conf = l3, _mean_conf(l3)
    return lines, img, conf, applied


def _good_share(lines) -> float:
    """Доля осмысленных строк: не короче 4 символов и с уверенностью не ниже 60."""
    if not lines:
        return 0.0
    return sum(1 for t, _, c in lines if c >= 60 and len(t) >= 4) / len(lines)


def _mean_conf(lines) -> float:
    return sum(c for _, _, c in lines) / len(lines) if lines else 0.0


def _cyr_share(lines) -> float:
    txt = "".join(t for t, _, _ in lines)
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", txt)
    if not letters:
        return 0.0
    return len(re.findall(r"[А-Яа-яЁё]", txt)) / len(letters)


# ---------- ячейки таблицы ----------
def _prep(cell: np.ndarray, scale: float, otsu: bool, lanczos: bool = False, thin: int = 0) -> np.ndarray:
    c = cv2.resize(cell, None, fx=scale, fy=scale,
                   interpolation=cv2.INTER_LANCZOS4 if lanczos else cv2.INTER_CUBIC)
    if otsu:
        _, c = cv2.threshold(c, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thin:
        c = cv2.dilate(c, np.ones((thin, thin), np.uint8))   # утоньшаем жирные штрихи
    return cv2.copyMakeBorder(c, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255)


def ocr_number_cell(cell: np.ndarray, base_scale: float = 4.0) -> list[str]:
    """Несколько прочтений числовой ячейки (разные масштабы и бинаризация) для голосования."""
    if cell.size == 0 or min(cell.shape[:2]) < 4:
        return []
    out = []
    lang = _digit_lang()
    for n, (scale, otsu, psm, lz, thin) in enumerate(((base_scale, False, 7, False, 0), (base_scale, True, 7, False, 0),
                                                      (base_scale * 1.25, False, 7, True, 0),
                                                      (base_scale, False, 6, False, 0),
                                                      (base_scale * 1.5, True, 7, True, 3))):
        if n == 3 and len({re.sub(r"\D", "", t) for t in out}) == 1 and re.search(r"\d", out[0]):
            break   # три прочтения совпали: дополнительные варианты не нужны
        try:
            t = pytesseract.image_to_string(
                _prep(cell, scale, otsu, lz, thin), lang=lang,
                config=_cfg(f'--psm {psm} -c tessedit_char_whitelist="0123456789,. "'))
        except Exception:
            continue
        out.append(t.strip())
    return out


def _digit_lang() -> str:
    # Русская модель Tesseract устойчивее читает жирные цифры (не путает 5 и 3), проверено на КФЗ.
    return "rus" if "rus" in _langs().split("+") else "eng"


def ocr_digits_fast(cell: np.ndarray, scale: float) -> str:
    if cell.size == 0 or min(cell.shape[:2]) < 4:
        return ""
    try:
        return pytesseract.image_to_string(_prep(cell, scale, False), lang=_digit_lang(),
                                           config=_cfg('--psm 7 -c tessedit_char_whitelist="0123456789"')).strip()
    except Exception:
        return ""


def ocr_text_cell(cell: np.ndarray, scale: float = 3.0, psm: int = 7) -> str:
    if cell.size == 0 or min(cell.shape[:2]) < 4:
        return ""
    try:
        return pytesseract.image_to_string(_prep(cell, scale, False), lang=_langs(),
                                           config=_cfg(f"--psm {psm}")).strip()
    except Exception:
        return ""
