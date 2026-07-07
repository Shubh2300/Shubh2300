"""Shared FastAPI dependencies / cached singletons."""

from __future__ import annotations

from functools import lru_cache

from config import get_settings
from services.registry import ActionRegistry


@lru_cache
def get_registry() -> ActionRegistry:
    """Load the Action Registry once per process.

    The registry file is owned by the action-registry package and mounted into
    the API container at ACTION_REGISTRY_PATH.
    """
    settings = get_settings()
    return ActionRegistry.load(settings.action_registry_path)
