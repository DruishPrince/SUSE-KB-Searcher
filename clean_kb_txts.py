#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Batch-clean SUSE KB text files and (optionally) a CSV.

What it does:
  1) Removes known boilerplate UI lines:
       - "Start Tour"
       - "How can we help you?"
       - "Customer Center"
       - "Give Feedback"
     (matching is case-insensitive; trims surrounding whitespace)

  2) Shortens any embedded 'file://...' PDF path to just its basename.
     Example:
        file:///D:/.../pdf/httpssupport.scc.suse.comskbXYZlanguageen_US.pdf
        -> httpssupport.scc.suse.comskbXYZlanguageen_US.pdf

  3) (Optional) Updates a CSV's 'url' column similarly when it contains 'file://...'.

Usage:
  # Dry-run (shows what would change; no files modified)
  python clean_kb_txts.py --dir out_txt --dry-run

  # Apply changes to .txt files, create .bak backups
  python clean_kb_txts.py --dir out_txt

  # Also clean a CSV's 'url' column (writes new CSV; keeps original as .bak)
  python clean_kb_txts.py --dir out_txt --csv suse_kb_last2yrs.csv

  # No backups, force write
  python clean_kb_txts.py --dir out_txt --no-backup
"""

import argparse
import os
import re
from pathlib import Path
from typing import Tuple, List

# --- Configurable boilerplate phrases (case-insensitive exact-line matches after strip)
BOILERPLATE_LINES = {
    "start tour",
    "how can we help you?",
    "customer center",
    "give feedback",
}

# Regex to find file://... tokens; include windows drive / backslashes or plain forward slashes.
RE_FILE_URL = re.compile(r'file://[^\s]+', re.IGNORECASE)

def shorten_file_url_token(token: str) -> str:
    """
    Given a token like file:///D:/.../pdf/httpssupport.scc.suse.comskbXYZlanguage_en_US.pdf
    return only the basename: httpssupport.scc.suse.comskbXYZlanguage_en_US.pdf
    """
    # Normalize path separators inside the URL (keep the token intact for split)
    # We just need the last part after slash or backslash
    # Strip trailing punctuation common in text contexts
    t = token.rstrip('),.;\'"')
    # split by both / and \, take last non-empty
    parts = re.split(r'[\\/]', t)
    for i in range(len(parts)-1, -1, -1):
        if parts[i]:
            return parts[i]
    return t  # fallback: return original token if nothing found

def shorten_all_file_urls_in_text(text: str) -> Tuple[str, int]:
    """
    Replace all file://... occurrences with just their basename.
    Returns (new_text, replacements_count).
    """
    count = 0
    def repl(m):
        nonlocal count
        count += 1
        return shorten_file_url_token(m.group(0))
    new_text = RE_FILE_URL.sub(repl, text)
    return new_text, count

def remove_boilerplate_lines(text: str) -> Tuple[str, int]:
    """
    Remove lines that exactly match any of the BOILERPLATE_LINES (case-insensitive, after strip()).
    Returns (new_text, removed_count).
    """
    removed = 0
    out_lines: List[str] = []
    for ln in text.splitlines():
        if ln.strip().lower() in BOILERPLATE_LINES:
            removed += 1
            continue
        out_lines.append(ln)
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else ""), removed

def process_txt_file(path: Path, dry_run: bool, backup: bool) -> Tuple[int, int]:
    """
    Process a single .txt file.
    Returns (boilerplate_removed_count, url_shortened_count).
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        raw = path.read_text(encoding="latin-1", errors="ignore")

    new_text, removed_count = remove_boilerplate_lines(raw)
    new_text, url_count = shorten_all_file_urls_in_text(new_text)

    if removed_count == 0 and url_count == 0:
        return (0, 0)

    if dry_run:
        print(f"[DRY] {path}  (boilerplate_removed={removed_count}, url_shortened={url_count})")
        return (removed_count, url_count)

    # Write backup if requested
    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            try:
                bak.write_text(raw, encoding="utf-8", errors="ignore")
            except Exception:
                bak.write_text(raw)  # best-effort

    # Write the updated text
    try:
        path.write_text(new_text, encoding="utf-8")
    except Exception:
        path.write_text(new_text)

    print(f"[OK ] {path}  (boilerplate_removed={removed_count}, url_shortened={url_count})")
    return (removed_count, url_count)

def clean_txt_tree(root_dir: Path, dry_run: bool, backup: bool) -> Tuple[int, int, int]:
    """
    Walk the directory and clean all *.txt files.
    Returns (files_touched, total_boilerplate_removed, total_url_shortened).
    """
    files_touched = 0
    total_boiler = 0
    total_urls = 0

    for p in root_dir.rglob("*.txt"):
        b, u = process_txt_file(p, dry_run, backup)
        if b or u:
            files_touched += 1
            total_boiler += b
            total_urls += u
    return files_touched, total_boiler, total_urls

def clean_csv_urls(csv_path: Path, dry_run: bool, backup: bool) -> Tuple[int, int]:
    """
    Update a CSV in-place to shorten 'file://...' in the 'url' column to basenames only.
    Returns (rows_changed, tokens_shortened).
    """
    import csv as _csv

    with csv_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = _csv.DictReader(f)
        if "url" not in (reader.fieldnames or []):
            print(f"[WARN] CSV has no 'url' column: {csv_path}")
            return (0, 0)
        rows = list(reader)
        fieldnames = reader.fieldnames

    changed = 0
    token_changes = 0
    for r in rows:
        url = r.get("url", "")
        if url.lower().startswith("file://"):
            new_val = shorten_file_url_token(url)
            if new_val != url:
                r["url"] = new_val
                changed += 1
                token_changes += 1

    if changed == 0:
        print(f"[INFO] CSV unchanged: {csv_path}")
        return (0, 0)

    if dry_run:
        print(f"[DRY] CSV would change: {csv_path}  (rows_changed={changed}, tokens_shortened={token_changes})")
        return (changed, token_changes)

    if backup:
        bak = csv_path.with_suffix(csv_path.suffix + ".bak")
        if not bak.exists():
            bak.write_text(csv_path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")

    tmp = csv_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    tmp.replace(csv_path)

    print(f"[OK ] CSV updated: {csv_path}  (rows_changed={changed}, tokens_shortened={token_changes})")
    return (changed, token_changes)

def main():
    ap = argparse.ArgumentParser(description="Batch-clean KB TXT files and optionally a CSV.")
    ap.add_argument("--dir", required=True, help="Root directory containing KB .txt files (e.g., out_txt)")
    ap.add_argument("--csv", help="Optional CSV to clean 'url' column (e.g., suse_kb_last2yrs.csv)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change; do not modify files")
    ap.add_argument("--no-backup", action="store_true", help="Do not write .bak backups")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        print(f"[ERR] Directory not found: {root}")
        raise SystemExit(2)

    backup = not args.no_backup

    print(f"[INFO] Cleaning TXT files under: {root}")
    files_touched, total_boiler, total_urls = clean_txt_tree(root, args.dry_run, backup)
    print(f"[DONE] TXT: files_touched={files_touched}, boilerplate_removed={total_boiler}, urls_shortened={total_urls}")

    if args.csv:
        csv_path = Path(args.csv).resolve()
        if not csv_path.exists():
            print(f"[WARN] CSV not found: {csv_path}")
        else:
            print(f"[INFO] Cleaning CSV: {csv_path}")
            clean_csv_urls(csv_path, args.dry_run, backup)

if __name__ == "__main__":
    main()
