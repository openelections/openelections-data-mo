#!/usr/bin/env python3
"""
Parse the remaining text-layer 2026 Missouri primary precinct PDFs (round 2)
into OpenElections CSVs:

  * "Turnout" SOVC variants -- Camden, St. Charles, St. Francois.  District-wide
    SOVC reports where each precinct row carries turnout + per-candidate votes
    (no method rows in Camden/St. Charles; Normal/Absentee rows + a Blank column
    in St. Francois), plus trailing Over/Under columns.  All column matching is
    by x position against header anchors; per row, the candidate votes must sum
    to the "Total Votes" cell.

Vote sums for each contest are cross-checked against the certified
county-totals file (2026/20260804__mo__primary__county.csv) and reported, never
silently trusted.

Run:  uv run 2026/parse_primary_pdfs_more.py [County]
"""
import os
import re
import sys
import csv
from collections import defaultdict

from natural_pdf import PDF

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_primary_sources import (  # noqa: E402
    SRC_DIR, _title, clean_candidate, county_slug, is_ballot_measure,
    normalize_office, normalize_party, titlecase_name, write_csv,
)
from parse_primary_pdfs import (  # noqa: E402
    _sovc_page_rows, _sovc_header_lower, _sovc_clean_precinct, _is_int_tok,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "2026", "counties")
ELECTION = "20260804__mo__primary"

CERTIFIED_PATH = os.path.join(ROOT, "2026", "20260804__mo__primary__county.csv")


def _int(s):
    s = str(s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else 0


def county_certified(county_name):
    """(office_lower, district, party, candidate_lower) -> certified votes."""
    names = {"De Kalb": ["DeKalb"]}.get(county_name, [county_name])
    totals = defaultdict(int)
    with open(CERTIFIED_PATH, newline="") as f:
        for r in csv.DictReader(f):
            if r["county"] in names:
                totals[(r["office"].lower(), r["district"], r["party"],
                        r["candidate"].lower())] += _int(r["votes"])
    return totals


def crosscheck(rows, certified, county):
    summed = defaultdict(int)
    for r in rows:
        if r[2] in ("Registered Voters", "Ballots Cast"):
            continue
        summed[(r[2].lower(), r[3], r[4], r[5].lower())] += int(r[6])
    msgs, bad, matched = [], 0, 0
    # suffix tolerance: sources and the certified file spell name suffixes
    # differently ("Wistrand" vs "Wistrand Jr.", "Gray III" vs "Gray 3rd")
    _SUF = re.compile(r"\s*\b(jr\.?|sr\.?|ii|iii|iv|2nd|3rd|4th)\s*$", re.I)
    def _norm_key(k):
        return k[:3] + (_SUF.sub("", k[3]),)
    cert_norm = {_norm_key(k): v for k, v in certified.items()}
    for key, total in sorted(summed.items()):
        nk = _norm_key(key)
        if nk in cert_norm:
            matched += 1
            if cert_norm[nk] != total:
                bad += 1
                msgs.append(f"  [X] {county} {key}: precincts sum {total} != "
                            f"certified {cert_norm[nk]}")
        else:
            msgs.append(f"  [?] {county} not in certified: {key}")
    msgs.append(f"  [=] {county}: {matched} certified candidates checked, "
                f"{bad} mismatch(es)")
    return bad, msgs


# ---------------------------------------------------------------------------
# Turnout SOVC (Camden / St. Charles / St. Francois)
#
# Page anatomy (per contest): page header; "Turnout" label at x~191 left of the
# contest title (x~290); party line "(REPUBLICAN PARTY)" -- or, on measure
# pages, no party line; candidate-name lines left-anchored in their
# votes+percent column pair; label row Reg./Ballots/%/[Blank]/Reg. Voters/
# Total Votes/.../Over/Under; "Jurisdiction Wide"; then precinct rows
# (leftmost = precinct name), optionally Normal/Absentee/Provisional method
# rows, then a jurisdiction "Total" row at the contest's end.
# ---------------------------------------------------------------------------

_SOVC_PARTY_RE2 = re.compile(
    r"^\((REPUBLICAN|DEMOCRATIC|LIBERTARIAN|GREEN|CONSTITUTION|NONPARTISAN)"
    r"(?:\s+PARTY)?\)$")
_METHOD_LABELS2 = {"Normal", "Absentee", "Provisional"}


def _page_els(page):
    return [(e.x0, e.x1, e.top, (e.text or "").strip())
            for e in page.find_all("text") if (e.text or "").strip()]


def _cluster_els(els, tol=15.0):
    """Cluster (x0,x1,top,text) elements into columns by x0."""
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


def _col_center(cluster):
    """X anchor for a header column's data cells: the label's left edge + 27
    (numbers are fixed-width cells whose centers land ~25-29px right of the
    label start; using the label center instead overshoots narrow labels like
    ``NO`` by ~20px and mis-assigns votes)."""
    return min(e[0] for e in cluster) + 27.0


def _col_name(cluster):
    """Reconstruct a header column's text: same top-band pieces join without a
    space (char-split words), separate bands join with spaces."""
    bands, cur, cur_top = [], [], None
    for e in sorted(cluster, key=lambda e: (e[2], e[0])):
        if cur_top is None or e[2] - cur_top <= 2.0:
            cur.append(e)
            if cur_top is None:
                cur_top = e[2]
        else:
            bands.append(cur)
            cur, cur_top = [e], e[2]
    if cur:
        bands.append(cur)
    words = []
    for i, b in enumerate(bands):
        b.sort(key=lambda e: e[0])
        w = "".join(e[3] for e in b).strip()
        if w and i > 0 and len(b) == 1 and len(w) <= 2 \
                and words[-1] and words[-1][-1].isalpha():
            # wrapped continuation: "CHRISTENSE" + "N" -> "CHRISTENSEN"
            words[-1] += w
        elif w:
            words.append(w)
    return " ".join(words)


def _classify_cols(hdr_els, measure=False):
    """Classify header columns; returns [(center, kind, name)] in x order.
    Kinds: reg (turnout Reg. Voters -- the first Reg. col), ballots, pct, total
    (contest Total Votes), cand (candidate or Yes/No), skip."""
    specs = []
    seen_reg = False
    for cluster in _cluster_els(hdr_els):
        center = _col_center(cluster)
        name = _col_name(cluster)
        u = name.upper()
        toks = set(re.sub(r"[^A-Z% -]", "", u).split())
        if "TOTAL" in toks and "VOTES" in toks:
            kind = "total"
        elif "BALLOTS" in toks and "TOTAL" not in toks and "%" not in u:
            kind = "ballots"
        elif ("REG" in toks or "VOTERS" in toks) and "TOTAL" not in toks:
            kind = "reg" if not seen_reg else "skip"
            seen_reg = True
        elif "%" in u and "TOTAL" not in toks:
            kind = "pct"
        else:
            # Over/Under/Blank/Turnout/Jurisdiction labels, robust to spacing:
            # match on letters only so "Over votes"/"Overvotes" both hit, while
            # real candidate names never equal one of these.
            uu = re.sub(r"[^A-Z]", "", u)
            if uu in {"OVERVOTES", "UNDERVOTES", "BLANK", "TURNOUT",
                      "JURISDICTIONWIDE", "VOTESCAST"}:
                kind = "skip"
            elif measure:
                if "YES" in uu or uu.startswith("NO"):
                    kind = "cand"
                else:
                    kind = "skip"
            else:
                kind = "cand"
        specs.append((center, kind, name))
    return specs


def _clean_cand(name, measure):
    if measure:
        uu = re.sub(r"[^A-Z]", "", name.upper())
        if "YES" in uu:
            return "Yes"
        if uu.startswith("NO"):
            return "No"
        return name.title()
    if re.search(r"write[\s-]*in", name, re.I):
        return "Write-Ins"
    if re.search(r"no\s+candidate\s+filed", name, re.I):
        return "No Candidate Filed"
    return titlecase_name(name)


def _row_clusters(els4):
    """Row numeric clusters (char-split digit pieces <=4.5pt apart merge).
    Returns (center, int|None) in x order; None for percent tokens."""
    nums = sorted([e for e in els4 if e[0] > 60], key=lambda e: e[0])
    clusters, cur, last = [], [], None
    for e in nums:
        if last is None or e[0] - last <= 4.5:
            cur.append(e)
        else:
            clusters.append(cur)
            cur = [e]
        last = e[0]
    if cur:
        clusters.append(cur)
    out = []
    for cl in clusters:
        center = (min(e[0] for e in cl) + max(e[1] for e in cl)) / 2.0
        s = "".join(e[3] for e in cl).replace(" ", "")
        out.append((center, int(s.replace(",", "")) if _is_int_tok(s) else None))
    return out


_TOL = 20.0

_JUDGE_RES = [
    (re.compile(
        r"^Associate Circuit Judge - Circuit (?P<cir>\d+) Division (?P<div>\d+)$"),
     ("Associate Circuit Judge", "{cir} Division {div}")),
    (re.compile(r"^Associate Circuit Judge Division (?P<div>\d+)$"),
     ("Associate Circuit Judge", "Division {div}")),
    (re.compile(r"^Circuit Judge - Circuit (?P<cir>\d+)(?: Division (?P<div>\d+))?$"),
     ("Circuit Judge", "{cir} Division {div}")),
]


def _reshape_judge(office, district):
    """Match the certified county file's judicial shape:
    office 'Circuit Judge', district '26 Division 3' (see
    20260804__mo__primary__county.csv)."""
    for rx, (name, dist_t) in _JUDGE_RES:
        m = rx.match(office)
        if m:
            gd = m.groupdict()
            dist = dist_t.format(cir=gd.get("cir") or "",
                                 div=gd.get("div") or "").strip()
            if not dist or dist.endswith(" Division"):
                dist = dist[: -len(" Division")].strip()
            return (name, dist if dist else district)
    return (office, district)


def _page_rows4(els4):
    """Cluster a page's (x0,x1,top,text) elements into visual rows (top within
    6pt of the row's first element)."""
    ordered = sorted(els4, key=lambda e: e[2])
    rows, cur, cur_top = [], [], None
    for e in ordered:
        if cur_top is None or e[2] - cur_top <= 6.0:
            if cur_top is None:
                cur_top = e[2]
            cur.append(e)
        else:
            rows.append(cur)
            cur, cur_top = [e], e[2]
    if cur:
        rows.append(cur)
    return rows


def _votes_for_row(vals, cand_centers, total_center):
    """Assign each candidate column the nearest unconsumed integer cluster
    (within _TOL); returns [c1..cn] plus the Total Votes cell if that column
    and a free cluster exist, else None when any candidate column is unmatched."""
    free = [(x, v) for x, v in vals if v is not None]
    out = []
    for cc in cand_centers:
        best = None
        for x, v in free:
            d = abs(x - cc)
            if d <= _TOL and (best is None or d < abs(best[0] - cc)):
                best = (x, v)
        if best is None:
            return None
        free.remove(best)
        out.append(best[1])
    if total_center is not None and free:
        rest = [xv for xv in free if abs(xv[0] - total_center) <= _TOL]
        if rest:
            out.append(min(rest, key=lambda xv: abs(xv[0] - total_center))[1])
    return out


def parse_sovc_turnout(path, county):
    """Camden / St. Charles / St. Francois SOVC.  Returns
    (rows, checksum_expected, turnout_rows, bc_method_sum)."""
    pdf = PDF(path)
    accum = {}
    checksum_expected = {}
    rv_first = {}               # precinct -> reg voters
    bc_first = {}               # precinct -> ballots cast (direct-row layout)
    bc_sum = defaultdict(int)   # precinct -> ballots cast over method rows
    method_seen = False
    warned = set()

    def note(k, msg):
        if k not in warned:
            warned.add(k)
            print(f"  [warn] {msg}")

    for page in pdf.pages:
        els = _page_els(page)
        if len(els) < 10:
            continue

        # --- contest detection ---
        party_line = next((e for e in els if _SOVC_PARTY_RE2.match(e[3])), None)
        measure = False
        if party_line is not None:
            party_top = party_line[2]
            office_raw = " ".join(t for x0, x1, top, t in sorted(
                [e for e in els if e[2] < party_top and 75 < e[2] and
                 e[3] != "Turnout" and e[0] > 170], key=lambda e: e[2]))
        else:
            tops = [top for x0, x1, top, t in els
                    if x0 < 230 and top > 80
                    and re.match(r"(Reg\.?|Total|Voters?|Votes?)$", t)]
            meta_top = min(tops) if tops else None
            if meta_top is None:
                continue
            band = sorted([e for e in els if e[0] < 600 and 75 < e[2] < meta_top
                           and e[3] != "Turnout"], key=lambda e: e[2])
            band_text = " ".join(e[3] for e in band).strip()
            if not re.search(r"\b(QUESTION|PROPOSITION|CONSTITUTIONAL AMENDMENT)\b",
                             band_text, re.I):
                continue
            office_raw = band_text
            measure = True
        office, district = normalize_office(office_raw)
        office, district = _reshape_judge(office, district)
        if office.startswith("Turnout"):
            continue
        party = "" if party_line is None else normalize_party(
            _SOVC_PARTY_RE2.match(party_line[3]).group(1))

        # --- header band -> column specs ---
        if party_line is not None:
            els3 = [(x0, top, t) for x0, x1, top, t in els]
            lower = _sovc_header_lower(els3, party_top)
            hdr_els = [e for e in els if party_top < e[2] < lower and e[0] > 60]
        else:
            tops = [top for x0, x1, top, t in els
                    if x0 < 230 and top > 80
                    and re.match(r"(Reg\.?|Total|Voters?|Votes?)$", t)]
            label_top = min(tops) if tops else None
            if label_top is None:
                continue
            lower = None
            jw = next((top for x0, x1, top, t in els if t == "Jurisdiction Wide"),
                      None)
            if jw is not None and jw > label_top:
                lower = jw
            else:
                # continuation pages often lack the Jurisdiction Wide row; bound
                # the header at the first row carrying a left-margin (<=60)
                # label -- precinct rows are the only such rows below the label
                # row, so anything between label_top and it is table header.
                for r in _page_rows4(els):
                    lab = next((e for e in r if e[0] <= 60), None)
                    if lab is not None and lab[2] > label_top:
                        lower = min(e[2] for e in r)
                        break
            if lower is None:
                lower = label_top + 45
            hdr_els = [e for e in els if label_top <= e[2] < lower and e[0] > 60]
        specs = _classify_cols(hdr_els, measure=measure)
        cand_specs = [(cx, nm) for cx, k, nm in specs if k == "cand"]
        total_centers = [cx for cx, k, nm in specs if k == "total"]
        total_center = total_centers[0] if total_centers else None
        if not cand_specs:
            note((office, district, "nocols"),
                 f"{county} {office}: no candidate columns found on page")
            continue
        names = [_clean_cand(nm, measure) for _, nm in cand_specs]
        cand_centers = [cx for cx, _ in cand_specs]
        n_cand = len(cand_specs)

        # --- data rows ---
        page_rows = _page_rows4(els)
        cur_precinct = None
        for rrow in page_rows:
            rrow.sort(key=lambda e: e[0])
            if rrow[0][0] > 60:
                continue  # page / contest / candidate headers
            label = rrow[0][3]
            vals = _row_clusters(rrow)

            if label == "Jurisdiction Wide":
                cur_precinct = None
                continue
            if label.lower().startswith("total"):
                if not any(v is not None for _, v in vals):
                    cur_precinct = None
                    continue  # bare section marker
                cv = _votes_for_row(vals, cand_centers, total_center)
                if cv and len(cv) == n_cand + (1 if total_center else 0):
                    for i in range(n_cand):
                        checksum_expected[(office, district, party, names[i])] = \
                            cv[i]
                else:
                    note((office, "totalrow"),
                         f"{county} {office}: Total row unreadable, no checksum")
                cur_precinct = None
                continue
            if label in _METHOD_LABELS2:
                method = label
                method_seen = True
            else:
                method = None
                cur_precinct = _sovc_clean_precinct(label)
            if cur_precinct is None:
                continue

            cv = _votes_for_row(vals, cand_centers, total_center)
            if not cv or len(cv) != n_cand + (1 if total_center else 0):
                if any(v is not None for _, v in vals):
                    note((office, district, "rows"),
                         f"{county} {office}/{cur_precinct}: could not map row "
                         f"numerics to candidate columns ({len(vals)} clusters vs "
                         f"{n_cand} candidates)")
                continue
            for i in range(n_cand):
                key = (cur_precinct, office, district, party, names[i])
                accum[key] = accum.get(key, 0) + cv[i]
            if len(cv) == n_cand + 1 and sum(cv[:-1]) != cv[-1]:
                print(f"  [warn] {county} {office}/{cur_precinct}: candidate "
                      f"sum {sum(cv[:-1])} != Total Votes {cv[-1]}")

            # turnout: first two integer clusters left of the first cand col
            ints = [(x, v) for x, v in vals
                    if v is not None and x < cand_centers[0] - 30]
            if ints:
                r_, b_ = ints[0][1], (ints[1][1] if len(ints) > 1 else None)
                if method is None:
                    rv_first.setdefault(cur_precinct, r_)
                    if b_ is not None:
                        bc_first.setdefault(cur_precinct, b_)
                elif method != "Provisional":
                    if cur_precinct not in rv_first:
                        rv_first[cur_precinct] = r_
                    else:
                        rv_first[cur_precinct] = max(rv_first[cur_precinct], r_)
                    if b_ is not None:
                        bc_sum[cur_precinct] += b_

    rows = [[county, p, o, d, cp, cn, v]
            for (p, o, d, cp, cn), v in accum.items()]
    turnout_rows = []
    if method_seen:
        for precinct, rv in rv_first.items():
            if rv:
                turnout_rows.append([county, precinct, "Registered Voters",
                                     "", "", "", rv])
            if bc_sum.get(precinct):
                turnout_rows.append([county, precinct, "Ballots Cast", "", "", "",
                                     bc_sum[precinct]])
    else:
        for precinct, rv in rv_first.items():
            if rv:
                turnout_rows.append([county, precinct, "Registered Voters",
                                     "", "", "", rv])
            if bc_first.get(precinct):
                turnout_rows.append([county, precinct, "Ballots Cast", "", "", "",
                                     bc_first[precinct]])
    return rows, checksum_expected, turnout_rows, dict(bc_sum)


# ---------------------------------------------------------------------------
# Cape Girardeau "Details by Precinct": for each contest, rows of
# (precinct, candidate, total votes) -- no party, no ballot-count columns.
# ---------------------------------------------------------------------------

def _cert_party_map(county_name):
    """(office_lower, candidate_lower) -> party, from the certified file."""
    names = {"De Kalb": ["DeKalb"]}.get(county_name, [county_name])
    m = {}
    with open(CERTIFIED_PATH, newline="") as f:
        for r in csv.DictReader(f):
            if r["county"] in names and r["party"]:
                m.setdefault((r["office"].lower(), r["candidate"].lower()),
                             r["party"])
    return m


def parse_details_by_precinct(path, county):
    pdf = PDF(path)
    party_map = _cert_party_map(county)
    cur = None
    out = []
    for page in pdf.pages:
        els4 = [(e.x0, e.x1, e.top, (e.text or "").strip())
                for e in page.find_all("text") if (e.text or "").strip()]
        for rrow in _page_rows4(els4):
            rrow.sort(key=lambda e: e[0])
            x0_first, _, top_first, _ = rrow[0]

            if len(rrow) < 3:
                # page footers may split across one or two rows
                if all(_PAGE_FOOTER_RE.match(e[3]) for e in rrow):
                    continue
                if len(rrow) == 1 and x0_first < 100:
                    t = rrow[0][3]
                    if not (_PAGE_FOOTER_RE.match(t) or re.match(
                            r"\d+/\d+/\d+", t)):
                        cur = t
                continue

            # contest-header rows: ('Precinct', 'Candidate', 'Total Votes')
            if any((e[3] or "").upper() == "CANDIDATE" for e in rrow):
                continue
            votes_el = rrow[-1]
            votes_tok = str(votes_el[3].replace(",", ""))
            if votes_el[0] <= 480 or not votes_tok.isdigit():
                continue  # footer junk / non-integral trailing column
            cand_el = next((e for e in rrow if 200 < e[0] < 480), None)
            if cand_el is None:
                continue
            prec_el = rrow[0] if rrow[0][0] < 100 else None
            if cur is None:
                continue
            office, district = normalize_office(cur)
            office, district = _reshape_judge(office, district)
            measure = is_ballot_measure(office)
            cand_raw = cand_el[3].strip()
            if measure:
                cand = clean_candidate(cand_raw, is_ballot_measure=True)
                party = ""
            elif re.search(r"write[\s-]*in", cand_raw, re.I):
                cand, party = "Write-Ins", "WI"
            else:
                cand = titlecase_name(cand_raw)
                party = party_map.get((office.lower(), cand.lower()), "")
            votes = _int(votes_el[3])
            out.append([county, prec_el[3] if prec_el else "", office,
                        district, party, cand, votes])
    return out, {}, [], {}


_PAGE_FOOTER_RE = re.compile(r"Page \d+ of \d+$")


# ---------------------------------------------------------------------------
# Greene "Precinct Detail": one precinct per page (ES&S Electionware Summary
# Results Report).  Contest lines are '<PARTY> For <office>' or measure/
# question titles; each candidate line is name + total votes.
# ---------------------------------------------------------------------------

_GREENE_PARTY = {"REP": "REP", "DEM": "DEM", "LBT": "LIB", "LIB": "LIB",
                 "GRE": "GRE", "CST": "CST", "NON": "", "NONPARTISAN": ""}
_GREENE_CONTEST_RE = re.compile(r"^([A-Z]+) For (.+)$", re.I)


def _greene_office(raw):
    m = re.match(r"U\.?S\.?\s*Rep(?:resentative)?\s*-\s*District\s*(\d+)",
                 raw, re.I)
    if m:
        return ("U.S. House", m.group(1))
    m = re.match(r"State Rep(?:resentative)?\s*-\s*District\s*(\d+)", raw, re.I)
    if m:
        return ("State House", m.group(1))
    m = re.match(r"(.+)\s*-\s*Question\s*$", raw, re.I)
    if m:
        place = re.sub(r"^(?:CITY|VILLAGE|TOWN) OF\s+", "", m.group(1),
                       flags=re.I)
        return (_title(place).strip() + " Question", "")
    return normalize_office(raw)


def parse_greene(path, county):
    pdf = PDF(path)
    out = []
    state = {}   # per-page: 'precinct', ('office', 'district', 'party', measure)
    for page in pdf.pages:
        els4 = [(e.x0, e.x1, e.top, (e.text or "").strip())
                for e in page.find_all("text") if (e.text or "").strip()]
        rows = _page_rows4(els4)
        st = {"precinct": None}
        for rrow in rows:
            rrow.sort(key=lambda e: e[0])
            n = len(rrow)
            t0 = rrow[0][3]
            if n >= 2 and str(rrow[-1][3]).replace(",", "").isdigit() \
                    and rrow[-1][0] > 175 and rrow[0][0] < 175 \
                    and st["precinct"] is not None and st.get("office"):
                votes = _int(rrow[-1][3])
                name_raw = " ".join(e[3] for e in rrow[:-1]).strip()
                office, district, party, measure = (st["office"], st["district"],
                                                    st["party"], st["measure"])
                if measure:
                    cand = clean_candidate(name_raw, is_ballot_measure=True)
                    party = ""
                elif re.search(r"write[\s-]*in", name_raw, re.I):
                    cand, party = "Write-Ins", "WI"
                else:
                    cand = titlecase_name(name_raw)
                    party = _GREENE_PARTY.get(party, party)
                out.append([county, st["precinct"], office, district, party,
                            cand, votes])
                continue
            if n == 1:
                t = t0.strip()
                x0 = rrow[0][0]
                # precinct slot: the lone label under the report header
                if 70 < rrow[0][2] < 100 and x0 < 40:
                    # keep suffix letters ("Battlefield A", "Clay B") —
                    # _sovc_clean_precinct would strip them as noise
                    st["precinct"] = " ".join(t.split())
                    st["office"], st["district"], st["party"], st["measure"] = \
                        None, None, None, False
                    continue
                m = _GREENE_CONTEST_RE.match(t)
                if m and m.group(1).upper() in _GREENE_PARTY:
                    office, district = _greene_office(m.group(2).strip())
                    office, district = _reshape_judge(office, district)
                    st["office"], st["district"] = office, district
                    st["party"] = _GREENE_PARTY.get(m.group(1).upper(),
                                                    m.group(1).upper())
                    st["measure"] = False
                    continue
                if re.match(r"CONSTITUTIONAL AMENDMENT NO\.?\s*\d+$", t, re.I):
                    office, district = normalize_office(t)
                    st["office"], st["district"] = office, district
                    st["party"], st["measure"] = "", True
                    continue
                # local question titles ("City of Ash Grove- Question") carry
                # no party prefix; without recognizing them, their Yes/No
                # rows would be attributed to the previous contest
                if re.match(r"(?:CITY|VILLAGE|TOWN) OF .+ ?[-–] ?QUESTION$",
                            t, re.I):
                    st["office"], st["district"] = _greene_office(t)
                    st["party"], st["measure"] = "", True
                    continue
                # candidate + votes rendered as one element: "Shane Schoeller 199"
                m2 = re.match(r"^(.+?)[\s ]+([\d,]+)$", t)
                if (m2 and st.get("office") and not t.startswith("Vote For")
                        and not re.match(r"Precinct Summary|Report generated", t)):
                    votes = _int(m2.group(2))
                    office, district, party, measure = (st["office"],
                                                        st["district"],
                                                        st["party"],
                                                        st["measure"])
                    name_raw = m2.group(1).strip()
                    if measure:
                        cand = clean_candidate(name_raw, is_ballot_measure=True)
                        party = ""
                    elif re.search(r"write[\s-]*in", name_raw, re.I):
                        cand, party = "Write-Ins", "WI"
                    else:
                        cand = titlecase_name(name_raw)
                        party = _GREENE_PARTY.get(party, party)
                    out.append([county, st["precinct"], office, district, party,
                                cand, votes])
    return out, {}, [], {}


# ---------------------------------------------------------------------------
# Kansas City "Ward Precinct Canvass" (ES&S Summary Results Report, one page
# set per precinct GROUP "W1-P1,5,10"): contest title '<PARTY> <OFFICE>',
# candidate line '<NAME> <TOTAL> <Election Day> <Early/by mail> ...' where the
# FIRST number is the group total.  Stats block only on the group's first page.
# ---------------------------------------------------------------------------

_KC_PARTY_RE = re.compile(r"^(REP|DEM|LIB)\s+(.+)$")
_KC_PRECINCT_RE = re.compile(r"^W\d+-P[\d,]+$")
_KC_QUESTION_RE = re.compile(r"^QUESTION NO\.?\s*(\d+)$", re.I)

_KC_SKIPS = ("Total Votes Cast", "Contest Totals", "Vote For",
             "Voter Turnout", "STATISTICS", "Absentee", "Summary Results",
             "Primary Election", "August 4", "Ballots Cast", "Election",
             "Early Voting", "Mail", "Person", "Precinct Summary",
             "Report generated", "Day")
_KC_OFFICE_RES = [
    (re.compile(r"U\.?S\.?\s*Representatives?\s+District\s+(\d+)$", re.I),
     ("U.S. House", "{n}")),
    (re.compile(r"State Representat?ives?\s+District\s+(\d+)$", re.I),
     ("State House", "{n}")),
    (re.compile(r"State Senator(?:s)?\s+District\s+(\d+)$", re.I),
     ("State Senate", "{n}")),
    (re.compile(r"State Auditor(?:\s+of\s+Missouri)?$", re.I),
     ("State Auditor", "")),
    (re.compile(r"At-Large County Legislator District\s+(\d+)$", re.I),
     ("County Legislator", "At-Large {n}")),
    (re.compile(r"County Legislator District\s+(\d+)$", re.I),
     ("County Legislator", "{n}")),
    (re.compile(r"County Committeeman Ward\s+(\d+)$", re.I),
     ("County Committeeman", "Ward {n}")),
    (re.compile(r"County Committeewoman Ward\s+(\d+)$", re.I),
     ("County Committeewoman", "Ward {n}")),
    (re.compile(r"County Executive(?:\s+\w+)?$", re.I),
     ("County Executive", "")),
]

def _kc_contest(line):
    """-> (office, district, party, measure) for a contest line, else None."""
    u = line.upper()
    m = re.match(r"CONSTITUTIONAL AMENDMENT NO\.?\s*(\d+)$", u)
    if m:
        return normalize_office("Constitutional Amendment No. %s" % m.group(1)) \
            + ("", True)
    m = _KC_QUESTION_RE.match(line)
    if m:
        return (("Question No. %s" % m.group(1), "", "", True))
    m = re.match(r"^QUESTION\s+(.+)$", line, re.I)
    if m:
        # e.g. "QUESTION CENTER 58 SD" (Center School District 58) — without
        # matching it, its Yes/No rows bleed into the previous question
        rest = m.group(1).strip()
        m2 = re.match(r"NO\.?\s*(\d+)$", rest, re.I)
        if m2:
            return ("Question No. %s" % m2.group(1), "", "", True)
        place = re.sub(r"^(?:CITY|VILLAGE|TOWN) OF\s+", "", rest, flags=re.I)
        place = re.sub(r"\bSd\b", "SD", _title(place).strip())
        return (place + " Question", "", "", True)
    m = _KC_PARTY_RE.match(line)
    if m:
        party = m.group(1)
        raw = m.group(2).strip()
        for rx, (off, dist) in _KC_OFFICE_RES:
            mm = rx.match(raw)
            if mm:
                if "{n}" in dist and mm.groups():
                    dist = dist.format(n=mm.group(1))
                return (off, dist, party, False)
        return (normalize_office(raw), "", party, False)
    return None


def parse_kc(path, county):
    pdf = PDF(path)
    out = []
    st = {"precinct": None, "office": None, "district": None, "party": None,
          "measure": False}
    for page in pdf.pages:
        for ln in (page.extract_text() or "").splitlines():
            t = " ".join(ln.split())
            if not t:
                continue
            if _KC_PRECINCT_RE.match(t):
                if t != st["precinct"]:
                    st = {"precinct": t, "office": None, "district": None,
                          "party": None, "measure": False}
                continue
            if t.startswith(_KC_SKIPS):
                continue
            contest = _kc_contest(t)
            if contest is not None:
                st["office"], st["district"], st["party"], st["measure"] = \
                    contest
                continue
            if t.startswith("Registered Voters") and re.search(r"\d", t):
                mm = re.search(r"Total\s+([\d,]+)\b", t)
                if mm:
                    out.append([county, st["precinct"], "Registered Voters",
                                "", "", "", _int(mm.group(1))])
                continue
            if t.startswith("Ballots Cast"):
                mm = re.match(r"Ballots Cast\s*[-–]\s*Total\s+([\d,]+)", t,
                              re.I)
                if mm:
                    out.append([county, st["precinct"], "Ballots Cast",
                                "", "", "", _int(mm.group(1))])
                continue
            # candidate line: name followed by at least two number columns
            # (TOTAL + method splits); the FIRST number is the group total.
            # (Survey confirmed no candidate name ever wraps to a second
            # line, so name-only lines only carry small-caps header residue
            # and are ignored.)
            m = re.match(r"^(.+?\S)\s+([\d,]+)((?:\s+[\d,]+)*)$", t)
            if (m and st["office"] is not None
                    and len(re.findall(r"[\d,]+", m.group(3))) >= 3):
                name_raw, votes = m.group(1).strip(), _int(m.group(2))
                office, district, party, measure = (st["office"],
                                                    st["district"],
                                                    st["party"], st["measure"])
                if re.search(r"^Write-?In\s+Totals$", name_raw, re.I):
                    cand, party = "Write-Ins", "WI"
                elif re.search(r"^No Candidate Filed$", name_raw, re.I):
                    continue
                elif measure:
                    cand, party = clean_candidate(
                        name_raw, is_ballot_measure=True), ""
                else:
                    cand = titlecase_name(name_raw)
                    party = _GREENE_PARTY.get(party, party)
                out.append([county, st["precinct"], office, district, party,
                            cand, votes])
                continue
    return out, {}, [], {}


def run_county(county, files, fn):
    per_file_rows, all_rows, all_ck, extras = [], [], {}, []
    for f in files:
        res = fn(os.path.join(SRC_DIR, f), county)
        rows, ck, turnout_rows = res[0], res[1], res[2]
        per_file_rows.append(rows)
        all_rows += rows
        all_ck.update(ck)
        extras += turnout_rows
    cert = county_certified(county)
    bad, msgs = crosscheck(all_rows, cert, county)
    msg_txt = "\n".join(m for m in msgs if not m.startswith("  [?]"))
    print(msg_txt)
    nq = sum(1 for m in msgs if m.startswith("  [?]"))
    if nq:
        print(f"  [?] {nq} rows not in certified file (local contests etc.)")
    # merge rows on the full non-vote key, summing votes (the duplicate-entries
    # test requires one row per county/precinct/office/district/party/
    # candidate; e.g. a county's REP and DEM versions of the same committee
    # contest both list "Write-Ins" for a precinct)
    merged, order = {}, []
    for r in [rr for rows in per_file_rows + [extras] for rr in rows]:
        k = tuple(r[:6])
        if k in merged:
            merged[k][6] = str(int(merged[k][6]) + _int(r[6]))
        else:
            merged[k] = list(r)
            order.append(k)
    deduped = [merged[k] for k in sorted(merged)]
    path = os.path.join(OUT_DIR, f"{ELECTION}__{county_slug(county)}__precinct.csv")
    write_csv(path, deduped)
    n_precincts = len(set(r[1] for r in deduped if r[2] not in
                          ("Registered Voters", "Ballots Cast")))
    print(f"  wrote {path}  ({len(deduped)} rows, {n_precincts} precincts), "
          f"crosscheck mismatch(es): {bad}")
    return bad


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else None
    targets = [
        ("Camden", ["Camden MO SOVC Official 08.04.26.pdf"], parse_sovc_turnout),
        ("St. Charles", ["St Charles MO SOVC Official 08.04.26.pdf"],
         parse_sovc_turnout),
        ("St. Francois", ["St Francois MO SOVC Official 08.04.26.pdf"],
         parse_sovc_turnout),
        ("Cape Girardeau",
         ["Cape Girardeau MO Details by Precinct Official 08.04.26.pdf"],
         parse_details_by_precinct),
        ("Greene", ["Greene MO Precinct Detail Official 08.04.26.pdf"],
         parse_greene),
        ("Kansas City", ["KC Ward Precinct Canvass 08.04.26.pdf"], parse_kc),
    ]
    total_bad = 0
    for county, files, fn in targets:
        if which and (which.lower() not in county.lower()
                      and which.lower() not in county_slug(county)):
            continue
        print(f"\n=== {county}")
        total_bad += run_county(county, files, fn)
    if total_bad:
        print(f"\nWARNING: {total_bad} crosscheck mismatch(es) overall")
    print("\nDone.")


if __name__ == "__main__":
    main()