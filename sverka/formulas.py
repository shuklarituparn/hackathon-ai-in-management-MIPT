"""Decimal evaluation of simple spreadsheet formulas (T04).

Preparation keeps what a workbook says and, separately, what our code derives from it:

* ``formula`` is the text stored in the cell, ``cached_value`` the result the file stored (if any);
  ``derived_value`` is computed here from the input cells with ``Decimal`` and is never copied from the cache.
* Supported: numbers, cell references (also ``'Other sheet'!B2``), ranges inside functions, ``+ - * / ^``,
  unary signs, ``%``, parentheses and ``SUM``/``СУММ``, ``ROUND``/``ОКРУГЛ`` (half away from zero, as Excel),
  ``ABS``, ``MIN``/``МИН``, ``MAX``/``МАКС``. ``;`` and ``,`` both separate arguments.
* Everything else stays ``unresolved`` with a reason and no value: other workbooks, missing sheets, defined
  names, unsupported functions, text operations, comparisons, dates, circular references and any formula
  that depends on an unresolved cell. Nothing is guessed.
* Spreadsheet errors (``#DIV/0!``, an input cell holding ``#N/A``, text in arithmetic) give ``error``.
* ``SUM``, ``MIN`` and ``MAX`` skip blank cells, text and booleans in ranges, as Excel does. A blank cell
  referenced directly in arithmetic (``=C9+1``) is an unknown dependency: Excel would read 0, but an empty
  input may be a lost value, so the result stays ``unresolved``.

A derived value keeps every digit. When the cell's number format shows more decimals than the exact result
has, the result is padded to that scale (``1110000`` shown as ``#,##0.00`` becomes ``1110000.00``); it is
never rounded to the format.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any, Literal

__all__ = [
    "Evaluation",
    "WorkbookEvaluator",
    "agrees_with_cached",
    "display_scale",
    "is_formula",
    "formula_text",
    "to_decimal",
]

MAX_DEPTH = 50
MAX_RANGE_CELLS = 100_000
ERROR_LITERALS = ("#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA", "#SPILL!")
_NUMERIC_TEXT = re.compile(r"^\s*[-+]?(\d+(\.\d*)?|\.\d+)\s*$")


@dataclass(frozen=True)
class Evaluation:
    """Result of evaluating one formula cell."""

    state: Literal["derived", "unresolved", "error"]
    value: Decimal | None = None
    reason: str | None = None  # why it is unresolved, or the spreadsheet error and its cause


class _Unresolved(Exception):
    pass


class _SheetError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def is_formula(raw: Any) -> bool:
    """A cell value that holds a formula (plain text starting with ``=``, or an openpyxl array formula)."""
    return (isinstance(raw, str) and raw.startswith("=") and len(raw) > 1) or _special_formula_text(raw) is not None


def formula_text(raw: Any) -> str:
    special = _special_formula_text(raw)
    return special if special is not None else str(raw)


def _special_formula_text(raw: Any) -> str | None:
    """Array and data-table formulas arrive as objects, not strings."""
    text = getattr(raw, "text", None)
    if type(raw).__name__ in ("ArrayFormula", "DataTableFormula"):
        return text if isinstance(text, str) and text else "=" + type(raw).__name__
    return None


def to_decimal(value: int | float | Decimal) -> Decimal:
    """Exact Decimal for a stored number; floats use their shortest repr (0.1 stays 0.1)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(repr(value))


# Excel computes in binary doubles and stores up to 17 significant digits, so a cached result differs from the
# exact Decimal one in its last digits (0.1 + 0.2 is stored as 0.30000000000000004). A difference of at most
# one millionth, or of a 1e-14 share of the value for very large numbers, is that noise, not a disagreement.
_FLOAT_NOISE = Decimal("0.000001")
_FLOAT_NOISE_SHARE = Decimal("1e-14")


def agrees_with_cached(cached: int | float | Decimal, derived: Decimal) -> bool:
    """Whether the number a file cached for a formula is the derived value, up to binary floating point."""
    stored = to_decimal(cached)
    diff = abs(stored - derived)
    return diff <= _FLOAT_NOISE or diff <= max(abs(stored), abs(derived)) * _FLOAT_NOISE_SHARE


def display_scale(number_format: str | None) -> int | None:
    """Decimals a number format shows (``#,##0.00`` -> 2), or None for General, percent and text formats."""
    if not number_format or number_format.lower() == "general":
        return None
    section = number_format.split(";", 1)[0]
    section = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", section)
    if "%" in section or "@" in section or not re.search(r"[0#]", section):
        return None
    if "." not in section:
        return 0
    return len(re.match(r"[0#?]*", section.split(".", 1)[1]).group(0))


def _plain(value: Decimal) -> Decimal:
    """No exponent notation and no negative zero, so the value prints as an ordinary decimal string."""
    if value == 0:
        return abs(value)
    if value.as_tuple().exponent > 0:
        return value.quantize(Decimal(1))
    return value


def _pad(value: Decimal, scale: int | None) -> Decimal:
    if scale is None or scale <= 0 or -value.as_tuple().exponent >= scale:
        return value
    return value.quantize(Decimal(1).scaleb(-scale))


# ======================= tokens =======================

_SHEET = r"(?:'(?P<qsheet>(?:[^']|'')+)'|(?P<sheet>[A-Za-z_А-Яа-яЁё][\w.А-Яа-яЁё]*))!"
_SHEET_ANY = r"(?:'(?:[^']|'')+'|[A-Za-z_А-Яа-яЁё][\w.А-Яа-яЁё]*)!"
_CELL = r"\$?[A-Za-z]{1,3}\$?[0-9]+"
_TOKEN = re.compile(
    rf"""
    (?P<ws>\s+)
    |(?P<external>\[[^\]]*\])
    |(?P<string>"(?:[^"]|"")*")
    |(?P<error>\#(?:NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|GETTING_DATA|SPILL!))
    |(?P<ref>(?:{_SHEET})?(?P<cells>{_CELL}(?::{_CELL})?))(?![\w(])
    |(?P<colrange>(?:{_SHEET_ANY})?(?:\$?[A-Za-z]{{1,3}}:\$?[A-Za-z]{{1,3}}|\$?[0-9]+:\$?[0-9]+))
    |(?P<func>[A-Za-z_А-Яа-яЁё][\w.А-Яа-яЁё]*)\s*\(
    |(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
    |(?P<name>[A-Za-z_А-Яа-яЁё][\w.А-Яа-яЁё]*!?)
    |(?P<op><>|<=|>=|[-+*/^%&=<>(),;:])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    sheet: str | None = None


def _tokenize(expr: str) -> list[_Tok]:
    out: list[_Tok] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if m is None:
            raise _Unresolved(f"cannot read the formula near {expr[pos : pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        if kind in ("ref", "colrange"):
            if kind == "colrange":
                raise _Unresolved(f"whole-row or whole-column reference {m.group(0)!r} is not supported")
            sheet = m.group("qsheet")
            sheet = sheet.replace("''", "'") if sheet is not None else m.group("sheet")
            out.append(_Tok("ref", m.group("cells").replace("$", "").upper(), sheet))
        elif kind == "func":
            out.append(_Tok("func", m.group("func").upper()))
        else:
            out.append(_Tok(kind, m.group(0)))
    return out


# ======================= values =======================


@dataclass(frozen=True)
class _Range:
    sheet: str
    ref: str


_Value = Decimal | bool | _Range


class WorkbookEvaluator:
    """Evaluates formula cells of one workbook; results are memoized per cell.

    ``formulas`` is the workbook loaded with ``data_only=False`` (formulas as text). Cached values are not
    read here: preparation keeps them separately.
    """

    def __init__(self, formulas: Any) -> None:
        self.wb = formulas
        self._memo: dict[tuple[str, str], Evaluation] = {}
        self._stack: list[tuple[str, str]] = []

    # ---------- public ----------

    def evaluate(self, sheet: str, ref: str) -> Evaluation:
        """Evaluate the formula in ``sheet!ref``. A cell without a formula is an unresolved request."""
        try:
            ws = self._sheet(sheet)
        except _Unresolved as exc:
            return Evaluation("unresolved", reason=str(exc))
        key = (ws.title, ref.upper())
        if key in self._memo:
            return self._memo[key]
        raw = ws[key[1]].value
        if not is_formula(raw):
            return Evaluation("unresolved", reason=f"{sheet}!{ref} holds no formula")
        if key in self._stack:
            return Evaluation("unresolved", reason="circular reference")
        if len(self._stack) >= MAX_DEPTH:
            return Evaluation("unresolved", reason="formula dependencies are nested too deeply")
        self._stack.append(key)
        try:
            result = self._evaluate_text(ws.title, formula_text(raw), ws[key[1]].number_format)
        finally:
            self._stack.pop()
        if "too deeply" not in (result.reason or ""):  # depth depends on where evaluation started
            self._memo[key] = result
        return result

    # ---------- internals ----------

    def _sheet(self, name: str):
        if name in self.wb.sheetnames:
            return self.wb[name]
        for title in self.wb.sheetnames:  # sheet names are case-insensitive in Excel
            if title.lower() == name.lower():
                return self.wb[title]
        raise _Unresolved(f"unknown sheet {name!r}")

    def _evaluate_text(self, sheet: str, text: str, number_format: str | None) -> Evaluation:
        if _special_formula_text(text) is not None or not text.startswith("="):
            return Evaluation("unresolved", reason="array and data-table formulas are not evaluated")
        try:
            with localcontext() as ctx:
                ctx.prec = 34
                ctx.traps[DivisionByZero] = True
                ctx.traps[InvalidOperation] = True
                parser = _Parser(_tokenize(text[1:]), sheet, self)
                value = parser.parse()
                self._shown_value_is_number(value)
                number = self._number(value, direct=True)
            return Evaluation("derived", _pad(_plain(number), display_scale(number_format)))
        except _SheetError as exc:
            return Evaluation("error", reason=str(exc))
        except _Unresolved as exc:
            return Evaluation("unresolved", reason=str(exc))
        except DivisionByZero:
            return Evaluation("error", reason="#DIV/0!: division by zero")
        except InvalidOperation:
            return Evaluation("error", reason="#NUM!: the result cannot be represented")
        except RecursionError:
            return Evaluation("unresolved", reason="the formula is nested too deeply")

    def _shown_value_is_number(self, value: _Value) -> None:
        """A formula that only shows another cell (``=A1``) shows its text or boolean, not a number."""
        if isinstance(value, bool):
            raise _Unresolved("the formula shows a boolean, not a number")
        if not isinstance(value, _Range):
            return
        cells = self._cells(value)
        if len(cells) != 1:
            return
        raw = self._sheet(value.sheet)[cells[0]].value
        if isinstance(raw, bool):
            raise _Unresolved(f"the formula shows the boolean in {value.sheet}!{cells[0]}, not a number")
        if isinstance(raw, str) and not is_formula(raw):
            text = raw.strip()
            if text and text not in ERROR_LITERALS and not _NUMERIC_TEXT.match(text):
                raise _Unresolved(f"the formula shows the text in {value.sheet}!{cells[0]}, not a number")

    def _number(self, value: _Value, *, direct: bool) -> Decimal:
        if isinstance(value, _Range):
            cells = self._cells(value)
            if len(cells) != 1:
                raise _SheetError("#VALUE!", f"range {value.sheet}!{value.ref} used as a single value")
            return self._cell_number(value.sheet, cells[0], direct=True)
        if isinstance(value, bool):
            return Decimal(int(value))
        return value

    def _cells(self, rng: _Range) -> list[str]:
        from openpyxl.utils import get_column_letter, range_boundaries

        try:
            min_col, min_row, max_col, max_row = range_boundaries(rng.ref)
        except ValueError:
            raise _Unresolved(f"invalid reference {rng.ref!r}") from None
        count = (max_col - min_col + 1) * (max_row - min_row + 1)
        if count > MAX_RANGE_CELLS:
            raise _Unresolved(f"range {rng.ref} is too large to evaluate")
        return [
            f"{get_column_letter(col)}{row}"
            for row in range(min_row, max_row + 1)
            for col in range(min_col, max_col + 1)
        ]

    def _cell_number(self, sheet: str, ref: str, *, direct: bool) -> Decimal | None:
        """Number held by a cell. ``direct`` reads follow arithmetic rules; range reads skip non-numbers (None)."""
        ws = self._sheet(sheet)
        raw = ws[ref].value
        if is_formula(raw):
            result = self.evaluate(ws.title, ref)
            if result.state == "derived":
                return result.value
            if result.state == "error":
                raise _SheetError(_error_code(result.reason), f"{ws.title}!{ref}: {result.reason}")
            raise _Unresolved(f"depends on {ws.title}!{ref}, which is unresolved ({result.reason})")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if direct:  # Excel reads 0 here; an empty input may be a lost value, so it stays unknown
                raise _Unresolved(f"depends on {ws.title}!{ref}, which is empty")
            return None
        if isinstance(raw, bool):
            return Decimal(int(raw)) if direct else None
        if isinstance(raw, (int, float, Decimal)):
            return to_decimal(raw)
        if isinstance(raw, (datetime, date, time, timedelta)):
            raise _Unresolved(f"{ws.title}!{ref} holds a date or time; date arithmetic is not supported")
        if isinstance(raw, str):
            text = raw.strip()
            if text in ERROR_LITERALS:
                raise _SheetError(text, f"{ws.title}!{ref} holds {text}")
            if not direct:
                return None
            if _NUMERIC_TEXT.match(text):
                return Decimal(text)
            raise _SheetError("#VALUE!", f"{ws.title}!{ref} holds text {text[:40]!r}, not a number")
        raise _Unresolved(f"{ws.title}!{ref} holds an unsupported value ({type(raw).__name__})")

    def _range_numbers(self, rng: _Range) -> list[Decimal]:
        out = []
        for ref in self._cells(rng):
            number = self._cell_number(rng.sheet, ref, direct=False)
            if number is not None:
                out.append(number)
        return out


def _error_code(reason: str | None) -> str:
    head = (reason or "").split(":", 1)[0]
    return head if head in ERROR_LITERALS else "#VALUE!"


# ======================= functions =======================


def _sum(ev: WorkbookEvaluator, args: list[_Value]) -> Decimal:
    total = Decimal(0)
    for arg in args:
        if isinstance(arg, _Range):
            total += sum(ev._range_numbers(arg), Decimal(0))
        else:
            total += ev._number(arg, direct=True)
    return total


def _numbers(ev: WorkbookEvaluator, args: list[_Value]) -> list[Decimal]:
    out: list[Decimal] = []
    for arg in args:
        out.extend(ev._range_numbers(arg) if isinstance(arg, _Range) else [ev._number(arg, direct=True)])
    return out


def _round(ev: WorkbookEvaluator, args: list[_Value]) -> Decimal:
    if len(args) != 2:
        raise _SheetError("#VALUE!", "ROUND takes a number and a number of digits")
    value, digits = ev._number(args[0], direct=True), ev._number(args[1], direct=True)
    if digits != digits.to_integral_value():
        digits = digits.to_integral_value(rounding=ROUND_DOWN)
    return value.quantize(Decimal(1).scaleb(-int(digits)), rounding=ROUND_HALF_UP)


def _abs(ev: WorkbookEvaluator, args: list[_Value]) -> Decimal:
    if len(args) != 1:
        raise _SheetError("#VALUE!", "ABS takes one number")
    return abs(ev._number(args[0], direct=True))


def _min(ev: WorkbookEvaluator, args: list[_Value]) -> Decimal:
    numbers = _numbers(ev, args)
    return min(numbers) if numbers else Decimal(0)


def _max(ev: WorkbookEvaluator, args: list[_Value]) -> Decimal:
    numbers = _numbers(ev, args)
    return max(numbers) if numbers else Decimal(0)


_FUNCTIONS: dict[str, Callable[[WorkbookEvaluator, list[_Value]], Decimal]] = {
    "SUM": _sum,
    "СУММ": _sum,
    "ROUND": _round,
    "ОКРУГЛ": _round,
    "ABS": _abs,
    "MIN": _min,
    "МИН": _min,
    "MAX": _max,
    "МАКС": _max,
}


# ======================= parser =======================


class _Parser:
    """Recursive descent over Excel precedence: + - < * / < ^ < % < unary sign < primary."""

    def __init__(self, tokens: list[_Tok], sheet: str, ev: WorkbookEvaluator) -> None:
        self.toks = tokens
        self.i = 0
        self.sheet = sheet
        self.ev = ev

    def parse(self) -> _Value:
        if not self.toks:
            raise _Unresolved("empty formula")
        value = self._additive()
        if self.i != len(self.toks):
            tok = self.toks[self.i]
            if tok.text in ("=", "<>", "<", ">", "<=", ">="):
                raise _Unresolved("comparisons are not supported")
            if tok.text == "&":
                raise _Unresolved("text concatenation is not supported")
            raise _Unresolved(f"unexpected {tok.text!r} in the formula")
        return value

    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self, text: str) -> bool:
        tok = self._peek()
        if tok is not None and tok.kind == "op" and tok.text == text:
            self.i += 1
            return True
        return False

    def _num(self, value: _Value) -> Decimal:
        return self.ev._number(value, direct=True)

    def _additive(self) -> _Value:
        value = self._multiplicative()
        while True:
            if self._take("+"):
                value = self._num(value) + self._num(self._multiplicative())
            elif self._take("-"):
                value = self._num(value) - self._num(self._multiplicative())
            else:
                return value

    def _multiplicative(self) -> _Value:
        value = self._power()
        while True:
            if self._take("*"):
                value = self._num(value) * self._num(self._power())
            elif self._take("/"):
                divisor = self._num(self._power())
                if divisor == 0:
                    raise _SheetError("#DIV/0!", "division by zero")
                value = self._num(value) / divisor
            else:
                return value

    def _power(self) -> _Value:
        value = self._percent()
        while self._take("^"):
            exponent = self._num(self._percent())
            if exponent != exponent.to_integral_value() or abs(exponent) > 100:
                raise _Unresolved("only small whole-number powers are supported")
            base = self._num(value)
            if base == 0 and exponent < 0:
                raise _SheetError("#DIV/0!", "zero to a negative power")
            value = base ** int(exponent)
        return value

    def _percent(self) -> _Value:
        value = self._unary()
        while self._take("%"):
            value = self._num(value) / 100
        return value

    def _unary(self) -> _Value:
        if self._take("-"):
            return -self._num(self._unary())
        if self._take("+"):
            return self._num(self._unary())
        return self._primary()

    def _primary(self) -> _Value:
        tok = self._peek()
        if tok is None:
            raise _Unresolved("the formula ends unexpectedly")
        self.i += 1
        if tok.kind == "number":
            return Decimal(tok.text)
        if tok.kind == "ref":
            sheet = tok.sheet if tok.sheet is not None else self.sheet
            self.ev._sheet(sheet)  # an unknown sheet is an unknown dependency
            return _Range(self.ev._sheet(sheet).title, tok.text)
        if tok.kind == "func":
            return self._call(tok.text)
        if tok.kind == "op" and tok.text == "(":
            value = self._additive()
            if not self._take(")"):
                raise _Unresolved("unbalanced parentheses")
            return value
        if tok.kind == "error":
            raise _SheetError(tok.text, f"the formula contains {tok.text}")
        if tok.kind == "string":
            raise _Unresolved("text values in formulas are not supported")
        if tok.kind == "external":
            raise _Unresolved(f"reference to another workbook {tok.text}")
        if tok.kind == "name":
            upper = tok.text.upper()
            if upper in ("TRUE", "ИСТИНА"):
                return True
            if upper in ("FALSE", "ЛОЖЬ"):
                return False
            raise _Unresolved(f"defined name or unknown reference {tok.text!r}")
        raise _Unresolved(f"unexpected {tok.text!r} in the formula")

    def _call(self, name: str) -> Decimal:
        fn = _FUNCTIONS.get(name)
        if fn is None:
            raise _Unresolved(f"function {name} is not supported")
        args: list[_Value] = []
        if not self._take(")"):
            while True:
                args.append(self._additive())
                if self._take(")"):
                    break
                if not (self._take(",") or self._take(";")):
                    raise _Unresolved(f"cannot read the arguments of {name}")
        return fn(self.ev, args)
