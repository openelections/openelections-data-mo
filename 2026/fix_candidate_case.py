"""Restore certified capitalization for candidate names in 2026/counties
CSVs. crosscheck() compares lowercase keys, so titlecase_name()'s naive
.title() (Judy McNeece -> Judy Mcneece, Mary McKenna -> Mary Mckenna) passed
all checks while mangling Mc/Mac/O' names. Rows whose lowercase
(office, dist, party, candidate) key is in the certified county file get the
certified spelling; rows that don't match anything certified are reported
for manual review (local contests)."""
import csv
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from parse_primary_pdfs_more import CERTIFIED_PATH  # noqa: E402

# certified (office_l, dist, party, cand_l) -> certified candidate spelling
cert_name = {}
with open(CERTIFIED_PATH, newline="") as f:
    for r in csv.DictReader(f):
        cert_name[(r["office"].lower(), r["district"], r["party"],
                   r["candidate"].lower())] = r["candidate"]

# local-contest candidates (not in the certified file), spelling taken from
# the printed source documents (mixed-case where they print it, otherwise
# the standard Irish Mc-name casing)
LOCAL_NAME_FIX = {
    "J D Mcfarland": "J D McFarland",
    "Trisha Elizabeth Mcculloch": "Trisha Elizabeth McCulloch",
    "Joseph G Mcculloch": "Joseph G McCulloch",
    "Michelle D Mcbride": "Michelle D McBride",
    "Steve Mcbride": "Steve McBride",
    "Teresa Mcbride": "Teresa McBride",
    "Brian C. Mccollum": "Brian C. McCollum",
    "Steven E. Mcintosh": "Steven E. McIntosh",
    "Lon Mcdonald": "Lon McDonald",
    "Mary Mckenna": "Mary McKenna",
    "Lincoln Mcguire": "Lincoln McGuire",
    "Mike Mcduffey": "Mike McDuffey",
    "James Mccool": "James McCool",
    "Sheila Mcgrew": "Sheila McGrew",
    "Wendi Mcghee": "Wendi McGhee",
    "Stacie Mccutcheon": "Stacie McCutcheon",
    "Christina Mcdonough Hunt": "Christina McDonough Hunt",
    "Gail Mccann Beatty": "Gail McCann Beatty",
    "Danny C. Mcbee": "Danny C. McBee",
    "Shad Mcguire": "Shad McGuire",
    "Heather Mcguire": "Heather McGuire",
    # NOTE: 'Roth Mcelvain' is the certified file's own spelling — left as-is
}

changed, suspects = 0, {}
for path in sorted(glob.glob(os.path.join(HERE, "counties",
                                          "*.csv"))):
    rows = list(csv.reader(open(path, newline="")))
    out, dirty = [rows[0]], False
    for r in rows[1:]:
        if len(r) < 7:
            out.append(r)
            continue
        key = (r[2].lower(), r[3], r[4], r[5].lower())
        good = cert_name.get(key) or LOCAL_NAME_FIX.get(r[5])
        if good and good != r[5]:
            suspects.setdefault((r[5], good), 0)
            suspects[(r[5], good)] += 1
            r = r[:5] + [good] + r[6:]
            dirty = True
            changed += 1
        elif not good:
            if re.search(r"\b(Mc|Mac|Mc')[a-z]", r[5]) \
                    or r[5].lower() in ("write-ins", "yes", "no"):
                pass
            if re.search(r"\bMc[a-z]", r[5]):
                suspects.setdefault((r[5], "---"), 0)
                suspects[(r[5], "---")] += 1
        out.append(r)
    if dirty:
        rel = os.path.relpath(path, HERE)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(out)
        print(f"fixed {rel}")

print(f"\n{changed} candidate cells updated; local-only suspects:")
for (bad, good), n in sorted(suspects.items(), key=lambda x: -x[1]):
    print(f"{n:5} {bad!r} -> {good!r}")