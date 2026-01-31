import re

_ABBREVIATIONS = [
    (r"\bst\b", "street"),
    (r"\bave\b", "avenue"),
    (r"\bblvd\b", "boulevard"),
    (r"\bdr\b", "drive"),
    (r"\brd\b", "road"),
    (r"\bln\b", "lane"),
    (r"\bct\b", "court"),
    (r"\bpl\b", "place"),
    (r"\bapt\b", "#"),
    (r"\bunit\b", "#"),
    (r"\bste\b", "#"),
]


def normalize_address(raw: str) -> str:
    if not raw:
        return ""
    result = raw.lower().strip()
    for pattern, replacement in _ABBREVIATIONS:
        result = re.sub(pattern, replacement, result)
    result = re.sub(r"\s+", " ", result).strip()
    return result
