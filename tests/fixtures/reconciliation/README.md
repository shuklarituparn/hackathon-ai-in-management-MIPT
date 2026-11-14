# Reconciliation results (T10)

Output of `sverka.reconciliation.reconcile` on the typed case19 fixtures in [`tests/recon_fixtures.py`](../../recon_fixtures.py). Use them to build and test result cards before live extraction (T09). Both are `ReconciliationResult` payloads.

| File | Use case | Highlights |
|---|---|---|
| `baseline_result.json` | Complete bundle | 5 verified, 2 discrepancies (column 15 and the total, the same +12 500,00), 8 insufficient; three exclusions plus the README as context |
| `missing_march_result.json` | March payroll statement removed | Payroll and total partial (`observed_sum` 4 456 674,83 and 10 466 077,40, `expected_value`/`diff` null); the +12 500,00 overhead discrepancy is kept |

These are synthetic typed fixtures, not recorded live extractions. The May payroll and contributions amounts are derived from the oracle totals until T07 restores the May spreadsheets, and block IDs such as `blk_sa174_amount` do not exist in any store. This is test data: never pass it to the agent as evidence.

`tests/test_reconciliation.py` fails when these files no longer match the code. To regenerate them, from the repository root:

```bash
python -m tests.recon_fixtures
```
