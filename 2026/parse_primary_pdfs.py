#!/usr/bin/env python3
"""
Parse the electronic (text-layer) 2026 Missouri primary precinct PDFs into
OpenElections CSVs, using natural-pdf.

Inputs live in sources/2026/primary/ (git-ignored; they belong in the separate
openelections-sources-mo repo). Only PDFs that (a) have a text layer and (b) contain
per-precinct breakdowns are parsed. County-summary PDFs and image-only scans are
skipped (see CLAUDE.md / the plan).

Three source format families are handled:
  * St. Louis City -- clean per-precinct blocks (one precinct spanning several pages)
  * MO SOS "Statement of Votes Cast" SOVC matrix -- Jackson, Stoddard, Scott, Christian
        (candidates as columns, precincts as rows; headers reconstructed from
        positioned text elements)
  * "by precinct" SOVC -- Jasper (reversed-char headers, ruling lines), Nodaway

Outputs (OpenElections precinct schema, issue #53):
    county,precinct,office,district,party,candidate,votes
written to 2026/counties/20260804__mo__primary__{county}__precinct.csv

Run:  uv run 2026/parse_primary_pdfs.py
"""
import os
import re
import sys

from natural_pdf import PDF

# Reuse the shared normalizer / writers from the non-PDF parser (same dir).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_primary_sources  # noqa: E402
from parse_primary_sources import (  # noqa: E402
    HEADER,
    clean_candidate,
    county_slug,
    is_ballot_measure,
    normalize_office,
    normalize_party,
    titlecase_name,
    write_csv,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
# Same SRC_DIR resolution as parse_primary_sources.py (which we import from).
SRC_DIR = parse_primary_sources.SRC_DIR
OUT_DIR = os.path.join(ROOT, "2026", "counties")
ELECTION = "20260804__mo__primary"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Contest -> ordered candidate names, learned from the cleanest sources and used
# to validate/correct noisy SOVC headers.  Only statewide / federal /
# constitutional contests share candidates across counties, so the map is only
# ever consulted or populated for those -- county-specific contests (County
# Clerk, Commissioner, Committeeman, ...) are reconstructed fresh per county.
KNOWN_CANDIDATES = {}  # contest_key -> [(candidate, party), ...]

# Offices whose candidates are the same in every county (cross-referenced via
# KNOWN_CANDIDATES).  Everything else is county-specific.
_SHARED_OFFICE_PREFIXES = (
    "U.S. Senate", "U.S. House", "Governor", "Lieutenant Governor",
    "Attorney General", "Secretary of State", "State Treasurer",
    "State Auditor", "State Senate", "State House",
    "Constitutional Amendment", "Proposition", "Question",
)


def _is_shared_contest(office):
    """True for statewide/federal/constitutional offices whose candidates are
    shared across counties (and thus cross-referenced via KNOWN_CANDIDATES)."""
    return any(office.startswith(p) for p in _SHARED_OFFICE_PREFIXES)


def _int(s):
    """Parse an integer that may contain commas; return 0 on failure."""
    if s is None:
        return 0
    s = str(s).strip().replace(",", "")
    return int(s) if s.isdigit() else 0


def _is_num(tok):
    return bool(re.fullmatch(r"\d[\d,]*", tok or ""))


def _is_pct(tok):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?%", tok or ""))


def _slug_candidates(s):
    """Normalize a candidate name to a lowercased comparison key."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------------------
# Format 1 -- St. Louis City per-precinct blocks
# ---------------------------------------------------------------------------

# Precinct IDs may be alphanumeric (e.g. "Ward 09 Precinct 04A").
_PRECINCT_RE = re.compile(
    r"^Ward\s+(\d+)\s+Precinct\s+([0-9A-Za-z]+)\s+(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+registered voters"
)

# Contest header suffixes.  " - <label>" where label is a party name or Nonpartisan.
_PARTY_LABELS = {
    "REPUBLICAN PARTY": "REP",
    "DEMOCRATIC PARTY": "DEM",
    "LIBERTARIAN PARTY": "LIB",
    "GREEN PARTY": "GRE",
    "CONSTITUTION PARTY": "CON",
    "NONPARTISAN": "",
    "NON-PARTISAN": "",
}


def _stl_party_and_office(line):
    """Split a St. Louis City contest-header line into (office_raw, party).

    Headers look like 'STATE AUDITOR - Republican Party' or
    'CONSTITUTIONAL AMENDMENT NO. 5 - Nonpartisan'. Returns (office_raw, party)
    or None if the line is not a contest header."""
    if " - " not in line:
        return None
    left, right = line.split(" - ", 1)
    right_u = right.strip().upper()
    # reject the 'Choice Party ...' column-header line and candidate rows
    if "%" in line or left.upper().startswith("CHOICE"):
        return None
    if right_u in _PARTY_LABELS:
        return left.strip(), _PARTY_LABELS[right_u]
    return None


def parse_st_louis_city(path):
    """Parse the St. Louis City per-precinct results PDF."""
    pdf = PDF(path)
    rows = []
    current_precinct = None
    current_office = None
    current_district = ""
    current_party = ""
    current_measure = False
    contest_totals = {}  # (office,district,party,candidate) -> votes, for checksum
    emitted_meta = set()  # precincts we've already emitted RV/BC for

    def add(office, district, party, candidate, votes):
        rows.append(["St. Louis City", current_precinct, office, district,
                     party, candidate, votes])
        contest_totals[(office, district, party, candidate)] = \
            contest_totals.get((office, district, party, candidate), 0) + int(votes)

    for page in pdf.pages:
        text = page.extract_text() or ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue

            # precinct header
            m = _PRECINCT_RE.match(line)
            if m:
                ward, prec, ballots, reg = m.group(1), m.group(2), m.group(3), m.group(4)
                current_precinct = f"Ward {ward} Precinct {prec}"
                if current_precinct not in emitted_meta:
                    add("Registered Voters", "", "", "", _int(reg))
                    add("Ballots Cast", "", "", "", _int(ballots))
                    emitted_meta.add(current_precinct)
                continue

            # contest header
            hp = _stl_party_and_office(line)
            if hp:
                office_raw, current_party = hp
                current_office, current_district = normalize_office(office_raw)
                current_measure = is_ballot_measure(current_office)
                # record known candidates for cross-county reference (populated
                # from candidate rows below; the first precinct seen anchors order)
                continue

            # skip boilerplate / summary rows
            if (line.startswith(("Choice", "Cast Votes:", "Undervotes:", "Overvotes:",
                                 "Absentee Voting", "Precinct Results", "City of",
                                 "Registered Voters", "Polling Places", "Election Day",
                                 "Run Time", "Run Date", "8/4/2026", "Page "))):
                continue
            # any leftover precinct-header shape (defensive) is not a candidate row
            if line.startswith("Ward "):
                continue
            if "%" not in line:
                continue
            if current_office is None:
                continue

            # candidate row: leading name tokens, then (vote, pct) pairs.
            toks = line.split()
            # find where numbers begin
            split_i = len(toks)
            for i, t in enumerate(toks):
                if _is_num(t) or _is_pct(t):
                    split_i = i
                    break
            name_toks = toks[:split_i]
            num_toks = toks[split_i:]
            if not name_toks:
                continue
            # votes = last non-% number (the Total column)
            vote_vals = [t for t in num_toks if _is_num(t)]
            votes = _int(vote_vals[-1]) if vote_vals else 0

            raw_name = " ".join(name_toks)
            # Ballot-measure options are Yes/No. Detect by first token so this
            # works even when the office name isn't a Proposition/Amendment keyword
            # (e.g. the "CWE SOUTH SBD" subdistrict measure).
            first = name_toks[0].upper().rstrip(".")
            if first in ("YES", "NO"):
                candidate = "Yes" if first == "YES" else "No"
                party = ""
            elif re.search(r"write[\s-]*in", raw_name, re.I):
                candidate = "Write-Ins"
                party = current_party  # contest party, like real candidates
            elif current_measure:
                candidate = raw_name
                party = ""
            else:
                candidate = titlecase_name(raw_name)
                party = current_party
            add(current_office, current_district, party, candidate, votes)

            # learn known candidates for shared (statewide/federal/constitutional)
            # contests only; city-specific contests stay out of the shared map.
            if _is_shared_contest(current_office):
                key = (current_office, current_district, current_party)
                if key not in KNOWN_CANDIDATES:
                    KNOWN_CANDIDATES[key] = []
                kc = KNOWN_CANDIDATES[key]
                cmp_name = _slug_candidates(candidate)
                if cmp_name and not any(_slug_candidates(c) == cmp_name for c, _ in kc):
                    kc.append((candidate, party))

    return rows, contest_totals


# ---------------------------------------------------------------------------
# Format 2 -- MO SOS "Statement of Votes Cast" SOVC matrix
#   (Jackson x3, Stoddard, Scott, Christian)
# ---------------------------------------------------------------------------

# Party line, e.g. "(REPUBLICAN PARTY)" or "(DEMOCRATIC)".
_SOVC_PARTY_RE = re.compile(
    r"^\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN|CONSTITUTION|NONPARTISAN)"
    r"(?:\s+PARTY)?\)$"
)
_NUM_RE = re.compile(r"^-?\d[\d,]*$")


def _is_int_tok(t):
    return bool(_NUM_RE.match(t or ""))


def _cluster_by_x0(els, tol=15.0):
    """Group (x0, top, text) tuples into columns by x0 (consecutive gap <= tol)."""
    els = sorted(els, key=lambda e: e[0])
    groups, cur, last = [], [], None
    for e in els:
        if last is None or e[0] - last <= tol:
            cur.append(e)
        else:
            groups.append(cur)
            cur = [e]
        last = e[0]
    if cur:
        groups.append(cur)
    return groups


def _reconstruct_name(col_els, band=2.0):
    """Concatenate a column's elements into a candidate name.

    Elements in the same top-band are char-split pieces of one word, so they are
    joined *without* spaces (sorted by x0: 'G'+'E'+'R'+'A'+'L'+'D' -> 'GERALD').
    Separate bands are distinct words (first name / last name rows) and are
    joined with spaces."""
    col_els = sorted(col_els, key=lambda e: (e[1], e[0]))
    bands, cur, cur_top = [], [], None
    for e in col_els:
        if cur_top is None or e[1] - cur_top <= band:
            cur.append(e)
            if cur_top is None:
                cur_top = e[1]
        else:
            bands.append(cur)
            cur, cur_top = [e], e[1]
    if cur:
        bands.append(cur)
    words = []
    for b in bands:
        b.sort(key=lambda e: e[0])
        w = "".join(e[2] for e in b)
        if w:
            words.append(w)
    return " ".join(words).strip()


def _name_tokens(s):
    return set(_slug_candidates(t) for t in (s or "").split() if _slug_candidates(t))


def _match_known(col_names, known):
    """Greedily match reconstructed column names to known candidate names by
    token overlap. Returns a list (one known name per column) or None if any
    column can't be matched confidently."""
    known_tokens = [(kn, _name_tokens(kn)) for kn in known]
    used = set()
    result = [None] * len(col_names)
    # score every (col, known) pair, assign strongest first
    pairs = []
    for ci, cn in enumerate(col_names):
        ct = _name_tokens(cn)
        for ki, (kn, kt) in enumerate(known_tokens):
            overlap = len(ct & kt)
            if overlap:
                pairs.append((overlap, ci, ki))
    pairs.sort(reverse=True)
    for overlap, ci, ki in pairs:
        if result[ci] is not None or ki in used:
            continue
        result[ci] = known_tokens[ki][0]
        used.add(ki)
    if any(r is None for r in result):
        return None
    return result


# ---------------------------------------------------------------------------
# SOVC data-row helpers.
#   The candidate *header* is reconstructed from positioned text elements
#   (vertically wrapped / char-scattered).  The *data* rows are also read from
#   elements: in some SOVC PDFs a vote count is char-split across neighbouring
#   elements ("344" -> "3"+"44"), while in others a row that extract_text garbles
#   (Stoddard p19 DEXTER) has perfectly clean elements.  Elements handle both:
#   we cluster a row's number elements by x0 (merging char pieces) and parse the
#   merged text.  Page/contest/candidate headers all sit in the right half of the
#   page (x0 > 60), so data rows are simply those whose leftmost element is at
#   the left margin (x0 < 60).
# ---------------------------------------------------------------------------

_METHOD_LABELS = ("Normal", "Absentee", "Provisional")
# row clustering: a method label and its numbers sit ~0.1-1px apart; consecutive
# rows are ~10px apart, so a 6px tolerance groups a row without merging rows.
_ROW_TOL = 6.0
# x0 tolerance for merging char-split digits of one number ("3"+"44" -> "344").
# Vote columns are ~30px apart; char pieces within a column are <4px apart.
_DIGIT_TOL = 4.5


def _sovc_clean_precinct(name):
    """Drop isolated stray single-char *alpha* tokens (the 'I' noise on
    Stoddard p19) while preserving single-digit ward numbers, e.g. the '1'
    in 'SIKESTON WARD 1'."""
    toks = name.split()
    cleaned = [t for t in toks if len(t) > 1 or t.isdigit()]
    return " ".join(cleaned) if cleaned else name


def _sovc_row_votes(rrow, n_cand):
    """Extract candidate vote values from a method row's elements.

    Numbers live at x0 > 100.  Cluster them by x0 (merging char-split digits),
    concatenate each cluster's text, keep the integer tokens (reg/total then one
    per candidate), and return the n_cand candidate values."""
    nums = sorted([e for e in rrow if e[0] > 100], key=lambda e: e[0])
    clusters, cur, last = [], [], None
    for e in nums:
        if last is None or e[0] - last <= _DIGIT_TOL:
            cur.append(e)
        else:
            clusters.append(cur)
            cur = [e]
        last = e[0]
    if cur:
        clusters.append(cur)
    vals = []
    for cl in clusters:
        s = "".join(e[2] for e in cl).replace(" ", "")
        if _is_int_tok(s):
            vals.append(int(s.replace(",", "")))
    return vals[2:2 + n_cand]


def _sovc_office_above(els, party_top):
    """Concatenate the office line(s) above the party line (left margin)."""
    above = sorted([e for e in els if e[0] < 240 and 70 < e[1] < party_top],
                   key=lambda e: e[1])
    return " ".join(e[2] for e in above).strip()


def _sovc_meta_top(els):
    """Top of the Reg./Total/Voters/Votes meta-label row -- the row that sits
    between the office header and the first data row.  Used to bound the
    measure office-title band (everything left-margin above it)."""
    tops = [e[1] for e in els
            if e[0] < 230 and e[1] > 80
            and re.match(r"(Reg\.?|Total|Voters?|Votes?)$", e[2])]
    return min(tops) if tops else None


def _sovc_header_lower(els, header_top):
    """Lower bound of the candidate-header band: the 'Jurisdiction Wide' row
    if present, else the Reg./Total/Voters/Votes meta-label row (for contest
    continuation pages that lack 'Jurisdiction Wide')."""
    jw_top = next((top for x0, top, t in els if t == "Jurisdiction Wide"), None)
    if jw_top is not None:
        return jw_top
    meta_tops = [e[1] for e in els
                 if e[0] < 230 and header_top < e[1] < header_top + 45
                 and re.match(r"(Reg|Total|Voter|Vote)", e[2])]
    return (max(meta_tops) + 3) if meta_tops else header_top + 45


def _sovc_clean_cands(col_names, party):
    """Clean reconstructed candidate names for county-specific contests."""
    out = []
    for nm in col_names:
        if re.search(r"write[\s-]*in", nm, re.I):
            out.append(("Write-Ins", party))  # contest party, like real candidates
        elif re.search(r"no\s+candidate\s+filed", nm, re.I):
            out.append(("No Candidate Filed", party))
        else:
            out.append((titlecase_name(nm), party))
    return out


def _norm_writein(name):
    """Normalize any write-in variant to 'Write-Ins' for comparison."""
    return "Write-Ins" if re.search(r"write[\s-]*in", name, re.I) else name


def _sovc_measure_cands(col_names):
    """Assign Yes/No to measure columns by detecting the words in the header."""
    if len(col_names) >= 2:
        result = []
        for nm in col_names:
            u = re.sub(r"[^A-Z]", "", nm.upper())
            if "YES" in u:
                result.append("Yes")
            elif "NO" in u:
                result.append("No")
            else:
                result.append(None)
        if all(r is not None for r in result):
            return [(r, "") for r in result]
    return [("Yes", ""), ("No", "")]


def _sovc_header(els, county):
    """Detect the contest header on a SOVC page and reconstruct the ordered
    candidate columns.  Returns (office, district, party, cands) or None when
    no contest header is present.  Shared by the method-row (Format 2) and
    by-precinct (Format 3) SOVC parsers."""
    party_top = None
    party_txt = None
    for x0, top, t in els:
        m = _SOVC_PARTY_RE.match(t)
        if m:
            party_top = top
            party_txt = m.group(1)
            break
    if party_top is not None:
        office_raw = _sovc_office_above(els, party_top)
        party = normalize_party(party_txt)
        header_top = party_top
        header_lower = _sovc_header_lower(els, header_top)
    else:
        # Measure (ballot question): the office title sits in the left-margin
        # band above the Reg./Total/Voters/Votes meta row.  The QUESTION /
        # PROPOSITION / AMENDMENT keyword may be at the start, the end, or on
        # its own line within that band (Scott has all three shapes).
        meta_top = _sovc_meta_top(els)
        if meta_top is None:
            return None
        band = sorted([e for e in els if e[0] < 240 and 70 < e[1] < meta_top],
                      key=lambda e: e[1])
        band_text = " ".join(e[2] for e in band).strip()
        if not re.search(r"\b(QUESTION|PROPOSITION|CONSTITUTIONAL AMENDMENT)\b",
                         band_text, re.I):
            return None  # no contest header on this page
        office_raw = band_text
        party = ""
        header_top = band[0][1] if band else meta_top
        header_lower = meta_top

    office, district = normalize_office(office_raw)
    measure = is_ballot_measure(office)

    # --- reconstruct candidate columns from positioned header elements ---
    hdr = [e for e in els if header_top < e[1] < header_lower and e[0] > 230]
    cols = _cluster_by_x0(hdr)
    col_names = [_reconstruct_name(c) for c in cols]

    if measure:
        cands = _sovc_measure_cands(col_names)
    elif _is_shared_contest(office):
        # statewide / federal / constitutional: candidates are the same in
        # every county, so cross-reference (and seed) the known map.
        key = (office, district, party)
        known = [c for c, _ in KNOWN_CANDIDATES.get(key, [])]
        if known and len(known) == len(col_names):
            # normalize write-in variants before matching so "Write-in"
            # (reconstructed) lines up with "Write-Ins" (known).
            col_norm = [_norm_writein(n) for n in col_names]
            known_norm = [_norm_writein(k) for k in known]
            matched = _match_known(col_norm, known_norm)
            if matched:
                cands = [(m, party) for m in matched]
            else:
                cands = _sovc_clean_cands(col_names, party)
                print(f"  [warn] {county}: fuzzy match failed for "
                      f"{office} {party}: {col_names}")
        elif col_names:
            cands = _sovc_clean_cands(col_names, party)
        else:
            cands = []
        if key not in KNOWN_CANDIDATES and cands:
            KNOWN_CANDIDATES[key] = cands[:]
    else:
        # county-specific contest (County Clerk, Commissioner, ...): its
        # candidates are unique to this county, so always reconstruct from
        # the page header and never touch the shared map.
        cands = _sovc_clean_cands(col_names, party) if col_names else []

    return (office, district, party, cands)


def _sovc_page_rows(els):
    """Cluster a page's positioned elements into visual rows (top within
    _ROW_TOL of the row's first element).  Shared by the SOVC parsers."""
    ordered = sorted(els, key=lambda e: e[1])
    page_rows, cur, cur_top = [], [], None
    for e in ordered:
        if cur_top is None or e[1] - cur_top <= _ROW_TOL:
            cur.append(e)
            if cur_top is None:
                cur_top = e[1]
        else:
            page_rows.append(cur)
            cur, cur_top = [e], e[1]
    if cur:
        page_rows.append(cur)
    return page_rows


def parse_sovc(path, county):
    """Parse a MO SOS Statement-of-Votes-Cast matrix PDF (Format 2:
    precincts as rows grouped under Normal/Absentee/Provisional method labels,
    with a trailing jurisdiction 'Total' section).

    Each precinct contributes up to three method rows (Normal/Absentee/
    Provisional) for the same candidate.  A precinct's vote for a candidate is
    the *sum* across those method rows (election-day + absentee + provisional),
    so we accumulate per (precinct, candidate) and emit one summed row -- not
    one row per method, which would create duplicate (precinct, office,
    candidate) entries.  (In the Jackson/Christian sources the absentee/
    provisional rows are 0, so this collapses to the Normal vote, but summing is
    correct regardless.)"""
    pdf = PDF(path)
    accum = {}  # (precinct, office, district, cparty, cname) -> summed votes
    contest_totals = {}  # (office,district,party,candidate) -> precinct sum
    checksum_expected = {}  # same key -> contest "Total" row votes

    for page in pdf.pages:
        els = [(e.x0, e.top, (e.text or "").strip())
               for e in page.find_all("text") if (e.text or "").strip()]
        if not els:
            continue  # image-only page (e.g. most of Stoddard)

        hdr = _sovc_header(els, county)
        if hdr is None:
            continue
        office, district, party, cands = hdr
        n_cand = len(cands)
        if n_cand == 0:
            continue

        # --- data rows: method labels (Normal/Absentee/Provisional) per
        # precinct, then a 'Total' section with the jurisdiction totals ---
        page_rows = _sovc_page_rows(els)
        precinct = None
        total_section = False
        for rrow in page_rows:
            rrow.sort(key=lambda e: e[0])
            left = rrow[0]
            if left[0] > 60:
                continue  # page header / contest header / candidate header
            label = left[2]
            if label == "Jurisdiction Wide":
                continue
            # "Total" with no numbers = jurisdiction-total section marker.
            if label == "Total" and not any(
                    _is_int_tok(e[2]) for e in rrow if e[0] > 100):
                total_section = True
                continue
            if label in _METHOD_LABELS:
                if total_section:
                    continue  # jurisdiction-wide Normal/Absentee (not precinct)
                cv = _sovc_row_votes(rrow, n_cand)
                if len(cv) != n_cand:
                    if precinct:
                        print(f"  [warn] {county}: skipped row {office} / "
                              f"{precinct}: got {len(cv)} values, want {n_cand}")
                    continue
                for i, (cname, cparty) in enumerate(cands):
                    if precinct is None:
                        continue
                    key = (precinct, office, district, cparty, cname)
                    accum[key] = accum.get(key, 0) + cv[i]
                    ck = (office, district, party, cname)
                    contest_totals[ck] = contest_totals.get(ck, 0) + cv[i]
            elif total_section and label == "Total":
                # the jurisdiction "Total" row carries the official per-candidate
                # contest totals -- use it as the checksum expectation.
                cv = _sovc_row_votes(rrow, n_cand)
                if len(cv) == n_cand:
                    for i, (cname, cparty) in enumerate(cands):
                        checksum_expected[(office, district, party, cname)] = \
                            cv[i]
            elif not total_section:
                precinct = _sovc_clean_precinct(label)

    rows = [[county, p, o, d, cp, cn, v]
            for (p, o, d, cp, cn), v in accum.items()]
    return rows, contest_totals, checksum_expected


def parse_sovc_by_precinct(path, county):
    """Parse a MO SOS "by precinct" SOVC PDF (Format 3): each precinct is one
    row (precinct name + registered/total + per-candidate votes), with a
    trailing 'Total' row.  County-summary prefix pages (header
    'Election Summary Report') are skipped; only 'Statement of Votes Cast'
    pages carry precinct data.  Scott uses this layout."""
    pdf = PDF(path)
    rows = []
    contest_totals = {}
    checksum_expected = {}

    for page in pdf.pages:
        els = [(e.x0, e.top, (e.text or "").strip())
               for e in page.find_all("text") if (e.text or "").strip()]
        if not els:
            continue
        # Skip the leading county-summary report; only the "Statement of Votes
        # Cast" pages are precinct-level.
        if not any(t == "Statement of Votes Cast" for _, _, t in els):
            continue

        hdr = _sovc_header(els, county)
        if hdr is None:
            continue
        office, district, party, cands = hdr
        n_cand = len(cands)
        if n_cand == 0:
            continue

        page_rows = _sovc_page_rows(els)
        for rrow in page_rows:
            rrow.sort(key=lambda e: e[0])
            left = rrow[0]
            if left[0] > 60:
                continue  # page header / contest header / candidate header
            label = left[2]
            if label == "Jurisdiction Wide":
                continue
            if label == "Total":
                # trailing contest total -> checksum expectation
                cv = _sovc_row_votes(rrow, n_cand)
                if len(cv) == n_cand:
                    for i, (cname, cparty) in enumerate(cands):
                        checksum_expected[(office, district, party, cname)] = \
                            cv[i]
                continue
            precinct = _sovc_clean_precinct(label)
            cv = _sovc_row_votes(rrow, n_cand)
            if len(cv) != n_cand:
                print(f"  [warn] {county}: skipped row {office} / "
                      f"{precinct}: got {len(cv)} values, want {n_cand}")
                continue
            for i, (cname, cparty) in enumerate(cands):
                rows.append([county, precinct, office, district,
                             cparty, cname, cv[i]])
                k = (office, district, party, cname)
                contest_totals[k] = contest_totals.get(k, 0) + cv[i]

    return rows, contest_totals, checksum_expected


# ---------------------------------------------------------------------------
# Format 3b -- Jasper "official results by precinct"
#   Ruling lines are present, so page.extract_tables() yields clean data rows
#   (precinct + per-candidate votes + a trailing 'County - Total' row).  The
#   candidate *header* cells are rendered as reversed, vertically-stacked
#   characters (the SOVC report's rotated-text artifact), so each candidate
#   name is recovered by un-reversing its header cell.  Each contest spans two
#   pages (precincts split across them); the office line only appears on the
#   first page (it contains "Vote for"), so office/party carry to the
#   continuation page.
# ---------------------------------------------------------------------------

def _jasper_unreverse(cell):
    """Recover a candidate name from a reversed, vertically-stacked header
    cell: each line is char-reversed and the lines themselves are in reverse
    order, so reverse each line, reverse the line order, and join with
    spaces."""
    if not cell:
        return ""
    lines = [ln for ln in cell.split("\n") if ln]
    lines = [ln[::-1] for ln in lines]
    lines.reverse()
    return " ".join(lines).strip()


def _jasper_party(els, office_top):
    """The standalone party label printed just below the office line (e.g.
    'REP' / 'DEM' / 'LIB').  Absent on measure pages -> ''."""
    for x0, top, t in sorted(els, key=lambda e: e[1]):
        if x0 < 60 and office_top < top <= office_top + 25:
            if t.upper() in ("REP", "REPUBLICAN", "DEM", "DEMOCRAT", "DEMOCRATIC",
                             "LIB", "LIBERTARIAN", "CON", "CONSTITUTION",
                             "GRE", "GREEN", "NONPARTISAN", "NON-PARTISAN"):
                return normalize_party(t)
    return ""


def _jasper_join_office(row_els):
    """Join the office-line elements (same visual row, sorted by x0) into the
    office name.  The PDF wraps long office lines mid-word across elements, so
    a piece that starts with a lowercase letter (or a closing paren) is a
    continuation of the previous token and is joined *without* a space.  The
    trailing '(REP)' party and '(Vote for N)' parentheticals are then stripped
    (the party is captured separately via _jasper_party)."""
    out = ""
    for x0, top, t in row_els:
        if not out:
            out = t
        elif t and (t[0].islower() or not t[0].isalnum()):
            out += t
        else:
            out += " " + t
    out = out.strip()
    for _ in range(3):
        new = re.sub(r"\s*\([^()]*\)\s*$", "", out).strip()
        if new == out:
            break
        out = new
    return out


_PARTY_PAREN_RE = re.compile(
    r"\s*\((REP|DEM|LIB|CON|GRE|CST|IND|REPUBLICAN|DEMOCRATIC|DEMOCRAT|"
    r"LIBERTARIAN|GREEN|CONSTITUTION|NONPARTISAN|NONPARTISAN)\)\s*",
    re.I,
)


def _jasper_clean_cand(nm, office, party):
    # Rotated header cells may carry the party label "(REP)" at the top of the
    # cell (Jasper, first-name-first -> leading) or embedded mid-name (Nodaway,
    # last-name-first).  The party is already captured separately via
    # _jasper_party, so strip *every* party parenthetical from the name and
    # collapse the gaps it leaves.
    nm = _PARTY_PAREN_RE.sub(" ", nm)
    nm = re.sub(r"\s+", " ", nm).strip()
    if is_ballot_measure(office):
        u = nm.upper()
        if "YES" in u:
            return ("Yes", "")
        if "NO" in u:
            return ("No", "")
        return (nm, "")
    if re.search(r"write[\s-]*in", nm, re.I):
        return ("Write-Ins", party)  # contest party, like real candidates
    if re.search(r"no\s+candidate\s+filed", nm, re.I):
        return ("No Candidate Filed", party)
    return (titlecase_name(nm), party)


def parse_sovc_rotated(path, county):
    """Parse a MO SOS "by precinct" SOVC report whose candidate headers are
    rendered as rotated (reversed, vertically-stacked) characters -- the
    format used by Jasper and Nodaway.  See _jasper_* helpers above."""
    pdf = PDF(path)
    rows = []
    contest_totals = {}
    checksum_expected = {}
    cur_office = None
    cur_district = ""
    cur_party = ""

    for page in pdf.pages:
        els = [(e.x0, e.top, (e.text or "").strip())
               for e in page.find_all("text") if (e.text or "").strip()]

        # A contest's first page carries the office line, which always contains
        # "Vote for"; continuation pages do not, so office/party carry over.
        vt = next((e[1] for e in els if "Vote for" in e[2]), None)
        if vt is not None:
            office_row = sorted([e for e in els if vt - 2 <= e[1] <= vt + 12],
                                key=lambda e: e[0])
            office_raw = _jasper_join_office(office_row)
            cur_office, cur_district = normalize_office(office_raw)
            cur_party = _jasper_party(els, vt)
        if cur_office is None:
            continue  # precinct-roster pages (pp1-2) have no contest yet

        try:
            tabs = page.extract_tables()
        except Exception:
            tabs = []
        if not tabs:
            continue
        # the candidate-results table is the widest one (>=4 cols); the
        # precinct roster table is only 3 cols and is ignored.
        results = max(tabs, key=lambda tb: max((len(r) for r in tb), default=0))
        if not results or max(len(r) for r in results) < 4:
            continue
        header = results[0]

        # candidate columns = non-None header cells at odd positions whose
        # un-reversed text is not the "Total Votes" column.
        cand_cols = []  # (col_index, (name, party))
        for i in range(1, len(header)):
            if header[i] is None:
                continue
            nm = _jasper_unreverse(header[i])
            if "Total" in nm:
                continue
            cand_cols.append((i, _jasper_clean_cand(nm, cur_office, cur_party)))
        if not cand_cols:
            continue

        for r in results[1:]:
            precinct = (r[0] or "").strip()
            if not precinct:
                continue
            first_i = cand_cols[0][0]
            first_val = r[first_i] if first_i < len(r) else ""
            if not first_val or not re.fullmatch(r"[\d,]+(?:\.\d+)?",
                                                 first_val.strip()):
                continue  # blank 'County' / 'Jasper County' sub-header rows
            if "Total" in precinct:
                for i, (cname, cparty) in cand_cols:
                    v = r[i] if i < len(r) else "0"
                    checksum_expected[(cur_office, cur_district, cur_party,
                                      cname)] = _int(v)
                continue
            for i, (cname, cparty) in cand_cols:
                v = r[i] if i < len(r) else "0"
                votes = _int(v)
                rows.append([county, precinct, cur_office, cur_district,
                             cparty, cname, votes])
                k = (cur_office, cur_district, cur_party, cname)
                contest_totals[k] = contest_totals.get(k, 0) + votes

    return rows, contest_totals, checksum_expected

def main():
    outputs = {}
    checksums = {}  # county -> (summed_per_precinct, contest_total_row)

    print("Parsing St. Louis City PDF ...")
    stl_rows, stl_totals = parse_st_louis_city(
        os.path.join(SRC_DIR, "St Louis City MO Final Official Precinct Results 08.04.26.pdf"))
    print(f"  {len(stl_rows)} rows, {len(set(r[1] for r in stl_rows))} precincts")
    outputs["st_louis_city"] = stl_rows

    # Stoddard's SOVC PDF is 17/19 image-only pages (no text layer); only 2 of
    # its ~dozen contests are electronic, so emitting a CSV now would be
    # misleadingly incomplete.  The whole county is deferred to the OCR pass.
    # print("Parsing Stoddard SOVC ...")  # deferred to PaddleOCR

    print("Parsing Jackson SOVC (3 files) ...")
    jc_rows = []
    jc_sum, jc_exp = {}, {}
    for fn in ["Jackson MO SOVC Amendments 08.04.26.pdf",
               "Jackson MO SOVC County Democratic 08.04.26.pdf",
               "Jackson MO SOVC County Republican 08.04.26.pdf"]:
        r, s, e = parse_sovc(os.path.join(SRC_DIR, fn), "Jackson")
        print(f"    {fn}: {len(r)} rows")
        jc_rows += r
        jc_sum.update(s)
        jc_exp.update(e)
    print(f"  Jackson total: {len(jc_rows)} rows, "
          f"{len(set(r[1] for r in jc_rows))} precincts")
    outputs["jackson"] = jc_rows
    checksums["jackson"] = (jc_sum, jc_exp)

    print("Parsing Christian SOVC ...")
    ch_rows, ch_sum, ch_exp = parse_sovc(
        os.path.join(SRC_DIR, "Christian MO SOVC 08.04.26.pdf"), "Christian")
    print(f"  {len(ch_rows)} rows, {len(set(r[1] for r in ch_rows))} precincts")
    outputs["christian"] = ch_rows
    checksums["christian"] = (ch_sum, ch_exp)

    print("Parsing Scott SOVC (by precinct) ...")
    sc_rows, sc_sum, sc_exp = parse_sovc_by_precinct(
        os.path.join(SRC_DIR, "Scott MO Official Results Combined 08.04.26.pdf"),
        "Scott")
    print(f"  {len(sc_rows)} rows, {len(set(r[1] for r in sc_rows))} precincts")
    outputs["scott"] = sc_rows
    checksums["scott"] = (sc_sum, sc_exp)

    # Jasper and Nodaway share the rotated-header SOVC report format.
    print("Parsing Jasper SOVC (rotated headers) ...")
    ja_rows, ja_sum, ja_exp = parse_sovc_rotated(
        os.path.join(SRC_DIR, "Jasper MO official results by precinct 08.04.26.pdf"),
        "Jasper")
    print(f"  {len(ja_rows)} rows, {len(set(r[1] for r in ja_rows))} precincts")
    outputs["jasper"] = ja_rows
    checksums["jasper"] = (ja_sum, ja_exp)

    print("Parsing Nodaway SOVC (rotated headers) ...")
    no_rows, no_sum, no_exp = parse_sovc_rotated(
        os.path.join(SRC_DIR, "Nodaway MO SOVC 08.04.26.pdf"),
        "Nodaway")
    print(f"  {len(no_rows)} rows, {len(set(r[1] for r in no_rows))} precincts")
    outputs["nodaway"] = no_rows
    checksums["nodaway"] = (no_sum, no_exp)

    # Drop exact-duplicate rows before writing.  These arise legitimately in
    # the rotated-header SOVC reports when a multi-party contest (e.g. U.S.
    # House) is laid out one party per page: each party page repeats the
    # nonpartisan "Write-Ins" column, so the same (precinct, office, district,
    # WI, Write-Ins, 0) row is emitted once per party page.  Real candidates
    # are party-specific so are not duplicated; only the shared Write-Ins
    # column is.  Exact dupes carry no information and the data-test suite
    # rejects them, so collapse to first occurrence (order preserved).
    for county, rows in outputs.items():
        seen = set()
        deduped = []
        for r in rows:
            key = tuple(r)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        if len(deduped) != len(rows):
            print(f"  [dedup] {county}: dropped {len(rows) - len(deduped)} "
                  f"exact-duplicate row(s) ({len(rows)} -> {len(deduped)})")
        outputs[county] = deduped

    for county, rows in outputs.items():
        path = os.path.join(OUT_DIR, f"{ELECTION}__{county_slug(county)}__precinct.csv")
        write_csv(path, rows)
        print(f"  wrote {path}  ({len(rows)} rows)")

    # Checksum: per-candidate precinct sums vs contest 'Total' row (SOVC only).
    for county, (summed, expected) in checksums.items():
        bad = 0
        for k, total in expected.items():
            if summed.get(k, 0) != total:
                bad += 1
                print(f"  [checksum] {county} MISMATCH {k}: "
                      f"precincts sum {summed.get(k,0)} != total {total}")
        print(f"  {county}: {len(expected)} contests checked, {bad} mismatch(es)")

    # Per-contest candidate totals summary.
    from collections import defaultdict
    for county, rows in outputs.items():
        totals = defaultdict(int)
        for r in rows:
            totals[(r[2], r[3], r[4], r[5])] += int(r[6])
        print(f"\n{county} -- {len(totals)} office/candidate combos (first 20):")
        for k in sorted(totals)[:20]:
            print(f"    {k} = {totals[k]}")

    print("\nDone.")


if __name__ == "__main__":
    main()