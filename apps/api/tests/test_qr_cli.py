"""
CLI tests for scripts/manage_qr_tokens.py — scenarios 39-43.

These load the CLI module by path and drive its `main()` entry point, asserting
the raw token is printed exactly once, only its hash is persisted, listing never
leaks raw tokens, revoke disables resolution and rotate mints a fresh token.

Requires the real database (the CLI opens its own SessionLocal against the
configured DATABASE_URL, i.e. the same PostgreSQL the API uses).
"""
import importlib.util
import os
import re

import pytest

from app.services import qr_token_service as svc
from app.models.table_qr_token import TableQrToken
from tests.conftest import (
    make_store_table,
    make_store_table_token,
    cleanup_store_table,
)


# ── Load the CLI module by path (it lives under repo-root/scripts) ───────────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", "..", ".."))
_CLI_PATH = os.path.join(_REPO_ROOT, "scripts", "manage_qr_tokens.py")

_spec = importlib.util.spec_from_file_location("manage_qr_tokens", _CLI_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


_RAW_RE = re.compile(r"raw token\s*:\s*(\S+)")


def _extract_raw(output: str) -> str:
    m = _RAW_RE.search(output)
    assert m, f"no raw token found in output:\n{output}"
    return m.group(1)


def test_issue_prints_raw_token_exactly_once_and_stores_only_hash(db, capsys):
    # A fresh table with no token yet — the CLI mints the first one.
    store, table = make_store_table(db)
    try:
        rc = cli.main(["issue", "--table-id", str(table.id)])
        assert rc == 0
        out = capsys.readouterr().out

        raw = _extract_raw(out)                       # scenario 39
        # Printed as a single issuance event: exactly one "raw token" line. The
        # same token also appears inside the printed customer URL (expected).
        assert len(_RAW_RE.findall(out)) == 1
        # Blocker 1 — the customer URL delivers the token in the FRAGMENT, never
        # a query string.
        assert f"/#qr={raw}" in out
        assert f"?qr={raw}" not in out

        # scenario 40 — DB stores only the hash, never the raw token.
        db.expire_all()
        row = (
            db.query(TableQrToken)
            .filter(TableQrToken.token_hash == svc.hash_token(raw))
            .first()
        )
        assert row is not None
        assert row.token_hash != raw
        assert db.query(TableQrToken).filter(
            TableQrToken.token_hash == raw
        ).first() is None
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_issue_rejected_when_active_token_exists(db, capsys):
    # One-active-token invariant surfaced at the CLI: a second issue fails and
    # points the operator at rotate.
    store, table, record, raw = make_store_table_token(db)
    try:
        rc = cli.main(["issue", "--table-id", str(table.id)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "already has an ACTIVE token" in err
        assert "rotate --token-id" in err
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_list_never_prints_raw_tokens(db, capsys):
    store, table, record, raw = make_store_table_token(db)
    try:
        rc = cli.main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert raw not in out                          # scenario 41
        assert record.token_prefix in out              # prefix is safe to show
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_revoke_by_id_disables_resolution(db, capsys):
    store, table, record, raw = make_store_table_token(db)
    try:
        # touch=False so this read holds no row lock the CLI's separate
        # session would deadlock behind.
        assert svc.resolve_token(db, raw, touch=False) is not None
        db.rollback()
        rc = cli.main(["revoke", "--token-id", str(record.id)])
        assert rc == 0
        db.expire_all()
        assert svc.resolve_token(db, raw, touch=False) is None  # scenario 42
    finally:
        db.rollback()
        cleanup_store_table(db, store.id, table.id)


def test_revoke_unknown_id_makes_no_change(db, capsys):
    # A destructive op with no exact match reports an error and changes nothing.
    rc = cli.main(["revoke", "--token-id", "2000000000"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_rotate_by_id_produces_a_different_raw_token(db, capsys):
    store, table, record, raw = make_store_table_token(db)
    try:
        first_raw = raw
        cli.main(["rotate", "--token-id", str(record.id)])
        second_raw = _extract_raw(capsys.readouterr().out)

        assert first_raw != second_raw                 # scenario 43
        db.expire_all()
        assert svc.resolve_token(db, first_raw, touch=False) is None
        assert svc.resolve_token(db, second_raw, touch=False) is not None
    finally:
        db.rollback()
        cleanup_store_table(db, store.id, table.id)
