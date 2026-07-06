#!/usr/bin/env python3
"""Dependency-free compatibility runner for this repository's pytest-style tests.

This is intentionally not pytest and must never be reported as an official
pytest result. It exists only for locked-down machines where pytest cannot be
installed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import re
import sys
import tempfile
import traceback
import types
from pathlib import Path
from typing import Callable, Optional


class _Raises:
    def __init__(self, error: type[BaseException], match: Optional[str] = None) -> None:
        self.error = error
        self.match = match

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, kind: object, value: object, traceback_value: object) -> bool:
        if kind is None:
            raise AssertionError(f"{self.error.__name__} was not raised")
        if not isinstance(kind, type) or not issubclass(kind, self.error):
            return False
        if self.match and not re.search(self.match, str(value)):
            raise AssertionError(f"exception did not match {self.match!r}: {value}")
        return True


class _Mark:
    def parametrize(self, names: str, values: object) -> Callable[[Callable[..., object]], Callable[..., object]]:
        def decorate(function: Callable[..., object]) -> Callable[..., object]:
            setattr(function, "__compat_params__", (names, values))
            return function
        return decorate


class _Capture:
    def __init__(self, stdout: io.StringIO, stderr: io.StringIO) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_position = 0
        self.stderr_position = 0

    def readouterr(self) -> types.SimpleNamespace:
        out = self.stdout.getvalue()[self.stdout_position :]
        err = self.stderr.getvalue()[self.stderr_position :]
        self.stdout_position = len(self.stdout.getvalue())
        self.stderr_position = len(self.stderr.getvalue())
        return types.SimpleNamespace(out=out, err=err)


class _MonkeyPatch:
    def __init__(self) -> None:
        self.undo_actions: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        self.undo_actions.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, value in reversed(self.undo_actions):
            setattr(target, name, value)


def _install_pytest_compatibility_module() -> None:
    module = types.ModuleType("pytest")
    module.mark = _Mark()  # type: ignore[attr-defined]
    module.raises = lambda *args, **kwargs: _Raises(*args, **kwargs)  # type: ignore[attr-defined]
    sys.modules["pytest"] = module


def main() -> int:
    print("COMPATIBILITY RUNNER ONLY — NOT OFFICIAL PYTEST")
    _install_pytest_compatibility_module()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    count = 0
    failures: list[str] = []
    for test_file in sorted((root / "tests").glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(test_file.stem, test_file)
        if spec is None or spec.loader is None:
            failures.append(f"{test_file}: import spec unavailable")
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            params = getattr(function, "__compat_params__", None)
            invocations: list[dict[str, object]] = [{}]
            if params:
                names, values = params
                keys = [key.strip() for key in names.split(",")]
                invocations = []
                for value in values:
                    arguments = value if isinstance(value, tuple) else (value,)
                    invocations.append(dict(zip(keys, arguments)))
            for provided in invocations:
                stdout = io.StringIO()
                stderr = io.StringIO()
                temporary: Optional[tempfile.TemporaryDirectory[str]] = None
                monkeypatch: Optional[_MonkeyPatch] = None
                kwargs = dict(provided)
                signature = inspect.signature(function)
                if "tmp_path" in signature.parameters:
                    temporary = tempfile.TemporaryDirectory()
                    kwargs["tmp_path"] = Path(temporary.name)
                if "capsys" in signature.parameters:
                    kwargs["capsys"] = _Capture(stdout, stderr)
                if "monkeypatch" in signature.parameters:
                    monkeypatch = _MonkeyPatch()
                    kwargs["monkeypatch"] = monkeypatch
                try:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        function(**kwargs)
                    count += 1
                except Exception:
                    failures.append(f"{test_file.name}::{name}\n{traceback.format_exc()}")
                finally:
                    if monkeypatch:
                        monkeypatch.undo()
                    if temporary:
                        temporary.cleanup()
    if failures:
        print(f"compatibility failures: {len(failures)}", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(f"compatibility cases passed: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
