from apptopics.pipeline.market import crowd_band, price_summary
from apptopics.sources.appstore_page import parse_iap, period_of

PAGE = ('<h2>In-App Purchases</h2><ul><li><div class="text-pair svelte-x"><span>Premium Monthly</span> <span>$9.99</span> </div></li>'
        '<li><div class="text-pair svelte-x"><span>Pro &amp; Coach Yearly</span> <span>$1,039.99</span> </div></li>'
        '<li><div class="text-pair svelte-x"><span>Weekly</span> <span>$2.99</span></div></li></ul>')


def test_parse_iap_and_periods():
    r = parse_iap(PAGE)
    assert [(x["item"], x["price_usd"]) for x in r] == [("Premium Monthly", 9.99), ("Pro & Coach Yearly", 1039.99), ("Weekly", 2.99)]
    assert [period_of(x["item"], x["price_usd"]) for x in r] == ["month", "year", "week"]
    assert period_of("Lifetime Unlock", 49.99) == "lifetime" and period_of("Gems pack", 1.99) == "other"
    assert parse_iap("<html>no purchases here</html>") == []


def test_crowd_bands():
    assert crowd_band(2, 0, 0, None)[0] == "Empty"
    assert crowd_band(20, 6, 1, 2.0)[0] == "Emerging"
    assert crowd_band(30, 15, 1, 2.0)[0] == "Crowded"
    assert crowd_band(30, 20, 4, 0.4)[0] == "Saturated"
    assert 0 <= crowd_band(30, 20, 4, 0.4)[1] <= 100


def test_price_summary_suggests_medians():
    p = price_summary({"a": {"month": [9.99], "year": [39.99]}, "b": {"month": [4.99], "year": [29.99]}, "c": {"month": [6.99], "year": [49.99]}}, 3)
    assert p["periods"]["month"]["median"] == 6.99 and p["periods"]["year"]["median"] == 39.99
    assert p["suggested"]["monthly"] == 6.99 and p["suggested"]["yearly"] == 39.99 and "weekly" not in p["suggested"]
    assert price_summary({}, 0)["suggested"]["reason"].startswith("not enough")
