"""Структуры данных."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass
class Fragment:
    """Фрагмент документа: строка текста или строка таблицы с адресом в источнике."""
    text: str
    loc: dict                      # где находится: страница+bbox, лист+ячейки, абзац, строка таблицы
    cells: list | None = None      # значения ячеек для табличных строк
    refs: list | None = None       # адреса ячеек (A1) для xlsx
    conf: float | None = None      # уверенность OCR, 0..100

    def where(self) -> str:
        l = self.loc
        t = l.get("type")
        if t == "xlsx":
            return f"лист «{l.get('sheet')}», ячейки {l.get('range')}"
        if t == "docx":
            if "table" in l:
                return f"таблица {l['table']}, строка {l['row']}"
            return f"абзац {l.get('paragraph')}"
        if t in ("pdf", "scan"):
            b = l.get("bbox")
            pos = f", область x={int(b[0])}..{int(b[2])}, y={int(b[1])}..{int(b[3])}" if b else ""
            kind = "скан" if t == "scan" else "PDF"
            return f"{kind}, стр. {l.get('page', 1)}{pos}"
        if t == "txt":
            return f"строка {l.get('line')}"
        return ""


@dataclass
class DocContent:
    """Содержимое файла после чтения."""
    path: str
    name: str
    fmt: str = "unknown"           # pdf | pdf-scan | image | docx | xlsx | txt | unknown
    fragments: list[Fragment] = field(default_factory=list)
    ocr: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_images: dict = field(default_factory=dict)   # номер страницы -> PIL.Image (для сканов)
    page_rotations: dict = field(default_factory=dict)  # номер страницы -> поворот по часовой (0/90/180/270)

    @property
    def text(self) -> str:
        return "\n".join(f.text for f in self.fragments)


@dataclass
class Evidence:
    """Подтверждающий фрагмент первичного документа."""
    file: str
    what: str            # какое значение подтверждает
    text: str            # текст фрагмента
    where: str           # человекочитаемый адрес
    loc: dict = field(default_factory=dict)
    image: str | None = None   # имя файла с вырезкой (png)


@dataclass
class LineItem:
    code: str | None
    label: str
    amount: Decimal
    evidence: Evidence | None = None


@dataclass
class PrimaryDoc:
    file: str
    fmt: str = ""
    doc_type: str = ""
    number: str = ""
    doc_date: date | None = None
    period: tuple | None = None        # (date, date) период оказания услуг / начисления
    contracts: list[str] = field(default_factory=list)
    amount: Decimal | None = None
    amount_words: Decimal | None = None
    base_amount: Decimal | None = None  # база начислений (для расчетов взносов)
    items: list[LineItem] = field(default_factory=list)
    description: str = ""
    kind: str | None = None            # вид затрат (labor, insurance, ...)
    kind_conf: float = 0.0
    kind_reason: str = ""
    kind_source: str = ""              # code | rules | llm
    status: str = "не обработан"       # учтен | дубликат | вне периода | другой договор | ...
    reason: str = ""
    duplicate_of: str | None = None
    evidence: dict[str, Evidence] = field(default_factory=dict)   # number/date/contract/amount/period
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ocr: bool = False
    ocr_conf: float | None = None


@dataclass
class ReportCell:
    col: int
    value: Decimal | None
    raw: str = ""
    variants: list[str] = field(default_factory=list)
    bbox: list | None = None
    reliable: bool = True
    repaired: bool = False
    note: str = ""


@dataclass
class ReportRow:
    label: str
    cells: dict[int, ReportCell] = field(default_factory=dict)
    bbox: list | None = None
    kind: str = "data"    # data | total | reference

    def val(self, col: int) -> Decimal | None:
        c = self.cells.get(col)
        return c.value if c else None


@dataclass
class ReportColumn:
    num: int
    key: str
    title: str
    role: str = "sub"            # article | sub | total | info
    kind: str | None = None      # вид затрат для статей
    code: str | None = None      # код статьи калькуляции
    unit: str = "руб."


@dataclass
class Report:
    file: str
    title: str = ""
    header_text: str = ""
    contract: str | None = None
    period_text: str = ""
    period: tuple | None = None
    columns: list[ReportColumn] = field(default_factory=list)
    rows: list[ReportRow] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    schema: str = ""
    source: str = ""             # ocr | text
    image: Any = None            # PIL.Image страницы отчета
    meta: dict = field(default_factory=dict)

    def col(self, num: int) -> ReportColumn | None:
        return next((c for c in self.columns if c.num == num), None)
