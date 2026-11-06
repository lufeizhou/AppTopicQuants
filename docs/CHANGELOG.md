# Changelog

One entry per model review step: the decisions (who decided, why), what changed in code/config, and the measured
before → after. Every number lives in `config/model.toml`; a change there starts a new backtest model version.
The method for running a review step is in [OPTIMISING.md](OPTIMISING.md).

---

## 2026-11-04 · Step 1 Universe (review round 1)

### Decisions (user, 2026-11-04)

| # | Area | Decision | Why |
|---|---|---|---|
| 1 | Coverage | Autocomplete on graded needs (A–D) + mine competitor titles/subtitles | `hiit timer`, `tabata timer`, `pomodoro timer`, `food log` were missing: App Store autocomplete only extends a seed's prefix |
| 2 | New sources | Google Play autocomplete (free) weekly. One $1–2 pilot each: DataForSEO Play keywords-for-app, Reddit "is there an app" posts. Skip Google web autocomplete | Store-style phrases; web phrasing rarely matches App Store searches |
| 2b | Pilot rule | Keep a paid source only if > 10% of its new terms grade C or better | Same rule as the earlier DataForSEO Labs pilot |
| 3 | Scoring new terms | App Store popularity only; source shown as a tag | Model stays iOS-first |
| 4 | Pruning | Skip results pages for brand/rubbish; keep all other terms (incl. 7,120 at the popularity floor) | Fetch time; a floor term may grow |
| 5 | Labels | Type (need / brand / rubbish) + specificity 1–5 for needs, multiplier 0.5 + 0.125 × (s − 1); DeepSeek sees the page's top 3 apps; relabel all terms with a page | A linear scale instead of broad ×0.7 / vague ×0.5; brands stay separate because a brand is not a weak need |
| 6 | Weights | One file, `config/model.toml`, a section per step, a reason per number; code reads only from it | Weights will be recalibrated often |
| 7 | Searches/day | Check `70 × 1.15^(pop − 40)` against Google volume before changing anything | Uncalibrated assumption |
| 8 | Fetch threshold | Keep, as a config knob | — |

### Changes

- `config/weights.toml` → **`config/model.toml`**, sections `[stepN.name]` (flattened by name in code, so
  `settings.weights()["filters"]` still works). Moved in every number that was hard-coded: opp_score exponents and
  blends (`[step6.score]`), grade cut-offs (`[step6.grades]`), Build-now buildability (`[step6.actions]`), aso_score
  (`[step6.aso]`), topic formula (`[step7.topics]`), research verdict weights (`[step8.verdict]`), popularity →
  searches/day and fetch threshold (`[step1.universe]`), label multipliers (`[step1.labels]`), expansion
  (`[step1.expand]`). Neutrality test: rescoring a copy with the refactored code reproduced all 21,279 keyword scores
  exactly, except the 105 old "broad" terms (×0.7 → ×0.625 by decision 5).
- **Labels:** `keyword_labels` gets `specificity` (1–5) and `context` (`top_apps` / `string`). Old `vague` → need,
  specificity 1; old `broad` → need, specificity 2. New prompt with a rubric and examples. `apptopics llm labels
  --scope new|context|pages|all`. Composite penalty is linear: 8 × (5 − s) / 4.
- **Expansion:** `apptopics expand collect` (App Store hints of graded seeds, `seed a..z` for grade A, Google Play
  autocomplete, competitor title phrases used by ≥ 3 apps, pilots `dfs_play_app`, `reddit`) and `apptopics expand
  finish` (label → Astro popularity → pages → relabel with context → rescore → attach to existing topics). Every hit
  is kept in `expand_finds` with its source, so each source's yield can be judged.
- Topics: `attach_hidden(only_hidden=False)` attaches new visible searches to existing topics without re-clustering.
- Long-tail autocomplete seeds now skip needs with specificity < 3.

### Findings while building

- Brand pages: 5,922 brand searches had results pages, all fetched on 2026-11-01 *before* labels existed. Since then
  `select_keywords` already skips brand/rubbish, so decision 4 needed no new code.
- Fetch threshold: the code fetches long-tail pages at popularity **≥ 15** (`collect longtail-finish` default), not 20
  as METHODOLOGY said. Kept at 15 in `[step1.universe].fetch_min_popularity` pending a decision.
- The bulk labeller (`llm labels`) already passed the top 3 apps; only long-tail terms (labelled before their page
  existed) were labelled blind. `--scope context` now relabels those once their page arrives.
- Searches/day check (7,085 needs with Google volume): Google volume rises ×1.105 per popularity point overall,
  ×1.13 on Apple-list needs (rule: ×1.15), ×1.07 in the Astro long tail; Spearman 0.52. Apple and Astro popularity
  agree where both exist (309 terms, r = 0.96, Astro ≈ Apple + 3). Google is another platform, so it checks the
  shape, not the level. Verdict: the rule's slope is plausible; no change.

### Run (2026-11-04 21:00 → 2026-11-05 04:00)

`expand collect` (all sources) → `expand finish` ×3. Wall time ~7 h, almost all of it iTunes pages (18/min) and Astro
popularity (~500 terms / 6 min). `hint_alphabet_grades` cut from AB to A after measuring: AB = 359 seeds × 26 letters
≈ 3.3 h of hints alone.

| Cost | USD |
|---|---|
| DataForSEO Play pilot: 8,054 terms from 50 apps, **lost** to a crash before saving (fixed: each source now saves on completion) | 1.77 |
| Reddit pilot (ScrapeCreators) | 0.42 |
| DeepSeek: labels 0.09, buildability 0.08, attaching new searches to topics 0.93 | 1.10 |
| **Total** | **3.29** |

### Run 2 (2026-11-05 09:00 → 20:30): the half-done parts in full (decision 13)

DataForSEO's other 25 apps → App Store `seed a..z` for grade A+B seeds (12,355 queries, 10:50 → 17:02, 37 Apple
429s waited out, ~33/min) → `expand finish --since 2026-11-04` (Astro 5,151 terms 17:08 → 18:38; 1,328 results
pages at ~15/min 18:38 → 20:10; labels, buildability, rescore, attach 20:10 → 20:28).

| Cost (both runs, from the `spend` table) | USD |
|---|---|
| DataForSEO Play pilot: 50 apps kept ($0.87 + $0.83) + 50 lost on 11-04 ($1.77) | 3.47 |
| Reddit pilot | 0.42 |
| DeepSeek: labels 0.37, buildability 0.12, attaching searches to topics 1.82 | 2.31 |
| **Total** | **6.20** (of which 1.77 lost) |

### Before → after

Baseline = read copy taken before any Step 1 change. "Labels only" = baseline rescored with the new labels and
config, no new terms.

| | Before | Labels only | After run 1 | After run 2 (linear) | **Final (anchored, #17)** |
|---|---|---|---|---|---|
| Keywords scored | 21,279 | 21,280 | 35,348 | 47,556 (+26,269 found) | **47,556** |
| Keyword grades A / B / C | 123 / 403 / 983 | 48 / 230 / 722 | 88 / 446 / 1,054 | 135 / 679 / 1,415 | **315 / 896 / 1,629** |
| Keyword Build now | 315 | 171 | 345 | 564 (169 old kept + new) | **842** (253 of the 315 old kept; 274 old + 568 new) |
| Topics | 7,512 | 7,512 | 9,198 | 10,637 | **10,640** |
| Topic grades A / B / C | 76 / 228 / 396 | 36 / 149 / 323 | 63 / 241 / 415 | 81 / 328 / 522 | **182 / 371 / 567** |
| Topic Build now | 192 | 120 | 221 | 310 | **421** |
| `validate` good_visible (target ≥ 0.8) | 0.80 | 0.80 | 0.73 ✗ | 0.73 ✗ (cause: chart move, Step 5) | **0.73 ✗** (chart move, Step 5) |
| `validate` junk / label in top 100, hard flagged, grossing ρ | pass | pass | pass | pass | pass |

**Yield by source, final** (a term can come from several sources; C+ = grade C or better; "only here" = C+ terms
no other source found):

| Source | Cost | New terms | Needs | Popularity ≥ 15 (page) | C+ | C+ only here | Build now | C+ / new terms | C+ / terms with a page |
|---|---|---|---|---|---|---|---|---|---|
| Competitor titles (≥ 3 apps) | free, minutes | 1,848 | 1,772 | 1,127 | 349 | 337 | 94 | **18.9%** | 31% |
| Reddit pilot | $0.42 | 878 | 868 | 116 | 60 | 48 | 22 | 6.8% | **52%** |
| App Store autocomplete | free, ~7 h total | 11,898 | 8,094 | 1,918 | 679 | 655 | 194 | 5.7% | 35% |
| DataForSEO Play pilot (50 apps) | $1.70 kept | 6,219 | 3,896 | 460 | 137 | 137 | 54 | 2.2% | 30% |
| Google Play autocomplete | free, ~25 min | 5,845 | 5,400 | 327 | 76 | 52 | 24 | 1.3% | 23% |

All 26,269 new terms: 1,265 C+, 376 Build now. 96 needs still have no popularity (Astro timeouts); the next
weekly run retries them.

**App Store autocomplete, by part of the run** (finds are saved per chunk, so hour = when saved):

| Part | Time | New terms | C+ | Build now |
|---|---|---|---|---|
| 11-04 night: plain seeds + `a..z` for grade A seeds | ~1 h | 5,909 | 160 | 44 |
| 11-05: plain seeds (one query each) | ~1.5 h (11:00–12:59) | 5,261 | 507 | 147 |
| 11-05: `a..z` for grade B seeds (~10,000 queries) | **~4.5 h** (13:00–17:02) | 728 | **12** | **3** |

By seed grade (11-05): C+ per seed A 0.74, B 0.61, C 0.64, **D 0.29** (622 seeds).

Top new Build-now searches: ai dance generator (75.6), water out (74.5), photo to video (70.3), bible stories (69.4),
stress tracker (64.8), pdf document (64.8), live voice changer (64.3), chair yoga free seniors (57.1).
The searches that motivated the review: `hiit timer` E (12.0), `tabata timer` D (18.7), `interval timer` E: found
and measured, but their p5–p20 apps earn ~$1.1k/mo. `pomodoro timer` was not found by any source.

### Bugs found by the run (fixed)

- **Buildability judgements voided:** the freshness rule in `keyword_factors` / `factor_matrix` / research ("a metric
  with rows on the scoring date ignores older rows") also applied to buildability, which is judged a few keywords at a
  time. Judging 689 new terms turned all 171 old Build-now keywords into Watch. Fix: `index.JUDGED` metrics always
  keep each keyword's latest row; test `test_judged_metric_keeps_older_judgements`.
- **New terms never got Build now:** `expand finish` did not judge buildability. Fix: `judge_buildability` (new A–C
  terms without a judgement), then rescore.
- **80 searches graded A–C never judged for buildability** (found 2026-11-05 after run 2): `judge_buildability` only
  looked at the new terms, and only once, but relabelling and the rescore also lift old searches into A–C. Fix: judge
  every A–C keyword without a judgement, and repeat judge → rescore until none is left (max 3). Build now 550 → 564.
- Pilots crashed or deadlocked while holding a read connection; inputs are now read, the connection closed, then the
  network called.

### Decisions on the results (user, 2026-11-05)

| # | Question | Decision | Done |
|---|---|---|---|
| 9 | good_visible 0.73 | Investigate before changing the rule | Cause found (chart move, see below); deferred to Step 5 |
| 10 | Weekly sources | Keep all four: titles, App Store autocomplete, Play autocomplete, Reddit ($0.42/run) | `[step1.expand] weekly_sources / weekly_pilots`; `run-weekly` ends with `weekly.run_expand` (own connections, skipped when Astro is down) |
| 11 | Specificity multiplier | Keep linear 0.5 + 0.125 × (s − 1) | Superseded by #17 |
| 12 | DataForSEO Play pilot | Rerun half (25 apps, ≤ $1) | `[step1.expand] dfs_apps = 25, dfs_max_usd = 1.0`; 3,802 new terms for $0.87 |
| 13 | Partial runs | Run in full: DFS pilot's other 25 apps, App Store `seed a..z` hints for grade B seeds, Astro retry for needs with no popularity. Pages below popularity 15 stay unfetched | `data/cache/expand_done.json` records alphabet seeds and DFS apps already expanded (skipped for `redo_after_days = 28`), so reruns pay only for the rest; `expand collect --alpha-grades AB` |
| 14 | Pilot keep rule | Count only terms that reached a results page (popularity ≥ 15); report the all-terms share beside it | All five sources pass (23–52%). OPTIMISING.md Phase 2 |
| 15 | DataForSEO Play | Keep, in the weekly run | `weekly_pilots = "reddit,dfs_play_app"`: 25 apps not done in the last 28 days, ≤ $1 per run |
| 16 | Autocomplete speed | `seed a..z` for grade A seeds only; record the letter | `hint_alphabet_grades = "A"`; `expand_finds.info.letter`; test `test_hints_record_the_letter`. Next step: drop letters that never pay off once the weekly runs have per-letter data |
| 17 | Specificity multiplier (146 old Build-now searches lost) | Anchored: s1 0.5, s2 0.65, s3 0.8, s4–s5 1.0 | `specificity_mults` in `[step1.labels]`; test `test_specificity_multiplier_is_anchored`. 360 more A–C searches judged for buildability |
| 18 | `pomodoro timer` not found by any source | Accept the gap | — |
| 19 | Test the new weekly run now (~$1.40, 3–4 h)? | No: check the log and yield after the next normal weekly run | open |
| 20 | Close Step 1 | Close; accept OPTIMISING.md; do not commit yet | — |

Note: Reddit fails the "> 10% of new terms grade C+" rule (6.8%) but was kept: half of the Reddit terms that are
searched at all (popularity ≥ 15) grade C+. The rule should probably count terms with a page, not every term found;
settled by #14.

### Open after round 1

- **Label audit (open):** a few brand-like searches are labelled need and now reach Build now (`vizio tv app` 69.8,
  `vizio tv remote`, `mosaïque+`, `black hole` s2). The relabel with context still lets device/brand names through.
- **Build now is less rare:** 842 searches and 421 topics (A-grade topics 76 → 182). Most is new terms, plus s4 at
  ×1.0. Revisit whether the A/B cutoffs should be re-centred on the larger universe (Step 6/8).
- 56 new needs have no popularity after two Astro tries (most likely too rare for Astro); the weekly run retries them.

- good_visible 0.80 → 0.73: `water tracker` (concentration 0.89 → 0.927) and `intermittent fasting` (0.86 → 0.903)
  are now hidden by the relative concentration cutoff (top 10% of sectors).
  **Investigated (2026-11-05): not caused by Step 1.** Rebuilding the `water tracker` sector from both databases gives
  the same 186 relevant apps and the same total (~$1.18M/mo). The 11-04 concentration row was computed at 14:53,
  before that day's grossing charts were pulled, so it used the 11-01 chart: MyFitnessPal #2 in Health & Fitness →
  $424k/mo. On the 11-04 chart it is #1 → $825k/mo (rank^-0.86 nearly doubles the estimate for one place at the top),
  and its share of the sector rose 0.545 → 0.699. Step 1's only effect is the cutoff itself moving 0.905 → 0.895 as
  long-tail pages joined the distribution. **Deferred to Step 5 (filters)** and the revenue step: one day's chart move
  of a single leader can flip a niche's visibility (smooth chart ranks over days? compute the cutoff on Apple-list
  terms only?).
- **Faster, better autocomplete (decided 2026-11-05: after this run's results).** The full A+B `seed a..z` run
  (12,355 queries at Apple's ~40/min) takes ~5 h. Last night's hints: the letters n, o, u, k, q, y, x, j, z gave 0
  C+ terms; `f` ("… free") gave 26 of 66. Use the full run's per-letter and per-seed-grade yield to pick letters and
  a seed cutoff (`[step1.expand]`), then compare yield per hour.
  **Run 2 result:** `a..z` for grade B seeds cost ~4.5 h for 12 C+ terms (3 Build now); plain seed queries gave 507
  C+ in ~1.5 h. Per-letter yield is not recorded (`expand_finds.seed` holds the seed, not the letter); log the
  letter in `info` before tuning letters.

---

## 2026-11-05 · Grades by evidence + brand check (Steps 1, 6, 7, 8)

Asked after Step 1 closed ("how to improve, methods and results"). Rating before this round: methods 7.5/10,
results 6.5/10; Build now had grown to 842 searches because the score cut-offs were set on the old universe.

### Decisions (user, 2026-11-05)

| # | Area | Decision | Why |
|---|---|---|---|
| 21 | Grades | Grades from evidence, not score cut-offs: **A** can earn, **B** maybe, **C** no (plus **?**, #27). Everywhere at once (searches, topics, research) | User: "the one can really earn money as A; maybe or not sure as B, not possible as C" |
| 22 | A rule | p5–p20 band ≥ $5k/mo · **≥ 2** young winners (< 2 years, $5k+) · popularity **≥ 30** (~17/day) · buildable ≥ 0.6. A dominant leader is shown, not a gate | One young winner could be a fluke; 30 = enough organic traffic for first users; young winners already prove newcomers earn despite a leader |
| 23 | Labels | Second DeepSeek pass on A–B needs: does it name a specific app, game or company? | `mosaïque+`, `minecraft building guide` reached Build now |
| 24 | Validation | Young-winner hit rate, old vs new Build now | Free, uses stored data |
| 25 | Expansion | Mid-word autocomplete (`<letter> <seed>` on Play, DeepSeek modifier + seed) | Prefix autocomplete never finds `hiit timer` from `timer`. **Not built yet** |
| 26 | TV-brand remotes | Count as needs: third-party utilities for a brand's device (`vizio tv remote`, `tcl remote`, `chromecast remote`) | A real indie market. `expand.DEVICE_COMPANION` is never relabelled brand; 23 reverted; test `test_device_companions_stay_needs` |
| 27 | Not sure | Four labels: A, B, C and **?** (not sure: the market cannot be measured) | User: keep unknown apart from weak. Action "Not sure" |

### Changes

- `index.earn_grade` (one rule for searches and research aspects) with `[step6.grades]` `a_money_usd`, `a_young_min`,
  `a_min_popularity`, `a_min_buildability`, `b_money_usd`. Each search's meta keeps `checks` (which passed) and a
  reason ("18 young apps earn $5k+ · p5-p20 apps earn ~$21,705/mo · popularity 56 (~655/day) · buildable 0.70",
  or "missing: … | has: …"). Actions: A Build now, B Maybe, ? Not sure, C No, "-" No data. The score only orders.
- Topics: grade = best member's grade (A > B > ? > C); a hidden-only topic is C, or ? when its searches are only
  too thin. Breadth bonus counts A/B members.
- Research: aspect grade = same checks on the aspect (median band of its searches, young winners, summed
  searches/day, best judged buildability); nobody serves it yet → ?; overall = best aspect. Aspect rows carry
  `build` and `n_a`.
- `judge_buildability` judges every search that passes the other three A checks (138 judged).
- `expand brand-check` (`expand.brand_check`, `[step1.labels] brand_check_min_p = 0.7`): 1,211 checked, 36 → brand,
  23 of them TV-device remotes reverted (#26), 13 kept; $0.15. Each answer stored as metric `llm_brand_check`.
- Seeds re-mapped to the new letters: `seed_grades = "AB"` (cap `seeds_max = 4000`), `title_grades = "AB"`,
  `hint_alphabet_grades = "A"` capped at `alpha_max_seeds = 150` (A is ~630 searches; uncapped = ~7 h).
- Dashboard and research page: Maybe / Not sure replace Watch / Avoid / Unproven; tooltips explain the checks.
- ASO score keeps its old A–E cut-offs (it answers "should an existing app target this?"); the backtest still
  buckets by score.
- Tests: `test_earn_grade_three_levels`, `test_device_companions_stay_needs`; topic test updated (57 pass).

### Before → after

| | Before (score cut-offs, anchored) | After (evidence grades) |
|---|---|---|
| Searches A / B / ? / C (D/E) | 315 / 896 / – / 1,629 (D 2,633, E 18,043) | **634 / 3,509 / 644 / 19,373** |
| Searches Build now | 842 | **634** (A) |
| Topics A / B / ? / C (D/E) | 182 / 371 / – / 567 (D 752, E 8,768) | **347 / 1,148 / 230 / 8,915** |
| Topics Build now | 421 | **347** |
| GLP-1 (research / topic) | research A, topic C | research **B**, topic **B** (band ~$1.9k–2.7k: money not proven; 8–14 young winners) |

Of the 842 old Build-now searches (before #26): 207 A, 616 B (mostly popularity 19–29, e.g. `photo to video free`
25, `holy bible widget daily verse` 25; or one young winner, e.g. `pdf filler`), 19 C (brands). After #26
`vizio tv app` (69.8), `tcl tv remote`, `vizio tv remote` are A.

### Validation: young-winner hit rate

| Build-now set (score cut-offs) | n | ≥ 1 young winner | ≥ 2 |
|---|---|---|---|
| Before Step 1 | 315 | 89% | 79% |
| Added by Step 1 | 589 | 97% | 87% |
| After Step 1 | 842 | 95% | 85% |

The new searches were at least as good as the old ones on this check. Under the new rule every A has ≥ 2 by
definition.

### Open

- Mid-word autocomplete (#25) not built.
- Brand check keeps 13 brands; debatable: `asvab` (an exam: prep apps are an open need), `free pokemon card scanner`
  (companion utility, like remotes).

---

## 2026-11-06 · Step 2 review: who competes for a search

Step 2 turns a results page (~174 apps) into the **sector**: the apps that really serve the search. Every money
number and grade comes from it.

### How it was checked

DeepSeek judged every app on 40 sample results pages (10 one-word, 18 two-word, 12 longer searches; ~7,000 apps):
"would someone who typed this search treat this app as a real option?" Then each grade was recomputed with
DeepSeek's list instead of the word rule's. Cost $0.57 (two runs; the second at temperature 0 is cached in the
scratchpad, so re-checks are free). DeepSeek is not perfect either (it reads name + 300 characters; `nom`,
`couple calendar` are debatable): this gives patterns, not exact rates.

### Findings

| # | Issue | Example |
|---|---|---|
| 1 | The required word was the rarest word, often a describing word, so real competitors were thrown out | `timer widget` dropped 71 timers (Big Timer, Visual Timer); `app blocker for productivity` dropped Refocus, ScreenZen |
| 2 | Word forms did not match, so leaders were dropped | `translator` dropped Google Translate, DeepL; `running` dropped Strava ("Run") |
| 3 | Typos and other languages kept almost nothing → **?** | `camra` kept 0 of 173 (110 are cameras); `despertador` 2; `tato` 6 |
| 4 | Longer searches keep apps that share only some words → band money too high | `custom keyboard sounds` kept 125, ~17 make keyboard sounds; band ~×1.7 too high on 3+ word searches |
| 5 | Subtitles are not used: iTunes Search returns them for 678 of 2.3M rows | method said "name, subtitle, description" |
| 6 | Not an issue: ~23,000 "No data" searches have popularity < 15, so no page is fetched on purpose | |

### Changes (`metrics/relevance.py`)

- **Job word from Apple's top 10:** the required word is one ≥ 8 of Apple's top-10 names carry (the rarest of
  those), else the most common one; never a generic word (`ai`, `easy`). `timer widget` → *timer*,
  `calorie counter` → *calorie*, `ai mirror` → *mirror*.
- **Word forms:** wider stemming (translator ≈ translate, running ≈ run, editor ≈ edit, gifs ≈ gif; chess,
  fitness untouched). More small words ignored (from, no, into, without, ...).
- **Read-as:** when ≤ 2 of Apple's top 10 contain the words but the page is full, and the search is labelled a need
  (p_brand < 0.5), match on the word Apple's top 10 share: `camra` → camera, `despertador` → clock, `tato` → tattoo.
  Shown in the reason: "note: read as 'camera' (Apple's top results)". 110 searches.
- Docs: METHODOLOGY Step 2 rule table; sector docstring said "up to 100 apps" (it is 200).
- Tests: `test_word_forms_match_the_leaders`, `test_anchor_is_the_job_word_apple_shows`,
  `test_read_as_finds_what_apple_understood` (60 pass).
- Rescore + the normal follow-ups: 104 new A candidates judged for buildability, 3,770 brand checks (~$0.47).

### Before → after

40-search check (against DeepSeek's list):

| | Before | After |
|---|---|---|
| Real competitors thrown out | 29% | **11%** |
| Wrong apps kept | 27% | 31% |
| Grade same as with DeepSeek's list | 28 / 40 | **30 / 40** |

Whole universe:

| | Before | After |
|---|---|---|
| Searches under 30 relevant apps | 5,102 | **3,403** |
| Searches A / B / ? / C | 634 / 3,509 / 644 / 19,373 | **767 / 3,738 / 185 / 19,470** |
| Topics A / B / ? / C | 347 / 1,148 / 230 / 8,915 | **396 / 1,209 / 83 / 8,952** |

734 searches changed grade: ? → B 338, B → A 112, ? → C 86, B → C 74, C → B 63, ? → A 39, A → B 14, other 8.

| Search | Kept | Grade | Why |
|---|---|---|---|
| timer widget | 103 → 160 | B → B | money now $2,146 (was $3,637), 0 young winners (was 1): more small timers in the band |
| translator | 165 → 189 | A → A | Google Translate, DeepL back in |
| app blocker for productivity | 49 → 176 | B → B | 12 young winners; only traffic missing (27) |
| camra | 0 → 160 | **? → A** | read as camera |
| despertador | 2 → 148 | ? → B | read as clock |

### Round 2: picking a cheaper judge (bake-off)

User: "do we have a cheap while effective method?" Nine methods were scored against DeepSeek's answer key (40
searches, 7,103 pairs, grouped cross-validation for anything fitted) and against **120 disputed pairs labelled
blind** by hand (the key is itself DeepSeek, so it favours DeepSeek-like methods).

| Method | F1 vs key | Grades same / 40 | Blind check (hard cases) | Cost / search |
|---|---|---|---|---|
| Word rule (fixed above) | 0.77 | 30 | 44% | free |
| Free embeddings (BGE-small, MiniLM) | 0.72–0.73 | 30–34 | – | free |
| Free rerankers (BGE-reranker-base, MiniLM cross-encoder) | 0.71–0.77 | 27–30 | – | free |
| Small trained checker (rule + rank + embeddings) | 0.75–0.76 | 30–36 | – | free |
| DeepSeek, thinking on, names | 0.88 | 37 | 70% | ~$0.005 |
| DeepSeek, thinking on, name + 300 chars (the key) | – | – | 69% | ~$0.007 |
| **DeepSeek, thinking off, names** | 0.83 | 33 | **78%** | **~$0.0007** (peak) |
| DeepSeek, thinking off, name + 150 chars | 0.86 | 30 | 77% | ~$0.003 |

Free local models measure "similar topic", not "does the same job" (keyboard-theme apps look close to `custom
keyboard sounds`), so they did not beat the word rule.

### Decisions (user, 2026-11-06)

| # | Area | Decision | Why |
|---|---|---|---|
| 28 | Step 2 judge | DeepSeek, thinking off, app names only, decides who competes, for **every** results page; the word rule is the fallback for apps not judged yet | Best on the blind check, cheapest DeepSeek option |
| 29 | Thinking | Off only for the competitor judge; **on** for labels, brand check, buildability and everything else | Thinking off changed 16–20% of those answers, mostly for the worse (adult / illegal searches became needs, "holy bible niv" licensing missed) and would save ~$1/month |
| 30 | Cost | Later runs send only new apps (+ Apple's top 10 as context, fixed parts first for DeepSeek's prefix cache); prices follow DeepSeek's peak / off-peak hours | User: cache misses made the first pass expensive; price differs by time |

### Changes (round 2)

- `DeepSeekClient.competitors()` (names only, thinking off, answers the apps that do NOT serve the search);
  `json_chat(..., thinking=)` and `DeepSeekClient.thinking`; a JSON answer followed by junk is recovered.
- Table `app_relevance` (one answer per search × app); `collect_llm_relevance` (cached in
  `data/cache/llm_relevance.jsonl`, resumable; with `con=` inside the weekly run); `apptopics llm relevance`;
  wired into `run-weekly` (after buildability) and `expand finish`. `relevance.py` uses the answer where it
  exists; each search's meta says `competitors_by` (DeepSeek / word rule).
- Prices: `[unit_prices]` now holds DeepSeek's CNY prices (miss 1.00, hit 0.02, output 4.00 per M off-peak,
  ×2 at peak: weekdays 09–12 and 14–18 Beijing time; weekends off-peak). Each call is priced at its own time
  with cache hits (`budget.deepseek_usd_per_m`); the cost preview shows "DeepSeek is at PEAK price now …".
- The old flat price (0.14 / 0.28 USD) under-counted: the full pass was recorded at $4.26 but billed ~¥72
  (~$10.14, all at peak: 21.5M input misses, 3.6M output). A $5.88 correction is booked
  (`llm_relevance_price_fix`). Earlier DeepSeek jobs this month were also under-counted (output priced at ¼).
  DeepSeek cap raised 8 → 15 for this pass (month-to-date $14.11 after it).
- Tests: `test_deepseek_peak_hours_beijing`; budget plan test updated (61 pass).

### Run

All 14,257 results pages judged (14:17–14:53, 16 workers): 2.33M answers, 53% of apps kept. Then rescore,
16 buildability judgements, 101 brand checks ($0.04), rescore.

### Before → after (whole Step 2)

| | Before Step 2 | Word rule fixed | **DeepSeek judge** |
|---|---|---|---|
| Searches A / B / ? / C | 634 / 3,509 / 644 / 19,373 | 767 / 3,738 / 185 / 19,470 | **526 / 3,668 / 273 / 19,693** |
| Topics A / B / ? / C | 347 / 1,148 / 230 / 8,915 | 396 / 1,209 / 83 / 8,952 | **270 / 1,192 / 136 / 9,042** |
| Searches under 30 relevant apps | 5,102 | 3,403 | see `?` |

Of the 634 A searches before Step 2, 422 stay A, 212 left (mostly to B: the band money or the young winners came
from apps that do a different job), 104 are new.

| Search | Before | Word rule fixed | DeepSeek | Why |
|---|---|---|---|---|
| text editor free | A | A | **B** | photo / video "editors" (Photoshop Express, Splice, VN) were counted; band $23k → $2.7k |
| custom keyboard sounds | B | B | **?** | 13 apps make keyboard sounds; the rest are themes |
| ai mirror | B | B | **A** | 54 real competitors; band $22k, 4 young winners |
| camra | ? | A | A | 118 cameras |
| translator / calorie counter | A | A | A | unchanged |

### Round 3: check on random pairs, hybrid, languages, minimum size (2026-11-06)

Rating before round 3: methods 8/10, results 7.5/10.

| # | Area | Decision | Why |
|---|---|---|---|
| 31 | Validation | Claude labels 200 random search × app pairs by hand (50 searches × 4); kept as `gold200.json` in the round's scratchpad and re-used for every judge change | User: "you do it for me, larger pairs" |
| 32 | Judge | Hybrid built and tested, then **not run**: keep DeepSeek names only (user: "stop it. let's use current"). `apptopics llm relevance-desc` stays available (manual, off-peak only); without its answers `relevance()` gives exactly the names-only result | ~¥35 for +5 points on the random check |
| 33 | Languages | Other-language searches: tag + soft hide (grade kept, "Show hidden") | User |
| 34 | Minimum | Keep 30 relevant apps | 20 adds +3 A on a 3-app band |

**The random check changed the picture.** The bake-off's blind check used only disputed cases, which flattered
DeepSeek (78% vs 44%). On 200 random pairs:

| Method | Right | Missed real | Wrongly kept | F1 |
|---|---|---|---|---|
| Word rule (fixed) | 76% | 11 / 135 | 38 / 162 | 0.84 |
| DeepSeek names only (round 2, live) | 75% | 23 / 135 | 27 / 139 | 0.82 |
| DeepSeek, descriptions for vague names (12% of apps) | 78% | 24 | 20 | 0.83 |
| **Hybrid** (descriptions where rule and names disagree) | **80%** | 13 | 27 | **0.86** |
| DeepSeek, descriptions for every app | 83% | 14 | 20 | 0.88 |

Names-only misses are mostly names that do not say the job (`Light Lamb`, `Google Flow`, `Brave Browser` for
"browser with vpn", `Magic Tiles 3` for "play piano"). Lesson added to OPTIMISING: sample disputed cases to *find*
errors, but measure on random pairs.

Changes:
- `metrics/language.py` + `[step5.filters] hide_other_language = true`: 608 searches tagged (A 15, B 77, ? 8,
  C 164, no data 344). Grades kept; topics grade from English members only (4 topics B → C, 1 A → B; 29 topics
  made only of other-language searches without data show "-"). Test `test_other_language_tag`.
- `app_relevance.relevant_desc`, `collect_llm_relevance_desc` (`apptopics llm relevance-desc`, manual, off-peak
  only by default; not in `run-weekly` or `expand finish`), hybrid decision in `relevance()` (inactive while
  `relevant_desc` is empty); `relevance(use_llm=False)` gives the word rule alone.
- Min-size test on a copy (min 20): searches ? 273 → 170 (+3 A, +54 B, +46 C), topics ? 136 → 85. Kept 30.
- DeepSeek cap stays 15 (the hybrid pass was not run).

### Round 4: one-off manual review of grade-deciding disputes (2026-11-06)

User: "review those missed words manually for one time; pick the important apps." Scope: the 221 searches where the
word rule and DeepSeek disagree about grade **A**; the 100 most searched of them; the 4 biggest-earning disputed apps
of each = **400 pairs**, judged by hand by Claude (name + description). Stored in `app_relevance.relevant_manual`
(wins over every other answer) and `data/cache/manual_relevance_2026-11-06.json`.

- On these 400: DeepSeek agreed with the manual answer 60%, the word rule 40%. DeepSeek right: crypto trackers out
  of `coin id`, storage cleaners out of `email cleaner`. DeepSeek wrong: pixel-art apps out of `pixel art`,
  storage cleaners out of `photo cleaner`, ChatGPT / Claude out of `ai agent`.
- Grades: searches A 526 → **539** (13 B → A, 1 B → ?), topics A 269 → **276** (7 B → A). New A: `ai chat roleplay`,
  `ai tutor`, `brain training`, `daily planner`, `keyboard free`, `voice translator`, `voice changer free`,
  `trading game`, `gallery cleaner`, `ringtone maker free`, `beat maker pro`, `daily devotional`,
  `voice changer with effects`.
- **Second pass (user: "judge more, at least 2000, highest value first"):** 2,000 more pairs on 279 searches where the
  two methods give different grades: 1,415 on A-deciding searches (most searched first), the rest on B/C and ?/B
  disagreements; biggest-earning disputed apps first. DeepSeek agreed with the manual answer 61% (60% on the
  A-deciding ones), the word rule 39%. Typical DeepSeek misses: general wallpaper apps for `anime wallpaper`,
  screen-mirroring apps for `mirror`, cleaners that merge contacts for `contact cleanup`, Ethiopian-Bible apps for
  `book of enoch`. Adult-intent searches (`ai clothes eraser`, `uncensored image editor`) were judged 0 on purpose:
  they should be labelled rubbish in Step 1 (backlog).
- After all 2,400 manual answers: searches A 526 → 539 → **563**, ? 273 → **260**; topics A 269 → 276 → **288**.
  New A (second pass): `ai anime`, `ai character generator`, `ai chat characters`, `ai dance`, `ai interior design
  free`, `ai voice over generator`, `audio recorder for iphone`, `barcode reader for iphone`, `free sketching app`,
  `photo cleaner`, `hd photo enhancer`, `unblur photo`, `toddler learning games`, `noaa weather radar` and 12 more;
  `emoji battery widget` and `slumber` A → B; `faceless reels`, `vibration massager`, `daily readings orthodox`
  B → C.
- **Sonnet fan-out (user: "use multiple agents and Sonnet to grade all remaining, one-time job"):** the 69,410 remaining
  disputed apps that earn money (>= $2.5k/mo, or young >= $1.5k) on 4,349 A/B/? need searches, in 69 batches of
  ~1,000 (one Sonnet agent each, Apple's top 5 as context, same rubric as the hand review), plus a blind
  calibration batch of 300 hand-labelled pairs. 71 agents, ~11 minutes, 3 IDs re-asked. **Agreement with the hand
  labels: Sonnet 78%, DeepSeek 61%** on the same 300 hard pairs. Stored in `app_relevance.relevant_sonnet`
  (below manual, above DeepSeek); `data/cache/sonnet_relevance_*_2026-11-06.json`. Sonnet kept 44% of them.
  Searches A 563 → **628** (77 B → A, 18 A → B, 40 B → C), ? 260 → **231**; topics A 288 → **313**.
- **Bug found and fixed:** a read copy made before the `relevant_desc` column existed made `relevance()`'s query
  fail and fall back to the word rule silently (the grades in use were not affected: they were scored before).
  The query now checks which columns exist. Regression test `test_llm_answers_used_even_without_new_columns`.

### Round 5: TypeSafe Jev (2026-11-06)

User: "shall we try jev? much faster and cheaper while accurate ... all the options, choices, labelling related work should
go to jev if it works." Jev ("System One" model) answers typed yes/no / choice questions with probabilities;
$0.042 per M input tokens, output free. Client `sources/jev.py` (`TYPESAFE_API_KEY`, model pinned `jev-1.14.0`).

**Who competes (Step 2)**, against Claude's hand labels:

| Method | 200 random pairs | 300 hard pairs | Cost / time |
|---|---|---|---|
| Word rule | 76% | – | free |
| DeepSeek names only | 75% | 61% | ~¥72, 35 min (all pages) |
| DeepSeek with descriptions | 83% | – | ~3× names |
| Sonnet agents | – | 78% | 71 agents, 11 min (69k pairs) |
| **Jev, p ≥ 0.5** | **85%** | 74% (F1 0.75 vs Sonnet 0.72) | **$0.005 for 500 pairs, 6 s** |

Decision #35 (user): **Jev is the base judge for every app on every page** (`app_relevance.p_jev`, keep at
`JEV_THRESHOLD = 0.5`); priority manual > Sonnet > Jev > DeepSeek names > word rule. `collect_jev_relevance`
(`apptopics llm relevance-jev`), in `run-weekly` and `expand finish` (new apps only). Full pass: 2.33M pairs, ~10 min.

**Result of the Jev pass** (2.33M pairs, 46k calls, 119M input tokens, $5.01, ~9 min; 60% of apps kept). Then the new
A candidates went through the hybrids (buildability 17: Jev 9 / DeepSeek 8; brand check 47: Jev 32 / DeepSeek 15) and a
rescore.

| | Before Step 2 | After Sonnet | **Final (manual > Sonnet > Jev)** |
|---|---|---|---|
| Right on 200 random hand-labelled pairs | 68% | 76% | **86%** (missed 3 of 135, wrongly kept 25, F1 0.90) |
| Searches A / B / ? / C | 634 / 3,509 / 644 / 19,373 | 628 / 3,553 / 231 / 19,748 | **644 / 3,787 / 192 / 19,537** |
| Topics A / B / ? / C | 347 / 1,148 / 230 / 8,915 | 313 / 1,132 / 120 / 9,046 | **320 / 1,238 / 110 / 8,943** |

Jev keeps more real competitors than DeepSeek did, so 256 searches moved C → B (money that strict lists had hidden).

**Other labelling jobs** (same samples as the thinking test; answer key = agreements + Claude's judgement of every
disagreement):

| Job | DeepSeek | Jev, first prompt | Jev, narrow questions | **Jev first, DeepSeek when unsure** | to DeepSeek |
|---|---|---|---|---|---|
| Labels, need vs not (100) | 97% | 94% | 87% | **97%** | 35% |
| Labels, 3-way (100) | 93% | 77% | 82% | **93%** | |
| Brand check (80) | 81% | 85% | 86% | **93%** | 21% |
| Buildability (80) | 93% | 74% | 83% | **91%** | 44% |

Decision #36 (user: "replace deepseek by jev if more or less same accuracy"): `pipeline/judges.py` runs Jev first and
sends only the unsure cases to DeepSeek (labels: confidence < 0.7; brand check: 0.2 ≤ p ≤ 0.8; buildability:
0.25 ≤ score ≤ 0.75). Narrow questions helped brand check and buildability (one question per blocker: licensed data,
network effects, OS access, big game / regulated); for labels the first prompt was better. Answers say which model
gave them (`reason: "jev ..."`). Without a Jev key everything goes to DeepSeek as before.

Not moved: topic clustering / attach-hidden (grouping, not a fixed set of labels; attach-hidden could become a Jev
Choice over existing topics, up to 255 options, next to test). ~~Jev spend inside the label jobs is not yet written
to the `spend` table~~ **Done** (round 6).

### Round 6: open decisions (2026-11-06)

Decisions (user): adult-intent searches are **rubbish**; record Jev spend now; test attach-hidden with Jev.

**Adult intent.** Jev asked one yes/no question ("sexual, nude, undressing, uncensored / NSFW generators, hookups,
18+?") for all 33,388 searches labelled need ($0.53, 6 minutes). Claude reviewed the 310 with p ≥ 0.3 against
Apple's top apps: **79 adult** (`uncensored ai chat`, `ai clothes eraser`, `sex dice`, `adult dating app free`,
`video call with women`, `truth or dare game for adults` whose top app is "Sex Game for Couple: Dirty 18+"), stored as
rubbish with `model='manual'` (`data/cache/manual_adult_2026-11-06.json`); relabel runs now skip manual labels.
Not adult: LGBTQ dating (`gay`, `jack`), `quit porn blocker`, `ai baby maker`, `free people remover`, photo vaults.
User decision: phone-vibrator searches (5, incl. `vibration massager`, an A in round 4) are adult too; AI
girlfriend searches (36) and couples / party games for adults (15) stay needs.

Jev alone is noisy in the middle (`quit porn blocker` 0.59), a word list alone is too broad (`sex offender map`,
`brain games for adults`). Rule for new searches (`metrics/adult.py`, asked inside the Jev label call):

| rule | adult caught (of 79) | wrongly flagged |
|---|---|---|
| Jev ≥ 0.75 | 23 | 1 |
| Jev ≥ 0.5 | 69 | 41 |
| word cue alone | 75 | 217 |
| **word cue and Jev ≥ 0.3, or Jev ≥ 0.75** | **77** | **1** (`juegos para adultos`) |

Tuned on the same set, so expect a little worse on new searches.

Before → after: searches B 3,787 → 3,767, ? 192 → 190, C 19,537 → 19,609 (A 644 unchanged); topics B 1,238 → 1,230,
? 110 → 108, C 8,943 → 8,953 (A 320 unchanged).

**Jev spend.** `judges.py` collects Jev tokens per job; `record_jev(con)` writes them (api `jev`, job `llm_labels` /
`llm_brand_check` / `llm_buildability`) where the job records its DeepSeek spend. Found on the way: `label_terms`
(discover / expand) recorded no spend at all, DeepSeek included; fixed.

**Attach to topics with Jev (test only).** 300 searches already in a topic, shortlist of 12 topics by shared words,
Jev picks one or "none" ($0.01, 7 seconds):

| | count |
|---|---|
| own topic in the shortlist | 276 / 300 |
| Jev picks DeepSeek's topic | 222 / 300 (74%) |
| Jev confident (≥ 0.7) | 224, of which 194 agree (87%) |

The confident disagreements are nearly all near-duplicate topics, both right: `budget planner` → "Budgeting app"
(DeepSeek) vs "Budget planner" (Jev); `rain sounds for sleep` → "Sleep sounds / white noise" vs "Rain sounds";
`secure vpn-safer internet` → "VPN" vs "Secure VPN". Jev usually picks the more specific one. Finding: the topic
merge leaves many near-duplicates.

### Open

- DeepSeek is stricter than a person on some pages (`new emojis for iphone free` excludes Facemoji Emoji Keyboard
  and "Emoji Maker").
- The brand check flagged some generic needs as brands (`insomnia coach`, `big math flash cards`, `yoga workout 3d`).
- ~~Model id~~ **Done:** `DEEPSEEK_MODEL=deepseek-flash` (the API lists only `deepseek-flash` and `deepseek-v4-pro`; `deepseek-v4-flash` was an alias answered by `deepseek-flash`, so every call this month already used it: nothing re-run).

---

## Backlog for later steps (found during Step 1)

| Step | Item | Evidence |
|---|---|---|
| 1 Labels | ~~Adult-intent searches graded as needs (`ai clothes eraser`, `uncensored image editor`)~~ **Done 2026-11-06**: rubbish (Step 2 round 6) | Manual review 2026-11-06 |
| 5 Filters | Concentration cutoff flips visibility on one day's chart move | `water tracker`, `intermittent fasting` hidden after MyFitnessPal #2 → #1 in Health & Fitness (see Step 1 open items) |
| 6 Score | Demand counts social views at most 25%: markets reached through TikTok/ads score low | GLP-1 medication tracker: demand 0.24 (~230 App Store searches/day) vs 4.5M video views |
| 6/7/8 | ~~Topic grade and research grade share letters but answer different questions~~ **Done 2026-11-05**: one evidence rule for both (#21) | GLP-1 now B in both |
| 8 Research | Verdict parts cap quickly (4 young winners, 3 advertisers, 3 wishes = full). The grade no longer uses the points (#21); the points still order aspects | GLP-1 "food, protein and fiber logging" 97/100 |
