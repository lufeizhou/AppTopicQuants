"""LLM consumer-panel appeal: mean persona score from llm_votes (0..1)."""
from __future__ import annotations

from ..registry import metrics
from .base import FactorRow, Metric


@metrics.register()
class LlmAppeal(Metric):
    name = "llm_appeal"
    description = "Mean simulated-consumer willingness to try/pay (0..1) from DeepSeek personas."
    requires = ("llm_votes",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""
            SELECT entity_id, avg(score), count(*), string_agg(persona || ': ' || rationale, ' || ')
            FROM llm_votes WHERE entity_type='keyword' AND snapshot_date<=? GROUP BY entity_id""",
            [snapshot_date]).fetchall()
        return [FactorRow(k, round(float(s), 4), {"n_votes": n, "rationales": (r or "")[:1500]}) for k, s, n, r in rows]
