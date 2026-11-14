# Contract examples (v1)

Published by T01 for Eva and Guillermo to build against. Schemas live in [`sverka/contracts.py`](../../../sverka/contracts.py); the generated JSON Schema is [`schema.json`](schema.json).

```python
from sverka.fakes import InMemoryStore, fake_reconcile, load_example

primary = load_example("primary")          # PrimaryExtraction
result = load_example("result")            # ReconciliationResult
```

| File | Type | What it shows |
|---|---|---|
| `report_field.json` | `IndicatorEntry` | Validated report field: column 15 on the scanned report, accepted with OCR evidence |
| `primary.json` | `PrimaryExtraction` | Primary document: Счет-акт № 174, 318 298,10, production overhead; service period inferred from the issue date |
| `unresolved_field.json` | `DecimalObservation` | Unresolved field: value `null`, reason and finding; the proposed 550 is not repaired (V05) |
| `result.json` | `ReconciliationResult` | Result and conclusion: overhead +12 500,00, total explained by it, verified zero, an unverifiable split column and three exclusions |
| `evidence_blocks.json` | `list[EvidenceBlock]` | Evidence locators: page region on a scan (OCR) and native PDF, XLSX literal and uncached `=SUM` formula, DOCX table cell |

## Real or synthetic

- Values, texts and boxes in `evidence_blocks.json`, `report_field.json` and `primary.json` come from `examples/case19`. Report boxes are on a 300 dpi render (2632×1860); act 174 boxes are on a 150 dpi render (1240×1754).
- `result.json` uses the oracle values from [validation](../../../docs/planning/05-validation.md) for a subset of columns (4, 8, 15, 16, 17), not all 15. It cites block IDs not present in `evidence_blocks.json`. This is a test oracle: never pass it to the agent as evidence.
- `unresolved_field.json` is synthetic (`blk_synthetic_amount` does not exist).
- IDs such as `ver_sa174_v1` are readable stand-ins; real IDs come from `contracts.new_id`.

## Rules

- Amounts are decimal strings (`"1145798.10"`), never JSON numbers. Their scale is kept, so `"1000"` stays `1000`.
- `null` means unresolved or unknown. Zero (`"0.00"`) is a value.
- Proposals (`ProposedField`) have no acceptance flag; only `validate_extraction` produces `accepted`/`unresolved`.
- If `coverage` is not `complete`, `expected_value` and `diff` are `null`.
- Examples are canonical: each file equals the JSON dump of the model it parses into (`tests/test_contracts.py` checks this). When a contract changes, update the examples and run `python -m sverka.contracts > tests/fixtures/contracts/schema.json` in the same change.
