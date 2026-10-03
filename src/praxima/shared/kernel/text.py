"""Text normalization and search tokens: pure, shared by the backend and the voice agent."""

import unicodedata


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in value
        ).split()
    )


_STOPWORDS = frozenset(
    """a an and are as at be by do does for from has have how i in is it its me my of on or our
    tell that the their there they this to was we what when where which who why will with you your
    ka ke ki ko kya kaun kaha kab hai hain ho me mein se aur ya bhi ye yeh wo woh
    का के की को क्या कौन कहाँ कब है हैं हो में से और या भी यह वह""".split()
)
_TITLES = frozenset("dr drs doctor prof professor mr mrs ms डॉ डा डॉक्टर श्री श्रीमती".split())


def tokens(text: str) -> list[str]:
    # normalize() already separates words and keeps combining marks, so Devanagari words
    # survive intact; a letter-class regex would split them at every matra.
    return [
        t
        for t in normalize(text).split()
        if t not in _STOPWORDS and t not in _TITLES and len(t) > 1
    ]
