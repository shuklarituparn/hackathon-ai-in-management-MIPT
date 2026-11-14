"""Модульные тесты: разбор сумм и дат, реквизиты, классификация, формулы XLSX. OCR не требуется."""
import json
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sverka import classify, config
from sverka.models import DocContent, Fragment, PrimaryDoc
from sverka.money import find_dates, fmt, parse_amount, words_to_amount
from sverka.readers import read_any, sniff
from sverka.report import _candidates, _one_digit_confusion
from sverka.requisites import extract, find_contracts, norm_contract, norm_number

KINDS = ["materials", "labor", "insurance", "prod_overhead", "gen_overhead"]
CODES = {"02": "labor", "03": "insurance", "08": "prod_overhead", "09": "gen_overhead"}


@pytest.mark.parametrize("raw,val", [
    ("1 145 798,10", "1145798.10"), ("245 000,00 руб.", "245000.00"), ("(12 500,00)", "-12500.00"),
    ("1,145,798.10", "1145798.10"), ("333000", "333000.00"), ("-5", "-5.00"), ("1 234,5", "1234.50"),
])
def test_parse_amount(raw, val):
    assert parse_amount(raw) == Decimal(val)


def test_fmt_and_words():
    assert fmt(Decimal("11580377.4")) == "11 580 377,40"
    assert words_to_amount("Стоимость: семьсот сорок четыре тысячи рублей 00 копеек.") == Decimal("744000.00")
    assert words_to_amount("Двенадцать тысяч пятьсот рублей 50 копеек") == Decimal("12500.50")
    assert words_to_amount("Услуги оказаны в полном объеме") is None


def test_dates():
    ds = [d for d, _, _ in find_dates('АКТ № 118 ОТ 28.01.2026, начало "16" августа 2025 г.')]
    assert ds == [date(2026, 1, 28), date(2025, 8, 16)]
    assert find_dates("договор 24/03/15-01") == []


def test_contracts():
    assert [c for c, _ in find_contracts("Основание: договор 24/03/15-01, этап 3.", "24/03/15-01")] == ["24/03/15-01"]
    assert [c for c, _ in find_contracts("по договору 24/03/16-02.", "24/03/15-01")] == ["24/03/16-02"]
    assert norm_contract("24/О3/15-О1") == "24/03/15-01"          # буквы О вместо нулей после OCR
    assert norm_number("АDМ-27") == norm_number("ADM-27")          # кириллица вместо латиницы
    assert find_contracts("Руководитель проекта Иванов, этап 3") == []


def _doc(lines, name="doc.docx"):
    dc = DocContent(path=name, name=name, fmt="docx")
    dc.fragments = [Fragment(t, {"type": "docx", "paragraph": i + 1}) for i, t in enumerate(lines)]
    return dc


def test_extract_act():
    d = extract(_doc(["АКТ № 118 ОТ 28.01.2026", "Основание: договор 24/03/15-01 от 09.04.2024, этап 3.",
                      "Содержание: эксплуатационное обслуживание лабораторного комплекса.",
                      "Стоимость оказанных услуг: 245 000,00 руб., в том числе НДС 40 833,33 руб.",
                      "Услуги оказаны в полном объеме за январь 2026 года."]), "24/03/15-01")
    assert (d.doc_type, d.number, d.doc_date, d.amount) == ("акт", "118", date(2026, 1, 28), Decimal("245000.00"))
    assert d.contracts == ["24/03/15-01"] and d.period == (date(2026, 1, 1), date(2026, 1, 31))
    assert d.evidence["amount"].text.startswith("Стоимость оказанных услуг")


def test_extract_total_and_words_mismatch():
    d = extract(_doc(["УПД № УПР-55", "Дата 29.05.2026", "Основание 24/03/15-01",
                      "Стоимость: семьсот сорок четыре тысячи рублей 00 копеек.", "Итого: 745 000,00 руб."]))
    assert d.amount == Decimal("745000.00") and d.amount_words == Decimal("744000.00")
    assert any("прописью" in w for w in d.warnings)


def test_extract_units_and_storno():
    d = extract(_doc(["Акт № 5 от 10.02.2026", "Договор: 24/03/15-01", "Сумма: 12,5 тыс. руб."]))
    assert d.amount == Decimal("12500.00")
    d = extract(_doc(["Акт № 6 от 10.02.2026", "Договор: 24/03/15-01", "Сторнирование ранее оказанных услуг.",
                      "Сумма: 10 000,00 руб."]))
    assert d.amount == Decimal("-10000.00")


@pytest.mark.parametrize("text,kind", [
    ("Техническое обслуживание испытательного стенда, обеспечение производственной деятельности", "prod_overhead"),
    ("Административное сопровождение, ведение документооборота, подготовка организационных материалов", "gen_overhead"),
    ("Расходы не относятся непосредственно к отдельной производственной операции и учитываются в составе "
     "общехозяйственного обеспечения. ИТ-инфраструктура и лицензии.", "gen_overhead"),
    ("Расчет страховых взносов. База начислений. Начислено взносов.", "insurance"),
])
def test_classify_rules(text, kind):
    d = PrimaryDoc(file="x", description=text)
    classify.classify(d, KINDS, CODES)
    assert d.kind == kind and d.kind_conf >= 0.5


def test_classify_by_code():
    from sverka.models import LineItem
    d = PrimaryDoc(file="x", description="ведомость", items=[LineItem("0201", "Основная", Decimal(1)),
                                                             LineItem("0202", "Доп.", Decimal(1))])
    classify.classify(d, KINDS, CODES)
    assert (d.kind, d.kind_source) == ("labor", "code")


def test_classify_local_llm(monkeypatch):
    """Неоднозначный документ уходит в локальную LLM (здесь ее имитирует HTTP-сервер)."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"choices": [{"message": {"content": '{"n": 4, "why": "обслуживание станка"}'}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "LLM_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    d = PrimaryDoc(file="x", description="Услуги по договору согласно приложению.")
    classify.classify(d, KINDS, CODES)
    srv.shutdown()
    assert (d.kind, d.kind_source) == ("prod_overhead", "llm")


def test_xlsx_formula_without_cached_value(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in (["РАСЧЕТНАЯ ВЕДОМОСТЬ ПО ОПЛАТЕ ТРУДА"], ["Документ: ВЕД-ЗП-2026-01"],
                ["Проект: 24/03/15-01 | Период: Январь 2026"], [],
                ["Код статьи", "Вид начисления", "Сумма, руб.", "Основание"],
                ["0201", "Основная заработная плата", 1010000, "Табель"],
                ["0202", "Дополнительная заработная плата и резерв отпусков", 100000, "Доплаты"],
                [None, "ИТОГО", "=SUM(C6:C7)", None]):
        ws.append(row)
    p = tmp_path / "ved.xlsx"
    wb.save(p)
    dc = read_any(str(p))
    assert any("ИТОГО | 1110000.00" in f.text for f in dc.fragments)      # формула вычислена системой
    d = extract(dc, "24/03/15-01")
    assert d.amount == Decimal("1110000.00") and d.number == "ВЕД-ЗП-2026-01"
    assert [it.code for it in d.items] == ["0201", "0202"] and d.contracts == ["24/03/15-01"]
    assert d.period == (date(2026, 1, 1), date(2026, 1, 31))


def test_broken_files_do_not_crash(tmp_path):
    cases = {"broken.pdf": b"%PDF-1.4\n" + b"\x00" * 50, "empty.pdf": b"", "fake.docx": b"not a zip",
             "junk.bin": b"\x01\x02\x03"}
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        dc = read_any(str(p))
        assert dc.errors and not dc.fragments, name
    assert sniff(str(tmp_path / "empty.pdf")) == "empty"


def test_report_cell_voting():
    assert _candidates(["5 558 474,83", "5558474,83", "555847483", "3 558 474,83"])[0] == (Decimal("5558474.83"), 3)
    assert _candidates(["", "abc"]) == []
    assert _one_digit_confusion(Decimal("5398463.60"), Decimal("5398468.60"))
    assert not _one_digit_confusion(Decimal("100.00"), Decimal("900.00"))
