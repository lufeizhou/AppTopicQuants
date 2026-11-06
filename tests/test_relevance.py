import re

from apptopics.metrics.relevance import patterns, stem, threshold


def _match(keyword, text):
    pats = patterns(keyword)
    hit = sum(bool(re.search(p, text.lower())) for p in pats) / len(pats)
    return hit >= threshold(len(pats))


def test_stem_keeps_short_words_and_merges_variants():
    assert stem("maker") == "maker"          # 'mak' would be meaningless
    assert stem("scanner") == "scan"
    assert stem("tracker") == "track" and stem("tracking") == "track"
    assert stem("ross") == "ross"


def test_relevance_matches_real_competitors_not_brand_fillers():
    assert _match("habit tracker", "Streaks - track your daily habits")
    assert _match("invoice maker", "Invoice Simple: invoice maker for small business")
    assert _match("calorie counter", "Cal AI - Calorie Tracker")          # half of the words is enough
    assert not _match("me@walmart", "Workday: HR for employees")
    assert not _match("pdf scanner", "Photo Editor Pro")


def test_short_tokens_need_whole_word():
    assert not _match("ai", "Aid Kit: first aid guide")
    assert _match("ai", "AI Chat assistant")


def test_word_forms_match_the_leaders():
    assert _match("translator", "Google Translate")
    assert _match("running", "Strava: Run, Bike, Walk")
    assert _match("photo editor", "Photo Edit & Filters")
    assert stem("chess") == "chess" and stem("fitness") == "fitness" and stem("glasses") == "glass"
    assert _match("gif maker", "GIFs creator - make gifs")


def test_anchor_is_the_job_word_apple_shows():
    from apptopics.metrics.relevance import anchor
    top = ["big timer", "visual timer", "timer widget", "pento timer", "countdown widget"]
    df = {"timer": 500, "widget": 80}.get
    assert tokens_at(anchor("timer widget", top, df), "timer widget") == "timer"        # rarest would be 'widget'
    top = ["calorie counter", "calorie tracker", "calorie counter", "lose it calorie counter", "calorie counter pro"]
    df = {"calori": 90, "count": 900}.get
    assert tokens_at(anchor("calorie counter", top, df), "calorie counter") == "calorie"  # both near-required: the rarer
    top = ["ai mirror", "mirror ai", "ai photo", "magic mirror", "ai video"]
    assert tokens_at(anchor("ai mirror", top, {"ai": 9000, "mirr": 50}.get), "ai mirror") == "mirror"   # never 'ai'


def tokens_at(i, kw):
    from apptopics.metrics.relevance import tokens
    return tokens(kw)[i]


def test_read_as_finds_what_apple_understood():
    from apptopics.metrics.relevance import read_as
    assert read_as(["camera", "blackmagic camera", "dazz cam - vintage camera", "timestamp camera", "huji cam", "pro camera"]) == "camera"
    assert read_as(["discord", "arc search", "lidl plus", "дія"]) is None


def test_other_language_tag():
    from apptopics.metrics.language import other_language
    for k in ("foto", "traductor de ingles a español", "la biblia", "juegos para niños", "verset du jour", "übersetzer"):
        assert other_language(k), k
    for k in ("camra", "photo editor", "lockdown", "y2k", "free ai chat", "text editor", "la la land"):
        assert not other_language(k), k


def test_llm_answers_used_even_without_new_columns():
    """Regression (2026-11-06): a read copy made before relevant_desc existed made the query fail and relevance()
    fell back to the word rule silently. Answers must apply with or without the optional columns; manual wins."""
    import duckdb

    from apptopics.metrics import relevance as R
    con = duckdb.connect()
    con.execute("CREATE TABLE sr_latest (store TEXT, keyword TEXT, app_id TEXT, name TEXT, subtitle TEXT, rank INT, snapshot_date DATE)")
    con.execute("CREATE TABLE apps (store TEXT, app_id TEXT, description TEXT, snapshot_date DATE)")
    rows = [("us", "timer", str(i), f"Big Timer {i}", None, i, "2026-11-05") for i in range(1, 4)]
    con.executemany("INSERT INTO sr_latest VALUES (?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE app_relevance (store TEXT, keyword TEXT, app_id TEXT, relevant BOOLEAN)")   # old shape
    con.execute("INSERT INTO app_relevance VALUES ('us','timer','1',false)")
    out = R.relevance(con, "us", "2026-11-05")
    assert out[("timer", "1")] == 0.0 and "timer" in R.JUDGED     # rule says yes, names says no, no description: names
    assert out[("timer", "2")] == 1.0                                # not judged: word rule
    con.execute("ALTER TABLE app_relevance ADD COLUMN relevant_manual BOOLEAN")
    con.execute("UPDATE app_relevance SET relevant_manual=true")
    assert R.relevance(con, "us", "2026-11-05")[("timer", "1")] == 1.0   # manual wins
    con.execute("ALTER TABLE app_relevance ADD COLUMN relevant_sonnet BOOLEAN")
    con.execute("UPDATE app_relevance SET relevant_sonnet=false")
    assert R.relevance(con, "us", "2026-11-05")[("timer", "1")] == 1.0   # manual beats Sonnet
    con.execute("UPDATE app_relevance SET relevant_manual=NULL")
    assert R.relevance(con, "us", "2026-11-05")[("timer", "1")] == 0.0   # Sonnet beats rule and names


def test_adult_intent_rule():
    from apptopics.metrics.adult import is_adult
    assert is_adult("uncensored ai chat", 0.4)            # word cue + Jev unsure -> adult
    assert is_adult("sex dice", 0.84)
    assert is_adult("anonymous chat for adults", 0.8)     # Jev sure on its own
    assert not is_adult("quit porn blocker", 0.59)        # Jev unsure, no cue ("porn" followed by blocker)
    assert not is_adult("ai baby maker", 0.56)
    assert not is_adult("sex offender map", 0.1)          # cue, but Jev says no
    assert not is_adult("brain games for adults", 0.2)
    assert not is_adult("anything", None)
