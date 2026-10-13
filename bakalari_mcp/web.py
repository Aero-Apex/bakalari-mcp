"""Readers for the Bakaláři *website* pages (used when logging in with a cookie).

Each parser turns a page into the same dictionary shape the REST API returns, so the formatters in
`formatters.py` work unchanged for both ways of logging in.
"""

from __future__ import annotations

import json
import re
from html import unescape

MARKS_PAGE = "/next/prubzna.aspx"

_ATTR = re.compile(r"""data-clasif=(?:'([^']*)'|"([^"]*)")""")
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_AVERAGE = re.compile(r"""class=["']modra-z["'][^>]*>\s*([^<]*?)\s*<""", re.S)
_BLOCK = re.compile(r"""<div[^>]*class=["'][^"']*(?<![\w-])predmet-radek(?![\w-])""")


def _clean(text: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _iso_date(czech: str) -> str:
    """'7.10.2026' -> '2026-10-07T00:00:00' ('' when not a date)."""
    m = re.fullmatch(r"\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*", czech or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}T00:00:00" if m else ""


def parse_marks_page(html: str) -> dict:
    """Current marks (průběžná klasifikace) as {'Subjects': [{'Subject', 'AverageText', 'Marks': [...]}]}.

    Every grade on the page carries a JSON blob (data-clasif) with caption, mark, weight, date, class rank...
    """
    starts = [m.start() for m in _BLOCK.finditer(html)]
    subjects = []
    for i, start in enumerate(starts):
        block = html[start: starts[i + 1] if i + 1 < len(starts) else len(html)]
        name_match = _H3.search(block)
        name = _clean(name_match.group(1)) if name_match else "?"
        average = _AVERAGE.search(block)
        marks = []
        for single, double in _ATTR.findall(block):
            try:
                raw = json.loads(unescape(single or double))
            except ValueError:
                continue
            points = (raw.get("PointsText") or "").strip()
            marks.append({
                "Id": raw.get("id"),
                "MarkDate": _iso_date(raw.get("strdatum") or raw.get("udel_datum") or ""),
                "Caption": raw.get("caption") or "",
                "Theme": raw.get("tema") or raw.get("Theme") or "",
                "MarkText": (raw.get("MarkText") or "").strip(),
                "Weight": raw.get("vaha"),
                "TypeNote": raw.get("oznaceni") or "",
                "IsNew": bool(raw.get("IsNew")),
                "IsPoints": bool(points),
                "PointsText": points,
                "MaxPoints": raw.get("bodymax") or "",
                "Note": _clean(raw.get("poznamkakzobrazeni") or raw.get("VerbalEvaluation") or ""),
                "ClassRank": raw.get("strporadivetrideuplne") or "",
                "Unconfirmed": bool(raw.get("Unconfirmed")),
            })
        subjects.append({
            "Subject": {"Name": name, "Abbrev": ""},
            "AverageText": average.group(1) if average else "",
            "Marks": marks,
        })
    return {"Subjects": subjects}
