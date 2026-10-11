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
    clean_candidate, county_slug, normalize_office, normalize_party,
    titlecase_name, write_csv,
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_cache")


def _ocr_cand(raw, measure=False):
    """clean_candidate for OCR text: strips accent marks the OCR model
    added ("Pondélick" -> "Pondelick") first."""
    s = unicodedata.normalize("NFD", str(raw))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
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
                    cand = clean_candidate(_join_name(cells[0]))
                    if cand in ("Total Votes", "", "Registered Voters"):
                        continue
                    if office and re.search(r"Amendment", office):
                        cand = clean_candidate(cells[0],
                                               is_ballot_measure=True)
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
                    cand = clean_candidate(name, is_ballot_measure=True)
                    if cand not in ("Yes", "No"):
                        continue
                    entry_party = ""
                else:
                    cand = clean_candidate(name)
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
    out = []
    for c in cells:
        t = c.replace(",", "").strip()
        if t.isdigit():
            out.append(int(t))
        elif "%" in c or t in ("-", ""):
            continue
    return out


def parse_stoddard(county):
    cdir = os.path.join(CACHE_DIR, _STODDARD_DIR)
    entries = []
    for page in range(1, 20):
        path = os.path.join(cdir, f"p{page:03d}.md")
        md = open(path).read()
        tables = _buchanan_tables(path)
        title = None
        for tbl in tables:
            # title rows: everything before the "Reg. Voters" header row
            tparts, hdr_i = [], None
            for i, cells in enumerate(tbl):
                if any(c in ("Reg. Voters", "Total Votes") for c in cells):
                    hdr_i = i
                    break
                tparts += [c for c in cells if c.strip()]
            if not tparts and title is None:
                m = re.search(r"<div[^>]*>([^<]*AMENDMENT[^<]*)</div>", md,
                              re.I)
                if m:
                    tparts = [m.group(1).strip()]
            if not tparts and page in _STODDARD_FALLBACK_TITLES:
                tparts = [_STODDARD_FALLBACK_TITLES[page]]
            if not tparts:
                continue
            title = "\\n".join(p.strip() for p in tparts)
            office, party = _title_cell(title)
            # amendment titles carry "NO. n", which normalize_office can't
            # fix later -> pre-normalize here
            if re.fullmatch(r"Constitutional Amendment No\.\s*\d+", office,
                            re.I):
                n = re.search(r"(\d+)", office).group(1)
                office = f"Constitutional Amendment {n}"
            office, dist = normalize_office(office)
            # candidate headers: non-"Reg. Voters"/"Total Votes" header cells
            hdr_cells = tbl[hdr_i]
            measure = office and bool(re.search(r"Amendment", office))
            cands = []
            for c in hdr_cells:
                if c in ("Reg. Voters", "Total Votes", ""):
                    continue
                cands.append(_ocr_cand(_join_name(c), measure=measure))
            if measure:
                cands = [c for c in cands if c and c.lower() in
                         ("yes", "no")] or ["Yes", "No"]
            if not cands:
                print(f"  [!] page {page}: no candidates for {title}")
                continue
            # data rows per precinct banner
            acc = []  # (precinct, {candidate: votes_normal}, votes_abs)
            for cells in tbl[hdr_i + 1:]:
                kind = cells[0].strip().lower()
                other = [c.strip() for c in cells[1:]]
                if kind in ("normal", "absentee") and not other[:2] == []:
                    if kind == "Absentee".lower() and acc:
                        pass
                # banner?
                if kind not in ("normal", "absentee", "total") \
                        and all(not c for c in cells[1:]):
                    name = cells[0].strip()
                    low = name.lower()
                    if low == "jurisdiction wide":
                        acc = []
                        continue
                    merged = re.match(r"(?i)jurisdiction wide\s+(.*)", name)
                    if merged:
                        name = merged.group(1).strip()
                    if kind == "absentee":
                        name = "ABSENTEE"
                    acc = [(name, {}, 0, 0)]
                    continue
                if kind == "total":
                    acc = []  # county final block: skip
                    continue
                if not acc:
                    continue
                nums = _stoddard_numeric(cells)
                n = len(cands)
                if len(nums) == n + 2:
                    v = nums[2:]
                elif len(nums) == n + 1:
                    v = nums[1:]
                elif len(nums) == n:
                    v = nums
                else:
                    print(f"  [!] page {page} {kind} row: unexpected shape "
                          f"{cells[:3]}... nums={nums}")
                    continue
                name, norm, tot, ntot = acc[-1]
                tot = list(tot) if len(tot) == n else [0] * n
                nvotes = tot  # keep per-candidate dict instead
                acc[-1] = (name, tot, 0, 0)
                d = acc[-1]  # placeholder
            # simpler accumulation below
        break
    return entries


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "buchanan"
    targets = {
        "buchanan": ("Buchanan", parse_buchanan),
        "adair": ("Adair", parse_adair),
        "stoddard": ("Stoddard", parse_stoddard),
    }
    key = which.lower()
    if key not in targets:
        raise SystemExit(f"unknown county: {which}")
    county, fn = targets[key]
    return fn(county)