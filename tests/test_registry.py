import pytest

from apptopics.registry import Registry


def test_register_and_duplicate():
    r = Registry("x")

    @r.register("a")
    class A: ...
    assert r["a"] is A
    with pytest.raises(ValueError):
        r.register("a")(A)


def test_builtin_metrics_loaded():
    from apptopics.metrics import metrics
    for name in ["demand", "brandness", "competition", "difficulty", "momentum", "monetization", "play_demand", "llm_appeal"]:
        assert name in metrics
