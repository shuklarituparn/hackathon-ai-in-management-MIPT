"""Извлечение реквизитов первичного документа: тип, номер, дата, период, договор, сумма."""
from __future__ import annotations

import re
from decimal import Decimal

from .models import DocContent, Evidence, Fragment, LineItem, PrimaryDoc
from .money import (DATE_RE, MONTH_RE, MONTH_YEAR_RE, find_amounts, find_dates, fmt, mk_date, month_num,
                    month_range, parse_amount, q, words_to_amount)

DOC_TYPES = [
    ("счет-акт", r"сч[её]т[\s-]*акт"),
    ("счет-фактура", r"сч[её]т[\s-]*фактур"),
    ("УПД", r"универсальн\w+\s+передаточн\w+|\bупд\b"),
    ("накладная", r"товарн\w+\s+накладн|\bнакладная\b|торг-?12"),
    ("акт", r"\bакт\b"),
    ("служебная записка", r"служебн\w+\s+записк"),
    ("бухгалтерская справка", r"бухгалтерск\w+\s+справк"),
    ("справка", r"\bсправк"),
    ("расчетная ведомость", r"ведомост"),
    ("расчет взносов", r"расч[её]т\w*\s+страхов|расч[её]т\w*\s+взнос"),
    ("платежное поручение", r"плат[её]жн\w+\s+поручени"),
    ("авансовый отчет", r"авансов\w+\s+отч[её]т"),
    ("счет", r"\bсч[её]т\b"),
    ("расчет", r"\bрасч[её]т"),
]

# Токен номера договора/документа: буквы, цифры и разделители, обязательно с цифрой.
TOKEN = r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё/\\\-.]*"
CONTRACT_KW_RE = re.compile(
    r"(?:договор\w*|контракт\w*|соглашени\w*|проект\w*)[ \t]*(?:[:№#]|N[oо]?\.?|номер)?[ \t]*[:№#]?[ \t]*(" + TOKEN + r")",
    re.I)
BASIS_RE = re.compile(r"основани\w*[ \t]*[:№#]?[ \t]*(" + TOKEN + r")", re.I)

# Буквы, похожие на цифры при OCR; применяются только к токенам, состоящим в основном из цифр.
_LOOK_DIGIT = str.maketrans({"О": "0", "о": "0", "O": "0", "o": "0", "З": "3", "з": "3", "l": "1", "I": "1",
                             "|": "1", "б": "6", "S": "5", "В": "8", "B": "8"})
# Кириллица, похожая на латиницу (для сравнения номеров документов).
_CYR2LAT = str.maketrans("АВЕКМНОРСТХУаеорсху", "ABEKMHOPCTXYaeopcxy")


def norm_contract(s: str) -> str:
    s = s.strip().strip(".,;:()«»\"'")
    s = re.sub(r"\s+", "", s).replace("\\", "/").replace("–", "-").replace("—", "-")
    digits = sum(ch.isdigit() for ch in s)
    if digits and digits >= len(re.sub(r"[/\-.]", "", s)) * 0.6:
        s = s.translate(_LOOK_DIGIT)
    return s.upper()


def norm_number(s: str) -> str:
    s = re.sub(r"[\s_]+", "", s.strip().strip(".,;:()«»\"'"))
    s = s.replace("–", "-").replace("—", "-").translate(_CYR2LAT).upper()
    return s


def contract_shape(target: str) -> re.Pattern:
    """Регулярное выражение той же формы, что и проверяемый договор: 24/03/15-01 -> \\d+/\\d+/\\d+-\\d+."""
    parts = re.findall(r"\d+|[A-Za-zА-Яа-яЁё]+|[^0-9A-Za-zА-Яа-яЁё]", target)
    rx = ""
    for p in parts:
        if p[0].isdigit():
            rx += r"[\dОоOoЗзlI]{%d,%d}" % (max(1, len(p) - 1), len(p) + 1)
        elif p[0].isalpha():
            rx += r"[A-Za-zА-Яа-яЁё]{1,%d}" % (len(p) + 2)
        else:
            rx += r"\s?" + re.escape(p) + r"\s?"
    return re.compile(r"(?<![\w/.-])" + rx + r"(?![\w/-]|\.\d)")


_KW_TAIL = re.compile(r"(?:договор\w*|контракт\w*|соглашени\w*|основани\w*)\s*[:№#]?\s*$", re.I)


def contract_text(dc: DocContent) -> str:
    """Текст для поиска договора: строки склеиваются, только если номер перенесен на следующую строку."""
    parts = []
    for fr in dc.fragments:
        if parts and not fr.cells and _KW_TAIL.search(parts[-1]):
            parts[-1] += " " + fr.text
        else:
            parts.append(fr.text)
    return " \n ".join(parts)


def find_contracts(text: str, target: str | None = None) -> list[tuple[str, int]]:
    """Номера договоров, упомянутые в тексте: (нормализованный номер, позиция)."""
    found: dict[str, int] = {}
    flat = re.sub(r"[ \t]+", " ", text)
    for rx in (CONTRACT_KW_RE, BASIS_RE):
        for m in rx.finditer(flat):
            tok = m.group(1)
            if not re.search(r"\d", tok) or DATE_RE.fullmatch(tok.strip(".")):
                continue
            if re.fullmatch(r"\d{1,2}", tok.strip(".,")):   # «этап 3», «проект 1» не номер договора
                continue
            found.setdefault(norm_contract(tok), m.start(1))
    if target:
        for m in contract_shape(target).finditer(flat):
            found.setdefault(norm_contract(m.group(0)), m.start())
    return sorted(found.items(), key=lambda kv: kv[1])


def _ev(dc: DocContent, fr: Fragment, what: str) -> Evidence:
    return Evidence(file=dc.name, what=what, text=fr.text.strip()[:400], where=fr.where(), loc=dict(fr.loc))


# ---------------- тип и номер ----------------
def _doc_type(dc: DocContent) -> str:
    fname = re.sub(r"[_\d]+", " ", dc.name.lower())
    for src in (" ".join(f.text for f in dc.fragments[:2]).lower(), fname,
                " ".join(f.text for f in dc.fragments[:6]).lower()):
        for name, rx in DOC_TYPES:
            if re.search(rx, src):
                return name
    return "документ"


_NUM_SIGN = r"(?:№|N[oо°º]\.?|Nо|№\.)"


def _doc_number(dc: DocContent) -> tuple[str, Fragment | None]:
    head = dc.fragments[:8]
    # 1) «Документ: ВЕД-ЗП-2026-01», «Номер документа: ...»
    for fr in head:
        m = re.search(r"(?:документ|номер(?:\s+документа)?)\s*[:№]\s*№?\s*(" + TOKEN + ")", fr.text, re.I)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).rstrip(".,"), fr
    # 2) «АКТ № 118 ОТ 28.01.2026»: знак номера не после слова «договор».
    for fr in head:
        for m in re.finditer(_NUM_SIGN + r"\s*(" + TOKEN + ")", fr.text):
            before = fr.text[max(0, m.start() - 25): m.start()].lower()
            if re.search(r"договор|контракт|соглашени", before):
                continue
            if re.search(r"\d", m.group(1)):
                return m.group(1).rstrip(".,"), fr
    # 3) Заголовок без знака номера: «Служебная записка КЛ-31», «... ДОКУМЕНТ 0426-77».
    for fr in head[:3]:
        if re.search(r"договор|контракт|дата|период", fr.text, re.I):
            continue
        toks = [t for t in re.findall(TOKEN, fr.text) if re.search(r"\d", t) and not DATE_RE.fullmatch(t)]
        if toks:
            return toks[-1].rstrip(".,"), fr
    return "", None


# ---------------- дата и период ----------------
def _doc_date(dc: DocContent):
    cands = []   # (приоритет, порядок, дата, фрагмент)
    for i, fr in enumerate(dc.fragments):
        t = fr.text
        for d, s, e in find_dates(t):
            before = t[max(0, s - 45): s].lower()
            if re.search(r"(договор|контракт|соглашени)[^.;]{0,40}$", before) and "акт" not in before:
                continue   # дата договора, а не документа
            if re.search(r"(?:^|\s)дата(?:\s+документа|\s+составления)?\s*[:\-]?\s*$", before):
                prio = 0
            elif i == 0 or re.search(r"\bот\s*$", before):
                prio = 1
            elif re.search(r"\b(с|по|до|период\w*)\s*$", before):
                prio = 5
            else:
                prio = 2 if i < 8 else 3
            cands.append((prio, i, d, fr))
    if not cands:
        return None, None
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[0][2], cands[0][3]


def _period(dc: DocContent):
    """Период оказания услуг или начисления: (начало, конец, фрагмент)."""
    for fr in dc.fragments:
        t = fr.text
        m = re.search(r"(?:с|c)\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\s*(?:г\.?)?\s*(?:по|-|–)\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})",
                      t, re.I)
        if m:
            a, b = find_dates(m.group(1)), find_dates(m.group(2))
            if a and b:
                return a[0][0], b[0][0], fr
    best = None
    for fr in dc.fragments:
        t = fr.text
        for m in MONTH_YEAR_RE.finditer(t):
            before = t[max(0, m.start() - 30): m.start()].lower()
            if re.search(r"\d{1,2}[»\"']?\s*$", before):       # «16 августа 2025» это дата
                continue
            mn = month_num(m.group(1))
            if not mn:
                continue
            start, end = month_range(int(m.group(2)), mn)
            labeled = bool(re.search(r"(период\w*|за|месяц)\s*[:\-]?\s*$", before))
            if labeled:
                return start, end, fr
            if best is None:
                best = (start, end, fr)
    if best:
        return best
    for fr in dc.fragments:
        m = re.search(r"\b(I{1,3}|IV|[1-4])\s*(?:-?й)?\s*квартал\w*\s*(\d{4})", fr.text, re.I)
        if m:
            qn = {"I": 1, "II": 2, "III": 3, "IV": 4}.get(m.group(1).upper()) or int(m.group(1))
            start, _ = month_range(int(m.group(2)), 3 * qn - 2)
            _, end = month_range(int(m.group(2)), 3 * qn)
            return start, end, fr
    return None, None, None


# ---------------- сумма ----------------
_HDR_AMOUNT = [r"начислено", r"сумма", r"итого", r"стоимост", r"к оплате", r"всего"]
_HDR_ANY = r"код|сумм|начислен|вид|наименован|описани|основани|баз|стоимост|комментар|назначени|количеств|цена|итого"
P1 = r"итого\s+к\s+оплате|всего\s+к\s+оплате|итого\s+по\s+\w+|всего\s+по\s+\w+|итого|всего"
P2 = (r"к\s+оплате(?:\s+по\s+\w+)?|сумма\s+по\s+\w+|стоимость\s+оказанных\s+услуг|стоимость\s+выполненных\s+работ|"
      r"стоимость\s+услуг|стоимость\s+работ|общая\s+стоимость|стоимость|сумма\s+документа|сумма|цена")
_VAT_TAIL = re.compile(r"(?:в\s*т\.?\s*ч\.?|в\s+том\s+числе|включая)\s*ндс.*$|\bндс\b[^.;]*$", re.I)


def _is_num(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    return bool(isinstance(v, str) and re.fullmatch(r"\s*[-−(]?\d[\d   ]*(?:[.,]\d{1,2})?\)?\s*(?:руб\.?)?\s*", v))


def _table_amount(dc: DocContent, doc: PrimaryDoc) -> bool:
    """Сумма из табличной части (xlsx или таблица docx). True, если сумма найдена."""
    rows = [f for f in dc.fragments if f.cells]
    for hi, hdr in enumerate(rows):
        texts = [str(c).lower() for c in hdr.cells]
        if sum(1 for t in texts if re.search(_HDR_ANY, t) and not _is_num(t)) < 2:
            continue
        amount_col = None
        for rx in _HDR_AMOUNT:
            idx = [i for i, t in enumerate(texts) if re.search(rx, t) and "баз" not in t]
            if idx:
                amount_col = idx[0]
                break
        if amount_col is None:
            continue
        mult = 1000 if re.search(r"тыс", texts[amount_col]) else 1
        code_col = next((i for i, t in enumerate(texts) if re.search(r"код", t)), None)
        base_col = next((i for i, t in enumerate(texts) if "баз" in t), None)
        label_col = next((i for i, t in enumerate(texts)
                          if i not in (amount_col, code_col, base_col)
                          and re.search(r"вид|наименован|описани|назначени|основани|содержани", t)), None)
        # Столбцы сопоставляем по адресам ячеек (xlsx) или по порядку (docx).
        hdr_cols = [re.sub(r"\d", "", r) for r in hdr.refs] if hdr.refs else None

        def cell(fr: Fragment, idx):
            if idx is None:
                return None
            if hdr_cols and fr.refs:
                want = hdr_cols[idx]
                for v, r in zip(fr.cells, fr.refs):
                    if re.sub(r"\d", "", r) == want:
                        return v
                return None
            return fr.cells[idx] if idx < len(fr.cells) else None

        items, total, total_fr, base = [], None, None, Decimal(0)
        same_tbl = lambda fr: fr.loc.get("table") == hdr.loc.get("table") and fr.loc.get("sheet") == hdr.loc.get("sheet")
        for fr in rows[hi + 1:]:
            if not same_tbl(fr):
                break
            joined = " ".join(str(c) for c in fr.cells).lower()
            av = cell(fr, amount_col)
            is_total = bool(re.match(r"\s*(итого|всего)", joined)) or \
                any(isinstance(c, str) and re.match(r"\s*(итого|всего)\b", c.lower()) for c in fr.cells)
            if is_total:
                nums = [parse_amount(c) for c in fr.cells if _is_num(c)]
                if nums:
                    total, total_fr = q(nums[-1] * mult), fr
                continue
            if not _is_num(av):
                continue
            amt = q(parse_amount(av) * mult)
            code = cell(fr, code_col)
            code = str(code).strip() if code is not None else None
            if code and re.fullmatch(r"\d{1,3}", code):
                code = code.zfill(4)
            label = cell(fr, label_col)
            items.append(LineItem(code=code, label=str(label).strip() if label is not None else "",
                                  amount=amt, evidence=_ev(dc, fr, "строка документа")))
            bv = cell(fr, base_col)
            if _is_num(bv):
                base += q(parse_amount(bv) * mult)
        if not items and total is None:
            continue
        doc.items = items
        s = sum((it.amount for it in items), Decimal(0))
        if items:
            doc.amount = q(s)
            if total is not None and abs(total - s) > Decimal("0.01"):
                doc.warnings.append(f"Итог в документе ({fmt(total)}) не равен сумме строк ({fmt(s)}); "
                                    f"в расчет взята сумма строк")
            src = total_fr if (total_fr is not None and total is not None and abs(total - s) <= Decimal("0.01")) else None
            if src is None:
                src = Fragment(" ; ".join(it.evidence.text for it in items)[:400],
                               dict(items[0].evidence.loc), None, None)
                if len(items) > 1 and src.loc.get("type") == "xlsx":
                    first, last = items[0].evidence.loc.get("range", ""), items[-1].evidence.loc.get("range", "")
                    src.loc["range"] = f"{first.split(':')[0]}:{last.split(':')[-1]}"
            doc.evidence["amount"] = _ev(dc, src, "сумма документа")
        else:
            doc.amount = total
            doc.evidence["amount"] = _ev(dc, total_fr, "сумма документа")
        if base:
            doc.base_amount = q(base)
        return True
    return False


def _text_amount(dc: DocContent, doc: PrimaryDoc) -> None:
    """Сумма из текста: строки с метками «Итого», «К оплате», «Стоимость», «Сумма»."""
    p1, p2, anyc = [], [], []
    for fr in dc.fragments:
        if fr.cells and fr.loc.get("type") == "xlsx":
            line = " ".join(str(c) for c in fr.cells)
        else:
            line = fr.text
        line_main = _VAT_TAIL.sub("", line)
        amounts = [a for a in find_amounts(line_main)]
        if not amounts:
            continue
        low = line_main.lower()
        for val, s, e, has_cur, has_dec, mult in amounts:
            before = low[max(0, s - 70): s]
            if re.search(r"(договор\w*|контракт\w*|этап\w*|№|n[oо])\s*[:№]?\s*$", before):
                continue
            if val == 0:
                continue
            money_like = has_cur or has_dec
            if re.search(r"(?:" + P1 + r")[^0-9]{0,45}$", before) and money_like:
                p1.append((val, fr))
            elif re.search(r"(?:" + P2 + r")[^0-9]{0,45}$", before) and money_like:
                p2.append((val, fr))
            elif has_cur:
                anyc.append((val, fr))
    chosen = None
    if p1:
        chosen = p1[-1]
    elif p2:
        vals = {v for v, _ in p2}
        chosen = max(p2, key=lambda x: x[0])
        if len(vals) > 1:
            doc.warnings.append("В документе несколько сумм: " + ", ".join(fmt(v) for v in sorted(vals))
                                + "; в расчет взята наибольшая")
    elif anyc:
        vals = {v for v, _ in anyc}
        chosen = max(anyc, key=lambda x: x[0])
        doc.warnings.append("Сумма определена без явной метки" +
                            (": найдено несколько значений" if len(vals) > 1 else ""))
    words = words_to_amount(dc.text)
    doc.amount_words = words
    if chosen:
        doc.amount = chosen[0]
        doc.evidence["amount"] = _ev(dc, chosen[1], "сумма документа")
        if words is not None and abs(words - chosen[0]) > Decimal("0.01"):
            doc.warnings.append(f"Сумма цифрами ({fmt(chosen[0])}) не совпадает с суммой прописью ({fmt(words)})")
    elif words is not None:
        doc.amount = words
        fr = next((f for f in dc.fragments if re.search(r"рубл", f.text, re.I)), dc.fragments[0])
        doc.evidence["amount"] = _ev(dc, fr, "сумма документа (прописью)")
        doc.warnings.append("Сумма указана только прописью")


_NEG_RE = re.compile(r"сторн|корректировк\w+\s+в\s+сторону\s+уменьшени|уменьшени\w+\s+стоимост|возврат\w*\s+средств", re.I)


# ---------------- сборка ----------------
def extract(dc: DocContent, target_contract: str | None = None) -> PrimaryDoc:
    doc = PrimaryDoc(file=dc.name, fmt=dc.fmt, ocr=dc.ocr)
    doc.warnings.extend(dc.warnings)
    if dc.errors:
        doc.errors.extend(dc.errors)
        doc.status, doc.reason = "ошибка чтения", "; ".join(dc.errors)
        return doc
    confs = [f.conf for f in dc.fragments if f.conf is not None]
    if confs:
        doc.ocr_conf = round(sum(confs) / len(confs), 1)
    doc.doc_type = _doc_type(dc)
    doc.number, nfr = _doc_number(dc)
    if nfr is not None:
        doc.evidence["number"] = _ev(dc, nfr, "номер документа")
    doc.doc_date, dfr = _doc_date(dc)
    if dfr is not None:
        doc.evidence["date"] = _ev(dc, dfr, "дата документа")
    ps, pe, pfr = _period(dc)
    if ps:
        doc.period = (ps, pe)
        doc.evidence["period"] = _ev(dc, pfr, "период")
    text = dc.text
    doc.contracts = [c for c, _ in find_contracts(contract_text(dc), target_contract)]
    if doc.contracts:
        tgt = norm_contract(target_contract) if target_contract else None
        key = tgt if tgt in doc.contracts else doc.contracts[0]
        raw = key.replace("0", "[0ОоOo]")
        for fr in dc.fragments:
            if re.search(re.escape(key), norm_contract(fr.text)) or re.search(raw, fr.text):
                doc.evidence["contract"] = _ev(dc, fr, "договор")
                break
    if not _table_amount(dc, doc):
        _text_amount(dc, doc)
    if doc.amount is not None and doc.amount > 0 and _NEG_RE.search(text):
        doc.amount = -doc.amount
        doc.warnings.append("Документ сторнирующий или уменьшающий: сумма учтена со знаком минус")
    if doc.amount is None:
        doc.warnings.append("Не удалось определить сумму документа")
    doc.description = re.sub(r"\s+", " ", text)[:1500]
    return doc
