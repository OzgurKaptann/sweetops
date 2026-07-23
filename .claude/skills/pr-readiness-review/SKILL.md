---
name: pr-readiness-review
description: Strictly review a completed SweetOps branch before a PR is opened — branch and working-tree state, changed files versus stated scope, untracked leftovers, schema/migration and dependency surprises, scope creep, verified tests and builds, reconcilers, documentation, generated PR body, and merge risk. Use when asked whether a branch is ready for a PR, to check a branch before opening one, or to write the PR description. It refuses to approve on unverified claims.
---

# SweetOps PR Readiness Review

Gate a finished branch before the PR exists. Be strict. **Unverified is not
passed** — if a command was not run in this session, its result is UNKNOWN, and
UNKNOWN blocks approval.

This skill reviews and reports. It does not create the PR, push, or merge unless
the user explicitly asks for that as a separate step.

## SweetOps context

Monorepo: FastAPI + PostgreSQL + Alembic in `apps/api`; four Next.js apps
(`customer-web` 3001, `kitchen-web` 3002, `owner-web` 3003, `cashier-web` 3004);
shared `packages/types` and `packages/ui`; operational scripts in `scripts/`;
documentation in `docs/`. Core product: QR ordering · kitchen flow · kitchen
timing · cashier payments/refunds · cashier shifts · order issues/refunds ·
inventory lifecycle · store-scoped stock · transfers · physical counts ·
threshold alerts · owner operational dashboard · demo seed · production
readiness docs. User-facing copy is Turkish.

The authoritative companion is `docs/RELEASE_CHECKLIST.md`; this skill is the
pre-PR subset of it. Deeper release verification belongs to
[release-verification](../release-verification/SKILL.md).

## Boundaries

Review only — do not "fix while reviewing" beyond what the user asks for. Never
approve a branch that quietly contains: forecasting · supplier management ·
purchase orders · new schema · new dependencies · payment redesign · inventory
redesign · shift redesign — unless that was the branch's explicit purpose.

## Procedure

Establish the branch's **stated scope** first, in one sentence, from the user or
the branch name. Every later finding is judged against it.

### 1. Branch state

```bash
git branch --show-current
git log -12 --oneline --decorate
git log --oneline main..HEAD
git fetch origin && git log --oneline HEAD..origin/main
```

Correct branch, not `main`, branched from an up-to-date `main`, expected prior
work present, commit messages describing the change. Report how far behind
`origin/main` the branch is.

### 2. Working tree state

```bash
git status --short
git status --porcelain --untracked-files=all
git diff --check
git diff --cached --stat
```

Clean tree, nothing staged unintentionally, no whitespace damage, no conflict
markers. Any output from `git diff --check` is a blocker.

### 3. Changed files match scope

```bash
git diff --stat main...HEAD
git diff --name-status main...HEAD
```

Walk the list file by file. Each file must be explainable by the stated scope.
Read the diff of anything surprising — do not infer intent from the filename.

### 4. No forgotten untracked files

Untracked output above must be empty or deliberate. Watch for scratch files,
`.env`, notes, screenshots, coverage output, `__pycache__`, editor files, and
anything that should have been in `.gitignore` instead.

```bash
git ls-files | grep -E '\.env'   # must return only *.env.example
```

### 5. No schema / migration surprise

```bash
git diff --name-only main...HEAD -- apps/api/alembic/versions
cd apps/api && python -m alembic heads && cd ../..
python scripts/verify_release_readiness.py
```

Exactly one head. If a migration was added: it is a new revision (never an edit
to a merged one), `downgrade()` is implemented or refuses explicitly with a
stated reason, a `test_*_migration.py` covers the round trip, and the caveat is
documented. If the branch was not supposed to touch schema, any migration file
is a blocker.

### 6. No dependency surprise

```bash
git diff main...HEAD -- package.json package-lock.json apps/*/package.json packages/*/package.json apps/api/requirements*.txt apps/api/pyproject.toml
```

Any dependency change on a branch that did not declare one is a blocker. A
declared one must be justified in the PR body: why, what it replaces, and its
maintenance cost.

### 7. No scope creep

Opportunistic refactors, drive-by renames, formatting churn across untouched
files, new endpoints "while I was there", debug output, commented-out code, and
TODOs added without an owner. Each is a finding; each should either be reverted
or moved to its own branch.

### 8. Tests and builds verified

Run them; do not accept a claim. From `apps/api`:

```bash
python -m pytest -q --collect-only
python -m pytest -q
```

From the repo root:

```bash
python -m compileall apps/api/app scripts
npm run build:types
npm run build:ui
npm run build --workspace=customer-web
npm run build --workspace=kitchen-web
npm run build --workspace=owner-web
npm run build --workspace=cashier-web
npm run test --workspace=customer-web
npm run test --workspace=cashier-web
npm run test --workspace=owner-web
npm run test --workspace=kitchen-web
```

Ordering rule that catches people out: run the backend suite **before** seeding
demo data — tests and local development share one database, and resident demo
data fails roughly two dozen tests for reasons that are not regressions.

Also confirm: no test was skipped, `xfail`ed, or deleted to get green; new tests
are deterministic; any failure was diagnosed to a cause rather than re-run until
it passed.

### 9. Reconcilers verified where relevant

Required whenever the branch touches payments, inventory, order issues, kitchen
timing, or the seed. All four are read-only and must exit `0`:

```bash
python scripts/reconcile_kitchen_timing.py
python scripts/reconcile_payments.py
python scripts/reconcile_inventory.py
python scripts/reconcile_order_issues.py
```

A mismatch is investigated, never "corrected" by editing a ledger.

### 10. Docs updated

Docs changed alongside the code they describe; README still accurate and every
command it advertises exists; `.env.example` files match what the code reads;
new settings documented; roadmap/readiness docs updated if the branch changes
what is true about the product.

### 11. PR body generated

Produce a ready-to-paste body:

- **Summary** — what changed and why, in plain language.
- **Scope** — what is in.
- **Deliberately excluded** — what is out, and why.
- **Changes** — the reviewed file list, grouped.
- **Verification** — every command above with PASS / FAIL / NOT RUN and the
  key output line. Never write PASS for something not run.
- **Migrations / dependencies** — "none", or the details.
- **Risk and rollback** — what breaks if this is wrong, how to revert.
- **Known limitations** — disclosed, not glossed.

### 12. Merge risk assessed

Conflict likelihood against `main`, whether a rebase would create a second
Alembic head, whether the change is code-only (revert-safe) or data-affecting,
what the rollback trigger symptom is, and whether anything needs to ship in a
particular order.

## Output

Report as a table: item · PASS / FAIL / UNKNOWN · evidence. Then blockers
(ordered), then non-blocking findings, then the PR body.

End with exactly one verdict:

- **READY FOR PR** — every item PASS, zero blockers, zero UNKNOWN.
- **NOT READY FOR PR** — anything else, with the shortest list of actions that
  would change the verdict.

Do not soften a verdict because the work looks finished, because the user is in
a hurry, or because a failure "is probably unrelated". Say plainly what was not
verified.
