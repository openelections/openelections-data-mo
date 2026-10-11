# OCR batch — per-county status notes (Aug 2026 primary)

Vendor formats seen so far:
- **Dominion "Election Summary Report"** — county totals ONLY, no precinct
  detail → dead end for precinct CSVs.
  Confirmed in: Barry, Bates, Callaway, Carroll, Cedar*, Chariton, Clinton,
  Cole Cumulative, Daviess, Dent (+ text-layer twins Cass, Jefferson, Phelps,
  Ralls, Howard-Total, DeKalb, Holt, Pike, Dunklin, Webster, Johnson,
  Lafayette, Buchanan-Summary, Camden-Summary, Greene-Summary, KC-Full).
  *Cedar says "7 of 9 Precincts Reported" — even if detail existed it
  wouldn't be certified-complete.
- **SOS SOVC "Statement of Votes Cast"** — real per-precinct detail.
  Stoddard parser: `parse_primary_ocr.py::parse_stoddard` (DONE, 252 rows,
  0 mismatches, tests OK).
- **ES&S "Official Election Results"** — Adair parser: `parse_adair` (DONE,
  generic since this session: `parse_adair(county, cdir_name=...)` reuses the
  same block-detection for any ES&S layout county).
- **Dominion "Precinct Summary Report"** — Buchanan parser (DONE; needed
  visually-read precinct-name labels because OCR turned banners into <img>).
- **Hickory compact SOVC**: DONE via `parse_hickory` (320 rows, 8 precincts,
  0 mismatches, 4 test groups OK). OCR artifacts universal in this county
  and likely in other Dominion SOVC counties — see "SOVC OCR row-shift"
  below.
- **Howard Dominion "Precinct Summary Report"** (2 pages/precinct, 8
  blocks): DONE via `parse_howard` (257 rows, 8 precincts, 0 mismatches,
  4 test groups OK). Names read visually from rendered images
  (/tmp/hw1-01.png etc.): Prairie-Burton, Bonne Femme, Moniteau, Franklin,
  Boonslick, Chariton, Richmond, Absentee — OCR kept the banner only on
  even-page "Page 2/2" strips for blocks 5-8. New OCR artifact fixed
  generically in `parse_precinct_summary`: candidate name split across two
  <tr>s (name part 1 + votes row, name-tail row with empty cells) — now
  appended to the previous row instead of emitting a 0-vote duplicate.

## SOVC OCR row-shift (IMPORTANT for other SOVC counties)

In Hickory's OCR, the numeric block is frequently shifted UP one row
relative to the precinct-label column: the 'Jurisdiction Wide' banner cell
carries the first precinct's numbers, each label gets the next precinct's
numbers, and a duplicate Total row follows. Candidate votes within a
shifted number-block stay internally consistent, so crosscheck (which only
validates candidate sums) CANNOT catch this — precinct attribution would
be silently scrambled. Detected in `parse_hickory` when the 'Jurisdiction
Wide' row carries numbers; re-paired positionally (number row i belongs to
label row i+1). Two related artifacts also handled:
- doubled label: 'Jurisdiction Wide' + precinct name split across two
  label cells (numbers already aligned);
- merged final labels: '<precinct> ABSENTEE' in one cell (expand, absentee
  numbers arrive on the following OCR row).

Also: Reg. Voters differs per contest — district/ward-restricted contests
(State House 126/128) report smaller reg values than county-wide pages, so
reg values can NOT be compared across contests; only same-coverage contests
share reg values. Verified page maps and per-precinct votes for Hickory
against rendered images (/tmp/hickory_p18r.png, hk_p8r.png, hk_p9r.png,
hk_p29r.png, hickory_p24-24.png).

## Chariton (SOVC, 21 pages) — DONE
288→312 rows, 13 precincts (12 + ABSENTEE as its own precinct, matching the
printed tables), parse via `parse_henry(..., "Chariton_MO_Official_Results_
08_04_26", use_transcripts=False)`. Verified: crosscheck 0 (18 certified
candidates + 6 local rows), all 4 external test groups OK, and my own
rendered-image reads of p2 (US House 6 REP, all 65 cells incl. the tricky
Liberty Bills 5/16/22/0/3 and Central Poll 0/0/7/0/0 rows) and p8 (US House
6 DEM) match the CSV exactly. New OCR artifacts fixed generically:
- **Over/Under votes leak**: trailing bare Over/Under value cells inflated
  the digit list so `digits[-n:]` shifted candidate attribution. Fix:
  `_henry_row_votes` reads votes positionally from the exact SOVC row
  layout `[reg, bc, %turnout, creg, tv, v1, p1, …, vn, pn, over, under]`
  (keeping `-` cells as placeholders) and validates against Total Votes;
  falls back to vote/% pairing (`_henry_pairs`), then the digit slice.
- **Generic cells[0] labels**: some tables print 'Jurisdiction Wide' in
  cells[0] and the precinct name in cells[1]; a promoted own-cell label is
  now passed as both block and sublabel so the walk never treats it as a
  county-total row. 'ABSENTEE' in cells[0] keeps its own block label.
- **Row-shift (Hickory-style) in single-value-row tables**: p8's DEM table
  had values moved UP onto the previous label (banner row carried ST
  MARY's votes, county Total landed on the ABSENTEE row); re-paired
  positionally (values of row i belong to the label of row i+1).
- HTML entities in labels (&#x27;) unescaped via `html.unescape`.
- AMENDED after the Benton work: the amendment pages (p18 etc.) print the
  table UNSHIFTED with the top 'Jurisdiction Wide' row a byte-identical
  DUPLICATE of the first precinct row and the true Total row at the
  bottom; the position-only row-shift re-pair originally misfired on
  these (scrambled per-precinct attribution, crosscheck caught it only
  via a −18 diff on Amendment 2). The re-pair is now cancelled when
  rows[1]'s values are byte-identical to rows[0]'s (dup signature). After
  the fix: 416 rows (zero-vote rows now kept), crosscheck 0, 4 test
  groups OK; hand-checked p18's full column set against certified
  (YES sums 66+204+52+82+52+157+121+71+393+47+62+6+84 = 1397 exactly).

## Henry (SOVC, 53 pages) — DONE
OCR damaged too many pages to trust standalone → rendered-image transcription
pipeline: subagents transcribed ALL pages to `2026/henry_transcripts/pNN.txt`
(TITLE/CANDS/rows TSV, TITLE2 marker for two-party pages; agents A-G covered
p3, p19-p36, p37-p43, p44-p51, p13-p18+p52-p53, p1-p12). `parse_henry`
prefers transcripts, falls back to OCR. Verification: (1) separate script
re-walks transcripts and compares per-precinct vote vectors positionally vs
CSV — 274/274 groups, 0 problems; (2) my own image reads of p2 (US House 4
REP) and p10 (Auditor DEM) match CSV exactly; (3) crosscheck vs certified
county totals 0 mismatches (30 certified candidates); (4) all 4 external
test groups OK. 518 rows, 10 precincts (BEAR CREEK/DEEPWATER, BENSON CENTER,
CLINTON WARD 1, FAIRVIEW, LEESVILLE, OSAGE, SPRINGFIELD/TEBO, URICH,
WINDSOR, ABSENTEE). 12 office groups: State Auditor (3 parties), U.S. House
4 (REP/DEM/LIB), State Senate 28, State House 126, various county offices
(REP/DEM), township committee contests (mostly single-precinct pages 19-51),
amendments 1/2/4/5 (no Amendment 3 on this ballot, statewide). OCR artifacts
documented: banner-shift (numbers move onto the precinct's banner row, Normal
row keeps zeros — parse_henry keeps the row with the larger vote sum),
candidate names split across two <tr> rows, merged cells "24 100.00%",
collapsed pages (hallucinated header junk); p11 is a turnout-only page with
no contest.

## Benton (SOVC, 28 pages) — DONE
Via `parse_henry(..., "Benton_MO_Official_Results_08_04_26",
use_transcripts=False)`. 395→350 rows, 8 precincts + ABSENTEE (matches the
report's "Number of Precincts 8"). Verified: crosscheck 0 (32 certified
candidates), all 4 external test groups OK, and my own rendered-image reads
of p7 (printed 4/25 — State House 57 REP: all cells incl. TOM's genuine
zero votes; precinct really is named TOM, not truncated) and p19 (printed
16/25 — compact prefix-2 US House DEM page: WHITE 13/4/7/1/8/6/6,
TOM 27/18/14/3/20/14/12, ABSENTEE 51/8/15/3/42/34/25) match the CSV
exactly. New OCR/parsing artifacts fixed generically:
- **Prefix-2 compact tables**: some pages print
  [Reg. Voters, Total Votes, v1, p1, …] (no turnout trio); prefix is now
  measured per contest from the header and validated against the first
  digit-bearing row (`_henry_row_votes` prefix arg, 4-tuple contests).
- **Truncated last candidate pair**: p19's 7-candidate table lost the
  final (Randy Miller) pair to OCR truncation — recovered as
  Total Votes minus the others (exact for single truncation).
- **Banner title fragments in candidate headers**: p20 printed 'STATE
  SENATOR DISTRICT' (colspan residue, from the banner row) as a candidate
  header cell, inflating n and emitting a zero-vote phantom candidate.
  Banner-row cells are now candidates ONLY when the header row confirms
  them; unconfirmed banner cells feed the title instead.
- **'0 -' merged cell** (vote + dash-%, e.g. TOM's zero row) was dropped
  from the value cells, deflating the row into the Ballots-Cast/turnout-%
  pairs fallback (wrong 576): the value-cell regex now keeps '0 -' and
  `_henry_row_votes` parses it as votes + dash percent.
- **'Absentee' subrow promotion**: value rows labeled 'Absentee' right
  after their block's 'Normal' row (Benton prints Normal/Absentee subrows
  under every banner, and under ABSENTEE too) were promoted into a
  phantom 'Absentee' precinct — now kept as subrows of the open block.
- Literal "\n" escapes in OCR cell text (e.g. "STATE AUDITOR\\n(REPUBLICAN
  PARTY)") are unescaped in `_henry_clean_label`.
- Garbled titles handled per page via `_BENTON_FALLBACK_TITLES`
  (fb replaces the title, keeping real candidates unless it's an
  amendment → force YES/NO). NOTE: shared-code changes re-verified Henry
  (518 rows, 0 mismatch, 274/274 transcript groups) and Chariton — see
  next section for the Chariton re-pair regression this surfaced.

- Iron, Lawrence, Lewis, Marion, McDonald OCR'd (all since parsed — see
  their sections); Montgomery and Morgan converted (batch 2, parse pending);
  Newton (80 pages) converting.

# Newton (Dominion "Precinct Summary", 80 pages) — DONE
`parse_mcdonald(county, cdir_name=..., page_titles=...)`. Two contests per
PDF page pair; every votes table's spillover slice (bottom of the internal
report page) appears as a separate PDF page whose ONLY real text is the
page footer ('Page: N of 60|80' — OCR emits it as a ## heading), which
`_MCD_JUNK_TITLE` now rejects; truly garbled footer titles
('l'ayus a la po', 'rage, 3 or 00', 't uye, o o', 'raye, 12 01 00',
'rage, le 01 00') are cleared via the `page_titles` override ("" forces
continuation). Fixes vs the McDonald first pass, all now generic in
`parse_mcdonald`:
- `_MCD_AGG` gained a generic '<name> county (- total)' alternative —
  Newton's 'Newton County - Total' aggregate rows previously loaded as a
  fake precinct, doubling every candidate sum.
- Write-in continuation tables (header cell exactly 'Write-in', rows
  Diamond 6 / Absentee 3 etc.) are now captured as candidate 'Write-Ins'
  and emitted with party WI (Stone/Cape convention) — 4 committee
  contests (Committeeman Diamond 6+3, Committeeman Staples #21 5+1,
  Crowder Committeeman 4, Crowder Committeewoman 1).
- `_MCD_FIXES` per-cache-dir label repairs: precincts 'Newtonla'→Newtonia
  (p065), 'Homet'→Hornet (p073); candidate 'tami (renfro) ovens'→
  'Tami (Renfro) Owens' (p047).
- p055 OCR lost its printed title entirely — page-title override 'Auditor
  (REP)' holds Matt Major's 27 contest rows (Newton County's Auditor
  office, local, not certified). Unopposed county offices get their own
  page printing only the winning candidate at 100.00% (Collector Jessica
  Crawford, County Clerk Tami (Renfro) Owens, ACJ Skouby, Recorder Jenny
  Cullers Childers, Pros Attorney William Lynch).
Cheryle Perkins IS (REP) — a three-way State House 160 Republican primary
(Wright 1257 / Russell 2053 / Perkins 3293); her table is on a title-less
continuation page and correctly merges into the SH160 REP contest.
Verified: crosscheck 0 (27 certified candidates), reverse-check all 27
certified keys present, all 4 external test groups OK, and image
verification of ALL 80 pages by 5 subagents (zero vote-value errors; all
zero-value Unresolved Write-In continuation pages are dropped by design).
29→27 precincts after the label repairs (23 precincts + Absentee/Central
Poll/Provisional/Federal).

## Known summary-only (no precinct CSV possible)
All Dominion Election Summary counties listed above + Laclede (garbled
text-layer, but the 3 per-precinct files cover only 3 of 15 precincts →
partial coverage, crosscheck would fail) + Knox (2 single-precinct +
absentee summaries of a larger precinct set → partial, skip) + Iron (OCR
batch 2 confirmed: Dominion Election Summary, county totals per contest
only, 4 pages; Montgomery OCR'd batch 2, 8 pages: county-total blocks in
the McDonald-style layout (## headings + Times Cast tables) WITHOUT
per-precinct pages; Morgan OCR'd batch 2, 4 pages "Election Summary
Report": county totals per contest only; Lewis OCR'd, 4 pages "Election
Summary Report" for its 8 precincts: county totals per contest only).

## Cole (Scytek, 33 pages) — DONE
`parse_cole` + `_COLE_IMAGE_FIXES` (dict of values OCR dropped but printed
on the page image, verified visually; applied post-parse keyed by contest).
Crosscheck 0, reverse-check 0, 4 test groups OK, per-precinct verification
against rendered page images. NOTE: for OCR'd counties, crosscheck only
validates candidate SUMS, never precinct attribution — row-shifts preserve
sums, so image verification of every page against `pdftoppm -r 120` renders
(REVERSE pattern of the Cole fix) is REQUIRED for every OCR county. Reverse
check: iterate all certified keys and flag any missing from the CSV — the
forward crosscheck silently skips candidates wholly absent from the CSV.

## Lawrence (ES&S "Official Election Results", 43+3 pages) — DONE
Two files (Precincts 43pp + Summary 3pp), `parse_lawrence` (generic ES&S
group layout, parse_adair's block detection). Three shift families
handled; precinct labels canonicalized against a label-canon list; OCR
typo 'Wice' repaired; unattributable value rows kept as a 'SkippedShifted'
placeholder; duplicate header rows deduped and stray 'Total ' header
residue stripped. Crosscheck 0, reverse-check clean, 4 test groups OK.

## McDonald (Dominion "Precinct Summary", 32 pages) — DONE
`parse_mcdonald`: per contest one page with a `## Title …` markdown heading
(NOT a center div) + a turnout table (Times Cast / Registered Voters
headers — skipped) + a votes table. County/McDonald-county aggregate rows
skipped but Absentee/Central Poll/Provisional/Federal kept as precincts.
Candidate columns can CONTINUE on the next title-less page (USH7 REP
continues on p011, PresComm REP on p020). Vote cells are combined
'59 71.95%' (colspan) or split '59'+'71.95%' pairs. 'Unresolved'+'Write-In'
may be two header cells or one combined 'Unresolved Write-In' cell —
`_MCD_SKIP_HDR` matches all forms (the combined form previously created a
phantom 'Write-Ins' candidate equal to the contest total). Titles carry a
dangling bare party word to strip ('State Auditor (REP) (Vote for 1) REP').
'James Mccool' capitalization matches existing repo convention
(Mccann/Mccollum). 444 rows, 16 precincts, crosscheck 18 checked/0
mismatch, 4 test groups OK, every page image-verified (p12-16, 18-20, 27-28
+ full-page reads). SOURCE GAP: Amendments 4/5 exist only in the p003
county summary (certified 813/2412 and 648/2581) — no precinct pages exist
in the source PDF; forward-check [?] + reverse-check 4 MISSING keys for
Amd 4/5, documented as a source limitation.

## Marion (Dominion "Statement of Votes Cast", 25 pages) — DONE
`parse_marion`. One contest per page (p001-p003 are aggregate-only
summaries). KEY TECHNIQUE: OCR shifts value rows up/down relative to the
wrapped two-line precinct labels (e.g. the 'Jurisdiction Wide' label row
carries 1ST WARD's numbers), so labels are NOT trusted — every value row is
keyed by the precinct's constant Reg. Voters count, unique per precinct
(`_MAR_REG` fingerprint map, built from the p006 image). Duplicate
(reg, votes) rows at wrap points (p009, p020) collapse via the per-precinct
dict. Title overrides in `_MAR_PAGE_TITLE` for OCR-lost banner rows (p006
PresComm REP, p012 Pros Attorney REP, p013 Collector of Revenue REP Donna
Goodin, p018 Collector DEM Teya Stice, p020 USH6 LIB Andy Maidment — that
whole contest would otherwise merge into the previous one or be dropped);
`_MAR_IMAGE_FIXES` for OCR-dropped values (p022 1st Ward NO=142; p025 adds
Miller [126,156] and all-zero Liberty [0,0] — the school proposition covers
only 9 precincts + Absentee; Circuit Clerk District II covers only 8 +
Absentee: genuinely district-restricted, Total Reg 12558). 769 rows,
17 precincts, reverse-check all 32 certified keys present, 4 test groups
OK, all 25 page images verified. SOURCE DISCREPANCY (documented, honest
data kept): the PDF's own printed Total rows EQUAL the extracted sums
(column sums verified by image review), but the SOS certified county file
is +3/+4 on every REP contest and all four amendments while DEM/LIB
contests match exactly (Auditor REP 3595 vs 3597, Amd 1 Yes 4895 vs 4899,
SS18 Sharpe 2444 vs 2447) — crosscheck reports 13 mismatches; the values
printed in the county's official SOVC are kept. Likely the certified
totals include late-canvass ballots absent from this 8/7 10:55 AM print.
## Ste. Genevieve (Dominion SOVC side-by-side, 40 pages) — DONE
`parse_ste_genevieve`. 393 rows, 13 precincts (12 + ABSENTEE), crosscheck 0,
reverse-check 0, 4 test groups OK, ALL 40 page images verified (5
subagents). Format: each page one contest, header turnout columns
(Reg. Voters | Ballots Cast | % Turnout) beside the contest columns;
data-row digits shift UP one OCR row relative to the wrapped two-line
precinct labels here too — the parser pairs number rows to labels
positionally, keyed by the precinct's constant Reg. Voters value.
Page-title overrides for OCR-lost banner rows (p009, p037, p039);
p022 is township-restricted (prints only its 4 precincts); internal page 7
missing from OCR cache (recovered from the image). One cosmetic OCR noise
row 'GORDON HESLOPH' reconciled to 'Gordon Heslop' downstream by
_reconcile_names. Printed Total sections verified for Amd 1 (3296/1072),
Amd 2 (1241), Amd 4 (688/3738), Amd 5 (524/3901). Local rows kept as [?]:
Associate Circuit Judge + committee posts.

## Worth (Official Election Returns, 2 pages) — DONE
`parse_worth`. 277 rows, 8 precincts: ABSENTEE, ALLEN, E FLETCHALL, GREENE,
MIDDLEFORK, SMITH, W FLETCHALL, W UNION (source prints 'E Fletchall'/
'W Fletchall'; OCR inserted an extra 'r' — fixed from 300dpi zooms).
Layout: candidates-as-rows x precinct-columns, 10-cell value rows, junk
trailing rows (Amendment 1-5 banners carrying 'Yes | 92' etc.) rejected.
Title remaps: 'AUDITOR' -> State Auditor, 'CLERK OF CIRCUIT COURT' ->
Circuit Clerk (OE convention), 'STATE REP 2ND DISTRICT' -> State House 2,
'STATE SENATOR 12th'/'US REPRESENTATIVE 6TH DISTRICT' -> State Senate 12 /
U.S. House 6. Amendment votes merged across party blocks into
`amends[(amend_no, prec_idx, cand)]`. 'Wistrand J' printed truncation
reconciled to Gerald (Jerry) Wistrand Jr. via certified rename.
DOCUMENTED SOURCE DISCREPANCIES: 5 printed rows are internally
inconsistent (page Total disagrees with its own precinct cells: Amd1 Yes
10 vs 9, Amd2 No 20 vs 0, Amd4 Yes 10 vs 1, Amd4 No 19 vs 9, Amd5 Yes 10
vs 1 — all LIB, [!] warnings fire); precinct cells are kept. Printed
county totals disagree with certified by ±1 on Amd rows (419/133, 623/146,
149/427, 92/470 printed vs 420/133, 624/145, 149/428, 92/471 certified) —
the 7 crosscheck mismatches are exactly those rows and are accepted.
Both page images verified. 9 [?] local rows (Auditor/Circuit Clerk/commitee
posts), reverse-check 0, tests OK.

## Platte (Dominion SOVC side-by-side matrix, 42 pages) — DONE
`parse_platte`. 1495 rows, 30 precincts (29 named + ABSENTEE), crosscheck
0, reverse-check 0, 4 test groups OK. Format: one contest per page, header
row0 = [Turnout, contest-title(+wrapped junk)], row1 = [Reg. Voters, Ballots
Cast, % Turnout, Reg. Voters, Total Votes, candidate names...]. Precinct
rows start with county reg + county ballots; ABSENTEE prints '-' for
county reg so its number sequence is one shorter (`_pl_row` handles both).
Merged 'v p%' cells keep their leading vote. KEY TECHNIQUE: p008's header
was reflowed by OCR (row0 'Mike Jones | Ryan Gerster', row1 'Sean Pouche |
Ryan Gerster'), so candidate columns are assigned by BIJECTING each
column's county sum onto the certified candidate totals (`_pl_assign`) —
falls back to header order only for local contests. Requires the
by_contest key lookup lowercased to match certified keys.
`_PL_FIX_ROWS` pages (transcribed whole from rendered images, with
per-row sum asserts):
- p033 (printed 32/42, Committeewoman 12-4): OCR collapsed the page into a
  single table and shifted rows; also `len(tables)==1` so the fix check
  must run BEFORE the two-tables requirement.
- p022 (printed 21/42, County Clerk REP): OCR dropped Jera Pruitt's whole
  vote column; 31-row transcription, column sums match the printed Total
  row (Calvillo 4456, Wojtkiewicz 3119, Pruitt 3411). 'Russ "Wojo"
  Wojtkiewicz' from the printed header.
- p024 (printed 23/42, Recorder of Deeds REP): OCR junk in the header row
  ('110', '33.85%') would have emitted candidate '110'; full 31-row
  transcription; row '41 Park Hill North' prints 206 66.03% / 106 33.97% /
  Total 312 (OCR read 208 in the cell; 300dpi zoom confirms the print is
  206 — 206+106=312 is consistent with the printed Total).
- p040 (printed 39/42, KC Question 2 GO Convention Bonds): row labels
  garbled ('1151' as a precinct name); 13-row transcription (Yes 6988 /
  No 4979 = Total 11967 printed). Office 'Question 2 Kansas City'.
Verification: image-verified by direct review (p022, p024, p040, p032,
p033) plus 7 subagents over the remaining 37 pages — all 41 data pages
(PDF 2-43) verified against 200/300dpi renders, zero numeric errors.
The subagent run against pages 8-13 confirmed every SS34-REP precinct
value (e.g. 01 Riverside = 71/268/31; column sums 4166/6557/885 =
printed Total) and its initial flag that Jones/Gerster were swapped
came from a stale pre-fix dump; the final CSV assigns names by
certified-total bijection and matches the printed column order exactly.

## Candidate-name case repair (ALL 2026 counties) — done
`fix_candidate_case.py`. titlecase_name() naive-.title()s every name, so
Mc/Mac surnames were emitted as 'Judy Mcneece', 'Tracy Mccreery',
'Mary Mckenna' etc.; crosscheck compares lowercase so nothing failed —
1012 certified rows quietly wrong. The repair rewrites each candidate
label to the certified file's own (correct) spelling when the lowercase
(contest, dist, party, candidate) key matches, plus a LOCAL_NAME_FIX map
for local-contest candidates (printed mixed-case spellings read from the
county source PDFs: 'Steven E. McIntosh', 'Shad McGuire', 'Mary McKenna',
'Gail McCann Beatty', ...). NOTE: 'Roth Mcelvain' is the certified file's
own spelling and is kept as-is. Reverse-check re-run over all county CSVs:
0 gaps. If titlecase_name is ever improved, this pass remains harmless.
