# Test Suite Baseline Restoration

Branch: `fix/test-suite-baseline`
Base commit: `dc7877b` (Merge #7 — secure QR table context, staff auth, store-scoped RBAC, WebSocket origin protection)

This document records the repair of the SweetOps API test baseline from **7 failing
tests** to a **fully green suite (0 failed)**, without adding product functionality,
migrations, or dependency upgrades.

---

## 1. Original seven failures

Baseline on `dc7877b`: **7 failed, 404 passed, 4 warnings** (411 collected).

| # | Test | Kind |
|---|------|------|
| 1 | `tests/test_kitchen_rt.py::test_broadcast_reaches_multiple_clients[trio]`   | Trio backend |
| 2 | `tests/test_kitchen_rt.py::test_broadcast_is_store_partitioned[trio]`        | Trio backend |
| 3 | `tests/test_kitchen_rt.py::test_dead_socket_removed_after_broadcast[trio]`   | Trio backend |
| 4 | `tests/test_kitchen_rt.py::test_all_dead_sockets_cleaned_no_crash[trio]`     | Trio backend |
| 5 | `tests/test_kitchen_rt.py::test_broadcast_with_zero_connections_is_noop[trio]` | Trio backend |
| 6 | `tests/test_kitchen_rt.py::test_disconnect_is_idempotent[trio]`              | Trio backend |
| 7 | `tests/test_owner_decisions.py::TestGetOwnerDecisions::test_envelope_structure` | Decision envelope (`assert 6 == 5`) |

Failures 1–6 share the identical error:
`KeyError: 'anyio._backends._trio'` raised from `anyio._core._eventloop.get_async_backend`.

---

## 2. Root cause of the Trio parametrization

The six failing tests are the six `@pytest.mark.anyio` async tests in
`test_kitchen_rt.py` (the WebSocket-manager lifecycle tests).

AnyIO ships a pytest plugin (pulled in transitively via `starlette`/`fastapi` →
`anyio==4.2.0`). When an `@pytest.mark.anyio` test runs and **no `anyio_backend`
fixture is defined**, the plugin's built-in default parametrizes each async test
across **both** backends — `asyncio` **and** `trio`:

```
tests/test_kitchen_rt.py::test_broadcast_reaches_multiple_clients[asyncio] PASSED
tests/test_kitchen_rt.py::test_broadcast_reaches_multiple_clients[trio]    FAILED
```

The `[asyncio]` variant passes. The `[trio]` variant fails at collection/run time
because **`trio` is not installed** — `anyio._backends._trio` cannot be imported,
so `get_async_backend("trio")` raises `KeyError`. There was no `pytest.ini`,
`pyproject.toml`, `setup.cfg`, or `anyio_backend` fixture anywhere in the repo to
constrain the backend, so the trio variants were being generated silently.

---

## 3. Supported async runtime decision — **asyncio only**

**SweetOps is an asyncio-only runtime. Trio is not part of the product contract.**
Approach **A** (asyncio-only test contract) was chosen. Evidence:

- **Database driver is synchronous:** `psycopg2` / `postgresql://…` via a plain
  synchronous SQLAlchemy `create_engine` (`app/core/db.py`). No async driver.
- **Server loop is asyncio:** the app is served by uvicorn's asyncio loop
  (`requirements.txt`: `uvicorn[standard]`, `fastapi`). No `trio`/`hypercorn`.
- **Own async code targets asyncio:** `tests/test_ws_auth.py` uses `asyncio.run()`
  directly; there is no `sniffio`/`trio`-aware branching anywhere.
- **Zero Trio references:** `git grep -n "trio"` over the whole repo returns nothing.
  Trio appears in **no** dependency file, source module, or doc.
- **Not installed:** `trio` is absent from the environment; the only reason it ran
  was the AnyIO plugin's implicit dual-backend default.

Approach B (genuine cross-backend Trio support) was rejected: it would require
adding a `trio` dependency and proving the sync-DB / asyncio code path actually
works under Trio, which the product neither needs nor claims.

---

## 4. Why the chosen fix is correct (not mere suppression)

A single **session-scoped `anyio_backend` fixture returning `"asyncio"`** was added
to `apps/api/tests/conftest.py`. This is the standard, AnyIO-documented mechanism
for pinning the backend:

```python
@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
```

Why this is a real fix, not a silencer:

- It makes the **supported backend explicit and deterministic** — the exact
  contract the runtime actually provides. It does not skip, xfail, or `filterwarnings`
  the failures away; the tests still run and still assert real behavior.
- It stops the suite **silently advertising an unsupported backend** (trio) that the
  product does not run on. No unsupported runtime is falsely claimed.
- It is **scoped to async tests only.** A fixture named `anyio_backend` affects only
  `@pytest.mark.anyio` tests (only `test_kitchen_rt.py` today), so no unrelated test
  behavior changes. No broad markers, no config-wide parametrization.
- **No production dependency added.** Trio is not introduced anywhere.

The six previously-passing `[asyncio]` variants keep passing; the six `[trio]`
variants are no longer generated.

---

## 5. Decision-engine signal inventory

`get_owner_decisions()` runs a fixed set of **six distinct signal evaluators**:

| # | Evaluator function | Signal type(s) emitted | Scope | Runs when |
|---|--------------------|------------------------|-------|-----------|
| 1 | `_stock_risk_signals`     | `stock_risk`      | Global inventory | single operational store only |
| 2 | `_slow_moving_signals`    | `slow_moving`     | Global inventory | single operational store only |
| 3 | `_demand_spike_signals`   | `demand_spike`    | Store-scoped     | always |
| 4 | `_sla_risk_signals`       | `sla_risk`        | Store-scoped     | always |
| 5 | `_revenue_anomaly_signals`| `revenue_anomaly` | Store-scoped     | always |
| 6 | `_metric_driven_signals`  | `metric_combo_health`, `metric_upsell_visibility`, `metric_owner_engagement`, `metric_kitchen_performance` | Store-scoped | always |

Evaluators 1–5 are the "five realtime signal categories" named in the older module
docstring. Evaluator 6, the **metric-driven batch**, is a real, intentional,
documented evaluator — it is one function that internally checks four
measurement-layer conditions and is wired into `store_scoped_fns`. Its intent is
confirmed by:

- `docs/STAFF_AUTH_RBAC.md` §12: *"`decision_engine`: demand-spike, SLA-risk,
  revenue-anomaly **and metric-driven** signals are store-filtered."*
- `app/services/operational_context_service.py` header: lists
  `decision_engine._metric_driven_signals()` as a downstream consumer of the
  operational-context mode.
- `apps/owner-web` (`DecisionPanel.tsx`, `FocusBanner.tsx`) renders the
  `metric_*` decision types in the product UI.

The two inventory evaluators (1–2) read the **global** `ingredients` /
`ingredient_stock` tables (no `store_id` in the current schema). They fail closed:
when more than one operational store exists they are **skipped** so one store's
global inventory never leaks into another store's decision feed
(`inventory_guard.is_single_operational_store`).

---

## 6. Definition of `signals_evaluated`

> **`signals_evaluated` = the number of distinct signal evaluators the engine
> executed for the authenticated store and request.**

Properties guaranteed by the implementation:

- **Computed, not hardcoded.** It is `len(inventory_fns) + len(store_scoped_fns)`,
  derived from the actual evaluator list built for the request. It can never drift
  from the code the way the previous magic constant `6` could.
- **Independent of emitted decisions.** An evaluator that runs but produces zero
  decisions still counts — the field measures evaluators *executed*, not decisions
  *returned*.
- **Skipped evaluators treated consistently.** When the inventory evaluators are
  skipped for multi-store fail-closed scoping, they are consistently excluded from
  the count, so the number never claims an evaluator ran when it did not.
- **Deterministic.** For a given store mode the value is fixed:
  - **single operational store** (normal + test environment): `2 + 4 = 6`
  - **multiple operational stores** (inventory evaluators skipped): `0 + 4 = 4`
- **No cross-store leakage; schema unchanged.** `signals_evaluated` remains an
  `int` on `OwnerDecisionsResponse` — backward compatible.

### The original mismatch

Both the code (`"signals_evaluated": 6`) and the test (`assert … == 5`) were
introduced together in the consolidation commit `cde97b0`; neither was newer. The
test's `5` and the module docstring's "Five signal categories" reflected the
**pre-metric-driven era** and were never updated when `_metric_driven_signals`
became the sixth evaluator. The engine genuinely evaluates six, so the **test
expectation was outdated** — the production count is the source of truth. The fix
also upgraded the code from a fragile constant to a computed value so the number is
honest in multi-store mode too (where the old constant `6` wrongly claimed inventory
evaluators had run).

---

## 7. Regression tests

**Async backend (`tests/test_kitchen_rt.py`):**

- `test_anyio_backend_fixture_is_asyncio` — the `anyio_backend` fixture is exactly
  `"asyncio"` (guards against the plugin re-parametrizing trio).
- `test_async_tests_actually_run_on_asyncio` — asserts, via `sniffio`, that the
  event loop the WS tests run on is genuinely asyncio.
- The six existing WebSocket lifecycle tests (broadcast fan-out, store
  partitioning, dead-socket cleanup, idempotent disconnect, zero-connection no-op)
  continue to assert real behavior on the pinned backend.
- WebSocket authentication / origin protection: `tests/test_ws_auth.py` (21 tests)
  unchanged and green.

**Decision envelope (`tests/test_owner_decisions.py`), new class
`TestSignalsEvaluatedContract`:**

- `test_single_store_runs_all_six_evaluators` — `signals_evaluated == 6` in the
  single-store case.
- `test_count_independent_of_emitted_decisions` — monkeypatches three evaluators to
  emit zero decisions; `signals_evaluated` stays `6`.
- `test_multi_store_excludes_skipped_inventory_evaluators` — with two operational
  stores, `signals_evaluated == 4` and no `stock_risk`/`slow_moving` decision leaks
  into store 1's feed.
- `test_store_scoping_signals_evaluated_is_per_store` — the count contract holds for
  each store independently.
- `test_envelope_structure` updated from `5` to `6` with an explanatory comment.

---

## 8. Full-suite result

From `apps/api`, `python -m pytest -q`:

```
411 passed, 4 warnings   (0 failed, 0 skipped)
collected = 411
```

(Baseline was 411 collected / 7 failed / 404 passed. Removing the 6 non-generated
`[trio]` variants, fixing the envelope test, and adding 6 new regression tests
yields 411 collected, all passing.)

The 4 warnings are pre-existing third-party deprecations (starlette `python_multipart`,
Pydantic class-based config, FastAPI `example`→`examples`) — unrelated to this change.

Alembic remains single-head: `a7d3f9b21c05 (head)`. No migration added.

---

## 9. Deferred test-infrastructure improvements

- **No `pytest.ini` / `pyproject.toml` for the API.** Test configuration currently
  lives only in `conftest.py`. A dedicated pytest config (registering markers,
  fixing `testpaths`, filtering the known third-party deprecation warnings) would
  tidy the suite but is out of scope here and intentionally deferred to avoid
  unrelated churn.
- **Store-scoped inventory.** The `stock_risk` / `slow_moving` evaluators depend on
  the single-operational-store guard because inventory tables are global. The real
  fix (adding `store_id` to inventory + stock movements) is tracked for
  `refactor/store-scoped-inventory` and is explicitly *not* part of this baseline
  repair.
