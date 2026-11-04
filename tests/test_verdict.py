"""Verdict parts and the Unproven grade (offline)."""
from apptopics.pipeline import verdict as V
from apptopics.pipeline.index import opportunity_parts


def _run():
    market = {
        "aspects": [{"aspect": "Food logging", "band": "Emerging", "searches_day": 23, "established": 11, "small_apps_earn": 1887},
                    {"aspect": "AI meal check", "band": "Empty", "searches_day": 8, "established": 0, "small_apps_earn": None}],
        "competitors": [{"app_id": "1", "name": "PeptidePal", "aspects": ["Food logging"], "age_months": 6, "est": 30000},
                        {"app_id": "2", "name": "Old Giant", "aspects": ["Food logging"], "age_months": 80, "est": 90000},
                        {"app_id": "3", "name": "Tiny", "aspects": ["Food logging"], "age_months": 3, "est": 400}],
        "ads": [{"app_id": "1", "name": "PeptidePal", "active_ads": 12, "verified": True, "oldest_active_days": 92},
                {"app_id": "2", "name": "Old Giant", "active_ads": 30, "verified": False, "oldest_active_days": 10}],
        "social": {"aspects": [{"aspect": "Food logging", "tiktok": {"views": 3_000_000, "videos": 20, "median_views": 5000},
                                "youtube": {"views": 1_500_000, "videos": 20, "median_views": 4000}},
                               {"aspect": "AI meal check", "tiktok": {"views": 10_000, "videos": 5, "median_views": 900}, "youtube": {"views": 0, "videos": 0}}]},
        "voice": {"wishes": [{"wish": "fiber on the dashboard", "aspect": "Food logging", "unmet": True},
                             {"wish": "photo check", "aspect": "AI meal check", "unmet": True}, {"wish": "dark mode", "aspect": "Food logging", "unmet": False}]},
        "term_aspect": {"glp-1 food tracker": "Food logging", "glp-1 meal checker": "AI meal check"},
    }
    return {"id": "t1", "topic": "glp-1", "result": {"market": market, "top_opportunities": []}}


def test_aspect_verdicts_parts_and_order():
    out = V.aspect_verdicts(_run())
    food, meal = out[0], out[1]
    assert food["aspect"] == "Food logging" and food["score"] > meal["score"]
    assert food["parts"]["opening"] == 1.0 and food["parts"]["newcomers_win"] == 0.25      # one young winner of four needed
    assert food["parts"]["someone_pays"] == 1 / 3 and food["parts"]["white_space"] == 1 / 3
    assert abs(food["parts"]["people_care"] - (6.653 - 4) / 3) < 0.01                       # 4.5M views
    assert meal["unproven"] and meal["parts"]["opening"] == 0.7
    assert food["evidence"]["young_winners"][0]["name"] == "PeptidePal"
    assert sum(food["weights"].values()) == 1.0 and "young app" in food["why"]


def test_opportunity_parts_use_newcomers_social_and_ads():
    base = {"breadth": 0.13, "difficulty": 36, "weak_spots": 3, "entrant_share": 0.9, "demand": 5, "concentration": 0.6, "buildability": 0.7}
    meta = {"attainable": {"attainable_usd": 1887}}
    p0 = opportunity_parts(base, meta)
    import math
    p1 = opportunity_parts({**base, "newcomer_winners": math.log1p(5), "social_views": 6.65, "ad_activity": 0.6}, meta)
    assert p1["money"] > p0["money"] and p1["demand"] > p0["demand"] and p1["score"] > p0["score"]
    assert p1["uses_social"] and not p0["uses_social"]
    assert p1["in"]["newcomers"] == 5.0 and p1["in"]["video_views"] == round(10 ** 6.65)
    assert opportunity_parts({"breadth": None}, {}) == {}                                   # no sector -> unproven upstream
