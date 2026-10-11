#!/usr/bin/env python3
"""
Submit the image-only 2026 Missouri primary precinct PDFs to PaddleOCR-VL
(AI Studio cloud) and cache the per-page markdown locally.

MO adaptation of ~/code/openelections-data-al/convert_canvass_pdfs_paddleocr.py's
job layer: convert + cache ONLY, no parsing -- 2026/parse_ocr_markdown.py parses
each county's markdown against the certified county-totals file afterward.

Auth: $PADDLEOCR_TOKEN, repo-local git-ignored .paddleocr_token, or
~/.paddleocr_token.  Treat OCR output as noisy; the certified totals are the
verification gate (CLAUDE.md).

    uv run 2026/paddleocr_convert.py [substring1 substring2 ...]

With no arguments it scans every 2026/primary PDF, reports which ones have a
bare text layer, and queues the image-only ones.
"""
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_primary_sources import SRC_DIR  # noqa: E402

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"
CACHE_DIR = os.environ.get("PADDLEOCR_CACHE",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "ocr_cache"))
OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
POLL_SECONDS = 5


def _token():
    tok = os.environ.get("PADDLEOCR_TOKEN")
    if tok:
        return tok.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(os.path.dirname(here), ".paddleocr_token"),
                 os.path.expanduser("~/.paddleocr_token")):
        if os.path.exists(path):
            return open(path).read().strip()
    raise SystemExit("No PaddleOCR token: set $PADDLEOCR_TOKEN or create "
                     ".paddleocr_token / ~/.paddleocr_token")


def _retry(fn, attempts=8, wait=5, what="request"):
    """The AI Studio API drops ~half of all incoming TLS handshakes
    (connection reset or 30s+ hang), so every HTTP call needs this wrapper."""
    import urllib3
    urllib3.disable_warnings()
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            print(f"    paddleocr {what} attempt {i+1}/{attempts} failed "
                  f"({type(e).__name__}), retrying", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"PaddleOCR {what} failed after {attempts} attempts") \
        from last


def _submit_job(pdf_path, token):
    headers = {"Authorization": f"bearer {token}"}
    data = {"model": MODEL, "optionalPayload": json.dumps(OPTIONAL_PAYLOAD)}

    last = None
    for i in range(30):
        try:
            with open(pdf_path, "rb") as f:
                r = requests.post(JOB_URL, headers=headers, data=data,
                                  files={"file": f}, timeout=300)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            print(f"    submit attempt {i+1}/30 failed "
                  f"({type(e).__name__}), retrying", flush=True)
            time.sleep(5)
            continue
        if r.status_code in (400, 401, 403, 422):
            if "10010" in r.text:  # queue full; wait for jobs to drain
                print("    queue full, waiting 60s", flush=True)
                last = RuntimeError("queue full (10010)")
                time.sleep(60)
                continue
            raise RuntimeError(f"PaddleOCR submit rejected ({r.status_code}): "
                               f"{r.text[:400]}")
        r.raise_for_status()
        return r.json()["data"]["jobId"]
    raise RuntimeError(f"PaddleOCR submit failed after 30 attempts") from last


def _poll_job(job_id, token):
    headers = {"Authorization": f"bearer {token}"}
    while True:
        try:
            d = requests.get(f"{JOB_URL}/{job_id}", headers=headers,
                             timeout=30).json()["data"]
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            time.sleep(POLL_SECONDS)
            continue
        state = d["state"]
        if state == "done":
            return d["resultUrl"]["jsonUrl"]
        if state == "failed":
            raise RuntimeError(f"PaddleOCR job failed: {d.get('errorMsg')}")
        prog = d.get("extractProgress") or {}
        if state == "running" and "totalPages" in prog:
            print(f"    running {prog.get('extractedPages')}/{prog['totalPages']}"
                  " pages", flush=True)
        time.sleep(POLL_SECONDS)


def _fetch_pages(jsonl_url):
    def call():
        r = requests.get(jsonl_url, timeout=120)
        r.raise_for_status()
        return r

    r = _retry(call, what="download")
    pages = []
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        for res in json.loads(line)["result"]["layoutParsingResults"]:
            pages.append(res["markdown"]["text"])
    return pages


def cache_name(path):
    return re.sub(r"[^A-Za-z0-9]+", "_",
                  os.path.splitext(os.path.basename(path))[0])


def is_done(path):
    return os.path.exists(os.path.join(CACHE_DIR, cache_name(path),
                                       ".complete"))


def convert_chunked(path, token, chunk_pages=5):
    """Large-PDF fallback: the AI Studio endpoint resets uploads of the
    biggest PDFs (6-13MB, e.g. Chariton/Benton/Cole) while taking ≤2.5MB
    ones fine, so split the file into page chunks, OCR each and write the
    combined per-page markdown under the same cache directory."""
    from pypdf import PdfReader, PdfWriter
    import io
    cache = os.path.join(CACHE_DIR, cache_name(path))
    os.makedirs(cache, exist_ok=True)
    reader = PdfReader(path)
    npages = len(reader.pages)
    # resume: reuse pages already cached from an earlier chunked attempt
    pages = []
    i = 1
    while True:
        p = os.path.join(cache, f"p{i:03d}.md")
        if os.path.exists(p):
            pages.append(open(p).read())
            i += 1
        else:
            break
    cached = len(pages)
    if cached:
        print(f"    resuming at page {cached + 1}/{npages}", flush=True)
    for start in range(cached, npages, chunk_pages):
        writer = PdfWriter()
        for i in range(start, min(start + chunk_pages, npages)):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunk_file = os.path.join(CACHE_DIR, "chunk.pdf")
        with open(chunk_file, "wb") as f:
            f.write(buf.getvalue())
        print(f"    chunk {start + 1}-{min(start + chunk_pages, npages)}"
              f"/{npages}", flush=True)
        job_id = _submit_job(chunk_file, token)
        jsonl_url = _poll_job(job_id, token)
        new_pages = _fetch_pages(jsonl_url)
        for j, raw in enumerate(new_pages):
            with open(os.path.join(cache, f"p{start + j + 1:03d}.md"),
                      "w") as f:
                f.write(raw)
        pages.extend(new_pages)
    if os.path.exists(os.path.join(CACHE_DIR, "chunk.pdf")):
        os.remove(os.path.join(CACHE_DIR, "chunk.pdf"))
    open(os.path.join(cache, ".complete"), "w").write(str(len(pages)))
    print(f"  got {len(pages)} pages", flush=True)
    return len(pages), "ok-chunked"


def convert(path, token):
    cache = os.path.join(CACHE_DIR, cache_name(path))
    os.makedirs(cache, exist_ok=True)
    done_marker = os.path.join(cache, ".complete")
    if os.path.exists(done_marker):
        return len(list(os.scandir(cache))), "cached"
    print(f"  submitting {os.path.basename(path)}", flush=True)
    # oversize PDFs: straight to chunked submission
    if os.path.getsize(path) > 3_000_000:
        return convert_chunked(path, token)
    try:
        job_id = _submit_job(path, token)
    except RuntimeError:
        return convert_chunked(path, token)
    jsonl_url = _poll_job(job_id, token)
    pages = _fetch_pages(jsonl_url)
    for i, raw in enumerate(pages, start=1):
        open(os.path.join(cache, f"p{i:03d}.md"), "w").write(raw)
    open(done_marker, "w").write(str(len(pages)))
    print(f"  got {len(pages)} pages", flush=True)
    return len(pages), "ok"


def has_text_layer(path, min_chars=400):
    """A PDF counts as image-only if no page (sampled) yields a real text
    layer.  natural_pdf is heavy; pypdf's extract_text is enough here -- fall
    back to natural_pdf when pypdf is unavailable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        from natural_pdf import PDF
        pdf = PDF(path)
        return any((pg.extract_text() or "").strip()
                   for pg in pdf.pages[: min(5, len(pdf.pages))])
    r = PdfReader(path)
    n = 0
    for pg in r.pages[: min(5, len(r.pages))]:
        t = (pg.extract_text() or "").strip()
        n += len(t)
    return n >= min_chars


def main():
    from natural_pdf import PDF
    token = _token()
    filters = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
    names = sorted(os.listdir(SRC_DIR))
    todo = []
    for fn in names:
        if not fn.lower().endswith(".pdf"):
            continue
        if filters and not any(x in fn.lower() for x in filters):
            continue
        path = os.path.join(SRC_DIR, fn)
        if is_done(path):
            continue
        todo.append(path)
    # drop the ones with a usable text layer (they go through natural_pdf)
    scanned = []
    print(f"{len(todo)} unprocessed PDFs; checking text layers ...")
    for path in todo:
        if has_text_layer(path):
            print(f"  text layer, skip: {os.path.basename(path)}")
        else:
            scanned.append(path)
    print(f"{len(scanned)} image-only PDF(s) to OCR:")
    for p in scanned:
        print("  -", os.path.basename(p))
    failed = []
    for p in scanned:
        try:
            convert(p, token)
        except Exception as e:  # keep the batch going
            print(f"  FAILED {os.path.basename(p)}: {e}", flush=True)
            failed.append(os.path.basename(p))
    if failed:
        print(f"\n{len(failed)} failed: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())