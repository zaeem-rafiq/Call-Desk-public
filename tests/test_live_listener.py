"""Sentence tracking for the Gemini Live listener (no network)."""

from app.live_listener import HISTORY_S, _complete_sentences, _norm, _seen, _Tracker


def test_complete_sentences_keeps_the_unfinished_tail():
    done, tail = _complete_sentences("It's the margin piece driving the guide down in EPS. Uh but sales as you")
    assert done == ["It's the margin piece driving the guide down in EPS."] and tail == "Uh but sales as you"


def test_revisions_and_merged_finals_are_not_resent():
    sent = [_norm("It's the margin piece that's driving the guide down in EPS."), _norm("Uh but sales as you mentioned we expect to be robust.")]
    assert _seen("it's the margin piece that's driving the guide down in EPS", sent)  # revision
    assert _seen("So it's the margin piece that's that's driving the guide down in EPS, but sales as you mentioned we expect to be robust.", sent)
    assert not _seen("We're also identifying structural cost reduction areas.", sent)


def test_a_sentence_with_new_numbers_is_never_a_repeat():
    sent = [_norm("For the fourth quarter, we expect earnings of $1.15 to $1.65 per share and an operating margin of 7% to 9%.")]
    assert not _seen("For the full year, we now expect earnings of $5.10 to $5.60 per share.", sent)
    assert _seen("For the fourth quarter we expect earnings of $1.15 to $1.65 per share", sent)


def test_decimals_never_end_a_sentence_and_no_text_is_dropped():
    # Delta run 20261011-123244 showed "72 per share." and "Pre-tax profit of 1." on screen.
    text = "Earnings were $1.72 per share. Operating margin was 11.4%. Pre-tax profit of $1."
    done, tail = _complete_sentences(text)
    assert done == ["Earnings were $1.72 per share.", "Operating margin was 11.4%."]
    assert tail == "Pre-tax profit of $1."  # may still become "$1.6 billion"
    assert _complete_sentences(text, settled=True) == (done + ["Pre-tax profit of $1."], "")


def test_abbreviations_do_not_end_a_sentence():
    done, tail = _complete_sentences("Delta's U.S. network grew 3%, e.g. in Atlanta, Mr. Bastian said. Next")
    assert done == ["Delta's U.S. network grew 3%, e.g. in Atlanta, Mr. Bastian said."] and tail == "Next"


def test_a_number_at_the_end_waits_for_its_decimals():
    t = _Tracker()
    assert t.update("Pre-tax profit of $1.", False, 0) == []
    assert t.update("Pre-tax profit of $1.6 billion. That", False, 1) == ["Pre-tax profit of $1.6 billion."]
    t = _Tracker()
    assert t.update("Free cash flow was $1.9.", False, 0) == []
    assert t.update("Free cash flow was $1.9.", False, 1) == ["Free cash flow was $1.9."]  # the speaker paused


def test_a_period_revised_to_a_comma_sends_only_the_new_words():
    t = _Tracker()
    first = "During the quarter, we further expanded self-service options in the Fly Delta app."
    assert t.update(first + " Customer", False, 0) == [first]
    revised = ("During the quarter, we further expanded self-service options in the Fly Delta app and completed "
               "the rollout of Delta Concierge to all SkyMiles members. Customer adoption")
    assert t.update(revised, False, 1) == ["and completed the rollout of Delta Concierge to all SkyMiles members."]


def test_a_late_final_does_not_resend_old_sentences():
    # The same run re-sent Dan Janki's 10:16:05 line at 10:17:46, 30 sentences later, and made a duplicate flag.
    t = _Tracker()
    dan = "And over the next several years, we are positioned to more than double MRO revenue while expanding margins."
    assert t.update(dan + " Now I'd like", False, 0) == [dan]
    for i in range(30):
        assert t.update(f"Fuel, capacity and cost item {i} moved during the quarter. Next", False, 1 + i)
    handover = "Now I'd like to turn it over to Erik to cover our financial performance."
    assert t.update(dan.replace("MRO", "MR") + " " + handover, True, 100) == [handover]
    assert t.update(dan, True, 100 + HISTORY_S + 1) == [dan]  # a genuine repeat much later is new again


def test_a_short_line_said_again_minutes_later_is_kept():
    t = _Tracker()
    assert t.update("Thank you. Next", False, 0) == ["Thank you."]
    for i in range(30):
        t.update(f"Fuel, capacity and cost item {i} moved during the quarter. Next", False, 1 + i)
    assert t.update("Thank you. Our", False, 200) == ["Thank you."]


# The cases below are Gemini Live updates captured on the Delta clip (Oct 11 run, 347 updates).

def test_sentences_glued_without_a_space_are_split():
    done, tail = _complete_sentences("Customer adoption is scaling rapidly.Our operational expertise creates value. I'm")
    assert done == ["Customer adoption is scaling rapidly.", "Our operational expertise creates value."] and tail == "I'm"


def test_glued_interim_words_still_match_the_spaced_final():
    sent = [_norm("Our outlook includes a refinerybenefit of more than $700million, underscoring its uniquevalue.")]
    assert _seen("Our outlook includes a refinery benefit of more than $700 million, underscoring its unique value.", sent)


def test_a_final_that_runs_two_sent_sentences_together_sends_nothing():
    t = _Tracker()
    proud = "I'm proud of the Tech Ops team for delivering a billion dollars of maintenance, repair, and overhaul revenue year to date."
    rise = "An increase of nearly 60% over last year while expanding margins and growing our customer base."
    assert t.update(f"{proud} {rise} Next", False, 0) == [proud, rise]
    for i in range(10):
        t.update(f"Fuel, capacity and cost item {i} moved during the quarter. Next", False, 1 + i)
    merged = proud[:-1] + ", an increase of nearly 60% over last year, while expanding margins and growing our customer base."
    assert t.update(merged, True, 90) == []


def test_a_final_that_restores_a_lost_opening_sends_nothing():
    t = _Tracker()
    short = "We expect non-fuel unit cost growth to improve 1 to 2 points sequentially."
    assert t.update(short + " Next", False, 0) == [short]
    full = ("With slightly higher capacity and continued operational efficiencies, "
            "we expect non-fuel unit cost growth to improve one to two points sequentially.")
    assert t.update(full, True, 1) == []


def test_a_final_that_extends_a_glued_interim_sentence_sends_only_the_new_words():
    t = _Tracker()
    glued = "Delivering this level of performance despite aroughly $6 billion increase in fuelexpenses this year reflectsthe power of Delta's strategicadvantages."
    assert t.update(glued + " Next", False, 0) == [glued]
    final = ("Delivering this level of performance, despite a roughly $6 billion increase in fuel expenses this year, "
             "reflects the power of Delta's strategic advantages and differentiated from the industry.")
    assert t.update(final, True, 1) == ["and differentiated from the industry."]
