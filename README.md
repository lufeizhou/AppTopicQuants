# AppTopicsQuants

A stock-quant-style model for **choosing what app to build** (and which keywords an existing app should target).
It ranks fine-grained App Store searches (`food diary`, `mood journal`, not categories) for a small team whose
target is **≥ $5,000/month per app within 12 months**: is the need popular, rising, paid for at the
5th–20th percentile, and can a newcomer get in? Every search, topic and research aspect gets a grade from evidence
checks, with its reason:

| Grade | Action | Meaning |
|---|---|---|
| **A** | Build now | can earn: p5–p20 apps earn ≥ $5k/month, ≥ 2 apps under 2 years old earn $5k+, popularity ≥ 30 (~17 searches/day), judged buildable |
| **B** | Maybe | money exists but a check fails (the reason says which) |
| **?** | Not sure | the market cannot be measured yet (too few apps serve it) |
| **C** | No | no money, hard to build, or a brand / junk search |

The 0–100 opportunity score only orders ideas inside a grade.

Read `docs/METHODOLOGY.md` for the model, `docs/CHANGELOG.md` for decisions and before/after numbers per review
round, and `docs/OPTIMISING.md` for how a model step is reviewed and changed.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # fill the keys you have; missing sources are skipped
.venv/bin/apptopics doctor      # what is reachable / configured
.venv/bin/pytest -q
.venv/bin/apptopics serve       # dashboard: http://127.0.0.1:8765
```

## Using it

**Dashboard** (`apptopics serve`), three tabs:
- **Opportunities** (light, clean layout): a headline with the number of Build now ideas, then a few controls
  (New app / ASO keywords, Topics / Keywords, search, Show hidden, Filters for min popularity, rows and all columns).
  The map shows where money is easy to reach (ease of entry × what small apps earn, top 60 Build now / Maybe ideas,
  "Show all" for the rest; the old Difficulty × Popularity view is one click away). The table has six columns:
  idea, verdict, searches/day, what small apps earn, open spots in the top 10, score parts. **Build now** = grade A
  (all four checks pass; every A candidate gets a DeepSeek buildability judgement). The detail panel leads
  with the verdict and its reason, four key numbers and why the score (a thin-market card for **?** searches), then tabs (Searches / Hidden for ideas; Competitors /
  Complaints / Numbers / Raw for searches). Tick rows or use "Add to compare" to **compare** up to 5.
  **Your apps** chips (from `config/own_apps.toml`) list your title / subtitle / keyword terms with their ASO numbers.
  Deep links: `?kw=<keyword>`, `?cmp=<id>|<id>`, `?own=<App>`, `?hidden=1`, `?dp=1` (market map), `?log=1`.
- **Research** (a market report): type a topic, optionally your app.
  Step 1 (2–4 min, ~$0.02) gathers evidence (autocomplete, tracked searches, top 40 competitors, their 4–5★ reviews,
  a few Reddit titles) and DeepSeek groups it into 10–15 aspects with the evidence behind each; tick the ones worth
  a look or add your own. Step 2 (5–40 min) finds real searches per aspect, scores them and builds the report:
  crowdedness per aspect, prices and a suggested price, feature map with white space, what happy users say,
  competitors, Reddit / TikTok / YouTube voice of users, who buys Meta ads, three positioning options, and the
  **verdict**: one grade per aspect (same checks as a search; five visible parts order the aspects) and an overall
  call (the best aspect's grade) with reasons, risks and a next step.
  The page shows the verdict first, then the aspect scorecard, then every section collapsed behind its one-line
  conclusion; the keyword table draws how each score was built. Thin markets are graded **?** (Not sure), not C;
  searches whose results page was never fetched are **No data**.
- **Budget & data**: monthly caps, next-run cost estimate, spend, recent jobs.
- **Run weekly update** (top right): manual only. Warns if the last run is < 7 days old or Apple's new week is
  not published yet (before Wednesday), and shows the paid cost first.
- **Run light update** (next to it): free, ~15 min, tap it when you are free, ideally once a day. Refreshes your own
  apps' result pages, the charts and the rating counts of band apps, so rating velocity, the backtest and your rank
  history get a series. Nothing runs on its own.

**Command line** (same functions):

```sh
apptopics run-weekly                 # full refresh (what the button runs), then search expansion ([step1.expand] weekly_sources + weekly_pilots)
apptopics research "pet health" [--app "Name: one-line description"] [--depth quick] [--no-reddit]
apptopics research-reddit <run_id> [--credits 40] [--no-video] [--no-ads]   # add Reddit/TikTok/YouTube/Meta ads to a report
apptopics research-verdict <run_id> [--no-rescore]   # aspect verdicts + overall call (also feeds the social evidence into the model)
apptopics social-topics --topics a,b | --grades AB --limit 50   # TikTok/YouTube views + Meta ads for topics (~12 credits each)
apptopics top -n 40 [--entity topic] [--all]
apptopics validate                   # ranking sanity checks (config/golden.toml + grossing charts)
apptopics backtest                   # did grades predict outcomes? early read after 28 days, young-app winners after 90
apptopics calibrate-anchors [--holdout] [--apply]   # verified-revenue anchors: error report, honest hold-out test, refit the curve
apptopics collect light              # light update (own-app pages, charts, rating counts); same as the dashboard button
apptopics collect longtail | longtail-finish   # discover long-tail searches, then popularity + competitors + rescore
apptopics expand collect [--sources titles,hints,play] [--pilots reddit,dfs_play_app] [--alpha-grades A]   # find new searches
apptopics expand finish --since YYYY-MM-DD   # label, popularity, result pages, buildability, rescore, attach to topics
apptopics expand brand-check [--grades AB]    # relabel graded needs that name one specific app, game or company
apptopics llm labels | buildability | pain-top | cluster | merge-topics | attach-hidden   # pain-top is manual only (not in the weekly run)
apptopics dfs volume | trends | play | play-reviews   # DataForSEO: Google volume+CPC, Trends, Play rankings, Play reviews → pain points
apptopics score | plan | budget show|set
```

## Credentials and cost

| Env vars | Unlocks | Cost |
|---|---|---|
| (none) | Apple charts, iTunes lookup/search, autocomplete, reviews, Google Play scraper | $0 |
| `APPLE_ADS_*` | Apple's weekly search popularity (top 500 per genre): universe + trending | $0 |
| Astro app running | popularity + difficulty for any term (long tail, research) | owned |
| `DEEPSEEK_API_KEY` | keyword labels, buildability, topic expansion/clusters, pain points, metadata advice | cents per job |
| `DATAFORSEO_LOGIN/PASSWORD` | Google volume + CPC, Google Trends 5y + seasonality, Google Play rankings and reviews | ~$5 per full pass |
| `TRUSTMRR_API_KEY` | verified revenue anchors for calibration (MobileMRR needs no key) | $0 |
| `ASC_*` | your own apps' sales | $0 |

Caps per paid API and a total live in `config/budget.toml` (editable in the dashboard).

## Layout

```
apptopics/
  sources/   itunes (search, lookup, autocomplete, reviews, charts), appleads, astro, googleplay, dataforseo,
             deepseek, revenue_dirs (TrustMRR, MobileMRR), scrapecreators, asc; base.py = shared rate limiter + machine locks
  metrics/   demand, trending, sector (revenue curve, band, breadth, concentration, entrants, indie, intent),
             relevance, entry (weak spots), difficulty, brandness, competition, external (Google/Play/reviews), ...
  pipeline/  score (factors → composite), index (opp/aso grades), discover (lock-friendly building blocks),
             expand (Step 1 search sources: autocomplete, competitor titles, Reddit, DataForSEO Play),
             research, market, ground, reddit_voice, social_voice, social_topics, verdict, collect (jobs, long tail),
             dfs (DataForSEO), anchor_fit, validate, backtest, weekly
  web/       FastAPI API + static/index.html (+ research.js: the report page)
config/      model.toml (target, weights, filters, calibrated revenue curve), budget.toml, golden.toml (test list),
             own_apps.toml (your apps: metadata + ASC ids for the overlay and revenue anchors)
data/        apptopics.duckdb (+ read copy for the dashboard), cache/ (incl. expand_done.json), research/
docs/        METHODOLOGY.md, CHANGELOG.md, OPTIMISING.md
scripts/     balances.py / .sh (API credit balances)
```

## Development

`ruff check apptopics tests` (rules in `pyproject.toml`), `pytest -q`, `apptopics validate` after model changes.
`data/`, `logs/` and `.env` are not committed; `.env.example` lists the keys.

## Extending

A metric is one class (`name`, `requires`, `higher_is_better`, `compute(con, store, date)` → `FactorRow`s)
registered with `@metrics.register()`, plus a weight in `model.toml`. A source is one client in `sources/`
plus a job; long jobs should use `pipeline/discover.write_retry` (short writes) so they never block each other.
