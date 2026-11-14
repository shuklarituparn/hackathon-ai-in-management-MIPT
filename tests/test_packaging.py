"""T12: the environment example and Compose agree, and the example holds no secret."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from sverka.harness import LIMITS_ENV_PREFIX, HarnessLimits
from sverka.model_adapter import ProviderConfig
from sverka.run_service import check_limits

ROOT = Path(__file__).resolve().parents[1]


def example() -> dict[str, str]:
    pairs = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.partition("=")
            pairs[name.strip()] = value.strip()
    return pairs


def test_the_example_has_placeholders_only():
    values = example()
    assert values["SVERKA_PROVIDER_API_KEY"] == ""
    assert not [n for n, v in values.items() if re.search(r"_KEY$|_TOKEN$|SECRET|PASSWORD", n) and v]


def test_compose_forwards_every_setting_of_the_example():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not [name for name in example() if f"${{{name}" not in compose]


def test_the_example_names_real_settings_and_loads():
    values = example()
    limit_names = {LIMITS_ENV_PREFIX + f.name.upper() for f in fields(HarnessLimits)}
    assert {n for n in values if n.startswith(LIMITS_ENV_PREFIX)} <= limit_names
    # Filled in as written, plus a key, the example is a valid provider configuration with the check's limits.
    config = ProviderConfig.from_env({**values, "SVERKA_PROVIDER_API_KEY": "placeholder"})
    assert (config.missing(), config.temperature) == ([], None)
    assert check_limits(values) == check_limits({})


def test_the_image_limits_tesseract_threads():
    """Parallel Tesseract processes stall without it (docs/release-check.md)."""
    assert "OMP_THREAD_LIMIT=1" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
