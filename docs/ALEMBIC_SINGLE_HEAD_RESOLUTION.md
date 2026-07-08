# Alembic Single-Head Resolution

**Branch:** `fix/alembic-single-head` (based on `chore/repo-consolidation`, commit `cde97b0`)
**Scope:** Alembic migration graph only. No business logic, models, API contracts, frontend,
or existing migration history were changed.
**Working directory for all Alembic commands:** `apps/api/` (where `alembic.ini` lives).

---

## 1. Migration graph before the fix

The repository consolidation (`chore/repo-consolidation`) brought two independently authored
migrations onto the same parent, producing a branch point with **two heads**:

```
2478943c11df  (initial_models,        down: None)
        │
b7e5f2a9c341  (add_waffle_mvp,         down: 2478943c11df)
        │
c9f1d3e8a042  (production_hardening,   down: b7e5f2a9c341)
        │
d4e2f1a8b753  (add_owner_decisions,    down: c9f1d3e8a042)   ← branch point
        ├── a1b2c3d4e5f6 (add_decision_outcome_fields, down: d4e2f1a8b753)   ← HEAD
        └── e5f3a2d9c847 (add_ingredient_is_promoted,  down: d4e2f1a8b753)   ← HEAD
```

Real `alembic heads` output before the fix:

```
a1b2c3d4e5f6 (head)
e5f3a2d9c847 (head)
```

Real `alembic branches` output before the fix:

```
d4e2f1a8b753 (branchpoint)
             -> e5f3a2d9c847 (head)
             -> a1b2c3d4e5f6 (head)
```

---

## 2. Why two heads existed

Revision `d4e2f1a8b753` (`add_owner_decisions_table`) has **two independent children**, each
authored for a different feature and each declaring `down_revision = "d4e2f1a8b753"`:

| Revision       | File                                        | Purpose                                              |
|----------------|---------------------------------------------|------------------------------------------------------|
| `a1b2c3d4e5f6` | `a1b2c3d4e5f6_add_decision_outcome_fields.py` | Adds outcome-tracking columns to `owner_decisions`.  |
| `e5f3a2d9c847` | `e5f3a2d9c847_add_ingredient_is_promoted.py`  | Adds `is_promoted` flag + index to `ingredients`.    |

Because neither migration descends from the other, Alembic sees two leaf nodes (two heads),
and `alembic upgrade head` becomes ambiguous ("Multiple head revisions are present"). This is
the expected, correct outcome of consolidating two feature branches that both forked from the
same parent — it is resolved with a **merge revision**, not by rewriting history.

---

## 3. The two parent revisions

Full metadata of the migrations at the split (verified by reading the files — **unchanged** by
this branch):

### `a1b2c3d4e5f6_add_decision_outcome_fields.py`
- **revision:** `a1b2c3d4e5f6`
- **down_revision:** `d4e2f1a8b753`
- **branch_labels:** `None`
- **depends_on:** `None`
- **schema operations (`upgrade`):**
  - `add_column owner_decisions.resolution_quality VARCHAR(20) NULL`
  - `add_column owner_decisions.estimated_revenue_saved FLOAT NULL`
- **schema operations (`downgrade`):** drops both columns (reverse order).

### `e5f3a2d9c847_add_ingredient_is_promoted.py`
- **revision:** `e5f3a2d9c847`
- **down_revision:** `d4e2f1a8b753`
- **branch_labels:** `None`
- **depends_on:** `None`
- **schema operations (`upgrade`):**
  - `add_column ingredients.is_promoted BOOLEAN NOT NULL DEFAULT false`
  - `create_index ix_ingredients_is_promoted ON ingredients (is_promoted)`
- **schema operations (`downgrade`):** drops the index, then the column.

### Common ancestor `d4e2f1a8b753_add_owner_decisions_table.py`
- **revision:** `d4e2f1a8b753`
- **down_revision:** `c9f1d3e8a042`
- **branch point** — both heads descend from it.
- Creates the `owner_decisions` table and its indexes.

The two branches touch **different tables** (`owner_decisions` vs `ingredients`) and are fully
independent, so there is no ordering conflict — either branch may apply before the other.

---

## 4. The generated merge revision

Created with Alembic's standard command (run from `apps/api/`):

```bash
alembic merge -m "merge decision outcome and ingredient promotion heads" a1b2c3d4e5f6 e5f3a2d9c847
```

- **filename:** `apps/api/alembic/versions/4299b615f7aa_merge_decision_outcome_and_ingredient_.py`
- **revision:** `4299b615f7aa`
- **down_revision:** `("a1b2c3d4e5f6", "e5f3a2d9c847")` — a tuple naming **both** parents
- **branch_labels:** `None`
- **depends_on:** `None`

```python
revision: str = "4299b615f7aa"
down_revision: Union[str, None] = ("a1b2c3d4e5f6", "e5f3a2d9c847")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
```

This is a true merge node with two parents — not a fake linear chain and not one branch
made to depend on the other. Both branches remain first-class ancestors in the history.

---

## 5. Why the merge migration contains no schema operations

A merge revision exists solely to **reunite two divergent branches into a single head** in the
revision graph. Its job is graph topology, not schema change.

- Each parent branch already performs its own DDL (`a1b2c3d4e5f6` on `owner_decisions`,
  `e5f3a2d9c847` on `ingredients`). By the time Alembic reaches the merge node, **both**
  branches have already been applied, so the target schema is complete.
- Adding any DDL to the merge would either **duplicate** operations already performed by a
  parent (causing "column already exists" / "relation already exists" errors) or introduce a
  schema change that belongs in its own dedicated migration — both are out of scope and unsafe.
- The two branches modify disjoint tables, so there is no conflict to reconcile in the merge.

Therefore `upgrade()` and `downgrade()` are intentionally `pass`. The merge changes only the
contents of the `alembic_version` table (which head(s) are recorded), never the schema.

---

## 6. Migration graph after the fix

```
2478943c11df
        │
b7e5f2a9c341
        │
c9f1d3e8a042
        │
d4e2f1a8b753   ← branch point
        ├── a1b2c3d4e5f6 ┐
        └── e5f3a2d9c847 ┘
                 │
            4299b615f7aa  (merge, down: a1b2c3d4e5f6 + e5f3a2d9c847)   ← single HEAD
```

Real `alembic heads` output after the fix:

```
4299b615f7aa (head)
```

Real `alembic heads --verbose` output after the fix:

```
Rev: 4299b615f7aa (head) (mergepoint)
Merges: a1b2c3d4e5f6, e5f3a2d9c847
Path: .../alembic/versions/4299b615f7aa_merge_decision_outcome_and_ingredient_.py

    merge decision outcome and ingredient promotion heads
```

Real `alembic history` output after the fix (both branches, then the merge node):

```
a1b2c3d4e5f6, e5f3a2d9c847 -> 4299b615f7aa (head) (mergepoint), merge decision outcome and ingredient promotion heads
d4e2f1a8b753 -> a1b2c3d4e5f6, Add outcome tracking fields to owner_decisions
d4e2f1a8b753 -> e5f3a2d9c847, Add is_promoted flag to ingredients for owner-driven menu ranking
c9f1d3e8a042 -> d4e2f1a8b753 (branchpoint), Add owner_decisions table for action lifecycle management
b7e5f2a9c341 -> c9f1d3e8a042, Production hardening: idempotency, audit log, actor tracking
2478943c11df -> b7e5f2a9c341, Add waffle MVP columns and tables
<base> -> 2478943c11df, Initial models
```

---

## 7. Verification commands and results

All static commands were run from `apps/api/`.

| Command | Result | Notes |
|---|---|---|
| `alembic heads` | **PASS** | Single head: `4299b615f7aa (head)` |
| `alembic heads --verbose` | **PASS** | Reports `(mergepoint)`, `Merges: a1b2c3d4e5f6, e5f3a2d9c847` |
| `alembic branches` | **PASS** | Only the `d4e2f1a8b753` branch point remains; leaves no longer marked `(head)` |
| `alembic history` | **PASS** | Both branches converge into `4299b615f7aa` |
| `python -c "... ScriptDirectory.get_heads()"` | **PASS** | Returns `['4299b615f7aa']` — exactly one item |
| `git diff --check` | **PASS** | No whitespace/conflict errors |
| `python -m compileall apps/api/alembic` | **PASS** | All migration modules compile |
| `pytest apps/api/tests -q --collect-only` (run from `apps/api/`) | **PASS** | 267 tests collected, no import/collection errors |

Programmatic head discovery:

```bash
python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; \
c=Config('alembic.ini'); s=ScriptDirectory.from_config(c); print(list(s.get_heads()))"
# -> ['4299b615f7aa']
```

---

## 8. Database verification

Real database verification was performed against a **disposable PostgreSQL 16** instance
(Docker container `sweetops_alembic_test`, image `postgres:16`, published on host port `5544`,
database created fresh for this task). No production or user database was touched.
`DATABASE_URL` was overridden per-command to point at the disposable instance.

### Fresh database (Scenario A)

```
alembic upgrade head
  -> ... d4e2f1a8b753 -> e5f3a2d9c847
  -> d4e2f1a8b753 -> a1b2c3d4e5f6
  -> a1b2c3d4e5f6, e5f3a2d9c847 -> 4299b615f7aa
alembic current
  -> 4299b615f7aa (head) (mergepoint)
```

All migrations apply; final revision is the merge head. **PASS.**

### Downgrade / re-upgrade behavior

- `alembic downgrade -1` from the merge head returns **`Ambiguous walk`** and makes **no
  change** — this is expected: a relative one-step downgrade across a merge point is ambiguous
  because Alembic cannot choose which of the two parent branches to unwind. The database safely
  remained at `4299b615f7aa` with schema intact (14 non-alembic tables, both branch columns
  present). This is documented rollback behavior, not a failure. See §9.
- Supported downgrade to the branch point demonstrates the merge node's rollback in isolation:

  ```
  alembic downgrade d4e2f1a8b753
    -> downgrade 4299b615f7aa -> a1b2c3d4e5f6, e5f3a2d9c847   (merge node: no-op)
    -> downgrade a1b2c3d4e5f6 -> d4e2f1a8b753                 (branch DDL reversed)
    -> downgrade e5f3a2d9c847 -> d4e2f1a8b753                 (branch DDL reversed)
  alembic current -> d4e2f1a8b753 (branchpoint)
  ```

  The merge node is the first step reversed and drops nothing (the column drops come from the
  two branch migrations, not the merge).
- Re-upgrade restores the single head:

  ```
  alembic upgrade head -> ... -> 4299b615f7aa (head) (mergepoint)
  alembic current      -> 4299b615f7aa (head) (mergepoint)
  # alembic_version row count: 1
  ```

**Downgrade PASS, re-upgrade PASS.**

### Existing-database compatibility (Scenarios B & C)

Each on its own disposable database:

- **Scenario B — DB already at `a1b2c3d4e5f6`:** `alembic upgrade head` applied **only** the
  missing branch (`e5f3a2d9c847`) and then the merge, reaching `4299b615f7aa`. **PASS.**
- **Scenario C — DB already at `e5f3a2d9c847`:** `alembic upgrade head` applied **only** the
  missing branch (`a1b2c3d4e5f6`) and then the merge, reaching `4299b615f7aa`. **PASS.**

In both cases the merge never re-runs an already-applied branch, so no duplicate DDL is
attempted regardless of which branch a live database happened to be on.

### End-to-end schema validation

On a fresh migrated database (`alembic upgrade head` -> `4299b615f7aa`), `seed.py` ran
successfully (`1 store, 6 tables, 1 product, 20 ingredients with stock`) and the API test suite
ran **261 passed, 6 failed**. The 6 failures are pre-existing and unrelated to the migration
graph (see §10): 5 are `[trio]` variants failing with `KeyError: 'anyio._backends._trio'`
(optional `trio` backend not installed; the `[asyncio]` variants pass) and 1 is
`test_envelope_structure` asserting `signals_evaluated == 5` while the app evaluates 6 (a
business-logic expectation mismatch). This branch adds no code and no schema and cannot affect
either.

### Database verification limitations

- The merge revision cannot be rolled back via relative `downgrade -1` (ambiguous across a
  merge point); rollback of the merge in isolation is demonstrated via downgrade to the branch
  point (§9), where the merge is the first, no-op step reversed.
- The full API test suite requires seed data and the optional `trio` package to be fully green;
  neither is related to the Alembic graph. Test **collection** (the migration-relevant check)
  passes with zero import/collection errors.

---

## 9. Rollback behavior of the merge revision

- **Upgrade:** `4299b615f7aa.upgrade()` is a no-op — it records the merge revision as the single
  head in `alembic_version` and performs no DDL.
- **Downgrade (isolated):** `4299b615f7aa.downgrade()` is a no-op — reversing it restores the
  two-parent (two-head) state in `alembic_version` and performs no DDL. Verified: the first step
  of `alembic downgrade d4e2f1a8b753` reverses `4299b615f7aa -> a1b2c3d4e5f6, e5f3a2d9c847` and
  drops no tables or columns.
- **Relative downgrade:** `alembic downgrade -1` from the merge head is **ambiguous** (Alembic
  reports `Ambiguous walk`) because the merge has two parents and Alembic cannot infer which
  branch to unwind one step. To move below the merge, target an explicit revision (e.g. the
  branch point `d4e2f1a8b753`). This is inherent to merge nodes, not a defect of this migration.
- **Data safety:** because the merge performs no schema mutation, neither its upgrade nor its
  downgrade can drop or alter tables, columns, indexes, or data.

---

## 10. Remaining migration risks

1. **Relative downgrade across the merge is ambiguous.** Operators must downgrade to an explicit
   revision (not `-1`) to walk below `4299b615f7aa`. Documented in §9.
2. **Future new heads.** Any new migration must set `down_revision = "4299b615f7aa"` (the current
   single head) — or be merged again — to avoid reintroducing multiple heads. Run
   `alembic heads` in CI to catch regressions early.
3. **Branch-order independence relied upon.** The two parents modify disjoint tables
   (`owner_decisions` vs `ingredients`), so apply order does not matter. If a future migration
   introduces cross-branch dependencies, that assumption must be revisited.
4. **`server_default` on `e5f3a2d9c847`.** `ingredients.is_promoted` is added `NOT NULL` with a
   `server_default='false'`, which backfills existing rows safely; no data migration risk. Noted
   for completeness — unchanged by this branch.
5. **Environment-specific verification.** Database verification used a disposable PostgreSQL 16
   container. Production upgrades should still be applied against a backup/staging copy first per
   standard practice.
