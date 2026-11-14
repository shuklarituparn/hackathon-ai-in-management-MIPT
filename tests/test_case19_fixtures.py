"""T07: the case19 dataset, its manifest and the scenario oracles.

Reconciliation-level oracles run through ``reconciliation.reconcile`` on the typed fixtures in
``recon_fixtures.py`` (the single source of typed values). Preparation-level oracles (rotated scan,
uncached SUM, corrupt files) check the input files themselves here; T04 checks its own output against
them. End-to-end runs over the built folders belong to T11 (``test_live_flow.py``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import recon_fixtures as f
from case19_scenarios import (
    CASE19,
    FIXTURES,
    build_scenario,
    input_roles,
    load_manifest,
    load_scenario,
    neutral_names,
    scenario_names,
)

from sverka import contracts as c
from sverka import readers
from sverka.reconciliation import reconcile

ACT_174 = "05_Счет-акт_174_25-02-2026.pdf"


# ======================= typed scenarios =======================


def _extra(key: str, extraction=None, **kw) -> f.Item:
    return f.Item(key, "primary", extraction, **kw)


def _act174(key: str, amount: str) -> c.PrimaryExtraction:
    return f.overhead(key, "Счет-акт", "174", date(2026, 2, 25), amount, f.PROD, "ООО «ИнжКонтур Бета»")


def _ps201(amount: str | None) -> c.PrimaryExtraction:
    return f.primary(
        "ps201",
        doc_type="Акт",
        number="ПС-201",
        amount=amount,
        issue=date(2026, 3, 16),
        counterparty="ООО «ЛабСервис Альфа»",
        lines=[(f.PROD, amount, (), None)],
    )


def _mat88() -> c.PrimaryExtraction:
    return f.primary(
        "mat88",
        doc_type="Накладная",
        number="МАТ-88",
        amount="45000.00",
        issue=date(2026, 3, 20),
        counterparty="ООО «Снабжение Дельта»",
        lines=[(None, "45000.00", ["materials", f.PROD], None)],
    )


def _unreadable(*keys: str) -> list[f.Item]:
    out = [replace(i, extraction=None, preparation="failed") if i.key in keys else i for i in f.items()]
    return out + [_extra(k, None, preparation="failed") for k in keys if k not in {i.key for i in f.items()}]


TYPED = {
    "baseline": f.items,
    "missing_march": lambda: f.without("pay03"),
    "replacement_version": lambda: f.plus(_extra("sa174v2", _act174("sa174v2", "318298.10"), predecessor="ver_sa174")),
    "conflicting_versions": lambda: f.plus(_extra("sa174fix", _act174("sa174fix", "381298.10"))),
    "typo_not_repaired/amount_unresolved": lambda: f.plus(_extra("ps201", _ps201(None))),
    "typo_not_repaired/amount_350": lambda: f.plus(_extra("ps201", _ps201("350.00"))),
    "unknown_category": lambda: f.plus(_extra("mat88", _mat88())),
    "corrupt_input": lambda: _unreadable("sa174", "empty16"),
    "rotated_scan": f.items,
    "uncached_sum": f.items,
}


def _oracle(case: str) -> dict:
    name, _, variant = case.partition("/")
    spec = load_scenario(name)
    expected = spec["variants"][variant]["expected"] if variant else spec["expected"]
    return load_scenario(expected["same_as"])["expected"] if "same_as" in expected else expected


def _key(version_id: str | None) -> str | None:
    return None if version_id is None else version_id.removeprefix("ver_")


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _check(result: c.ReconciliationResult, expected: dict) -> None:
    by_id = {i.entry_id: i for i in result.indicators}
    counts = Counter(i.status for i in result.indicators)
    assert {s: counts.get(s, 0) for s in expected["status_counts"]} == expected["status_counts"]
    for entry_id, fields in expected["indicators"].items():
        got = by_id[entry_id]
        for name, want in fields.items():
            have = getattr(got, name)
            if name in ("observed_sum", "expected_value", "diff"):
                assert _dec(have) == _dec(want), f"{entry_id}.{name}: {have} != {want}"
            else:
                assert have == want, f"{entry_id}.{name}: {have} != {want}"
    exclusions = {
        (_key(e.version_id), e.reason, _key(e.duplicate_of)) for e in result.exclusions if e.reason != "context_only"
    }
    assert exclusions == {(x["fixture"], x["reason"], x.get("duplicate_of")) for x in expected["exclusions"]}
    unresolved = {(_key(u.version_id), tuple(u.possible_categories)) for u in result.unresolved}
    assert unresolved == {(u["fixture"], tuple(u["possible_categories"])) for u in expected["unresolved"]}
    conclusion = expected["conclusion"]
    assert [(d.entry_id, d.diff, d.problem) for d in result.conclusion.component_discrepancies] == [
        (d["entry_id"], Decimal(d["diff"]), d["problem"]) for d in conclusion["component_discrepancies"]
    ]
    total = result.conclusion.grand_total
    want = conclusion["grand_total"]
    assert (total.entry_id, total.status, total.explained_by) == (
        want["entry_id"],
        want["status"],
        want["explained_by"],
    )
    assert (_dec(total.diff), _dec(total.unexplained_diff)) == (_dec(want["diff"]), _dec(want["unexplained_diff"]))


@pytest.mark.parametrize("case", sorted(TYPED))
def test_scenario_oracle_matches_reconciliation(case):
    result = reconcile(*f.bundle(TYPED[case]()))
    c.ReconciliationResult.model_validate(result.model_dump(mode="json"))
    _check(result, _oracle(case))


def test_every_scenario_with_a_reconciliation_oracle_is_typed():
    cases = set()
    for name in scenario_names():
        spec = load_scenario(name)
        cases |= {f"{name}/{v}" for v in spec["variants"]} if "variants" in spec else {name}
    assert cases - {"renamed_files", "reordered_upload"} == set(TYPED)


def test_typo_oracle_never_accepts_550():
    spec = load_scenario("typo_not_repaired")
    assert spec["expected_extraction"]["amount"]["accepted_value"] == "350.00"
    totals = [v["expected"]["indicators"]["col15"]["observed_sum"] for v in spec["variants"].values()]
    assert "1133848.10" not in totals  # what 550 would give


def _renamed(items: list[f.Item]):
    scope, report, primaries, manifest = f.bundle(items)
    names = neutral_names([e.version.original_filename for e in manifest.entries])
    entries = [
        e.model_copy(
            update={"version": e.version.model_copy(update={"original_filename": names[e.version.original_filename]})}
        )
        for e in manifest.entries
    ]
    return scope, report, primaries, manifest.model_copy(update={"entries": entries})


def _reordered(items: list[f.Item]):
    scope, report, primaries, manifest = f.bundle(list(reversed(items)))
    return scope, report, list(reversed(primaries)), manifest.model_copy(update={"entries": manifest.entries[::-1]})


@pytest.mark.parametrize("name, build", [("renamed_files", _renamed), ("reordered_upload", _reordered)])
def test_renamed_or_reordered_inputs_give_the_baseline_result(name, build):
    assert load_scenario(name)["expected"] == {"same_as": "baseline"}
    baseline = reconcile(*f.bundle())
    result = reconcile(*build(f.items()))
    _check(result, _oracle(name))

    def facts(r):
        return [(i.entry_id, i.status, i.problem, i.observed_sum, i.expected_value, i.diff) for i in r.indicators]

    assert facts(result) == facts(baseline)


# ======================= the input folder and the manifest =======================


def test_input_folder_holds_no_oracle():
    names = {p.name for p in CASE19.iterdir()}
    assert not {n for n in names if n.endswith(".json") or "expected" in n.lower() or "manifest" in n.lower()}


def test_manifest_lists_every_supplied_input_once():
    roles = input_roles()
    assert set(roles) == {p.name for p in CASE19.iterdir()}
    assert Counter(roles.values()) == {"report": 1, "primary": 22, "context": 2}
    m = load_manifest()
    for entry in [m["report"], *m["primaries"], *m["context"]]:
        assert entry["format"] == Path(entry["file"]).suffix[1:]


def test_manifest_scan_flags_match_the_files():
    import pdfplumber

    m = load_manifest()
    for entry in [m["report"], *m["primaries"]]:
        path = CASE19 / entry["file"]
        if path.suffix == ".pdf":
            with pdfplumber.open(path) as pdf:
                scanned = all(len(page.extract_words()) < 3 for page in pdf.pages)
        else:
            scanned = path.suffix == ".png"
        assert entry["scan"] == scanned, entry["file"]


def _value(obs):
    if obs.state != "accepted":
        return None
    v = obs.value
    if isinstance(v, c.Period):
        return {"from": v.start.isoformat(), "to": v.end.isoformat()}
    if isinstance(v, date):
        return v.isoformat()
    return v if isinstance(v, list) else str(v)


def _expected_fields(ext: c.PrimaryExtraction) -> dict:
    allocations = []
    for a in ext.allocations:
        line = {"line_label": a.line_label, "category": _value(a.category), "amount": _value(a.amount)}
        if a.possible_categories:
            line["possible_categories"] = a.possible_categories
        allocations.append(line)
    names = ["doc_type", "number", "contracts", "counterparty", "issue_date", "service_period", "amount"]
    return {**{n: _value(getattr(ext, n)) for n in names}, "allocations": allocations}


def test_manifest_matches_typed_fixtures_and_published_result():
    typed = {i.key: i.extraction for i in f.items()}
    published = json.loads(Path(f.RESULTS_DIR, "baseline_result.json").read_text(encoding="utf-8"))
    excluded = {_key(e["version_id"]): e for e in published["exclusions"]}
    by_key = {}
    for entry in load_manifest()["primaries"]:
        key = entry["fixture"]
        by_key[key] = entry["file"]
        assert entry["expected"] == _expected_fields(typed[key]), entry["file"]
        if key in excluded:
            assert entry["expected_status"] == "excluded"
            assert entry["expected_exclusion_reason"] == excluded[key]["reason"]
        else:
            assert entry["expected_status"] == "accepted"
    assert set(by_key) == set(typed)
    for entry in load_manifest()["primaries"]:
        if "duplicate_of" in entry:
            assert entry["duplicate_of"] == by_key[_key(excluded[entry["fixture"]]["duplicate_of"])]


def test_manifest_report_oracle_matches_v01():
    report = load_manifest()["report"]
    published = json.loads(Path(f.RESULTS_DIR, "baseline_result.json").read_text(encoding="utf-8"))
    by_status: dict[str, list[int]] = {}
    for i in published["indicators"]:
        by_status.setdefault(i["status"], []).append(i["column"])
    assert report["expected_verified_columns"] == by_status["verified"]
    assert report["expected_discrepancy_columns"] == by_status["discrepancy"]
    assert report["expected_insufficient_columns"] == by_status["insufficient_evidence"]
    assert (report["row_label"], report["contract"]) == (f.ROW, f.CONTRACT)


def test_unstated_dates_and_periods_are_not_expected():
    """The statements print a period but no date; the acts print a date but no period."""
    m = {p["file"]: p["expected"] for p in load_manifest()["primaries"]}
    for name, exp in m.items():
        if name.startswith(("02_", "03_")) or name.startswith("11_"):
            assert exp["issue_date"] is None and exp["service_period"] is not None, name
    for name in ("05_Счет-акт_174_25-02-2026.pdf", "06_Служебная_записка_КЛ-31.docx", "07_УПД_0426-77.pdf"):
        assert m[name]["issue_date"] is not None and m[name]["service_period"] is None, name


# ======================= the spreadsheets and synthetic examples =======================


def _sheet(path: Path):
    import openpyxl

    return openpyxl.load_workbook(path).active, openpyxl.load_workbook(path, data_only=True).active


@pytest.mark.parametrize("month", sorted(f.PAYROLL))
def test_payroll_statements_match_typed_fixtures(month):
    path = CASE19 / f"02_0{month}_Ведомость_ЗП_{f.MONTHS_RU[month]}_2026.xlsx"
    ws, cached = _sheet(path)
    rows = {ws[f"A{r}"].value: Decimal(str(ws[f"C{r}"].value)) for r in (6, 7)}
    main, rest = f.PAYROLL[month]
    assert (rows["0201"], rows["0202"]) == (Decimal(main), Decimal(rest))
    # V04 input: the total is a SUM with no cached result in every statement.
    assert ws["C8"].value == "=SUM(C6:C7)" and cached["C8"].value is None


@pytest.mark.parametrize("month", sorted(f.CONTRIBUTIONS))
def test_contribution_statements_match_typed_fixtures(month):
    path = CASE19 / f"03_0{month}_Страховые_взносы_{f.MONTHS_RU[month]}_2026.xlsx"
    ws, _ = _sheet(path)
    main, rest = f.PAYROLL[month]
    assert Decimal(str(ws["A6"].value)) == Decimal(main) + Decimal(rest)  # base = that month's payroll
    assert Decimal(str(ws["B6"].value)) == Decimal(f.CONTRIBUTIONS[month])
    assert ws["C6"].value == "0300"


@pytest.mark.parametrize(
    "name, key", [("primary_174", "sa174"), ("primary_kl31", "kl31"), ("primary_pay01_jan", "pay01")]
)
def test_synthetic_examples_agree_with_typed_fixtures(name, key):
    p = c.PrimaryExtraction.model_validate_json((FIXTURES / f"{name}.synthetic.json").read_text(encoding="utf-8"))
    typed = {i.key: i.extraction for i in f.items()}[key]
    for field in ("number", "contracts", "issue_date", "service_period", "amount"):
        assert _value(getattr(p, field)) == _value(getattr(typed, field)), field
    lines = [a for a in typed.allocations if a.line_label != "ИТОГО"]
    assert [(_value(a.category), _value(a.amount), a.possible_categories) for a in p.allocations] == [
        (_value(a.category), _value(a.amount), a.possible_categories) for a in lines
    ]


# ======================= built folders and preparation-level oracles =======================


@pytest.mark.parametrize("name", scenario_names())
def test_every_scenario_builds(name, tmp_path):
    uploads = build_scenario(name, tmp_path / name)
    files = {u.path.name for u in uploads}
    assert files == {p.name for p in (tmp_path / name).iterdir()}
    assert not {n for n in files if n.endswith(".json")}
    roles = Counter(u.role for u in uploads)
    assert roles["report"] == 1 and roles["context"] == 2
    spec = load_scenario(name)["inputs"]
    assert roles["primary"] == 22 - len(spec.get("remove", [])) + len(spec.get("add", []))
    for u in uploads:
        if u.replaces is not None:
            assert u.replaces in files


def test_replacement_upload_names_its_predecessor(tmp_path):
    uploads = build_scenario("replacement_version", tmp_path)
    [newer] = [u for u in uploads if u.replaces]
    assert (newer.path.name, newer.replaces) == ("05_Счет-акт_174_25-02-2026_v2.pdf", ACT_174)


def test_renamed_and_reordered_folders_hold_the_same_bytes(tmp_path):
    import hashlib

    def digests(uploads):
        return sorted(hashlib.sha256(u.path.read_bytes()).hexdigest() for u in uploads)

    base = build_scenario("baseline", tmp_path / "base")
    renamed = build_scenario("renamed_files", tmp_path / "renamed")
    reordered = build_scenario("reordered_upload", tmp_path / "reordered")
    assert digests(base) == digests(renamed) == digests(reordered)
    assert not {u.path.name for u in base} & {u.path.name for u in renamed}
    assert [u.path.name for u in reordered] == [u.path.name for u in reversed(base)]


def test_rotated_scan_is_an_image_only_turned_page():
    import pdfplumber

    with pdfplumber.open(FIXTURES / "scenarios" / "rotated_scan" / ACT_174) as pdf:
        [page] = pdf.pages
        assert page.extract_words() == [] and len(page.images) == 1
        assert page.width > page.height  # the portrait act lies on its side


@pytest.mark.ocr
def test_rotated_scan_reads_the_amount_after_orientation(tmp_path):
    content = readers.read_any(str(FIXTURES / "scenarios" / "rotated_scan" / ACT_174))
    assert not content.errors
    assert "318 298,10" in content.text.replace(" ", " ")


def test_uncached_sum_oracle_is_the_decimal_sum_of_its_inputs():
    spec = load_scenario("uncached_sum")["expected_preparation"]
    [(name, prep)] = spec.items()
    cell = prep["cell"]
    ws, cached = _sheet(CASE19 / name)
    assert ws[cell["ref"]].value == cell["formula"] and cached[cell["ref"]].value is cell["cached_value"] is None
    total = sum(Decimal(str(ws[r].value)) for r in ("C6", "C7"))
    assert total == Decimal(cell["derived_value"]) and cell["evaluation"] == "derived"
    c.FormulaState.model_validate(cell)


def test_corrupt_inputs_are_unreadable(tmp_path):
    spec = load_scenario("corrupt_input")["expected_preparation"]
    folder = tmp_path / "corrupt"
    build_scenario("corrupt_input", folder)
    for name, state in spec.items():
        assert state == "unreadable"
        content = readers.read_any(str(folder / name))
        assert content.errors and not content.fragments, name


def test_scenario_input_files_are_documents_not_answers():
    for name in scenario_names():
        folder = FIXTURES / "scenarios" / name
        spec = load_scenario(name)["inputs"]
        listed = set(spec.get("add", [])) | set(spec.get("replace", []))
        assert {p.name for p in folder.iterdir()} - {"expected.json"} == listed, name
