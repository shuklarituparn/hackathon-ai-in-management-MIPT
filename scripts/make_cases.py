"""Генератор проверочных наборов на основе examples/case19.

Каждый набор меняет одно условие (нет документа, поврежденный файл, лишний дубль и т.д.)
и содержит ожидаемые статусы основных показателей. Используется тестами и для демонстрации:

    python scripts/make_cases.py ./cases
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "examples", "case19")
OK, BAD, UNK = "ОК", "РАСХОЖДЕНИЕ", "НЕПРОВЕРЕНО"
BASELINE = {5: OK, 10: OK, 15: BAD, 16: OK, 17: BAD}

ROW_2026 = [34.14, 0, 5558474.83, 5248019.04, 5054470.26, 193548.78, 310455.79, 1671359.96, 1578126.37,
            1526450.09, 51676.28, 93233.59, 1145798.10, 3204744.51, 11580377.40]
ROW_2025 = [0.54, 0, 152284.98, 150449.56, 53521.17, 96928.39, 1835.42, 37272.94, 37269.26, 16163.39,
            21105.87, 3.68, 52433.87, 71932.26, 313924.05]


def _copy(dst: str, skip=()) -> None:
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(BASE):
        if not any(n.startswith(s) for s in skip):
            shutil.copy(os.path.join(BASE, n), os.path.join(dst, n))


def _docx(path: str, lines: list[str]) -> None:
    import docx
    d = docx.Document()
    for l in lines:
        d.add_paragraph(l)
    d.save(path)


def _xlsx_report(path: str, row26: list[float], wide: bool = True) -> None:
    """КФЗ в формате XLSX: полная форма на 17 граф или сокращенная на 7 граф."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["КАРТОЧКА УЧЕТА ФАКТИЧЕСКИХ ЗАТРАТ № 3/1"])
    ws.append(["Август 2025 г. - Май 2026 г."])
    ws.append(["контракт № 24/03/15-01 от 09.04.2024г."])
    if wide:
        ws.append(["Этап", "Месяц", "Трудоемкость", "Материальные затраты",
                   "Затраты на оплату труда - всего (Статья калькуляции 0200)", "", "", "", "",
                   "Страховые взносы (Статья калькуляции 0300)", "", "", "", "",
                   "Общепроизводственные затраты (Статья калькуляции 0800)",
                   "Общехозяйственные затраты (Статья калькуляции 0900)", "Итого"])
        ws.append(list(range(1, 18)))
        r25, r26 = ROW_2025, row26
    else:
        ws.append(["Этап", "Месяц", "Затраты на оплату труда (0200)", "Страховые взносы (0300)",
                   "Общепроизводственные затраты (0800)", "Общехозяйственные затраты (0900)", "Итого"])
        ws.append(list(range(1, 8)))
        pick = lambda r: [r[2], r[7], r[12], r[13], round(r[2] + r[7] + r[12] + r[13], 2)]
        r25, r26 = pick(ROW_2025), pick(row26)
    ws.append(["3 этап, 2025 г.:", ""] + r25)
    ws.append(["3 этап, 2026 г.:", ""] + r26)
    ws.append(["ВСЕГО 3 этап:", ""] + [round(a + b, 2) for a, b in zip(r25, r26)])
    wb.save(path)


def build(out: str) -> dict[str, dict]:
    """Создает наборы в каталоге out. Возвращает {имя: {"path", "expect", "verdict", "docs"}}."""
    cases: dict[str, dict] = {}

    def case(name, expect, verdict, docs=None, path=None):
        cases[name] = {"path": path or os.path.join(out, name), "expect": expect, "verdict": verdict,
                       "docs": docs or {}}
        return os.path.join(out, name)

    d = case("01_baseline", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН",
             {"10_Акт_ADM-27_копия.pdf": "дубликат", "14_Акт_ОРГ-0603.pdf": "вне периода",
              "15_Акт_ADM-75_15-05-2026.pdf": "другой договор"})
    _copy(d)

    d = case("02_no_readme", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН")
    _copy(d, skip=("00_",))

    d = case("03_missing_month", {5: UNK, 10: OK, 15: BAD, 16: OK}, "ОТЧЕТ НЕДОСТОВЕРЕН")
    _copy(d, skip=("02_03",))

    d = case("04_corrupt_file", {5: OK, 10: OK, 15: UNK, 16: OK, 17: UNK}, "ДОСТОВЕРНОСТЬ ПОДТВЕРЖДЕНА ЧАСТИЧНО",
             {"07_УПД_0426-77.pdf": "ошибка чтения"})
    _copy(d)
    with open(os.path.join(d, "07_УПД_0426-77.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n" + os.urandom(300))

    d = case("05_extra_duplicate", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН", {"13_УПД_УПР-55 (1).pdf": "дубликат"})
    _copy(d)
    shutil.copy(os.path.join(BASE, "13_УПД_УПР-55.pdf"), os.path.join(d, "13_УПД_УПР-55 (1).pdf"))

    d = case("06_other_period_doc", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН", {"16_Акт_АС-77.docx": "вне периода"})
    _copy(d)
    _docx(os.path.join(d, "16_Акт_АС-77.docx"),
          ["АКТ № АС-77", "Дата: 15.12.2025", "Договор: 24/03/15-01", "Исполнитель: ООО «ДокКонтур Эпсилон»",
           "Административное сопровождение и документооборот за декабрь 2025 года.", "Сумма: 50 000,00 руб."])

    d = case("07_discrepancy_closed", {5: OK, 10: OK, 15: OK, 16: OK, 17: OK}, "ОТЧЕТ ДОСТОВЕРЕН")
    _copy(d)
    _docx(os.path.join(d, "16_Акт_ПС-201.docx"),
          ["АКТ № ПС-201 от 16.03.2026", "Основание: договор 24/03/15-01, этап 3.",
           "Исполнитель: ООО «ЛабСервис Альфа»",
           "Поверка измерительных каналов испытательного стенда производственного назначения.",
           "Стоимость оказанных услуг: 12 500,00 руб."])

    d = case("08_no_contract_doc", {5: OK, 10: OK, 15: UNK, 16: OK}, "ДОСТОВЕРНОСТЬ ПОДТВЕРЖДЕНА ЧАСТИЧНО",
             {"16_Акт_ПС-202.docx": "договор не указан"})
    _copy(d)
    _docx(os.path.join(d, "16_Акт_ПС-202.docx"),
          ["АКТ № ПС-202 от 17.03.2026", "Исполнитель: ООО «ЛабСервис Альфа»",
           "Поверка измерительных каналов испытательного стенда производственного назначения.",
           "Стоимость оказанных услуг: 12 500,00 руб."])

    # Посторонние файлы (неизвестный формат, заметка без суммы) не мешают найти расхождение.
    d = case("09_unrelated_files", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН",
             {"98_заметки.txt": "сумма не определена", "99_архив.bin": "ошибка чтения"})
    _copy(d)
    with open(os.path.join(d, "98_заметки.txt"), "w", encoding="utf-8") as f:
        f.write("Заметки по проекту. Позвонить исполнителю, уточнить сроки сдачи этапа.\n")
    with open(os.path.join(d, "99_архив.bin"), "wb") as f:
        f.write(os.urandom(64))

    # Отчет в XLSX, в который ошибочно включен дубликат акта ADM-27 (487 500,00) по статье 0900.
    d = case("10_xlsx_report_with_duplicate", {5: OK, 10: OK, 15: BAD, 16: BAD, 17: BAD}, "ОТЧЕТ НЕДОСТОВЕРЕН")
    _copy(d, skip=("01_", "00_"))
    row = list(ROW_2026)
    row[13] = round(row[13] + 487500.00, 2)
    row[14] = round(row[14] + 487500.00, 2)
    _xlsx_report(os.path.join(d, "01_Карточка_учета_фактических_затрат.xlsx"), row)

    # Нестандартная форма отчета на 7 граф: схема определяется по шапке.
    d = case("11_short_form_report", {3: OK, 4: OK, 5: BAD, 6: OK, 7: BAD}, "ОТЧЕТ НЕДОСТОВЕРЕН")
    _copy(d, skip=("01_",))
    _xlsx_report(os.path.join(d, "01_Карточка_учета_фактических_затрат.xlsx"), list(ROW_2026), wide=False)

    # Тот же базовый набор, но одним zip-архивом с вложенной папкой.
    zpath = os.path.join(out, "12_zip_input.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(os.listdir(BASE)):
            z.write(os.path.join(BASE, n), "данные/" + n)
    case("12_zip_input", BASELINE, "ОТЧЕТ НЕДОСТОВЕРЕН", path=zpath)
    return cases


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "cases")
    os.makedirs(target, exist_ok=True)
    for name, c in build(target).items():
        print(f"{name}: {c['verdict']}; ожидаемые статусы граф: {c['expect']}")
    print(f"\nНаборы созданы в {target}. Проверка одного набора: python -m sverka check {target}/01_baseline --out out")
