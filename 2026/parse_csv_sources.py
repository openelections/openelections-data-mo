#!/usr/bin/env python3
"""
Parse the CSV 2026 Missouri primary precinct sources into OpenElections CSVs.

  * Stone         -- long-format CSV (precinct, contest, candidate, votes)
  * St Louis County -- wide CSV (one row per precinct/contest/choice, party
                       splits in "X Ballots Cast" columns; prefixed '#' header)

Outputs (OpenElections precinct schema):
    county,precinct,office,district,party,candidate,votes
written to 2026/counties/20260804__mo__primary__{county}__precinct.csv

Run:  uv run 2026/parse_csv_sources.py
"""
import csv
import io
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_primary_sources import (  # noqa: E402
    SRC_DIR,
    ELECTION,
    HEADER,
    _title,
    clean_candidate,
    is_ballot_measure,
    normalize_office,
    titlecase_name,
    write_csv,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "2026", "counties")


def county_slug(name):
    """Repo convention: dots dropped, spaces underscored, lowercased
    (e.g. 'st_louis' vs 'st_louis_city')."""
    return re.sub(r"[\s.]+", "_", name.strip()).strip("_").lower()

BALLOT_STYLE_COLS = ["REP", "DEM", "LIB", "GRN", "NON"]

_LOCAL_PLACE_RE = re.compile(r"^(?:CITY|VILLAGE|TOWN) OF\s+(.+?)\s*[-–]\s*(.*)$")
_STATE_PREFIX_RE = re.compile(r"^STATE\s*[-–]\s*(.*)$")
_PROPOSITION_RE = re.compile(r"^PROPOSITION\s+(.+)$", re.I)


def normalize_local(contest):
    """Normalize a municipal/district question title.
    'CITY OF BERKELEY - PROPOSITION U' -> ('Berkeley Proposition U', ''):
    a '<place> <measure>' name, matching the 'Columbia Proposition 1' style
    already used in the Jackson CSVs.  Returns office string or None."""
    m = _LOCAL_PLACE_RE.match(contest)
    if not m:
        return None
    place = strip_party_suffix(m.group(1))
    rest = m.group(2).strip()
    pm = _PROPOSITION_RE.match(rest)
    if pm:
        return f"{place} Proposition {pm.group(1).title()}"
    return f"{place} {rest.title()}"


def _int(s):
    s = str(s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else 0


# ---------------------------------------------------------------------------
# Stone -- long format
# ---------------------------------------------------------------------------

def _stone_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = [ln for ln in f]
    # title lines ("Official Election Results" / county) precede the real header
    hdr_i = next(i for i, ln in enumerate(lines) if ln.startswith("Precinct"))
    return [r for r in csv.DictReader(lines[hdr_i:]) if r.get("Precinct")]


def build_row(county, precinct, contest, cand_raw, votes, cand_party_map):
    """Standard row for a long-format source (Stone)."""
    local = normalize_local(contest)
    if local is not None:
        office, district = local, ""
    else:
        office, district = normalize_office(contest)
    measure = is_ballot_measure(office)
    if measure:
        return [county, precinct, office, district, "",
                clean_candidate(cand_raw, is_ballot_measure=True), votes]
    if re.search(r"write[\s-]*in", cand_raw, re.I):
        return [county, precinct, office, district, "WI", "Write-Ins", votes]
    candidate = titlecase_name(cand_raw)
    party = cand_party_map.get((office.lower(), candidate.lower()), "")
    return [county, precinct, office, district, party, candidate, votes]


def party_from_contest(contest, default):
    """Party code from a contest-title suffix like '... - REPUBLICAN'."""
    m = re.search(r"\b(REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN)\s*$", contest, re.I)
    if not m:
        return default
    return {"REPUBLICAN": "REP", "DEMOCRATIC": "DEM",
            "LIBERTARIAN": "LIB", "GREEN": "GRE"}[m.group(1).upper()]


def strip_party_suffix(contest):
    return re.sub(r"\s*[-–]\s*(REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN)\s*$",
                  "", contest, flags=re.I).strip()


# ---------------------------------------------------------------------------
# St Louis County -- wide format
# ---------------------------------------------------------------------------

def parse_stl_county(path, cand_party_map):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows_in = list(csv.DictReader(
            (ln.lstrip("#").lstrip() for ln in f), ))
    out = []
    meta_emitted = set()
    for r in rows_in:
        precinct = (r.get("#Precinct") or r.get("Precinct") or "").strip()
        if not precinct:
            continue
        contest = (r["Contest Title"] or "").strip()
        cand_raw = (r["Choice Name"] or "").strip()
        votes = _int(r.get("Total Votes"))
        local_office = normalize_local(contest)
        if local_office is not None:
            office, district = local_office, ""
        else:
            office, district = normalize_office(strip_party_suffix(contest))
        measure = is_ballot_measure(office)
        if re.search(r"write[\s-]*in", cand_raw, re.I):
            cand, p = "Write-Ins", "WI"
        else:
            cand = clean_candidate(cand_raw, is_ballot_measure=measure)
            p = ""
            if not measure:
                p = (r.get("Contest Party") or "").strip().upper()
                p = {"GRN": "GRE", "NON": ""}.get(p, p)
                if p == "":
                    p = party_from_contest(contest, "") or \
                        cand_party_map.get((office.lower(), cand.lower()), "")
        out.append(["St. Louis", precinct, office, district, p, cand, votes])

        # per-precinct turnout meta (RV + ballots cast by style), first row only
        key = precinct
        if key not in meta_emitted:
            meta_emitted.add(key)
            rv = _int(r["Registered Voters"])
            if rv:
                out.append(["St. Louis", precinct, "Registered Voters",
                            "", "", "", rv])
            for col, code in zip(BALLOT_STYLE_COLS, BALLOT_STYLE_COLS):
                n = _int(r.get(f"{col} Ballots Cast"))
                if n:
                    # NON keeps its label: mapping it to '' collided with the
                    # party-less total row below in the duplicate-entries test
                    party_code = {"GRN": "GRE"}.get(col, col)
                    out.append(["St. Louis", precinct, "Ballots Cast",
                                "", party_code, "", n])
            bc = (sum(_int(r.get(f"{c} Ballots Cast")) for c in BALLOT_STYLE_COLS))
            if bc:
                out.append(["St. Louis", precinct, "Ballots Cast", "", "", "", bc])
    return out


# ---------------------------------------------------------------------------
# Verification: precinct sums vs the certified county-totals file
# ---------------------------------------------------------------------------

def load_county_totals(path):
    """(county, office, district, party, candidate) -> votes, plus a
    (office, candidate) -> party map learned from statewide rows (used to
    backfill party for sources that carry no per-candidate party)."""
    totals = defaultdict(int)
    cand_party = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = (_norm_county(r["county"]), r["office"].lower(),
                   r["district"], r["party"], r["candidate"].lower())
            totals[key] += _int(r["votes"])
            ck = (r["office"].lower(), r["candidate"].lower())
            if r["party"]:
                cand_party.setdefault(ck, r["party"])
    return totals, cand_party


def _norm_county(c):
    return {"De Kalb": "DeKalb", "Ste. Genevieve": "Ste. Genevieve",
            "Kansas City": "Kansas City"}.get(c, c)


def crosscheck(rows, county, certified):
    """Sum rows per (office,district,party,candidate) and compare to certified
    county totals where the candidate appears in certified file."""
    summed = defaultdict(int)
    for r in rows:
        summed[(r[2].lower(), r[3], r[4], r[5].lower())] += _int(r[6])
    matched = bad = 0
    msgs = []
    for key, total in summed.items():
        ck = (county, key[0], key[1], key[2], key[3])
        if ck not in certified:
            msgs.append(f"  [?] {county} not in certified: {key}")
            continue
        matched += 1
        if certified[ck] != total:
            bad += 1
            msgs.append(f"  [X] {county} {key}: precincts {total} != certified "
                        f"{certified[ck]}")
    msgs.append(f"  [=] {county}: {matched} candidates matched to certified "
                f"totals, {bad} mismatch(es)")
    return bad, msgs


def main():
    certified, cand_party_map = load_county_totals(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "2026", "20260804__mo__primary__county.csv"))

    outputs = {}
    print("Parsing Stone CSV ...")
    stone = [build_row("Stone", r["Precinct"], r["Contest Name"],
                       r["Candidate Name"], _int(r["Votes"]), cand_party_map)
             for r in _stone_rows(os.path.join(
                 SRC_DIR, "Stone MO Official Results by Precinct 08.04.26.csv"))]
    outputs["Stone"] = stone

    print("Parsing St Louis County CSV ...")
    stl = parse_stl_county(os.path.join(
        SRC_DIR, "St Louis County MO Precinct Results Official 08.04.26.csv"),
        cand_party_map)
    outputs["St. Louis"] = stl

    report = []
    mismatch = 0
    for county, rows in outputs.items():
        bad, msgs = crosscheck(rows, county, certified)
        report += msgs
        mismatch += bad
        path = os.path.join(
            OUT_DIR, f"{ELECTION}__{county_slug(county)}__precinct.csv")
        write_csv(path, rows)
        print(f"  wrote {path}  ({len(rows)} rows)")

    print("\n".join(report))
    if mismatch:
        print(f"\nWARNING: {mismatch} crosscheck mismatch(es)")


if __name__ == "__main__":
    main()