"""DeepSeek (OpenAI-compatible) helpers: topic clustering, brand classification,
and a simulated consumer panel that votes on topic attractiveness."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import settings
from ..registry import sources

PERSONAS = [
    ("busy_parent", "a busy parent in the US, 38, price-sensitive, phone-first"),
    ("fitness_enthusiast", "a 27-year-old gym-goer who pays for apps that save time"),
    ("student", "a 20-year-old college student with little disposable income"),
    ("professional", "a 45-year-old knowledge worker who expenses productivity tools"),
    ("retiree", "a 66-year-old retiree, cautious with subscriptions, values simplicity"),
]


@sources.register("deepseek")
@dataclass
class DeepSeekClient:
    api_key: str = field(default_factory=lambda: settings.deepseek_api_key)
    base_url: str = field(default_factory=lambda: settings.deepseek_base_url)
    model: str = field(default_factory=lambda: settings.deepseek_model)
    _client: object = None
    usage_in: int = 0
    usage_out: int = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def cost_usd(self) -> float:
        from ..budget import load
        p = load().get("unit_prices", {})
        return self.usage_in / 1e6 * p.get("deepseek_in_per_m", 0.14) + self.usage_out / 1e6 * p.get("deepseek_out_per_m", 0.28)

    def _oa(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=180.0, max_retries=2)
        return self._client

    HARD_TIMEOUT = 240   # seconds per call, wall clock: a trickling response can otherwise hang for many minutes

    def json_chat(self, system: str, user: str, temperature: float = 0.2) -> dict:
        import threading
        box: dict = {}

        def work():   # daemon thread: a hung call can't keep the process alive at exit (ThreadPoolExecutor would)
            try:
                box["r"] = self._json_chat(system, user, temperature)
            except BaseException as e:
                box["e"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(self.HARD_TIMEOUT)
        if t.is_alive():
            raise TimeoutError(f"DeepSeek call exceeded {self.HARD_TIMEOUT}s")
        if "e" in box:
            raise box["e"]
        return box["r"]

    def _json_chat(self, system: str, user: str, temperature: float = 0.2) -> dict:
        r = self._oa().chat.completions.create(
            model=self.model, temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if getattr(r, "usage", None):
            self.usage_in += int(getattr(r.usage, "prompt_tokens", 0) or 0)
            self.usage_out += int(getattr(r.usage, "completion_tokens", 0) or 0)
        return json.loads(r.choices[0].message.content or "{}")

    # -- tasks ---------------------------------------------------------------
    def classify_brands(self, keywords: list[str]) -> dict[str, float]:
        """Return keyword -> probability that it is a brand/app-name navigational query."""
        sys = ("You classify App Store search terms. For each term give p_brand in [0,1]: the probability "
               "the user is looking for one specific existing app or company (navigational), versus a generic "
               "need (e.g. 'hiit timer'). Respond as JSON: {\"items\": [{\"term\": str, \"p_brand\": float}]}")
        out: dict[str, float] = {}
        for i in range(0, len(keywords), 80):
            chunk = keywords[i:i + 80]
            data = self.json_chat(sys, json.dumps(chunk))
            for it in data.get("items", []):
                out[it["term"]] = float(it.get("p_brand", 0.5))
        return out

    LABELS = ("need", "brand", "rubbish")

    def label_keywords(self, items: list[dict]) -> dict[str, dict]:
        """items: [{term, top_apps:[names]}] -> term -> {label, specificity, p_brand, reason}.
        label = type: need (any app could serve it) / brand (one specific app, company, game, show, person) / rubbish.
        specificity 1-5 for needs: how precisely the term names one app a new developer could build (see config
        [step1.labels]); None for brand and rubbish."""
        sys = ("You label US App Store search terms. Each term comes with the names of the top apps Apple shows for it "
               "(the list may be empty when the results page has not been fetched).\n"
               "type:\n"
               "- 'need' = the user wants ANY app that does a job, feature or topic (e.g. 'habit tracker', 'pdf scanner', "
               "'white noise', 'fasting', 'timer', 'compass', even a vague word like 'real');\n"
               "- 'brand' = the user wants one specific existing app, company, store, game, franchise, show, sports league or "
               "person, including nicknames, abbreviations and misspellings (e.g. 'me@walmart', 'toca boca', 'ff max', 'cai' "
               "when the top app is Character AI). If the top apps are all one brand and the term is its name or part of it, "
               "it is 'brand'. A generic word that happens to be an app's name ('notes', 'calculator') is still a 'need';\n"
               "- 'rubbish' = gibberish, keyboard noise, adult or illegal content.\n"
               "specificity (needs only, integer 1-5): how precisely the term names ONE app a small developer could build:\n"
               "5 = a specific job with a qualifier, audience or format ('hiit timer', 'baby sleep tracker', 'receipt scanner "
               "for taxes', 'ai calorie counter');\n"
               "4 = one clear job or feature ('timer widget', 'habit tracker', 'pdf scanner', 'white noise', 'mood journal');\n"
               "3 = a real but broad need that spans several kinds of app ('timer', 'budget', 'journal', 'workout');\n"
               "2 = a whole category or generic tool word ('compass', 'photos', 'learning', 'kids games', 'sleep');\n"
               "1 = an everyday word or phrase that names no app need by itself ('real', 'wonder', 'treat', 'five star', 'd').\n"
               "For brand and rubbish, specificity = null. p_brand in [0,1] = probability the term is navigational (looking "
               "for one app). reason: at most 8 words.\n"
               "Respond as JSON: {\"items\": [{\"term\": str, \"label\": str, \"specificity\": int|null, \"p_brand\": float, \"reason\": str}]}")
        data = self.json_chat(sys, json.dumps(items, ensure_ascii=False), temperature=0.0)
        out = {}
        for it in data.get("items", []):
            lab = str(it.get("label", "")).lower()
            lab = {"vague": "need", "broad": "need"}.get(lab, lab)
            lab = lab if lab in self.LABELS else "need"
            sp = it.get("specificity")
            try:
                sp = max(1, min(5, int(sp))) if sp is not None and lab == "need" else None
            except (TypeError, ValueError):
                sp = None
            if lab == "need" and sp is None:
                sp = 3
            out[str(it.get("term", "")).lower()] = {"label": lab, "specificity": sp, "p_brand": float(it.get("p_brand") or 0.5),
                                                    "reason": str(it.get("reason", ""))[:120]}
        return out

    def expand_topic(self, topic: str, n: int = 80) -> list[dict]:
        """Brainstorm App Store searches around a topic in many directions so research does not stay in a narrow
        lane: synonyms, audiences, jobs-to-be-done, problems/pain, features/formats, adjacent needs, situations."""
        sys = ("You are an App Store keyword researcher. Given a topic, list about %d distinct search phrases (1-5 words, "
               "lowercase, no brand names) that real US users type when looking for an app related to it. Cover ALL of "
               "these directions, roughly evenly: direct synonyms; specific audiences (kids, seniors, students, adhd, "
               "couples, pros); jobs-to-be-done; problems and pain points; features and formats (widget, offline, ai, "
               "voice, photo, apple watch); adjacent needs people have at the same moment; niche sub-genres. "
               "Respond as JSON: {\"items\": [{\"term\": str, \"direction\": str}]}") % n
        data = self.json_chat(sys, json.dumps({"topic": topic}), temperature=0.6)
        return [{"term": str(i.get("term", "")).strip().lower(), "direction": str(i.get("direction", ""))}
                for i in data.get("items", []) if i.get("term")]

    def suggest_metadata(self, app: str, keywords: list[dict]) -> dict:
        """Title (<=30), subtitle (<=30), keyword field (<=100, comma-separated, no spaces, no words repeated from
        title/subtitle) that cover as many of the given high-value keywords as possible."""
        sys = ("You write App Store metadata for ASO. Constraints: title <= 30 characters and must start with the app's "
               "brand name; subtitle <= 30 characters; keyword field <= 100 characters, comma-separated, no spaces after "
               "commas, no word repeated from title or subtitle, no competitor brand names, no Apple trademarks. Apple "
               "combines words across the three fields, so cover the highest-value keywords (listed best first) with as few "
               "words as possible, put the most valuable phrase in the title. Be truthful to the app description. "
               "Respond as JSON: {\"title\": str, \"subtitle\": str, \"keywords\": str, \"covers\": [str], \"why\": str}")
        return self.json_chat(sys, json.dumps({"app": app, "keywords": keywords}), temperature=0.3)

    def longtail_variants(self, seeds: list[str], per_seed: int = 6) -> dict[str, list[str]]:
        """seed -> long-tail App Store searches real users would type (modifiers: audience, feature, platform,
        constraint), each a distinct product angle a small app could own."""
        from concurrent.futures import ThreadPoolExecutor
        sys = (f"For each App Store search term, give {per_seed} longer search phrases (2-5 words) that real US users "
               "type when they want a more specific app: audience (for kids, for seniors, for adhd), feature (widget, "
               "offline, with friends, ai), format or constraint (no subscription, minimal, simple). Each must be a "
               "plausible search, lowercase, no brand names, no near-duplicates. Respond as JSON: "
               "{\"items\": [{\"seed\": str, \"variants\": [str]}]}")
        out: dict[str, list[str]] = {}

        def one(chunk):
            try:
                return self.json_chat(sys, json.dumps(chunk), temperature=0.4).get("items", [])
            except Exception:
                return []
        with ThreadPoolExecutor(8) as ex:
            for items in ex.map(one, [seeds[i:i + 25] for i in range(0, len(seeds), 25)]):
                for it in items:
                    out[str(it.get("seed", "")).lower()] = [str(v).strip().lower() for v in it.get("variants", []) if v]
        return out

    def buildability(self, keywords: list[str], constraints: str) -> dict[str, dict]:
        """keyword -> {score 0..1, reason}: can a team with these constraints ship a credible app for this need?"""
        sys = ("You assess whether a small team can realistically build and ship a competitive app for an App Store "
               f"search need, given these constraints:\n{constraints}\nScore 0..1 (1 = clearly buildable). Penalize needs that "
               "require licensed content, network effects, heavy infrastructure, regulation, or hardware. "
               "Respond as JSON: {\"items\": [{\"term\": str, \"score\": float, \"reason\": str}]}")
        from concurrent.futures import ThreadPoolExecutor
        out: dict[str, dict] = {}

        def one(chunk):
            try:
                return self.json_chat(sys, json.dumps(chunk)).get("items", [])
            except Exception:
                return []
        with ThreadPoolExecutor(8) as ex:
            for items in ex.map(one, [keywords[i:i + 50] for i in range(0, len(keywords), 50)]):
                for it in items:
                    out[str(it.get("term", "")).lower()] = {"score": float(it.get("score", 0.5)), "reason": it.get("reason", "")}
        return out

    def brand_check(self, items: list[dict]) -> dict[str, dict]:
        """items: {term, apps: top result names} -> term -> {p_brand, kind, reason}. Second pass for searches that
        name a company, product line, device or app (vizio tv app, mosaique+) and so are not an open need."""
        sys = ("You check App Store search terms that were labelled as generic needs. For each, using the top apps it returns, "
               "give p_brand in [0,1]: the probability the term looks for one specific existing app, game or company "
               "(navigational: e.g. 'minecraft building guide', 'mosaique+', 'premier league fantasy football', 'magic sort'), "
               "versus an open need any new app could serve ('tv remote', 'hiit timer', 'ai photo generator'). A brand used as a "
               "platform word ('iphone', 'android', 'apple watch') is NOT a brand, and neither is a third-party utility for a "
               "brand's device ('vizio tv remote', 'tcl remote', 'chromecast remote', 'lg tv app'): those are needs. "
               "kind: one of company, device, app, franchise, none. Respond as JSON: "
               "{\"items\": [{\"term\": str, \"p_brand\": float, \"kind\": str, \"reason\": str}]}")
        from concurrent.futures import ThreadPoolExecutor
        out: dict[str, dict] = {}

        def one(chunk):
            try:
                return self.json_chat(sys, json.dumps(chunk, ensure_ascii=False)).get("items", [])
            except Exception:
                return []
        with ThreadPoolExecutor(8) as ex:
            for res in ex.map(one, [items[i:i + 40] for i in range(0, len(items), 40)]):
                for it in res:
                    out[str(it.get("term", "")).lower()] = {"p_brand": float(it.get("p_brand", 0) or 0), "kind": it.get("kind") or "none",
                                                            "reason": it.get("reason", "")}
        return out

    def pain_points(self, keyword: str, reviews: list[dict]) -> dict:
        """Mine negative reviews of the band apps: recurring, fixable complaints = opportunity for a newcomer."""
        sys = ("You read 1-3 star App Store reviews of apps competing for one search need and extract recurring, "
               "FIXABLE complaints (bugs, missing features, pricing anger, bad UX), ignoring one-off rants. "
               "opportunity in [0,1] = how much a well-built newcomer could win users by fixing these. "
               "Respond as JSON: {\"opportunity\": float, \"pain_points\": [{\"issue\": str, \"share\": float, \"example\": str}]}")
        return self.json_chat(sys, json.dumps({"search_need": keyword, "reviews": reviews}))

    def cluster_topics(self, keywords: list[str], known_labels: list[str] | None = None) -> list[dict]:
        """Group keywords into fine-grained topics. Returns [{label, description, keywords:[...]}].
        Pass known_labels so later batches reuse earlier topic names instead of inventing near-duplicates."""
        sys = ("Group App Store search terms into fine-grained product topics (e.g. 'Interval / HIIT timers', "
               "'AI calorie photo logging'), NOT broad categories. A topic is one product a single app could be. "
               "Every term goes in exactly one topic; a term can be its own topic. Reuse a label from the known list "
               "when the meaning matches exactly. Respond as JSON: "
               "{\"topics\": [{\"label\": str, \"description\": str, \"keywords\": [str]}]}")
        user = json.dumps({"terms": keywords, "known_labels": known_labels or []})
        return self.json_chat(sys, user).get("topics", [])

    # -- market report (research) -------------------------------------------
    def aspects(self, topic: str, app: str | None = None, n: int = 14) -> list[dict]:
        """Imaginative breakdown of a topic into distinct aspects (sub-needs, audiences, moments, problems) that could
        each be its own app or a signature feature. Each comes with seed App Store searches."""
        sys = ("You are a product strategist for a small iOS studio. Break the topic into about %d DISTINCT aspects a "
               "person dealing with it cares about: sub-needs, problems and side effects, audiences, moments in the day, "
               "jobs-to-be-done, adjacent needs, emerging angles. Be imaginative but concrete (e.g. 'glp-1' -> protein and "
               "muscle loss, nausea and side effects, shot log and dose schedule, food noise, coming off the drug, cost "
               "and insurance, meal ideas for small appetite...). For each give 4-6 seed App Store search phrases "
               "(lowercase, 1-5 words, no brand names) people would really type. Respond as JSON: {\"aspects\": [{\"name\": str "
               "(2-5 words), \"description\": str (one sentence, plain words), \"audience\": str, \"seeds\": [str]}]}") % n
        user = json.dumps({"topic": topic, "our_app": app or ""})
        data = self.json_chat(sys, user, temperature=0.7)
        out = []
        for a in data.get("aspects", []):
            seeds = [str(x).strip().lower() for x in a.get("seeds", []) if str(x).strip()]
            if a.get("name") and seeds:
                out.append({"name": str(a["name"]).strip(), "description": str(a.get("description") or ""),
                            "audience": str(a.get("audience") or ""), "seeds": seeds[:6]})
        return out

    def search_roots(self, topic: str, n: int = 5) -> list[str]:
        """Other words people TYPE for the same topic (generic names, drug/brand names of the condition, abbreviations,
        spelling variants). Only used as autocomplete probes: a root that Apple does not complete is dropped."""
        sys = ("List up to %d short alternative words or spellings people type in App Store search for the same topic "
               "(abbreviations, spelling variants, generic and well-known product names of the thing itself, e.g. glp-1 -> "
               "glp1, ozempic, wegovy, mounjaro, semaglutide). Lowercase, 1-2 words, no app names. JSON: {\"roots\": [str]}") % n
        out = self.json_chat(sys, json.dumps({"topic": topic})).get("roots", [])
        return [str(x).strip().lower() for x in out if str(x).strip()][:n]

    def grounded_aspects(self, topic: str, app: str | None, evidence: dict, n: int = 14) -> list[dict]:
        """Group REAL evidence into aspects. evidence = {searches: [[id, term, searches_per_day]], apps: [[id, name, text]],
        reviews: [[id, text]], reddit: [[id, title]]}. Every aspect cites the evidence ids it rests on."""
        sys = ("You group real market evidence about a topic into about %d DISTINCT aspects (sub-needs, problems, audiences, "
               "moments) that could each be an app or a signature feature. Evidence ids: S = real App Store searches with "
               "searches/day, A = competitor apps, R = 4-5 star review snippets, P = Reddit post titles. Rules: every aspect "
               "must rest on evidence and cite ALL ids that support it (cite generously, an id may support several aspects); "
               "name aspects after what the evidence says, not after general knowledge; prefer aspects backed by several "
               "kinds of evidence; skip pure brand/app-name groups. seeds = 4-6 search phrases for that aspect, taken from "
               "the S list where possible. Then add at most 3 extra aspects you believe matter but that have NO evidence "
               "here, with idea=true and empty evidence. Respond as JSON: {\"aspects\": [{\"name\": str (2-5 words), "
               "\"description\": str (one plain sentence), \"audience\": str, \"seeds\": [str], \"evidence\": [id], "
               "\"idea\": bool}]}") % n
        data = self.json_chat(sys, json.dumps({"topic": topic, "our_app": app or "", **evidence}), temperature=0.3)
        out = []
        for a in data.get("aspects", []):
            if a.get("name"):
                out.append({"name": str(a["name"]).strip(), "description": str(a.get("description") or ""),
                            "audience": str(a.get("audience") or ""),
                            "seeds": [str(x).strip().lower() for x in a.get("seeds", []) if str(x).strip()][:6],
                            "evidence": [str(x).strip().upper() for x in a.get("evidence", [])], "idea": bool(a.get("idea"))})
        return out

    def feature_taxonomy(self, topic: str, apps: list[dict], n: int = 24) -> list[dict]:
        """A shared feature list for a market, from competitor descriptions. apps: [{id, name, text}]."""
        sys = ("You read App Store descriptions of competing apps in one market. Produce ONE shared list of about %d "
               "user-facing features or capabilities that distinguish these apps (e.g. 'Shot reminders', 'Injection site "
               "rotation', 'Protein goal', 'Photo food logging', 'Apple Watch app', 'Doctor report export', 'Community'). "
               "Merge synonyms; skip generic items like 'easy to use' or 'beautiful design'. Respond as JSON: "
               "{\"features\": [{\"id\": str (short snake_case), \"name\": str (2-4 words), \"group\": str (e.g. Tracking, "
               "Food, Coaching, Platform, Social, Data)}]}") % n
        return self.json_chat(sys, json.dumps({"topic": topic, "apps": apps})).get("features", [])

    def feature_matrix(self, features: list[dict], apps: list[dict], topic: str = "", aspects: list[str] | None = None) -> dict:
        """apps: [{id, name, text}] -> {id: {"features": [ids], "what": summary, "model": str, "relevant": bool}}."""
        sys = ("For each app, mark which of the given features it clearly offers according to its description (only "
               "when stated or obvious), write a one-line plain summary of what it is, its business model "
               "(subscription, one-time purchase, free with ads, free), and whether it is relevant: true when someone with the "
               "topic or ANY of its aspects could use it for that need (a general calorie or protein tracker IS relevant to "
               "a protein aspect). relevant=false ONLY for apps from a different domain that merely share a word, e.g. a "
               "golf 'shot tracker' or a basketball app in a medication market, or a meal-kit delivery service. "
               "Respond as JSON: {\"apps\": [{\"id\": str, \"features\": [feature id], \"what\": str, \"model\": str, "
               "\"relevant\": bool}]}")
        data = self.json_chat(sys, json.dumps({"topic": topic, "aspects": aspects or [], "features": features, "apps": apps}))
        return {str(a.get("id")): a for a in data.get("apps", []) if a.get("id")}

    def praise_mining(self, topic: str, reviews: list[dict]) -> dict:
        """Mine 4-5 star reviews: what problem the app solved for them (the pain before), what they love, who they are."""
        sys = ("These are 4 and 5 star App Store reviews of apps in one market. Happy users describe the pain they had "
               "before and what fixed it. Extract: pains_solved (the problem or struggle in their life, not app bugs), "
               "loved_features (what they praise), audiences (who they are, situations), wishes (what they still ask "
               "for even while happy). Give a share estimate 0-1 of reviews mentioning each and one short quote. "
               "Respond as JSON: {\"pains_solved\": [{\"pain\": str, \"share\": float, \"quote\": str}], "
               "\"loved_features\": [{\"feature\": str, \"share\": float, \"quote\": str}], \"audiences\": [{\"who\": str, "
               "\"share\": float}], \"wishes\": [{\"wish\": str, \"share\": float, \"quote\": str}]}")
        return self.json_chat(sys, json.dumps({"topic": topic, "reviews": reviews}))

    def voice_of_users(self, topic: str, aspects: list[str], app_store: dict, reddit: dict) -> dict:
        """Merge App Store 4-5 star review findings with Reddit, TikTok and YouTube posts/comments into one voice-of-users view."""
        sys = ("You merge two sources about one market into a single 'voice of users' for a small app studio. Source A: a "
               "summary of 4-5 star App Store reviews of competing apps. Source B: social posts and comments grouped by "
               "aspect: Reddit (posts, comments), and under 'tiktok' / 'youtube' video titles with views and viewer comments. Find what people struggle with in their life (pains), what they wish an app did (wishes, especially "
               "unmet ones nobody offers), who they are (audiences), what they love in current apps (loved), and the exact "
               "words they use (phrases, useful for App Store copy and search). Tag each item with sources (any of \"Reddit\", "
               "\"TikTok\", \"YouTube\", \"App Store\"), the aspect it belongs to, a strength 0-1 (how often it comes up), and one short real "
               "quote with its source. Do not invent quotes. Respond as JSON: {\"pains\": [{\"pain\": str, \"aspect\": str, "
               "\"sources\": [str], \"strength\": float, \"quote\": str, \"quote_source\": str}], \"wishes\": [same keys with "
               "\"wish\", plus \"unmet\": bool], \"audiences\": [{\"who\": str, \"sources\": [str], \"strength\": float}], "
               "\"loved\": [{\"what\": str, \"sources\": [str], \"strength\": float, \"quote\": str, \"quote_source\": str}], "
               "\"phrases\": [str], \"summary\": str (3 sentences)}")
        return self.json_chat(sys, json.dumps({"topic": topic, "aspects": aspects, "app_store": app_store, "reddit": reddit}))

    def highlights(self, context: dict) -> list[dict]:
        """Three positioning options with signature features, checked against the market facts in context."""
        sys = ("You are a product strategist for a 2-3 person iOS studio aiming at >= $5k/month within 12 months. From "
               "the market facts (aspects with crowdedness and scores, competitor feature coverage, what happy users "
               "say, prices, and our app if given), propose 3 DIFFERENT positioning options. Each: name (a short working title; never the name of an existing competitor "
               "app from the facts; if our app is given, at least one option keeps our app's name), one_liner (what "
               "the app is, plain words), audience, aspects (which aspects it targets), signature_features (3-4; prefer "
               "features with LOW coverage among competitors but backed by user pains or wishes; give the feature's "
               "coverage from the facts when it exists), why_now, price (monthly and yearly in USD, with reason), "
               "keywords (5-8 App Store searches from the facts), risks (2). Respond as JSON: {\"options\": [{\"name\": str, "
               "\"one_liner\": str, \"audience\": str, \"aspects\": [str], \"signature_features\": [{\"feature\": str, "
               "\"coverage\": str, \"why\": str}], \"why_now\": str, \"price\": {\"monthly\": float, \"yearly\": float, "
               "\"reason\": str}, \"keywords\": [str], \"risks\": [str]}]}")
        return self.json_chat(sys, json.dumps(context), temperature=0.5).get("options", [])

    def verdict(self, context: dict) -> dict:
        """Word the overall call of a research report from its facts (the grade is computed by us and given in context)."""
        sys = ("You are a product strategist for a 2-3 person iOS studio aiming at >= $5k/month within 12 months. You get the facts "
               "of a market report: one verdict per aspect with its evidence, positioning options, young winners (apps under 2 years "
               "old already earning the target), who pays for Meta ads, what people struggle with and wish for, prices, and the "
               "search-led keyword scores (these only ask whether an App Store search would carry an app; they are low when nobody "
               "searches the store yet, which is not the same as a bad market). Write the call in plain words, conclusions first. "
               "Respond as JSON: {\"call\": str (at most 12 words: what to do, e.g. 'Go, as a GLP-1 mode inside RightMeal'), "
               "\"not\": str (one line: what NOT to do), \"one_liner\": str (the product in one sentence), "
               "\"reasons\": [{\"title\": str, \"detail\": str (one sentence with the numbers), \"section\": str (which report section shows it: "
               "Aspects, Competitors, TikTok and YouTube, Reddit, Who buys ads, Prices, Feature map, Voice of users, Searches)}] (exactly 3), "
               "\"risks\": [{\"title\": str, \"detail\": str}] (exactly 2), \"next_step\": str (one sentence, concrete), "
               "\"growth\": str (one sentence: where users will come from, given search vs social vs ads)}")
        return self.json_chat(sys, json.dumps(context), temperature=0.3)

    def consumer_vote(self, label: str, description: str, sample_keywords: list[str]) -> list[dict]:
        """Each persona scores willingness to try (0-10) and to pay (0-10) with a one-line rationale."""
        votes = []
        for pid, desc in PERSONAS:
            sys = (f"You are {desc}. You answer honestly as a consumer, not as a marketer. "
                   "Respond as JSON: {\"try\": 0-10, \"pay\": 0-10, \"rationale\": str, \"missing_feature\": str}")
            user = f"App topic: {label}\n{description}\nTypical searches: {', '.join(sample_keywords[:8])}"
            d = self.json_chat(sys, user, temperature=0.7)
            votes.append({"persona": pid, "score": (float(d.get("try", 0)) + 2 * float(d.get("pay", 0))) / 30.0,
                          "rationale": f"{d.get('rationale','')} | missing: {d.get('missing_feature','')}"})
        return votes
