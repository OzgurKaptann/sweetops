#!/usr/bin/env python
"""
SweetOps release-readiness verification — READ-ONLY, no database, no network.

Purpose
-------
The real proof that SweetOps works is the pytest suite, the Alembic migrations,
the frontend builds, and the four reconcilers. This script does NOT replace any
of them. It answers a narrower, cheaper question that is easy to get wrong right
before a release:

    "Is the repository itself in a reviewable, runnable, documented state?"

That means: the runbooks a reviewer is told to read exist, the commands the
README tells them to run exist, the migration history has exactly one head, no
conflict marker survived a merge, and no obvious secret was committed.

Guarantees
----------
* READ-ONLY   — it opens files for reading and nothing else. It never writes,
                deletes, migrates, seeds, or connects to PostgreSQL/Redis.
* Offline     — no network, no Docker, no external service.
* Stdlib only — no new dependency.

Usage
-----
    python scripts/verify_release_readiness.py            # from the repo root
    python scripts/verify_release_readiness.py --json     # machine-readable
    python scripts/verify_release_readiness.py --root .   # explicit root

Exit code
---------
    0  every check passed (warnings are allowed)
    1  at least one check FAILED, or the root is not a SweetOps repository

Notes on the secret scan
------------------------
It reports only LITERAL values (a quoted string, or a bare value running to the
end of the line as in .env/YAML), because a secret can only be committed as a
literal. Values this repository documents as local-only placeholders are listed
in ``PLACEHOLDER_VALUES`` below. A single line can be exempted with a
``readiness-scan: allow`` comment that says why — see ``ALLOW_PRAGMA_RE``.

Notes on the Alembic check
--------------------------
The single-head check reads ``apps/api/alembic/versions/*.py`` and rebuilds the
revision graph with ``ast`` — it deliberately does NOT run ``alembic heads``, so
it needs no database and no configuration. ``python -m alembic heads`` from
``apps/api`` remains the authoritative check against a live database; this one
catches a second head the moment it is committed, on any machine, with the DB
down.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ── Statuses ──────────────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

_ICON = {PASS: "OK  ", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    items: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "items": self.items,
        }


# ── What a release-ready SweetOps repository must contain ─────────────────────
REQUIRED_DOCS = (
    "README.md",
    "docs/PRODUCTION_READINESS.md",
    "docs/RELEASE_CHECKLIST.md",
    "docs/OPERATIONS_RUNBOOK.md",
    "docs/PROJECT_ROADMAP.md",
    "docs/DEMO_SEED_DATA.md",
    "docs/TEST_SUITE_BASELINE.md",
    "docs/ALEMBIC_SINGLE_HEAD_RESOLUTION.md",
    "docs/STAFF_AUTH_RBAC.md",
    "docs/SECURE_QR_TABLE_CONTEXT.md",
    "docs/PAYMENT_SETTLEMENT_WORKFLOW.md",
    "docs/ORDER_ISSUE_REFUND_WORKFLOW.md",
    "docs/CASHIER_SHIFT_CLOSING.md",
    "docs/INVENTORY_LIFECYCLE.md",
    "docs/KITCHEN_PREP_TIMING_METRICS.md",
    "docs/OWNER_OPERATIONAL_DASHBOARD.md",
)

REQUIRED_SCRIPTS = (
    "scripts/seed_demo_data.py",
    "scripts/verify_release_readiness.py",
    "scripts/reconcile_payments.py",
    "scripts/reconcile_inventory.py",
    "scripts/reconcile_order_issues.py",
    "scripts/reconcile_kitchen_timing.py",
    "scripts/manage_staff_users.py",
    "scripts/manage_qr_tokens.py",
)

REQUIRED_PACKAGE_SCRIPTS = (
    "build:types",
    "build:ui",
    "seed:demo",
    "dev:customer",
    "dev:kitchen",
    "dev:owner",
    "dev:cashier",
)

REQUIRED_ENV_EXAMPLES = (
    ".env.example",
    "apps/api/.env.example",
)

ALEMBIC_VERSIONS_DIR = "apps/api/alembic/versions"

# ── File-scan configuration ───────────────────────────────────────────────────
SCANNED_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".sh", ".env", ".example",
}

SKIPPED_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "__pycache__",
    ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov", "coverage", "target",
}

MAX_SCAN_BYTES = 2_000_000

# A conflict marker only counts at the start of a line, with the trailing space
# git actually writes, so prose about merge markers (this file, the runbooks)
# is never mistaken for one.
MERGE_MARKER_RE = re.compile(r"^(<{7} |={7}$|>{7} )", re.MULTILINE)

# ── Secret scanning ───────────────────────────────────────────────────────────
# High-signal credential shapes. These are formats that have no legitimate reason
# to appear in a repository at all, so they are reported regardless of value.
HIGH_SIGNAL_PATTERNS = (
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("generic API secret key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
)

# Assignment shapes: `password = "..."`, `SECRET_KEY: "..."`, `API_TOKEN=...`.
# These DO have legitimate uses (local defaults, test fixtures, env examples), so
# a match is only reported when its value is a LITERAL (see `_is_literal_value`)
# and is not a documented placeholder. Without the literal rule the scanner
# drowns in false positives from ordinary code — `password_hash=hash_password(pw)`,
# a `password: string` TypeScript annotation, a SQLAlchemy `Column(...)` — none of
# which carry a secret.
ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(?P<key>[A-Za-z0-9_]*(?:secret|passwd|password|api[_-]?key|access[_-]?token|auth[_-]?token)[A-Za-z0-9_]*)
    \s* [:=] \s*
    (?P<quote>["']?)
    (?P<value>[^\s"'#,;)\]}]{6,})
    (?P=quote)
    """
)

# Trailing text that still counts as "the value ended the line": nothing, or a
# comment. Used to recognise env/YAML assignments, which are unquoted.
_LINE_TAIL_RE = re.compile(r"^\s*(?:[#;]|//|$)")

# Inline escape hatch, in the tradition of `# noqa` / `# nosec`.
#
# A secret scanner has to be able to write down the shapes it detects — its own
# test fixtures need a credential-shaped string, and documentation needs to show
# what a bad value looks like. Without an escape hatch the only options are to
# blind the scanner to whole files or to weaken a pattern, and both are worse.
#
# Scope: ONE line, and it must say why. Use it for a value that is demonstrably
# not a credential (a test fixture, a documented example). NEVER use it to
# silence a real finding — the fix for a committed secret is to rotate and remove
# it. `git grep "readiness-scan: allow"` audits every use in seconds.
ALLOW_PRAGMA_RE = re.compile(r"readiness-scan:\s*allow")

# Credentials embedded in a URL: postgresql://user:password@host/db
URL_CREDENTIAL_RE = re.compile(r"://(?P<user>[^:/@\s]+):(?P<value>[^@/\s]{3,})@")

# Documented, local-only placeholder values. Every entry here is either a value
# that appears in docker-compose.yml / .env.example for the local stack, a demo
# credential that is published on purpose (docs/DEMO_SEED_DATA.md), a test
# fixture constant, or an obvious "fill this in" token. Anything NOT on this list
# that matches the shapes above is reported.
PLACEHOLDER_VALUES = {
    # Local docker-compose Postgres credentials (also used by data/dbt/profiles.yml).
    "sweetops_password",
    "sweetops",
    # Published local demo credentials — see docs/DEMO_SEED_DATA.md.
    "demo1234",
    # Backend test-fixture constant — see apps/api/tests/conftest.py.
    "testpassw0rd",
    # Illustrative credentials in origin-rejection tests and auth docs, e.g.
    # "http://user:pass@localhost:3001" — a URL shape being rejected, not a login.
    "pass", "user",
    # Generic fill-me-in tokens.
    "changeme", "change-me", "change_me", "placeholder", "example",
    "password", "secret", "none", "null", "true", "false", "",
}

# Value prefixes that are self-evidently placeholders rather than real secrets.
PLACEHOLDER_PREFIXES = (
    "your-", "your_", "<", "${", "{{", "os.environ", "process.env",
    "getpass", "settings.", "replace-", "replace_", "xxx", "***",
)


def _is_literal_value(match: re.Match, line: str) -> bool:
    """
    True when the matched value is a written-down constant rather than code.

    A secret can only be committed as a literal. Two shapes qualify:

      * a quoted string  — ``DEMO_PASSWORD = "demo1234"``
      * a bare value that runs to the end of the line, which is how .env and
        YAML assignments are written — ``POSTGRES_PASSWORD: sweetops_password``

    Everything else is an expression (a call, an identifier, a type annotation,
    a dict entry) and cannot itself contain a secret.
    """
    if match.group("quote"):
        return True
    return bool(_LINE_TAIL_RE.match(line[match.end():]))


def _is_placeholder(value: str) -> bool:
    # Documentation writes values as `code spans`, and prose ends in punctuation;
    # neither changes what the value IS, so strip both before comparing.
    lowered = value.strip().strip("\"'`*_,.;:)").lower()
    if lowered in PLACEHOLDER_VALUES:
        return True
    if any(lowered.startswith(p) for p in PLACEHOLDER_PREFIXES):
        return True
    # A value made only of placeholder punctuation/repeats carries no secret.
    if len(set(lowered)) <= 2:
        return True
    return False


# ── Repository file discovery ─────────────────────────────────────────────────
def _git_tracked_files(root: Path) -> list[Path] | None:
    """
    Files git knows about, or None when git/the repo is unavailable.

    Tracked files PLUS untracked-but-not-ignored ones (``--others
    --exclude-standard``). A secret is most dangerous in the window before it is
    committed, so a file that is staged-to-be-added must be scanned too; ignored
    files (real ``.env``, node_modules, build output) are correctly excluded.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
    return [root / n for n in names]


def _walked_files(root: Path) -> list[Path]:
    """Filesystem fallback when the root is not a git checkout (e.g. a temp dir)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def _scannable(paths: Iterable[Path], root: Path) -> list[Path]:
    keep: list[Path] = []
    for path in paths:
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIPPED_DIRS for part in rel_parts[:-1]):
            continue
        if path.suffix.lower() not in SCANNED_SUFFIXES and not path.name.startswith(".env"):
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        keep.append(path)
    return keep


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ── Checks ────────────────────────────────────────────────────────────────────
def check_required_files(root: Path, label: str, required: Iterable[str]) -> Result:
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        return Result(label, FAIL, f"{len(missing)} missing", sorted(missing))
    return Result(label, PASS, f"all {len(tuple(required))} present")


def check_package_scripts(root: Path) -> Result:
    pkg = root / "package.json"
    if not pkg.is_file():
        return Result("package scripts", FAIL, "package.json not found")
    try:
        data = json.loads(_read_text(pkg))
    except json.JSONDecodeError as exc:
        return Result("package scripts", FAIL, f"package.json is not valid JSON: {exc}")
    scripts = data.get("scripts") or {}
    missing = [name for name in REQUIRED_PACKAGE_SCRIPTS if name not in scripts]
    if missing:
        return Result("package scripts", FAIL, f"{len(missing)} missing", sorted(missing))
    return Result("package scripts", PASS, f"all {len(REQUIRED_PACKAGE_SCRIPTS)} present")


def _revision_ids(source: str) -> tuple[str | None, list[str]]:
    """(revision, down_revisions) from a migration's module-level assignments."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, []

    values: dict[str, ast.expr] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value

    def _strings(node: ast.expr | None) -> list[str]:
        if node is None:
            return []
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out: list[str] = []
            for element in node.elts:
                out.extend(_strings(element))
            return out
        return []

    revisions = _strings(values.get("revision"))
    return (revisions[0] if revisions else None), _strings(values.get("down_revision"))


def check_alembic_single_head(root: Path) -> Result:
    versions = root / ALEMBIC_VERSIONS_DIR
    if not versions.is_dir():
        return Result(
            "alembic single head", SKIP,
            f"{ALEMBIC_VERSIONS_DIR} not found (not an API checkout)",
        )
    files = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    if not files:
        return Result("alembic single head", FAIL, "no migration files found")

    revisions: set[str] = set()
    parents: set[str] = set()
    unparsed: list[str] = []
    for path in files:
        revision, downs = _revision_ids(_read_text(path))
        if revision is None:
            unparsed.append(_rel(path, root))
            continue
        revisions.add(revision)
        parents.update(downs)

    if unparsed:
        return Result(
            "alembic single head", FAIL,
            "could not read a revision id from these migrations", sorted(unparsed),
        )

    heads = sorted(revisions - parents)
    if len(heads) == 1:
        return Result("alembic single head", PASS, f"head {heads[0]} ({len(revisions)} revisions)")
    return Result(
        "alembic single head", FAIL,
        f"{len(heads)} heads — the history has diverged "
        "(see docs/ALEMBIC_SINGLE_HEAD_RESOLUTION.md)",
        heads,
    )


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def check_doc_links(root: Path) -> Result:
    """Every relative markdown link in the top-level docs must resolve on disk."""
    doc_paths = [root / "README.md"] + sorted((root / "docs").glob("*.md"))
    broken: list[str] = []
    checked = 0
    for doc in doc_paths:
        if not doc.is_file():
            continue
        for target in _MD_LINK_RE.findall(_read_text(doc)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            resolved = (doc.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                broken.append(f"{_rel(doc, root)} -> {target}")
    if broken:
        return Result("doc links", FAIL, f"{len(broken)} broken", sorted(set(broken)))
    return Result("doc links", PASS, f"{checked} relative links resolve")


def check_merge_markers(root: Path, files: list[Path]) -> Result:
    hits: list[str] = []
    for path in files:
        text = _read_text(path)
        match = MERGE_MARKER_RE.search(text)
        if match:
            line = text[: match.start()].count("\n") + 1
            hits.append(f"{_rel(path, root)}:{line}")
    if hits:
        return Result("merge markers", FAIL, f"{len(hits)} unresolved", sorted(hits))
    return Result("merge markers", PASS, f"none in {len(files)} scanned files")


def check_committed_secrets(root: Path, files: list[Path]) -> Result:
    hits: list[str] = []
    for path in files:
        rel = _rel(path, root)
        text = _read_text(path)

        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW_PRAGMA_RE.search(line):
                continue

            for label, pattern in HIGH_SIGNAL_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno} — {label}")

            stripped = line.strip()
            # Prose and comments describe secrets; they do not carry them.
            if stripped.startswith(("#", "//", "*", ">", "-----")):
                continue
            for match in ASSIGNMENT_RE.finditer(line):
                if not _is_literal_value(match, line):
                    continue
                if not _is_placeholder(match.group("value")):
                    hits.append(
                        f"{rel}:{lineno} — {match.group('key')} has a non-placeholder "
                        "literal value"
                    )
            for match in URL_CREDENTIAL_RE.finditer(line):
                if not _is_placeholder(match.group("value")):
                    hits.append(f"{rel}:{lineno} — credential embedded in a URL")

    if hits:
        return Result(
            "committed secrets", FAIL,
            f"{len(hits)} candidate(s) — review each, then either remove it or add "
            "the documented placeholder to PLACEHOLDER_VALUES",
            sorted(set(hits)),
        )
    return Result("committed secrets", PASS, f"none in {len(files)} scanned files")


def check_git_available(root: Path) -> Result:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return Result("git", WARN, "git is not available — file scan used the filesystem")
    if out.returncode != 0:
        return Result("git", WARN, "not a git checkout — file scan used the filesystem")
    branch = out.stdout.decode("utf-8", "replace").strip()
    return Result("git", PASS, f"on branch {branch}")


# ── Driver ────────────────────────────────────────────────────────────────────
def run_checks(root: Path) -> list[Result]:
    tracked = _git_tracked_files(root)
    files = _scannable(tracked if tracked is not None else _walked_files(root), root)

    return [
        check_git_available(root),
        check_required_files(root, "required docs", REQUIRED_DOCS),
        check_required_files(root, "required scripts", REQUIRED_SCRIPTS),
        check_required_files(root, "env examples", REQUIRED_ENV_EXAMPLES),
        check_package_scripts(root),
        check_alembic_single_head(root),
        check_doc_links(root),
        check_merge_markers(root, files),
        check_committed_secrets(root, files),
    ]


def _print_human(results: list[Result], root: Path) -> None:
    print(f"SweetOps release-readiness verification — {root}")
    print("(read-only: no database, no network, no writes)\n")
    width = max(len(r.name) for r in results)
    for result in results:
        print(f"  [{_ICON[result.status]}] {result.name.ljust(width)}  {result.detail}")
        for item in result.items:
            print(f"           - {item}")
    failed = [r for r in results if r.status == FAIL]
    print()
    if failed:
        print(f"FAILED — {len(failed)} of {len(results)} checks did not pass.")
    else:
        warned = sum(1 for r in results if r.status in (WARN, SKIP))
        suffix = f" ({warned} warning/skipped)" if warned else ""
        print(f"OK — all {len(results)} checks passed{suffix}.")
        print("This is a repository-state check only. It does not replace pytest,")
        print("the Alembic migrations, the frontend builds, or the reconcilers.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only SweetOps release-readiness verification.",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root to verify (defaults to this script's repository).",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: --root is not a directory: {root}", file=sys.stderr)
        return 1

    results = run_checks(root)

    if args.json:
        print(json.dumps(
            {
                "root": str(root),
                "ok": not any(r.status == FAIL for r in results),
                "checks": [r.as_dict() for r in results],
            },
            indent=2,
            ensure_ascii=False,
        ))
    else:
        _print_human(results, root)

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
