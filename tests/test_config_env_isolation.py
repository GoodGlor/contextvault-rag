"""Settings .env isolation.

Tests read settings from real environment variables + code defaults only — never a
developer's local ``.env`` — so a local override can't bleed into the suite. The
mechanism is a per-instance / configurable ``env_file``: pointing ``Settings`` at a
file loads it; disabling it (the test configuration) falls back to the defaults.
"""

from pathlib import Path

from contextvault.core.config import Settings


def test_settings_reads_a_pointed_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("OPENROUTER_MODEL=from-the-env-file\n", encoding="utf-8")

    loaded = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert loaded.openrouter_model == "from-the-env-file"


def test_settings_ignores_env_file_when_disabled(tmp_path: Path) -> None:
    # A file that WOULD override the default is ignored when env-file loading is off,
    # so the code default stands — this is how the test process stays isolated.
    env_file = tmp_path / "custom.env"
    env_file.write_text("OPENROUTER_MODEL=should-be-ignored\n", encoding="utf-8")

    disabled = Settings(_env_file=None)  # type: ignore[call-arg]
    assert disabled.openrouter_model == "openai/gpt-4o"
