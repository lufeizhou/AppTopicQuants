# AppTopicsQuants: Methodology

*For a human reader. Rewritten 2026-11-04. Running example: the search `timer widget`, numbers from the
2026-11 scoring run.*

## 1. The question

We are a small company. We want apps that each earn **at least $5,000 a month within a year** (roughly the
top 5-10% of subscription apps at one year). We will rarely be number one in a niche and do not need to be.
So the model asks:

> Where do the apps that are **not** the leaders (the 5th-20th percentile of the niche) still earn $5k a
> month, and can a newcomer get into that group?

## 2. The flow at a glance

```
 1. Universe        ~20k App Store searches (Apple's popular list + long tail)
        │
 2. Sector          for each search: the apps on its results page that really serve it
        │
 3. Revenue         every app gets an estimated $/month (from its US ratings)
        │
 4. Market numbers  what the p5-p20 apps earn, how top-heavy, do newcomers win, how hard to enter
        │
 5. Filters         drop brands, games, thin or winner-take-all markets
        │
 6. Score + grade   opp_score 0-100 → grade A-E → action (Build now / Watch / Avoid)
        │
 7. Topics          group searches into app ideas; the dashboard ranks ideas
        │
 8. Research        on demand: deep report on one idea, ending in a verdict
        │
 9. Check           validation every change, revenue hold-out test, backtest (28 / 90 days), own-app ranks
```

Each step below says what goes in, what comes out, and what happens to `timer widget`.

## 3. Step by step

### Step 1. Universe: which searches we look at

**In:** Apple Ads API popularity list (top 500 terms per genre, weekly, 8,303 terms) + App Store
autocomplete and DeepSeek variants of real needs (~12,600 long-tail terms) + anything a research run finds.
**Out:** one row per search string, with popularity 1-100 (Apple, or Astro for the long tail).

A search is our "ticker". It is always a concrete string (`timer widget`), never a category.

> `timer widget`: popularity 35. Popularity converts to searches per day (40 ≈ 70/day, ×1.15 per
> point), so 35 ≈ **35 searches/day**.

DeepSeek labels every search as **need**, **broad**, **vague**, **brand** or **rubbish**. Brand and rubbish
are dropped later.

### Step 2. Sector: who competes for it

**In:** the iTunes results page (top 200) for the search.
**Out:** the sector = the apps that actually serve the search (search words appear in name, subtitle or
description). Apple pads vague searches with unrelated big apps; those are removed.

A sector needs **≥ 30 relevant apps**, otherwise the p5-p20 band is just the 2-3 leaders. Fewer than 30 →
grade **?** "Unproven" (no market to judge yet, which is different from a bad market).

### Step 3. Revenue: what each app earns

**In:** each app's US rating count and release date (or the rating gain between our snapshots), plus the
top-grossing charts.
**Out:** estimated $/month per app.

1. **Ratings per month → revenue.** Fitted on **487 apps with verified revenue** (TrustMRR + MobileMRR):

   | ratings/month | 1 | 4 | 16 | 64 | 300 | 1,000 |
   |---|---|---|---|---|---|---|
   | median $/month | 272 | 584 | 1,063 | 3,153 | ~12.7k | ~38k |

   Revenue grows slower than ratings (~0.6-0.8 power).
2. **Grossing charts** correct the level per genre: revenue = a × rank^-0.86, with `a` refit each run.
   Genres differ ~100× (rank 100 in Productivity ≈ $32k/mo, in Travel ≈ $1.5k).
3. **Noise is kept.** Real revenue scatters ×6 around the curve (sd 0.77 in log10). So each app gets a
   probability, not a yes/no: P(earns ≥ $5k).

**How good is it?** Hold-out test (2026-11-04, `apptopics calibrate-anchors --holdout`): refit on two thirds
of the 487 anchors, score the other third, 20 random splits.

| Out of sample | Result |
|---|---|
| single-app estimate within 3× of the truth | 56% |
| ranking power for "earns ≥ $5k" (AUC) | 0.73 |
| apps flagged ≥ $5k that really are | ~55% |
| real ≥ $5k apps the point estimate catches | 13–16% |
| P(≥ $5k) calibration | predicted 37% → actual 35%; 61% → 64% |

Read: the **probabilities are honest, the point estimates miss most winners** (ad-grown apps earn with few
ratings). That is why the market numbers below use P(≥ $5k), not "estimate ≥ $5k". A logistic model on ratings,
age and price was tried as an alternative; it ranks no better (AUC 0.74), so the curve stays. `--apply` writes a
refitted curve into `config/weights.toml`.

Known biases: apps grown by paid ads are underestimated; free/ad-only apps are overestimated, because all
anchors are apps that charge.

### Step 4. Market numbers: is there room for us

**In:** the sector, sorted by estimated revenue.
**Out:** a handful of numbers per search.

| Number | Meaning | `timer widget` |
|---|---|---|
| **band** | median revenue of the p5-p20 apps | **$3,576/mo** |
| **P(app ≥ $5k)** | mean probability across the sector | **19%** |
| **young winners** | apps < 2 years old earning ≥ $5k anywhere in the sector (half credit within ×2) | 0.5 |
| **concentration** | revenue share of the top 5% of apps | 62% |
| **difficulty** | 0-100 from the top 10: size, heavyweights, age | 55 |
| **weak spots** | top-10 apps with < 100 ratings (a new app can take these) | 3 |
| **entrant share** | band apps released in the last 24 months | 6% |
| **Google** | monthly searches, cost per click (DataForSEO) | 4,401, $1.25 |
| **trend** | Apple weekly change, Google 12-month change | none |
| **buildability** | DeepSeek: can a small team build it (no heavy backend, licences, regulated finance/medical) | 1.0 |

### Step 5. Filters: what we never show

Hidden, with the reason kept (all in `config/weights.toml [filters]`):

- **Hard:** label brand or rubbish · games · non-Latin script · gambling, lending, VPN · buildability < 0.3 ·
  thin sector (< 30 apps) · data coverage < 50%.
- **Soft:** the 10% most concentrated sectors · band under $1,000/month.

"Show hidden" in the dashboard brings them back greyed out.

> `timer widget` passes every filter.

### Step 6. Score, grade, action

Three parts, each 0-1, multiplied:

```
opp_score = 100 × money^0.45 × reach^0.35 × demand^0.20 × concentration × buildability × trend
```

Multiplying is deliberate: a fatal weakness (no money, no way in, no searches) cannot be bought back by
the other parts. The exponents say money matters most, then reach, then demand.

**Worked example, `timer widget`:**

| Part | How it is built | Value |
|---|---|---|
| **money** | ½ band on a log scale ($1k → 0, $30k → 1): $3,576 → 0.37 · ½ P(≥$5k)/40%: 19% → 0.47 · → 0.42<br>then 30% young winners (4 → 1): 0.5 → 0.13 · then 20% CPC ($0.20 → 0, $3 → 1): $1.25 → 0.38 | **0.34** |
| **reach** | 50% difficulty curve (0.92 at 50, 0.5 at 70, 0.08 at 90): 55 → 0.87<br>30% weak spots (4 → 1): 3 → 0.75 · 20% entrant share: 6% | **0.67** |
| **demand** | 75% searches/day on a log scale (1 → 0, 2,000 → 1): 35/day → 0.47<br>25% Google volume (100 → 0, 100k → 1): 4,401 → 0.55. TikTok/YouTube views can only raise it | **0.49** |
| concentration | 1 at ≤ 50% top-5% share, down to 0.5 at 95%: 62% → | ×0.86 |
| buildability | 0.5 + 0.5 × buildability | ×1.00 |
| trend | ±15% from Apple weekly and Google 12-month change | ×1.00 |

100 × 0.34^0.45 × 0.67^0.35 × 0.49^0.20 × 0.86 = **40 → grade B**.

**Grades:** A ≥ 50 (about the top 2% of visible niches), B ≥ 38, C ≥ 28, D ≥ 18, else E. Labels `broad`
(compass, chat) ×0.7, `vague` ×0.5.

**Actions:**
- **Build now**: grade A/B, visible, buildability judged ≥ 0.6.
- **Watch**: grade C; or A/B with buildability not judged or low; or good numbers hidden for a soft reason.
- **Avoid**: the rest. **Unproven** (?): sector too thin. **No data** (?): results page never fetched
  (fetched once popularity reaches 20 or research finds it).

> `timer widget` → **B, Build now**. Reason shown: "easy to enter (55) · fits our constraints | but ~1
> young app already earns $5k+, p5-p20 apps earn ~$3,576/mo". Read: you can get in, but the band sits
> below target, so a B and not an A.

A second, smaller score answers a different question: **aso_score**, should an *existing* app target this
search? `100 × demand^0.5 × ease^0.5`, ease from weak spots. Target now / Later / Skip.

### Step 7. Topics: from searches to app ideas

Many searches are the same idea (`hiit timer`, `tabata timer`, `interval timer`). DeepSeek groups the
~2,600 scoreable searches into topics; a second pass merges duplicates.

```
topic score = 0.75 × best member + 0.25 × mean of top 3
              + bonus 2 × log2(1 + members graded A-C), max +6
              × 0.9-1.0 for total traffic (5/day → 0.9, 1,000+/day → 1.0)
```

> Topic **Timer** = `timer widget` (B, 40.2) + `timer plus` (B, 38.3) + `timer` (D, 21.0), plus
> `timer no ads simple` attached as hidden (thin sector, not scored).
> 0.75 × 40.2 + 0.25 × mean(40.2, 38.3, 21.0) + 2 × log2(1 + 2) = 30.2 + 8.3 + 3.2 = **41.6 → B, Build
> now**; traffic ~1,101 searches/day, so ×1.0.

Build now for a topic needs grade A/B *and* a Build-now best member. The topic shows its best member's
numbers. Hidden searches (brands, games…) are attached to topics for context but never count in the score.

Current run: **192 topics Build now** (60 A, 132 B), 364 Watch.

### Step 8. Research on demand

The search box (or `apptopics research "topic"`) runs a deep pass on one idea:

```
DeepSeek: 10-15 aspects + seed searches  →  autocomplete (real searches)  →  labels  →  popularity
  →  competitor pages (best 150, or 30 quick)  →  rescore  →  topics  →  market report  →  verdict
```

The **market report** (`pipeline/market.py`) has, per aspect: crowdedness (Empty / Emerging / Crowded /
Saturated, from how many apps have ≥ 1,000 and ≥ 50,000 ratings), top competitors by estimated revenue,
their prices (App Store IAP list → suggested monthly/yearly price), a feature map (≤ 20% coverage = white
space), what happy 4-5★ users say, and three positioning options.

The **verdict** answers the market question, which the keyword score cannot: a market nobody searches the
store for yet grades D/E on keywords even when young apps earn in it. One grade per aspect from five parts:

| Part | Weight | Full marks at |
|---|---|---|
| opening | 30% | Emerging 1.0, Empty 0.7, Crowded 0.35, Saturated 0.1 |
| people care | 25% | 10M TikTok + YouTube views (10k → 0) |
| newcomers win | 20% | 4 young competitors at $5k+ |
| someone pays for ads | 15% | 3 competitors with active Meta ads |
| white space | 10% | 3 unmet wishes users voice |

A ≥ 80, B ≥ 65, C ≥ 50, D ≥ 35. The overall grade is the best proven aspect's; DeepSeek writes the call,
risks and next step. Every search found joins the universe, so the weekly run keeps tracking it.

### Step 9. Checking the model

| Check | When | What it tests | Status |
|---|---|---|---|
| `apptopics validate` | every change | 30 known real niches visible, 20 brands/vague words hidden, 8 hard markets low, ratings vs grossing ranks | all pass; Spearman 0.52 |
| `apptopics calibrate-anchors --holdout` | when anchors grow | revenue curve vs 487 verified apps, out of sample | within 3× 56%, AUC 0.73, recall at $5k 13–16% |
| `apptopics backtest` | 28 days after a run (early read), ~90 days for winners | do A/B searches produce more young apps reaching the $5k pace than C-E? fitted exponents vs 0.45/0.35/0.20 | early read **2026-11-30** |
| Own-app rank history | daily (light run) | does a keyword with 3+ open spots really give your app a page-one rank? | collecting since 2026-11-04 |
| Probe apps | months | can *we* win a topic the model likes? | not started |
| Own sales | ad hoc | SparkNote: invisible in US search → model predicts ~$0, ASC shows ~$50/mo | consistent, weak test |

**How the backtest works.** Every scoring run stores, per graded search, the score and its parts, the young
apps (≤ 12 months old) serving it with their rating count that day, and a model version (hash of the weights
and formula, so different models are never mixed). Later it asks:

- **winner**: did a young app gain ratings at a pace the revenue curve maps to ≥ $5k/month (P ≥ 0.5), or enter
  a grossing chart? This is the outcome the score claims to predict. Needs ~90 days.
- **newcomer entry**: apps released after the snapshot now in the top 30. Readable after 28 days.
- **popularity change**.

Results are reported per grade, plus a logistic fit of `winner` on log money, reach and demand: the fitted
coefficients are the exponents the data supports, to compare with 0.45 / 0.35 / 0.20. If A/B do not beat
C-E on winners, the weights are wrong.

**Your own apps** (`config/own_apps.toml`): title and subtitle words and 2-word phrases, the keyword field and
any `watch` terms are looked up in the universe. The daily light run fetches their results pages, so the
dashboard shows a rank history per term (sparkline, first → now) next to the model's open spots. This tests
the reach claim directly: does a search with 3+ open spots give your app a page-one rank?

## 4. Limits, plainly

- Revenue per app is a guess within 3× about half the time. Trust sector numbers more than single apps.
- Ad-grown apps are underestimated, free/ad-only apps overestimated.
- Long-tail popularity comes from Astro and is slow (~500 terms per 6 minutes).
- Topic grouping by an LLM is sometimes too broad or too narrow.
- Until the backtest matures (early read 2026-11-30, young-app winners ~2027-02) the weights are reasoned, not proven.
- US App Store only.

## 5. Decided, not yet in the code (2026-11-04)

- Searches hidden for a hard reason show grade **–** instead of A-E (today 161 hidden searches still show A/B).
- Retire the composite (Appendix B); the coverage filter will use opp_score's own inputs.
- Drop the "band under $1,000/month" filter; the money part already scores low bands low (frees `boxing
  timer`, metronome, teleprompter, white noise).
- Keep the money scale as is ($1k → 0, $30k → 1, so $5k ≈ 0.47).

---

## Appendix A. Data sources

| Source | Gives | Cost |
|---|---|---|
| Apple Ads Platform API | popularity, weekly history (universe, trending) | free |
| App Store autocomplete | real long-tail searches | free |
| Astro (local ASO app) | popularity + difficulty for any term | owned |
| iTunes Search / Lookup, RSS | results pages, app metadata, grossing charts, reviews | free |
| Google Play (scraper + DataForSEO) | Android results, installs, reviews | free / per task |
| DataForSEO Google Ads + Trends | Google volume, CPC, 5-year trend | ~$5 per full pass |
| ScrapeCreators | TikTok/YouTube views, Meta Ad Library | ~12 credits per topic |
| TrustMRR, MobileMRR | verified revenue (calibration anchors) | free |
| DeepSeek | labels, buildability, clusters, complaints, report text | cents |
| App Store Connect | our own sales (also stored as anchors) | free |

## Appendix B. The composite (older score)

Before opp_score, the ranking was a **composite**: ~25 factors z-scored across the universe, clipped at ±3,
weighted (`config/weights.toml [composite]`, breadth and attainable 2.0, brandness -1.5, difficulty -1.0…)
and summed, with revenue upside multiplied by reach. It is still computed and drives the *coverage* filter
and the raw-factor tab, but the dashboard ranks by opp_score.

## Appendix C. Running it

- `apptopics serve`: dashboard. **Run weekly update** (or `apptopics run-weekly`) once a week, Wednesday or
  later (Apple's popularity week is published by then), by hand.
- The free **daily light run** (`apptopics collect light`, 03:30 via launchd, ~5 min) refreshes your own apps'
  result pages, the grossing charts and the rating counts of the band apps of the top 1,500 searches, so rating
  velocity, the backtest and your rank history get a daily series. A missed time runs at the next wake.
  `scripts/install_launchd.sh` installs only this job; `install_launchd.sh light weekly` also schedules the paid
  weekly run (normally started by hand).
- Every paid API has a monthly cap in `config/budget.toml`; a job that would exceed it is skipped with a
  warning.
- After `llm cluster` (rebuilds topics), re-run `llm attach-hidden`; `llm merge-topics` merges duplicates
  without re-clustering.
- DuckDB allows one writer: long jobs call APIs without the lock and write in short transactions; the
  dashboard reads a copy refreshed after every scoring run.
