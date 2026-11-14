"""Основной процесс: загрузка набора, связывание документов, расчет ожидаемых значений, заключение."""
from __future__ import annotations

import difflib
import itertools
import os
import re
import shutil
import tempfile
import time
import unicodedata
import zipfile
from datetime import date, datetime
from decimal import Decimal

from . import __version__, config
from .classify import KIND_NAMES, LEXICON, classify, score
from .models import DocContent, Evidence, PrimaryDoc, Report
from .money import (MONTH_NAMES, MONTH_YEAR_RE, find_dates, fmt, fmt_date, month_num, month_range,
                    months_between, q)
from .readers import read_any
from .report import parse_report
from .requisites import extract, find_contracts, norm_contract, norm_number

OK, BAD, UNK = "ОК", "РАСХОЖДЕНИЕ", "НЕПРОВЕРЕНО"

# Категории несоответствий.
C_SUM = "расхождение суммы"
C_MISSING = "пропущенный документ"
C_DUP = "дублирование"
C_PERIOD = "неверный период"
C_LINK = "связь документов"
C_NODATA = "недостаточно данных"
C_OTHER = "несовпадение другого показателя"
C_ARITH = "арифметика отчета"
C_READ = "ошибка чтения"

S_COUNTED = "учтен"
S_DUP = "дубликат"
S_PERIOD = "вне периода"
S_CONTRACT = "другой договор"
S_NOCONTRACT = "договор не указан"
S_NODATE = "дата не определена"
S_NOAMOUNT = "сумма не определена"
S_NOKIND = "статья не определена"
S_ERROR = "ошибка чтения"
UNCERTAIN = {S_NOCONTRACT, S_NODATE, S_NOAMOUNT, S_NOKIND, S_ERROR}

SKIP_NAMES = re.compile(r"^(~\$|\.)|^thumbs\.db$|^desktop\.ini$", re.I)
README_RE = re.compile(r"readme|описани|набор[_ ]данных|условие", re.I)
KEY_RE = re.compile(r"^!|правильн|ответ|эталон|разбор|answer|solution", re.I)
REPORT_RE = re.compile(r"карточк|кфз|итогов\w*[_ ]отч|\bотч[её]т|report", re.I)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# ======================= загрузка набора =======================
def _unzip(path: str, dest: str) -> None:
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:   # имя не в UTF-8: архивы Windows используют cp866
                try:
                    name = name.encode("cp437").decode("cp866")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            name = nfc(name).replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".", "..")]
            if not parts or info.is_dir() or parts[0] == "__MACOSX":
                continue
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def collect_files(path: str) -> tuple[list[str], str | None]:
    """Список файлов набора. Для zip распаковывает во временный каталог (второй элемент результата)."""
    tmp = None
    if os.path.isfile(path) and zipfile.is_zipfile(path) and not path.lower().endswith((".docx", ".xlsx")):
        tmp = tempfile.mkdtemp(prefix="sverka_")
        _unzip(path, tmp)
        path = tmp
    if os.path.isfile(path):
        return [path], tmp
    files = []
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d != "__MACOSX" and not d.startswith(".")]
        for n in sorted(names):
            if SKIP_NAMES.search(n):
                continue
            files.append(os.path.join(root, n))
    files.sort(key=lambda p: nfc(os.path.basename(p)).lower())
    return files, tmp


# ======================= параметры проверки =======================
def _params_from_readme(dcs: list[DocContent]) -> dict:
    p: dict = {}
    for dc in dcs:
        for fr in dc.fragments:
            t = fr.text
            m = re.search(r"итогов\w+\s+отч[её]т\w*\s*[:\-]\s*[«\"']?([^»\"'\n]+?)[»\"']?\s*\.?\s*$", t, re.I)
            if m and "report_file" not in p:
                p["report_file"] = nfc(m.group(1).strip().rstrip("."))
            m = re.search(r"проверяем\w+\s+строк\w+(?:\s+отч[её]та)?\s*[:\-]\s*[«\"']?([^»\"'\n]+?)[»\"']?\s*\.?\s*$", t, re.I)
            if m and "row" not in p:
                p["row"] = m.group(1).strip()
            m = re.match(r"\s*(?:договор|контракт)\w*\s*(?:№|N)?\s*[:\-]\s*(\S+)", t, re.I)
            if m and "contract" not in p:
                p["contract"] = m.group(1).rstrip(".,;")
            if re.search(r"период", t, re.I) and "period" not in p:
                ds = find_dates(t)
                if len(ds) >= 2:
                    p["period"] = (ds[0][0], ds[1][0])
    return p


def _match_row(rep: Report, wanted: str | None):
    """Строка отчета, которую нужно проверить."""
    data = [r for r in rep.rows if r.kind == "data"] or rep.rows
    if not wanted:
        return (data[-1] if data else None), (len(data) > 1)

    def norm(s):
        return re.sub(r"[^0-9a-zа-я]", "", s.lower().replace("ё", "е"))

    def nums(s):
        return set(re.findall(r"\d+", s))

    best, best_sc = None, 0.0
    for r in rep.rows:
        sc = difflib.SequenceMatcher(None, norm(wanted), norm(r.label)).ratio()
        if nums(wanted) and nums(wanted) <= nums(r.label):
            sc += 0.5
        if (r.kind == "total") != bool(re.search(r"всего|итого", wanted.lower())):
            sc -= 0.3
        if sc > best_sc:
            best, best_sc = r, sc
    return (best if best_sc >= 0.6 else None), False


def _infer_period(rep: Report, row_label: str):
    """Период по подписи строки: «3 этап, 2026 г.» внутри периода карточки."""
    m = MONTH_YEAR_RE.search(row_label)
    if m and month_num(m.group(1)):
        return month_range(int(m.group(2)), month_num(m.group(1)))
    y = re.search(r"(?<!\d)(20\d{2})(?!\d)", row_label)
    if y:
        start, end = date(int(y.group(1)), 1, 1), date(int(y.group(1)), 12, 31)
        if rep.period:
            start, end = max(start, rep.period[0]), min(end, rep.period[1])
        if start <= end:
            return start, end
    return rep.period


# ======================= связывание документов =======================
_COPY_RE = re.compile(r"копи|copy|дубл|\(\d+\)|scan|скан", re.I)


def _dedup(docs: list[PrimaryDoc]) -> None:
    usable = [d for d in docs if d.amount is not None and not d.errors]

    def key(d):
        when = d.doc_date or (d.period[0] if d.period else None)
        return (norm_number(d.number), d.amount, when)

    def rank(d):   # оригиналом считаем файл без слова «копия», затем по имени
        return (bool(_COPY_RE.search(d.file)), d.ocr, d.file.lower())

    groups: dict = {}
    for d in usable:
        if d.number:
            groups.setdefault(key(d), []).append(d)
    seen = set()
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=rank)
        for d in g[1:]:
            d.status, d.duplicate_of = S_DUP, g[0].file
            d.reason = (f"повтор документа «{g[0].file}»: совпадают номер ({d.number}), дата/период и сумма "
                        f"({fmt(d.amount)})")
            seen.add(d.file)
    # Копии без номера или с искаженным номером: почти одинаковый текст и та же сумма.
    rest = [d for d in usable if d.file not in seen]
    for a, b in itertools.combinations(sorted(rest, key=rank), 2):
        if b.status == S_DUP or a.status == S_DUP or a.amount != b.amount:
            continue
        if a.number and b.number and norm_number(a.number) != norm_number(b.number):
            continue
        if difflib.SequenceMatcher(None, a.description[:600], b.description[:600]).ratio() >= 0.93:
            b.status, b.duplicate_of = S_DUP, a.file
            b.reason = f"повтор документа «{a.file}»: совпадают текст и сумма ({fmt(b.amount)})"
    # Один номер и дата, но разные суммы: возможна исправленная версия.
    by_num: dict = {}
    for d in usable:
        if d.number and d.status != S_DUP:
            by_num.setdefault((norm_number(d.number), d.doc_date), []).append(d)
    for g in by_num.values():
        if len(g) > 1 and len({d.amount for d in g}) > 1:
            for d in g:
                d.warnings.append("Есть другой документ с тем же номером и датой, но другой суммой: "
                                  + ", ".join(x.file for x in g if x is not d))


def _link(d: PrimaryDoc, contract: str | None, period) -> None:
    """Статус документа относительно проверяемого договора и периода."""
    if d.status == S_DUP:
        return
    if d.errors:
        d.status, d.reason = S_ERROR, "; ".join(d.errors)
        return
    if d.amount is None:
        d.status, d.reason = S_NOAMOUNT, "в документе не найдена сумма"
        return
    if contract:
        tgt = norm_contract(contract)
        if tgt not in d.contracts:
            if d.contracts:
                d.status = S_CONTRACT
                d.reason = f"договор {', '.join(d.contracts)} вместо проверяемого {contract}"
            else:
                d.status, d.reason = S_NOCONTRACT, "в документе не найден номер договора"
            return
        others = [c for c in d.contracts if c != tgt]
        if others:
            d.warnings.append("В документе упомянуты и другие номера: " + ", ".join(others))
    if period:
        start, end = period
        by_date = (start <= d.doc_date <= end) if d.doc_date else None
        by_per = None
        if d.period:
            ps, pe = d.period
            by_per = True if (ps >= start and pe <= end) else (False if (pe < start or ps > end) else None)
        if by_date is None and d.period is None:
            d.status, d.reason = S_NODATE, "в документе не найдены дата и период"
            return
        inside = by_date if by_date is not None else by_per
        if inside is None:
            d.status, d.reason = S_NODATE, (f"период документа {fmt_date(d.period[0])}-{fmt_date(d.period[1])} "
                                            f"лишь частично входит в проверяемый период")
            return
        if by_date is not None and by_per is not None and by_date != by_per:
            d.warnings.append("Дата документа и период оказания услуг относятся к разным периодам; "
                              "решение принято по дате документа")
        if not inside:
            when = (f"дата {fmt_date(d.doc_date)}" if d.doc_date else
                    f"период {fmt_date(d.period[0])}-{fmt_date(d.period[1])}")
            d.status = S_PERIOD
            d.reason = f"{when} вне проверяемого периода {fmt_date(start)}-{fmt_date(end)}"
            return
    if d.kind is None:
        d.status, d.reason = S_NOKIND, "не удалось определить статью затрат: " + d.kind_reason
        return
    d.status = S_COUNTED
    d.reason = f"договор и период совпадают; статья: {KIND_NAMES.get(d.kind, d.kind)} ({d.kind_reason})"


_DOC_EXT = (".pdf", ".docx", ".xlsx", ".doc", ".xls", ".rtf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
_ACC_TYPES = {"акт", "счет-акт", "счет-фактура", "УПД", "накладная", "счет", "расчетная ведомость",
              "расчет взносов", "бухгалтерская справка", "справка", "авансовый отчет", "платежное поручение"}


def _blocking(d: PrimaryDoc) -> bool:
    """Документ не прочитан, хотя по виду является учетным: его сумма могла бы объяснить расхождение.
    Посторонние файлы (неизвестный формат, заметки без суммы) проверку не блокируют."""
    if d.amount is not None:
        return False
    if d.errors:
        return d.file.lower().endswith(_DOC_EXT)
    return d.ocr or d.doc_type in _ACC_TYPES


def _kind_from_name(name: str, kinds: list[str]) -> str | None:
    """Вид затрат по имени файла (для нечитаемых документов)."""
    text = re.sub(r"[_\-.\d]+", " ", name.lower())
    text = re.sub(r"\bзп\b", "заработная плата", text)
    sc = score(text, kinds)
    top = max(sc.items(), key=lambda kv: kv[1][0], default=None)
    return top[0] if top and top[1][0] >= 2 else None


# ======================= расчет =======================
def _subset(docs: list[PrimaryDoc], target: Decimal, max_n: int = 3):
    """Подмножество документов с суммой ровно target (до max_n документов)."""
    docs = [d for d in docs if d.amount is not None][:18]
    for n in range(1, min(max_n, len(docs)) + 1):
        for combo in itertools.combinations(docs, n):
            if abs(sum(d.amount for d in combo) - target) <= config.TOLERANCE:
                return list(combo)
    return None


def _source(d: PrimaryDoc) -> dict:
    ev = d.evidence.get("amount")
    return {"file": d.file, "doc_type": d.doc_type, "number": d.number, "date": fmt_date(d.doc_date),
            "period": _per(d.period), "amount": d.amount,
            "fragment": ev.text if ev else "", "where": ev.where if ev else "",
            "image": ev.image if ev else None}


def _per(p) -> str:
    if not p:
        return ""
    if p[0].day == 1 and month_range(p[0].year, p[0].month)[1] == p[1]:
        return f"{MONTH_NAMES[p[0].month]} {p[0].year}"
    return f"{fmt_date(p[0])}-{fmt_date(p[1])}"


class Reconciler:
    def __init__(self, rep: Report, row, docs: list[PrimaryDoc], period, contract):
        self.rep, self.row, self.docs, self.period, self.contract = rep, row, docs, period, contract
        self.indicators: list[dict] = []
        self.issues: list[dict] = []

    def counted(self, kind):
        return [d for d in self.docs if d.status == S_COUNTED and d.kind == kind]

    def excluded(self, kind):
        return [d for d in self.docs if d.status in (S_DUP, S_PERIOD, S_CONTRACT) and d.kind == kind]

    def uncertain(self, kind):
        """Документы, которые могли бы относиться к статье, но не могут быть учтены уверенно."""
        out = []
        for d in self.docs:
            if d.status in UNCERTAIN and (d.kind in (None, kind)):
                out.append(d)
        return out

    def _base(self, col):
        cell = self.row.cells.get(col.num)
        ind = {"id": f"col{col.num}", "column": col.num, "name": col.title, "role": col.role, "code": col.code,
               "unit": col.unit, "report_value": cell.value if cell else None, "expected_value": None,
               "diff": None, "status": UNK, "category": "", "explanation": "", "sources": [], "excluded": [],
               "report_note": (cell.note if cell else "ячейка отчета не найдена"),
               "report_reliable": bool(cell and cell.reliable and cell.value is not None),
               "report_bbox": cell.bbox if cell else None}
        return ind, cell

    # ---------- статья затрат ----------
    def article(self, col) -> dict:
        ind, cell = self._base(col)
        counted, excluded = self.counted(col.kind), self.excluded(col.kind)
        uncertain = self.uncertain(col.kind)
        ind["sources"] = [_source(d) for d in counted]
        ind["excluded"] = [{"file": d.file, "status": d.status, "reason": d.reason, "amount": d.amount}
                           for d in excluded + uncertain]
        rv = ind["report_value"]
        expected = q(sum((d.amount for d in counted), Decimal(0)))
        ind["expected_value"] = expected if counted else None
        tol = config.TOLERANCE
        if rv is None:
            ind.update(category=C_READ, explanation="Значение в отчете не распознано; "
                       + (f"по документам получено {fmt(expected)} руб." if counted else "документов по статье нет."))
            return ind
        if not counted:
            if abs(rv) <= tol:
                ind.update(status=OK, expected_value=Decimal("0.00"), diff=Decimal("0.00"),
                           explanation="В отчете нулевое значение, документов по статье в наборе нет.")
            else:
                ind.update(category=C_NODATA,
                           explanation="В наборе нет подтверждающих документов по этой статье: проверить значение нельзя."
                           + self._uncertain_text(uncertain))
            return ind
        diff = q(rv - expected)
        ind["diff"] = diff
        n = len(counted)
        base = f"Сумма {n} {_plural(n, 'документа', 'документов', 'документов')}: {fmt(expected)} руб."
        if excluded:
            base += " Не учтены: " + "; ".join(f"«{d.file}» ({d.status})" for d in excluded) + "."
        if abs(diff) <= tol:
            ind.update(status=OK, explanation=base + (" Значение отчета совпадает."
                       if diff == 0 else f" Отличие {fmt(diff)} руб. в пределах допуска на округление."))
            return ind
        direction = "завышено" if diff > 0 else "занижено"
        text = f"{base} Значение отчета {direction} на {fmt(abs(diff))} руб."
        if not ind["report_reliable"]:
            ind.update(category=C_READ, explanation=text + " Значение отчета распознано неуверенно "
                       f"({ind['report_note']}), поэтому расхождение не подтверждено.")
            return ind
        # 1) Не хватает ежемесячного документа.
        missing = self._missing_months(col.kind, counted)
        if missing and diff > 0:
            ind.update(category=C_MISSING, explanation=text + " В наборе нет документов за: "
                       + ", ".join(f"{MONTH_NAMES[m]} {y}" for y, m in missing)
                       + ". Это недостаток данных, а не подтвержденная ошибка отчета.")
            return ind
        # 2) Расхождение объясняется документом, который нельзя уверенно учесть.
        if diff > 0 and uncertain:
            with_amount = [d for d in uncertain if d.amount is not None]
            hit = _subset(with_amount, diff)
            unreadable = [d for d in uncertain if _blocking(d)] if config.UNREADABLE_BLOCKS else []
            if hit or unreadable:
                why = ("сумма расхождения равна сумме документов с неоднозначной связью: "
                       + ", ".join(f"«{d.file}» ({d.status})" for d in hit)) if hit else \
                      ("часть документов не прочитана: " + ", ".join(f"«{d.file}»" for d in unreadable)
                       + ". Если эти документы не относятся к статье, расхождение является ошибкой отчета")
                ind.update(category=C_LINK if hit else C_NODATA,
                           explanation=text + " Расхождение не подтверждено: " + why + ".")
                return ind
        # 3) Подтвержденное расхождение: уточняем категорию.
        cat, hint = C_SUM, ""
        if diff > 0:
            hit = _subset(excluded, diff) or _subset([d for d in self.docs if d.status in (S_DUP, S_PERIOD, S_CONTRACT)], diff)
            if hit:
                sts = {d.status for d in hit}
                cat = C_DUP if sts == {S_DUP} else C_PERIOD if sts == {S_PERIOD} else C_LINK
                hint = (" Сумма расхождения равна сумме исключенных документов: "
                        + ", ".join(f"«{d.file}» ({d.status}, {fmt(d.amount)})" for d in hit)
                        + ". Вероятно, они ошибочно включены в отчет.")
            else:
                hint = (" Расхождение не объясняется ни одним документом набора: вероятна ошибка суммы в отчете "
                        "либо отсутствует подтверждающий документ на эту сумму.")
        else:
            hit = _subset(counted, -diff)
            if hit:
                cat = C_MISSING
                hint = (" Сумма расхождения равна сумме документов: "
                        + ", ".join(f"«{d.file}» ({fmt(d.amount)})" for d in hit)
                        + ". Вероятно, они не включены в отчет.")
        ind.update(status=BAD, category=cat, explanation=text + hint)
        return ind

    def _uncertain_text(self, uncertain) -> str:
        if not uncertain:
            return ""
        return " Документы с неоднозначной связью: " + "; ".join(f"«{d.file}» ({d.status})" for d in uncertain) + "."

    def _missing_months(self, kind, counted):
        """Для ежемесячных документов (ведомости, расчеты взносов) проверяем покрытие периода."""
        if kind not in ("labor", "insurance") or not self.period:
            return []
        monthly = [d for d in counted if d.period and (d.period[0].year, d.period[0].month) == (d.period[1].year, d.period[1].month)]
        if len(monthly) < 2 or len(monthly) != len(counted):
            return []
        have = {(d.period[0].year, d.period[0].month) for d in monthly}
        return [ym for ym in months_between(*self.period) if ym not in have]

    # ---------- итог ----------
    def total(self, col, arts: list[dict]) -> dict:
        ind, cell = self._base(col)
        rv = ind["report_value"]
        known = [a for a in arts if a["expected_value"] is not None]
        bad = [a for a in arts if a["status"] == BAD]
        unk = [a for a in arts if a["status"] == UNK]
        expected = q(sum((a["expected_value"] for a in known), Decimal(0)))
        ind["sources"] = [{"file": "", "doc_type": "статья", "number": a["code"] or "", "date": "", "period": "",
                           "amount": a["expected_value"], "fragment": a["name"], "where": "расчет по статье",
                           "image": None} for a in known]
        if rv is None:
            ind.update(category=C_READ, explanation="Итог в отчете не распознан.")
            return ind
        ind["expected_value"] = expected
        ind["diff"] = q(rv - expected)
        if unk:
            ind.update(category=C_NODATA, explanation="Итог нельзя подтвердить полностью: не проверены статьи "
                       + ", ".join(a["name"] for a in unk) + ". По проверенным статьям: " + fmt(expected) + " руб.")
            if bad:
                ind["explanation"] += " Подтвержденные расхождения: " + ", ".join(
                    f"{a['name']} ({fmt(a['diff'])})" for a in bad) + "."
                ind["status"], ind["category"] = BAD, C_SUM
            return ind
        if abs(ind["diff"]) <= config.TOLERANCE:
            ind.update(status=OK, explanation=f"Сумма ожидаемых значений по статьям: {fmt(expected)} руб. Итог совпадает.")
        elif not ind["report_reliable"]:
            ind.update(category=C_READ, explanation="Итог отчета распознан неуверенно, расхождение не подтверждено.")
        else:
            parts = ", ".join(f"{a['name']}: {fmt(a['diff'])} руб." for a in bad)
            expl = sum((a["diff"] for a in bad), Decimal(0))
            tail = (" Общее расхождение полностью объясняется статьями: " + parts) if bad and abs(expl - ind["diff"]) <= config.TOLERANCE \
                else (" Расхождения по статьям: " + parts if bad else " Итог не равен сумме статей отчета.")
            ind.update(status=BAD, category=C_SUM if bad else C_ARITH,
                       explanation=f"Ожидаемый итог по документам: {fmt(expected)} руб., в отчете {fmt(rv)} руб., "
                                   f"расхождение {fmt(ind['diff'])} руб." + tail)
        return ind

    # ---------- детализация (подпоказатели) ----------
    def sub(self, col) -> dict:
        ind, cell = self._base(col)
        rv = ind["report_value"]
        labor = self.counted("labor")
        items = [(d, it) for d in labor for it in d.items]
        main = [(d, it) for d, it in items if re.search(r"основн", it.label.lower())]
        add = [(d, it) for d, it in items if re.search(r"дополнит|резерв|отпуск", it.label.lower())]

        def src(pairs):
            return [{"file": d.file, "doc_type": d.doc_type, "number": d.number, "date": fmt_date(d.doc_date),
                     "period": _per(d.period), "amount": it.amount,
                     "fragment": it.evidence.text if it.evidence else it.label,
                     "where": it.evidence.where if it.evidence else "", "image": None} for d, it in pairs]

        nodata = "Отдельного подтверждающего документа (оборотно-сальдовой ведомости) в наборе нет."
        if col.key == "labor_main" and main and rv is not None:
            s = q(sum(it.amount for _, it in main))
            if abs(s - rv) <= config.TOLERANCE:
                ind.update(status=OK, expected_value=s, diff=q(rv - s), sources=src(main),
                           explanation=f"Подтверждено строками «основная заработная плата» расчетных ведомостей: {fmt(s)} руб.")
            else:
                ind.update(category=C_NODATA, expected_value=None, sources=src(main),
                           explanation=f"{nodata} Строки «основная заработная плата» ведомостей дают {fmt(s)} руб.; "
                                       f"этого недостаточно, чтобы считать значение ошибочным.")
            return ind
        if col.key in ("labor_add", "labor_reserve") and add:
            s = q(sum(it.amount for _, it in add))
            other_key = 9 if col.key == "labor_add" else 8
            pair = (rv or Decimal(0)) + (self.row.val(other_key) or Decimal(0))
            note = (f"Сумма граф 8 и 9 ({fmt(pair)} руб.) подтверждена строками дополнительной заработной платы "
                    f"и резерва отпусков ведомостей ({fmt(s)} руб.), но разбивка между графами документами не "
                    f"подтверждается.") if abs(pair - s) <= config.TOLERANCE else \
                   (f"Ведомости дают только общую сумму дополнительной заработной платы и резерва отпусков "
                    f"({fmt(s)} руб.), разбивка по графам отсутствует.")
            ind.update(category=C_NODATA, sources=src(add), explanation=note)
            return ind
        ind.update(category=C_NODATA, explanation=nodata if col.role == "sub" else
                   "В наборе нет документов, подтверждающих этот показатель (например, табелей учета рабочего времени).")
        return ind

    # ---------- дополнительные проверки ----------
    def cross_checks(self) -> None:
        pay: dict = {}
        for d in self.counted("labor"):
            if d.period:
                k = (d.period[0].year, d.period[0].month)
                pay[k] = pay.get(k, Decimal(0)) + d.amount
        for d in self.counted("insurance"):
            if d.base_amount is None or not d.period:
                continue
            k = (d.period[0].year, d.period[0].month)
            if k in pay and abs(pay[k] - d.base_amount) > config.TOLERANCE:
                self.issues.append({"category": C_OTHER, "severity": "warning", "files": [d.file],
                                    "text": f"База начисления взносов за {MONTH_NAMES[k[1]]} {k[0]} ({fmt(d.base_amount)} руб.) "
                                            f"не равна начисленной оплате труда по ведомости ({fmt(pay[k])} руб.)."})


def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


_REPORT_CACHE: dict = {}


def _parse_report_cached(path: str, dc: DocContent) -> Report:
    """Разбор отчета занимает основное время (OCR), поэтому результат кешируется по хешу файла."""
    import copy
    import hashlib
    try:
        with open(path, "rb") as f:
            key = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return parse_report(dc)
    if key not in _REPORT_CACHE:
        if len(_REPORT_CACHE) > 16:
            _REPORT_CACHE.clear()
        _REPORT_CACHE[key] = parse_report(dc)
    rep = copy.copy(_REPORT_CACHE[key])
    rep.file = dc.name
    return rep


# ======================= главный вход =======================
def run_check(path: str, contract: str | None = None, period_from: date | None = None,
              period_to: date | None = None, row: str | None = None, report_file: str | None = None,
              progress=None) -> dict:
    """Выполняет сверку набора (каталог или zip). Возвращает результат и служебные объекты для вывода."""
    t0 = time.time()
    say = progress or (lambda msg: None)
    files, tmp = collect_files(path)
    result: dict = {"meta": {"version": __version__, "generated_at": datetime.now().isoformat(timespec="seconds"),
                             "input": os.path.basename(os.path.abspath(path))},
                    "params": {}, "verdict": {}, "indicators": [], "documents": [], "report_checks": [],
                    "issues": [], "service_files": [], "errors": []}
    ctx = {"contents": {}, "report": None, "tmp": tmp}
    if not files:
        result["errors"].append("Входной набор пуст: файлы не найдены")
        _verdict(result)
        return {"result": result, "ctx": ctx}

    # 1. Служебные файлы: описание набора и эталон (эталон в сверке не участвует).
    readmes, primaries_paths = [], []
    for f in files:
        name = nfc(os.path.basename(f))
        stem = os.path.splitext(name)[0]
        if KEY_RE.search(stem):
            result["service_files"].append({"file": name, "role": "эталон или разбор: в сверке не используется"})
        elif README_RE.search(stem):
            readmes.append(read_any(f))
            result["service_files"].append({"file": name, "role": "описание набора: источник параметров проверки"})
        else:
            primaries_paths.append(f)
    params = _params_from_readme(readmes)

    # 2. Итоговый отчет.
    want = nfc(report_file or params.get("report_file") or "")
    rep_path = None
    if want:
        rep_path = next((f for f in primaries_paths if nfc(os.path.basename(f)).lower() == want.lower()), None) or \
                   next((f for f in primaries_paths if os.path.splitext(want.lower())[0] in nfc(os.path.basename(f)).lower()), None)
    if rep_path is None:
        rep_path = next((f for f in primaries_paths if REPORT_RE.search(nfc(os.path.basename(f)))), None)
    contents: dict[str, DocContent] = {}
    if rep_path is None:   # ищем по содержимому
        for f in primaries_paths:
            dc = read_any(f)
            dc.name = nfc(dc.name)
            contents[f] = dc
            if re.search(r"карточка\s+уч[её]та\s+фактических\s+затрат", dc.text, re.I):
                rep_path = f
                break
    if rep_path is None:
        result["errors"].append("Итоговый отчет не найден: укажите файл параметром --report")
        _verdict(result)
        return {"result": result, "ctx": ctx}
    say(f"Разбор отчета: {os.path.basename(rep_path)}")
    rep_dc = contents.get(rep_path) or read_any(rep_path)
    rep_dc.name = nfc(rep_dc.name)
    rep = _parse_report_cached(rep_path, rep_dc)
    ctx["report"], ctx["report_dc"] = rep, rep_dc
    result["errors"].extend(f"Отчет: {e}" for e in rep.errors + rep_dc.errors if e)

    # 3. Параметры проверки: явные > описание набора > шапка отчета.
    contract = contract or params.get("contract") or rep.contract
    target_row, guessed = _match_row(rep, row or params.get("row"))
    row_label = target_row.label if target_row else (row or params.get("row") or "")
    period = (period_from, period_to) if period_from and period_to else params.get("period")
    period_src = "задан пользователем" if period_from and period_to else "описание набора"
    if not period and target_row is not None:
        period, period_src = _infer_period(rep, row or params.get("row") or target_row.label), "определен по строке и шапке отчета"
    if guessed:
        result["issues"].append({"category": C_NODATA, "severity": "info", "files": [rep_dc.name],
                                 "text": f"Проверяемая строка не задана, выбрана последняя строка данных: «{row_label}»."})
    if (row or params.get("row")) and target_row is None and rep.rows:
        result["errors"].append(f"Строка «{row or params.get('row')}» не найдена в отчете. Найдены строки: "
                                + "; ".join(f"«{r.label}»" for r in rep.rows))

    # 4. Первичные документы.
    kinds = [c.kind for c in rep.columns if c.role == "article" and c.kind]
    code_map = {c.code[:2]: c.kind for c in rep.columns if c.role == "article" and c.code and c.kind}
    if not kinds:   # отчет не разобран: используем стандартные статьи, чтобы хотя бы описать документы
        kinds = ["materials", "labor", "insurance", "prod_overhead", "gen_overhead"]
        code_map = {"01": "materials", "02": "labor", "03": "insurance", "08": "prod_overhead", "09": "gen_overhead"}
    docs: list[PrimaryDoc] = []
    for f in primaries_paths:
        if f == rep_path:
            continue
        say(f"Чтение: {os.path.basename(f)}")
        dc = contents.get(f) or read_any(f)
        dc.name = nfc(dc.name)
        contents[dc.name] = dc
        d = extract(dc, contract)
        if not d.errors:
            classify(d, kinds, code_map)
        else:
            d.kind = _kind_from_name(d.file, kinds)
        docs.append(d)
    ctx["contents"] = {k: v for k, v in contents.items() if isinstance(k, str) and not os.path.isabs(k)}
    ctx["docs"] = docs
    if not contract:   # договор не задан нигде: берем самый частый в первичке
        freq: dict = {}
        for d in docs:
            for c in d.contracts[:1]:
                freq[c] = freq.get(c, 0) + 1
        if freq:
            contract = max(freq, key=freq.get)
            result["issues"].append({"category": C_NODATA, "severity": "info", "files": [],
                                     "text": f"Номер договора не задан и не найден в отчете, принят самый частый: {contract}."})
    _dedup(docs)
    for d in docs:
        _link(d, contract, period)

    result["params"] = {"report_file": rep_dc.name, "report_title": rep.title, "report_schema": rep.schema,
                        "report_source": "текстовый слой" if rep.source == "text" else "OCR скана",
                        "row": row_label, "contract": contract or "",
                        "period_from": period[0] if period else None, "period_to": period[1] if period else None,
                        "period_source": period_src if period else "не определен",
                        "tolerance": config.TOLERANCE,
                        "report_meta": {k: v for k, v in rep.meta.items() if not k.startswith("_")}}

    # 5. Сверка показателей.
    rec = Reconciler(rep, target_row, docs, period, contract)
    if target_row is not None:
        arts = []
        for col in rep.columns:
            if col.role == "article":
                ind = rec.article(col)
                arts.append(ind)
            elif col.role == "total":
                continue
            else:
                ind = rec.sub(col)
            result["indicators"].append(ind)
        for col in rep.columns:
            if col.role == "total":
                result["indicators"].append(rec.total(col, arts))
        result["indicators"].sort(key=lambda i: i["column"])
        rec.cross_checks()
    result["issues"].extend(rec.issues)

    # 6. Реестр документов и замечания.
    for d in docs:
        result["documents"].append(_doc_dict(d))
        cat = {S_DUP: C_DUP, S_PERIOD: C_PERIOD, S_CONTRACT: C_LINK, S_NOCONTRACT: C_LINK, S_NODATE: C_NODATA,
               S_NOAMOUNT: C_NODATA, S_NOKIND: C_LINK, S_ERROR: C_READ}.get(d.status)
        if cat:
            result["issues"].append({"category": cat, "severity": "warning", "files": [d.file],
                                     "text": f"«{d.file}» не учтен ({d.status}): {d.reason}."})
        if d.status == S_COUNTED and d.kind_source != "code" and d.kind_conf < config.CLASSIFY_MIN_CONF:
            result["issues"].append({"category": C_LINK, "severity": "warning", "files": [d.file],
                                     "text": f"«{d.file}»: статья затрат определена неуверенно ({d.kind_reason})."})
        for w in d.warnings:
            if re.search(r"OCR|формул", w):
                continue
            result["issues"].append({"category": C_OTHER, "severity": "info", "files": [d.file],
                                     "text": f"«{d.file}»: {w}"})
    for ch in rep.checks:
        result["report_checks"].append({k: ch[k] for k in ("check", "status", "left", "right", "note")})
        if ch["status"] == "НАРУШЕНО":
            result["issues"].append({"category": C_ARITH, "severity": "warning", "files": [rep_dc.name],
                                     "text": f"Арифметика отчета: {ch['check']}: {fmt(ch['left'])} против {fmt(ch['right'])}."})
    for w in rep.warnings:
        result["issues"].append({"category": C_READ, "severity": "info", "files": [rep_dc.name], "text": "Отчет: " + w})
    ctx["target_row"] = target_row
    result["report_rows"] = [{"label": r.label, "kind": r.kind, "is_target": r is target_row,
                              "cells": {str(c): r.cells[c].value for c in sorted(r.cells)}} for r in rep.rows]
    _verdict(result)
    result["meta"]["elapsed_sec"] = round(time.time() - t0, 1)
    result["meta"]["llm"] = config.LLM_URL or "не используется"
    return {"result": result, "ctx": ctx}


def _doc_dict(d: PrimaryDoc) -> dict:
    return {"file": d.file, "format": d.fmt, "ocr": d.ocr, "ocr_confidence": d.ocr_conf, "doc_type": d.doc_type,
            "number": d.number, "date": d.doc_date, "period": _per(d.period), "contracts": d.contracts,
            "amount": d.amount, "amount_in_words": d.amount_words, "base_amount": d.base_amount,
            "article": KIND_NAMES.get(d.kind, "") if d.kind else "", "article_kind": d.kind,
            "article_confidence": d.kind_conf, "article_reason": d.kind_reason, "article_source": d.kind_source,
            "status": d.status, "reason": d.reason, "duplicate_of": d.duplicate_of,
            "items": [{"code": it.code, "label": it.label, "amount": it.amount,
                       "where": it.evidence.where if it.evidence else ""} for it in d.items],
            "evidence": {k: {"what": e.what, "text": e.text, "where": e.where, "image": e.image}
                         for k, e in d.evidence.items()},
            "warnings": d.warnings, "errors": d.errors}


def _verdict(result: dict) -> None:
    inds = result["indicators"]
    main = [i for i in inds if i["role"] in ("article", "total")]
    bad = [i for i in inds if i["status"] == BAD]
    unk_main = [i for i in main if i["status"] == UNK]
    counts = {OK: sum(i["status"] == OK for i in inds), BAD: len(bad), UNK: sum(i["status"] == UNK for i in inds)}
    docs = result["documents"]
    dcount: dict = {}
    for d in docs:
        dcount[d["status"]] = dcount.get(d["status"], 0) + 1
    lines = []
    if not inds:
        status = "ПРОВЕРКА НЕ ВЫПОЛНЕНА"
        lines.append("Не удалось получить показатели отчета: " + ("; ".join(result["errors"]) or "причина не определена") + ".")
    elif bad:
        status = "ОТЧЕТ НЕДОСТОВЕРЕН"
        arts = [i for i in bad if i["role"] == "article"]
        tot = next((i for i in bad if i["role"] == "total"), None)
        amount = tot["diff"] if tot and tot["diff"] is not None else sum((i["diff"] for i in arts), Decimal(0))
        lines.append(f"Выявлено подтвержденных расхождений по статьям: {len(arts)}, общее отклонение итога {fmt(amount)} руб.")
        for i in arts:
            lines.append(f"{i['name']}: в отчете {fmt(i['report_value'])}, по документам {fmt(i['expected_value'])}, "
                         f"расхождение {fmt(i['diff'])} руб. ({i['category']}).")
    elif unk_main:
        status = "ДОСТОВЕРНОСТЬ ПОДТВЕРЖДЕНА ЧАСТИЧНО"
        lines.append("Расхождений не найдено, но часть основных показателей нельзя проверить: "
                     + ", ".join(i["name"] for i in unk_main) + ".")
    else:
        status = "ОТЧЕТ ДОСТОВЕРЕН"
        lines.append("Все статьи затрат и итог подтверждены первичными документами.")
    if inds:
        lines.append(f"Показателей: подтверждено {counts[OK]}, с расхождением {counts[BAD]}, не проверено {counts[UNK]}.")
        ok_main = [i["name"] for i in main if i["status"] == OK]
        if ok_main:
            lines.append("Подтверждены: " + "; ".join(ok_main) + ".")
        unk_sub = [i for i in inds if i["status"] == UNK and i["role"] not in ("article", "total")]
        if unk_sub:
            lines.append(f"Не проверены из-за недостатка данных детализирующие показатели: графы "
                         + ", ".join(str(i["column"]) for i in unk_sub) + ".")
    excl = [d for d in docs if d["status"] != S_COUNTED]
    if docs:
        lines.append(f"Документов: {len(docs)}, учтено {dcount.get(S_COUNTED, 0)}, исключено {len(excl)}"
                     + (": " + "; ".join(f"«{d['file']}» ({d['status']})" for d in excl) if excl else "") + ".")
    viol = [c for c in result["report_checks"] if c["status"] == "НАРУШЕНО"]
    if result["report_checks"]:
        lines.append(f"Внутренняя арифметика отчета: проверок {len(result['report_checks'])}, нарушений {len(viol)}.")
    result["verdict"] = {"status": status, "summary": lines, "counts": counts, "documents": dcount}
