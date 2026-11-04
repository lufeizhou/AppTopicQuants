
from apptopics import budget


def test_estimate_and_guard(con, tmp_path, monkeypatch):
    # isolate the config file
    cfg = tmp_path / "budget.toml"
    cfg.write_text("[caps]\ndeepseek = 1.0\ndataforseo = 0.5\nscrapecreators = 0.1\ntotal = 1.2\n[cadence]\ntrends_every_days = 7\nvolume_every_days = 7\n"
                   "[unit_prices]\ndeepseek_in_per_m = 0.14\ndeepseek_out_per_m = 0.28\ndataforseo_volume_task = 0.06\ndataforseo_trends_task = 0.011\nscrapecreators_credit = 0.005\n")
    monkeypatch.setattr(budget, "BUDGET_PATH", cfg)
    monkeypatch.setattr(budget.subprocess, "run", lambda *a, **k: None)
    assert budget.estimate_job("dataforseo", "trends", 1000) == 250 * 0.011
    assert budget.estimate_job("dataforseo", "volume", 1500) == 2 * 0.06
    assert budget.estimate_job("scrapecreators", "reddit_research", 50) == 0.25
    assert budget.guard(con, "scrapecreators", "reddit_research", 50) is False          # 0.25 > cap 0.1 -> skipped + warning
    assert con.execute("SELECT count(*) FROM warnings WHERE api='scrapecreators'").fetchone()[0] == 1
    assert budget.guard(con, "dataforseo", "volume", 1500)   # 0.12 within 0.5
    budget.record(con, "dataforseo", "volume", 2, 0.12)
    budget.record(con, "deepseek", "llm_brands", 1e6, 0.9)
    assert budget.month_to_date(con) == 1.02
    ok, reason = budget.check(con, "deepseek", 0.3)           # 0.9 + 0.3 > deepseek cap 1.0
    assert not ok and "deepseek" in reason
    ok, reason = budget.check(con, "dataforseo", 0.3)         # within api cap, but total 1.02 + 0.3 > 1.2
    assert not ok and reason.startswith("total")
    budget.save_caps({"deepseek": 20, "total": 50}, {"trends_every_days": 14})
    c = budget.load()
    assert c["caps"]["deepseek"] == 20 and c["caps"]["total"] == 50 and c["cadence"]["trends_every_days"] == 14
    plan = budget.plan(con, {("deepseek", "llm_votes"): 50, ("scrapecreators", "reddit_research"): 10})
    assert plan[-1]["api"] == "total" and len(plan) == 3
