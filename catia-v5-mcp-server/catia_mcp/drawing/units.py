"""Recognize drawing unit evidence without treating the preposition 'in' as inches."""

import re


def drawing_units(text: str) -> set[str]:
    units = set()
    if (re.search(r"\b(?:mm|millim(?:et|it)(?:er|re)s?)\b", text, re.IGNORECASE)
            or re.search(r"\b(?:all)?dimensions(?:are)?inmillim(?:et|it)(?:er|re)s?\b", text, re.IGNORECASE)):
        units.add("mm")
    if (re.search(r"\binch(?:es)?\b", text, re.IGNORECASE)
            or re.search(r"\bunits?\s*[:=]\s*in\b", text, re.IGNORECASE)):
        units.add("inch")
    return units


def unit_evidence_matches(text: str, factor: float) -> bool:
    expected = "mm" if factor == 1.0 else "inch"
    return drawing_units(text) == {expected}
