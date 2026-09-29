"""Admin API for client logins: create, disable, reset, link tenants.
Mounted by panel.py behind the admin login. Filled in by phase 4.
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
