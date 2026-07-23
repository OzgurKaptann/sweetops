"""
Tests for scripts/verify_release_readiness.py — the read-only readiness check.

The script's whole value is that a reviewer can trust its verdict, so the two
ways it could betray them are what get tested here:

  * a FALSE PASS — it says the repository is fine while a doc is missing, a
    second Alembic head exists, a conflict marker survived a merge, or a secret
    was committed;
  * a FALSE FAIL — it cries wolf over the placeholders this repository documents
    on purpose (the Compose Postgres password, the published demo password, the
    test-fixture password) or over ordinary code that merely *mentions* a
    password, which would train everyone to ignore it.

Every construction test builds a throwaway repository under pytest's ``tmp_path``
and points the script at it with ``--root``, so nothing here depends on a local
absolute path or on the state of the real checkout. Exactly one test looks at the
real repository, and it asserts the property the release actually cares about:
this repository passes.

The script is read-only by contract — it opens files and nothing else — so these
tests need no database, no fixtures, and no teardown.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_release_readiness.py"


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_release_readiness", VERIFY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load_verify_module()


def _result(results, name):
    """The single Result with this check name."""
    matches = [r for r in results if r.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} check, got {len(matches)}"
    return matches[0]


# ── A minimal repository that passes every check ─────────────────────────────
def _make_passing_repo(root: Path) -> Path:
    """
    Build the smallest tree the script considers release-ready.

    Deliberately minimal: it contains only what the script asserts, so a test
    that fails points at the script's contract rather than at scaffolding.
    """
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "api" / "alembic" / "versions").mkdir(parents=True, exist_ok=True)

    for name in verify.REQUIRED_DOCS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n", encoding="utf-8")
    for name in verify.REQUIRED_SCRIPTS:
        (root / name).write_text("# placeholder script\n", encoding="utf-8")
    for name in verify.REQUIRED_ENV_EXAMPLES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# example\n", encoding="utf-8")

    scripts = ",\n".join(f'    "{s}": "echo {s}"' for s in verify.REQUIRED_PACKAGE_SCRIPTS)
    (root / "package.json").write_text(
        '{\n  "name": "tmp",\n  "scripts": {\n' + scripts + "\n  }\n}\n",
        encoding="utf-8",
    )

    _write_migration(root, "aaaa1111", None)
    _write_migration(root, "bbbb2222", "aaaa1111")
    return root


def _write_migration(root: Path, revision: str, down: str | None) -> Path:
    """A migration file in the real style: annotated, module-level assignments."""
    down_literal = "None" if down is None else f"'{down}'"
    path = root / "apps" / "api" / "alembic" / "versions" / f"{revision}_test.py"
    path.write_text(
        "from typing import Sequence, Union\n\n"
        f"revision: str = '{revision}'\n"
        f"down_revision: Union[str, None] = {down_literal}\n"
        "branch_labels: Union[str, Sequence[str], None] = None\n\n"
        "def upgrade() -> None:\n    pass\n\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _make_passing_repo(tmp_path / "repo")


# ── Baseline: the synthetic repo passes, so later failures mean something ─────
def test_minimal_repo_passes_every_check(repo: Path):
    results = verify.run_checks(repo)
    failed = [(r.name, r.detail, r.items) for r in results if r.status == verify.FAIL]
    assert failed == [], f"unexpected failures in a clean synthetic repo: {failed}"


def test_exit_code_is_zero_when_everything_passes(repo: Path, capsys):
    assert verify.main(["--root", str(repo)]) == 0
    capsys.readouterr()


# ── Required files ───────────────────────────────────────────────────────────
def test_missing_doc_is_detected(repo: Path):
    (repo / "docs" / "OPERATIONS_RUNBOOK.md").unlink()

    result = _result(verify.run_checks(repo), "required docs")
    assert result.status == verify.FAIL
    assert "docs/OPERATIONS_RUNBOOK.md" in result.items


def test_missing_script_is_detected(repo: Path):
    (repo / "scripts" / "reconcile_payments.py").unlink()

    result = _result(verify.run_checks(repo), "required scripts")
    assert result.status == verify.FAIL
    assert "scripts/reconcile_payments.py" in result.items


def test_missing_env_example_is_detected(repo: Path):
    (repo / "apps" / "api" / ".env.example").unlink()

    result = _result(verify.run_checks(repo), "env examples")
    assert result.status == verify.FAIL
    assert "apps/api/.env.example" in result.items


def test_missing_package_script_is_detected(repo: Path):
    (repo / "package.json").write_text('{"scripts": {"build:types": "x"}}\n', encoding="utf-8")

    result = _result(verify.run_checks(repo), "package scripts")
    assert result.status == verify.FAIL
    assert "seed:demo" in result.items


def test_a_missing_file_fails_the_whole_run(repo: Path, capsys):
    (repo / "docs" / "PRODUCTION_READINESS.md").unlink()

    assert verify.main(["--root", str(repo)]) == 1
    capsys.readouterr()


# ── Alembic head detection ───────────────────────────────────────────────────
def test_single_head_is_reported_with_its_revision(repo: Path):
    result = _result(verify.run_checks(repo), "alembic single head")
    assert result.status == verify.PASS
    assert "bbbb2222" in result.detail


def test_second_head_is_detected(repo: Path):
    # A second leaf hanging off the same parent — exactly what two branches each
    # adding a migration produces.
    _write_migration(repo, "cccc3333", "aaaa1111")

    result = _result(verify.run_checks(repo), "alembic single head")
    assert result.status == verify.FAIL
    assert sorted(result.items) == ["bbbb2222", "cccc3333"]


def test_merge_revision_restores_a_single_head(repo: Path):
    _write_migration(repo, "cccc3333", "aaaa1111")
    # A merge revision names BOTH parents in a tuple, spanning lines as the real
    # merge migration in this repository does.
    (repo / "apps" / "api" / "alembic" / "versions" / "dddd4444_merge.py").write_text(
        "from typing import Sequence, Union\n\n"
        "revision: str = 'dddd4444'\n"
        "down_revision: Union[str, None] = (\n"
        "    'bbbb2222', 'cccc3333')\n",
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "alembic single head")
    assert result.status == verify.PASS
    assert "dddd4444" in result.detail


def test_alembic_check_skips_when_there_is_no_api_checkout(tmp_path: Path):
    bare = tmp_path / "bare"
    bare.mkdir()

    result = _result(verify.run_checks(bare), "alembic single head")
    assert result.status == verify.SKIP


# ── Merge markers ────────────────────────────────────────────────────────────
def test_merge_marker_is_detected(repo: Path):
    (repo / "docs" / "PROJECT_ROADMAP.md").write_text(
        "# Roadmap\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "merge markers")
    assert result.status == verify.FAIL
    assert any("docs/PROJECT_ROADMAP.md:2" in item for item in result.items)


def test_prose_about_merge_markers_is_not_a_merge_marker(repo: Path):
    # The runbooks tell people to look for conflict markers. Describing one must
    # not trip the scanner, or the docs cannot discuss their own check.
    (repo / "docs" / "OPERATIONS_RUNBOOK.md").write_text(
        "Look for a line starting with <<<<<<< followed by the branch name,\n"
        "then ======= and >>>>>>> at the end of the conflicted region.\n",
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "merge markers")
    assert result.status == verify.PASS


# ── Secret scanning: false negatives ─────────────────────────────────────────
def test_committed_aws_key_is_detected(repo: Path):
    (repo / "scripts" / "manage_qr_tokens.py").write_text(
        'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n',  # readiness-scan: allow — AWS's own published example key, used as a fixture
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.FAIL
    assert any("AWS access key id" in item for item in result.items)


def test_private_key_block_is_detected(repo: Path):
    (repo / "docs" / "DEMO_SEED_DATA.md").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n",  # readiness-scan: allow — header text only, there is no key here
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.FAIL
    assert any("private key block" in item for item in result.items)


def test_real_looking_password_literal_is_detected(repo: Path):
    (repo / ".env.example").write_text(
        "DATABASE_PASSWORD=Tr0ub4dor-and-3-horses\n", encoding="utf-8"
    )

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.FAIL
    assert any(".env.example:1" in item for item in result.items)


def test_credential_embedded_in_a_url_is_detected(repo: Path):
    (repo / ".env.example").write_text(
        "DATABASE_URL=postgresql://admin:Hunter2Hunter2@db.example.com:5432/app\n",  # readiness-scan: allow — invented host and password, used as a fixture
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.FAIL
    assert any("credential embedded in a URL" in item for item in result.items)


# ── Secret scanning: false positives ─────────────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        # Documented local-only placeholders — see PLACEHOLDER_VALUES.
        "POSTGRES_PASSWORD=sweetops_password",
        "  password: sweetops_password",
        'DEMO_PASSWORD = "demo1234"  # LOCAL/DEMO ONLY',
        'DEFAULT_PASSWORD = "testpassw0rd"',
        "DATABASE_URL=postgresql://sweetops:sweetops_password@localhost:5432/sweetops_db",
        # Fill-me-in tokens.
        "API_KEY=your-api-key-here",
        "SECRET_TOKEN=<replace-me>",
        # Ordinary code that mentions a password but carries no literal.
        "    password_hash=hash_password(password),",
        "  password: string,",
        "    password_changed_at = Column(DateTime(timezone=True), nullable=True)",
        "            password = _prompt_password()",
        # An illustrative URL shape in a test/doc, not a login.
        '        "http://user:pass@localhost:3001",  # embedded credentials',
    ],
)
def test_documented_placeholders_and_code_are_not_flagged(repo: Path, line: str):
    (repo / "scripts" / "seed_demo_data.py").write_text(line + "\n", encoding="utf-8")

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.PASS, f"false positive on: {line!r} -> {result.items}"


def test_allow_pragma_exempts_only_its_own_line(repo: Path):
    """
    The escape hatch must be line-scoped. A pragma on one line exempting the
    whole file would be a silent blind spot, which is worse than no pragma.
    """
    # Composed from parts so this test's own source carries no matchable literal:
    # the second written line must be reported, so it cannot itself be exempted.
    key = "AKIA" + "Z" * 16
    (repo / "scripts" / "manage_staff_users.py").write_text(
        f'EXAMPLE = "{key}"  # readiness-scan: allow — illustration\n'
        f'REAL = "{key}"\n',
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "committed secrets")
    assert result.status == verify.FAIL
    assert result.items == ["scripts/manage_staff_users.py:2 — AWS access key id"]


# ── Doc links ────────────────────────────────────────────────────────────────
def test_broken_relative_doc_link_is_detected(repo: Path):
    (repo / "docs" / "PRODUCTION_READINESS.md").write_text(
        "See [the runbook](DOES_NOT_EXIST.md).\n", encoding="utf-8"
    )

    result = _result(verify.run_checks(repo), "doc links")
    assert result.status == verify.FAIL
    assert any("DOES_NOT_EXIST.md" in item for item in result.items)


def test_external_and_anchor_links_are_not_checked(repo: Path):
    (repo / "docs" / "PRODUCTION_READINESS.md").write_text(
        "[web](https://example.com/nope) [anchor](#section) "
        "[mail](mailto:a@b.c) [ok](RELEASE_CHECKLIST.md#1-branch-state)\n",
        encoding="utf-8",
    )

    result = _result(verify.run_checks(repo), "doc links")
    assert result.status == verify.PASS


# ── Read-only contract ───────────────────────────────────────────────────────
def test_running_the_script_mutates_nothing(repo: Path, capsys):
    def snapshot():
        return {
            p.relative_to(repo).as_posix(): (p.stat().st_size, p.read_bytes())
            for p in sorted(repo.rglob("*")) if p.is_file()
        }

    before = snapshot()
    verify.main(["--root", str(repo)])
    capsys.readouterr()

    assert snapshot() == before


# ── CLI surface ──────────────────────────────────────────────────────────────
def test_json_output_is_machine_readable(repo: Path, capsys):
    import json

    assert verify.main(["--root", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert {c["check"] for c in payload["checks"]} >= {
        "required docs", "required scripts", "alembic single head",
        "merge markers", "committed secrets",
    }


def test_nonexistent_root_is_reported_not_crashed(tmp_path: Path, capsys):
    assert verify.main(["--root", str(tmp_path / "nope")]) == 1
    assert "not a directory" in capsys.readouterr().err


# ── The real repository ──────────────────────────────────────────────────────
def test_this_repository_is_release_ready():
    """The property the release cares about: the real checkout passes."""
    completed = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
