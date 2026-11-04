"""Plugin registries. Metrics and sources register with a decorator; external
packages can also expose metrics through the 'apptopics.metrics' entry point."""
from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import TypeVar

T = TypeVar("T")


class Registry(dict[str, T]):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind

    def register(self, name: str | None = None) -> Callable[[T], T]:
        def deco(obj: T) -> T:
            key = name or getattr(obj, "name", None) or obj.__name__  # type: ignore[attr-defined]
            if key in self:
                raise ValueError(f"{self.kind} '{key}' already registered")
            self[key] = obj
            return obj
        return deco

    def load_entry_points(self, group: str) -> None:
        for ep in entry_points(group=group):
            obj = ep.load()
            self.setdefault(getattr(obj, "name", ep.name), obj)


metrics: Registry = Registry("metric")
sources: Registry = Registry("source")
