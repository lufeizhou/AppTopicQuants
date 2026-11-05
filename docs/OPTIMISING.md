# Optimising a model step

How to review one step of the opportunity model (the steps in [methodology.md](methodology.md) §3), change it, and
prove the change helped. Written after Step 1 (2026-11-04/05); every example below is from that round. Agents:
follow the phases in order and do not skip the stops marked **ASK**.

Outputs of every round: a section in [CHANGELOG.md](CHANGELOG.md), numbers only in `config/model.toml`, tests,
and an updated methodology section.

---

## Phase 0. Ground rules

- **The user decides.** Present options with a recommendation; never pick a design choice silently. Ask with a
  multiple-choice question, recommended option first, each option with its cost or effect in numbers.
- **Every number lives in `config/model.toml`**, in the step's section (`[step1.expand]`, `[step6.score]` …), with a
  comment saying why. Code reads it through `settings.section("name")`. A change there is a new backtest model
  version (the version hash covers the file).
- **Money:** free first. Any paid source is a *pilot* with a hard cap (`max_usd`) agreed beforehand. Ask before
  spending past an agreed pilot. Record every spend in the `spend` table (`budget.record`).
- **Nothing is scheduled.** Weekly and light runs are buttons; a kept source joins `run-weekly`, not a cron job.
- **Show the logic.** Every reported number comes with how it was built (inputs → rule → result).

## Phase 1. Explain the step (no changes yet)

Write for the user, in this order:

1. **Process:** what the step does, in 3–6 numbered lines.
2. **In / out:** tables and row counts (`Out: 21,279 keywords with popularity`).
3. **Rationale and weights:** each rule with its number and *why that number* (or "uncalibrated").
4. **One worked example end to end**, with real values from the database
   (`timer widget`: popularity 35 → 70 × 1.15^(35 − 40) ≈ 35 searches/day).
5. **Weak spots you found**, each with evidence (`hiit timer`, `tabata timer` missing: autocomplete only extends a
   seed's prefix).

**ASK** one question per weak spot (max 4 per round of questions): what to change, with options.

## Phase 2. Decide

Record each decision in CHANGELOG at once (table: # · area · decision · why). If an answer opens a sub-question
(for example "which new sources?"), research it (prices, endpoints, limits) and ask again before building.
Write down the **success rule** for anything experimental *before* running it, for example:

> Keep a paid source only if > 10% of its new terms grade C or better.

Define the denominator precisely. Step 1 lesson: "of its new terms" counted terms nobody searches (no page, never
graded), so Reddit scored 6.8% overall but 52% of the terms that were measurable. **Rule (decided 2026-11-05):**
count only the terms that reached the stage where they can be judged (Step 1: a results page, popularity ≥ 15),
and report the all-terms share beside it.

## Phase 3. Baseline (before touching code)

1. Copy the read-only database to the scratchpad: `before/apptopics.read.duckdb`. Back up the main DB before any
   migration.
2. Save the numbers you will compare: grade and action counts for keywords and topics, `apptopics validate`, and
   the worked examples' scores. A small script (`compare.py`: latest `opp_score` per entity in two files → grade /
   action counts and changed entities) is reused at the end.

## Phase 4. Build

1. **Config first:** move every number the step uses into its section, with a reason per number.
2. **Neutrality test:** after a refactor and *before* any behaviour change, rescore a copy and check scores are
   identical. Step 1: all 21,279 scores reproduced except the 105 old `broad` terms, the one intended change.
3. **Separate the effects.** If a round changes rules *and* data, score the baseline with the new rules but old
   data ("labels only") so each effect can be measured. Step 1: new labels alone took Build now 315 → 171; the
   new terms then added 171 more.
4. **Long jobs:**
   - Every source saves as soon as it finishes. A crash must not lose paid data (Step 1 lost a $1.77 pilot
     this way).
   - DuckDB has one writer. Read the inputs, **close the connection**, *then* call the network, then write
     through `write_retry`. A read connection held in a process blocks that process's own writes (self-deadlock
     seen twice).
   - Measure throughput on a sample before the full run and give the user an ETA. Rates seen: Astro popularity
     ~500 terms / 6 min, iTunes search 18/min (shared limiter), Google Play suggest ~90/min, App Store hints ~57/min.
     Cut scope in config if the ETA is absurd (App Store alphabet hints for grades A+B = 3.3 h → grade A only).
   - Run in the background with a log in `logs/`; report progress when asked; never poll in a loop.
   - Record enough with each output to split its yield later (Step 1 stored the autocomplete seed but not the
     letter, so letters could not be tuned after a 7 h run).
   - Watch yield *during* a long run. Step 1's last 4.5 h (`a..z` for grade B seeds) found 12 good terms; the
     first 1.5 h found 507. When the marginal yield collapses, tell the user and offer to stop.
5. **Tests** in `tests/test_stepN.py` for every rule and for every bug found (regression test named after it).

## Phase 5. Run and compare

1. Run the new step on the real data.
2. Compare with the baseline in one table: Before · rules-only · After. Rows: entity counts, grades A/B/C, Build
   now, topics, `validate` checks.
3. **Yield per new input** (source, label, filter): count → passed each gate → graded C+ → Build now, plus cost.
4. **Look at examples.** The top 20 new winners, the cases that motivated the round, and anything that moved a lot.
   Read them as a user would: is `ai scanner` really a specific need?
5. **Explain every surprise before reporting.** A suspicious pattern is a bug until shown otherwise. Step 1:
   - "All 171 old Build-now keywords lost it, all 171 new ones gained it." Cause: a freshness rule voided
     incrementally judged metrics. Fixed, with a test.
   - "good_visible dropped." Cause: a grossing-chart move unrelated to the step. Rebuilding the sector from both
     databases showed identical inputs.

   Isolate by recomputing one entity from both databases and diffing its inputs.
6. Spend: sum the `spend` table for the round, lost money included.

## Phase 6. Report and ask

Report in this order: the verdict table, yield, bugs fixed, open issues with their cause, spend. Then **ASK**
about: what to keep (sources, rules), threshold changes the results suggest, reruns of anything that failed, and
issues that belong to another step (record them there; do not fix them in this round).

## Phase 7. Close the round

- **CHANGELOG:** decisions, changes, run and costs, before → after, bugs, decisions on the results, open items
  with the step they are deferred to.
- **methodology.md:** rewrite the step's section to the new behaviour, with examples. Keep the step-by-step layout.
- **Weekly run:** wire anything kept into `run-weekly`, behind config switches (`[step1.expand] weekly = true`,
  `weekly_sources`, `weekly_pilots`).
- **Tests** pass (`.venv/bin/python -m pytest -q tests`); `apptopics validate` is run and any failure explained.
- Commit only when the user asks.

## Checklist (copy into the CHANGELOG entry while working)

```
[ ] Phase 1 explained: process, in/out, weights with reasons, worked example, weak spots
[ ] Decisions recorded; success rules written before running
[ ] Baseline copied; compare script ready
[ ] Numbers moved to config/model.toml with reasons; neutrality test passed
[ ] Rules-only run measured separately
[ ] Long jobs: save per source, no read connection across network calls, ETA given
[ ] Before → after table, yield per input, examples read
[ ] Every surprise explained (bug fixed + test, or cause recorded and deferred)
[ ] Spend totalled
[ ] User asked about keep / tune / rerun
[ ] CHANGELOG, methodology, weekly run, tests, validate
```
