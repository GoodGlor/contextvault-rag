"""Tests for the bootstrap CLI's credential resolution."""

from types import SimpleNamespace

import pytest

from contextvault.cli import resolve_admin_credentials


def _settings(username: str | None, password: str | None) -> SimpleNamespace:
    return SimpleNamespace(initial_admin_username=username, initial_admin_password=password)


def test_cli_args_take_precedence() -> None:
    args = SimpleNamespace(username="cli", password="clipw")
    username, password = resolve_admin_credentials(args, _settings("env", "envpw"))
    assert (username, password) == ("cli", "clipw")


def test_falls_back_to_settings() -> None:
    args = SimpleNamespace(username=None, password=None)
    username, password = resolve_admin_credentials(args, _settings("env", "envpw"))
    assert (username, password) == ("env", "envpw")


def test_missing_credentials_raise() -> None:
    args = SimpleNamespace(username=None, password=None)
    with pytest.raises(SystemExit):
        resolve_admin_credentials(args, _settings(None, None))
