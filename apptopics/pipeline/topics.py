"""Topic view: group the scoreable keywords into product topics (one app could own a topic) and score each topic.

  cluster_universe   DeepSeek groups keywords (parallel batches of similar terms), then a merge pass folds
                     near-duplicate topic labels together. Replaces topics / topic_keywords. ~$0.10-0.30.
  build_topic_index  Topic score from its members' opportunity scores (run after every keyword index build):
                       score = 0.75 x best member + 0.25 x mean(top-3 members) + breadth bonus (up to +6)
                       breadth bonus = 2 x log2(1 + members graded A-C): more good searches = more ways in
                     x traffic adjustment 0.9 .. 1.0 (total searches/day 5 -> 0.9, 1,000+ -> 1.0)
                     Grade cut-offs as keywords. Action Build now if grade A/B and its best member is Build now.
                     Hidden members (brands, games, thin sectors...) never count; a topic made only of hidden searches
                     gets a row flagged hidden (composite.excluded) so the dashboard shows it only on "Show hidden".
  attach_hidden      DeepSeek puts the hidden keywords into topics too: an existing topic when one fits, else a new
                     (hidden-only) topic. Re-run after `llm cluster`, which rebuilds topics from scratch. ~$0.3-0.8.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..db import connect_read_live, today, upsert
from .index import HARD_HIDE, _grade, composite_date, keyword_factors, searches_per_day

log = logging.getLogger("apptopics.topics")


def _latest_opp(con) -> dict[str, dict]:
    d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'").fetchone()[0]
    if d is None:
        return {}
    dc = composite_date(con) or d      # the index may be rebuilt on a day without a scoring run: hidden flags live on the composite date
    out = {}
    for k, v, m, cm in con.execute("""SELECT o.entity_id, o.value, o.meta, c.meta FROM factor_values o
            LEFT JOIN factor_values c ON c.entity_type='keyword' AND c.entity_id=o.entity_id AND c.metric='composite' AND c.snapshot_date=?
            WHERE o.metric='opp_score' AND o.entity_type='keyword' AND o.snapshot_date=?""", [dc, d]).fetchall():
        m, cm = json.loads(m) if m else {}, json.loads(cm) if cm else {}
        why = cm.get("excluded_reason") or ""
        hard = bool(cm.get("excluded")) and any(h in why for h in HARD_HIDE)
        out[k] = {"opp": v, **m, "hard_hidden": hard, "hidden": bool(cm.get("excluded")), "why": why}
    return out


def clusterable(con) -> list[str]:
    """Keywords with a scored sector that are not hidden for a hard reason (brand, games, excluded term...)."""
    return [k for k, r in _latest_opp(con).items() if r.get("parts") and not r["hard_hidden"]]


def _batch_key(term: str) -> tuple:
    w = sorted(term.split(), key=lambda x: (-len(x), x))   # longest word first: similar products sit together
    return tuple(w)


MERGE_SYS = ("You get App Store product-topic labels, each with a few example searches. Some labels describe the SAME "
             "product (one app would serve both), e.g. 'AI photo to video' and 'Photo animation video generator'. "
             "Merge only true duplicates; keep different products apart. Respond as JSON: "
             "{\"merges\": [{\"canonical\": str, \"labels\": [str]}]} listing only groups of 2+ labels.")


def cluster_universe(store: str = settings.store, batch: int = 120, workers: int = 6) -> dict:
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from .collect import slugify
    from .discover import write_retry

    with connect_read_live() as con:
        terms = sorted(set(clusterable(con)), key=_batch_key)
    if not terms:
        return {"terms": 0}
    if not write_retry(lambda c: budget.guard(c, "deepseek", "topics", len(terms))):
        return {"terms": len(terms), "skipped": "budget"}
    batches = [terms[i:i + batch] for i in range(0, len(terms), batch)]
    clients: list[DeepSeekClient] = []

    def run(b: list[str]) -> list[dict]:
        ds = DeepSeekClient()
        clients.append(ds)
        allowed = set(b)
        for attempt in range(3):
            try:
                got = ds.cluster_topics(b)
                break
            except Exception as e:
                log.warning("cluster batch failed (%s), retry %d", e, attempt + 1)
        else:
            got = []
        seen, out = set(), []
        for t in got:
            kws = [k.strip().lower() for k in t.get("keywords", []) if k.strip().lower() in allowed and k.strip().lower() not in seen]
            seen.update(kws)
            if kws and t.get("label"):
                out.append({"label": t["label"].strip(), "description": t.get("description") or "", "keywords": kws})
        out += [{"label": k, "description": "", "keywords": [k]} for k in b if k not in seen]   # never lose a term
        return out

    raw: list[dict] = []
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(run, batches), 1):
            raw += res
            log.info("cluster batches %d/%d", i, len(batches))
    # merge identical labels, then near-duplicates (labels sorted so similar names share a chunk)
    topics: dict[str, dict] = {}
    for t in raw:
        tid = slugify(t["label"])
        cur = topics.setdefault(tid, {"label": t["label"], "description": t["description"], "keywords": []})
        cur["keywords"] += t["keywords"]
    multi = sorted((tid for tid in topics if len(topics[tid]["keywords"]) >= 1), key=lambda t: topics[t]["label"].lower())
    chunks = [multi[i:i + 300] for i in range(0, len(multi), 300)]

    def merge(chunk: list[str]) -> list[dict]:
        ds = DeepSeekClient()
        clients.append(ds)
        payload = [{"label": topics[t]["label"], "examples": topics[t]["keywords"][:4]} for t in chunk]
        try:
            return ds.json_chat(MERGE_SYS, json.dumps(payload)).get("merges", [])
        except Exception as e:
            log.warning("merge chunk failed: %s", e)
            return []

    by_label = {topics[t]["label"]: t for t in topics}
    n_merged = 0
    with ThreadPoolExecutor(workers) as ex:
        for merges in ex.map(merge, chunks):
            for g in merges:
                ids = [by_label[x] for x in g.get("labels", []) if x in by_label and by_label[x] in topics]
                if len(ids) < 2:
                    continue
                keep = max(ids, key=lambda t: len(topics[t]["keywords"]))
                topics[keep]["label"] = g.get("canonical") or topics[keep]["label"]
                for t in ids:
                    if t != keep:
                        topics[keep]["keywords"] += topics.pop(t)["keywords"]
                        n_merged += 1
    now = dt.datetime.now()
    trows = [{"topic_id": tid, "label": t["label"], "description": t["description"], "created_at": now} for tid, t in topics.items()]
    krows = [{"topic_id": tid, "store": store, "keyword": k, "weight": 1.0} for tid, t in topics.items() for k in dict.fromkeys(t["keywords"])]

    def save(c):
        c.execute("DELETE FROM topic_keywords WHERE store=?", [store])
        c.execute("DELETE FROM topics")
        c.execute("DELETE FROM factor_values WHERE entity_type='topic'")
        upsert(c, "topics", trows)
        upsert(c, "topic_keywords", krows)
        cost = sum(x.cost_usd() for x in clients)
        budget.record(c, "deepseek", "topics", sum(x.usage_in + x.usage_out for x in clients), cost, f"{len(terms)} keywords")
        return cost
    cost = write_retry(save)
    return {"terms": len(terms), "topics": len(trows), "merged": n_merged, "cost_usd": round(cost, 3)}


HIDE_KINDS = (("label:brand", "brand"), ("games", "games"), ("non-latin", "non-latin"), ("excluded term", "excluded term"),
              ("buildability<", "not buildable"), ("label:rubbish", "rubbish"), ("thin sector", "thin sector"), ("coverage", "thin sector"))


def _hide_kind(why: str) -> str:
    return next((name for sub, name in HIDE_KINDS if sub in (why or "")), "hidden")


def _hidden_summary(kws: list[str], opp: dict) -> str:
    n: dict[str, int] = {}
    for k in kws:
        kind = _hide_kind(opp[k].get("why", ""))
        n[kind] = n.get(kind, 0) + 1
    return " · ".join(f"{kind} {c}" for kind, c in sorted(n.items(), key=lambda x: -x[1]))


def build_topic_index(con, store: str = settings.store, snapshot_date: dt.date | None = None) -> int:
    """Score every topic from its scoreable members (see module docstring). Returns the number of topic rows written,
    hidden-only topics included."""
    d = snapshot_date or today()
    members: dict[str, list[str]] = {}
    for t, k in con.execute("SELECT topic_id, keyword FROM topic_keywords WHERE store=?", [store]).fetchall():
        members.setdefault(t, []).append(k)
    if not members:
        return 0
    labels = dict(con.execute("SELECT topic_id, label FROM topics").fetchall())
    opp = _latest_opp(con)
    F, M = keyword_factors(con, ('demand', 'difficulty', 'trending', 'weak_spots', 'search_volume', 'breadth', 'concentration', 'entrant_share',
                                 'indie_share', 'buildability', 'intent', 'competition', 'cpc', 'trend_slope', 'attainable'), composite_date(con) or d)
    usd = {e: (mm.get("attainable") or {}).get("attainable_usd") for e, mm in M.items()}
    spd_of = lambda k: searches_per_day(F.get(k, {}).get("demand"))
    now, out = dt.datetime.now(), []
    for t, kws in members.items():
        known = [k for k in kws if k in opp]
        ms = sorted((k for k in known if not opp[k]["hard_hidden"]), key=lambda k: -(opp[k]["opp"] or 0))
        hs = sorted((k for k in known if opp[k]["hard_hidden"]), key=lambda k: -spd_of(k))
        if not ms and not hs:
            continue
        is_hidden = not ms
        use = ms or hs          # a hidden-only topic is scored from its hidden members, then flagged
        vals = [opp[k]["opp"] or 0 for k in use]
        strong = 0 if is_hidden else sum(1 for k in use if opp[k].get("grade") in ("A", "B", "C"))
        spd = sum(spd_of(k) for k in use)
        # small-traffic penalty: an app idea needs total search traffic, not one well-scored phrase (-10% at 5/day, 0 at 1,000+)
        traffic_adj = 0.9 + 0.1 * min(1.0, max(0.0, (math.log10(max(spd, 1)) - math.log10(5)) / (3 - math.log10(5))))
        score = round(min(100.0, (0.75 * vals[0] + 0.25 * sum(vals[:3]) / len(vals[:3]) + min(6.0, 2 * math.log2(1 + strong))) * traffic_adj), 1)
        grade, best = _grade(score), use[0]
        best_act = opp[best].get("action")
        if not is_hidden and all(opp[k].get("grade") == "?" for k in use):
            grade, best_act = "?", "Unproven"      # every member is too thin to judge: unknown, not bad
            if all(opp[k].get("action") == "No data" for k in use):
                best_act = "No data"
        action = ("Avoid" if is_hidden else best_act if grade == "?" else
                  "Build now" if grade in "AB" and best_act == "Build now" else
                  "Watch" if grade in "ABC" and best_act in ("Build now", "Watch") else "Avoid")
        pops = [F.get(k, {}).get("demand") or 0 for k in use]
        difs = [F[k]["difficulty"] for k in use if F.get(k, {}).get("difficulty") is not None]
        trs = [F[k]["trending"] for k in use if F.get(k, {}).get("trending") is not None]
        band = usd.get(best)
        why = _hidden_summary(hs, opp) if hs else ""
        if is_hidden:
            reason = f"hidden: all {len(hs)} searches fail a hard rule ({why}) · ~{spd:,.0f} searches/day in total"
        else:
            reason = (f"{len(ms)} searches ({strong} graded A-C) · best: “{best}” {opp[best].get('grade')} · ~{spd:,.0f} searches/day in total"
                      + (f" · p5-p20 apps earn ~${band:,.0f}/mo" if band else "") + (f" · +{len(hs)} hidden" if hs else ""))
        top = [{"keyword": k, "opp": opp[k]["opp"], "grade": opp[k].get("grade"), "action": opp[k].get("action"),
                "popularity": F.get(k, {}).get("demand"), "difficulty": F.get(k, {}).get("difficulty"),
                "weak_spots": F.get(k, {}).get("weak_spots"), "trending": F.get(k, {}).get("trending"),
                "band_usd": usd.get(k), "reason": opp[k].get("reason")} for k in ms]
        hidden = [{"keyword": k, "popularity": F.get(k, {}).get("demand"), "opp": opp[k]["opp"], "grade": opp[k].get("grade"),
                   "kind": _hide_kind(opp[k].get("why", "")), "why": opp[k].get("why", "")} for k in hs[:150]]
        meta = {"grade": grade, "action": action, "reason": reason, "label": labels.get(t, t), "best": best,
                "n_members": len(ms), "n_strong": strong, "n_hidden": len(hs), "hidden": is_hidden, "hidden_kinds": why,
                "traffic_adj": round(traffic_adj, 3), "searches_per_day": round(spd), "members": top, "hidden_members": hidden,
                "parts": opp[best].get("parts")}
        base = {"entity_type": "topic", "entity_id": t, "snapshot_date": d, "computed_at": now}
        out += [{**base, "metric": "opp_score", "value": score, "meta": json.dumps(meta)},
                {**base, "metric": "demand", "value": max(pops), "meta": None},
                {**base, "metric": "searches_per_day", "value": round(spd), "meta": None}]
        if is_hidden:   # same shape as the keyword 'composite' meta, so the dashboard's hidden filter / grey rows work unchanged
            out.append({**base, "metric": "composite", "value": score,
                        "meta": json.dumps({"excluded": True, "excluded_reason": f"all {len(hs)} searches hidden: {why}", "label": "hidden"})})
        if difs:
            out.append({**base, "metric": "difficulty", "value": round(min(difs), 1), "meta": json.dumps({"rollup": "min (easiest member)"})})
        if trs:
            out.append({**base, "metric": "trending", "value": round(max(trs, key=abs), 1), "meta": None})
        if band:   # same shape as the keyword 'attainable' factor so the dashboard columns and map work unchanged
            out.append({**base, "metric": "attainable", "value": band, "meta": json.dumps({"attainable_usd": band, "from": best})})
        for mname in ("weak_spots", "breadth", "concentration", "entrant_share", "indie_share", "buildability", "intent", "competition", "cpc", "trend_slope"):
            if F.get(best, {}).get(mname) is not None:   # best member's values so topic rows carry the same columns as keywords
                out.append({**base, "metric": mname, "value": F[best][mname], "meta": json.dumps({"from": best})})
    con.execute("DELETE FROM factor_values WHERE entity_type='topic' AND snapshot_date=?", [d])
    # topics that were merged away or re-clustered must not linger as stale rows from earlier dates
    con.execute("DELETE FROM factor_values WHERE entity_type='topic' AND entity_id NOT IN (SELECT DISTINCT topic_id FROM topic_keywords WHERE store=?)", [store])
    upsert(con, "factor_values", out)
    return sum(1 for r in out if r["metric"] == "opp_score")


def _merge_near_duplicates(topics: dict[str, dict], clients: list, workers: int = 6, chunk_size: int = 300) -> int:
    """Fold near-duplicate labels of `topics` (modified in place) into one, using MERGE_SYS. Returns merges done."""
    from ..sources.deepseek import DeepSeekClient
    ids = sorted(topics, key=lambda t: topics[t]["label"].lower())
    chunks = [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]

    def merge(chunk: list[str]) -> list[dict]:
        ds = DeepSeekClient()
        clients.append(ds)
        payload = [{"label": topics[t]["label"], "examples": topics[t]["keywords"][:4]} for t in chunk]
        try:
            return ds.json_chat(MERGE_SYS, json.dumps(payload)).get("merges", [])
        except Exception as e:
            log.warning("merge chunk failed: %s", e)
            return []
    by_label = {}
    for t in topics:
        by_label.setdefault(topics[t]["label"], t)
    n_merged = 0
    with ThreadPoolExecutor(workers) as ex:
        for merges in ex.map(merge, chunks):
            for g in merges:
                tids = list(dict.fromkeys(by_label[x] for x in g.get("labels", []) if x in by_label and by_label[x] in topics))
                if len(tids) < 2:
                    continue
                keep = max(tids, key=lambda t: len(topics[t]["keywords"]))
                topics[keep]["label"] = g.get("canonical") or topics[keep]["label"]
                for t in tids:
                    if t != keep:
                        topics[keep]["keywords"] += topics.pop(t)["keywords"]
                        n_merged += 1
    return n_merged


def attach_hidden(store: str = settings.store, batch: int = 120, workers: int = 6, min_pop: float = 5) -> dict:
    """Put the hidden keywords (brands, games, thin sectors, non-latin...) into topics as well, so the dashboard can show
    them next to the visible searches of the same product. DeepSeek assigns each to an existing topic label when one
    fits, else to a new topic; near-duplicate new labels are merged. Topic scores ignore hidden members."""
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from .collect import slugify
    from .discover import write_retry

    with connect_read_live() as con:
        opp = _latest_opp(con)
        pop = dict(con.execute("""SELECT entity_id, value FROM factor_values WHERE entity_type='keyword' AND metric='demand'
                                  QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""").fetchall())
        done = {k for (k,) in con.execute("SELECT keyword FROM topic_keywords WHERE store=?", [store]).fetchall()}
        topics = {t: {"label": l, "description": d or ""} for t, l, d in con.execute("SELECT topic_id, label, description FROM topics").fetchall()}
    terms = sorted({k for k, r in opp.items() if r["hard_hidden"] and k not in done and (pop.get(k) or 0) >= min_pop}, key=_batch_key)
    if not terms:
        return {"terms": 0}
    if not write_retry(lambda c: budget.guard(c, "deepseek", "topics", len(terms))):
        return {"terms": len(terms), "skipped": "budget"}
    labels = sorted({t["label"] for t in topics.values()})
    by_label = {t["label"].lower(): tid for tid, t in topics.items()}
    batches = [terms[i:i + batch] for i in range(0, len(terms), batch)]
    clients: list[DeepSeekClient] = []

    def run(b: list[str]) -> list[dict]:
        ds = DeepSeekClient()
        clients.append(ds)
        allowed = set(b)
        for attempt in range(3):
            try:
                got = ds.cluster_topics(b, known_labels=labels)
                break
            except Exception as e:
                log.warning("attach batch failed (%s), retry %d", e, attempt + 1)
        else:
            got = []
        seen, out = set(), []
        for t in got:
            kws = [k.strip().lower() for k in t.get("keywords", []) if k.strip().lower() in allowed and k.strip().lower() not in seen]
            seen.update(kws)
            if kws and t.get("label"):
                out.append({"label": t["label"].strip(), "description": t.get("description") or "", "keywords": kws})
        out += [{"label": k, "description": "", "keywords": [k]} for k in b if k not in seen]   # never lose a term
        return out

    raw: list[dict] = []
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(run, batches), 1):
            raw += res
            log.info("attach-hidden batches %d/%d", i, len(batches))
    assign: dict[str, str] = {}            # keyword -> existing topic
    new: dict[str, dict] = {}              # new topics (hidden-only)
    for t in raw:
        tid = by_label.get(t["label"].lower())
        if tid is None and slugify(t["label"]) in topics:
            tid = slugify(t["label"])
        if tid is not None:
            for k in t["keywords"]:
                assign[k] = tid
            continue
        cur = new.setdefault(slugify(t["label"]), {"label": t["label"], "description": t["description"], "keywords": []})
        cur["keywords"] += t["keywords"]
    n_merged = _merge_near_duplicates(new, clients, workers) if len(new) > 1 else 0
    now = dt.datetime.now()
    trows = [{"topic_id": tid, "label": t["label"], "description": t["description"], "created_at": now} for tid, t in new.items()]
    krows = ([{"topic_id": tid, "store": store, "keyword": k, "weight": 1.0} for k, tid in assign.items()]
             + [{"topic_id": tid, "store": store, "keyword": k, "weight": 1.0} for tid, t in new.items() for k in dict.fromkeys(t["keywords"])])

    def save(c):
        upsert(c, "topics", trows)
        upsert(c, "topic_keywords", krows)
        cost = sum(x.cost_usd() for x in clients)
        budget.record(c, "deepseek", "topics_hidden", sum(x.usage_in + x.usage_out for x in clients), cost, f"{len(terms)} hidden keywords")
        return build_topic_index(c, store), cost
    scored, cost = write_retry(save)
    return {"terms": len(terms), "to_existing": len(assign), "new_topics": len(trows), "merged": n_merged, "scored": scored, "cost_usd": round(cost, 3)}


MERGE2_SYS = ("You get App Store product-topic labels with example searches, sorted alphabetically. Find labels that are the "
              "SAME product idea at the same level, e.g. 'Baby games' and 'Baby games for toddlers', 'AI video generator' and "
              "'Free AI video maker'. Do NOT merge a specific product into a broader one when they would be different apps "
              "(e.g. 'Blood pressure tracker' vs 'Heart rate monitor', 'PDF scanner' vs 'PDF editor'). Respond as JSON: "
              "{\"merges\": [{\"canonical\": str, \"labels\": [str]}]} listing only groups of 2+ labels.")


def merge_topics(store: str = settings.store, workers: int = 6) -> dict:
    """Second merge pass over the EXISTING topics (no re-clustering): near-duplicate labels become one topic.
    Two orderings are tried (by label, by first member word) so duplicates with different first words still meet."""
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from .discover import write_retry
    with connect_read_live() as con:
        topics = {t: {"label": l, "description": d or "", "keywords": []} for t, l, d in con.execute("SELECT topic_id, label, description FROM topics").fetchall()}
        for t, k in con.execute("SELECT topic_id, keyword FROM topic_keywords WHERE store=? ORDER BY topic_id, keyword", [store]).fetchall():
            if t in topics:
                topics[t]["keywords"].append(k)
    if not topics:
        return {"topics": 0}
    clients: list[DeepSeekClient] = []
    n_merged = 0
    for order in ("label", "word"):
        key = (lambda t: topics[t]["label"].lower()) if order == "label" else (lambda t: " ".join(sorted((topics[t]["keywords"] or [topics[t]["label"]])[0].split(), key=len, reverse=True)))
        ids = sorted(topics, key=key)
        chunks = [ids[i:i + 250] for i in range(0, len(ids), 250)]

        def run(chunk):
            ds = DeepSeekClient()
            clients.append(ds)
            payload = [{"label": topics[t]["label"], "examples": topics[t]["keywords"][:4]} for t in chunk]
            try:
                return ds.json_chat(MERGE2_SYS, json.dumps(payload)).get("merges", [])
            except Exception as e:
                log.warning("merge chunk failed: %s", e)
                return []
        by_label = {}
        for t in topics:
            by_label.setdefault(topics[t]["label"], t)
        with ThreadPoolExecutor(workers) as ex:
            for merges in ex.map(run, chunks):
                for g in merges:
                    tids = [by_label[x] for x in g.get("labels", []) if x in by_label and by_label[x] in topics]
                    tids = list(dict.fromkeys(tids))
                    if len(tids) < 2:
                        continue
                    keep = max(tids, key=lambda t: len(topics[t]["keywords"]))
                    topics[keep]["label"] = g.get("canonical") or topics[keep]["label"]
                    for t in tids:
                        if t != keep:
                            topics[keep]["keywords"] += topics.pop(t)["keywords"]
                            n_merged += 1
        log.info("merge pass by %s: %d topics left", order, len(topics))
    now = dt.datetime.now()
    trows = [{"topic_id": tid, "label": t["label"], "description": t["description"], "created_at": now} for tid, t in topics.items()]
    krows = [{"topic_id": tid, "store": store, "keyword": k, "weight": 1.0} for tid, t in topics.items() for k in dict.fromkeys(t["keywords"])]

    def save(c):
        c.execute("DELETE FROM topic_keywords WHERE store=?", [store])
        c.execute("DELETE FROM topics")
        upsert(c, "topics", trows)
        upsert(c, "topic_keywords", krows)
        cost = sum(x.cost_usd() for x in clients)
        budget.record(c, "deepseek", "topics_merge", len(trows), cost, f"{n_merged} merged")
        return build_topic_index(c, store), cost
    scored, cost = write_retry(save)
    return {"topics": len(trows), "merged": n_merged, "scored": scored, "cost_usd": round(cost, 3)}
