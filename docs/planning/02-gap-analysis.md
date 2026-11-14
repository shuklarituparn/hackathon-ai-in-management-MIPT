# Rubric coverage

Reference for review. [Current-code evidence](01-architecture-assessment.md) · [Validation cases](05-validation.md). Points are maxima, not predictions.

| ID | Requirement / points | Gap | Tasks | Checks |
|---|---|---|---|---|
| R01 | Report extraction / 8 | Specialized unsafe parser; no primary agent path | T03–T06,T08 | V01,V03,V05 |
| R02 | Primary extraction / 8 | Readers exist; interpretation/validation missing | T03–T08 | V01,V03,V04 |
| R03 | Correct links / 4 | Contract/period/duplicate decisions need revision | T06,T10 | V06,V07 |
| R04 | Aggregation / 8 | Arithmetic must consume accepted facts | T10 | V01,V06 |
| R05 | Period/unit/sign/rounding / 5 | Formula/period handling unsafe | T04,T06,T10 | V04,V07 |
| R06 | Duplicates/gaps/ambiguity / 3 | States can be bypassed | T06,T10 | V02,V06 |
| R07 | Find discrepancies / 12 | Mutation/permissive acceptance | T06,T10 | V01,V05–V07 |
| R08 | Preserve correct results / 8 | Same failure modes | T04,T06,T10 | V03–V07 |
| R09 | Problem categories / 4 | New decision path needed | T06,T10 | V01,V02,V06,V07 |
| R10 | Specific evidence / 6 | Version IDs/classification support missing | T02–T06,T09 | V01,V03,V09 |
| R11 | Complete clear result / 6 | Chat result missing | T09–T11 | V01,V02,V09 |
| R12 | Insufficient evidence vs error / 4 | Coverage bypass | T06,T10 | V02,V06 |
| R13 | File/technical reliability / 4 | Agent/run failure states missing | T02,T04,T06,T08,T11 | V03,V08,V10 |
| R14 | Docker deployment / 8 | Clean run unverified | T12,T16 | V11 |
| R15 | README / 6 | Old claims; env example missing | T12–T13,T16 | V11,V12 |
| R16 | Architecture + two use cases / 6 | Align proposal with final build | T13–T14,T16 | V12 |
| R17 | Workflow/demo / 16 industry | Intended interaction missing | T08–T14,T16 | V01,V02,V09,V11 |
| R18 | Useful extras/roadmap / 10 industry | Conversational investigation planned; revision optional | T08–T09,T13–T15 | V09,V12,V13 |
| R19 | Presentation / 4 industry | Defense describes old design | T13–T14,T16 | V12 |

Technical scores decide the top three; industry scores follow at defense. No specific export format is mandated. Evidence, uncertainty and reproducible launch take precedence over optional UI features.
