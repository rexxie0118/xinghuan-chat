"""Deterministic failure injection for tests and evaluation.

Example:
    WEBCHAT_FAILURE_INJECT='[{"component":"tool","target":"web.search","times":1}]'
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


class InjectedFailure(RuntimeError):
    def __init__(self, component: str, target: str = "") -> None:
        self.component = component
        self.target = target
        super().__init__(f"injected failure: {component}" + (f" ({target})" if target else ""))


@dataclass
class FailureRule:
    component: str
    target: str = "*"
    times: int = 1

    def matches(self, component: str, target: str) -> bool:
        if self.component != component:
            return False
        return self.target in {"", "*"} or self.target == target


class FailureInjector:
    def __init__(self, rules: list[FailureRule] | None = None) -> None:
        self.rules = list(rules or [])

    @classmethod
    def from_env(cls) -> "FailureInjector":
        raw = (os.getenv("WEBCHAT_FAILURE_INJECT") or "").strip()
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        entries = data if isinstance(data, list) else [data]
        rules = []
        for item in entries:
            if not isinstance(item, dict) or not item.get("component"):
                continue
            rules.append(
                FailureRule(
                    component=str(item["component"]),
                    target=str(item.get("target") or "*"),
                    times=max(1, int(item.get("times") or 1)),
                )
            )
        return cls(rules)

    def maybe(self, component: str, target: str = "") -> None:
        for rule in self.rules:
            if rule.matches(component, target) and rule.times > 0:
                rule.times -= 1
                raise InjectedFailure(component, target)

    def public(self) -> list[dict[str, Any]]:
        return [
            {"component": r.component, "target": r.target, "times": r.times}
            for r in self.rules
            if r.times > 0
        ]
