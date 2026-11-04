# AppTopicsQuants: Methodology

*For a human reader. Updated 2026-11-04.*

## 1. The question

We are a small company. We want apps that each earn **at least $5,000 a month within a year**.
That is roughly the top 5-10% of subscription apps (RevenueCat 2026: the top 10% earn over
$2,500 a month at one year). We will almost never be the number one app in a niche, and we do
not need to be. So the model does not ask "where is the most money". It asks:

> In which niches do the apps that are **not** the leaders, the 5th to 20th percentile of
> the niche, still earn our target, and can a newcomer get there?

## 2. The idea in one paragraph

Treat app niches like a quant treats stocks. The **universe** is Apple's list of the ~7,500
most searched App Store terms in the US (a concrete string such as `hiit timer`, never a
category). For each term we take its **sector**: the full page of apps that rank for it (up to
100). Every app gets an **estimated monthly revenue**. We sort the sector by that estimate and
measure what the 5th-20th percentile band earns, how concentrated the money is, and whether
newcomers reach that band. Those and other **factors** are standardized, weighted, and summed
into a **composite**; the ranking is by composite. Everything is snapshotted daily, so the
model can be **backtested** and its parameters **calibrated** against real revenue.

## 3. Words we use

| Term | Meaning |
|---|---|
| Keyword | One App Store search string. Our ticker. |
| Topic | One app idea: a DeepSeek cluster of related searches (`hiit timer`, `tabata timer`, ...). Scored from its members (7b). The dashboard's default view. |
| Sector | The apps on a keyword's results page that actually serve it (keyword words in name, subtitle or description). Apple pads brand and vague searches with unrelated big apps; those are dropped. Topics have no sector of their own; they inherit the best member's numbers. |
| Label | DeepSeek's verdict per keyword: need, brand, vague or rubbish. Brand and rubbish are hidden. |
| Reach | Chance a newcomer gets into the band, falling with difficulty (0.92 at 50, 0.5 at 70, 0.08 at 90). |
| Band | The p5-p20 slice of the sector by estimated revenue. Where a strong newcomer lands. |
| Factor | One number per keyword (or topic) for one aspect. |
| Composite | Weighted sum of z-scored factors. |
| Anchor | A real revenue figure for a real app, used to calibrate the estimator. |

## 4. Where the data comes from

| Source | Gives us | Cost |
|---|---|---|
| Apple Ads Platform API | Weekly popularity (1-100) for the top 500 terms per genre, with history: universe + **trending** | free |
| App Store autocomplete | Real searches users type: the long tail (`habit tracker with friends`) | free |
| Astro (local ASO app) | Popularity + difficulty for any term (long tail, research) | owned |
| Apple iTunes Search / Lookup | Results pages (top 200) and app metadata: price, dates, ratings, description | free |
| Apple RSS charts / reviews | Top-100 grossing/free per genre; recent reviews (pain points) | free |
| Google Play (scraper + DataForSEO) | Android results, installs, IAP flag | free / per task |
| DataForSEO Google Ads | Google monthly searches, **cost per click**, 12-month history | ~$0.09 per 1,000 keywords |
| DataForSEO Google Trends | 5 years weekly: growth, seasonality, peak month | ~$0.011 per 4 keywords |
| TrustMRR API, MobileMRR | Verified revenue of real named apps: calibration anchors | free |
| DeepSeek | Keyword labels, buildability, topic expansion and clusters, pain points, metadata advice | cents |
| App Store Connect | Our own apps' sales | free |

The universe: Apple's popular list (8,303) + long-tail discovery (~12,600 from autocomplete and DeepSeek
variants of the 1,132 real needs) + everything a research run finds.

## 5. Estimating revenue per app

Calibrated 2026-11-02 on **487 apps with verified revenue** (TrustMRR + MobileMRR, revenue checked via
RevenueCat/Stripe/Superwall; worldwide, all platforms). `apptopics calibrate-anchors` reruns the fit.

1. **Ratings → revenue curve.** US iOS ratings per month since release (or observed rating gain between
   our snapshots) → median monthly revenue, a running median over the anchors:

   | ratings/month | 0.5 | 1 | 2 | 4 | 8 | 16 | 32 | 64 | 300 | 1,000 |
   |---|---|---|---|---|---|---|---|---|---|---|
   | median $/month | 225 | 272 | 459 | 584 | 792 | 1,063 | 1,712 | 3,153 | ~12.7k | ~38k |

   Revenue grows slower than ratings (~0.6-0.8 power). A $5-20k/month indie app has a median of 82 US ratings.
2. **Grossing charts**, per chart: revenue = a_g × rank^-0.86 (slope from Garg & Telang). The level a_g is
   recomputed each run as the median over that chart's apps of the curve estimate × rank^0.86. Genres differ
   ~100× (rank 100: Productivity ≈ $32k/mo, Travel ≈ $1.5k), which replaced the wrong "genre rank × 8" rule.
3. **Noise is modelled, not hidden.** Real revenue scatters around the curve with sd 0.77 in log10 (about
   ×6). Each app gets P(earns ≥ $5k) = Φ((log10 est − log10 5000) / 0.77); breadth is the mean of those.

**Accuracy (honest):** single-app estimates are within 3× of truth about 55% of the time; ratings rank
revenue with Spearman 0.44 at indie scale. Sector-level medians are steadier than single apps.
**Blind spot:** apps grown by paid ads have few ratings (Calyra: $72k/month, 149 US ratings) and are
underestimated by any ratings-based method. **Bias:** anchors are apps that monetize; free/ad-only apps
in a sector are overestimated. Google Play shows 91 installs per rating (median, 759 apps).

## 6. The factors

**Sector (the small-team view)**

| Factor | Definition | Why |
|---|---|---|
| breadth | share of sector apps earning ≥ target | "do 5-20% of them make money?" |
| attainable | log10(median band revenue / target) | what we would likely earn without being #1 |
| concentration | revenue share of the top 5% of apps | winner-take-all niches are useless to us; **the most concentrated 10% are hidden**, the rest penalised |
| entrant_share | share of band apps released in the last 24 months | newcomers do reach the paying band |
| indie_share | share of band apps from small developers | teams like ours sit there |
| newcomer_winners | log(1 + apps < 2 years old estimated ≥ target anywhere in the sector, half credit within ×2) | the market pays newcomers *now*, even when the p5-p20 band is thin |
| monetization | log10 total sector revenue | someone pays here at all (small weight) |

**Demand, trend, competition**

| Factor | Definition |
|---|---|
| demand | Apple popularity 1-100 (low weight: giant demand attracts giants) |
| trending | Apple popularity this week minus its mean over weeks seen in the prior 12. Apple lists only the top 500 per genre, so absent weeks are not zeros; a brand-new term is compared with the genre cut-off |
| momentum | our own 7/28-day popularity deltas plus incumbents' rating velocity |
| search_volume, cpc | Google monthly searches; cost per click = advertisers pay where buyers are |
| trend_slope, trend_level | Google Trends 12-month change; level vs a fixed reference term |
| competition | log10 median rating count of the top 10 |
| difficulty | 0-100 from the top 10: 100 × (0.5 size + 0.3 heavyweight share + 0.2 maturity); Astro's score blended in at 25% when tracked |
| brandness | probability the term is a brand; DeepSeek label decides when present, heuristic otherwise |
| intent | share of Apple's top 10 that actually serve the keyword (low = navigational or vague search) |

**Openings and buildability**

| Factor | Definition |
|---|---|
| review_neg_share | share of 1-2 star recent reviews across band apps |
| review_gap | DeepSeek: how much a newcomer wins by fixing recurring complaints (complaints kept as a feature list) |
| social_views | log10 TikTok + YouTube views of videos about the need (ScrapeCreators; research aspects and sampled topics, `keyword_social` / `topic_social`) |
| ad_activity | share of the market's top apps with verified, active Meta ads × longevity (90+ days = 1): advertisers only keep paying when users are worth more than the ads |
| play_demand, play_iap_share, play_grossing | Google Play installs, monetization, grossing presence |
| buildability | DeepSeek: can we ship it under our constraints (iOS + Android, no heavy backend, no licensed content, no regulated finance or medical) |
| llm_appeal | five simulated consumer personas: willingness to try, and (double weight) to pay |

## 7. From factors to a ranking

Each factor is z-scored across the universe, clipped at ±3, multiplied by its weight
(`config/weights.toml`) and summed. Revenue factors carry the most weight (breadth and attainable 2.0).
Their *upside* (positive part) is multiplied by **reach**, so a rich market nobody new can enter
(`dating`, `photo editor`) does not top the list; poor revenue always counts in full. Vague keywords
lose 8 points. Missing factors contribute nothing; **coverage** shows how much weight is backed by data.

Hidden, with the reason shown (all under `[filters]`): DeepSeek label brand or rubbish; thin sector
(< 30 relevant apps, too few for a p5-p20 band); top-10% concentration; buildability < 0.3; coverage < 50%;
non-Latin scripts; game sectors; gambling, lending and VPN terms; p5-p20 band under $1,000/month.

## 7b. Recommendation index (suggests, not only shows)

The index reads each factor with the same freshness rule as the composite: a factor recomputed on the scoring
day counts only from that day, so a search whose sector shrank keeps no stale band. When the index is rebuilt on a
day without a scoring run (the research verdict step), it uses the latest scoring day's composite.

Two absolute scores per keyword, 0-100, each with a grade (**A ≥ 50, B ≥ 38, C ≥ 28, D ≥ 18, E**), an action
and a reason. Real opportunities are rare: A is roughly the top 2% of visible niches.

**opp_score — should we build a new app for this need?**
`100 × money^0.45 × reach^0.35 × demand^0.20 × concentration × buildability × trend`
- money = band revenue ($1k → 0, $30k → 1) and P(app ≥ $5k) (40% → 1); then 30% young winners (apps < 2 years
  old earning the target anywhere in the sector, 4 → 1), 15% Meta ad activity and 20% Google CPC when known
- reach = difficulty curve (0.5 at 70) + weak spots in the top 10 + newcomer share
- demand = searches/day (1 → 0, 2,000 → 1); 25% Google volume when known; TikTok + YouTube views (10k → 0,
  10M → 1) can then only raise it (few videos does not mean few buyers)
- adjusters: top-heavy markets down to ×0.5; buildability ×0.5-1; trend ±15% (Apple weekly + Google 12-month)
- labels: `broad` (real but very wide words: compass, chat) ×0.7, `vague` ×0.5
Multiplicative on purpose: a fatal weakness (no money, no way in) cannot be bought back by the rest.
Actions: **Build now** (A/B, visible, buildability judged ≥ 0.6), **Watch** (C; or A/B whose buildability DeepSeek
has not judged yet or scored low; or good numbers hidden for a soft reason), **Avoid**.
A keyword whose sector is too thin to measure (< 30 relevant apps) gets grade **?** and action **Unproven**: the
store has no market to judge yet, which is not the same as a bad market (a topic made only of such searches is
Unproven too); the dashboard shows the off-store signals it does have (Google volume, CPC, trend, open spots).
A search whose results page was never fetched is **No data** (also grade ?): it is fetched once its popularity
reaches 20 or a research run finds it. The stored `parts.in` carry every input, so the report can draw how a
score was built.

**aso_score — should an existing app target this keyword?** `100 × demand^0.5 × ease^0.5`,
ease = 0.1 + 0.9 × min(1, weak spots / 5). **Target now** (≥ 3 weak spots, popularity ≥ 20), **Later** (needs
ratings first), **Skip**.

**weak_spots** = top-10 results with < 100 ratings: the spots a 0-rating app can take. It proved more reliable
than Astro's difficulty (RightMeal / SparkNote analysis). Searches/day from popularity: 40 ≈ 70/day,
×1.15 per point (RespectASO).

**Pain points**: for A/B niches, DeepSeek reads recent 1-3★ reviews of the p5-p20 apps and lists recurring,
fixable complaints (factor `review_gap`, shown in the keyword details). In most top niches the anger is about
reliability and subscription tricks, not missing features.

**Topics** (`apptopics llm cluster`, ~$0.10–0.30): every scoreable keyword not hidden for a hard reason (~2,600) is
grouped by DeepSeek into product topics (batches of similar terms, then a merge pass for duplicate labels).
Topic score = 0.75 × best member's opportunity + 0.25 × mean of its top 3 + breadth bonus 2 × log2(1 + members
graded A–C), capped at +6: an idea with many good searches has more ways in; then × 0.9–1.0 for total traffic (5 searches/day →
0.9, 1,000+ → 1.0) so a single well-scored but tiny phrase cannot top the list. Same grade cut-offs; Build now needs
grade A/B *and* a Build-now best member. Popularity, difficulty (easiest member), weak spots and the p5–p20 band
come from the best member; searches/day is the topic total. Rebuilt after every scoring run; re-cluster when the
universe grows (new long tail, research). `apptopics llm merge-topics` runs a second, stricter merge pass over the
existing topics (two orderings, so duplicates with different first words still meet) without re-clustering.
Topic rows also carry the best member's breadth, concentration, newcomer share, buildability and intent so the
table, compare drawer and map read the same for topics and keywords.
**Hidden searches in topics** (`apptopics llm attach-hidden`, ~$0.3–0.8): the keywords hidden for a hard reason
(brands, games, non-latin, thin sectors…, popularity ≥ 5) are also assigned by DeepSeek to an existing topic when
one fits, else to a new topic; near-duplicate new labels are merged. They never count in a topic's score. A topic
made only of hidden searches gets a row flagged hidden (same `composite.excluded` shape as keywords) with the mix
of reasons, so the dashboard lists it only under **Show hidden**; a visible topic's detail lists its hidden
searches under the same tick. Re-run after `llm cluster`, which rebuilds the topics from scratch.

**Google Play complaints** (`apptopics dfs play-reviews`, DataForSEO $0.00075 per app + DeepSeek cents): for
Build-now niches that have Play rankings, the newest reviews of the top Play apps → the same fixable-complaint
extraction (factor `play_review_gap`). Android complaints are often different (ads, permissions, device bugs).

**Your own apps** (`config/own_apps.toml`): title / subtitle / keyword-field terms are split into words and 2-word
phrases (Apple indexes words), looked up in the universe, and shown with their ASO numbers and your rank (top 200).
ASC revenue of the last 90 days is also stored as a `calibration_anchors` row (source `own_asc`) so the revenue fit
sees real points with few ratings.

## 7c. Research on demand

Dashboard search box (or `apptopics research "topic" [--app "name: description"] [--depth quick]`):
DeepSeek expands the topic in many directions (synonyms, audiences, jobs, problems, features, adjacent needs)
→ App Store autocomplete for every seed (real searches) → DeepSeek labels (keep needs) → popularity (Apple Ads
list, else Astro) → iTunes competitor pages for the best 150 (deep) or 30 (quick) → rescore → DeepSeek topic
clusters → report: Build now, Topics, ASO targets, Rising, and suggested title/subtitle/keywords when an app is
described. Every found term joins the universe, so the weekly run keeps tracking it. The run ends with the market
report, the social step and the **verdict** (below).

**Verdict** (`pipeline/verdict.py`, `apptopics research-verdict <run>`). The keyword scores answer a narrow question
(would a search-led app for this phrase pay?), so a market that nobody searches the store for yet grades D/E even
when young apps earn in it. The verdict answers the market question: one grade per aspect from five visible parts,
each 0–1 and weighted: **opening** 30% (Emerging 1.0, Empty 0.7 = unproven, Crowded 0.35, Saturated 0.1) ·
**people care** 25% (TikTok + YouTube views, 10k → 0, 10M → 1) · **newcomers win** 20% (competitors < 2 years old
earning $5k+, 4 → 1) · **someone pays ads** 15% (competitors with verified active Meta ads, 3 → 1) · **white space**
10% (unmet wishes users voice for the aspect, 3 → 1). Grades A ≥ 80, B ≥ 65, C ≥ 50, D ≥ 35, else E; Empty aspects
are flagged unproven. The overall grade is the best proven aspect's; DeepSeek words the call, what not to do, three
reasons that point at report sections, two risks, where users will come from and the next step. The same step
stores the run's views and ad activity per keyword (`keyword_social`), recomputes the social factors and the index,
and refreshes the report's keyword tables, so the two views agree on the numbers and disagree only on the question.

**Topic-level social and ads** (`apptopics social-topics --topics a,b | --grades ABC --limit N`, `pipeline/social_topics.py`):
one TikTok + one YouTube search per topic label and the Meta Ad Library for its top 5 apps by estimated revenue
(≈ 12 credits per topic), summarised in `topic_social` and read by the `social_views` and `ad_activity` factors for
every member keyword. Run on 5 pilot topics on 2026-11-03; the full A–C pass (~600 topics, ≈ $13) is a decision
for later.

## 7d. Market report (Research)

Built by `pipeline/market.py` at the end of a research run.

- **Aspects**: DeepSeek proposes 10–15 aspects of the topic with seed searches; the user picks. Each aspect is
  expanded separately and its autocomplete results inherit the aspect, so every search belongs to one aspect.
  Competitor pages are fetched round-robin across aspects so each gets coverage.
- **Crowdedness** per aspect, from the apps that really serve its searches (relevance filter, top 25 of each):
  Empty (< 3 apps with ≥ 50 ratings: users must be taught the idea) · Emerging (≤ 11 apps with ≥ 1,000 ratings:
  the sweet spot) · Crowded (12+) · Saturated (3+ apps with ≥ 50,000 ratings and < 1 weak spot on average in the top
  10). Index 0–100 = 0.6 × log-scaled established count + 0.4 × log-scaled giant count.
- **Competitors**: ranked by our calibrated revenue estimate, boosted 30% per extra aspect they appear in; the top
  30 with ≥ 20 ratings are profiled.
- **Prices**: the public App Store page's In-App Purchases list (top ~10 items), cached 14 days in `app_iap`.
  Period from the item name (week / month / year / lifetime). Each app counts once per period at its median price.
  Suggested price = competitors' median monthly and yearly (rounded to .99); a weekly plan when ≥ 30% sell one.
- **Feature map**: DeepSeek derives one shared feature list from the competitors' descriptions, then marks each
  app. Coverage = share of profiled apps; ≤ 20% coverage = white space.
- **What happy users say**: 4–5★ reviews (≥ 60 characters, longest 12 per app, top 15 apps). Happy users describe
  the pain they had before and what fixed it, which 1–3★ complaints rarely do.
- **Your highlights**: DeepSeek proposes three positioning options from those facts (written once, after the
  social step, so Voice of users and the advertisers are in), preferring low-coverage features backed by pains or
  wishes. A starting point, not a verdict.
- **The page** (2026-11-03, direction "Brief" from the design canvas): the verdict card first, then the aspect
  scorecard with each verdict's parts as a stacked bar, then every section collapsed behind a one-line conclusion
  computed from its data (click to expand). The keyword table draws how the selected search's score was built
  (falling bars: 100 × money^0.45 × reach^0.35 × demand^0.20 × adjusters, with the inputs behind each part).

## 8. Testing and calibration

- **Own-sales check (2026-11-03).** SparkNote: 0 US ratings, not on any of the 10,286 tracked result pages; the
  model therefore predicts no US organic revenue. ASC: ~$50/month and ~390 downloads/month, mostly from a brand
  misspelling and non-US stores. Consistent, but a weak test; it mainly confirms "invisible = no revenue".
- **Validation (every change).** `apptopics validate` checks the ranking against `config/golden.toml`
  (30 real niches, 20 brands or vague words, 8 hard markets) and the grossing charts. Current: all pass
  (80% of real niches visible; metronome, teleprompter, white noise are hidden as sub-$1k bands; 0 junk
  in the top 100; rating-vs-grossing Spearman 0.52).
- **Revenue anchors.** `apptopics calibrate-anchors [--refresh]`: TrustMRR + MobileMRR → fit + error report.

- **Backtest (weeks).** Every scoring run snapshots each keyword's score, popularity, band revenue and weak spots
  (`predictions`). After 28 days `apptopics backtest` compares them with reality: newcomer apps entering the
  top 30, popularity change, band revenue change, per grade and as Spearman vs score. First verdict 2026-11-30.
  If A/B do not beat C-E on newcomer entry, the weights are wrong.
- **Probe apps (months).** Ship a small app in a top topic; its real revenue becomes an anchor
  and tests the one thing no dataset can: whether we can win a topic the model likes.

## 9. Reading the dashboard

- **Map** (Opportunities): x = ease of entry, y = what small apps earn, for the top 60 Build now / Watch ideas
  ("Show all" for the rest); the older Difficulty × Popularity view is one click away. Ring = grade A (thick) or
  B (thin), dashed = hidden, ▲▼ = popularity moved ≥ 3 points.
- **Table**: New app view (opportunity) or ASO view, Topics or Keywords; **Show hidden** adds greyed rows with the
  reason. Every column header has an ⓘ.
- **Details**: verdict and reason first, then the key numbers and the score parts, then tabs for searches,
  competitors, complaints and raw factors.
- **Research report**: verdict card, aspect scorecard, then sections collapsed behind one-line conclusions (7d).

## 10. Spend control

Each paid API has a monthly cap and there is a total (`config/budget.toml`, editable in the
dashboard). Before a paid job runs we estimate its cost, add month-to-date actual spend from
the APIs' own usage fields, and skip the job with a warning (dashboard banner, macOS
notification) if it would exceed a cap. The full run is **manual** (dashboard button or `apptopics run-weekly`),
best on Wednesday or later: Apple's Sun-Sat popularity week is published by then. The button warns before
running early or within 7 days of the last run. A full DataForSEO pass costs about $5 (volume $1, Trends
$2.75 for the top 1,000, Play rankings ~$1).

## 11. Limits, stated plainly

- Single-app revenue is within 3× about half the time; market-level medians are steadier. Apps grown by paid
  ads (few ratings, big revenue) are underestimated; free/ad-only apps are overestimated.
- Long-tail popularity comes from Astro (slow, ~500 terms per 6 minutes); Apple Ads covers only top terms.
- Keyword sectors overlap; topics group the near-duplicate phrasings into one idea (LLM grouping: occasionally too broad or too narrow).
- Backtest evidence starts after 4 weeks of runs; until then weights are reasoned, not proven.
- US store only.

## 12. Routine and concurrency

`apptopics serve` for the dashboard; **Run weekly update** (or `apptopics run-weekly`) once a week, Wednesday or
later; `apptopics validate` after any model change; `apptopics backtest` once four weeks of snapshots exist.
Code layout is in the README.

DuckDB allows one writer. Every long job calls APIs without the lock and writes in short transactions
(`write_retry`); pipeline reads wait for the live database (`connect_read_live`); only the dashboard falls back to
the read copy, which is refreshed after every scoring run.
