# TODO

## Open decisions (ask Lufei)

- [ ] Opportunities social/ads: run the full A–C pass (`apptopics social-topics --grades ABC`, ~600 topics, ≈ $13 once,
      ~$5 a month if repeated)? Piloted on 5 topics 2026-11-03; mostly +1–2 points where videos have millions of views.
- [ ] Weekly snapshots: click **Run weekly update** each week (Wednesday or later). First backtest 2026-11-30.

## Next

- [ ] **Backtest** (2026-11-30, `apptopics backtest`): do A/B beat C–E on newcomer entry? Retune weights from evidence.
- [ ] **Probe-app log**: model prediction before launch vs ASC after (RightMeal / SparkNote metadata tests; SparkNote
      estimate in `data/research/sparknote_metadata_estimate_2026-11-03.txt`, check against ASC in 4–6 weeks).
- [ ] **Difficulty → expected rank**: strength-vs-difficulty tables; validate on our own launches.
- [ ] **Paid-UA blind spot**: apps with revenue but few ratings (Calyra); Play installs / chart presence as predictors.
- [ ] **Paywall-aware revenue per download** (hard paywall vs freemium).
- [ ] **Other storefronts** (UK, CA, AU, DE) for localization wins.

## Nice to have

- [ ] Watchlist with week-by-week history per starred keyword; sparklines; CSV export.
- [ ] Clear the ~10k research keywords tracked under the "AppTopicsQuants Research" placeholder in Astro.
- [ ] Reddit / X keys for the buzz factors.

## Parked

- Pain points for all niches and Google Play reviews: built (`llm pain-top`, `dfs play-reviews`), stopped as not
  useful enough; 420 iTunes-review apps and ~150 Play-review apps are stored.
- DataForSEO Labs keyword ideas: one pilot ($0.48, 2026-11-02), code removed 2026-11-04.

## Done

**2026-11-04** — cleanup and review. Removed: DataForSEO Labs pilot, topic roll-up, refresh helpers, unused clients,
the old `calibrate` fit and its dead weights, Reddit/X buzz factors and sources (ScrapeCreators replaced them), the
pain-point and persona-vote jobs from the weekly run (CLI kept), the Budget pain button. Ruff config in
`pyproject.toml` (clean), one `_spawn` helper for dashboard jobs, `.env.example`, `own_apps.example.toml`, docs trimmed.
Bugs fixed (16 from the review): verdict button left reports "busy" forever; rescoring on a non-scoring day did
nothing (index now falls back to the latest composite and reads factors with the composite's freshness rule, so
stale bands no longer grade shrunken sectors; dashboard and report tables use the same rule); 740 game / non-Latin /
excluded keywords were "Unproven" instead of Avoid; DeepSeek spend of research, market and pain-point jobs was never
recorded or capped; DataForSEO volume / trends / Play jobs and label jobs ran without a cap check; one failed Reddit
request dropped everything already paid for; zero video views and failed video searches were the same thing;
long-tail discovery wrote outside the lock discipline; observed rating velocity needed a minimum gain; reach
curve and "30 needed" now read from config; popularity-less sectors are Unproven, not E; ScrapeCreators cap is
visible and editable; Google Trends slope kept None; TikTok links stored without creator handles.
Model: Build now requires a judged buildability (unknown → Watch) and buildability ran for all A–D keywords;
Unproven split into **Unproven** (thin market, with its off-store signals shown) and **No data** (page never fetched).

**2026-11-03** — Topics as the main view (1,075 topics after merge), hidden searches attached to topics; dashboard
redesign (direction A, light, six columns, ease × money map, compare drawer, your-apps overlay, deep links, cached
list API); Research market report (aspects, crowdedness, prices, feature map, happy-user reviews, positioning
options), grounded aspects, Reddit + TikTok + YouTube voice of users, Meta Ad Library per competitor; model:
`newcomer_winners`, `social_views`, `ad_activity`, grade ? / Unproven, `parts.in` stored; aspect verdicts and
overall verdict; report redesign (verdict first, scorecard, collapsed sections, score waterfall); topic social pilot
(5 topics, 51 credits); own-sales check (SparkNote); SparkNote metadata estimate.

**2026-11-02** — ASC sales imported; DataForSEO, TrustMRR, MobileMRR connected; keyword labels for ~21,000 terms;
buildability; relevant-apps-only sectors, intent and weak-spots factors, concentration cutoff, reach gating;
revenue calibrated on 487 verified apps; recommendation index (grades, actions, reasons); research on demand;
long-tail discovery (12,649 terms); manual weekly run with warnings; backtest snapshots; `apptopics validate`.

**2026-11-01** — skeleton, DuckDB schema, sources, first factors, spend caps, first full collection.
