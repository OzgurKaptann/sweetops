"""
Central role → permission matrix.

Single source of truth for authorization. Routers and dependencies reference
named permissions (e.g. "owner:read"), never hardcoded role-name comparisons,
so least-privilege changes happen in exactly one place.
"""
from __future__ import annotations

# ── Canonical staff roles ────────────────────────────────────────────────────
ROLE_OWNER = "OWNER"
ROLE_MANAGER = "MANAGER"
ROLE_KITCHEN = "KITCHEN"
ROLE_CASHIER = "CASHIER"

CANONICAL_ROLES = [ROLE_OWNER, ROLE_MANAGER, ROLE_KITCHEN, ROLE_CASHIER]

# Roles that operate against a single store and therefore MUST have a store_id.
# CASHIER settles payments at a store, so a store is likewise mandatory for it.
OPERATIONAL_ROLES = {ROLE_OWNER, ROLE_MANAGER, ROLE_KITCHEN, ROLE_CASHIER}

# ── Named permissions ────────────────────────────────────────────────────────
PERM_OWNER_READ = "owner:read"
PERM_OWNER_DECISIONS_WRITE = "owner:decisions:write"
PERM_KITCHEN_READ = "kitchen:read"
PERM_KITCHEN_ORDERS_WRITE = "kitchen:orders:write"

# Payment settlement / cashier permissions.
#   payments:read    — view open tables, order bills, payment history.
#   payments:collect — record a cash/card collection (settlement).
#   payments:refund  — reverse previously-collected money.
PERM_PAYMENTS_READ = "payments:read"
PERM_PAYMENTS_COLLECT = "payments:collect"
PERM_PAYMENTS_REFUND = "payments:refund"

# Inventory lifecycle permissions.
#   inventory:read   — view stock summary and the movement ledger.
#   inventory:adjust — mutate physical stock: purchase receipt, manual
#                      adjustment, waste. This is a physical-count authority,
#                      not a sales authority.
PERM_INVENTORY_READ = "inventory:read"
PERM_INVENTORY_ADJUST = "inventory:adjust"

# Store setup / menu provisioning permissions.
#   setup:read   — view the branch's setup readiness, its catalog publication
#                  state and its tables. Read-only.
#   setup:manage — create/edit catalog products, publish or withdraw them from
#                  THIS branch's customer menu, switch an item off for the day,
#                  reorder the menu, add a table, rotate a table's QR token.
#
# Deliberately its own authority rather than a reuse of ``owner:read`` /
# ``inventory:adjust``. Publishing decides what a guest can order and rotating a
# QR invalidates a printed sticker: neither is a stock movement and neither is a
# read. A future role that may count the freezer but must not rewrite the menu
# then already exists as a matrix change rather than a router change.
PERM_SETUP_READ = "setup:read"
PERM_SETUP_MANAGE = "setup:manage"

# ── Matrix ───────────────────────────────────────────────────────────────────
# MANAGER matches OWNER for current operational functionality. Neither is
# granted any user-management capability here — that is a future authenticated
# feature. CASHIER may read bills and collect payments but never refund.
_ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_OWNER: {
        PERM_OWNER_READ,
        PERM_OWNER_DECISIONS_WRITE,
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
        PERM_PAYMENTS_READ,
        PERM_PAYMENTS_COLLECT,
        PERM_PAYMENTS_REFUND,
        PERM_INVENTORY_READ,
        PERM_INVENTORY_ADJUST,
        PERM_SETUP_READ,
        PERM_SETUP_MANAGE,
    },
    ROLE_MANAGER: {
        PERM_OWNER_READ,
        PERM_OWNER_DECISIONS_WRITE,
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
        PERM_PAYMENTS_READ,
        PERM_PAYMENTS_COLLECT,
        PERM_PAYMENTS_REFUND,
        PERM_INVENTORY_READ,
        PERM_INVENTORY_ADJUST,
        PERM_SETUP_READ,
        PERM_SETUP_MANAGE,
    },
    # KITCHEN sees what stock is left so it can flag a shortage, but cannot
    # rewrite physical stock: a cook correcting the count is exactly the
    # unaccountable adjustment this lifecycle exists to prevent. Waste reporting
    # by kitchen is deliberately NOT enabled here — see docs/INVENTORY_LIFECYCLE.md.
    ROLE_KITCHEN: {
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
        PERM_INVENTORY_READ,
    },
    # CASHIER settles at the till: it may read bills and collect money, but must
    # never refund (that is a MANAGER/OWNER control) and has no owner/kitchen
    # write access. It has NO inventory permission at all — money and stock are
    # separate authorities.
    ROLE_CASHIER: {
        PERM_PAYMENTS_READ,
        PERM_PAYMENTS_COLLECT,
    },
}


def permissions_for_role(role_name: str | None) -> list[str]:
    """Return the sorted list of permissions granted to a role name."""
    if not role_name:
        return []
    return sorted(_ROLE_PERMISSIONS.get(role_name.upper(), set()))


def role_has_permission(role_name: str | None, permission: str) -> bool:
    if not role_name:
        return False
    return permission in _ROLE_PERMISSIONS.get(role_name.upper(), set())


def is_operational_role(role_name: str | None) -> bool:
    return bool(role_name) and role_name.upper() in OPERATIONAL_ROLES
