# Case 19 fixtures (T07)

Test oracles for the supplied case19 bundle. The inputs live in `examples/case19/` (1 report, 22 primaries, 2 context files); that folder holds **no** expected values and is what gets uploaded. Everything here is test-only: never upload it or pass it to the agent as evidence.

## Files

| File | What it is |
|---|---|
| `manifest.json` | Every input file with its role, format, whether it is a scan, the typed fixture it maps to (`fixture`), and its expected extraction and status. Fields a document doesn't print are `null`: statements give a period but no date, and most acts give a date but no period. |
| `primary_174.synthetic.json` | `PrimaryExtraction` for `05_Счет-акт_174_25-02-2026.pdf`: native PDF, production overhead 318 298,10, no stated service period. |
| `primary_kl31.synthetic.json` | `PrimaryExtraction` for `06_Служебная_записка_КЛ-31.docx`: DOCX table cell (V03), 270 000,00, no stated service period. |
| `primary_pay01_jan.synthetic.json` | `PrimaryExtraction` for `02_01_Ведомость_ЗП_Январь_2026.xlsx`: uncached `=SUM(C6:C7)` (V04). Line 0202 sits at `payroll_total` with `possible_categories` `payroll_additional`/`leave_reserve`. |
| `scenarios/<name>/expected.json` | One scenario: how its inputs differ from the bundle (`inputs`), and its contract-shaped oracle (`expected`, or `variants`, or `expected_preparation`). Extra input files sit next to it. |

## Single source of typed values

`tests/recon_fixtures.py` holds the typed case19 extractions used by reconciliation (T10). `manifest.json` and the synthetic examples must agree with it, and `tests/test_case19_fixtures.py` checks that, plus the spreadsheets' actual cell values, so the oracles can't drift apart.

## Scenarios

| Scenario | Inputs | Checks |
|---|---|---|
| `baseline` | the bundle | V01: 5 verified, 2 discrepancies (+12 500 in column 15, explaining the total), 8 insufficient |
| `missing_march` | March payroll removed | V02: payroll and the total partial (observed 4 456 674,83 and 10 466 077,40), no delta; +12 500 kept |
| `replacement_version` | act 174 uploaded again **as a replacement** (`replaces`) | the original is superseded (`duplicate_of` the new version); result as V01 |
| `conflicting_versions` | a separate act № 174 with 381 298,10 | policy 3: neither version chosen; column 15 and the total `conflicting_versions` |
| `typo_not_repaired` | act ПС-201 printing 350,00 | V05: 350 accepted (diff 12 150) or unresolved; never 550 |
| `unknown_category` | накладная МАТ-88, materials or overhead | V06: category unresolved; columns 4 and 15 insufficient |
| `corrupt_input` | act 174 truncated, an empty file | V03/V06: both unreadable and visible; nothing verified |
| `rotated_scan` | act 174 as an image-only page turned 90° | V03: OCR after orientation (`transform.rotation` 90); result as V01 |
| `uncached_sum` | the bundle (January already stores no cached SUM) | V04: formula kept, derived 1 110 000,00 |
| `renamed_files` | every file given a neutral name | same result as `baseline` |
| `reordered_upload` | upload order reversed | same result as `baseline` |

Reconciliation-level oracles run through `reconcile` on typed fixtures in `test_case19_fixtures.py`. Preparation-level oracles (`expected_preparation`) are checked against the files here and are for T04 to assert on its output. For live runs (T11), `tests/case19_scenarios.py` builds a scenario's upload folder:

```python
from case19_scenarios import build_scenario

uploads = build_scenario("missing_march", tmp_path / "missing_march")  # [Upload(path, role, replaces)]
```

## Synthetic vs live

- **`*.synthetic.json`**: typed by hand from the source file. They are test targets, not proof of what a model produces.
- **`*.live.json`**: produced by a real harness run (T08/T11) and saved verbatim. None exist yet. Do not rename a synthetic file to `.live.json` by hand.

## Loading an example

```python
from decimal import Decimal
from pathlib import Path

from sverka import contracts as c

p = c.PrimaryExtraction.model_validate_json(
    Path("tests/fixtures/case19/primary_174.synthetic.json").read_text(encoding="utf-8")
)
assert p.amount.value == Decimal("318298.10")
assert p.allocations[0].category.value == "production_overhead"
```
