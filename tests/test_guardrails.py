from app.chat.guardrails import (check_numbers, cited_numbers, money_values, renumber_citations,
                                 strip_invalid_citations)

CTX = [
    "Program Type: Out-Patient Program | Duration: 3 Weeks | Total Cost (USD): $18,995",
    "Program Type: In-Patient Program | Duration: 3 Weeks | Total Cost (USD): $30,000",
]


def test_number_formats_normalise():
    assert money_values("It costs $18,995 USD") == {18995.0}
    assert money_values("cuesta 18.995 USD") == {18995.0}
    assert money_values("التكلفة ١٨٬٩٩٥ دولار") == {18995.0}
    assert money_values("$18,995 [2]") == {18995.0}       # citation marker is not a number
    assert money_values("24/7 care for 3 weeks") == set()  # small non-currency numbers ignored


def test_grounded_answer_passes():
    cited, bad = check_numbers("The out-patient program costs $18,995 [1].", [1], CTX, "how much?")
    assert bad == set() and cited == [1]


def test_invented_price_is_caught():
    _, bad = check_numbers("It costs $12,000 [1].", [1], CTX, "how much?")
    assert bad == {12000.0}


def test_user_claimed_price_can_be_quoted_back_but_not_invented():
    _, bad = check_numbers("No, it is not $50; it is $18,995 [1].", [1], CTX, "I heard it costs $50. Confirm that.")
    assert bad == set()
    _, bad = check_numbers("It is $50 [1].", [1], CTX, "how much is it?")
    assert bad == {50.0}


def test_wrong_citation_is_repaired():
    cited, bad = check_numbers("In-patient costs $30,000 [1].", [1], CTX, "q")
    assert bad == set() and cited == [1, 2]


def test_citation_helpers():
    assert cited_numbers("a [1] b [9]", [2], n_passages=3) == [1, 2]
    assert strip_invalid_citations("a [1] b [9]", 3) == "a [1] b "
    assert renumber_citations("A [3][5] x [5]. B [7] .", {3: 1, 5: 1, 7: 2}) == "A [1] x [1]. B [2]."
