#!/usr/bin/env python3
"""Probe specific pages of specific text-layer PDFs to see data-row layouts."""
import sys

from natural_pdf import PDF

from parse_primary_sources import SRC_DIR  # noqa: E402

TARGETS = [
    ("St Charles MO SOVC Official 08.04.26.pdf", [1, 2]),
    ("St Francois MO SOVC Official 08.04.26.pdf", [1, 2]),
    ("Lafayette MO Certified County Wide Official Results 08.04.26.pdf", [2, 3]),
    ("Phelps MO Election Summary Official 08.04.26.pdf", [1, 2]),
    ("Pike MO Official Results ALL 08.04.26.pdf", [1, 2]),
    ("Ralls MO Official Results Totals 08.04.26.pdf", [1, 2]),
    ("Webster MO Final Official Results 08.04.26.pdf", [1, 2]),
    ("St Louis County MO Canvass Results Precinct Official 08.04.26.pdf", [2]),
    ("Cape Girardeau MO Details by Precinct Official 08.04.26.pdf", [2, 3]),
    ("Greene MO Precinct Detail Official 08.04.26.pdf", [2]),
    ("KC Ward Precinct Canvass 08.04.26.pdf", [2, 3]),
]

def main():
    which = sys.argv[1] if len(sys.argv) > 1 else None  # filter by county substring
    for fn, pages in TARGETS:
        if which and which.lower() not in fn.lower():
            continue
        path = SRC_DIR.joinpath(fn) if hasattr(SRC_DIR, "joinpath") else SRC_DIR + "/" + fn
        pdf = PDF(str(path))
        print(f"\n########## {fn}")
        for pn in pages:
            page = pdf.pages[pn - 1]
            t = page.extract_text() or ""
            print(f"----- page {pn}")
            for ln in [ln for ln in t.splitlines() if ln.strip()][:38]:
                print("   |", ln[:120])

if __name__ == "__main__":
    main()