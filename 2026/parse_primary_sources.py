#!/usr/bin/env python3
"""
Parse the non-PDF 2026 Missouri primary precinct sources into OpenElections CSVs.

Inputs live in sources/2026/primary/ (downloaded from openelections-sources-mo;
git-ignored — they belong in the separate openelections-sources-mo repo):
  * Boone  -- wide-matrix CSV  (3-row header: office / party / candidate)
  * Clay   -- nested JSON      (results.ballotItems -> ballotOptions -> precinctResults)
  * Lincoln-- multi-sheet XLSX  (one sheet per contest + Registered Voters sheet)

"St Clair MO Official Results 08.04.26.json" is intentionally NOT parsed: it is
St. Clair County, *Michigan* (township government, MI districts), mislabeled in the
sources repo.

Outputs (OpenElections precinct schema from issue #53):
    county,precinct,office,district,party,candidate,votes
written to 2026/counties/20260804__mo__primary__{county}__precinct.csv

Run:  uv run 2026/parse_primary_sources.py
"""
import csv
import json
import os
import re
import sys

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
# The canonical sources live in the openelections-sources-mo repo (also checked
# out at ~/code/openelections-sources-mo); sources/2026/primary here is a stale
# snapshot kept for local convenience.
_SOURCES_REPO_DIR = os.path.join(
    os.path.dirname(ROOT), "openelections-sources-mo", "2026", "primary")
LOCAL_DIR = os.path.join(ROOT, "sources", "2026", "primary")
SRC_DIR = _SOURCES_REPO_DIR if os.path.isdir(_SOURCES_REPO_DIR) else LOCAL_DIR
OUT_DIR = os.path.join(ROOT, "2026", "counties")
ELECTION = "20260804__mo__primary"

HEADER = ["county", "precinct", "office", "district", "party", "candidate", "votes"]

# ---------------------------------------------------------------------------
# Party normalization
# ---------------------------------------------------------------------------

_PARTY_MAP = {
    "REPUBLICAN": "REP", "REP": "REP", "REPUBLICAN PARTY": "REP",
    "DEMOCRAT": "DEM", "DEMOCRATIC": "DEM", "DEM": "DEM",
    "DEMOCRATIC PARTY": "DEM",
    "LIBERTARIAN": "LIB", "LIB": "LIB", "LIBERTARIAN PARTY": "LIB",
    "GREEN": "GRE", "GREEN PARTY": "GRE",
    "INDEPENDENT": "IND",
    "NONPARTISAN": "", "NON PARTISAN": "", "NON": "",
    "WRITE-IN": "WI", "WRITEIN": "WI", "WI": "WI",
}


def normalize_party(raw):
    if raw is None:
        return ""
    return _PARTY_MAP.get(str(raw).strip().upper(), str(raw).strip())


# ---------------------------------------------------------------------------
# Candidate name casing
# ---------------------------------------------------------------------------

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "VI"}


def titlecase_name(s):
    """Title-case an (often ALL-CAPS) candidate name, preserving Roman-numeral
    and Jr./Sr. suffixes. Good enough for low-hanging fruit; not a full name-caser."""
    s = (s or "").strip()
    if not s:
        return s
    out = []
    for tok in s.split(" "):
        if not tok:
            out.append("")
            continue
        bare = tok.rstrip(".,")
        if bare.upper() in _SUFFIXES:
            if bare.upper() in {"II", "III", "IV", "V", "VI"}:
                out.append(bare.upper())
            else:
                out.append(bare.capitalize() + ("." if tok.endswith(".") else ""))
        else:
            out.append(tok.title())
    s = " ".join(out)
    # fix ordinals produced by .title(): "1St" -> "1st", "2Nd" -> "2nd", ...
    return re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b",
                  lambda m: m.group(1) + m.group(2).lower(), s)


def clean_candidate(raw, is_ballot_measure=False):
    if is_ballot_measure:
        u = (raw or "").strip().upper()
        if u in ("YES", "NO"):
            return u.capitalize()
        return (raw or "").strip()
    if re.search(r"write[\s-]*in", (raw or ""), re.I):
        return "Write-Ins"
    return titlecase_name(raw)


# ---------------------------------------------------------------------------
# Office / district normalization
# ---------------------------------------------------------------------------

_PREFIX_WORDS = (
    "NONPARTISAN", "NON PARTISAN", "REPUBLICAN", "DEMOCRATIC", "DEMOCRAT",
    "LIBERTARIAN", "INDEPENDENT", "GREEN", "NON", "REP", "DEM", "LIB",
)


def _strip_prefixes(s):
    for _ in range(4):
        s = re.sub(r"^FOR\s+", "", s, flags=re.I)
        s = re.sub(
            r"^(" + "|".join(re.escape(w) for w in _PREFIX_WORDS) + r")\s+",
            "", s, flags=re.I,
        )
    s = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", s)  # trailing "( 1)" vote-for marker
    return s.strip()


def _district_num(text):
    m = re.search(r"(\d+)", text)
    return m.group(1) if m else ""


def _title(s):
    # Title-case a fallback office name; collapse runs of whitespace.
    s = _strip_prefixes(s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return s
    SMALL = {"of", "the", "and", "for", "in", "on", "at", "to"}
    # title-case each token, but preserve short all-caps abbreviations
    # (e.g. "PR"/"SN" committee labels) instead of lowercasing them to "Pr".
    # Limited to 2-letter tokens so real 3-letter words ("TAX", "AMD") still
    # title-case normally.
    def _case(tok):
        if tok.isupper() and len(tok) == 2 and tok.isalpha():
            return tok
        return tok.title()

    parts = [_case(tok) for tok in s.split(" ")]
    out = [parts[0]]
    for tok in parts[1:]:
        out.append(tok.lower() if tok.lower() in SMALL else tok)
    s = " ".join(out)
    # fix ordinals produced by .title(): "1St" -> "1st", "2Nd" -> "2nd", ...
    s = re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b",
               lambda m: m.group(1) + m.group(2).lower(), s)
    return s


def normalize_office(raw):
    """Return (office, district). district is '' except for legislative offices."""
    s = _strip_prefixes(raw)
    u = s.upper()

    # Ballot measures
    m = re.search(r"CONSTITUTIONAL AMENDMENT (?:NO\.?\s*)?(\d+)", u)
    if m:
        return (f"Constitutional Amendment {m.group(1)}", "")
    m = re.match(r"PROPOSITION\s+(.+)", u)
    if m:
        return ("Proposition " + _title(m.group(1)), "")
    m = re.match(r"QUESTION\s+NO\.?\s*(\d+)\s+(.+)", u)
    if m:
        return (f"Question {m.group(1)} {_title(m.group(2))}", "")
    if re.match(r"QUESTION\s+NO\.?\s*(\d+)", u):
        d = _district_num(u)
        return (f"Question {d}", "")
    # bare "QUESTION <subject>" (keyword at start, no number)
    m = re.match(r"QUESTION\s+(.+)", u)
    if m:
        return ("Question: " + _title(m.group(1)), "")
    # "<title> QUESTION" / "<title> PROPOSITION" (keyword at the end) -- local
    # ballot questions whose office line is the question's title.
    m = re.match(r"(.+?)\s+QUESTION$", u)
    if m:
        return ("Question: " + _title(m.group(1)), "")
    m = re.match(r"(.+?)\s+PROPOSITION$", u)
    if m:
        return ("Proposition: " + _title(m.group(1)), "")

    # Federal / statewide
    if re.search(r"U\.?\s*S\.?\s*SENATOR", u) or "UNITED STATES SENATOR" in u:
        return ("U.S. Senate", "")
    m = re.search(r"U\.?\s*S\.?\s*REPRESENTATIVE.*?DIST(?:RICT)?\.?\s*(\d+)", u)
    if m:
        return ("U.S. House", m.group(1))
    m = re.search(r"CONGRESS.*?(\d+)\w*\s*DISTRICT", u)  # "Representative in Congress 9th District"
    if m:
        return ("U.S. House", m.group(1))
    m = re.search(r"STATE SEN(?:ATOR|ATE).*?DIST(?:RICT)?\.?\s*(\d+)", u)
    if m:
        return ("State Senate", m.group(1))
    m = re.search(r"STATE SEN(?:ATOR|ATE).*?(\d+)\w*\s*DISTRICT", u)  # "32nd District"
    if m:
        return ("State Senate", m.group(1))
    m = re.search(r"STATE REPRESENTATIVE.*?DIST(?:RICT)?\.?\s*(\d+)", u)
    if m:
        return ("State House", m.group(1))
    m = re.search(r"STATE REPRESENTATIVE.*?(\d+)\w*\s*DISTRICT", u)  # "127th District"
    if m:
        return ("State House", m.group(1))
    m = re.search(r"STATE LEGISLATURE.*?(\d+)\w*\s*DISTRICT", u)
    if m:
        return ("State House", m.group(1))
    if "STATE AUDITOR" in u:
        return ("State Auditor", "")
    if "LIEUTENANT GOVERNOR" in u:
        return ("Lieutenant Governor", "")
    if "GOVERNOR" in u:
        return ("Governor", "")
    if "ATTORNEY GENERAL" in u:
        return ("Attorney General", "")
    if "SECRETARY OF STATE" in u:
        return ("Secretary of State", "")
    if "STATE TREASURER" in u:
        return ("State Treasurer", "")

    # Judicial -- circuit/division stays in the office name, district col empty.
    m = re.search(
        r"ASSOCIATE CIRCUIT JUDGE.*?CIRCUIT\s*(\d+).*?DIV(?:ISION)?\.?\s*([0-9IVX]+)", u)
    if m:
        return (f"Associate Circuit Judge - Circuit {m.group(1)} Division {m.group(2)}", "")
    m = re.search(r"ASSOCIATE CIRCUIT JUDGE.*?DIV(?:ISION)?\.?\s*([0-9IVX]+)", u)
    if m:
        return (f"Associate Circuit Judge Division {m.group(1)}", "")
    m = re.search(r"CIRCUIT JUDGE.*?CIRCUIT\s*(\d+)(?:.*?DIV(?:ISION)?\.?\s*([0-9IVX]+))?", u)
    if m:
        div = f" Division {m.group(2)}" if m.group(2) else ""
        return (f"Circuit Judge - Circuit {m.group(1)}{div}", "")

    # County / local offices: cleaned title-case fallback.
    return (_title(s), "")


def is_ballot_measure(office):
    return bool(re.match(r"(Constitutional Amendment|Proposition|Question)\b", office))


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return path


def county_slug(name):
    # repo convention: dots dropped, spaces underscored ("st_francois")
    return re.sub(r"[\s.]+", "_", name.strip()).strip("_").lower()


# ---------------------------------------------------------------------------
# Boone CSV (wide matrix)
# ---------------------------------------------------------------------------

def parse_boone(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    office_row, party_row, cand_row = rows[0], rows[1], rows[2]

    # Locate the meta columns by header text.
    def find(colname):
        for i, h in enumerate(office_row):
            if h and h.strip().upper() == colname:
                return i
        return None

    rv_col = find("REGISTERED VOTERS TOTAL")
    bc_col = find("BALLOTS CAST TOTAL")
    bc_rep = find("BALLOTS CAST - REPUBLICAN PARTY")
    bc_dem = find("BALLOTS CAST - DEMOCRATIC PARTY")
    bc_lib = find("BALLOTS CAST - LIBERTARIAN PARTY")
    name_col = find("PRECINCT NAME")

    # Candidate columns: from the first candidate office onward to end.
    first_cand = None
    for i, h in enumerate(office_row):
        if h and re.search(r"FOR\s|AMENDMENT|PROPOSITION|COUNCIL|COMMISSIONER|CLERK|AUDITOR|"
                           r"COLLECTOR|RECORDER|PROSECUTING|JUDGE", h, re.I) \
                and i not in (rv_col, bc_col, bc_rep, bc_dem, bc_lib, name_col, 0, 1):
            first_cand = i
            break
    if first_cand is None:
        first_cand = 9  # observed fallback

    out = []
    for r in rows[3:]:
        precinct = r[name_col].strip() if name_col is not None and name_col < len(r) else ""
        if not precinct:
            continue
        # Registered Voters / Ballots Cast
        def v(idx):
            return int(r[idx]) if idx is not None and idx < len(r) and r[idx].strip().isdigit() else 0
        if rv_col is not None:
            out.append(["Boone", precinct, "Registered Voters", "", "", "", v(rv_col)])
        if bc_col is not None:
            out.append(["Boone", precinct, "Ballots Cast", "", "", "", v(bc_col)])
        if bc_rep is not None:
            out.append(["Boone", precinct, "Ballots Cast", "", "REP", "", v(bc_rep)])
        if bc_dem is not None:
            out.append(["Boone", precinct, "Ballots Cast", "", "DEM", "", v(bc_dem)])
        if bc_lib is not None:
            out.append(["Boone", precinct, "Ballots Cast", "", "LIB", "", v(bc_lib)])
        # Candidate columns
        for i in range(first_cand, len(cand_row)):
            cand = cand_row[i].strip() if i < len(cand_row) else ""
            if not cand:
                continue
            office_raw = office_row[i].strip() if i < len(office_row) else ""
            if not office_raw:
                continue
            office, district = normalize_office(office_raw)
            measure = is_ballot_measure(office)
            party = "" if measure else normalize_party(party_row[i] if i < len(party_row) else "")
            candidate = clean_candidate(cand, is_ballot_measure=measure)
            if not measure and re.search(r"write[\s-]*in", cand, re.I):
                party = "WI"
            val = r[i].strip() if i < len(r) else ""
            votes = int(val) if val.isdigit() else 0
            if votes == 0 and measure is False and candidate == "Write-Ins":
                # keep write-in rows even at 0? drop to reduce noise
                continue
            out.append(["Boone", precinct, office, district, party, candidate, votes])
    return out


# ---------------------------------------------------------------------------
# Clay JSON
# ---------------------------------------------------------------------------

def parse_clay(path):
    d = json.load(open(path))
    out = []
    checksum = []  # (contest, candidate, sum_precincts, reported_total)
    for item in d["results"]["ballotItems"]:
        office, district = normalize_office(item["name"])
        measure = is_ballot_measure(office)
        for opt in item["ballotOptions"]:
            candidate = clean_candidate(opt["name"], is_ballot_measure=measure)
            if measure:
                party = ""
            elif re.search(r"write[\s-]*in", opt.get("name", ""), re.I):
                party = "WI"
            else:
                party = normalize_party(opt.get("politicalParty"))
            pr = opt.get("precinctResults") or []
            total = 0
            for p in pr:
                votes = int(p.get("voteCount") or 0)
                total += votes
                out.append(["Clay", p["name"], office, district, party, candidate, votes])
            checksum.append((office, candidate, total, int(opt.get("voteCount") or 0)))
    return out, checksum


# ---------------------------------------------------------------------------
# Lincoln XLSX
# ---------------------------------------------------------------------------

def build_candidate_party_map(boone_rows, clay_rows):
    """candidate(lower) -> party code, from the explicitly-labeled sources."""
    m = {}
    for row in boone_rows + clay_rows:
        county, precinct, office, district, party, candidate, votes = row
        if candidate and candidate != "Write-Ins" and party:
            m.setdefault(candidate.lower(), party)
    return m


def parse_lincoln(path, cand_party_map):
    wb = openpyxl.load_workbook(path, data_only=True)

    # Table of Contents -> ordered contest sheets (by page number).
    toc = []
    for row in wb["Table of Contents"].iter_rows(values_only=True):
        if isinstance(row[0], int) and row[1]:
            toc.append((str(row[0]), row[1].strip()))

    out = []
    current_party = ""  # block inheritance for county-only contest sheets
    for sheet_name, _title in sorted(toc, key=lambda t: int(t[0])):
        if sheet_name == "1":  # "Registered Voters" handled separately
            continue
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        title = (rows[0][0] or "").strip()
        office, district = normalize_office(title)
        measure = is_ballot_measure(office)
        cand_row = rows[1] if len(rows) > 1 else ()

        # Candidate names live at the first column of each 4-col block starting at col 2.
        candidates = []  # (name, total_votes_col)
        for c, cell in enumerate(cand_row):
            if c < 2:
                continue
            if isinstance(cell, str) and cell.strip() and cell.strip().lower() != "total":
                candidates.append((cell.strip(), c + 3))

        # Determine this sheet's party.
        if measure:
            party = ""
        else:
            found = {cand_party_map[name.lower()]
                      for name, _ in candidates if name.lower() in cand_party_map}
            if found and len(found) == 1:
                party = next(iter(found))
                current_party = party
            elif found:
                # disagreement -- trust the majority, else inherit
                party = max(found, key=lambda p: sum(1 for n, _ in candidates
                            if cand_party_map.get(n.lower()) == p))
                current_party = party
            else:
                party = current_party  # county-only sheet: inherit block party

        for r in rows[3:]:
            precinct = (r[0] or "").strip() if r and r[0] else ""
            if not precinct or precinct.upper() == "TOTAL":
                continue
            for name, vcol in candidates:
                val = r[vcol] if vcol < len(r) else None
                votes = int(val) if isinstance(val, (int, float)) or (isinstance(val, str) and val.strip().isdigit()) else 0
                cand = clean_candidate(name, is_ballot_measure=measure)
                p = "WI" if (not measure and re.search(r"write[\s-]*in", name, re.I)) else party
                out.append(["Lincoln", precinct, office, district, p, cand, votes])

    # Registered Voters sheet.
    if "Registered Voters" in wb.sheetnames:
        ws = wb["Registered Voters"]
        for r in ws.iter_rows(min_row=2, values_only=True):
            precinct = (r[0] or "").strip() if r and r[0] else ""
            if not precinct or precinct.upper() in ("TOTAL",):
                continue
            rv = r[1] if len(r) > 1 else None
            bc = r[2] if len(r) > 2 else None
            if isinstance(rv, (int, float)):
                out.append(["Lincoln", precinct, "Registered Voters", "", "", "", int(rv)])
            if isinstance(bc, (int, float)):
                out.append(["Lincoln", precinct, "Ballots Cast", "", "", "", int(bc)])
    return out


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------

def verify_clay(checksum):
    bad = 0
    for office, candidate, summed, reported in checksum:
        if summed != reported:
            bad += 1
            print(f"  CLAY MISMATCH {office}/{candidate}: precincts sum {summed} != reported {reported}")
    return bad


def verify_duplicates(rows, county):
    seen = {}
    dups = 0
    for row in rows:
        key = tuple(row[i] for i in (0, 1, 2, 3, 4, 5))
        if key in seen:
            dups += 1
        seen[key] = True
    if dups:
        print(f"  {county} DUPLICATES: {dups}")
    return dups


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Parsing Boone CSV ...")
    boone = parse_boone(os.path.join(SRC_DIR, "Boone MO 20260804PPR.csv"))
    verify_duplicates(boone, "Boone")

    print("Parsing Clay JSON ...")
    clay, clay_checksum = parse_clay(os.path.join(SRC_DIR, "Clay MO Official Results 08.04.26.json"))
    verify_duplicates(clay, "Clay")
    clay_bad = verify_clay(clay_checksum)

    print("Parsing Lincoln XLSX ...")
    cand_map = build_candidate_party_map(boone, clay)
    lincoln = parse_lincoln(
        os.path.join(SRC_DIR, "Lincoln MO Precinct Detail Official 08.04.26.xlsx"), cand_map)
    verify_duplicates(lincoln, "Lincoln")

    outputs = {
        "boone": boone,
        "clay": clay,
        "lincoln": lincoln,
    }
    for county, rows in outputs.items():
        path = os.path.join(OUT_DIR, f"{ELECTION}__{county_slug(county)}__precinct.csv")
        write_csv(path, rows)
        print(f"  wrote {path}  ({len(rows)} rows)")

    # Per-county contest/candidate totals summary.
    for county, rows in outputs.items():
        from collections import defaultdict
        totals = defaultdict(int)
        for r in rows:
            totals[(r[2], r[3], r[4], r[5])] += int(r[6])
        print(f"\n{county.title()} totals ({len(totals)} office/candidate combos):")
        for k in sorted(totals):
            print(f"    {k} = {totals[k]}")

    if clay_bad:
        print(f"\nWARNING: {clay_bad} Clay checksum mismatch(es)")
    print("\nDone.")


if __name__ == "__main__":
    main()