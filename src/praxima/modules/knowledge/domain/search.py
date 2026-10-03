"""Turn a caller-style question into a keyword query: meaningful words, any of them.

Questions are full of filler ("are you open on Sunday?"); requiring every word would find
almost nothing. Filler words (English, Hindi, Hinglish) and honorifics are dropped with the
same tokenizer the voice agent uses, and the rest are OR-ed as prefixes so "sunday" also
matches "sundays". Ranking (ts_rank) puts passages matching more of the words first.
"""

from praxima.shared.kernel.text import tokens

MAX_TERMS = 12
# Characters with meaning in tsquery syntax; stripped so a question can never be an operator.
_TSQUERY_SYNTAX = str.maketrans("", "", "&|!():*<>'\\\"")


def search_terms(question: str) -> list[str]:
    """Distinct meaningful words, in order, safe to use as tsquery lexemes."""
    terms: list[str] = []
    for token in tokens(question):
        term = token.translate(_TSQUERY_SYNTAX)
        if len(term) > 1 and term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def tsquery_text(terms: list[str]) -> str:
    """`'open':* | 'sunday':*` for to_tsquery('simple', ...)."""
    return " | ".join(f"'{term}':*" for term in terms)
