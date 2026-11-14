# Team plan

Track status and blockers in the [GitHub task issues](https://github.com/shuklarituparn/hackathon-mipt/issues); task IDs match this plan.

**Deadline: 14 November 2026, 23:59 Istanbul. Submit by 22:00.**

Build a local chat application that checks the report against supporting documents and shows results with clickable evidence. **The browser UI, Python backend and harness run locally.** The backend calls the configured model and, if selected, hosted OCR/vision APIs. Files and SQLite remain in the local `/data` volume.

The agent interprets the report and every primary document. Native readers/OCR provide evidence; validation and financial calculations run in code. [Architecture image and explanation](../architecture/README.md).

## What each person reads

Everyone: this page and the diagram. Then only the assigned task section:

| Person | Tasks | Build estimate | Integration/testing |
|---|---|---:|---:|
| [Eva](03-backlog.md#eva) | T03–T07: preparation, evidence tools, extraction validation, fixtures | 12h | 4h |
| [Aadi](03-backlog.md#aadi) | T01–T02,T10–T12: contracts, storage/API, reconciliation, Docker | 12h | 4h |
| [Guillermo](03-backlog.md#guillermo) | T08–T09,T13–T14: harness, chat/evidence UI, docs, demo | 11h | 4h |

T15 is an optional 1h interpretation-confirmation feature for Guillermo. Total: **47 team hours**, or 48h with T15. These are estimates for discussion.

## Work asynchronously

1. No task requires all three people online together. Aadi publishes v1 schemas/examples from the [written contracts/policies](../architecture/README.md#contracts). Eva and Guillermo review and use them when available; breaking changes need the affected consumer's explicit review.
2. Aadi targets publication during the first 1.5h of his own work, then storage/reconciliation. Start Docker early.
3. Eva publishes evidence adapters and repairs baseline inputs, then preparation/inspection/validation functions. These are ordinary Python functions; Guillermo owns the model loop.
4. Guillermo connects one available provider, builds the harness against fake tools and chat against a sample result. No model comparison exercise.

Each task lists reusable code and starting files. Publish small working functions/examples before going offline. Use [written handoffs](04-dependencies.md#asynchronous-coordination); a dependency means an artifact is ready, not that its owner attends a meeting.

## Checkpoints

| Target | Done means |
|---|---|
| First compatible evidence/harness/storage handoffs ready | Integrator checks a real scanned report field and source amount in chat with evidence |
| 13 Nov, 18:00 | Complete baseline runs live with expected values/statuses |
| 14 Nov, 12:00 | Feature freeze; drop optional work if core checks fail |
| 14 Nov, 15:00 | Another teammate launches clean Docker and runs both use cases |
| 14 Nov, 18:00 | Code freeze; release blockers only |
| 14 Nov, 22:00 | Submit and verify receipt |

Calendar targets need checking against individual schedules; team-hour estimates are not elapsed time. If the baseline slips, stop optional exports, streaming, complex viewers and T15. Aadi assigns each blocking defect to its owner; others continue independent work. Keep evidence, indicator coverage, uncertainty handling and reproducible startup.

## Reference only

[Code/reuse](01-architecture-assessment.md) · [Rubric](02-gap-analysis.md) · [Dependencies](04-dependencies.md) · [Expected results/tests](05-validation.md) · [Risks](06-risks.md)

Application code is unchanged. This is proposed implementation work.
