"""Adult-intent searches are labelled rubbish (Step 1, decided 2026-11-06).

Rule for a new search: rubbish when (an adult word cue is in the term AND Jev's adult probability >= 0.3) OR Jev's
adult probability >= 0.75 alone. Jev alone is noisy in the middle ('quit porn blocker', 'ai baby maker' score 0.5-0.6),
the word cue alone is too broad ('sex offender map', 'brain games for adults'); together they caught 77 of 79 adult
searches in a manual review of the 310 Jev flagged (data/cache/manual_adult_2026-11-06.json) with 1 wrong flag.
Tuned on that same set, so expect a little worse on new searches. The manual answers are stored with model='manual'
and are never overwritten by a relabel.
"""
from __future__ import annotations

import re

JEV_WITH_CUE = 0.3
JEV_ALONE = 0.75

CUE = re.compile(
    r"uncensor|unfilter|unrestrict|\bno filters?\b|\bno restrictions?\b|\bno limits\b|without (filters?|restrictions?|limits|boundaries)"
    r"|\bnsfw\b|\bsex\b|\bspicy\b|\b18\b|18\+"
    r"|\b(chat|dating|stickers?|emoji|truth or dare|would you rather|couples?)\b.*\badults?\b|\badults?\b.*\b(dating|chat)\b"
    r"|clothes eraser|eraser\b.*\bclothes|remove censor|\bnude|\bnaked|\bporn\b(?!.*block)|sugar daddy"
    r"|video (calls?|chat) (with )?(women|girls)|\bai\b.*\bhot\b|\bhot\b.*\b(video|photo)")

QUESTION = ("Is the person looking for sexual, nude or pornographic content, a tool that undresses people or removes clothes "
            "from photos, 'uncensored' / NSFW image or chat generators, hookups or sex chat, or other adults-only (18+) "
            "content? No for health, pregnancy, periods, sex education, ordinary dating apps, swimwear or lingerie shopping, "
            "and body or photo editing without nudity.")


def is_adult(term: str, p_jev: float | None) -> bool:
    if p_jev is None:
        return False
    return p_jev >= JEV_ALONE or (p_jev >= JEV_WITH_CUE and bool(CUE.search(term.lower())))
