"""Parse OCR markdown (PaddleOCR-VL cache) into 2026 precinct CSVs.

Each county gets a parser keyed to its vendor report format. Votes are
verified with crosscheck() against the certified county-totals file before
anything is written.
"""
import difflib
import html
import os
import re
import sys
import unicodedata

from parse_primary_pdfs_more import (
    ELECTION, OUT_DIR, county_certified, crosscheck, _int,
)
from parse_primary_sources import (
    clean_candidate, county_slug, is_ballot_measure, normalize_office,
    normalize_party, titlecase_name, write_csv,
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_cache")


def _ocr_cand(raw, measure=False):
    """clean_candidate for OCR text: strips accent marks the OCR model
    added ("Pondélick" -> "Pondelick") first."""
    s = unicodedata.normalize("NFD", str(raw))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"&[Qq]uot;", '"', s)
    return clean_candidate(s, is_ballot_measure=measure)


def _reconcile_names(rows, cert, county):
    """OCR name-noise repair: rows whose (office, dist, party) contest IS in
    the certified file but whose candidate key is not get difflib-matched
    against that contest's certified candidate names and renamed when a
    clearly-best match exists. Returns (repaired_rows, renames)."""
    contests = {}
    for (office, dist, party, cand), _ in cert.items():
        contests.setdefault((office, dist, party), []).append(cand)
    out, renames = [], []
    for r in rows:
        k = (r[2].lower(), r[3], r[4], r[5].lower())
        if k in cert or k[:3] not in contests:
            out.append(r)
            continue
        cands = contests[k[:3]]
        scores = sorted((difflib.SequenceMatcher(None, k[3], c).ratio(), c)
                        for c in cands)
        best_score, best = scores[-1]
        if best_score < 0.55 or (len(scores) > 1
                                 and best_score - scores[-2][0] < 0.08):
            out.append(r)
            continue
        r = list(r)
        r[5] = _ocr_cand(best)
        renames.append((r[1], k[:3], r[5] and best, r[5]))
        out.append(r)
    for precinct, contest, new, ren in renames:
        print(f"  [~] {county} {precinct}: {contest} rename -> '{ren}'")
    return out, renames


def _title_cell(cell):
    """(office_text, party) from a contest title cell: the office name is the
    first line, an optional trailing line is '(REPUBLICAN PARTY)' etc."""
    lines = [l.strip() for l in re.split(r"\\n|\n", str(cell)) if l.strip()]
    office, party = lines[0], ""
    if len(lines) > 1:
        m = re.match(r"^\((.*\bPARTY)\)$", lines[-1])
        if m:
            party = normalize_party(m.group(1))
            office = " ".join(lines[:-1])
    if not party:  # variant: "… (REPUBLICAN PARTY)" on the same line
        joined = " ".join(lines)
        m = re.search(r"\s*\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN|"
                      r"CONSTITUTION)\s+PARTY\)\s*$", joined, re.I)
        if m:
            party = normalize_party(m.group(1))
            office = joined[:m.start()].strip()
    return office, party


def _join_name(cell):
    return " ".join(l.strip() for l in cell.split("\\n") if l.strip())


def _votes_of(cells):
    """Vote count from a data row: the first purely-numeric cell (OCR
    sometimes shifts columns to name|percent|votes); percent cells are
    ignored, but an all-numeric cell still wins so Total Votes pairs work."""
    for c in cells[1:]:
        if "%" in c:
            continue
        t = c.replace(",", "").strip()
        if t.isdigit():
            return int(t)
    return 0


def _county_rows(county, pages_rows):
    """Crosscheck + write pipeline shared by all OCR parsers."""
    rows = [r for rows in pages_rows for r in rows]
    cert = county_certified(county)
    rows, _renames = _reconcile_names(rows, cert, county)
    bad, msgs = crosscheck(rows, cert, county)
    print("\n".join(m for m in msgs if not m.startswith("  [?]")))
    nq = sum(1 for m in msgs if m.startswith("  [?]"))
    if nq:
        print(f"  [?] {nq} rows not in certified file (local contests etc.)")
    path = os.path.join(OUT_DIR,
                        f"{ELECTION}__{county_slug(county)}__precinct.csv")
    write_csv(path, rows)
    n_prec = len(set(r[1] for r in rows if r[2] not in
                     ("Registered Voters", "Ballots Cast")))
    print(f"  wrote {path}  ({len(rows)} rows, {n_prec} precincts), "
          f"crosscheck mismatch(es): {bad}")
    return bad


def _emit(entries, county):
    """entries: (office, dist, party, precinct, [(candidate, votes)])."""
    out = []
    for office, dist, party, precinct, rows in entries:
        for cand, votes in rows:
            out.append([county, precinct, office, dist, party, cand,
                        str(votes)])
    return out


# ---------------------------------------------------------------------------
# Buchanan -- Dominion "Precinct Summary Report" (scanned, 23 blocks of 3
# pages). The header banner (precinct name) is a bitmap the OCR model left as
# an <img>; names below were read visually from rendered page-image crops of
# the block headers (blocks start on pages 1, 4, 7, ..., 67).
# ---------------------------------------------------------------------------

_BUCHANAN_NAMES = [
    "1 - St Peter", "3 - St Joseph Visitors Bureau", "4 - Brookdale",
    "5 - Pony Express", "6 - Copeland", "7 - Huffman", "8 - The Evolution",
    "9 - The Keys", "10 - Faith United", "11 - Ashland United",
    "12 - Wesley United", "13 - Knights of Columbus",
    "14 - MO National Guard", "15 - Central Christian", "16 - Agency",
    "17 - DeKalb", "18 - Willowbrook", "19 - Faucett",
    "20 - St Joseph Catholic", "21 - Frazier", "22 - Rushville",
    "23 - Grace", "Absentee",
]


def _buchanan_tables(path):
    """A table (from one page's markdown) as a list of rows of cell texts."""
    with open(path) as f:
        md = f.read()
    out = []
    for m in re.finditer(r"<table[^>]*>.*?</table>", md):
        tbl = []
        for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", m.group(0)):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1))
            tbl.append([c.strip() for c in cells])
        out.append(tbl)
    return out


def parse_precinct_summary(county, cdir_name, precincts, pages_per, blocks=None):
    """Dominion "Precinct Summary Report" core (Buchanan/Howard): per-precinct
    party-distribution + contest tables, OCR'd, with precinct banners dropped
    by the OCR and supplied visually (names list, one per block)."""
    cdir = os.path.join(CACHE_DIR, cdir_name)
    if blocks is None:
        blocks = len(precincts)
    assert len(precincts) == blocks
    # First pass: every table becomes an entry (office|None, district,
    # party, precinct, rows). OCR sometimes drops the office line inside a
    # title cell (leaving only "(DEMOCRATIC PARTY)") or splits a title off
    # its rows entirely; such tables keep party but office=None.
    entries = []
    for block in range(blocks):
        precinct = precincts[block]
        for page in range(block * pages_per + 1,
                          (block + 1) * pages_per + 1):
            for tbl in _buchanan_tables(os.path.join(
                    cdir, f"p{page:03d}.md")):
                title = tbl[0][0] if tbl else ""
                if "Party Distribution" in title \
                        or title.startswith("Registered Voters"):
                    continue
                title_office, party = _title_cell(title)
                # orphan titles: the OCR dropped the office line so the
                # cell is only "(X PARTY)" / "Vote For 1" / wrapped residue
                if not title_office or re.match(
                        r"^\((?:\w+\s+)?PARTY\)$", title_office, re.I) \
                        or title_office.startswith("Vote For"):
                    title_office = None
                # amendment titles carry "NO. n", which normalize_office
                # can't fix later -> pre-normalize here
                if title_office and re.fullmatch(
                        r"Constitutional Amendment No\.\s*\d+",
                        title_office, re.I):
                    n = re.search(r"(\d+)", title_office).group(1)
                    title_office = f"Constitutional Amendment {n}"
                if title_office:
                    office, dist = normalize_office(title_office)
                else:
                    office, dist = None, None
                rows = []
                for cells in tbl[1:]:
                    if not cells or "Vote For" in cells[0]:
                        continue
                    # continuation row: OCR split a candidate name across
                    # two <tr>s, the second carrying only the name tail
                    # (no votes, no percent) -> append to the previous name
                    if not any(c.strip() for c in cells[1:]) and rows:
                        last = rows.pop()
                        rows.append([last[0] + " " + _ocr_cand(cells[0]),
                                     last[1]])
                        continue
                    cand = _ocr_cand(_join_name(cells[0]))
                    if cand in ("Total Votes", "", "Registered Voters"):
                        continue
                    if office and re.search(r"Amendment", office):
                        cand = _ocr_cand(cells[0], measure=True)
                        if cand not in ("Yes", "No"):
                            continue
                        party = ""
                    rows.append((cand, _votes_of(cells)))
                if not rows:
                    continue
                entries.append([office, dist, party, precinct, rows])
    # office inference: candidate -> (office, district, party) from titled
    # tables
    known = {}
    for office, dist, party, precinct, rows in entries:
        if office:
            for cand, _ in rows:
                known.setdefault(cand.lower(), (office, dist, party))
    unresolved = []
    for e in entries:
        if e[0]:
            continue
        offices, cands = set(), []
        for cand, _ in e[4]:
            cands.append(cand)
            k = cand.lower()
            if k not in known:
                unresolved.append((e[3], cand))
            else:
                offices.add(known[k])
        if unresolved:
            continue
        if len(offices) == 1:
            e[0], e[1], e[2] = offices.pop()
        else:
            unresolved.append((e[3], f"ambiguous {cands} -> {offices}"))
    if unresolved:
        raise SystemExit(f"unresolved tables: {sorted(set(map(str, unresolved)))}")
    return _county_rows(county, [_emit(entries, county)])


def parse_buchanan(county):
    return parse_precinct_summary(
        county, "Buchanan_MO_2026_August_Primary_Prec_by_Precinct",
        _BUCHANAN_NAMES, 3)


# Howard -- Dominion "Precinct Summary Report" (scanned, 8 blocks of 2
# pages). Banner names read visually from rendered page images; OCR only
# kept them on some page-2 banners (pages 1,3,4,15,16 lost theirs). Blocks
# start on odd pages 1,3,...,15.
_HOWARD_NAMES = [
    "Prairie-Burton", "Bonne Femme", "Moniteau", "Franklin",
    "Boonslick", "Chariton", "Richmond", "Absentee",
]


def parse_howard(county):
    return parse_precinct_summary(
        county, "Howard_MO_Precinct_Official_Results_08_04_26",
        _HOWARD_NAMES, 2)


# ---------------------------------------------------------------------------
# Adair -- ES&S "Official Election Results" (scanned, 12 blocks of 6 pages,
# preceded by a 6-page county "Summary" block, which is skipped). Block-start
# pages carry "# Official Election Results Primary Election ADAIR COUNTY
# August 4, 2026 <precinct>"; the KV1 block's heading is split by the OCR so
# its label is a standalone "KV1-Cornerstone" line.
# ---------------------------------------------------------------------------

_ADAIR_HEADER_RES = re.compile(
    r"(?:#+\s*)?Official Election Results.*?August 4, 2026\s*(\S.*)$",
    re.I)
_ADAIR_CONTEST_RE = re.compile(
    r"^#{1,6}\s+(.+?)\s+\((REP|DEM|LIB)\)\s+\(Vote for \d+\)\s+\2$", re.I)
_ADAIR_MEASURE_RE = re.compile(r"^(.+?)\s+\(Vote for \d+\)$", re.I)
_ADAIR_BOILER = ("Official Election Results", "Primary Election",
                 "ADAIR COUNTY", "August 4, 2026")


def _clean_heading_line(s):
    """`<div style="...">TEXT</div>` -> TEXT; '#'-headings -> text after #."""
    s = re.sub(r"^<div[^>]*>(.*)</div>.*$", r"\1", s)
    s = re.sub(r"^#+\s*", "", s)
    return s.strip()


def _page_items(md):
    """[('heading', text) | ('row', [cells])] in document order."""
    items = []
    pos = 0
    for m in re.finditer(r"<table[^>]*>.*?</table>", md):
        for line in md[pos:m.start()].split("\n"):
            s = line.strip()
            if s.startswith("#") or s.startswith("<div"):
                items.append(("heading", _clean_heading_line(s)))
        for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", m.group(0)):
            items.append(("row", [c.strip() for c in
                                  re.findall(r"<td[^>]*>(.*?)</td>",
                                             row.group(1))]))
        pos = m.end()
    for line in md[pos:].split("\n"):
        s = line.strip()
        if s.startswith("#") or s.startswith("<div"):
            items.append(("heading", _clean_heading_line(s)))
    return items


def _adair_block_label(md):
    """Precinct label from a block-start page: the heading tail after
    'August 4, 2026' (or, when the OCR split the heading, the first plain
    line between it and 'Precincts Reported')."""
    first = [l.strip() for l in md.split("\n") if l.strip()][0]
    m = _ADAIR_HEADER_RES.match(first)
    if m:
        tail = m.group(1).strip()
        if tail and tail != "Summary":
            return tail
    lines = [l.strip() for l in md.split("\n") if l.strip()]
    for i, l in enumerate(lines):
        if l == "August 4, 2026":
            for tl in lines[i + 1:]:
                if "Precincts Reported" in tl:
                    break
                if tl and tl not in _ADAIR_BOILER:
                    return tl
            break
    return None


def parse_adair(county, cdir_name=None):
    cdir = os.path.join(CACHE_DIR, cdir_name
                        or "Adair_MO_Official_Results_08_04_26")
    pages = sorted(int(m.group(1)) for m in
                   (re.match(r"p(\d+)\.md$", f)
                    for f in os.listdir(cdir)) if m)
    starts = []
    for p in pages:
        first = open(os.path.join(cdir, f"p{p:03d}.md")).readline().strip()
        if re.search(r"Official Election Results", first, re.I):
            starts.append(p)
    # label for each block start
    labels = {}
    for p in starts:
        labels[p] = _adair_block_label(open(os.path.join(
            cdir, f"p{p:03d}.md")).read())
    entries = []
    for i, p in enumerate(starts):
        label = labels[p]
        if not label or label == "Summary":
            continue  # county summary / certification pages
        end = starts[i + 1] if i + 1 < len(starts) else pages[-1] + 1
        for q in range(p, end):
            md = open(os.path.join(cdir, f"p{q:03d}.md")).read()
            office, dist, party, measure = None, None, None, False
            for kind, payload in _page_items(md):
                if kind == "heading":
                    m = re.match(r"(.+?)\s+\((REP|DEM|LIB)\)\s+"
                                 r"\(Vote for \d+\)\s+\2$", payload, re.I)
                    if m:
                        office, dist = normalize_office(m.group(1))
                        party = normalize_party(m.group(2))
                        measure = False
                    elif _ADAIR_MEASURE_RE.match(payload):
                        office = _ADAIR_MEASURE_RE.match(payload).group(1)
                        dist, party, measure = "", "", True
                    continue
                # table rows
                cells = payload
                if len(cells) < 3:
                    continue
                name = _join_name(cells[0])
                if name in ("", "Candidate", "Times Cast", "Total",
                            "Total Votes") or name.startswith("Times Cast"):
                    continue
                if measure:
                    cand = _ocr_cand(name, measure=True)
                    if cand not in ("Yes", "No"):
                        continue
                    entry_party = ""
                else:
                    cand = _ocr_cand(name)
                    entry_party = normalize_party(cells[1]) \
                        if len(cells) > 1 else ""
                if not office:
                    continue  # stray table outside a contest
                entries.append([office, dist, entry_party, label,
                                (cand, _votes_of(cells))])
    # regroup into (office, dist, party, precinct, rows)
    grouped = []
    for office, dist, party, precinct, pr in entries:
        if grouped and (grouped[-1][0], grouped[-1][1], grouped[-1][2],
                        grouped[-1][3]) == (office, dist, party, precinct):
            grouped[-1][4].append(pr)
        else:
            grouped.append([office, dist, party, precinct, [pr]])
    n_prec = len({g[3] for g in grouped})
    print(f"  parsed precincts: {n_prec}, blocks: "
          f"{[labels[p] for p in starts]}")
    return _county_rows(county, [_emit(grouped, county)])


# ---------------------------------------------------------------------------
# Stoddard -- MO SOS SOVC ("Statement of Votes Cast"), one contest per page.
# Each page table: title row(s) (office, optionally DIVISION n + "(X PARTY)"),
# then a header row "Reg. Voters | Total Votes | <candidate>(each spanning its
# votes+percent columns) ...", then per-precinct: banner row (all cells after
# cols[0] empty), a "Normal" row and an "Absentee" row. The county-final
# "Total" block and the "Jurisdiction Wide" row are excluded; the "ABSENTEE"
# block becomes an "Absentee" precinct (its votes sit on its Absentee row).
# One quirk: the Amendment 5 title is dropped entirely by the OCR -- it is
# hardcoded from the contest sequence (verified against the certified file).
# ---------------------------------------------------------------------------

_STODDARD_DIR = "Stoddard_MO_SOVC_08_04_26"
_STODDARD_FALLBACK_TITLES = {19: "Constitutional Amendment No. 5"}


def _stoddard_numeric(cells):
    """Numbers from a data row, in order: Reg. Voters, Total Votes, then each
    candidate's votes (percent cells and '-' cells are dropped)."""
    out = []
    for c in cells:
        t = c.replace(",", "").strip()
        if t.isdigit():
            out.append(int(t))
    return out


def parse_stoddard(county):
    cdir = os.path.join(CACHE_DIR, _STODDARD_DIR)
    entries = []
    for page in range(1, 20):
        path = os.path.join(cdir, f"p{page:03d}.md")
        md = open(path).read()
        for tbl in _buchanan_tables(path):
            # title rows: everything before the "Reg. Voters" header row
            tparts, hdr_i = [], None
            for i, cells in enumerate(tbl):
                if any(c in ("Reg. Voters", "Total Votes") for c in cells):
                    hdr_i = i
                    break
                tparts += [c.strip() for c in cells if c.strip()]
            if not tparts:
                m = re.search(r"<div[^>]*>([^<]*AMENDMENT[^<]*)</div>", md,
                              re.I)
                if m:
                    tparts = [m.group(1).strip()]
            if not tparts and page in _STODDARD_FALLBACK_TITLES:
                tparts = [_STODDARD_FALLBACK_TITLES[page]]
            if not tparts:
                continue
            title = "\n".join(tparts)
            office, party = _title_cell(title)
            # amendment titles carry "NO. n", which normalize_office can't
            # fix later -> pre-normalize here
            if re.fullmatch(r"Constitutional Amendment No\.\s*\d+", office,
                            re.I):
                n_no = re.search(r"(\d+)", office).group(1)
                office = f"Constitutional Amendment {n_no}"
            office, dist = normalize_office(office)
            # keep DIVISION roman numerals capitalized after titlecasing
            office = re.sub(r"(Division\s+)Iii$", r"\1III", office)
            office = re.sub(r"(Division\s+)Ii$", r"\1II", office)
            measure = bool(re.search(r"Amendment", office or ""))
            # candidate headers: name cells in the header row (YES/NO for
            # measures)
            cands = []
            for c in tbl[hdr_i]:
                c = c.strip()
                if c in ("", "Reg. Voters", "Total Votes"):
                    continue
                cands.append(_ocr_cand(_join_name(c), measure=measure))
            if measure:
                cands = [c for c in cands if c and c.lower() in ("yes", "no")]
            if not cands:
                print(f"  [!] page {page}: no candidates for {title!r}")
                continue
            n = len(cands)
            # walk data rows: banner (all cells after cols[0] empty), then
            # Normal / Absentee rows; 'Total' and 'Jurisdiction Wide' blocks
            # are county-level and are skipped.
            blocks = {}
            prec = None
            for cells in tbl[hdr_i + 1:]:
                kind = cells[0].strip()
                low = kind.lower()
                if all(not c.strip() for c in cells[1:]):
                    merged = re.match(r"(?i)jurisdiction wide\s+(.*)", kind)
                    kind = merged.group(1).strip() if merged else kind
                    prec = None if low in ("jurisdiction wide", "total") \
                        else kind
                    if prec:
                        blocks[prec] = {}
                    continue
                if prec is None:
                    continue
                if low in ("normal", "absentee"):
                    nums = _stoddard_numeric(cells)
                    if len(nums) == n + 2:
                        v = nums[2:]
                    elif len(nums) == n + 1:
                        v = nums[1:]
                    elif len(nums) == n:
                        v = nums
                    else:
                        print(f"  [!] p{page:03d} {prec} {kind}: bad shape "
                              f"nums={nums}")
                        continue
                    blocks[prec][low] = v
            for prec, tallies in blocks.items():
                votes = [(cands[k], (tallies.get("normal") or [0] * n)[k]
                          + (tallies.get("absentee") or [0] * n)[k])
                         for k in range(n)]
                entries.append([office, dist, party, prec, votes])
    return _county_rows(county, [_emit(entries, county)])



# ---------------------------------------------------------------------------
# Hickory -- compact SOVC ("Statement of Votes Cast", landscape, one contest
# per page): precinct | Reg. Voters | Total Votes | per candidate (votes, %).
# Variants handled: votes+percent merged into one cell ("367 100.00%"), title
# rows split across several rows ("US REPRESENTATIVE" / "DISTRICT 4" /
# "(LIBERTARIAN PARTY)"), "(REPUBLICAN, PARTY)" commas, Jurisdiction Wide /
# ABSENTEE banners merged with data rows, the last candidate's (votes, %)
# pair dropped on p18 (recovered as Total Votes - sum of the others), and
# five contest titles lost entirely -- the fixed page->contest map below is
# the ballot order confirmed against the Dominion summary pages in the same
# PDF and the OCR titles where they survived.
# ---------------------------------------------------------------------------

_HICKORY_DIR = "Hickory_MO_Election_Results_08_04_26"
_HICKORY_TITLES = {
    5: ("State Auditor", "", "REP"),
    6: ("U.S. House", "4", "REP"),
    7: ("State Senate", "28", "REP"),
    8: ("State House", "126", "REP"),
    9: ("State House", "128", "REP"),
    10: ("Presiding Commissioner of the County Commission", "", "REP"),
    11: ("Clerk of the County Commission", "", "REP"),
    12: ("Circuit Clerk", "", "REP"),
    13: ("Recorder of Deeds", "", "REP"),
    14: ("County Treasurer", "", "REP"),
    15: ("Prosecuting Attorney", "", "REP"),
    16: ("Collector", "", "REP"),
    17: ("State Auditor", "", "DEM"),
    18: ("U.S. House", "4", "DEM"),
    19: ("State Senate", "28", "DEM"),
    20: ("State House", "126", "DEM"),
    21: ("State House", "128", "DEM"),
    22: ("Associate Circuit Judge 30th District", "", "DEM"),
    23: ("State Auditor", "", "LIB"),
    24: ("U.S. House", "4", "LIB"),
    25: ("Constitutional Amendment 1", "", ""),
    26: ("Constitutional Amendment 2", "", ""),
    27: ("Constitutional Amendment 4", "", ""),
    28: ("Constitutional Amendment 5", "", ""),
    29: ("Hermitage R-IV Proposition Teachers", "", ""),
}


def _sovc_num_tokens(cells):
    """All whitespace-split tokens from the data cells, keeping the votes /
    percent / '-' interleaving order."""
    toks = []
    for c in cells:
        toks.extend(c.split())
    return toks


def parse_hickory(county):
    """Compact SOVC variant: one contest per page, one row per precinct with
    (Reg. Voters, Total Votes, per-candidate votes/percent).

    OCR reliability (verified against rendered page images): the numeric
    block is frequently shifted UP one row relative to the label column --
    the 'Jurisdiction Wide' banner cell then carries the first precinct's
    numbers and a duplicate Total row follows.  A shifted page is detected
    by the banner row carrying numbers (its own numeric cell is a label, not
    a name) and re-paired positionally: number row i belongs to label i+1.
    OCR can also merge 'Jurisdiction Wide' into the first precinct's label
    cell (both styles) or fuse the final two labels into one
    '<precinct> ABSENTEE' cell, which is expanded before pairing."""
    cdir = os.path.join(CACHE_DIR, _HICKORY_DIR)
    entries = []
    _title_kw = re.compile(
        r"PARTY\)|AUDITOR|REPRESENTATIVE|SENATOR|SENATE|\bDISTRICT\s+\d+|"
        r"STATE HOUSE|COMMISSION|"
        r"CLERK|JUDGE|ATTORNEY|RECORDER|AMENDMENT|TREASURER|COLLECTOR|"
        r"PROSECUTING|PRESIDING", re.I)
    _hdr_frag = re.compile(
        r"^(?:Reg\.\s*)?Voters$|^Total\s*Votes$|^Reg\.$|^Total$|^Votes$|"
        r"^%?\s*Turnout$|^Ballots\s*Cast$", re.I)
    _num = re.compile(r"^[\d,]+$")
    for page, (office, dist, party) in _HICKORY_TITLES.items():
        md = open(os.path.join(cdir, f"p{page:03d}.md")).read()
        md = md.replace("&#x27;", "'")
        tbl = re.search(r"<table[^>]*>.*?</table>", md, re.S)
        if not tbl:
            print(f"  [!] p{page:03d}: no table")
            continue
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(0))
        cellsr = lambda r: [re.sub(r"<[^>]+>", "", c).strip()
                            for c in re.findall(r"<td[^>]*>(.*?)</td>", r)]
        # header band: title/candidate rows precede the first numeric row
        measure = bool(re.search(r"Amendment|Proposition", office))
        cands = []
        for r in rows:
            cs = cellsr(r)
            if any(_num.match(c) or "%" in c for c in cs[1:] if c):
                break  # first data row
            for c in cs:
                if not c or _hdr_frag.match(c) \
                        or c.lower() == "jurisdiction wide" \
                        or _title_kw.search(c) and "%" not in c:
                    continue
                cands.append(_ocr_cand(_join_name(c), measure=measure))
        cands = [c for c in cands if c and (not measure
                                            or c.lower() in ("yes", "no"))]
        if not cands:
            print(f"  [!] p{page:03d}: no candidates")
            continue
        n = len(cands)
        # ordered (label, number) rows; title rows carry an empty label and
        # are dropped here
        seq = []
        for r in rows:
            cells = cellsr(r)
            lab = cells[0].strip()
            if not lab or _hdr_frag.match(lab):
                continue
            nums = [int(t.replace(",", ""))
                    for t in _sovc_num_tokens(cells[1:]) if _num.match(t)]
            seq.append((lab, cells[1:], nums))
        # alignment style (per parse_hickory docstring)
        shifted = doubled = False
        for lab, cs, nums in seq:
            if lab.lower() == "jurisdiction wide" and nums:
                if len(cs) > 1 and cs[0] and not _num.match(cs[0]) \
                        and "%" not in cs[0]:
                    doubled = True  # 'Jurisdiction Wide | SKYLINE | nums...'
                else:
                    shifted = True
                break
        labs, nums_all = [], []
        for lab, cs, nums in seq:
            if lab.lower() == "jurisdiction wide":
                if not nums:
                    continue  # bare banner (aligned pages)
                if doubled:
                    # the precinct name rides in the next cell over
                    name = cs[0].strip() if cs and not _num.match(cs[0]) \
                        and "%" not in cs[0] else None
                    if not name:
                        print(f"  [!] p{page:03d}: doubled-label row without "
                              f"precinct name")
                        continue
                    lab, nums = name, nums
            else:
                m = re.match(r"(?i)^(.+?)\s+ABSENTEE$", lab)
                if m and shifted:
                    # OCR fused the last two label rows; absentee numbers
                    # follow on the next OCR row
                    labs.append(m.group(1).strip())
                    nums_all.append(nums)
                    labs.append("ABSENTEE")
                    continue
            labs.append(lab)
            nums_all.append(nums)
        if shifted and any(not nu for nu in nums_all[:-1]):
            print(f"  [!] p{page:03d}: shifted page has a numberless row")
        pairs = (list(zip(labs[1:], nums_all)) if shifted
                 else list(zip(labs, nums_all)))
        for label, nums in pairs:
            low = label.strip().lower()
            if not low or low.startswith("total") \
                    or low == "jurisdiction wide":
                continue
            if low.endswith("absentee") and len(low) > 8:
                m = re.match(r"(?i)^(.+?)\s+ABSENTEE$", label.strip())
                label = "ABSENTEE" if (m and shifted) else label.strip()
            if low == "absentee" and nums and nums[0] > 0:
                # reg != 0: restates the jurisdiction total, not a counting
                # group
                print(f"  [!] p{page:03d} {label}: absentee row has reg "
                      f"{nums[0]}, skipped")
                continue
            if not nums:
                print(f"  [!] p{page:03d} {label}: no numbers")
                continue
            # reg/total/vote alignment
            if len(nums) == n + 2:
                v = nums[2:]
            elif len(nums) == n + 1:
                v = (nums[2:] if sum(nums[2:]) <= nums[1] and nums[1] > 0
                     else nums[1:])
            elif len(nums) == n:
                v = nums
            else:
                print(f"  [!] p{page:03d} {label}: bad shape nums={nums}")
                continue
            v = list(v)
            if len(v) == n - 1:
                # OCR dropped the last candidate's (votes, %) pair; its votes
                # are the Total Votes cell minus the surviving votes
                total = nums[1] if nums else sum(v)
                gap = total - sum(v)
                v.append(max(0, gap))
                if gap:
                    print(f"  [~] p{page:03d} {label}: last candidate "
                          f"recovered as {gap} (OCR drop)")
            if len(v) < n:
                v += [0] * (n - len(v))
            entries.append([office, dist, party, label.strip(),
                            list(zip(cands, v))])
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Henry -- MO SOS SOVC ("Statement of Votes Cast"), one contest per page, but
# with the turnout columns (Reg. Voters | Ballots Cast | % Turnout)
# INTERLEAVED before the contest's Reg. Voters/Total Votes columns, and many
# pages the OCR model collapsed to a hallucinated header-only table
# (regardless of job size).  For those pages the printed table is recovered
# from 2026/henry_transcripts/pNN.txt (TSV, transcribed visually from the
# rendered page images -- the pages are clean; only the OCR run is broken).
# Data-row digits therefore come out as: [Turnout reg, Ballots Cast,
# Contest reg, Total Votes, <one per candidate>] -- with % cells and '-'
# cells dropped, and Absentee rows often missing the turnout reg.
# ---------------------------------------------------------------------------

_HENRY_DIR = "Henry_MO_Election_Results_08_04_26"
_HENRY_FALLBACK_TITLES = {53: "Constitutional Amendment No. 5"}
# Benton's four SOVC amendment tables have garbled title rows (the
# amendment name leaked into the header cells); the page order is on the
# report itself (pp. 25-28 printed in the markdown header row)
_BENTON_FALLBACK_TITLES = {
    "20": "State Senator District 28 (Democratic Party)",
    "25": "Constitutional Amendment No. 1",
    "26": "Constitutional Amendment No. 2",
    "27": "Constitutional Amendment No. 4",
    "28": "Constitutional Amendment No. 5",
}
_HENRY_ROW_TOTALS = ("jurisdiction", "jurisdiction wide", "total", "-")
_HENRY_BOILER = ("statement of votes cast", "primary election", "election "
                 "results", "henry county", "official", "turnout",
                 "reg. voters", "ballots cast", "% turnout", "total votes",
                 "jurisdiction", "wide", "misouri", "missouri")


_HENRY_TDIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "2026",
    "henry_transcripts")


def _henry_clean_label(cells):
    label = " ".join(html.unescape(c.strip()) for c in cells if c.strip())
    label = re.sub(r"\\n", " ", label)   # PaddleOCR literal "\n" escapes
    label = re.sub(r"(?i)jurisdiction\s*wide\s*", "", label).strip()
    return label


def _henry_transcript(page):
    """[(title, cands, [(block, rowlabel, value-cells)]), ...] from
    2026/henry_transcripts/pNN.txt — split into contests at the TITLE2
    marker — or None when no transcript exists.  Values are kept verbatim
    (percent strings included); the caller drops non-count cells."""
    for name in (f"p{page:02d}.txt", f"p{page:03d}.txt", f"p{page}.txt"):
        path = os.path.join(_HENRY_TDIR, name)
        if os.path.exists(path):
            break
    else:
        return None
    contests = []
    title = cands = None
    rows = []
    for raw in open(path):
        parts = [c.strip() for c in raw.rstrip("\n").split("\t")]
        if not any(parts) or (len(parts) == 1 and parts[0].startswith("=")):
            continue
        if parts[0] in ("TITLE", "TITLE2") and len(parts) > 1:
            if title is not None:
                contests.append((title, cands or [], rows))
            title, cands, rows = parts[1], None, []
        elif parts[0] == "CANDS" and len(parts) > 1:
            cands = [c for c in parts[1:] if c]
        elif len(parts) >= 4:
            rows.append((parts[1], parts[2],
                         [c for c in parts[3].split(",") if c]))
    if title is None:
        return None
    contests.append((title, cands or [], rows))
    return contests


def _henry_ocr_page(path):
    """[(title, cands, [(block, rowlabel, value-cells)]), ...] from the OCR
    markdown of one page — one contest per table (two-party pages carry
    two).  Title cells ending in "PARTY)" are banner fragments that OCR
    mis-filed as candidate headers; they are moved back into the title.
    A garbled title is returned as-is; the caller applies the fallback
    map.  Returns [] when nothing is usable (collapsed OCR)."""
    contests = []
    for tbl in _buchanan_tables(path):
        tparts, hdr_i = [], None
        for i, cells in enumerate(tbl):
            if any(c.strip() in ("Reg. Voters", "Total Votes")
                   for c in cells):
                hdr_i = i
                break
            tparts += [c.strip() for c in cells if c.strip()]
        if hdr_i is None or not tparts:
            continue
        # header-row cells: party fragments go back into the title, "Over/
        # Under votes" columns are dropped, other non-numeric text is a
        # candidate.  Candidates are taken from the header row FIRST
        # (column order = printed order); pre-header cells (banner rows
        # OCR merged into the table) are only fallbacks, appended after —
        # taking those first would silently flip vote attribution.
        drop = ("over votes", "under votes", "over", "under")

        def _scan_hdr(cells):
            got, pcells = [], []
            for c in cells:
                c = c.strip()
                if (not c or c.lower() in _HENRY_BOILER
                        or c.lower() in drop
                        or re.fullmatch(r"[\d,.%-]+", c)):
                    continue
                if re.search(r"PARTY\)$", c) or \
                        re.search(r"\bAMENDMENT\b", c, re.I):
                    # party banner fragments / amendment titles that OCR
                    # mis-filed as candidate headers
                    pcells.append(c)
                    continue
                if c.lower() not in {x.lower() for x in got}:
                    got.append(c)
            return got, pcells

        cands, party_cells = _scan_hdr(tbl[hdr_i])
        # banner row(s) above the header: a candidate name OCR also left
        # there (dup of a header cell) stays a candidate; any banner cell
        # NOT confirmed by the header row is a title fragment (e.g. p20's
        # 'STATE SENATOR DISTRICT' colspan residue) and must not inflate
        # the candidate list.
        banner_c, banner_party = [], []
        if hdr_i > 0:
            banner_c, banner_party = _scan_hdr(tbl[hdr_i - 1])
        cand_low = {x.lower() for x in cands}
        if not cands and banner_c:
            # fully merged banner: the banner row carries the whole header
            cands = banner_c
            party_cells = party_cells + banner_party
        else:
            for c in banner_c:
                if c.lower() not in cand_low:
                    party_cells.append(c)
            party_cells = party_cells + banner_party
        # title: remaining non-boilerplate, non-candidate header cells.
        # Recompute cand_low against the FINAL candidate list so demoted
        # banner duplicates are still hidden from the title.
        cand_low = {c.lower() for c in cands}
        tp = [t for t in tparts
              if t and t.lower() not in _HENRY_BOILER
              and t.lower() not in cand_low] + party_cells
        seen, tp2 = set(), []
        for t in tp:                     # tparts and the banner often repeat
            if t and t.lower() not in seen:
                tp2.append(t)
                seen.add(t.lower())
        title = " ".join(tp2)
        rows = []
        prec, last_low = None, ""
        for cells in tbl[hdr_i + 1:]:
            vals = []
            for c in cells[1:]:
                c = c.strip()
                if c == "-":
                    vals.append(c)  # keep as a column placeholder
                    continue
                if not c:
                    continue
                # digits/percents, incl. OCR-merged cells like "24 100.00%"
                # and "0 -" (vote + dash percent)
                if re.fullmatch(r"[\d,.%-]+", c) or \
                        re.fullmatch(r"(?=.*\d)[\d,.%\-\s]+", c):
                    vals.append(c)
            if not vals:
                label = _henry_clean_label(cells)
                prec = None if label.lower() in _HENRY_ROW_TOTALS else label
                continue
            # some tables put the precinct label directly on the value
            # row (no separate banner); promote it when so.  The label can
            # also be merged with 'Jurisdiction Wide' or split across the
            # first two cells (OCR splits the banner), so scan both.
            rowkey = ""
            for c in cells[:2]:
                c = c.strip()
                if not c or c.lower() == "normal":
                    continue  # 'ABSENTEE' stays an own block label
                if re.fullmatch(r"[\d,.%\-\s]*", c):
                    continue  # numeric/perc/sublabel cell
                rk = _henry_clean_label([c])
                if rk and rk.lower() not in _HENRY_ROW_TOTALS:
                    rowkey = rk
                    break
            if rowkey:
                # A value row whose own cell is 'Absentee' right after the
                # same block's 'Normal' subrow is a SUBROW of that block,
                # not a new precinct: Benton-style tables print
                # Normal/Absentee subrows under the ABSENTEE banner too,
                # and promoting the cell would create a phantom 'Absentee'
                # precinct and leave the real ABSENTEE block with the
                # zero row only.
                if rowkey.lower() == "absentee" and last_low == "normal":
                    rowkey = ""
            if rowkey:
                prec = rowkey.rstrip("/")
                # own-cell label: pass it as both block and sublabel so the
                # walk never mistakes a generic cells[0] ('Jurisdiction
                # Wide' — OCR puts the precinct name in the next column)
                # for a county-total row
                rows.append((prec, prec.lower(), vals))
                last_low = prec.lower()
            else:
                rows.append((prec or "?", cells[0].strip().lower(), vals))
                last_low = cells[0].strip().lower()
        if rows:
            # Hickory-style SOVC row-shift: the value block moved UP one
            # row, so the county banner row carries the first precinct's
            # votes and every label gets its neighbour's numbers, with the
            # county Total landing on the ABSENTEE row.  Detected when the
            # banner-label row carries digits and the next row is not a
            # Normal/Absentee subrow (i.e. a single-value-row table).
            # Re-pair positionally: the values of row i belong to the
            # label of row i+1; the last label row (Total) ends up without
            # its own values and its numbers are dropped with the county
            # banner.
            if rows[0][1].strip() in _HENRY_ROW_TOTALS \
                    and len(rows) > 2 \
                    and rows[1][1].strip() not in ("normal", "absentee") \
                    and rows[1][2] != rows[0][2] \
                    and any(re.fullmatch(r"[\d,]+", c.strip())
                            for c in rows[0][2] if c.strip()):
                rows = [(rows[i + 1][0] or rows[i][0], rows[i + 1][1],
                         rows[i][2]) for i in range(len(rows) - 1)]
            prefix = 5
            for pfx in (5, 2):
                for _, _, vals in rows:
                    if not any(v.strip().replace(",", "") and
                               re.fullmatch(r"[\d,]+",
                                            v.strip().replace(",", ""))
                               for v in vals):
                        continue
                    if _henry_row_votes(vals, len(cands), pfx) is not None:
                        prefix = pfx
                        break
                else:
                    continue
                break
            # turnout/contest prefix columns: Benton-style compact tables
            # print [Reg. Voters, Total Votes, candidates…] (2 columns)
            # where Chariton-style SOVC prints [Reg., Ballots, %Turnout,
            # Reg., TV] (5); detected against the first digit-bearing row
            contests.append((title, cands, rows, prefix))
    return contests


def _henry_row_votes(vals, n, prefix=5):
    """Candidate votes from one Dominion SOVC data row via its exact
    column layout [prefix cells..., v1, p1, …, vn, pn, over, under] with
    '-' cells kept as placeholders.  The prefix is the turnout+contest
    columns measured against the table's header row: 5 for [Reg. Voters,
    Ballots Cast, % Turnout, Reg. Voters, Total Votes], 2 for compact
    tables [Reg. Voters, Total Votes].  Votes are read by position and
    validated against Total Votes, so turnout and trailing Over/Under
    cells can never shift the slice; when the OCR truncated the last
    candidate pair, its votes are recovered as Total Votes minus the
    others (exact for a single truncation).  Returns the n votes, or
    None when the row does not fit."""
    cell = (vals[prefix - 1] if len(vals) >= prefix else "").strip()
    if not re.fullmatch(r"[\d,]+", cell):
        return None
    tv = int(cell.replace(",", ""))
    votes, i = [], prefix
    while i < len(vals):
        cell = (vals[i] or "").strip().replace(",", "")
        m = (re.fullmatch(r"(\d+)\s+\d[\d.,]*%", cell)   # merged "24 100.00%"
             or re.fullmatch(r"(\d+)\s+[-%]+", cell))    # merged "0 -" (dash %)
        if m:
            votes.append(int(m.group(1)))
        elif re.fullmatch(r"\d+", cell):
            votes.append(int(cell))
        elif cell in ("", "-"):
            votes.append(0)  # dash at a vote column = 0 votes
        else:
            return None  # a percent at a vote column → misaligned row
        if len(votes) == n:
            break
        i += 2
    if not votes:
        return None
    if len(votes) == n - 1 and i >= len(vals):
        votes.append(tv - sum(votes))  # single candidate pair truncated
    if len(votes) != n or sum(votes) != tv:
        return None
    return votes


def _henry_pairs(vals):
    """Fallback: (vote, %) pairs — candidate votes print beside their
    percent, so a digit whose next cell is a % is a vote column.  The
    leading "% Turnout" pair is dropped by taking the tail; bare trailing
    Over/Under cells carry no % and drop out.  Callers slice [-n:]."""
    pairs = []
    for i, v in enumerate(vals):
        v = (v or "").strip()
        if not v or v == "-":
            continue
        m = re.fullmatch(r"(\d[\d,]*)\s+\d[\d.,]*%", v)  # merged vote+pct
        if m:
            pairs.append(int(m.group(1).replace(",", "")))
        elif (re.fullmatch(r"[\d,]+", v) and i + 1 < len(vals)
                and re.fullmatch(r"\d[\d.,]*%", (vals[i+1] or "").strip())):
            pairs.append(int(v.replace(",", "")))
    return pairs


def _henry_digits(vals):
    """int vote cells from one row's value cells — drops percents, splits
    OCR-merged cells like "24 100.00%", strips thousands commas."""
    out = []
    for v in vals:
        v = (v or "").strip()
        if not v or v == "-":
            continue
        m = re.fullmatch(r"(\d[\d,]*)\s+\d[\d.,]*%", v)
        if m:
            out.append(int(m.group(1).replace(",", "")))
        elif not v.endswith("%") and re.fullmatch(r"[\d,]+", v):
            out.append(int(v.replace(",", "")))
    return out


def parse_henry(county, cdir_name=None, use_transcripts=True):
    cdir = os.path.join(CACHE_DIR, cdir_name or _HENRY_DIR)
    fb_titles = (_BENTON_FALLBACK_TITLES if cdir_name
                 and "Benton" in cdir_name else _HENRY_FALLBACK_TITLES)
    entries = []
    pages = sorted(int(m.group(1)) for m in
                   (re.match(r"p(\d+)\.md$", f)
                    for f in os.listdir(cdir)) if m)
    for page in pages:
        path = os.path.join(cdir, f"p{page:03d}.md")
        source = "transcript"
        contests = _henry_transcript(page) if use_transcripts else None
        if contests is None:
            source = "ocr"
            contests = _henry_ocr_page(path)
        # OCR titles are often garbled: substitute the known fallback title
        # (fb_titles is keyed by page number).  Entries are
        # (title, cands, rows) or (title, cands, rows, prefix).
        for i, ent in enumerate(contests):
            ent_cands, rows, rest = ent[1], ent[2], ent[3:]
            title = re.sub(r"\s+", " ", ent[0]).strip()
            garbled = not (re.search(r"\bPARTY\)", title)
                           or re.search(r"AMENDMENT", title, re.I))
            garbled = garbled or (str(page) in fb_titles
                                  and re.match(r"^\d+\s*\(", title))
            if str(page) in fb_titles and garbled:
                fb = fb_titles[str(page)]
                if re.search(r"AMENDMENT", fb, re.I):
                    # amendment pages mis-print the title as a candidate:
                    # force the YES/NO header
                    cands = ["YES", "NO"]
                else:
                    # contest title garbled only: keep the real candidates
                    cands = ent_cands
                contests[i] = (fb, cands, rows, *rest)
                source += "+fb"
            elif garbled:
                contests[i] = ("", ent_cands, rows, *rest)
        for ent in contests:
            title, cands, trows = ent[0], ent[1], ent[2]
            prefix = ent[3] if len(ent) > 3 else 5
            if not title.strip():
                print(f"  [!] p{page:03d}: unrecognized title")
                continue
            if re.search(r"statement of votes|election results|page \d+",
                         title, re.I):
                continue  # boilerplate-only fragment
            if re.search(r"number of precincts|precincts reporting",
                         title, re.I):
                continue  # county-total summary table, not precinct detail
            office, party = _title_cell(title)
            if re.fullmatch(r"Constitutional Amendment No\.\s*\d+", office,
                            re.I):
                no = re.search(r"(\d+)", office).group(1)
                office = f"Constitutional Amendment {no}"
            office, dist = normalize_office(office)
            office = re.sub(r"(Division\s+)Ii(i?)$", r"\1I\2", office)
            measure = bool(re.search(r"Amendment", office or ""))
            cands = [c for c in
                     (_ocr_cand(c, measure=measure) for c in cands) if c]
            if measure:
                cands = [c for c in cands if c.lower() in ("yes", "no")]
            if not cands:
                print(f"  [!] p{page:03d} ({source}): no candidates: "
                      f"{title!r}")
                continue
            n = len(cands)
            # walk data rows: (block, rowlabel, value-cells).  'Total' and
            # 'Jurisdiction Wide' blocks are county-level and skipped; the
            # 'ABSENTEE' block becomes an Absentee precinct (its row with
            # digits).  OCR often shifts the printed numbers of a block UP
            # onto the precinct's banner row, leaving the Normal row with
            # zeros; when both exist keep the richer row.
            blocks = {}
            prec = None
            for block, sublabel, vals in trows:
                if block == "TITLE2":
                    prec = None
                    continue
                b = _henry_clean_label([block])
                if b.lower() in _HENRY_ROW_TOTALS:
                    prec = None
                    continue
                low = sublabel.strip().lower()
                if low in _HENRY_ROW_TOTALS or low in ("total votes",
                                                       "reg. voters"):
                    continue  # county/summary row, not precinct detail
                digits = _henry_digits(vals)
                if block == "?" and low in ("normal", "absentee") and digits:
                    continue  # county-total subrow, banner label lost by OCR
                if low not in ("normal", "absentee"):
                    if not digits:
                        prec = b.rstrip("/")
                        continue  # all-dash banner/name row
                    low = "normal"
                prec = b.rstrip("/")
                blocks.setdefault(prec, {})
                # candidate votes: exact column layout first (validated
                # against Total Votes, so turnout / Over/Under cells can
                # never shift the slice), then vote/percent pairs (useful
                # when OCR dropped a dash placeholder), then the risky
                # digit slice as a last resort
                cand = _henry_row_votes(vals, n, prefix)
                if cand is None:
                    pairs = _henry_pairs(vals)
                    cand = pairs[-n:] if len(pairs) >= n else None
                if cand is None:
                    if not n <= len(digits) <= n + 4:
                        print(f"  [!] p{page:03d} ({source}) {prec} {low}: "
                              f"bad shape digits={digits}")
                        continue
                    cand = digits[-n:]
                cur = blocks[prec].get(low)
                if cur is None or sum(cur) <= sum(cand):
                    blocks[prec][low] = cand
            for prec, tallies in blocks.items():
                nv = tallies.get("normal") or [0] * n
                av = tallies.get("absentee") or [0] * n
                votes = list(zip(cands, [nv[k] + av[k] for k in range(n)]))
                entries.append([office, dist, party, prec, votes])
    if not entries:
        print("  [!] no entries parsed")
        return []
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Cole -- Dominion "Abstract Report (Percentages)" (33 pages, one contest
# per page, compact prefix-2 rows [Reg. Voters, Total Votes, v1, p1, ...]).
# ---------------------------------------------------------------------------

_COLE_DIR = "Cole_MO_August_4_2026_Turnout_and_Abstract"

# Pages whose contest title prints only as an image (seal-box) or whose
# banner is garbled/incomplete: titles read from the rendered PDF pages
# (pdftoppm), cross-checked against the certified candidate sets.
_COLE_FALLBACK_TITLES = {
    3: "US REPRESENTATIVE DISTRICT 5 (REPUBLICAN PARTY)",
    5: "STATE REPRESENTATIVE DISTRICT 59 (REPUBLICAN PARTY)",
    7: "CIRCUIT JUDGE CIRCUIT 19 DIVISION 2 (REPUBLICAN PARTY)",
    9: "ASSOCIATE CIRCUIT JUDGE CIRCUIT 19 DIVISION 6 (REPUBLICAN PARTY)",
    17: "3RD WARD COMMITTEE MAN (REPUBLICAN PARTY)",
    18: "3RD WARD COMMITTEE WOMAN (REPUBLICAN PARTY)",
    20: "JEFFERSON TOWNSHIP COMMITTEE MAN (REPUBLICAN PARTY)",
    21: "MOREAU TOWNSHIP COMMITTEE MAN (REPUBLICAN PARTY)",
}

# banner cells that look like contest titles (others may be candidate-name
# fragments that OCR split off the header row, e.g. p28 'RANDALL (RANDY)')
_COLE_TITLE_TOKEN = re.compile(
    r"PARTY\)|AMENDMENT|DISTRICT|REPRESENTATIVE|SENATOR|COMMITTEE|"
    r"PROPOSITION|JUDGE|CIRCUIT|DIVISION|COLLECTOR|AUDITOR|CLERK|"
    r"COMMISSIONER|ATTORNEY|RECORDER|CORONER|SURVEYOR|ASSESSOR",
    re.I)


# Pages 3 and 4 of the PDF print 6 candidate columns but the OCR captured
# only the first 3 pairs of value cells (a scan-width loss).  Votes below
# were transcribed from rendered page images (pdftoppm, /tmp/cole/) keyed by
# printed precinct label; vectors are in the page's header-candidate order
# [BRAD PATTY, BRETT HUEFFMEIER, TAYLOR BURKS, RICK BRATTIN, BERTON A. KNOX,
# MICAH BEEBE] on p3 and [JAKE VOGEL, DERRICK SPICER, LISA THOMAS,
# AMBER BUCKLES, RUDY VEIT, IKE SKELTON] on p4.  Every column sums to the
# printed Total, and the Total row matches the certified county file
# (1403/1792/4300/6266/304/1116 and 4641/2594/1206/383/6439/1920).
_COLE_IMAGE_FIXES = {
    3: {
        "Ward 1 Precinct 1": [26, 25, 51, 70, 7, 32],
        "Ward 1 Precinct 2": [34, 60, 119, 153, 14, 24],
        "Ward 1 Precinct 3": [48, 63, 159, 172, 5, 29],
        "Ward 2 Precinct 1": [16, 16, 41, 33, 3, 13],
        "Ward 2 Precinct 2": [38, 50, 91, 106, 13, 30],
        "Ward 2 Precinct 3": [24, 24, 41, 73, 4, 14],
        "Ward 3 Precinct 1": [38, 63, 133, 234, 12, 43],
        "Ward 3 Precinct 2": [18, 54, 108, 180, 8, 24],
        "Ward 3 Precinct 3": [33, 63, 164, 242, 10, 43],
        "Ward 4 Precinct 1": [72, 102, 212, 266, 18, 73],
        "Ward 4 Precinct 2": [73, 90, 296, 352, 20, 52],
        "Ward 5 Precinct 1": [48, 63, 162, 154, 13, 47],
        "Ward 5 Precinct 2": [44, 51, 148, 175, 12, 41],
        "Ward 5 Precinct 3": [27, 22, 46, 63, 3, 14],
        "CLARK / BRAZITO": [46, 48, 129, 274, 11, 30],
        "CLARK / EUGENE": [27, 30, 84, 168, 11, 17],
        "JEFFERSON / APACHE FLATS": [78, 82, 186, 307, 14, 55],
        "JEFFERSON / CC GENERAL": [49, 66, 164, 285, 14, 54],
        "JEFFERSON / EXPRESSWAY RENTALS": [8, 16, 54, 73, 1, 21],
        "JEFFERSON / JC FAIRGROUNDS": [47, 64, 128, 238, 10, 32],
        "JEFFERSON / SCOTT STATION": [81, 80, 170, 378, 8, 46],
        "LIBERTY / TAOS": [107, 109, 314, 444, 18, 62],
        "MARION / CENTERTOWN": [27, 45, 81, 149, 6, 13],
        "MARION / ST. MARTINS": [67, 92, 198, 301, 14, 41],
        "MOREAU / LOHMAN": [13, 14, 62, 143, 5, 11],
        "MOREAU / RUSSELLVILLE": [47, 41, 115, 160, 4, 23],
        "OSAGE / ST. THOMAS": [15, 44, 82, 95, 4, 29],
        "OSAGE / WARDSVILLE": [80, 96, 269, 390, 17, 37],
        "ABSENTEE": [172, 219, 493, 588, 25, 166],
    },
    4: {
        "Ward 1 Precinct 1": [53, 45, 20, 16, 72, 32],
        "Ward 1 Precinct 2": [117, 63, 30, 7, 177, 65],
        "Ward 1 Precinct 3": [158, 96, 39, 9, 204, 45],
        "Ward 2 Precinct 1": [34, 19, 18, 4, 47, 18],
        "Ward 2 Precinct 2": [94, 52, 35, 9, 136, 40],
        "Ward 2 Precinct 3": [56, 41, 18, 4, 46, 36],
        "Ward 3 Precinct 1": [190, 91, 41, 8, 217, 56],
        "Ward 3 Precinct 2": [131, 69, 31, 19, 114, 61],
        "Ward 3 Precinct 3": [183, 99, 46, 18, 216, 69],
        "Ward 4 Precinct 1": [197, 155, 65, 20, 308, 92],
        "Ward 4 Precinct 2": [322, 138, 74, 23, 342, 86],
        "Ward 5 Precinct 1": [166, 69, 44, 14, 198, 61],
        "Ward 5 Precinct 2": [154, 76, 36, 15, 193, 55],
        "Ward 5 Precinct 3": [51, 37, 17, 1, 53, 27],
        "CLARK / BRAZITO": [134, 107, 49, 10, 227, 87],
        "CLARK / EUGENE": [78, 53, 27, 8, 137, 62],
        "JEFFERSON / APACHE FLATS": [236, 119, 66, 32, 251, 90],
        "JEFFERSON / CC GENERAL": [174, 113, 64, 19, 219, 117],
        "JEFFERSON / EXPRESSWAY RENTALS": [47, 29, 12, 5, 88, 20],
        "JEFFERSON / JC FAIRGROUNDS": [182, 96, 45, 16, 173, 74],
        "JEFFERSON / SCOTT STATION": [259, 117, 70, 9, 285, 109],
        "LIBERTY / TAOS": [308, 183, 62, 13, 485, 142],
        "MARION / CENTERTOWN": [99, 63, 19, 9, 126, 44],
        "MARION / ST. MARTINS": [203, 139, 50, 26, 253, 113],
        "MOREAU / LOHMAN": [91, 48, 21, 4, 81, 25],
        "MOREAU / RUSSELLVILLE": [101, 93, 33, 8, 144, 52],
        "OSAGE / ST. THOMAS": [44, 15, 13, 3, 226, 13],
        "OSAGE / WARDSVILLE": [235, 154, 38, 17, 477, 97],
        "ABSENTEE": [544, 215, 123, 37, 944, 132],
    },
    # p28's candidate header is truncated OCR ('LANGKRAEH'); votes came out
    # of the OCR walk fine, only the name is repaired via _COLE_CAND_FIX.
}


def _cole_fix_title(title):
    title = re.sub(r"LIBERTIARIAN", "LIBERTARIAN", title, flags=re.I)
    title = re.sub(r"\bCOMMITTEE (MAN|WOMAN)\b", r"COMMITTEE\1", title,
                   flags=re.I)
    title = re.sub(r"\b(\d+)(ST|ND|RD|TH)\b",
                   lambda m: m.group(1) + m.group(2).lower(), title, re.I)
    title = re.sub(r"CIRCUITS OUDGE", "CIRCUIT JUDGE", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip()


def parse_cole(county):
    cdir = os.path.join(CACHE_DIR, _COLE_DIR)
    entries = []
    pages = sorted(int(m.group(1)) for m in
                   (re.match(r"p(\d+)\.md$", f)
                    for f in os.listdir(cdir)) if m)
    for page in pages:
        path = os.path.join(cdir, f"p{page:03d}.md")
        md = html.unescape(open(path).read())
        tbls = _buchanan_tables(path)
        if not tbls:
            # p001: turnout-only page, no contest
            continue
        tbl = tbls[0]
        # title: centered <div> text outside the table (before or after
        # it), else banner rows above the header row, else the fallback map
        outside = re.sub(r"<table.*?</table>", " ", md, flags=re.S)
        tparts = []
        for m in re.finditer(r"<div[^>]*>(.*?)</div>", outside, re.S):
            if "<img" not in m.group(1):
                tt = re.sub(r"<[^>]+>", " ", m.group(1))
                tt = " ".join(tt.split())
                if tt and tt.lower() != "jurisdiction wide":
                    tparts.append(tt)
        hdr_i = None
        hdr = []
        for i, cells in enumerate(tbl):
            if "Reg. Voters" in cells:
                hdr_i = i
                break
        if hdr_i is None:
            hdr_i = 0
        for i, cells in enumerate(tbl[:hdr_i]):
            for c in cells[1:]:
                if c and _COLE_TITLE_TOKEN.search(c):
                    tparts.append(c)
        title = _cole_fix_title(" ".join(tparts))
        if int(page) in _COLE_FALLBACK_TITLES:
            title = _COLE_FALLBACK_TITLES[int(page)]
        if not title.strip():
            print(f"  [!] p{page:03d}: unrecognized title")
            continue
        # candidates: header cells after Reg. Voters + Total Votes, stopping
        # at a repeated 'Total Votes' (p6 prints an OCR-doubled column)
        cands = []
        if hdr_i is not None:
            seen_tv = 0
            for c in tbl[hdr_i][1:]:
                if c == "Total Votes":
                    seen_tv += 1
                    if seen_tv > 1:
                        break
                elif c and c not in ("Reg. Voters",):
                    cands.append(c)
        office, party = _title_cell(title)
        if re.fullmatch(r"Constitutional Amendment No\.\s*\d+", office, re.I):
            office = ("Constitutional Amendment "
                      + re.search(r"(\d+)", office).group(1))
        office, dist = normalize_office(office)
        # certified files name judges as office "Circuit Judge" with the
        # circuit/division in the district column
        jm = re.match(r"(Associate )?Circuit Judge - Circuit (\d+) "
                      r"Division (\S+)$", office)
        if jm:
            office = ("Associate " if jm.group(1) else "") + "Circuit Judge"
            dist = f"{jm.group(2)} Division {jm.group(3)}"
        measure = bool(re.search(r"Amendment", office or ""))
        cands = [c for c in
                 (_ocr_cand(c, measure=measure) for c in cands) if c]
        _COLE_CAND_FIX = {"Langkraeh": "Randall (Randy) Langkraehr",
                          "Mike Bernskoett Er": "Mike Bernskoetter"}
        cands = [_COLE_CAND_FIX.get(c, c) for c in cands]
        if measure:
            cands = [c for c in cands if c.lower() in ("yes", "no")]
        if not cands:
            print(f"  [!] p{page:03d}: no candidates: {title!r}")
            continue
        n = len(cands)
        # pages whose value columns were lost/truncated by the OCR: use the
        # image-transcribed vectors instead
        if int(page) in _COLE_IMAGE_FIXES:
            rows = [[office, dist, party, prec,
                     list(zip(cands, vec))]
                    for prec, vec in _COLE_IMAGE_FIXES[int(page)].items()]
            entries.extend(rows)
            print(f"  [~] p{page:03d} {title!r}: {len(rows)} precinct rows "
                  f"(image fix)")
            continue
        # precinct rows: label col 0, value cols [Reg, TV, v1, p1, ...]
        # (prefix 2).  A label with no digits is a continuation of the
        # previous label ('JEFFERSON / EXPRESSWAY' + 'RENTALS'); if the
        # digits already rode an emitted row above (the usual order here),
        # the tail label is appended to that row instead.
        rows = []
        buf = ""
        just_emitted = False
        for cells in tbl[hdr_i + 1:]:
            lab = cells[0].strip()
            vals = cells[1:]
            digits = _henry_digits(vals)
            low = lab.lower()
            if low in ("total", "jurisdiction wide"):
                just_emitted = False
                continue
            if lab and not digits:
                if just_emitted and rows:
                    rows[-1][3] = (rows[-1][3] + " " + lab).strip()
                else:
                    buf = (buf + " " + lab).strip() if buf else lab
                just_emitted = False
                continue
            if not lab and not digits:
                continue
            prec = (buf + " " + lab).strip() if buf else lab
            buf = ""
            cand = _henry_row_votes(vals, n, 2)
            if cand is None:
                pairs = _henry_pairs(vals)
                cand = pairs[-n:] if len(pairs) >= n else None
            if cand is None:
                print(f"  [!] p{page:03d} {prec}: bad shape "
                      f"digits={digits}")
                just_emitted = False
                continue
            rows.append([office, dist, party, prec, list(zip(cands, cand))])
            just_emitted = True
        if rows:
            entries.extend(rows)
        print(f"  [~] p{page:03d} {title!r}: {len(rows)} precinct rows")
    if not entries:
        print("  [!] no entries parsed")
        return []
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Lawrence -- ES&S "Election Results" pages: one contest per 1-2 pages,
# each precinct a GROUP of rows (printed label row, then 'Normal' +
# 'Absentee' value rows), column layout
# [Reg, Ballots Cast, % Turnout, Reg, Total Votes, v1, p1, ...].  Absentee
# votes county-wide live in the printed 'ABSENTEE' group (all other
# Absentee rows are zeros); its votes ride a row labeled 'Absentee'.
# County 'Total' label + value rows close each contest page.
# OCR artifacts repaired here: labels shifted up one row relative to their
# values (a 'Normal' row left empty while the following 'Absentee' row
# carries the group's votes), the first group's label merged into the
# title-row cells ('Jurisdiction | Wide 01- SW AUORA'), and the turnout
# trio is precinct-constant across the whole report, which serves as a
# per-precinct sanity check.
# ---------------------------------------------------------------------------

_LAWRENCE_DIR = "Lawrence_MO_August_2026_Precincts_08_04_26"
_LAWRENCE_LABEL = re.compile(r"^(\d+)\s*-\s*(\S.*)$")
_LAWRENCE_TITLE_WORD = re.compile(
    r"PARTY|DISTRICT|JUDGE|COMMISSIONER|CLERK|REVENUE|AMENDMENT|TOWNSHIP|"
    r"AUDITOR|TREASURER|ATTORNEY|RECORDER")
# junk titles: a precinct-label / banner fragment rides in front of the
# office name ('Turnout 01- SW AURORA ASSOCIATE CIRCUIT JUDGE …'); cut
# everything before the first office phrase (a title that starts AT an
# office phrase is cut by zero characters, so township prefixes like
# 'N MT. VERNON TOWNSHIP COMMITTEEMAN' survive)
_LAW_TITLE_PHRASE = re.compile(
    r"^.*?(?=STATE AUDITOR|US REPRESENTATIVE|STATE REPRESENTATIVE|"
    r"ASSOCIATE CIRCUIT JUDGE|PRESIDING COMMISSIONER|COUNTY CLERK|"
    r"CIRCUIT CLERK|RECORDER OF DEEDS|COUNTY TREASURER|"
    r"PROSECUTING ATTORNEY|COLLECTOR OF REVENUE|CONSTITUTIONAL AMENDMENT|"
    r"TOWNSHIP COMMITTEE)", re.I)
_LAW_TITLE_JUNK = re.compile(r"(?i)\b(turnout|tumou[rt]|tumout)\b")
_LAW_PARTY_ABBR = {"REPUBLICAN": "REP", "DEMOCRATIC": "DEM",
                   "LIBERTARIAN": "LIB"}


def _lawrence_label_of(cells):
    """Printed precinct-label text in a row's leading cells, else None."""
    lab = " ".join((c or "").strip() for c in
                   cells[:2] if (c or "").strip())
    lab = lab.replace("\\n", " ")
    if re.match(r"(?i)jurisdiction", lab):
        # 'Jurisdiction Wide' or an OCR-mangled variant ('Jurisdiction
        # Wice'); strip the banner token and whatever follows it
        lab = re.sub(r"(?i)^jurisdiction\s*\S*\s*", "", lab).strip()
    if _LAWRENCE_LABEL.match(lab):
        return lab
    if lab.strip().lower() == "absentee":
        return "ABSENTEE"
    return None


def parse_lawrence(county):
    cdir = os.path.join(CACHE_DIR, _LAWRENCE_DIR)
    pages = sorted(int(m.group(1)) for m in
                   (re.match(r"p(\d+)\.md$", f)
                    for f in os.listdir(cdir)) if m)
    # pass 1: canonical precinct names, keyed by their leading number
    bynum = {}
    for page in pages:
        tbls = _buchanan_tables(os.path.join(cdir, f"p{page:03d}.md"))
        if not tbls:
            continue
        for cells in tbls[0]:
            lab = _lawrence_label_of(cells)
            m = _LAWRENCE_LABEL.match(lab) if lab else None
            if m:
                bynum.setdefault(m.group(1), []).append(m.group(2))
    canon = {}
    for num, names in bynum.items():
        spellings = sorted(set(n.upper() for n in names))
        canon[num] = max(spellings,
                         key=lambda s: names.count(s) + spellings.index(s))
    # pass 2: walk
    entries = []
    rows = []
    cur = None          # (title, cands, office, dist, party, measure, n)
    for page in pages:
        path = os.path.join(cdir, f"p{page:03d}.md")
        tbls = _buchanan_tables(path)
        if not tbls:
            continue
        tbl = tbls[0]
        # title row: skip label fragments and turnout junk cells
        tparts = []
        for c in tbl[0][1:]:
            c = c.strip()
            if not c or c.lower() in ("turnout", "tumout", "turnour"):
                continue
            if _lawrence_label_of([c]) or "$" in " ".join(
                    c.split()[-1:]) and c.split()[-1] == "AURORA":
                continue
            if "<img" in c:
                continue
            tparts.append(c.replace("\\n", " "))
        title = _LAW_TITLE_PHRASE.sub("", " ".join(tparts))
        title = _LAW_TITLE_JUNK.sub(" ", title).strip()
        title = _cole_fix_title(title)
        hdr_i = next((i for i, c in enumerate(tbl)
                      if "Reg. Voters" in c), None)
        if hdr_i is None:
            print(f"  [!] p{page:03d}: no header row")
            continue
        # contest continuity: a title fragment that carries a party or
        # title word continues the current contest only when the party
        # matches; a bare-party repeat stays the same contest
        old_party = (cur[4] or "").upper() if cur else ""
        pm = re.search(r"\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN) PARTY\)",
                       title, re.I)
        pm2 = re.fullmatch(r"\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN) PARTY\)",
                           title, re.I)
        if (pm2 and cur
                and _LAW_PARTY_ABBR.get(pm2.group(1).upper()) == old_party):
            title = cur[0]
        elif not cur or not title or not _LAWRENCE_TITLE_WORD.search(title):
            title = cur[0] if cur else title
        title = _cole_fix_title(title)
        if not cur or cur[0] != title:
            office, party = _title_cell(title)
            if re.fullmatch(r"Constitutional Amendment No\.\s*\d+", office,
                            re.I):
                office = ("Constitutional Amendment "
                          + re.search(r"(\d+)", office).group(1))
            jm = re.match(r"(Associate Circuit Judge) - Division (.+)$",
                          office)
            if jm:
                office, dist = jm.group(1), jm.group(2)
            else:
                office, dist = normalize_office(office)
            if not party:  # party parens lost in OCR title junk
                pmm = re.search(r"\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN|"
                                r"GREEN|CONSTITUTION) PARTY\)", title, re.I)
                if pmm:
                    party = normalize_party(pmm.group(1))
            if office and re.fullmatch(r"(?i)(US REPRESENTATIVE"
                                       r"|US REPRESENTATIVE DISTRICT"
                                       r"|U\.S\. HOUSE)( DISTRICT)?",
                                       office.strip()):
                # OCR dropped the district number on a US House page
                office, dist = "U.S. House", "7"
            measure = bool(re.search(r"Amendment", office or ""))
            cands = [_ocr_cand(c, measure=measure)
                     for c in tbl[hdr_i][1:]]
            cands = [c for c in cands
                     if c and c not in ("Reg. Voters", "Reg", "Total Votes",
                                        "Ballots Cast", "% Turnout")]
            # OCR sometimes bleeds the preceding column header into a
            # candidate cell ('Total MATTHEW KASPER')
            cands = [re.sub(r"(?i)^total\s+", "", c) if re.fullmatch(
                r"(?i)total\s+\S.*", c) else c for c in cands]
            # OCR-doubled header cells ('BRADLEY BOS', 'BRADLEY BOS') or
            # ('YES', 'NO', 'NO') need name-level dedupe for every contest
            _seen, dedup = set(), []
            for c in cands:
                _k = c.lower()
                if _k not in _seen:
                    _seen.add(_k)
                    dedup.append(c)
            cands = dedup
            if measure:
                cands = [c for c in cands if c.lower() in ("yes", "no")]
            if not cands:
                print(f"  [!] p{page:03d}: no candidates: {title!r}")
                cur = None
                continue
            n = len(cands)
            if rows:
                entries.extend(rows)
                rows = []
            cur = [title, cands, office, dist, party, measure, n]
            print(f"  [~] p{page:03d} {title!r}: {n} candidates")
        title, cands, office, dist, party, measure, n = cur
        # repair the OCR row-shift: a 'Normal' row left empty with the
        # following 'Absentee' row carrying this group's values means the
        # value row slid one label up -- treat the 'Absentee' row's values
        # as the group's Normal row
        body = tbl[hdr_i + 1:]
        _swapped = set()
        for i in range(len(body) - 1):
            a, b = body[i], body[i + 1]
            if (a[0].strip().lower() == "normal"
                    and not _henry_digits(a[1:])
                    and b[0].strip().lower() == "absentee"
                    and _henry_digits(b[1:])
                    and i not in _swapped):
                # a swapped-in placeholder row must NOT read 'Absentee':
                # an empty 'Absentee' row would be mistaken for the county
                # ABSENTEE group's label and swallow the next row's values
                body[i] = ["SkippedShifted"] + a[1:]
                body[i + 1] = ["Normal"] + b[1:]
                _swapped.add(i + 1)
        # second shift family: the value row slid up ONTO the precinct
        # label row and the printed 'Normal' row carries only zeros,
        # while the group's real 'Absentee' row is empty:
        #   ['01- SW AURORA', 2530, 536, '21.19%', 2530, 84, '84 100.00%']
        #   ['Normal',        2530, '-',  '-',    2530, 0,  '0']
        # Swap the label row's values onto the Normal row.
        for i in range(len(body) - 1):
            if i in _swapped or i + 1 in _swapped:
                continue
            lab_i = _lawrence_label_of(body[i])
            if lab_i is None or not _henry_digits(body[i][1:]):
                continue
            nxt = body[i + 1]
            if nxt[0].strip().lower() != "normal":
                continue
            av = _henry_row_votes(body[i][1:], n, 5)
            bv = _henry_row_votes(nxt[1:], n, 5)
            if av and sum(av) and bv is not None and not sum(bv):
                vals_a = body[i][1:]
                body[i] = [(body[i][0] or lab_i), ""] + \
                    [""] * (len(vals_a) - 1)
                body[i + 1] = ["Normal"] + vals_a
                _swapped.add(i + 1)
        # the first group's label may have merged into the title row:
        # recover it from the pre-header cells
        first_lab = None
        for i in range(hdr_i):
            t = _lawrence_label_of(tbl[i])
            if t:
                first_lab = t  # keep last one seen above the header
        gname, gvals = None, None   # open group
        skip_total = False
        pending = None   # closed [office, dist, party, prec, (cand, votes)]

        def flush_group():
            if pending is not None:
                rows.append(pending)
        for cells in body:
            rawlab = (cells[0] or "").strip()
            low = rawlab.lower()
            labels_join = _lawrence_label_of(cells)
            vals = cells[1:]
            digits = _henry_digits(vals)
            joined = " ".join(x.strip() for x in cells
                              if (x or "").strip())
            if low == "total":
                flush_group()
                pending = None
                skip_total = True
                continue
            # a group label row: printed precinct label, or the ABSENTEE
            # group label (no digits -- value rows labelled Absentee carry
            # digits)
            is_label = (labels_join is not None
                        and (low not in ("normal", "absentee", "total")
                             or not digits))
            if _LAWRENCE_LABEL.match(rawlab) or (
                    labels_join is not None
                    and _LAWRENCE_LABEL.match(labels_join)):
                flush_group()
                pending = None
                skip_total = False
                mm = _LAWRENCE_LABEL.match(
                    _LAWRENCE_LABEL.match(rawlab) and rawlab or labels_join)
                num, name = mm.group(1), mm.group(2)
                gname = f"{num}-{canon.get(num, name)}"
                gvals = None
                continue
            if is_label:
                flush_group()
                pending = None
                skip_total = False
                gname = labels_join          # 'ABSENTEE' group label
                gvals = None
                continue
            if low in ("normal", "absentee", ""):
                if skip_total or not digits:
                    continue
                if low == "absentee" and gname != "ABSENTEE":
                    continue   # per-group Absentee rows are zeros here
                if gname is None:
                    if not first_lab:
                        print(f"  [!] p{page:03d}: votes before a label "
                              f"({low} {digits})")
                        continue
                    fm = _LAWRENCE_LABEL.match(first_lab)
                    if fm:
                        num, name = fm.group(1), fm.group(2)
                        gname = f"{num}-{canon.get(num, name)}"
                    else:
                        gname = first_lab
                votes = _henry_row_votes(vals, n, 5)
                if votes is None:
                    votes = _henry_pairs(vals)
                    votes = votes[-n:] if len(votes) >= n else None
                if votes is None:
                    print(f"  [!] p{page:03d} {gname}: bad shape "
                          f"digits={digits}")
                    continue
                gvals = votes if gvals is None else \
                    [a + b for a, b in zip(gvals, votes)]
                pending = [office, dist, party, gname,
                           list(zip(cands, gvals))]
                gvals = None
                # gname stays open so the ABSENTEE group's Absentee row
                # merges into the same group above
        flush_group()
    if rows:
        entries.extend(rows)
    return _county_rows(county, [_emit(entries, county)])


_MCD_DIR = "McDonald_MO_Official_Primary_Election_Results_08_04_26"
_MCD_AGG = re.compile(r"(?i)^(county|mcdonald county"
                      r"|mcdonald county ?- ?total"
                      r"|cumulative|cumulative ?- ?total"
                      r"|county ?- ?total"
                      r"|[a-z .']+ county( ?- ?total)?$)")
# header cells that are not candidate columns
_MCD_SKIP_HDR = re.compile(
    r"(?i)^(total votes|unresolved(\s*write[- ]?in)?(\s*votes)?"
    r"|write[- ]?in(\s*votes)?)$")
_MCD_TITLE_PARTY = re.compile(
    r"\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN|CONSTITUTION"
    r"|REP|DEM|LIB|GRE|CST|NONPARTISAN)\)(\s+PARTY)?\s*$", re.I)
_MCD_TRAILING_WORD = re.compile(
    r"\s+(?i:REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN|CONSTITUTION|"
    r"REP|DEM|LIB|GRE|CST)\s*$")
_MCD_OFFICE_REMAP = {
    "presiding commissioner of the county commission": "Presiding Commissioner",
    "clerk of the county commission": "County Clerk",
}
# page-footers ('Page: 33 or 80') that OCR emits as a ## heading when
# the printed contest title was lost; such a page's table is a spillover
# slice of the current contest, never a new one. Truly garbled footers
# ('rage, 3 or 00') are handled via per-page title overrides.
_MCD_JUNK_TITLE = re.compile(r"(?i)^\s*pa[a-z]{0,2}e?:? ?\d+ ?(of|or) \S+$")
# image-verified repairs for OCR-garbled precinct / candidate labels
_MCD_FIXES = {
    "Newton_MO_Official_Results_08_04_26": {
        "pnames": {"newtonla": "Newtonia", "homet": "Hornet"},
        "cands": {"tami (renfro) ovens": "Tami (Renfro) Owens"},
    },
}
_WRITEINS_HDR = re.compile(r"(?i)^write-?ins?$")


def _mcd_cand_headers(row0):
    """Candidate names from the header row of a Dominion votes table.
    A candidate name may span several header cells (OCR splits
    'Sheila Gideon Fuller (REP)' into 'Sheila Gideon' + 'Fuller (REP)');
    'Total Votes' / 'Unresolved Write-In' columns are dropped."""
    cands = []
    open_name = None
    for cell in row0[1:]:
        c = html.unescape(cell).strip()
        if not c:
            continue
        low = c.lower()
        if _MCD_SKIP_HDR.match(low):
            open_name = None
            continue
        low = _MCD_TRAILING_WORD.sub("", low).strip()  # title's bare 'REP'
        has_parens = bool(re.search(r"\(([^)]+)\)\s*$", low))
        if open_name is not None and not open_name[1]:
            # still-open name: append until a party parens closes it
            open_name = (f"{open_name[0]} {c}", has_parens)
            if has_parens:
                cands.append(open_name[0])
                open_name = None
        elif low.upper() in ("YES", "NO") or has_parens:
            cands.append(low)
        else:
            open_name = (c, False)
    if open_name is not None:
        cands.append(open_name[0])
    return cands


def _mcd_votes_in_row(row, n):
    """Pull n candidate vote values from one Dominion data row. Vote
    cells are either combined '59 71.95%' single cells (colspan merged)
    or split '59' + '71.95%' pairs; Total Votes / Write-In cells sit
    after the candidate cells and are never consumed."""
    cells = [html.unescape(c).strip() for c in row[1:]]
    out = []
    i = 0
    for _ in range(n):
        if i >= len(cells):
            out.append(None)
            continue
        s = cells[i]
        pm = re.match(r"([\d,]+)\s*[\d.]+\s*%", s)
        if pm:  # combined cell
            out.append(_int(pm.group(1)))
            i += 1
        elif s == "":
            out.append(None)
            i += 1
        else:
            nxt = cells[i + 1] if i + 1 < len(cells) else ""
            if "%" in nxt:
                out.append(_int(s))
                i += 2
            else:
                out.append(_int(s))
                i += 1
    return out


def _mcd_title_of(txt):
    m = re.search(r"^## (.+)$", txt, re.M)
    if not m:
        m = re.search(r'<div style="text-align: center;">([^<]+)</div>', txt)
    if not m:
        return None
    t = html.unescape(m.group(1)).strip()
    t = re.sub(r"\s*\(Vote for \d+\)", "", t, flags=re.I).strip()
    return t


def parse_mcdonald(county, cdir_name=None, page_titles=None):
    """Dominion 'Precinct Summary' report: per contest, a turnout table
    (Times Cast / Registered Voters, skipped) plus one or more votes
    tables. A contest's candidate columns may continue onto following
    title-less pages (US House 7 REP spans two pages: Casey + Hunt, then
    Burlison + Total + write-in). Pages with no votes tables are all-zero
    Unresolved-Write-In continuations and are skipped. Aggregate rows
    (County / McDonald County / Cumulative / Newton County - Total / ...)
    are dropped; Absentee / Central Poll / Provisional / Federal rows are
    kept. page_titles: optional {page.md: title} overrides for pages
    where OCR lost the printed contest title ("" forces continuation
    of the previous contest)."""
    cdir = os.path.join(CACHE_DIR, cdir_name or _MCD_DIR)
    fixes = _MCD_FIXES.get(cdir_name or _MCD_DIR, {})
    pages = sorted(f for f in os.listdir(cdir) if re.fullmatch(r"p\d+\.md", f))
    contests = []   # [office, dist, party, {precinct: [cand votes]}]
    cur = None
    for path in pages:
        full = os.path.join(cdir, path)
        txt = open(full).read()
        title = _mcd_title_of(txt)
        if title and _MCD_JUNK_TITLE.match(title.strip()):
            title = None
        if page_titles and path in page_titles:
            title = page_titles[path] or None
        tables = _buchanan_tables(full)
        votes_tables = []
        writein_tables = []  # per-precinct 'Write-in' vote continuations
        for t in tables:
            row0 = [html.unescape(c).strip() for c in t[0]]
            if not row0 or row0[0].strip().lower() != "precinct":
                continue
            hdrs = [c.lower() for c in row0[1:]]
            if any(h == "times cast" for h in hdrs) or \
                    any(h.startswith("registered") for h in hdrs):
                continue  # turnout table
            if not _mcd_cand_headers(row0):
                if any(_WRITEINS_HDR.match(h) for h in hdrs):
                    writein_tables.append(t)  # write-in votes per precinct
                continue  # write-in-only continuation table
            votes_tables.append(t)
        title_ok = title and title.strip().lower() != "precinct"
        if not votes_tables and not (writein_tables and cur is not None):
            continue
        if title_ok:
            t = _MCD_TRAILING_WORD.sub("", title).strip()
            pm = _MCD_TITLE_PARTY.search(t)
            party = ""
            if pm:
                party = normalize_party(pm.group(1))
                t = t[:pm.start()].strip()
            office_part = re.sub(r"(?i)\s*\(Vote for \d+\)", "", t).strip()
            office, dist = normalize_office(office_part)
            remap = _MCD_OFFICE_REMAP.get(office.lower())
            if remap:
                office = remap
            if cur is not None:
                contests.append(cur)
            cur = [office, dist, party, {}]
        elif cur is None:
            print(f"  [!] {path}: votes table with no contest title")
            continue
        data = cur[3]
        for t in votes_tables:
            cands = _mcd_cand_headers(t[0])
            # strip the trailing party parens from each name
            names = []
            for c in cands:
                pm = re.search(r"\s*\(([^)]+)\)\s*$", c)
                if pm and pm.group(1).upper() in (
                        "REP", "DEM", "LIB", "GRE", "CST"):
                    names.append(c[:pm.start()].strip())
                else:
                    names.append(c)
            names = [fixes.get("cands", {}).get(c.lower(), c)
                     for c in names]
            for row in t[1:]:
                pname = html.unescape(row[0]).strip()
                pname = fixes.get("pnames", {}).get(pname.lower(), pname)
                if _MCD_AGG.match(pname) or not pname:
                    continue
                votes = _mcd_votes_in_row(row, len(names))
                slots = data.setdefault(pname, {})
                for nm, v in zip(names, votes):
                    if nm in slots:
                        slots[nm] = (slots[nm] or 0) + (v or 0)
                    else:
                        slots[nm] = v
        for t in writein_tables:
            # single 'Write-in' vote column per row; the percentage and
            # Total Votes columns sit after it and are never consumed
            for row in t[1:]:
                pname = html.unescape(row[0]).strip()
                pname = fixes.get("pnames", {}).get(pname.lower(), pname)
                if _MCD_AGG.match(pname) or not pname:
                    continue
                slots = data.setdefault(pname, {})
                slots["Write-Ins"] = ((slots.get("Write-Ins") or 0)
                                      + (_mcd_votes_in_row(row, 1)[0]
                                         or 0))
    if cur is not None:
        contests.append(cur)
    entries = []
    for office, dist, party, data in contests:
        # candidate order = order first seen across the precinct dicts
        order = []
        for slots in data.values():
            for nm in slots:
                if nm != "Write-Ins" and nm not in order:
                    order.append(nm)
        meas = office.lower().startswith("constitutional amendment")
        for pname, slots in data.items():
            rows = [(_ocr_cand(nm, measure=meas), slots.get(nm) or 0)
                    for nm in order]
            entries.append([office, dist, party, pname, rows])
            if slots.get("Write-Ins"):
                # committee write-in tallies use the WI party convention
                entries.append([office, dist, "WI", pname,
                                [("Write-Ins", slots["Write-Ins"])]])
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Marion -- Dominion "Statement of Votes Cast" (scanned, 25 pages, one
# contest per page). The OCR unpredictably shifts value rows up/down
# relative to the wrapped two-line precinct labels, so precinct labels
# are NOT trusted: instead every value row is keyed by the precinct's
# constant Reg. Voters count, which is unique per precinct. Duplicate
# (reg, votes) rows at wrap points are collapsed.
# ---------------------------------------------------------------------------

_MAR_DIR = "Marion_MO_August_4_2026_Primary_Election_Results_08_04_26"
_MAR_REG = {
    2229: "1st Ward - Hannibal LaGrange University",
    875: "2W-1P - First Assembly of God",
    992: "2W-2P - Douglass Community Services",
    1541: "3W Admiral Coontz Recreation Center",
    1664: "4W-2P Admiral Coontz Recreation Center",
    1902: "5W Prince Ave. Baptist Church",
    2286: "6W American Legion Post Home",
    1069: "Miller - Clover Road Christian Church",
    3256: "Liberty - American Legion",
    328: "West Ely - Immanuel Lutheran Church",
    766: "Warren-Monroe City/Tri County Tire",
    336: "Warren-Warren Baptist Church",
    644: "Union - Philadelphia Community Center",
    672: "Round Grove/Emerson - Emmerson Community Center",
    524: "Fabius/Smileyville - Fabius Community Center",
    532: "Fabius/Taylor - Mt. Olivet Church",
    0: "Absentee",
}
# pages where OCR lost the whole contest banner row (title recovered from
# the rendered page image); p020 additionally splits its candidate name
# across header rows, so its name is forced
_MAR_PAGE_TITLE = {
    "p006.md": "Presiding Commissioner (REPUBLICAN PARTY)",
    "p012.md": "Prosecuting Attorney (REPUBLICAN PARTY)",
    "p013.md": "Collector of Revenue (REPUBLICAN PARTY)",
    "p018.md": "Collector of Revenue (DEMOCRATIC PARTY)",
    "p020.md": ("US REPRESENTATIVE DISTRICT 6 (LIBERTARIAN PARTY)",
                ["Andy Maidment"]),
}
# Values the OCR dropped or misset but which are printed on the page image
# (verified visually, Cole-style); (office, dist, party, precinct) ->
# {candidate: votes}. A precinct absent from the parsed data gets a whole
# new row.
_MAR_IMAGE_FIXES = {
    ("Constitutional Amendment 2", "", "",
     "1st Ward - Hannibal LaGrange University"): {"No": 142},
    ("Proposition: Hannibal School", "", "",
     "Miller - Clover Road Christian Church"): {"Yes": 126, "No": 156},
    ("Proposition: Hannibal School", "", "",
     "Liberty - American Legion"): {"Yes": 0, "No": 0},
}


def _mar_header(t):
    """(header row index, candidate names) for a Marion contest table:
    the header row holds Reg. Voters, Total Votes, then one cell per
    candidate."""
    for i, row in enumerate(t[:8]):
        cells = [html.unescape(c).strip() for c in row]
        joined = " ".join(cells).lower()
        # normal pages: one 'Reg. Voters' cell; p020: header split over two
        # rows with bare 'Reg.' + 'Total' cells ("Reg. Total ANDY MAIDMENT")
        is_hdr = any(re.fullmatch(r"(?i)reg\.? ?vote?r?s?\.?", c)
                     for c in cells) or \
            ("reg." in joined and "total" in joined)
        if not is_hdr:
            continue
        names, seen_total = [], False
        for c in cells:
            if not c:
                continue
            low = c.lower()
            if "total" in low or low == "votes":
                seen_total = True
                continue
            if seen_total:
                names.append(c)
        return i, names
    return None, []


def _mar_row_votes(row):
    """(reg fingerprint, total, [votes]) from one Marion data row: the
    first numeric cell is the Reg. Voters fingerprint, the next is Total
    Votes, then one count per candidate (trailing zeros OCR sometimes
    drops)."""
    nums = []
    for c in (html.unescape(x).strip() for x in row):
        t = c.replace(",", "")
        if t.isdigit():
            nums.append(int(t))
    if not nums or nums[0] not in _MAR_REG:
        return None  # aggregate row / label continuation / junk
    total = nums[1] if len(nums) > 1 else None
    return nums[0], total, nums[2:]


def parse_marion(county):
    cdir = os.path.join(CACHE_DIR, _MAR_DIR)
    pages = sorted(f for f in os.listdir(cdir) if re.fullmatch(r"p\d+\.md", f))
    contests = {}   # (office, dist, party) -> [names, {precinct: votes}]
    order = []
    cur_key = None
    for path in pages:
        full = os.path.join(cdir, path)
        txt = open(full).read()
        override = _MAR_PAGE_TITLE.get(path)
        for tbl in _buchanan_tables(full):
            hdr_i, t_names = _mar_header(tbl)
            if hdr_i is None:
                continue
            parts = [c.strip() for row in tbl[:hdr_i]
                     for c in row if c.strip()]
            title = " ".join(parts)
            forced = None
            if override is not None:
                forced = override[1] if isinstance(override, tuple) else None
                title = override[0] if isinstance(override, tuple) else override
            elif not title:
                title = _mcd_title_of(txt)
            if not title and cur_key is None:
                continue
            if cur_key is not None and not title:
                key = cur_key  # title-less continuation table
            else:
                title = re.sub(r"\s*\(Vote For \d+\)", "", title,
                               flags=re.I).strip()
                office, party = _title_cell(title)
                office, dist = normalize_office(office)
                key = (office, dist, party)
                if key not in contests:
                    contests[key] = [forced if forced is not None
                                     else t_names, {}]
                    order.append(key)
            cur_key = key
            names = contests[key][0] or t_names
            blob = contests[key][1]
            for row in tbl[hdr_i + 1:]:
                mv = _mar_row_votes(row)
                if mv is None:
                    continue
                reg, _total, votes = mv
                pname = _MAR_REG[reg]
                v = (votes + [0] * len(names))[:len(names)]
                if pname in blob and blob[pname] != v:
                    print(f"  [!] {path} {key[0]}: conflicting rows for "
                          f"{pname}")
                    continue
                blob[pname] = v
    for (office, dist, party, pname), fixes in _MAR_IMAGE_FIXES.items():
        c = contests.get((office, dist, party))
        if c is None:
            print(f"  [X] image-fix target contest not parsed: {office}")
            continue
        f_names, blob = c
        if pname not in blob:
            v = [fixes.get(n.lower(), 0) for n in f_names]
        else:
            v = list(blob[pname])
            for n, val in fixes.items():
                m = [i for i, n2 in enumerate(f_names)
                     if n2.lower() == n.lower()]
                if len(m) != 1:
                    print(f"  [X] image-fix candidate not found: {n} "
                          f"in {office}")
                    continue
                v[m[0]] = val
        blob[pname] = v
    entries = []
    for key in order:
        office, dist, party = key
        names, blob = contests[key]
        meas = office.lower().startswith(
            ("constitutional amendment", "proposition"))
        missing = [n for n in _MAR_REG.values() if n not in blob]
        if missing:
            print(f"  [!] {office} ({party}): no fingerprint row for "
                  f"{missing}")
        for pname, votes in blob.items():
            entries.append([office, dist, party, pname,
                            [(_ocr_cand(n, measure=meas), v)
                             for n, v in zip(names, votes)]])
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Ste. Genevieve -- Dominion "Statement of Votes Cast" (40 pages, 2 tables per
# contest: a big9 table for 9 precincts + a 4-precinct table on the paired
# page; ABSENTEE block = its own precinct; printed Total rows are dropped).
# ---------------------------------------------------------------------------

_SG_DIR = "Ste_Genevieve_MO_SOVC_08_04_26"
_SG_SUB = ("Normal", "Absentee", "Provisional")
_SG_AGG = re.compile(r"(?i)^(jurisdiction wide|total)$")
_SG_DIV_TITLE = re.compile(r"<div[^>]*>([^<]{8,})</div>")
# Pages whose banner rows OCR lost outright; titles recovered via the paired
# page's <div> title and the rendered page images (p023/p029/p031 keep a
# partial multi-row banner and need no override).
_SG_PAGE_TITLE = {
    # row-0 IS the header row; pairs with p008 (PresComm REP big9 half)
    "p009.md": "PRESIDING COMMISSIONER (REPUBLICAN PARTY)",
    # pairs with p038 (whose <div> title the OCR kept)
    "p037.md": "CONSTITUTIONAL AMENDMENT NO. 4",
    # pairs with p040 (whose <div> title the OCR kept)
    "p039.md": "CONSTITUTIONAL AMENDMENT NO. 5",
}


def _sg_clean(c):
    return html.unescape(str(c)).replace("\\n", " ").strip()


def _sg_header(tbl):
    """(header row index, candidate names, banner title) -- or
    (None, [], '') when no header row is present."""
    for i, row in enumerate(tbl[:8]):
        if not any(re.fullmatch(r"(?i)reg\.? ?vote?r?s?\.?", _sg_clean(c))
                   for c in row):
            continue
        title = " ".join(_sg_clean(c) for crow in tbl[:i]
                         for c in crow if _sg_clean(c))
        names, seen_total = [], False
        for c in (_sg_clean(x) for x in row):
            if not c:
                continue
            if re.fullmatch(r"(?i)total\s*votes?\.?", c):
                seen_total = True       # one duplicate 'Total Votes' exists
                continue
            if not seen_total or re.fullmatch(
                    r"(?i)(reg\.? ?vote?r?s?\.?|total\s*votes?\.?|votes)", c):
                continue
            names.append(c)
        return i, names, title
    return None, [], ""


def _sg_row_votes(cells, n):
    """(reg, total, [votes]) from a value row: the first two integer cells
    are Reg. Voters / Total Votes, then one integer per candidate; percent
    cells are skipped; a merged '51 100.00%' cell keeps its leading vote."""
    nums = []
    for c in cells[1:]:
        c = c.replace(",", "").strip()
        if not c or c == "-":
            continue
        if "%" in c:
            # digits that are part of the percent are dropped
            bare = re.sub(r"\d+(?:\.\d+)?\s*%", "", c)
            nums.extend(int(m) for m in re.findall(r"\d+", bare))
        elif c[-1].isdigit():
            nums.append(int(re.findall(r"\d+", c)[0]))
        else:
            nums.extend(int(m) for m in re.findall(r"\d+", c))
    if len(nums) < 2:
        return None
    reg, total, votes = nums[0], nums[1], nums[2:]
    if len(votes) < n:
        votes += [0] * (n - len(votes))
    return reg, total, votes[:n]


def parse_stegen(county, dump=False):
    """dump=True prints every assigned (page, contest, precinct, label,
    votes) line instead of writing the CSV -- used for image verification."""
    cdir = os.path.join(CACHE_DIR, _SG_DIR)
    pages = sorted(f for f in os.listdir(cdir) if re.fullmatch(r"p\d+\.md", f))
    contests = {}   # (office, dist, party) -> [names, {precinct: [votes]}]
    order, cur_key = [], None
    for path in pages:
        full = os.path.join(cdir, path)
        txt = open(full).read()
        for tbl in _buchanan_tables(full):
            hdr_i, t_names, t_title = _sg_header(tbl)
            if hdr_i is None:
                continue
            if path in _SG_PAGE_TITLE:
                title = _SG_PAGE_TITLE[path]
            else:
                title = t_title
                if not title:
                    m = _SG_DIV_TITLE.search(txt)
                    title = m.group(1).strip() if m else ""
            if cur_key is not None and not title:
                key = cur_key   # title-less continuation table
            elif title:
                title = re.sub(r"\s*\(Vote For \d+\)", "", title,
                               flags=re.I).strip()
                office, party = _title_cell(title)
                office, dist = normalize_office(office)
                key = (office, dist, party)
                if key not in contests:
                    contests[key] = [t_names, {}]
                    order.append(key)
                if dump:
                    print(f"DUMP-TITLE {path} {title!r} "
                          f"cands={t_names} merged into {key}")
            else:
                continue
            cur_key = key
            names, blob = contests[key]
            if t_names and len(t_names) != len(names):
                print(f"  [!] {path} {key[0]}: header candidates "
                      f"{t_names} != {names}")
            n = len(names)
            block, in_total = None, False
            for row in tbl[hdr_i + 1:]:
                cells = [_sg_clean(c) for c in row]
                if not any(cells):
                    continue
                label, low = cells[0].strip(), cells[0].strip().lower()
                if in_total:
                    continue
                if low == "total":          # printed Total section begins
                    in_total, block = True, None
                    continue
                if label in _SG_SUB:   # banners are ALL-CAPS (ABSENTEE)
                    if block is None:
                        print(f"  [!] {path} {key[0]}: subrow {label!r} "
                              f"without a precinct banner")
                        continue
                elif _SG_AGG.match(label):
                    block = None            # banner above the big9 half
                    continue
                else:
                    block = low == "absentee" and "ABSENTEE" or label
                    continue
                vv = _sg_row_votes(cells, n)
                if vv is None:
                    print(f"  [!] {path} {key[0]}: {block} {label!r} row "
                          f"has no votes")
                    continue
                _reg, total, votes = vv
                if sum(votes) != total:
                    print(f"  [!] {path} {key[0]}: {block} {label}: sum "
                          f"{sum(votes)} != Total Votes {total}")
                pname = "Absentee" if block == "ABSENTEE" else block
                slots = blob.setdefault(pname, [0] * n)
                for i, v in enumerate(votes):
                    if v:
                        slots[i] += v
                if dump:
                    print(f"DUMP {path} {key[0]}|{dist}|{party} "
                          f"n={names} {pname} {label}: "
                          f"{votes} (sum={total})")
    entries = []
    for key in order:
        office, dist, party = key
        names, blob = contests[key]
        meas = office.lower().startswith(
            ("constitutional amendment", "proposition"))
        for pname, votes in blob.items():
            entries.append([office, dist, party, pname,
                            list(zip((_ocr_cand(x, measure=meas)
                                      for x in names), votes))])
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Worth -- "Official Election Returns" (2 pages, candidates as ROWS x
# precincts as COLUMNS, party sections stacked: DEMOCRAT + LIBERTARIAN on
# p001, Republican on p002). Value rows have 10 cells: candidate name + 8
# precinct votes + county Total. The OCR never dropped a cell; the ONLY
# blanks are the printed-empty E/W Fletcherall cells in the LIB candidate
# rows (confirmed on the 300dpi render). Trailing junk (bare 'Amendment N'
# banners + single-value Yes/No summary rows) is rejected by the 10-cell /
# banners-are-1-line rules; so is the '69' colspan residue against the
# ASSOCIATE CIRCUIT JUDGE banner (p002) and the 'Precincts Reporting' /
# 'Candidates' header rows.
# ---------------------------------------------------------------------------

_W_CDIR = "Worth_MO_Official_Election_Returns_08_04_26"
_W_JUNK = {"OFFICIAL ELECTION RETURNS", "PRECINCTS REPORTING",
           "CANDIDATES", "AMENDMENTS"}
_W_PARTY = {"DEMOCRAT": "DEM", "DEMOCRATIC": "DEM", "REPUBLICAN": "REP",
            "LIBERTARIAN": "LIB"}
_W_TITLES = {   # printed title -> (office, district)
    "AUDITOR": ("State Auditor", ""),
    "CLERK OF CIRCUIT COURT": ("Circuit Clerk", ""),
}
_W_SEN = re.compile(r"STATE SENATOR\s*(\d+)", re.I)
_W_USH = re.compile(r"(?:US|U\.?S\.?)\s+REPRESENTATIVE\s*(\d+)", re.I)
_W_SH = re.compile(r"STATE REP(?:RESENTATIVE)?\s*(\d+)", re.I)


def _w_con(t):
    """(office, dist, is_amend_no) from a contest-title / banner cell."""
    u = re.sub(r"\s+", " ", str(t).strip().upper())
    if u in _W_JUNK:
        return None
    if u in _W_TITLES:
        return (*_W_TITLES[u], None)
    m = re.match(r"CONSTITUTIONAL AMENDMENT (?:NO\.?\s*)?(\d+)$", u)
    if m:
        return ("Constitutional Amendment %s" % m.group(1), "", m.group(1))
    m = _W_SEN.match(u)
    if m:
        return ("State Senate", m.group(1), None)
    m = _W_USH.match(u)
    if m:
        return ("U.S. House", m.group(1), None)
    m = _W_SH.match(u)
    if m:
        return ("State House", m.group(1), None)
    office, dist = normalize_office(t)
    return (office, dist, None)


def parse_worth(county):
    cdir = os.path.join(CACHE_DIR, _W_CDIR)
    # precinct spelling as printed (300dpi header zoom: E Fletchall,
    # W Fletchall -- the OCR inserts an extra 'r', 'Fletcherall')
    precincts = ["ABSENTEE", "ALLEN", "E FLETCHALL", "GREENE",
                 "MIDDLEFORK", "SMITH", "W FLETCHALL", "W UNION"]
    # amendments merge party blocks (certified amendment rows are party-less)
    amends = {}     # (amend_no, prec_idx) -> {"Yes": n, "No": n}
    a_no = None
    party = None
    cur = None      # (office, dist)
    buckets = {}    # (office, dist, party, precinct) -> [(cand, votes)]
    order = []
    for path in sorted(os.listdir(cdir)):
        if not re.fullmatch(r"p\d+\.md", path):
            continue
        full = os.path.join(cdir, path)
        a_no, cur, party = None, None, None   # party sections restart per page
        for tbl in _buchanan_tables(full):
            for row in tbl:
                text = [_sg_clean(c) for c in row]
                if len(text) == 10 and all(
                        v.strip() == "" or v.strip().isdigit()
                        for v in text[1:]) and any(
                        v.strip() for v in text[1:]):
                    if cur is None and not a_no:
                        continue
                    cand, is_am = _ocr_cand(text[0], measure=bool(a_no)), a_no
                    votes = [int(v) if v.strip() else 0
                             for v in text[1:9]]
                    total = int(text[9]) if text[9].strip() else None
                    if total is not None and total != sum(votes):
                        print(f"  [!] {path} "
                              f"{cur[0] if cur else 'Constitutional Amendment ' + a_no} "
                              f"(party={party}) "
                              f"{text[0]}: precinct cells sum "
                              f"{sum(votes)} != printed Total {total} "
                              f"(source inconsistency; cells kept)")
                    if is_am:
                        for i, v in enumerate(votes):
                            k = (is_am, i, cand)
                            if k not in amends:
                                amends[k] = 0
                            amends[k] += v
                    else:
                        for i, v in enumerate(votes):
                            if v:
                                ent = (*cur, party, precincts[i])
                                if ent not in buckets:
                                    buckets[ent] = []
                                    order.append(ent)
                                buckets[ent].append((cand, v))
                    continue
                # banner / title row: text of the first cell only
                label = text[0].strip()
                if not label:
                    continue
                pu = label.upper()
                if pu in _W_JUNK or pu.startswith(
                        ("TUESDAY", "AMENDMENT ", "YES", "NO")):
                    a_no = None
                    continue
                if pu in _W_PARTY:
                    party = _W_PARTY[pu]
                    a_no = None
                    continue
                con = _w_con(label)
                if con is None:
                    a_no = None
                    continue
                office, dist, am = con
                a_no = am
                cur = None if am else (office, dist)
    for (am, i, cand), v in sorted(amends.items()):
        ent = (f"Constitutional Amendment {am}", "", "", precincts[i])
        if ent not in buckets:
            buckets[ent] = []
            order.append(ent)
        buckets[ent].append((cand, v))
    entries = [[*ent, buckets[ent]] for ent in order]
    return _county_rows(county, [_emit(entries, county)])


# ---------------------------------------------------------------------------
# Platte -- Dominion SOVC "side-by-side" matrix (42 contest pages, one contest
# per page: turnout trio + [Reg. Voters | Total Votes | (votes, %)*] columns,
# precincts as ROWS, ABSENTEE row kept; 'Jurisdiction Wide' (empty) and
# 'Total' rows dropped). Header candidate NAMES are unreliable -- the OCR
# reflows them across the two header rows (p008 lists Mike Jones/Ryan Gerster
# on row 0 and Sean Pouche/Ryan Gerster on row 1, losing one of three) -- so
# certified-contest candidate columns are assigned by bijecting each column's
# county sum against the certified candidate totals; local contests fall back
# to header appearance order. District-restricted contests (committee/township
# posts, KC questions) print only their precincts. Merged cells ("315
# 100.00%") keep their leading vote; a dropped trailing percent cell changes
# nothing. p033's OCR rows are scrambled (label/value misalignment) and come
# from the rendered-page transcription in _PL_FIX_ROWS.
# ---------------------------------------------------------------------------

_PL_CDIR = "Platte_MO_08042026_Official"
_PL_HDR = ("Reg. Voters", "Ballots Cast", "% Turnout", "Total Votes",
           "Turnout", "Candidates")


def _pl_con(t):
    """(office, dist, party) from a contest-title cell; party '' for
    measures/questions."""
    tt = " ".join(str(t).replace("\\n", " ").split())
    m = re.match(r"(.+?)\s*\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN)\)\s*$",
                 tt, re.I)
    party = normalize_party(m.group(2)) if m else ""
    office = m.group(1) if m else tt
    u = re.sub(r"[.]", "", office.upper())
    m = re.match(r"US REP DIST(?:RICT)?\s*(\d+)$", u)
    if m:
        return ("U.S. House", m.group(1), party)
    m = re.match(r"STATE REP DIST(?:RICT)?\s*(\d+)$", u)
    if m:
        return ("State House", m.group(1), party)
    m = re.match(r"CONST\.?\s+AMEND(?:MENT)?\.?\s+NO\.?\s*(\d+)$", u)
    if m:
        return (f"Constitutional Amendment {m.group(1)}", "", party)
    m = re.match(r"COMMITTEE(MAN|WOMAN)\s+(?:DISTRICT\s*)?(\d+-\d+)$", u)
    if m:
        return (f"Committee{m.group(1)} {m.group(2)}", "", party)
    m = re.match(r"KC QUESTION\s*(\d+)", u)
    if m:
        return (f"Question {m.group(1)} Kansas City", "", "")
    if u == "PROSECUTOR":
        return ("Prosecuting Attorney", "", party)
    if u == "AUDITOR":
        return ("State Auditor", "", party)
    office, dist = normalize_office(office)
    return (office, dist, party)


def _pl_row(cells):
    """(name, creg, cvotes, [votes]) from a data row, or (None,) to skip.
    Name = the first non-numeric cell ('Jurisdiction Wide' cells skipped);
    then pure-number tokens, with percent cells skipped (a merged
    'v p%' cell keeps its leading vote) and dash/blank turnout cells
    passing through. Precinct rows start with county reg + ballots;
    the ABSENTEE row prints '-' for county reg, so its number sequence is
    [ballots, creg, cvotes, votes...] vs [reg, ballots, creg, cvotes,
    votes...]."""
    cs = [c.strip() for c in cells]
    i = 0
    while i < len(cs) and cs[i].lower() == "jurisdiction wide":
        i += 1
    if i >= len(cs) or not cs[i]:
        return (None,)
    name = cs[i]
    low = name.lower()
    if low == "total":
        return (None,)
    vals = []
    for c in cs[i + 1:]:
        if c == "" or c == "-":
            continue
        if re.fullmatch(r"(\d+)\s+\d+(?:\.\d+)?%", c) \
                or re.fullmatch(r"(\d+)\s+\D+.*?\d+(?:\.\d+)?\s*%", c):
            # merged vote+pct cell (p008: '126 $ ^{{*}} $ 37.06%')
            vals.append(int(c.split()[0]))
        elif re.fullmatch(r"\d+(?:\.\d+)?%", c):
            continue
        elif c.replace(",", "").isdigit():
            vals.append(int(c.replace(",", "")))
        else:
            return (None,)
    if not vals:
        return (None,)
    base = 1 if low == "absentee" else 2
    if len(vals) < base + 2:
        return (None,)
    creg, cvotes = vals[base], vals[base + 1]
    return (name, creg, cvotes, vals[base + 2:])


# Pages whose OCR is unusable or incomplete, transcribed from rendered-page
# images of the printed tables: rows are (precinct, contest reg, total
# votes, [candidate votes]).
#   p033 (printed 32/42, Committeewoman 12-4): OCR misaligns the rows
#     (Houston Lake's numbers land on the '38 Line Creek' label etc.).
#   p022 (printed 21/42, County Clerk REP): OCR drops Jera Pruitt's whole
#     vote column; names confirmed incl. Russ "Wojo" Wojtkiewicz.
#   p024 (printed 23/42, Recorder of Deeds REP): OCR 41-Park-Hill-North row
#     reads 208/106/314 against a printed 206 66.03% / 106 33.97% / Total
#     312 (300dpi zoom: it is 206); header names reflowed, so the full page
#     is supplied.
#   p040 (printed 39/42, KC Question 2 GO Convention Bonds): OCR garbles
#     the row labels; district-restricted page transcribed whole.
_PL_FIX_ROWS = {
    "p033.md": {
        "office": "Committeewoman 12-4", "dist": "", "party": "REP",
        "names": ["Charlotte Hoverder", "Marsha Jones"],
        "rows": [("04 Houston Lake / 37 Southeast", 2230, 207, [116, 91]),
                 ("38 Line Creek", 2077, 184, [88, 96]),
                 ("ABSENTEE", 0, 73, [40, 33])]},
    "p022.md": {
        "office": "County Clerk", "dist": "", "party": "REP",
        "names": ['Mary Calvillo', 'Russ "Wojo" Wojtkiewicz', 'Jera Pruitt'],
        "rows": [
            ("01 Riverside / 03 Northmoor", 3346, 344, [138, 133, 73]),
            ("02 Prairie Point", 1151, 89, [41, 19, 29]),
            ("04 Houston Lake / 37 Southeast", 3976, 420, [221, 107, 92]),
            ("05 Parkville", 3028, 427, [195, 112, 120]),
            ("06 Par 4", 3763, 416, [158, 130, 128]),
            ("07 Platte Hills / 15 Hampton East", 2382, 312, [122, 75, 115]),
            ("08 Platte Woods / 36 Park Hill", 1586, 182, [77, 54, 51]),
            ("09 Lake Waukomis", 743, 136, [58, 46, 32]),
            ("10 Weatherby Lake", 1719, 332, [173, 91, 68]),
            ("11 Embassy", 1700, 185, [88, 57, 40]),
            ("12 Farley", 486, 125, [46, 30, 49]),
            ("13 Hampton West", 3373, 569, [253, 146, 170]),
            ("14 Platte City", 2743, 332, [116, 81, 135]),
            ("16 Beverly / 17 Tracy / 28 Settles Station", 842, 147,
             [37, 49, 61]),
            ("18/19 Weston et al", 2428, 383, [107, 79, 197]),
            ("23 New Market / 24 Dearborn", 831, 179, [46, 35, 98]),
            ("25 Edgerton / 26 Ridgely", 1260, 268, [80, 95, 93]),
            ("27 Camden Point", 671, 172, [36, 44, 92]),
            ("29 Hoover / 43 Shiloh", 2206, 483, [178, 146, 159]),
            ("30 Ferrelview/31 Mid-Continent/33 Linkville", 2659, 361,
             [157, 104, 100]),
            ("32 Tiffany Springs", 3662, 299, [138, 88, 73]),
            ("34 Barry East", 3767, 435, [182, 117, 136]),
            ("35 Northern Heights", 2815, 359, [160, 104, 95]),
            ("38 Line Creek", 3163, 308, [139, 95, 74]),
            ("40 Barry North", 4024, 379, [168, 126, 85]),
            ("41 Park Hill North", 2730, 324, [185, 77, 62]),
            ("42 Barry South", 2691, 322, [132, 122, 68]),
            ("44 Seven Bridges", 4816, 742, [244, 213, 285]),
            ("45 Parkville West", 2858, 365, [174, 111, 80]),
            ("ABSENTEE", 0, 1591, [607, 433, 551])]},
    "p024.md": {
        "office": "Recorder of Deeds", "dist": "", "party": "REP",
        "names": ["Christopher L. Wright", "Nicholas Palmer"],
        "rows": [
            ("01 Riverside / 03 Northmoor", 3346, 325, [215, 110]),
            ("02 Prairie Point", 1151, 84, [53, 31]),
            ("04 Houston Lake / 37 Southeast", 3976, 399, [285, 114]),
            ("05 Parkville", 3028, 407, [264, 143]),
            ("06 Par 4", 3763, 405, [211, 194]),
            ("07 Platte Hills / 15 Hampton East", 2382, 301, [201, 100]),
            ("08 Platte Woods / 36 Park Hill", 1586, 179, [133, 46]),
            ("09 Lake Waukomis", 743, 127, [90, 37]),
            ("10 Weatherby Lake", 1719, 310, [200, 110]),
            ("11 Embassy", 1700, 184, [137, 47]),
            ("12 Farley", 486, 125, [69, 56]),
            ("13 Hampton West", 3373, 556, [350, 206]),
            ("14 Platte City", 2743, 325, [160, 165]),
            ("16 Beverly / 17 Tracy / 28 Settles Station", 842, 145,
             [81, 64]),
            ("18/19 Weston et al", 2428, 377, [186, 191]),
            ("23 New Market / 24 Dearborn", 831, 179, [69, 110]),
            ("25 Edgerton / 26 Ridgely", 1260, 268, [162, 106]),
            ("27 Camden Point", 671, 175, [77, 98]),
            ("29 Hoover / 43 Shiloh", 2206, 480, [270, 210]),
            ("30 Ferrelview/31 Mid-Continent/33 Linkville", 2659, 355,
             [211, 144]),
            ("32 Tiffany Springs", 3662, 287, [186, 101]),
            ("34 Barry East", 3767, 418, [273, 145]),
            ("35 Northern Heights", 2815, 345, [228, 117]),
            ("38 Line Creek", 3163, 302, [192, 110]),
            ("40 Barry North", 4024, 362, [220, 142]),
            ("41 Park Hill North", 2730, 312, [206, 106]),
            ("42 Barry South", 2691, 311, [208, 103]),
            ("44 Seven Bridges", 4816, 735, [371, 364]),
            ("45 Parkville West", 2858, 344, [214, 130]),
            ("ABSENTEE", 0, 1560, [848, 712])]},
    "p040.md": {
        "office": "Question 2 Kansas City", "dist": "", "party": "",
        "names": ["Yes", "No"],
        "rows": [
            ("02 Prairie Point", 1151, 353, [228, 125]),
            ("04 Houston Lake / 37 Southeast", 3819, 1165, [650, 515]),
            ("08 Platte Woods / 36 Park Hill", 1281, 393, [213, 180]),
            ("11 Embassy", 1700, 575, [358, 217]),
            ("30 Ferrelview/31 Mid-Continent/33 Linkville", 2382, 724,
             [397, 327]),
            ("32 Tiffany Springs", 3662, 945, [555, 390]),
            ("34 Barry East", 3767, 1165, [669, 496]),
            ("35 Northern Heights", 2815, 934, [508, 426]),
            ("38 Line Creek", 3163, 1107, [691, 416]),
            ("40 Barry North", 4024, 1074, [640, 434]),
            ("41 Park Hill North", 2730, 958, [538, 420]),
            ("42 Barry South", 2691, 985, [593, 392]),
            ("ABSENTEE", 0, 1589, [948, 641])]},
}


def _pl_assign(sums, cert_cand):
    """Column -> certified-candidate assignment when each column's county
    sum bijects onto one certified total (p008's reordered headers); None
    when impossible."""
    if cert_cand is None or len(sums) != len(cert_cand):
        return None
    if len(set(sums)) != len(sums):
        return None
    val2c = {}
    for c, v in cert_cand.items():
        if v in val2c:
            return None
        val2c[v] = c
    if sorted(sums) != sorted(cert_cand.values()):
        return None
    return [val2c[s] for s in sums]


def parse_platte(county, dump=False):
    cdir = os.path.join(CACHE_DIR, _PL_CDIR)
    cert = county_certified(county)
    by_contest = {}
    for (office, dist, party, cand), votes in cert.items():
        by_contest.setdefault((office, dist, party), {})[cand] = votes
    entries = []
    for path in sorted(os.listdir(cdir)):
        if not re.fullmatch(r"p\d+\.md", path) \
                or not os.path.exists(os.path.join(cdir, path)):
            continue
        if path in _PL_FIX_ROWS:
            fix = _PL_FIX_ROWS[path]
            for (prec, creg, cvotes, votes) in fix["rows"]:
                assert sum(votes) == cvotes, (path, prec, sum(votes), cvotes)
                entries.append([fix["office"], fix["dist"], fix["party"],
                                prec.upper(),
                                list(zip(fix["names"], votes))])
                if dump:
                    print(f"DUMP {path} {fix['office']}|{fix['party']} "
                          f"{prec} {votes}")
            continue
        full = os.path.join(cdir, path)
        tables = _buchanan_tables(full)
        if len(tables) < 2:
            continue
        tbl = tables[1]
        title, extra_names, cand_names = "", [], []
        data, total_row = [], None
        for ri, row in enumerate(tbl):
            cells = [_sg_clean(c) for c in row]
            non = [c.strip() for c in cells if c.strip()]
            if not non:
                continue
            if ri == 0:
                parts = [c for c in non if c.lower() != "turnout"]
                if len(parts) >= 2:
                    title, extra_names = parts[0], parts[1:]
                elif parts:
                    title = parts[0]
                continue
            if ri == 1 and "Total Votes" in [c.strip() for c in cells]:
                cand_names = [c.strip() for c in non
                              if c.strip() not in _PL_HDR]
                continue
            pr = _pl_row(cells)
            if len(pr) == 1:
                continue
            data.append(pr)
        if not title:
            print(f"  [!] {path}: no contest title")
            continue
        office, dist, party = _pl_con(title)
        # candidate list: header row names, then row-0 wrapped extras the
        # row-1 names missed (deduped, order-preserving)
        names = []
        for n in cand_names + extra_names:
            if n not in names:
                names.append(n)
        ncols = max((len(v[3]) for v in data), default=0)
        if ncols != len(names):
            print(f"  [!] {path} {office} ({party}): {len(names)} header "
                  f"names vs {ncols} vote columns")
        for (name, creg, cvotes, votes) in data:
            if sum(votes) != cvotes:
                print(f"  [!] {path} {office} ({party}) {name}: cells sum "
                      f"{sum(votes)} != Total Votes {cvotes}")
            if dump:
                print(f"DUMP {path} {office}|{dist}|{party} {name.upper()} "
                      f"{names}: {votes} (sum={cvotes})")
        sums = [0] * ncols
        prec_v = {}
        for (name, creg, cvotes, votes) in data:
            slots = prec_v.setdefault(name, [0] * ncols)
            for k, v in enumerate(votes):
                slots[k] += v
        for name, slots in prec_v.items():
            for k, v in enumerate(slots):
                sums[k] += v
        names_out = _pl_assign(
            sums, by_contest.get((office.lower(), dist, party)))
        if names_out:
            print(f"  [~] {path} {office} (dist={dist} party={party}): "
                  f"columns {names_out} <- certified-total bijection")
        else:
            names_out = names[:ncols]
            if by_contest.get((office, dist, party)) \
                    and len(names) != ncols:
                print(f"  [!] {path} {office} ({party}): unresolved "
                      f"candidate list {names} for {ncols} columns")
        meas = is_ballot_measure(office)
        for (name, creg, cvotes, votes) in data:
            slots = prec_v[name]
            entries.append([office, dist, party, name.upper(),
                            [(_ocr_cand(names_out[k], measure=meas), slots[k])
                             for k in range(len(names_out))]])
    return _county_rows(county, [_emit(entries, county)])


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "buchanan"
    targets = {
        "buchanan": ("Buchanan", parse_buchanan),
        "adair": ("Adair", parse_adair),
        "stoddard": ("Stoddard", parse_stoddard),
        "hickory": ("Hickory", parse_hickory),
        "howard": ("Howard", parse_howard),
        "henry": ("Henry", parse_henry),
        "benton": ("Benton",
                   lambda county: parse_henry(
                       county, "Benton_MO_Official_Results_08_04_26",
                       use_transcripts=False)),
        "chariton": ("Chariton",
                     lambda county: parse_henry(
                         county, "Chariton_MO_Official_Results_08_04_26",
                         use_transcripts=False)),
        "cole": ("Cole", parse_cole),
        "lawrence": ("Lawrence", parse_lawrence),
        "mcdonald": ("McDonald", parse_mcdonald),
        "newton": ("Newton", lambda c: parse_mcdonald(
            c, cdir_name="Newton_MO_Official_Results_08_04_26",
            page_titles={
                # OCR-garbled page-footers (spillover slices of the
                # previous contest): 'l'ayus a la po' ~ 'Page: 5 of 60',
                # 't uye, o o', 'rage, 3 or 00', 'raye, 12 01 00',
                # 'rage, le 01 00'
                "p005.md": "", "p006.md": "", "p009.md": "",
                "p012.md": "", "p016.md": "",
                # OCR lost the title entirely; page carries a full new
                # contest (verified from the rendered page image)
                "p055.md": "Auditor (REP)",
            })),
        "marion": ("Marion", parse_marion),
        "ste_genevieve": ("Ste. Genevieve", parse_stegen),
        "worth": ("Worth", parse_worth),
        "platte": ("Platte", parse_platte),
    }
    key = which.lower()
    if key not in targets:
        raise SystemExit(f"unknown county: {which}")
    county, fn = targets[key]
    return fn(county)

if __name__ == "__main__":
    main()
