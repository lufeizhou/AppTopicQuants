"""Metric plugins. Import registers the built-ins; external packages add more
via the 'apptopics.metrics' entry point."""
from ..registry import metrics
from . import brandness, competition, demand, difficulty, entry, external, llm, market_scores, momentum, monetization, play, sector  # noqa: F401

metrics.load_entry_points("apptopics.metrics")

__all__ = ["metrics"]
