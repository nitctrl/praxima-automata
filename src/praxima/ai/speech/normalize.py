"""English speech normalization; phone digits stay separate and dates unambiguous."""

import re
from datetime import date

SMALL = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()


def number(value: int) -> str:
    if value < 20:
        return SMALL[value]
    if value < 100:
        return TENS[value // 10] + (" " + SMALL[value % 10] if value % 10 else "")
    if value < 1000:
        return SMALL[value // 100] + " hundred" + (" " + number(value % 100) if value % 100 else "")
    if value < 1_000_000:
        remainder = " " + number(value % 1000) if value % 1000 else ""
        return number(value // 1000) + " thousand" + remainder
    return " ".join(SMALL[int(c)] for c in str(value))


def english_speech(text: str) -> str:
    def clock(found: re.Match[str]) -> str:
        hour, minute = int(found[1]), int(found[2])
        if hour > 23 or minute > 59:
            return found[0]
        minutes = " " + ("oh " if minute < 10 else "") + number(minute) if minute else ""
        return number(hour % 12 or 12) + minutes + (" a.m." if hour < 12 else " p.m.")

    def calendar(found: re.Match[str]) -> str:
        try:
            day = date.fromisoformat(found[0])
        except ValueError:
            return found[0]
        return f"{day.strftime('%B')} {number(day.day)}, {number(day.year)}"

    text = re.sub(
        r"\+[1-9][0-9]{7,14}\b",
        lambda m: "plus " + " ".join(SMALL[int(c)] for c in m[0][1:]), text,
    )
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", calendar, text)
    text = re.sub(r"(\d{1,2}:\d{2})\s*[–—-]\s*(?=\d{1,2}:\d{2})", r"\1 to ", text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, text)
    text = text.replace("Asia/Kolkata", "India Standard Time")
    text = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: m[0].replace(",", ""), text)
    text = re.sub(r"\b(\d+)\.00\b", r"\1", text)
    text = re.sub(
        r"\b(\d+)\.(\d+)\b",
        lambda m: m[1] + " point " + " ".join(SMALL[int(c)] for c in m[2]), text,
    )
    text = re.sub(
        r"\b\d+\b",
        lambda m: number(int(m[0])) if len(m[0]) <= 6 else " ".join(SMALL[int(c)] for c in m[0]),
        text,
    )
    return text.replace("INR", "rupees")