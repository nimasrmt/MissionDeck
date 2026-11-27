"""Validation command allowlist."""

from __future__ import annotations

import pytest

from app.execution import AppError, parse_validation_command


def test_pytest_form() -> None:
    argv = parse_validation_command("python -m pytest tests/test_example.py -q")
    assert argv[1:5] == ["-m", "pytest", "tests/test_example.py", "-q"]


def test_python_script_form() -> None:
    argv = parse_validation_command("python src/example.py")
    assert argv[-1].replace("\\", "/").endswith("src/example.py")


def test_rejects_shell_and_other() -> None:
    with pytest.raises(AppError):
        parse_validation_command("pytest tests -q")
    with pytest.raises(AppError):
        parse_validation_command("python -m pytest tests -q && rm -rf /")
    with pytest.raises(AppError):
        parse_validation_command("python -c 'print(1)'")
