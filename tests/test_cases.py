"""Сквозные тесты на демонстрационном наборе и его вариациях. Нужен Tesseract с русской моделью."""
import json
import os

import pytest

import make_cases
from sverka import ocr
from sverka.engine import run_check
from sverka.render import write_all

pytestmark = pytest.mark.skipif(not ocr.available(), reason="Tesseract не установлен")


@pytest.fixture(scope="session")
def cases(tmp_path_factory):
    return make_cases.build(str(tmp_path_factory.mktemp("cases")))


@pytest.mark.parametrize("name", [
    "01_baseline", "02_no_readme", "03_missing_month", "04_corrupt_file", "05_extra_duplicate",
    "06_other_period_doc", "07_discrepancy_closed", "08_no_contract_doc", "09_unrelated_files",
    "10_xlsx_report_with_duplicate", "11_short_form_report", "12_zip_input"])
def test_case(cases, name):
    c = cases[name]
    r = run_check(c["path"])["result"]
    got = {i["column"]: i["status"] for i in r["indicators"]}
    assert {k: got.get(k) for k in c["expect"]} == c["expect"]
    assert r["verdict"]["status"] == c["verdict"]
    docs = {d["file"]: d["status"] for d in r["documents"]}
    assert {k: docs.get(k) for k in c["docs"]} == c["docs"]


def test_baseline_matches_answer_key(cases, tmp_path):
    """Эталон кейса: 0200, 0300, 0900 совпадают; 0800 завышена на 12 500,00; итог расходится на 12 500,00."""
    run = run_check(cases["01_baseline"]["path"])
    r = run["result"]
    ind = {i["column"]: i for i in r["indicators"]}
    exp = {5: ("5558474.83", "5558474.83"), 10: ("1671359.96", "1671359.96"), 15: ("1145798.10", "1133298.10"),
           16: ("3204744.51", "3204744.51"), 17: ("11580377.40", "11567877.40")}
    for col, (rep, expected) in exp.items():
        assert f"{ind[col]['report_value']:.2f}" == rep and f"{ind[col]['expected_value']:.2f}" == expected
    assert f"{ind[15]['diff']:.2f}" == "12500.00" and ind[15]["category"] == "расхождение суммы"
    assert all(ind[c]["status"] == "НЕПРОВЕРЕНО" for c in (3, 6, 8, 9, 11, 12, 13, 14))
    assert all(c["status"] == "ОК" for c in r["report_checks"]) and len(r["report_checks"]) == 35
    # Каждый учтенный документ имеет подтверждающий фрагмент с адресом.
    for d in r["documents"]:
        assert d["evidence"]["amount"]["text"] and d["evidence"]["amount"]["where"]
    paths = write_all(run, str(tmp_path / "out"))
    data = json.load(open(paths["json"], encoding="utf-8"))
    assert data["verdict"]["status"] == "ОТЧЕТ НЕДОСТОВЕРЕН"
    assert os.path.getsize(paths["xlsx"]) > 5000 and "12 500,00" in open(paths["html"], encoding="utf-8").read()
    assert os.path.isfile(os.path.join(tmp_path, "out", "evidence", "отчет__проверяемая_строка.png"))


def test_duplicate_in_report_is_categorized(cases):
    r = run_check(cases["10_xlsx_report_with_duplicate"]["path"])["result"]
    ind = {i["column"]: i for i in r["indicators"]}
    assert ind[16]["category"] == "дублирование" and "10_Акт_ADM-27_копия.pdf" in ind[16]["explanation"]


def test_web_api(cases):
    from fastapi.testclient import TestClient
    from sverka.web import app
    c = TestClient(app)
    assert c.get("/health").json()["status"] == "ok"
    with open(cases["12_zip_input"]["path"], "rb") as f:
        resp = c.post("/api/check", files=[("files", ("case.zip", f.read(), "application/zip"))])
    assert resp.status_code == 200 and resp.json()["verdict"]["status"] == "ОТЧЕТ НЕДОСТОВЕРЕН"
    job = resp.json()["meta"]["job"]
    assert c.get(f"/jobs/{job}").status_code == 200 and c.get(f"/jobs/{job}/report.xlsx").status_code == 200
