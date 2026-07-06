#!/usr/bin/env python3
"""Safely scaffold local Mac configuration without touching source repos."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


REPO_NAME = re.compile(r"(?:loro[-_ ]?topik|lorotopik|loroexam)", re.IGNORECASE)
PERSONAL_NAMES = {
    "tokenforge", "hwangtodo", "lovey moment", "personal", "private",
    "playground", "toy", "side",
}


def discover_loro_repos(roots: Iterable[Path], *, max_depth: int = 6) -> list[Path]:
    candidates: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        for current, directories, _files in os.walk(root):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                continue
            if depth >= max_depth:
                directories[:] = []
                continue
            folded_parts = {part.casefold() for part in current_path.parts}
            if folded_parts & PERSONAL_NAMES:
                directories[:] = []
                continue
            if ".git" in directories:
                directories.remove(".git")
                if REPO_NAME.search(current_path.name):
                    candidates.add(current_path.resolve())
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and name.casefold() not in PERSONAL_NAMES
            ]
    return sorted(candidates)


def select_python() -> tuple[Optional[Path], Optional[tuple[int, int, int]]]:
    requested = os.environ.get("PYTHON_BIN")
    names = ([requested] if requested else []) + ["python3.12", "python3.11", "python3.10", "python3"]
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        executable = shutil.which(name) if not Path(name).is_absolute() else name
        if not executable:
            continue
        completed = subprocess.run(
            [str(executable), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            continue
        try:
            version = tuple(int(part) for part in completed.stdout.strip().split("."))
        except ValueError:
            continue
        if len(version) == 3:
            return Path(executable).resolve(), version  # type: ignore[return-value]
    return None, None


def scaffold_config(
    project_root: Path,
    home: Path,
    repo_candidates: Iterable[Path],
) -> tuple[Path, bool]:
    config_path = project_root / "config.local.json"
    if config_path.exists():
        return config_path, False
    example_path = project_root / "config.example.json"
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    candidates = list(repo_candidates)
    payload["timezone"] = "Asia/Seoul"
    payload["repo_paths"] = [str(candidates[0])] if len(candidates) == 1 else []
    payload["notes"] = {"enabled": False, "path": ""}
    payload["plan"] = {"enabled": False, "path": ""}
    payload.pop("outbox_path", None)
    payload["outbox_dir"] = str(home / "Documents" / "WorklogBridge" / "outbox")
    payload["log_dir"] = str(home / "Documents" / "WorklogBridge" / "logs")
    if not candidates:
        payload["setup_todo"] = "Add one verified LoroTOPIK company Git repository path."
    elif len(candidates) > 1:
        payload["setup_todo"] = "Multiple LoroTOPIK repo candidates found; choose one without guessing."
        payload["discovered_repo_candidates"] = [str(path) for path in candidates]
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with config_path.open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    return config_path, True


def create_runtime_directories(home: Path) -> tuple[list[Path], list[str]]:
    paths = [
        home / "Documents" / "WorklogBridge" / "outbox",
        home / "Documents" / "WorklogBridge" / "logs",
    ]
    errors: list[str] = []
    for path in paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return paths, errors


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    search_roots = [home / "Documents", home / "Documents" / "GitHub", home / "Desktop"]
    candidates = discover_loro_repos(search_roots)
    config_path, created = scaffold_config(project_root, home, candidates)
    runtime_paths, directory_errors = create_runtime_directories(home)
    python_path, python_version = select_python()

    print("Worklog Bridge Mac local setup")
    print(f"config: {config_path} ({'created' if created else 'preserved; not overwritten'})")
    print(f"repo candidates: {len(candidates)}")
    for candidate in candidates:
        print(f"- {candidate}")
    for path in runtime_paths:
        print(f"runtime directory: {path} ({'ready' if path.is_dir() else 'not created'})")
    if python_path and python_version:
        print(f"python: {python_path} ({'.'.join(map(str, python_version))})")
    else:
        print("python: not found")

    blocked_reasons: list[str] = []
    blocked_reasons.extend(directory_errors)
    if len(candidates) != 1:
        blocked_reasons.append(
            "exactly one verified LoroTOPIK Git repo is required; discovery did not produce one unique candidate"
        )
    if not python_version or python_version < (3, 10, 0):
        blocked_reasons.append("Python 3.10+ is required")
    if blocked_reasons:
        for reason in blocked_reasons:
            print(f"[BLOCKED] {reason}")
        return 2
    print("[OK] local scaffold ready for --preflight")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

