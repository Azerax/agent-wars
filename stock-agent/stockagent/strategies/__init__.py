"""Strategy registry."""
from __future__ import annotations

from .base import Signal, SignalContext, Strategy
from .catalyst import CatalystMomentum, SocialMomentum
from .momentum import GapAndGo, MomentumBreakout, SqueezeBreakout
from .reversion import OversoldBounce

ALL: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (MomentumBreakout, SqueezeBreakout, GapAndGo, OversoldBounce, CatalystMomentum, SocialMomentum)
}


def build(names: list[str]) -> list[Strategy]:
    unknown = [n for n in names if n not in ALL]
    if unknown:
        raise ValueError(f"unknown strategies {unknown}; available: {sorted(ALL)}")
    return [ALL[n]() for n in names]


__all__ = ["ALL", "Signal", "SignalContext", "Strategy", "build"]
