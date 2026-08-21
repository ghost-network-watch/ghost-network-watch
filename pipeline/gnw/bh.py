"""Behavioral-health classifier.

Two tiers, per scoping/evidence/bh_filter_v0.json (vendored in gnw/data/):

  string tier   directory `specialty` free text -> include/exclude lists built
                from every observed value in the scoping corpus. A CANDIDATE
                screen only — issuers' vocabularies are chaos (conditions as
                specialties, 2-letter codes, empty strings).
  taxonomy tier the AUTHORITATIVE check: NPPES NUCC taxonomy codes. Include
                prefixes from the spec, minus the 14 neurology/sleep/pain
                codes in the 2084 family that a bare prefix would over-capture.

`classify_record` fuses both; scoring should treat taxonomy as authoritative
and string-only matches as one confidence notch lower.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_SPEC_PATH = Path(__file__).parent / "data" / "bh_filter_v0.json"

# Regex tier for strings never seen in the scoping corpus — deliberately
# marks candidates as AMBIGUOUS, never as definite BH (spec step 7/8).
_UNSEEN_RE = re.compile(
    r"(?<!develop)mental|psych|behavior|counsel|social work|addiction|substance"
    r"|\btherapist\b|marriage|lcsw|lmft|lpc|lmhc|lcmhc|licsw|lmsw|lgsw",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _spec() -> dict:
    return json.loads(_SPEC_PATH.read_text())


@lru_cache(maxsize=1)
def _string_sets() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    spec = _spec()

    def norm_set(entries) -> frozenset[str]:
        return frozenset(_norm_one(e["value"]) for e in entries)

    return norm_set(spec["include"]), norm_set(spec["exclude"]), norm_set(spec["ambiguous"])


@lru_cache(maxsize=1)
def _taxonomy_rules() -> tuple[tuple[str, ...], frozenset[str], frozenset[str]]:
    """(safe_prefixes, explicit_include_codes, exclude_codes).

    The spec's include_code_prefixes mixes literal prefixes with descriptive
    rule keys whose VALUES enumerate exact codes ("2084P0800X Psychiatry").
    Literal-prefix keys are all-alnum and short; everything else contributes
    its enumerated codes.
    """
    cross = _spec()["nucc_taxonomy_crosscheck"]
    prefixes: list[str] = []
    codes: set[str] = set()
    for key, value in cross["include_code_prefixes"].items():
        if re.fullmatch(r"[0-9]{3}[0-9A-Z]", key):  # literal 4-char prefix (101Y, 1041, ...)
            prefixes.append(key)
        elif re.fullmatch(r"[0-9A-Z]{10}", key):  # full code as key (363LP0808X)
            codes.add(key)
        else:  # descriptive rule: extract enumerated codes from the value(s)
            values = value if isinstance(value, list) else [value]
            for v in values:
                codes.update(re.findall(r"\b\d{3}[0-9A-Z]{2}\d{4}X\b|\b\d{9}X\b", v))
    # BH-flavored code inside the excluded OT family (spec critical_gotchas).
    codes.add("225XM0800X")
    # Clinical-nurse-specialist psych prefix enumerated as 364SP08xx.
    prefixes.append("364SP08")
    excludes = frozenset(cross["2084_exclude_codes"])
    return tuple(prefixes), frozenset(codes), excludes


def _norm_one(value: str) -> str:
    s = html.unescape(value)
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(";.,: ")
    return s.casefold()


def normalize_specialty(raw: str) -> list[str]:
    """Spec normalization; multi-value strings split BEFORE matching."""
    return [_norm_one(p) for p in re.split(r"[|;]", raw) if _norm_one(p)]


def classify_specialty(raw: str | None) -> str:
    """'bh' | 'not_bh' | 'ambiguous' | 'unknown' for a directory specialty string."""
    if not raw:
        return "unknown"
    include, exclude, ambiguous = _string_sets()
    verdicts = set()
    for part in normalize_specialty(raw):
        if part in include:
            verdicts.add("bh")
        elif part in exclude:
            verdicts.add("not_bh")
        elif part in ambiguous:
            verdicts.add("ambiguous")
        elif _UNSEEN_RE.search(part):
            verdicts.add("ambiguous")  # unseen strings never auto-include
        else:
            verdicts.add("unknown")
    for v in ("bh", "ambiguous", "not_bh"):  # any BH part makes the record a candidate
        if v in verdicts:
            return v
    return "unknown"


def is_bh_taxonomy(code: str | None) -> bool:
    if not code:
        return False
    prefixes, codes, excludes = _taxonomy_rules()
    code = code.strip().upper()
    if code in excludes:
        return False
    return code in codes or code.startswith(prefixes)


def any_bh_taxonomy(taxonomies: str | None, sep: str = "|") -> bool:
    if not taxonomies:
        return False
    return any(is_bh_taxonomy(c) for c in taxonomies.split(sep))


def classify_record(specialties: str | None, taxonomies: str | None) -> str:
    """Fused verdict with provenance:
    'bh_taxonomy' > 'bh_string' > 'ambiguous' > 'not_bh' > 'unknown'.
    """
    if any_bh_taxonomy(taxonomies):
        return "bh_taxonomy"
    string_verdict = classify_specialty(specialties)
    if string_verdict == "bh":
        return "bh_string"
    return string_verdict
