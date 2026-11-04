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
