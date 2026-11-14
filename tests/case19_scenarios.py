"""Build the case19 input folders for each scenario in ``tests/fixtures/case19/scenarios``.

Test-only. ``build_scenario`` copies the supplied inputs from ``examples/case19`` (never the oracle
files), applies the scenario's ``inputs`` (remove, replace, add, rename, order) and returns the uploads
in order. Use it for live/end-to-end runs (T11); reconciliation-level checks use typed fixtures.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE19 = ROOT / "examples" / "case19"
FIXTURES = ROOT / "tests" / "fixtures" / "case19"
SCENARIOS = FIXTURES / "scenarios"
MANIFEST = FIXTURES / "manifest.json"


@dataclass(frozen=True)
class Upload:
    path: Path
    role: str
    replaces: str | None = None  # filename of the earlier upload this file is a new version of


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def input_roles() -> dict[str, str]:
    """Supplied input filename -> role, in manifest order: report, primaries, context."""
    m = load_manifest()
    roles = {m["report"]["file"]: "report"}
    roles.update({p["file"]: "primary" for p in m["primaries"]})
    roles.update({x["file"]: "context" for x in m["context"]})
    return roles


def scenario_names() -> list[str]:
    return sorted(p.parent.name for p in SCENARIOS.glob("*/expected.json"))


def load_scenario(name: str) -> dict:
    return json.loads((SCENARIOS / name / "expected.json").read_text(encoding="utf-8"))


def neutral_names(files: list[str]) -> dict[str, str]:
    """Rename map for ``"rename": "neutral"``: names carry no type, number or date, numbered backwards."""
    n = len(files)
    return {name: f"документ_{n - i:02d}{Path(name).suffix}" for i, name in enumerate(files)}


def build_scenario(name: str, dest: Path) -> list[Upload]:
    """Materialize scenario ``name`` in ``dest`` and return its uploads in upload order."""
    spec = load_scenario(name).get("inputs", {})
    folder = SCENARIOS / name
    roles = input_roles()
    files = [f for f in roles if f not in set(spec.get("remove", []))]
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        source = folder / f if f in spec.get("replace", []) else CASE19 / f
        shutil.copyfile(source, dest / f)
    for f in spec.get("add", []):
        shutil.copyfile(folder / f, dest / f)
        roles[f] = "primary"
        files.append(f)
    replaces = dict(spec.get("replaces", {}))
    rename = spec.get("rename")
    if rename is not None:
        mapping = neutral_names(files) if rename == "neutral" else dict(rename)
        for old, new in mapping.items():
            (dest / old).rename(dest / new)
            roles[new] = roles.pop(old)
        files = [mapping.get(f, f) for f in files]
        replaces = {mapping.get(k, k): mapping.get(v, v) for k, v in replaces.items()}
    if spec.get("order") == "reversed":
        files.reverse()
    return [Upload(dest / f, roles[f], replaces.get(f)) for f in files]
