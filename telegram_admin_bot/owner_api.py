"""Client-facing API: the owner's login and dashboard, under /api/owner/*.

Mounted by panel.py WITHOUT the admin login: these routes have their own
(owner_auth.py). Every query is scoped to the tenants linked to the
logged-in owner (owner_tenants). Filled in by phase 4.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

router = APIRouter()

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus
