"""Recorder adapters. `make()` picks one from the RECORDER setting."""
from __future__ import annotations

from ..config import Settings
from .base import Recorder


def make(settings: Settings) -> Recorder:
    if settings.recorder == "mock":
        from .mock import MockRecorder
        return MockRecorder(settings.mock_dir)
    from .frigate import FrigateRecorder
    return FrigateRecorder(settings.frigate_url, settings.frigate_user, settings.frigate_password)
