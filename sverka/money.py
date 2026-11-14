"""Разбор денежных сумм, дат и числительных."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SP = "    "
CENT = Decimal("0.01")

# Число: группы по три цифры через пробел или сплошное, необязательная дробная часть.
NUM_RE = re.compile(
    r"(?<![\w/.,-])[-−–]?\(?(?:\d{1,3}(?:[    ]\d{3})+|\d+)(?:[.,]\d{1,2})?\)?(?![\w/]|[.,-]\d)"
)
CUR_RE = re.compile(r"^\s*(?:тыс\.?\s*|млн\.?\s*)?(?:руб|р\.|₽|py6|рy6|pуб)", re.I)


def q(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(s) -> Decimal | None:
    """'1 145 798,10' -> Decimal('1145798.10'). Скобки и минус дают отрицательное значение."""
    if s is None:
        return None
    if isinstance(s, (int, float, Decimal)) and not isinstance(s, bool):
        try:
            return q(Decimal(str(s)))
        except InvalidOperation:
            return None
    t = str(s).strip()
    if not t:
        return None
    neg = t.startswith(("-", "−", "–")) or (t.startswith("(") and t.endswith(")"))
    t = re.sub(r"[^\d.,]", "", t).strip(".,")
    if not t or not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        dec = max(t.rfind(","), t.rfind("."))
        t = re.sub(r"[.,]", "", t[:dec]) + "." + t[dec + 1:]
    elif t.count(",") + t.count(".") > 1:
        t = re.sub(r"[.,]", "", t)
    else:
        m = re.search(r"[.,](\d+)$", t)
        if m and len(m.group(1)) == 3:  # 1.234 или 1,234 это разделитель тысяч
            t = re.sub(r"[.,]", "", t)
        else:
            t = t.replace(",", ".")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return q(-d if neg else d)


def fmt(d) -> str:
    """Decimal -> '1 145 798,10'."""
    if d is None:
        return ""
    d = q(Decimal(d))
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"{sign}{' '.join(groups)},{frac}"


def find_amounts(line: str):
    """Все числа в строке: (значение, начало, конец, есть_валюта, есть_копейки, множитель)."""
    out = []
    for m in NUM_RE.finditer(line):
        raw = m.group(0)
        val = parse_amount(raw)
        if val is None:
            continue
        tail = line[m.end(): m.end() + 14]
        has_cur = bool(CUR_RE.match(tail))
        mult = 1
        tl = tail.lower().lstrip()
        if tl.startswith("тыс"):
            mult = 1000
        elif tl.startswith("млн"):
            mult = 1000000
        has_dec = bool(re.search(r"[.,]\d{2}\)?$", raw))
        out.append((q(val * mult), m.start(), m.end(), has_cur, has_dec, mult))
    return out


# ---------- даты ----------
MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
MONTH_NAMES = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль",
               "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTH_RE = (r"(январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?|"
            r"сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе])")
DATE_RE = re.compile(r"(?<![\d/.-])(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?![\d/-])")
DATE_TXT_RE = re.compile(r"[«\"']?(\d{1,2})[»\"']?\s+" + MONTH_RE + r"\s+(\d{4})", re.I)
MONTH_YEAR_RE = re.compile(r"(?<![а-яё])" + MONTH_RE + r"\s+(\d{4})", re.I)


def month_num(word: str) -> int | None:
    w = word.lower()
    for stem, n in MONTHS.items():
        if stem == "ма":
            if re.fullmatch(r"ма[йяе]", w):
                return 5
        elif w.startswith(stem):
            return n
    return None


def mk_date(d, m, y) -> date | None:
    try:
        y = int(y)
        if y < 100:
            y += 2000
        return date(y, int(m), int(d))
    except (ValueError, TypeError):
        return None


def find_dates(text: str):
    """Все даты в тексте: (date, начало, конец)."""
    out = []
    for m in DATE_RE.finditer(text):
        d = mk_date(m.group(1), m.group(2), m.group(3))
        if d and 1990 < d.year < 2100:
            out.append((d, m.start(), m.end()))
    for m in DATE_TXT_RE.finditer(text):
        mn = month_num(m.group(2))
        d = mk_date(m.group(1), mn, m.group(3)) if mn else None
        if d:
            out.append((d, m.start(), m.end()))
    out.sort(key=lambda x: x[1])
    return out


def month_range(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    from datetime import timedelta
    return start, end - timedelta(days=1)


def months_between(start: date, end: date):
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


# ---------- сумма прописью ----------
_UNITS = {"ноль": 0, "один": 1, "одна": 1, "одного": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
          "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
          "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
          "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
          "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
          "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "двести": 200,
          "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600, "семьсот": 700,
          "восемьсот": 800, "девятьсот": 900}
_SCALES = (("миллиард", 10 ** 9), ("миллион", 10 ** 6), ("тысяч", 10 ** 3))


def words_to_amount(text: str) -> Decimal | None:
    """'семьсот сорок четыре тысячи рублей 00 копеек' -> 744000.00. None, если прописи нет."""
    tokens = re.findall(r"[а-яё]+|\d+", text.lower())
    best = None
    i = 0
    while i < len(tokens):
        total, cur, used, j = 0, 0, 0, i
        while j < len(tokens):
            t = tokens[j]
            if t in _UNITS:
                cur += _UNITS[t]
            else:
                sc = next((v for k, v in _SCALES if t.startswith(k)), None)
                if sc is None:
                    break
                total += (cur or 1) * sc
                cur = 0
            used += 1
            j += 1
        if used and j < len(tokens) and tokens[j].startswith("рубл"):
            val = Decimal(total + cur)
            if j + 2 < len(tokens) and tokens[j + 1].isdigit() and tokens[j + 2].startswith("копе"):
                val += Decimal(int(tokens[j + 1])) / 100
            if used >= 2 or val >= 100:
                best = q(val)
        i = max(j, i + 1)
    return best
