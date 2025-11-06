#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SUSE KB scraper — URL-list based, visible-text extraction, incremental, verbose.

• Reads article URLs from:
    --urls-csv suse_kb_urls.csv   (must contain 'url' column), OR
    --urls-txt urls.txt           (one URL per line)

• For each article:
    - opens the page in Chromium (headless or headed),
    - accepts cookie banners,
    - recovers from Salesforce "Sorry to interrupt / CSS Error" by refreshing,
    - extracts the **visible text** (like manual copy/paste) from a content container,
    - parses common sections from the extracted text,
    - skips if Modified Date + content hash unchanged (state.json),
    - writes TXT (prepped format) + merges/upserts CSV.

Install (first time):
  pip install playwright bs4 lxml pandas python-dateutil tenacity tqdm
  python -m playwright install chromium
"""

import asyncio
import csv
import json
import os
import re
import sys
import hashlib
import argparse
import logging
from pathlib import Path
from datetime import datetime
from dateutil.relativedelta import relativedelta

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger("suse_kb")

# ------------------ Settings / Heuristics ------------------

SECTION_LABELS = [
    "Environment", "Situation", "Cause", "Resolution",
    "Status", "Disclaimer", "Additional Information",
    "Applies To", "Creation Date", "Modified Date", "Document ID",
]

CONSENT_BUTTONS = (
    "Accept All", "Accept all", "Accept all cookies",
    "Accept Cookies", "I agree", "Allow all"
)

CSV_FIELDS = [
    "article_number",
    "title",
    "document_id",
    "creation_date",
    "modified_date",
    "environment",
    "situation",
    "cause",
    "resolution",
    "status",
    "additional_information",
    "applies_to",
    "product_tags",
    "url",
]

RE_DOC_ID = re.compile(r"\b(Document\s*ID)\s*:\s*([A-Za-z0-9._-]+)", re.I)
RE_CREATION = re.compile(r"\b(Creation\s*Date)\s*:\s*([0-9A-Za-z\-/., ]+)", re.I)
RE_MODIFIED = re.compile(r"\b(Modified\s*Date)\s*:\s*([0-9A-Za-z\-/., ]+)", re.I)
RE_ARTICLE_NUMBER_IN_URL = re.compile(r"/article/([^/?#]+)")
RE_ARTICLE_NUMBER_TEXT = re.compile(r"\b(KB[_-]?\d{6,})\b", re.I)

# ------------------ Logging ------------------

def setup_logging(debug: bool, verbose: bool):
    level = logging.WARNING
    if verbose:
        level = logging.INFO
    if debug:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not debug:
        logging.getLogger("asyncio").setLevel(logging.ERROR)
        logging.getLogger("playwright").setLevel(logging.ERROR)

# ------------------ Small utils ------------------

def sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="ignore")).hexdigest()

def sanitize_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^\w\s.-]", "", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:max_len].strip().replace(" ", "_") or "untitled")

def parse_date_loose(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%b %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return pd.to_datetime(s, errors="coerce").to_pydatetime()
    except Exception:
        return None

def within_last_months(dt_str: str, months: int) -> bool:
    if not dt_str:
        return False
    dt = parse_date_loose(dt_str)
    if not dt:
        # If unparsable, keep (avoid dropping potentially relevant articles)
        return True
    threshold = datetime.now() - relativedelta(months=months)
    return dt >= threshold

def infer_article_number(url: str, title: str, visible_text: str) -> str:
    m = RE_ARTICLE_NUMBER_IN_URL.search(url or "")
    if m:
        return m.group(1)
    for source in (title, visible_text):
        m = RE_ARTICLE_NUMBER_TEXT.search(source or "")
        if m:
            return m.group(0).replace("-", "_").upper()
    return ""

# ------------------ Visible text helpers ------------------

async def accept_cookies_if_any(page):
    for label in CONSENT_BUTTONS:
        try:
            btn = page.get_by_role("button", name=label)
            if await btn.count():
                await btn.first.click()
                log.debug("CONSENT: clicked '%s'", label)
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

async def get_visible_text(page):
    """
    Try multiple likely containers; fall back to full-body innerText.
    This mimics manual copy (preserves human-facing line breaks).
    """
    # Common Salesforce content wrappers
    candidates = [
        "article",
        "div.slds-rich-text-editor__output",
        "div.slds-card__body",
        "div.slds-p-around--medium",
        "main",
        "div[role='main']",
        "div.siteforceContentArea",
        "div.contentRegion",
        "div.content",
        "div.container",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if await loc.count():
                # Find a sizeable one
                for i in range(min(6, await loc.count())):
                    t = await loc.nth(i).inner_text()
                    if t and len(t.strip()) > 200:
                        return t
        except Exception:
            pass
    # Fallback: whole document body
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""

def split_sections_from_text(visible_text: str):
    """
    Split plain visible text into sections by headings (Environment, Situation, etc.).
    Strategy:
      - Normalize whitespace.
      - Treat any line that exactly matches a known label (case-insensitive),
        or starts with 'Label:' as the start of that section.
      - Capture until the next label.
    Returns dict with cleaned fields and a best-effort title (first non-empty line).
    """
    # Normalize Windows/Mac newlines and excessive blank lines
    text = visible_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]

    # Guess title: first non-empty line that's not just a label
    title = ""
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if t.lower() in [l.lower() for l in SECTION_LABELS]:
            continue
        if re.match(r"^(Environment|Situation|Cause|Resolution|Status|Disclaimer|Additional Information|Applies To|Creation Date|Modified Date|Document ID)\s*:\s*$", t, re.I):
            continue
        title = t
        break

    label_set = {l.lower(): l for l in SECTION_LABELS}
    current_label = None
    buckets = {l: [] for l in SECTION_LABELS}  # accumulate lines per section

    def is_label_line(s: str):
        s1 = s.strip()
        if s1.lower() in label_set:
            return label_set[s1.lower()]
        m = re.match(r"^\s*([A-Za-z ]{3,40})\s*:\s*$", s)
        if m:
            cand = m.group(1).strip().lower()
            if cand in label_set:
                return label_set[cand]
        return None

    for ln in lines:
        lab = is_label_line(ln)
        if lab:
            current_label = lab
            continue
        if current_label:
            buckets[current_label].append(ln)

    # Join/clean
    cleaned = {}
    for k, arr in buckets.items():
        blob = "\n".join(arr).strip()
        # Collapse >2 consecutive blank lines
        blob = re.sub(r"\n{3,}", "\n\n", blob)
        cleaned[k] = blob

    # Extract dates/ID from anywhere if not captured in labeled buckets
    full = "\n".join(lines)
    document_id = ""
    creation_date = ""
    modified_date = ""

    # Prefer explicit bucket content first
    if cleaned.get("Document ID"):
        m = RE_DOC_ID.search("Document ID: " + cleaned.get("Document ID", ""))
        if m: document_id = m.group(2).strip()
    if not document_id:
        m = RE_DOC_ID.search(full)
        if m: document_id = m.group(2).strip()

    if cleaned.get("Creation Date"):
        creation_date = cleaned.get("Creation Date", "").strip()
    if not creation_date:
        m = RE_CREATION.search(full)
        if m: creation_date = m.group(2).strip()

    if cleaned.get("Modified Date"):
        modified_date = cleaned.get("Modified Date", "").strip()
    if not modified_date:
        m = RE_MODIFIED.search(full)
        if m: modified_date = m.group(2).strip()

    # Map to CSV fields
    fields = {
        "title": title.strip(),
        "document_id": document_id,
        "creation_date": creation_date,
        "modified_date": modified_date,
        "environment": cleaned.get("Environment", "").strip(),
        "situation": cleaned.get("Situation", "").strip(),
        "cause": cleaned.get("Cause", "").strip(),
        "resolution": cleaned.get("Resolution", "").strip(),
        "status": cleaned.get("Status", "").strip(),
        "additional_information": cleaned.get("Additional Information", "").strip(),
        "applies_to": cleaned.get("Applies To", "").strip(),
        # Salesforce sometimes shows product tags as pills; visible-text capture may miss them.
        "product_tags": "",
    }
    return fields

# ------------------ Fetch & parse ------------------

@retry(wait=wait_exponential(multiplier=1, min=1, max=20),
       stop=stop_after_attempt(5),
       retry=retry_if_exception_type((PWTimeout,)))
async def fetch_visible_text(context, url: str):
    """
    Open article, accept cookies, recover from interstitials, and return visible text.
    """
    page = await context.new_page()
    try:
        log.debug("GET: %s", url)
        await page.goto(url, wait_until="load", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        await accept_cookies_if_any(page)

        # Interstitial recovery loop
        interstitial_sigs = (
            "Sorry to interrupt", "CSS Error", "An internal server error has occurred",
            "Something has gone wrong", "We can’t complete your request"
        )
        html = await page.content()
        tries = 2
        while tries and any(sig in html for sig in interstitial_sigs):
            log.warning("ARTICLE: interstitial/error detected — refreshing (%d left)", tries)
            try:
                refresh = page.get_by_text("Refresh")
                if await refresh.count():
                    await refresh.first.scroll_into_view_if_needed()
                    await refresh.first.click()
                else:
                    await page.evaluate("() => location.reload(true)")
            except Exception:
                await page.reload(wait_until="load")
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(800)
            await accept_cookies_if_any(page)
            html = await page.content()
            tries -= 1

        # Now get visible text
        vis = await get_visible_text(page)
        return vis
    finally:
        await page.close()

# ------------------ Formatting & state ------------------

def format_txt_like_example(row: dict) -> str:
    parts = []
    title_line = f"{row.get('article_number','')} {row.get('title','')}".strip()
    if title_line:
        parts.append(title_line)
    parts.append("")
    def add(label, key):
        v = (row.get(key) or "").strip()
        if v:
            parts.append(f"{label}\n{v}\n")
    add("Environment", "environment")
    add("Situation", "situation")
    add("Cause", "cause")
    add("Resolution", "resolution")
    add("Status", "status")
    add("Additional Information", "additional_information")
    add("Applies To", "applies_to")

    meta = []
    if row.get("document_id"):  meta.append(f"Document ID: {row['document_id']}")
    if row.get("creation_date"): meta.append(f"Creation Date: {row['creation_date']}")
    if row.get("modified_date"): meta.append(f"Modified Date: {row['modified_date']}")
    if meta: parts.append(" ".join(meta))
    if row.get("product_tags"): parts.append(row["product_tags"])
    if row.get("url"): parts.append(row["url"])
    return "\n".join(parts).strip() + "\n"

def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            log.info("STATE: loaded %d entries from %s", len(data), path)
            return data
    log.info("STATE: no previous state at %s", path)
    return {}

def save_state(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    log.info("STATE: saved %d entries to %s", len(data), path)

def load_existing_csv(csv_path: str) -> dict:
    if not os.path.exists(csv_path):
        log.info("CSV: starting fresh (%s not found)", csv_path)
        return {}
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    rows = {}
    for _, r in df.iterrows():
        rows[r.get("url","")] = {k: str(r.get(k,"")) for k in CSV_FIELDS}
    log.info("CSV: loaded %d existing rows from %s", len(rows), csv_path)
    return rows

def merge_rows(old: dict, new_rows: list) -> list:
    merged = dict(old)
    for r in new_rows:
        merged[r.get("url","")] = r
    rows = list(merged.values())
    rows.sort(key=lambda r: (r.get("modified_date") or "", r.get("title") or ""), reverse=True)
    return rows

# ------------------ URL loaders ------------------

def load_urls_from_csv(path: str) -> list:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "url" not in df.columns:
        raise ValueError(f"{path} must contain a 'url' column")
    urls = [u.strip() for u in df["url"].tolist() if u.strip()]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def load_urls_from_txt(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    seen, out = set(), []
    for u in lines:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# ------------------ Main ------------------

async def main():
    ap = argparse.ArgumentParser(description="SUSE KB scraper (URL-list, visible-text, incremental).")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--urls-csv", help="CSV file with a 'url' column (preferred).")
    grp.add_argument("--urls-txt", help="Plain text file with one URL per line.")
    ap.add_argument("--months", type=int, default=24, help="Lookback window in months (by article 'Modified Date').")
    ap.add_argument("--outcsv", default="suse_kb_last2yrs.csv", help="Output CSV path.")
    ap.add_argument("--outdir", default="out_txt", help="Directory to write TXT files.")
    ap.add_argument("--state", default="state.json", help="State manifest for incremental runs.")
    ap.add_argument("--concurrency", type=int, default=2, help="Article fetch concurrency.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--slowmo", type=int, default=250, help="Playwright slowMo (ms).")
    ap.add_argument("--debug", action="store_true", help="Enable DEBUG logging.")
    ap.add_argument("--verbose", action="store_true", help="Enable INFO logging.")
    args = ap.parse_args()

    setup_logging(args.debug, args.verbose)
    log.info("START: urls_csv=%s urls_txt=%s months=%d headless=%s slowmo=%d concurrency=%d",
             args.urls_csv, args.urls_txt, args.months, args.headless, args.slowmo, args.concurrency)

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load URLs
    urls = load_urls_from_csv(args.urls_csv) if args.urls_csv else load_urls_from_txt(args.urls_t
xt)
    log.info("URLS: loaded %d unique URLs", len(urls))

    state = load_state(args.state)
    old_csv_rows = load_existing_csv(args.outcsv)

    rows_out = []
    sem = asyncio.Semaphore(args.concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, slow_mo=args.slowmo)
        context = await browser.new_context()

        async def process(url):
            async with sem:
                try:
                    visible_text = await fetch_visible_text(context, url)
                except Exception as e:
                    log.warning("WARN: fetch failed %s: %s", url, e)
                    return

                if not visible_text or len(visible_text.strip()) < 50:
                    log.warning("WARN: empty/too-short visible text -> %s", url)
                    return

                fields = split_sections_from_text(visible_text)

                # If Modified Date missing, keep the item (we can’t be sure)
                mod_str = (fields.get("modified_date") or "").strip()
                if mod_str and not within_last_months(mod_str, args.months):
                    log.debug("FILTER: outside window (article) -> %s (modified=%s)", url, mod_str)
                    return

                # Title fallback: first line of text if section parser missed it
                title = fields.get("title") or visible_text.strip().split("\n", 1)[0]

                article_number = infer_article_number(url, title, visible_text)

                row = {
                    "article_number": article_number,
                    "title": title.strip(),
                    "document_id": fields.get("document_id","").strip(),
                    "creation_date": fields.get("creation_date","").strip(),
                    "modified_date": mod_str,
                    "environment": fields.get("environment","").strip(),
                    "situation": fields.get("situation","").strip(),
                    "cause": fields.get("cause","").strip(),
                    "resolution": fields.get("resolution","").strip(),
                    "status": fields.get("status","").strip(),
                    "additional_information": fields.get("additional_information","").strip(),
                    "applies_to": fields.get("applies_to","").strip(),
                    "product_tags": fields.get("product_tags","").strip(),
                    "url": url,
                }

                # Prepare file path
                title_for_file = sanitize_filename(row["title"])
                kb_for_file = (row["article_number"] or "KB").replace("/", "_")
                txt_path = (Path(args.outdir) / f"{kb_for_file}_{title_for_file}.txt").as_posix()

                # Build TXT body from parsed fields; if some sections empty, we still keep them empty
                txt_body = format_txt_like_example(row)
                body_hash = sha1(txt_body)

                prev = state.get(url)
                if prev:
                    prev_mod = (prev.get("modified_date") or "").strip()
                    prev_hash = prev.get("sha1") or ""
                    prev_path = prev.get("txt_path") or txt_path
                    if prev_mod == row["modified_date"] and prev_hash == body_hash and os.path.exists(prev_path):
                        log.info("SKIP: unchanged (mod+hash) -> %s", url)
                        return

                # Write file if new or changed
                write_needed = True
                if prev and prev.get("sha1") == body_hash and os.path.exists(prev.get("txt_path", txt_path)):
                    write_needed = False

                if write_needed:
                    Path(args.outdir).mkdir(parents=True, exist_ok=True)
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(txt_body)
                    log.info("WRITE: %s", txt_path)
                else:
                    log.info("SKIP-WRITE: unchanged content -> %s", url)

                # Update state & collect for CSV
                state[url] = {
                    "modified_date": row["modified_date"],
                    "txt_path": txt_path,
                    "sha1": body_hash,
                    "article_number": row["article_number"],
                    "title": row["title"],
                }
                rows_out.append(row)

        if urls:
            log.info("SCRAPE: starting (%d URLs, concurrency=%d, slowmo=%d)", len(urls), args.concurrency, args.slowmo)
            for idx in tqdm(range(0, len(urls), args.concurrency), desc="Scraping", unit="batch"):
                batch = urls[idx:idx+args.concurrency]
                await asyncio.gather(*(process(u) for u in batch))
        else:
            log.warning("SCRAPE: no URLs to process.")

        await browser.close()

    # Merge CSV
    merged_rows = merge_rows(old_csv_rows, rows_out)
    with open(args.outcsv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in merged_rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    log.info("CSV: wrote %d rows -> %s (new/updated this run: %d)", len(merged_rows), args.outcsv, len(rows_out))

    save_state(args.state, state)
    log.info("DONE.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")
