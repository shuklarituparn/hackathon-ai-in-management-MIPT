# Contributing

Read the [team plan](docs/planning/README.md), then the assigned section of the [backlog](docs/planning/03-backlog.md). The backlog defines scope and acceptance; the [dependency diagram](docs/planning/04-dependencies.md) defines handoffs. No joint meeting is required.

## Track work

The [task issues](https://github.com/shuklarituparn/hackathon-mipt/issues) track T01–T16. Each issue records its owner and links to prerequisite issues and the backlog task. T15 is optional and must not block the core release.

Use issues for status and blockers, the backlog for task requirements, and PRs for code review. If scope or dependencies change, update the backlog and affected issue links together. Assign Aadi's tasks to `shuklarituparn`, Eva's to `evva-m` and Guillermo's to `jodejode77`. Guillermo owns submission; all three have checks in T16.

## Branch and PR

Start from current `main`; use a branch such as `eva/t03-evidence`, `aadi/t01-contracts` or `guillermo/t08-harness`:

```bash
git switch main
git pull --ff-only origin main
git switch -c guillermo/t08-harness
```

Each owner opens a PR once there are changes to review. Aim for one focused PR per task; split it into smaller handoffs when useful. Do not create empty PRs for future tasks. Assign the PR to its owner and name the task ID in the title.

For example, Eva's T03 is [issue #3](https://github.com/shuklarituparn/hackathon-mipt/issues/3):

- Branch: `eva/t03-evidence`
- PR title: `T03: Add versioned evidence`
- Assignee: `evva-m`
- Description: explain the changed behavior, list exact check commands/results, and include `Closes #3` if all T03 acceptance criteria pass.

`Closes #3` links the PR to the issue; merging into `main` closes that issue automatically. Use the actual GitHub issue number, which is separate from the task ID. For unfinished work, open a **Draft PR** and describe what remains:

```markdown
Refs #3

Adds PDF evidence locators and their validation tests.
Remaining: spreadsheet locators and document-version checks.
```

`Refs #3` records the reference without closing the issue. A partial handoff can be merged while the task issue stays open; mark the PR ready for review when that handoff is usable.

Keep start and closing dependencies on the task issue. If a PR also needs another PR's code, link it as `Depends on #<PR number>` and merge the prerequisite first. This text is a reviewer note, not an automated merge gate.

Before merging, obtain one teammate's review when available. Breaking shared contracts require explicit review from affected consumers; silence is not approval. Publish schemas and example payloads in the same change. If a reviewer is offline, continue independent work and record the blocker. Coordinate edits to shared files such as `web.py` with Aadi.

Merge through the PR after the [checks](#checks) pass. Resolve conflicts on the task branch; do not force-push `main` or include unrelated files.

## Checks

Use Python 3.13 to match CI and Docker. Run commands from the repository root in an isolated Python environment:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q --ignore=tests/test_cases.py -m "not live and not ocr"
git diff --check
```

Opening a PR to `main` (including a Draft PR), pushing new commits to it, or pushing to `main` runs [CI](.github/workflows/ci.yml). A branch push without a PR does not trigger this workflow. No provider keys are used.

Open the PR's **Checks** tab to see the three jobs below. For a failed job, open its logs, fix the cause on the same branch and push again; CI reruns automatically. Merge after all three jobs succeed and the applicable review is complete.

| GitHub check | What runs | Failure means |
|---|---|---|
| Ruff | Pinned Ruff scans all Python files using `ruff.toml` | Syntax, undefined-name or related correctness error |
| Unit tests | Development dependency install, then the pytest command above | Dependency installation or an ordinary test failed |
| Docker build and health | Compose validation, image build, OCR languages, Qwen-Agent import, container startup and `/health` | The packaged app cannot build/start with its declared dependencies |

Ruff is pinned in `requirements-dev.txt`. Formatting is separate from the correctness gate; when editing a Python file, format that file rather than the whole prototype:

```bash
python -m ruff format sverka/changed_module.py
```

Replace the example path with the file being edited. Do not combine a codebase-wide reformat with a feature PR.

CI excludes the legacy OCR suite `tests/test_cases.py`: its bundled dataset is missing two May spreadsheets tracked in [T07](https://github.com/shuklarituparn/hackathon-mipt/issues/7). It also excludes tests marked `live` or `ocr`; apply these markers to new tests that need a provider or OCR. Green CI does not establish financial correctness or complete release acceptance.

After T07, run all tests that do not require a live provider with Tesseract `rus`/`eng` installed: `python -m pytest -q -m "not live"`. Run live-provider checks separately with the configured credentials and record the [release gates](docs/planning/05-validation.md#release-gates). Document failures/skips in the PR; never report skipped tests as passed.

To reproduce the packaging check locally:

```bash
docker compose config --quiet
docker build --tag sverka:ci .
docker run --rm -p 127.0.0.1:8000:8000 sverka:ci
```

In another terminal, open `http://127.0.0.1:8000/health`; the JSON response must contain `"status": "ok"`. Stop the container with Ctrl+C. Making these checks required for merge is a separate repository-settings change; this workflow runs them but does not enable branch protection.

## Publish a handoff

Post the commit/PR, callable interface, example input/output, test or reproduction command, and known limitations. Keep progress in the task issue so teammates can pick it up later. For an incomplete interface, label what is usable and what is blocked.

Keep provider keys, `.env`, virtual environments, runtime data and generated reports out of commits. Commit fixtures deliberately; the answer workbook is test-only and must never enter agent input. Explain any changed limitations in the README. Release checks are recorded against the submission commit as described in T16.
