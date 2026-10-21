"""Admin API for operator rules (rules.py): list, propose, accept, edit,
reject, retire, and fold them into the prompt. Mounted by panel.py behind the
admin login; every route is under /api/finetune/ (admin only, never a
manager's, see staff.ADMIN_ONLY_PREFIXES).

Scaffold: the rules branch fills this in.
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
