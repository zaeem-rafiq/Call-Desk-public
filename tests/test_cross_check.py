"""After the verdict, Snowflake's agent and Nimble's check move the confidence score (app.confidence.cross_check)."""

from app import confidence
from app.models import Flag, SnowflakeAgent, WebDesk, WebVerify


def _flag(direction="cut", conf=0.92, sf=None, change=None, trust="high"):
    f = Flag(id="f1", call_ts="2026-10-08T08:20:18-04:00", t_offset=147.0, speaker="CFO", quote="q", claim_type="guidance",
             metric="core_eps_growth", metric_label="Core EPS growth", direction=direction, confidence=conf,
             confidence_rationale="0.92 = prior 0.45×1.0 + web 0.35×1.0 + price 0.20×0.6")
    if sf:
        f.snowflake_agent = SnowflakeAgent(status="done", verdict=sf)
    if change:
        f.web_desk = WebDesk(verify=WebVerify(confidence=trust, answer={"change": change}))
    return f


def test_both_agents_agreeing_with_a_cut_raise_confidence_but_never_to_certainty():
    f = _flag(sf="NEW", change="cut")
    assert confidence.cross_check(f)
    assert f.confidence_base == 0.92 and f.confidence == 0.98  # 0.92 + 0.03 + 0.03
    assert [c.result for c in f.cross_checks] == ["agrees", "agrees"]
    assert "cross-checks: Snowflake agent agrees (+0.03), Nimble check agrees (+0.03) → 0.98" in f.confidence_rationale
    f2 = _flag(conf=0.97, sf="NEW", change="cut")
    confidence.cross_check(f2)
    assert f2.confidence == 0.98  # capped below 1.0


def test_a_disagreeing_agent_lowers_confidence_and_the_card_says_why():
    f = _flag(direction="reaffirm", conf=0.81, sf="CONSISTENT", change="cut")
    confidence.cross_check(f)
    assert f.confidence == round(0.81 + 0.03 - 0.10, 2)
    nimble = f.cross_checks[1]
    assert nimble.result == "disagrees" and "calls it a cut, not a reaffirm" in nimble.reason


def test_snowflake_reading_a_cut_as_unchanged_is_disagreement():
    f = _flag(sf="CONSISTENT")
    confidence.cross_check(f)
    assert f.confidence == 0.82 and f.cross_checks[0].result == "disagrees"


def test_no_signal_leaves_the_score_alone():
    for f in (_flag(sf="UNVERIFIABLE"), _flag(change="cut", trust="low"), _flag(direction="n/a", sf="NEW", change="cut")):
        assert not confidence.cross_check(f)
        assert f.confidence in (0.92,) and f.cross_checks == [] and f.confidence_base is None


def test_recheck_is_idempotent_and_recomputes_from_the_verdict_score():
    f = _flag(sf="NEW")
    assert confidence.cross_check(f) and f.confidence == 0.95
    assert not confidence.cross_check(f)  # nothing new: no republish
    f.web_desk = WebDesk(verify=WebVerify(confidence="medium", answer={"change": "cut"}))
    assert confidence.cross_check(f)
    assert f.confidence == round(0.92 + 0.03 + 0.015, 2) and f.confidence_base == 0.92
    assert f.confidence_rationale.count("cross-checks") == 1


def test_nimble_phrases_are_read_as_a_change_word():
    f = _flag(sf="NEW", change="Revised down from 5%-7% to 2.5%-3.5%")
    confidence.cross_check(f)
    assert [c.result for c in f.cross_checks] == ["agrees", "agrees"] and f.confidence == 0.98
    assert confidence._change_word("Narrowed to approximately 3%") == "reaffirm"
    assert confidence._change_word("Raised to 2.2x") == "raise"
    assert confidence._change_word("") == ""
