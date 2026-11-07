"""General recipe for a transparent 0-100 index (decided 2026-11-07).

An index is a list of ingredients, each with a weight and a plain-words formatter. For every search:

  1. each ingredient's raw value -> its percentile among all searches that have it ("better than X% of searches"),
     ties share the average rank, so a zero shared by half the searches sits at ~25, not 0;
  2. the index = weighted mean of the available ingredients' percentiles (missing ingredients drop out and the
     weights of the others are rescaled; `coverage` says how much weight was measured), re-ranked as a percentile;
  3. 5 blocks = which fifth of searches it falls in (1 = bottom 20%, 5 = top 20%);
  4. the same ranks are also taken within the search's App Store category.

Percentiles (not raw scales or z-scores) because they read plainly, a single giant app cannot squash everyone
else, and they need no hand-picked "0 = x, 100 = y" anchors (OECD/JRC Handbook on Constructing Composite
Indicators lists ranking as a standard normalisation). They are relative and dynamic: recomputed on every
rescore against the current set of searches, so a score can move when other searches change.

Every result carries `parts` (one row per ingredient: raw value, its text, percentile, blocks, weight, share of
the index), which the dashboard shows on hover and click.
"""
from __future__ import annotations

from dataclasses import dataclass, field


KINDS = {   # number formats shared with the dashboard (index.html fmtKind): the text is pre + number + post
    "int": lambda v: f"{v:,.0f}", "num2": lambda v: f"{v:,.2f}", "num1s": lambda v: f"{v:+.1f}", "usd": lambda v: f"${v:,.2f}",
    "pct": lambda v: f"{v:.0%}", "pct1s": lambda v: f"{v:+.1%}", "times": lambda v: f"{v:.2f}×"}


@dataclass
class Ingredient:
    key: str
    label: str
    weight: float
    kind: str = "num2"
    pre: str = ""
    post: str = ""
    zero: str = ""          # text when the value is 0 (e.g. "no rival on a top-grossing chart")
    higher_is_better: bool = True
    note: str = ""          # where the number comes from, one line

    def fmt(self, v: float) -> str:
        return self.zero if (v == 0 and self.zero) else f"{self.pre}{KINDS[self.kind](v)}{self.post}"

    def spec(self) -> dict:
        return {"key": self.key, "label": self.label, "weight": self.weight, "kind": self.kind, "pre": self.pre, "post": self.post,
                "zero": self.zero, "note": self.note}


@dataclass
class IndexSpec:
    name: str
    label: str
    question: str
    ingredients: list[Ingredient] = field(default_factory=list)
    min_coverage: float = 0.5   # share of the weight that must be measured, else no score

    def spec(self) -> dict:
        tw = sum(i.weight for i in self.ingredients)
        return {"name": self.name, "label": self.label, "question": self.question, "min_coverage": self.min_coverage,
                "ingredients": [{**i.spec(), "weight": round(i.weight / tw, 3)} for i in self.ingredients]}


def percentiles(values: dict[str, float]) -> dict[str, float]:
    """Mid-rank percentile 0-100 (ties share the average rank)."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n, out, i = len(items), {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        p = 100.0 * ((i + j) / 2 + 0.5) / n
        for k in range(i, j + 1):
            out[items[k][0]] = p
        i = j + 1
    return out


def blocks(p: float | None) -> int | None:
    return None if p is None else max(1, min(5, int(p // 20) + 1))


def _grouped_pct(values: dict[str, float], group: dict[str, str]) -> dict[str, float]:
    by: dict[str, dict[str, float]] = {}
    for k, v in values.items():
        g = group.get(k)
        if g:
            by.setdefault(g, {})[k] = v
    out = {}
    for g, vs in by.items():
        if len(vs) >= 20:          # a percentile among fewer than 20 searches says little
            out.update(percentiles(vs))
    return out


def build(spec: IndexSpec, raw: dict[str, dict[str, float | None]], category: dict[str, str] | None = None) -> dict[str, dict]:
    """raw: {search: {ingredient key: value or None}} -> {search: {value, value_cat, blocks, coverage, parts, ...}}."""
    category = category or {}
    pct, pct_cat, n_with = {}, {}, {}
    for ing in spec.ingredients:
        vals = {k: (v if ing.higher_is_better else -v) for k, r in raw.items() if (v := r.get(ing.key)) is not None}
        pct[ing.key], pct_cat[ing.key], n_with[ing.key] = percentiles(vals), _grouped_pct(vals, category), len(vals)
    total_w = sum(i.weight for i in spec.ingredients)
    mix: dict[str, float] = {}
    rows: dict[str, list[dict]] = {}
    cov: dict[str, float] = {}
    for k, r in raw.items():
        have = [i for i in spec.ingredients if k in pct[i.key]]
        w = sum(i.weight for i in have)
        cov[k] = w / total_w if total_w else 0.0
        parts = []
        for i in spec.ingredients:
            p = pct[i.key].get(k)
            v = r.get(i.key)
            parts.append({"key": i.key, "label": i.label, "raw": v, "text": i.fmt(v) if v is not None else "not measured",
                          "pct": round(p, 1) if p is not None else None, "pct_cat": round(pct_cat[i.key][k], 1) if k in pct_cat[i.key] else None,
                          "blocks": blocks(p), "weight": round(i.weight / total_w, 3), "note": i.note,
                          "share": round(i.weight / w, 3) if p is not None and w else 0.0})
        rows[k] = parts
        if have and cov[k] >= spec.min_coverage:
            mix[k] = sum(i.weight * pct[i.key][k] for i in have) / w
    final, final_cat = percentiles(mix), _grouped_pct(mix, category)
    out = {}
    for k in mix:
        out[k] = {"value": round(final[k], 1), "value_cat": round(final_cat[k], 1) if k in final_cat else None,
                  "blocks": blocks(final[k]), "mix": round(mix[k], 1), "coverage": round(cov[k], 2), "category": category.get(k),
                  "n": len(mix), "parts": rows[k]}
    return out


def why(res: dict) -> str:
    """One line: the two ingredients that pull the index most, in plain words."""
    ps = [p for p in res["parts"] if p["pct"] is not None]
    ps.sort(key=lambda p: -p["share"])
    return " · ".join(f"{p['label'].lower()}: {p['text']}" for p in ps[:3])
