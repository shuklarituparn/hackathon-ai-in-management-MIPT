# Validation reference

Existing: `test_units.py`, `test_cases.py`, `scripts/make_cases.py` describe 37 checks and 12 generated scenarios. End-to-end cases skip without Tesseract; the example dataset is incomplete until T07. Full application/Docker acceptance was not run during the audit.

Tests named below are planned. Model connectivity/live checks verify integration; there is no comparative model benchmark.

## Checks

| ID | Required assertion | Planned location |
|---|---|---|
| V01 | Real full baseline matches all 15 fields below; correct exclusions and evidence | `test_reconciliation.py`, `test_live_flow.py`, browser |
| V02 | Missing March: partial payroll, no complete payroll/total delta; known overhead error remains | Same |
| V03 | Supplied formats, watermark/rotated scan, native cells/quotes; actual crop maps to correct version | `test_preparation.py`, `test_evidence.py` |
| V04 | Uncached SUM correct; unknown dependency stays unknown; half-up rounding; raw formulas retained | `test_formula_states.py` |
| V05 | Typed 350 is never repaired to 550; literal 1000 never becomes 10; arithmetic issue is a finding | `test_extraction.py`, `test_reconciliation.py` |
| V06 | Unaccepted category not counted; one matching month incomplete; unknown not zero; duplicates identity-aware | `test_reconciliation.py` |
| V07 | Service/issue dates, overlap, allocations, units/sign/rounding and conflicting versions follow policy | Same |
| V08 | Bad model/tool output, false evidence, timeout, retry exhaustion/cancel and missing credentials fail explicitly | `test_harness.py`, `test_extraction.py`, `test_live_flow.py` |
| V09 | Chat card/evidence/follow-up/refresh work; null and partial values stay correctly labeled | Browser checklist, API tests |
| V10 | Same-name versions, wrong workspace/path, invalid/oversized archive, interrupted write/restart and late cancel response | `test_storage.py`, `test_api.py`, `test_live_flow.py` |
| V11 | Clean local Docker launch, provider configuration, persistence and restart without source edits | Teammate's clean-machine check |
| V12 | README/diagram/two use cases/defense match the submitted build | Documentation/demo review |
| V13 | Optional interpretation revision preserves old source/run and recalculates from recorded decision | `test_revisions.py`, browser |

## Baseline expected results

Test oracle only: never send the answer workbook or these expected results as primary evidence to the agent. Values are RUB except column 3. Column numbers identify the supplied fixture, not a universal schema.

| Column / indicator | Reported | Complete expected | Status |
|---|---:|---:|---|
| 3 workforce intensity | 34.14 person-months | — | Insufficient evidence |
| 4 materials | 0.00 | 0.00 | Verified within closed supplied scope |
| 5 payroll total | 5,558,474.83 | 5,558,474.83 | Verified |
| 6 payroll account subtotal | 5,248,019.04 | — | Insufficient evidence |
| 7 main payroll | 5,054,470.26 | 5,054,470.26 | Verified |
| 8 additional payroll | 193,548.78 | — | Insufficient evidence |
| 9 leave reserve | 310,455.79 | — | Insufficient evidence |
| 10 contributions total | 1,671,359.96 | 1,671,359.96 | Verified |
| 11 contributions subtotal | 1,578,126.37 | — | Insufficient evidence |
| 12 main-payroll contributions | 1,526,450.09 | — | Insufficient evidence |
| 13 additional-payroll contributions | 51,676.28 | — | Insufficient evidence |
| 14 reserve contributions | 93,233.59 | — | Insufficient evidence |
| 15 production overhead | 1,145,798.10 | 1,133,298.10 | Discrepancy +12,500.00 |
| 16 administrative overhead | 3,204,744.51 | 3,204,744.51 | Verified |
| 17 total | 11,580,377.40 | 11,567,877.40 | Discrepancy +12,500.00 |

Overhead calculation: `245000.00 + 318298.10 + 270000.00 + 300000.00`. Total and overhead describe the same RUB 12,500 error, not RUB 25,000.

Manifest: 1 report, 22 primaries, separate context. Exclude ADM-27 copy, June document and other-contract ADM-75 with reasons. Check all four overhead sources, one source per other principal category, the exclusions, one formula total and one DOCX quote. Test a rotated source and an older version after replacement.

Converse-zero test: an unreadable/unclassified accounting document whose possible categories include materials makes column 4 unverified. Unknown relevance blocks every plausible category. Legacy confidence 0.20/0.34 is a bypass regression, not a new model threshold.

## Missing-March expected results

Surviving payroll `observed_sum` is **4,456,674.83**, explicitly partial. Payroll and overall total have `expected_value=null`, `diff=null`. The conclusion retains the RUB 12,500 component discrepancy and the evidence gaps; no confirmed grand-total delta is available.

## Commands and records

From the repo root; service name remains `sverka` unless all instructions are updated together:

```text
# Existing suite; repair fixtures and retain/adapt/retire legacy tests explicitly
docker compose run --rm --entrypoint python sverka -m pytest -q tests/test_units.py tests/test_cases.py

# New deterministic tests after implementation
docker compose run --rm --entrypoint python sverka -m pytest -q -m "not live"

# New real-provider tests; register @pytest.mark.live
docker compose run --rm --entrypoint python sverka -m pytest -q -m live tests/test_live_flow.py

# Clean launch, then manually run both use cases in the browser
docker compose config
docker compose build --no-cache
docker compose up -d
curl http://localhost:8000/health
docker compose down
docker compose up -d
```

Use local `.env` credentials from the placeholder example; no secrets in fixtures. Preserve `/data` across stop/start. Do not add volume-deletion flags. A green old suite or skipped OCR/live suite is not new-runtime acceptance.

## Release gates

| Gate | Pass condition |
|---|---|
| G1 | All streams consume shared examples; IDs, decimal strings, null/zero and tool errors agree |
| G2 | Real source evidence passes through agent/validation/storage into clickable chat output |
| G3 | Complete live baseline matches all fields, exclusions and fragments |
| G4 | Missing-data case, six safety regressions and explicit technical failures pass |
| G5 | Follow-up, evidence, refresh/restart/cancel preserve run/version references |
| G6 | Another teammate follows README from clean Docker and completes both use cases |
| G7 | Docs/demo match tested commit; submitted version and receipt confirmed |

Record commit, commands/results/skips, machine/resources, redacted provider/configuration, actual duration and limits in `docs/release-check.md`. Replaying accepted facts must reproduce arithmetic; label replay and separately prove live G2/G3. Inspect fragments visually, not only their IDs. No scale benchmark or unmeasured speed claim is required.
