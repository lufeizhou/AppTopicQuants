"""Step 1 review (2026-11-04): config flattening, specificity multiplier, title phrase mining."""
import pytest

from apptopics.config import flatten_model, settings
from apptopics.pipeline.expand import title_phrases
from apptopics.pipeline.index import searches_per_day, specificity_mult


def test_flatten_model_lifts_step_sections():
    raw = {"target": {"monthly_usd": 5000}, "step1": {"universe": {"x": 1}}, "step6": {"score": {"money_exp": 0.45}},
           "run": {"sources": {"search_top": 10}}}
    flat = flatten_model(raw)
    assert flat["universe"] == {"x": 1} and flat["score"]["money_exp"] == 0.45 and flat["sources"]["search_top"] == 10
    assert flat["target"]["monthly_usd"] == 5000


def test_flatten_model_rejects_duplicates():
    with pytest.raises(ValueError):
        flatten_model({"step1": {"filters": {}}, "step5": {"filters": {}}})


def test_model_toml_has_every_step_section():
    w = settings.weights()
    for name in ("universe", "expand", "labels", "monetization", "filters", "score", "reach", "grades", "actions", "aso",
                 "topics", "verdict", "composite", "sources", "target", "constraints"):
        assert name in w, name


def test_specificity_multiplier_is_anchored():
    """Only vague searches are cut; specific ones (4, 5) keep their score (decision 17, 2026-11-05)."""
    assert specificity_mult(5) == pytest.approx(1.0)
    assert specificity_mult(4) == pytest.approx(1.0)
    assert specificity_mult(3) == pytest.approx(0.8)
    assert specificity_mult(1) == pytest.approx(0.5)
    assert specificity_mult(None) == pytest.approx(1.0)


def test_searches_per_day_rule():
    assert searches_per_day(40) == pytest.approx(70)
    assert searches_per_day(35) == pytest.approx(70 / 1.15 ** 5)
    assert searches_per_day(None) == 0


def test_title_phrases_split_segments_and_stopwords():
    got = title_phrases([("1", "Seconds: HIIT Tabata Timer", None), ("2", "Tabata Timer for HIIT", None),
                         ("3", "Interval Timer - HIIT Tabata Timer", "Workout timer & more")])
    assert got["tabata timer"] == {"1", "2", "3"}
    assert "hiit tabata" in got and "hiit tabata timer" in got
    assert "timer for" not in got and "for hiit" not in got          # stop words at the edges
    assert "seconds hiit" not in got                                   # ':' splits segments


def test_judged_metric_keeps_older_judgements():
    """Judging new keywords today must not void older buildability judgements (2026-11-05 bug: every old Build now
    turned Watch after `expand finish` judged 689 new terms)."""
    import datetime as dt

    import duckdb

    from apptopics.pipeline.index import keyword_factors
    con = duckdb.connect()
    con.execute("CREATE TABLE factor_values (entity_type TEXT, entity_id TEXT, metric TEXT, value DOUBLE, meta TEXT, snapshot_date DATE)")
    d0, d1 = dt.date(2026, 11, 4), dt.date(2026, 11, 5)
    con.execute("INSERT INTO factor_values VALUES ('keyword','old','buildability',0.8,NULL,?), ('keyword','new','buildability',0.7,NULL,?),"
                "('keyword','old','concentration',0.5,NULL,?), ('keyword','new','concentration',0.4,NULL,?)", [d0, d1, d0, d1])
    F, _ = keyword_factors(con, ("buildability", "concentration"), d1)
    assert F["old"]["buildability"] == 0.8 and F["new"]["buildability"] == 0.7
    assert "concentration" not in F["old"]          # recomputed metrics still drop stale rows


def test_app_titles_are_not_search_terms():
    from apptopics.pipeline.expand import _ok
    assert _ok("sleep calculator") and not _ok("sleep calculator: bedtime") and not _ok("chatverse - ai chat characters")


def test_hints_record_the_letter(monkeypatch, tmp_path):
    """Per-letter yield needs the letter that produced each autocomplete term (2026-11-05)."""
    from apptopics.pipeline import expand
    from apptopics.sources import itunes
    monkeypatch.setattr(expand, "DONE_PATH", tmp_path / "done.json")
    monkeypatch.setattr(itunes.ITunesClient, "hints", lambda self, q: [f"{q} app"])
    monkeypatch.setattr(expand, "_cfg", lambda: {"hints_per_minute": 1e6})
    found = expand.from_hints(["timer"], ["timer"], known=set())
    assert found["timer app"]["letter"] is None
    assert found["timer f app"]["letter"] == "f" and found["timer f app"]["seed"] == "timer"


def test_earn_grade_three_levels():
    """A/B/C from evidence (decided 2026-11-05): all four checks -> A; money but a check fails -> B; none -> C."""
    from apptopics.pipeline.index import earn_grade
    assert earn_grade(20000, 5, 40, 0.9)[0] == "A"
    assert earn_grade(20000, 5, 25, 0.9)[0] == "B"                       # too little traffic
    assert earn_grade(20000, 1, 40, 0.9)[0] == "B"                       # one young winner could be a fluke
    assert earn_grade(20000, 5, 40, None)[0] == "B"                      # not judged buildable yet
    assert earn_grade(20000, 5, 40, 0.3)[0] == "C"                       # judged hard to build
    assert earn_grade(500, 0, 60, 0.9)[0] == "C"                         # no money signal
    assert earn_grade(500, 0, 60, 0.9, unknown="thin sector")[0] == "?"  # not sure: unknown, not bad


def test_device_companions_stay_needs():
    """Third-party remotes for a TV brand are an open market (user, 2026-11-05); a game or app name is a brand."""
    from apptopics.pipeline.expand import DEVICE_COMPANION
    assert DEVICE_COMPANION.search("vizio tv remote") and DEVICE_COMPANION.search("free fire stick remote control")
    assert DEVICE_COMPANION.search("lg tv app") and not DEVICE_COMPANION.search("minecraft building guide")
