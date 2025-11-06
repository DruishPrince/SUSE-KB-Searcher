#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Rename KB .txt files to: KB_<ID>_<bottom-text-slug>.txt

- KB_<ID> is taken from:
    1) first line like "KB_000012345 Title...", or
    2) "Document ID: 000012345" anywhere in the file  -> KB_000012345
- <bottom-text-slug> is derived from the last non-empty line:
    * If it looks like a URL or file:// path, use its basename and strip ".pdf"
    * Otherwise use the line as-is
- Sanitizes names and avoids collisions by appending _v2, _v3, ...
- Optional: dry-run and backups

Usage:
  # Preview only
  python rename_kb_txts.py --dir out_txt --dry-run

  # Rename with .bak backups
  python rename_kb_txts.py --dir out_txt

  # Rename without backups
  python rename_kb_txts.py --dir out_txt --no-backup
"""

import argparse
import re
from pathlib import Path
from typing import Optional

RE_KB_HEADER = re.compile(r'^\s*(KB[_-]?\d{6,})\b', re.I)
RE_DOC_ID = re.compile(r'\bDocument\s*ID\s*:\s*([0-9]{6,})', re.I)
RE_FILE_URL = re.compile(r'file://[^\s]+', re.I)
RE_HTTP_URL = re.compile(r'https?://[^\s]+', re.I)

SAFE_CHARS = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

def slugify(s: str, maxlen: int = 100) -> str:
    s = (s or "").strip()
    # replace illegal chars with underscore, compress repeats
    s = "".join(ch if ch in SAFE_CHARS else "_" for ch in s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:maxlen] or "KB_Article"

def kb_id_from_text(text: str) -> Optional[str]:
    # Prefer explicit KB_ on first non-empty line
    first = ""
    for ln in text.splitlines():
        if ln.strip():
            first = ln.strip()
            break
    m = RE_KB_HEADER.match(first)
    if m:
        return m.group(1).replace("-", "_").upper()
    # Else Document ID -> KB_########
    m = RE_DOC_ID.search(text)
    if m:
        return f"KB_{m.group(1)}"
    return None

def last_nonempty_line(text: str) -> str:
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s:
            return s
    return ""

def basename_from_pathish(s: str) -> str:
    # split on both / and \
    parts = re.split(r"[\\/]", s.strip())
    last = ""
    for i in range(len(parts)-1, -1, -1):
        if parts[i]:
            last = parts[i]
            break
    # strip query/fragment and enclosing punctuation
    last = re.split(r"[?#]", last, 1)[0].rstrip('),.;\'"')
    # drop .pdf suffix if present
    if last.lower().endswith(".pdf"):
        last = last[:-4]
    return last or s

def slug_from_bottom_line(s: str) -> str:
    # If it's file://..., keep basename
    m = RE_FILE_URL.search(s)
    if m:
        return slugify(basename_from_pathish(m.group(0)))
    # If it's http(s)://..., keep basename of path
    m = RE_HTTP_URL.search(s)
    if m:
        return slugify(basename_from_pathish(m.group(0)))
    # If it looks like a path-like token (contains slash or backslash), trim to basename
    if "/" in s or "\\" in s:
        return slugify(basename_from_pathish(s))
    # Otherwise use the line
    return slugify(s)

def unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    i = 2
    while True:
        cand = base.with_name(f"{stem}_v{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1

def rename_one(path: Path, dry: bool, backup: bool) -> Optional[Path]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        raw = path.read_text(encoding="latin-1", errors="ignore")

    kb = kb_id_from_text(raw)
    if not kb:
        print(f"[SKIP] {path.name}: KB ID not found")
        return None

    bottom = last_nonempty_line(raw)
    if not bottom:
        print(f"[SKIP] {path.name}: no bottom line")
        return None

    slug = slug_from_bottom_line(bottom)
    new_name = f"{kb}_{slug}.txt"
    new_path = path.with_name(new_name)

    if new_path == path:
        print(f"[KEEP] {path.name}")
        return None

    # Collision avoidance
    new_path = unique_path(new_path)

    if dry:
        print(f"[DRY ] {path.name}  ->  {new_path.name}")
        return new_path

    # backup
    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            try:
                bak.write_text(raw, encoding="utf-8", errors="ignore")
            except Exception:
                bak.write_text(raw)

    path.rename(new_path)
    print(f"[RENM] {path.name}  ->  {new_path.name}")
    return new_path

def main():
    ap = argparse.ArgumentParser(description="Rename KB .txt files to KB_<ID>_<bottom-text-slug>.txt")
    ap.add_argument("--dir", required=True, help="Folder containing KB .txt files (e.g., out_txt)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be renamed; do not rename")
    ap.add_argument("--no-backup", action="store_true", help="Do not create .bak backups")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        print(f"[ERR ] directory not found: {root}")
        raise SystemExit(2)

    backup = not args.no_backup
    total = 0
    renamed = 0
    skipped = 0

    for p in sorted(root.glob("*.txt")):
        total += 1
        out = rename_one(p, args.dry_run, backup)
        if out is None:
            skipped += 1
        else:
            renamed += 1

    print(f"[DONE] total={total} renamed={renamed} skipped={skipped} dry_run={args.dry_run}")

if __name__ == "__main__":
    main()
