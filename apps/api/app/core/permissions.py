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
# (CASHIER also settles at a store, but this branch grants it no operational
# endpoints; it is included so a store is still required for it.)
OPERATIONAL_ROLES = {ROLE_OWNER, ROLE_MANAGER, ROLE_KITCHEN, ROLE_CASHIER}

# ── Named permissions ────────────────────────────────────────────────────────
PERM_OWNER_READ = "owner:read"
PERM_OWNER_DECISIONS_WRITE = "owner:decisions:write"
PERM_KITCHEN_READ = "kitchen:read"
PERM_KITCHEN_ORDERS_WRITE = "kitchen:orders:write"

# ── Matrix ───────────────────────────────────────────────────────────────────
# MANAGER matches OWNER for current operational functionality. Neither is
# granted any user-management capability here — that is a future authenticated
# feature. CASHIER intentionally has no permissions in this branch.
_ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_OWNER: {
        PERM_OWNER_READ,
        PERM_OWNER_DECISIONS_WRITE,
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
    },
    ROLE_MANAGER: {
        PERM_OWNER_READ,
        PERM_OWNER_DECISIONS_WRITE,
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
    },
    ROLE_KITCHEN: {
        PERM_KITCHEN_READ,
        PERM_KITCHEN_ORDERS_WRITE,
    },
    ROLE_CASHIER: set(),
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
