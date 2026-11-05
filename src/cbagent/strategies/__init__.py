"""Strategy registry. Add a new strategy module here so `[strategy] name = ...` can find it."""
from __future__ import annotations

from .base import Strategy
from .confluence import Confluence
from .dca import Dca
from .sma_crossover import SmaCrossover
from .trend import TrendFollow

REGISTRY: dict[str, type[Strategy]] = {
    SmaCrossover.name: SmaCrossover,
    Confluence.name: Confluence,
    TrendFollow.name: TrendFollow,
    Dca.name: Dca,
}


def build_strategy(settings: dict) -> Strategy:
    params = dict(settings)
    name = params.pop("name")
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; known: {', '.join(REGISTRY)}")
    return REGISTRY[name](**params)
