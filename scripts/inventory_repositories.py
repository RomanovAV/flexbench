#!/usr/bin/env python3
"""Inventory local Git repositories without executing project code (Python 3.9+)."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


SKIP = {"node_modules", "vendor", "venv", "build", "dist", "target", "artifacts"}
MARKERS = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "requirements.txt", "uv.lock", "poetry.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle.kts",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile",
    "Makefile", "Dockerfile", "compose.yaml", "docker-compose.yml",
    "pytest.ini", "tox.ini", ".gitlab-ci.yml", "Jenkinsfile", "AGENTS.md",
}


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True, text=True, errors="replace", timeout=30, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, result.stderr.strip()[:1000]
    return result.stdout, None


def discover(root, depth, warnings):
    def onerror(error):
        warnings.append(str(error))

    for current, dirs, _ in os.walk(root, followlinks=False, onerror=onerror):
        here = Path(current)
        if (here / ".git").exists():
            yield here
            dirs[:] = []  # Nested repositories/submodules are outside this inventory.
            continue
        level = len(here.relative_to(root).parts)
        dirs[:] = sorted(
            d for d in dirs
            if level < depth and not d.startswith(".") and d not in SKIP
            and not (here / d).is_symlink()
        )


def inspect(repo, commits):
    record = {"path": str(repo), "errors": {}}

    def query(label, *args):
        output, error = git(repo, *args)
        if error is not None:
            record["errors"][label] = error
        return output

    head = query("head", "rev-parse", "--verify", "HEAD")
    record["head"] = head.strip() if head else None
    branch = query("branch", "rev-parse", "--abbrev-ref", "HEAD") if head else None
    record["branch"] = branch.strip() if branch else None
    status = query("status", "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    record["dirty"] = bool(status) if status is not None else None
    listed = query("files", "ls-files", "-z")
    files = listed.rstrip("\0").split("\0") if listed else []
    record["tracked_file_count"] = len(files) if listed is not None else None
    extensions = Counter(Path(name).suffix.lower() or "[no extension]" for name in files)
    record["top_extensions"] = dict(extensions.most_common(20))
    markers = sorted(name for name in files if (
        Path(name).name in MARKERS
        or Path(name).name.lower().startswith(("readme", "test_", "vitest.config", "jest.config", "playwright.config"))
        or name.startswith((".github/workflows/", "openspec/"))
    ))
    record["inspection_candidates"] = markers[:100]
    record["inspection_candidates_total"] = len(markers)
    record["recent_commits"] = []
    if head and commits:
        log = query("history", "log", "--no-show-signature", "--max-count=" + str(commits),
                    "--format=%H%x00%aI%x00%s")
        for line in (log or "").splitlines():
            parts = line.split("\0", 2)
            if len(parts) == 3:
                record["recent_commits"].append(dict(zip(("sha", "author_date", "subject"), parts)))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, help="Repeat for each work-machine root")
    parser.add_argument("--output", type=Path, required=True, help="New local JSON file; will not overwrite")
    parser.add_argument("--depth", type=nonnegative, default=3, help="Directory levels below roots (default: 3)")
    parser.add_argument("--commits", type=nonnegative, default=20, help="Recent commit subjects per repo; 0 disables")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new filename")
    warnings, repos, seen = [], [], set()
    roots = [Path(root).expanduser().resolve() for root in args.root]
    for root in roots:
        if not root.is_dir():
            warnings.append("Missing or non-directory root: " + str(root))
            continue
        for repo in discover(root, args.depth, warnings):
            if repo not in seen:
                seen.add(repo)
                repos.append(inspect(repo, args.commits))
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "roots": [str(root) for root in roots],
        "max_depth": args.depth,
        "scope": "Git working trees only; no symlink traversal or nested repos; tracked filenames, not contents",
        "warnings": warnings,
        "repositories": sorted(repos, key=lambda item: item["path"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=True, indent=2)
        target.write("\n")
    print("Wrote {} repositories to {}".format(len(repos), args.output))
    if warnings or any(repo["errors"] for repo in repos) or not repos:
        print("Inventory incomplete or empty: inspect warnings and repository errors.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
