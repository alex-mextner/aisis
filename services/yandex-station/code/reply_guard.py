"""Last line of defence against the brain's «not heard» habits.

The persona asks for «Повтори?» or one short question, but the brain still sometimes answers
«Урок из пяти чего: минут, упражнений, слов? Не расслышал, уточни.» (owner, tg#28376). tidy()
removes the «не расслышал, уточни» tail and collapses an option list after a colon, so the
speaker says at most one short question.
"""
from __future__ import annotations

import re

NOT_HEARD_REPLY = "Повтори?"
_TAIL = re.compile(r"\s*(?:извини[,!.]?\s*)?не\s+(?:расслыша\w*|понял\w*)[\s,.!—-]*"
                   r"(?:уточни\w*|повтори\w*|скажи\s+ещё\s+раз|можешь\s+повторить)[^.?!]*[.?!]*\s*$", re.IGNORECASE)
# «Урок из пяти чего: минут, упражнений, слов?»: a question whose colon opens a list of guesses.
_OPTION_LIST = re.compile(r"^(?P<head>[^:?!.]{3,}):\s*(?P<items>[^?!.]*(?:,|\sили\s)[^?!.]*)\?\s*$")


def tidy(reply: str) -> str:
    """`reply` without the not-heard tail and without a trailing option list; «Повтори?» if nothing is left."""
    text = _TAIL.sub("", reply.strip()).strip()
    sentences = re.split(r"(?<=[.?!])\s+", text)
    if sentences and sentences[-1].endswith("?"):
        listed = _OPTION_LIST.match(sentences[-1])
        if listed:
            sentences[-1] = listed.group("head").strip() + "?"
            text = " ".join(sentences)
    return text or NOT_HEARD_REPLY
