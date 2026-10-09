"""Deterministic checks applied to every LLM answer.

The LLM is instructed to stay grounded, but instructions are not guarantees. These checks are:
1. Number grounding: every price-like figure in the answer must literally exist in the passages the answer
   cites (or in the user's own message, so the bot can say "the $50 you mention is not correct").
2. Citation repair/validation: [n] markers must point at retrieved passages; URLs are attached from those
   passages server-side, so the model can never invent a link.
"""
from __future__ import annotations

import re

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫", "01234567890123456789,.")
_NUM_RE = re.compile(r"\d{1,3}(?:[,.    ]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,2})?(?!\d)")
_CURRENCY_RE = re.compile(r"(\$|usd|us\$|€|eur|£|gbp|dollars?|d[oó]lares|دولار|euros?)", re.I)
_CITE_RE = re.compile(r"\[(\d{1,2})\]")


def _to_value(token: str) -> float | None:
    token = token.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    # A separator followed by exactly three digits is a thousands separator (18,995 / 18.995 / 18 995).
    token = re.sub(r"[,. ](?=\d{3}(?:\D|$))", "", token)
    token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def money_values(text: str, only_pricey: bool = True) -> set[float]:
    """Numbers in `text`. With only_pricey, keep figures >= 100 or written next to a currency marker."""
    text = _CITE_RE.sub(" ", text.translate(_DIGIT_MAP))  # "[2]" is a citation, not a figure
    values: set[float] = set()
    for m in _NUM_RE.finditer(text):
        value = _to_value(m.group())
        if value is None:
            continue
        if only_pricey:
            window = text[max(0, m.start() - 6): m.end() + 9]
            if value < 100 and not _CURRENCY_RE.search(window):
                continue
        values.add(value)
    return values


def cited_numbers(answer: str, sources: list[int], n_passages: int) -> list[int]:
    """Union of the `sources` field and inline [n] markers, restricted to valid passage numbers."""
    nums = set(sources) | {int(x) for x in _CITE_RE.findall(answer)}
    return sorted(n for n in nums if 1 <= n <= n_passages)


def strip_invalid_citations(answer: str, n_passages: int) -> str:
    return _CITE_RE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_passages else "", answer)


def check_numbers(answer: str, cited: list[int], passages: list[str], user_message: str) -> tuple[list[int], set[float]]:
    """Returns (possibly repaired citation list, ungrounded values).

    If a figure is missing from the cited passages but present in another retrieved passage, that passage is
    added to the citations instead of failing the answer (the model just cited the wrong block).
    """
    answer_vals = money_values(answer)
    if not answer_vals:
        return cited, set()
    allowed = set().union(*(money_values(passages[i - 1], only_pricey=False) for i in cited)) if cited else set()
    allowed |= money_values(user_message, only_pricey=False)
    missing = answer_vals - allowed
    repaired = list(cited)
    for value in sorted(missing):
        for i, text in enumerate(passages, start=1):
            if value in money_values(text, only_pricey=False):
                if i not in repaired:
                    repaired.append(i)
                missing = missing - {value}
                break
    return sorted(repaired), missing


def renumber_citations(answer: str, mapping: dict[int, int]) -> str:
    """Rewrite passage numbers to source numbers; drop uncited markers and collapse repeats like [1][1]."""
    answer = _CITE_RE.sub(lambda m: f"[{mapping[int(m.group(1))]}]" if int(m.group(1)) in mapping else "", answer)
    answer = re.sub(r"(\[\d+\])(\s*\1)+", r"\1", answer)
    return re.sub(r" +([.,;:])", r"\1", answer)
