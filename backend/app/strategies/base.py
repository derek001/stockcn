"""Trading strategy plugin framework.

Drop a new .py file into this folder, subclass Strategy, and it is
auto-discovered and available in the UI and the backtester.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    id: str = ""
    name: str = ""
    description: str = ""
    # [{key,label,type(number/select),default,options?,min?,max?}]
    params_schema: list[dict] = []

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        """df: columns date/open/high/low/close/volume (ascending).
        Return [{"date","side":"buy"|"sell","reason"}]."""

    def meta(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "params_schema": self.params_schema,
        }


def get_strategy(strategy_id: str) -> Strategy | None:
    from . import discover
    return discover().get(strategy_id)
