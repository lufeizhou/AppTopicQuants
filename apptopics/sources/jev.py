"""TypeSafe Jev ("System One" model): fast, cheap classification with calibrated confidence (2026-11-06).

Jev answers typed questions (Noul = yes/no probability, Choice = one label + confidence) about a `state`; it does not
write text. One request = one state + any number of named questions (64k tokens per request, 32k per question).
Price: $0.042 per million input tokens, output free (docs.typesafe.ai/models). Key: TYPESAFE_API_KEY in .env.
Model: pinned to a versioned id (JEV_MODEL, default jev-1.14.0) so tuned thresholds do not drift.

Candidate for every labelling / choice job in the pipeline (user, 2026-11-06), once it passes the same checks as the
current method: see scripts in the Step 2 CHANGELOG entry (200 random hand-labelled pairs, 300 hard ones).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import _env
from ..registry import sources

USD_PER_M_INPUT = 0.042
logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)   # the SDK logs every request at INFO


@sources.register("jev")
@dataclass
class JevClient:
    api_key: str = field(default_factory=lambda: _env("TYPESAFE_API_KEY"))
    model: str = field(default_factory=lambda: _env("JEV_MODEL", "jev-1.14.0"))
    usage_in: int = 0
    calls: int = 0
    _client: object = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def cost_usd(self) -> float:
        return self.usage_in / 1e6 * USD_PER_M_INPUT

    def _c(self):
        if self._client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
            self._client = TypeSafeClient(api_key=self.api_key, model=self.model,
                                          retry=RetryPolicy(max_retries=5, backoff_initial=1.0, backoff_max=20.0))
        return self._client

    def ask(self, state, questions: dict) -> dict:
        """One request: state + named questions -> {name: answer object}. Tracks input tokens."""
        r = self._c().system_one(state=state, questions=questions, model=self.model)
        self.calls += 1
        u = getattr(r, "usage", None)
        if u is not None:
            self.usage_in += int(getattr(u, "input_tokens", 0) or 0)
        return dict(r.answers)

    # -- tasks -------------------------------------------------------------------------------------------------
    def competitors(self, keyword: str, apps: list[tuple[str, str]], context: list[str] | None = None,
                    per_call: int = 60) -> dict[int, float]:
        """Step 2: for each (name, description snippet), the probability that someone who typed `keyword` treats the
        app as a real option. Returns {index: p_yes}."""
        from typesafe_sdk import Noul
        state = (f'App Store search: "{keyword}".\n'
                 + (f"Apple's top results for it (shows what the search means): {' | '.join(context)}\n" if context else "")
                 + "Question for each app: would a person who typed this search treat the app as a real option for what "
                   "they want? Yes if it does the same job, even with other words. No if it does a different job and only "
                   "shares a word or topic (a game vs a tool, a store, a hardware companion). Adult or harmful intent: no.")
        out: dict[int, float] = {}
        for s in range(0, len(apps), per_call):
            qs = {f"a{i}": Noul(instructions=f"App: {n} — {d}".strip(" —"))
                  for i, (n, d) in enumerate(apps[s:s + per_call], start=s)}
            ans = self.ask(state, qs)
            out.update({int(k[1:]): float(getattr(v, "noul", 0.0)) for k, v in ans.items()})
        return out

    def choice(self, state, instructions: str, criteria: dict[str, str | None]) -> tuple[str, float, dict]:
        """Generic single-label choice -> (label, confidence, probabilities)."""
        from typesafe_sdk import Choice
        a = self.ask(state, {"q": Choice(instructions=instructions, criteria=criteria)})["q"]
        return a.choice, float(a.confidence), dict(a.probabilities)

    # -- labelling jobs (2026-11-06): used first; DeepSeek only for the unsure cases (pipeline/judges.py) ---------
    @staticmethod
    def _term_state(term: str, apps: list[str]) -> dict:
        import re
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
        first = apps[0].split(":")[0].split(" - ")[0] if apps else ""
        return {"search_term": term, "apple_top_apps": apps or ["(results page not fetched)"],
                "term_equals_or_is_inside_top_app_name": bool(apps) and len(norm(term)) >= 3
                and (norm(term) in norm(first) or norm(first) in norm(term)), "term_length_letters": len(norm(term))}

    def label_term(self, term: str, apps: list[str]) -> dict:
        """need / brand / rubbish (Choice, with confidence) + specificity 1-5 (Score). Same meanings as DeepSeek's
        label_keywords; Jev alone is 94% right on need-vs-not, so callers send confidence < 0.7 to DeepSeek."""
        from typesafe_sdk import Choice, Noul, Score

        from ..metrics.adult import QUESTION as ADULT_Q
        a = self.ask(f'App Store search term: "{term}". Top apps Apple shows for it: {" | ".join(apps) or "(results page not fetched)"}', {
            "label": Choice(instructions="What kind of App Store search term is this?", criteria={
                "need": "the user wants ANY app that does a job, feature or topic (e.g. 'habit tracker', 'pdf scanner', 'white noise', "
                        "'timer', even a vague word). A generic word that happens to be an app's name ('notes', 'calculator') is a need. "
                        "A third-party utility for a brand's device ('vizio tv remote') is a need.",
                "brand": "the user wants one specific existing app, company, store, game, franchise, show, sports league or person, "
                         "including nicknames, abbreviations and misspellings ('me@walmart', 'toca boca', 'ff max').",
                "rubbish": "gibberish, keyboard noise, adult, sexual or illegal content (fraud, piracy, undressing people)."}),
            "spec": Score(instructions="If this is a need: how precisely does the term name ONE app a small developer could build?", criteria=[
                "an everyday word or phrase that names no app need by itself ('real', 'five star')",
                "a whole category or generic tool word ('compass', 'photos', 'kids games', 'sleep')",
                "a real but broad need spanning several kinds of app ('timer', 'budget', 'journal')",
                "one clear job or feature ('timer widget', 'habit tracker', 'pdf scanner')",
                "a specific job with a qualifier, audience or format ('hiit timer', 'baby sleep tracker', 'ai calorie counter')"]),
            "adult": Noul(instructions=ADULT_Q)})
        lab = a["label"].choice
        return {"label": lab, "confidence": float(a["label"].confidence), "p_brand": float(dict(a["label"].probabilities).get("brand", 0.0)),
                "specificity": max(1, min(5, round(float(a["spec"].score) + 1))) if lab == "need" else None,
                "p_adult": float(a["adult"].noul)}

    def brand_score(self, term: str, apps: list[str]) -> dict:
        """p_brand = P(names one specific app/company) x (1 - P(third-party companion utility)). Two narrow questions."""
        from typesafe_sdk import Noul
        a = self.ask(self._term_state(term, apps), {
            "brand": Noul(instructions="Is the search term the name, nickname, abbreviation or misspelling of ONE specific existing app, "
                          "company, store, service, game, show or person, so the user wants that one app rather than any app for a job? "
                          "Example yes: 'duet' when the top app is Duet Dating, 'quran majeed', 'big bus tours', 'nps parks app', 'livescore', "
                          "'keep' (Google Keep), 'minecraft building guide'. Example no: 'habit tracker', 'calculator', 'notes', 'translator'."),
            "comp": Noul(instructions="Is the term a third-party utility FOR a brand's device or platform that any developer can build, such "
                         "as a TV remote for a TV brand ('vizio tv remote', 'chromecast remote', 'lg tv remote'), a cast or mirroring app, or "
                         "a test-prep app for a public exam ('asvab')? Platform words ('iphone', 'apple') do not make a search a brand.")})
        b, c = float(a["brand"].noul), float(a["comp"].noul)
        return {"p_brand": b * (1 - c), "p_name": b, "p_companion": c}

    def buildability(self, term: str, constraints: str) -> dict:
        """score = P(real need) x (1 - max(P(licensed content/data), P(network effects), P(OS/hardware access), P(big game /
        regulated))). Five narrow questions instead of one broad one."""
        from typesafe_sdk import Noul
        a = self.ask({"search_need": term, "team": constraints}, {
            "need": Noul(instructions="Is this a real, understandable app need that an app could serve (not gibberish, a word fragment, a "
                         "domain-like token, or the name of one specific company or app)?"),
            "lic": Noul(instructions="Does serving this need require licensed content or a data feed the team would have to buy or scrape: "
                        "movie / TV / IPTV streams, sports data, coupon or deal inventory, job listings, speed-camera databases, copyrighted texts?"),
            "net": Noul(instructions="Does the app only work with network effects: a marketplace with buyers and sellers, a social or dating "
                        "network, a ride or delivery service?"),
            "os": Noul(instructions="Does it need heavy infrastructure, special hardware, or OS access an iPhone app does not get (recovering "
                       "deleted files, deep system cleaning, call interception, real phone hardware control)?"),
            "big": Noul(instructions="Is it a large 3D or open-world game, or a regulated service (banking, medical diagnosis, gambling)?")})
        p = {k: float(v.noul) for k, v in a.items()}
        blocker = max(p["lic"], p["net"], p["os"], p["big"])
        why = max(("lic", "net", "os", "big"), key=lambda k: p[k])
        return {"score": p["need"] * (1 - blocker), "parts": p, "main_blocker": why}
