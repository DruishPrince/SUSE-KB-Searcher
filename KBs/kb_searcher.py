#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import argparse
import glob
import html
import os
import re
import sqlite3
import sys
import textwrap
from dataclasses import dataclass
from typing import Iterable, List, Optional

DB_PATH_DEFAULT = "kb_index.db"

try:
    from flask import Flask, request, jsonify, render_template_string, abort, Response
except Exception:
    Flask = None  # Only needed for --serve

# ---------- KB parsing ----------

KB_ID_IN_FILENAME_RE = re.compile(r"(?i)\bKB[_-]?(\d+)")
KB_ID_IN_TEXT_RE = re.compile(r"(?i)\b(?:KB[_-]?)?(\d{6,})\b")
DOC_ID_LINE_RE = re.compile(r"(?mi)^\s*Document ID:\s*(.+?)\s*$")
TITLE_LINE_RE = re.compile(r"(?m)^(?!Document ID:|URL:)(.+?)\s*$")  # Match non-empty line that's not "Document ID:" or "URL:"
URL_LINE_RE = re.compile(r"(?mi)^\s*URL:\s*(.+?)\s*$")

def norm_kb_id(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")

def extract_title_from_text(text: str) -> str:
    """
    Extract title from KB article text.

    New format:
        Document ID: 123456
        Title Here
        URL: https://...

        Content...

    Old format:
        First line is title
    """
    lines = [line.strip() for line in text.split('\n') if line.strip()]

    # Try to find title in new format (line after Document ID, before URL)
    for i, line in enumerate(lines):
        if line.startswith('Document ID:'):
            # Title should be the next non-empty line that's not "URL:"
            for j in range(i + 1, min(i + 5, len(lines))):  # Check next few lines
                if lines[j] and not lines[j].startswith('URL:'):
                    return lines[j]

    # Fallback: first non-empty line that's not "Document ID:" or "URL:"
    for line in lines:
        if line and not line.startswith('Document ID:') and not line.startswith('URL:'):
            return line

    return "Untitled"

def kb_id_from_filename(path: str) -> str:
    """
    Extract KB ID from filename.
    For KB_*.txt files, extract whatever comes after KB_ (could be numeric, alphanumeric, etc.)
    If no clear pattern, use the base filename without extension.
    Always returns a string (never None).
    """
    basename = os.path.basename(path)

    # Try numeric pattern first (KB_123456...)
    m = KB_ID_IN_FILENAME_RE.search(basename)
    if m:
        normalized = norm_kb_id(m.group(1))
        if normalized:  # Only return if we got something after normalization
            return normalized

    # Fallback: for files starting with KB_, extract everything between KB_ and first _ or .txt
    # This handles KB_A, KB_1, KB_ABC123, etc.
    if basename.startswith('KB_'):
        # Remove KB_ prefix
        remainder = basename[3:]
        # Take everything up to next underscore or .txt
        kb_part = remainder.split('_')[0].split('.txt')[0]
        if kb_part:
            return kb_part

    # Last resort: use filename without extension as the ID
    result = basename.replace('.txt', '').replace('KB_', '')
    return result if result else 'unknown'

def kb_id_from_text(text: str) -> Optional[str]:
    m = DOC_ID_LINE_RE.search(text)
    if m:
        return norm_kb_id(m.group(1))
    m2 = KB_ID_IN_TEXT_RE.search(text)
    return norm_kb_id(m2.group(1)) if m2 else None

@dataclass
class Article:
    kb_id: Optional[str]
    title: str
    source_file: str
    content: str

def chunk_combined_file(path: str) -> Iterable[Article]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    parts = []
    last = 0
    for match in DOC_ID_LINE_RE.finditer(text):
        end = match.end()
        seg = text[last:end]
        if seg.strip():
            parts.append(seg)
        last = end
    tail = text[last:]
    if tail.strip():
        parts.append(tail)

    if not parts:
        kb = kb_id_from_text(text)
        title = extract_title_from_text(text)
        yield Article(kb_id=kb, title=title, source_file=path, content=text.strip())
        return

    for seg in parts:
        kb = kb_id_from_text(seg)
        title = extract_title_from_text(seg)
        yield Article(kb_id=kb, title=title, source_file=path, content=seg.strip())

# ---------- DB & search ----------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    kb_id TEXT,
    title TEXT NOT NULL,
    source_file TEXT NOT NULL,
    content TEXT NOT NULL,
    is_individual INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_kb ON articles(kb_id);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, content, kb_id UNINDEXED, source_file UNINDEXED, is_individual UNINDEXED,
    content='articles', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con

def rebuild_index(db_path: str, roots: List[str]) -> None:
    con = connect(db_path)
    with con:
        # Use executescript so multiple statements work on any line endings
        con.executescript(SCHEMA)

        # Clear existing rows
        con.execute("DELETE FROM articles")
        con.execute("DELETE FROM articles_fts")

        # 1) Index individual KB files first (preferred)
        kb_files = []
        for root in roots:
            kb_files.extend(glob.glob(os.path.join(root, "KB_*.txt")))
        for path in sorted(set(kb_files)):
            try:
                kb = kb_id_from_filename(path)
                # Now kb_id_from_filename always returns something, even if it's the filename
                # No need to skip based on KB ID
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read().strip()
                if not text:
                    continue

                # Extract title using new logic
                title = extract_title_from_text(text)

                row = con.execute(
                    "INSERT INTO articles(kb_id, title, source_file, content, is_individual) VALUES(?,?,?,?,1)",
                    (kb, title, path, text),
                )
                con.execute(
                    "INSERT INTO articles_fts(rowid, title, content, kb_id, source_file, is_individual) VALUES(?,?,?,?,?,?)",
                    (row.lastrowid, title, text, kb, path, 1),
                )
            except Exception as e:
                print(f"[WARN] individual {path}: {e}", file=sys.stderr)

        # 2) Index combined_* files, skipping any KB that already exists as individual
        have_kb = {r["kb_id"] for r in con.execute(
            "SELECT DISTINCT kb_id FROM articles WHERE is_individual=1"
        ) if r["kb_id"]}
        combined_files = []
        for root in roots:
            combined_files.extend(glob.glob(os.path.join(root, "combined_*.txt")))
        for path in sorted(set(combined_files)):
            try:
                for art in chunk_combined_file(path):
                    if art.kb_id and art.kb_id in have_kb:
                        continue
                    row = con.execute(
                        "INSERT INTO articles(kb_id, title, source_file, content, is_individual) VALUES(?,?,?,?,0)",
                        (art.kb_id, art.title, path, art.content),
                    )
                    con.execute(
                        "INSERT INTO articles_fts(rowid, title, content, kb_id, source_file, is_individual) VALUES(?,?,?,?,?,?)",
                        (row.lastrowid, art.title, art.content, art.kb_id or "", path, 0),
                    )
            except Exception as e:
                print(f"[WARN] combined {path}: {e}", file=sys.stderr)
    con.close()

def search(con: sqlite3.Connection, q: str, file_filter: Optional[str], limit: int = 50):
    """Search articles using FTS5."""
    # Sanitize query for FTS5 - quote phrases and escape special characters
    # FTS5 special chars: " ( ) AND OR NOT

    # If query already has quotes, use as-is (phrase search)
    if '"' in q:
        fts_query = q
    else:
        # For simple queries, wrap each word in quotes for literal matching
        # This prevents FTS5 syntax errors with special characters
        words = q.split()
        fts_query = ' '.join(f'"{word}"' for word in words)

    where = "articles_fts MATCH ?"
    params = [fts_query]
    if file_filter:
        where += " AND source_file LIKE ?"
        params.append(f"%{file_filter}%")

    sql = f"""
    SELECT a.id, a.kb_id, a.title, a.source_file, a.is_individual,
           snippet(articles_fts, 1, '<mark>', '</mark>', '…', 8) AS snippet
    FROM articles a
    JOIN articles_fts ON a.id = articles_fts.rowid
    WHERE {where}
    ORDER BY bm25(articles_fts), a.is_individual DESC
    LIMIT ?
    """
    params.append(limit)

    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # If FTS query fails, try a simpler LIKE search
        print(f"[WARN] FTS search failed, falling back to LIKE: {e}", file=sys.stderr)

        # Fallback to simple LIKE search
        like_where = "(a.title LIKE ? OR a.content LIKE ?)"
        like_params = [f"%{q}%", f"%{q}%"]
        if file_filter:
            like_where += " AND a.source_file LIKE ?"
            like_params.append(f"%{file_filter}%")

        sql = f"""
        SELECT a.id, a.kb_id, a.title, a.source_file, a.is_individual,
               substr(a.content, 1, 200) AS snippet
        FROM articles a
        WHERE {like_where}
        ORDER BY a.is_individual DESC
        LIMIT ?
        """
        like_params.append(limit)
        return con.execute(sql, like_params).fetchall()

def search_regex(con: sqlite3.Connection, pattern: str, file_filter: Optional[str], limit: int = 50, case_sensitive: bool = False, literal: bool = False):
    r"""
    Search articles using Python regex patterns.
    Supports full regex syntax: .* (any chars), \d+ (digits), | (OR), etc.
    If literal=True, escapes all special regex characters for exact matching.
    """
    import re

    # If literal mode, escape all regex special characters
    if literal:
        pattern = re.escape(pattern)

    try:
        # Compile the regex pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)
    except re.error as e:
        # Invalid regex pattern
        raise ValueError(f"Invalid regex pattern: {e}")

    # Get all articles (or filtered by file)
    if file_filter:
        rows = con.execute(
            "SELECT id, kb_id, title, source_file, content, is_individual FROM articles WHERE source_file LIKE ?",
            (f"%{file_filter}%",)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, kb_id, title, source_file, content, is_individual FROM articles"
        ).fetchall()

    results = []
    for row in rows:
        # Search in both title and content
        title_match = regex.search(row["title"])
        content_match = regex.search(row["content"])

        if title_match or content_match:
            # Create a snippet showing the match context
            if content_match:
                # Get context around the match
                start = max(0, content_match.start() - 100)
                end = min(len(row["content"]), content_match.end() + 100)
                snippet = row["content"][start:end]
                # Highlight the match
                snippet = regex.sub(r'<mark>\g<0></mark>', snippet)
                if start > 0:
                    snippet = "…" + snippet
                if end < len(row["content"]):
                    snippet = snippet + "…"
            else:
                # Match was in title, show beginning of content
                snippet = row["content"][:200] + "…" if len(row["content"]) > 200 else row["content"]

            results.append({
                "id": row["id"],
                "kb_id": row["kb_id"],
                "title": row["title"],
                "source_file": row["source_file"],
                "is_individual": row["is_individual"],
                "snippet": snippet
            })

            if len(results) >= limit:
                break

    return results

def get_article_by_kb(con: sqlite3.Connection, kb_id: str):
    return con.execute(
        "SELECT * FROM articles WHERE kb_id=? ORDER BY is_individual DESC LIMIT 1", (kb_id,)
    ).fetchone()

def get_article_by_row(con: sqlite3.Connection, rowid: int):
    return con.execute("SELECT * FROM articles WHERE id=?", (rowid,)).fetchone()

# ---------- Web App (SPA) ----------

PAGE_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>KB Web Search</title>
<style>
:root{
  --border:#e5e7eb; --muted:#6b7280; --bg:#f8fafc; --radius:16px;
}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;background:#ffffff;color:#0f172a;overflow:hidden}
.app{display:grid;grid-template-columns:420px 1fr;height:100vh;overflow:hidden}
.left{border-right:1px solid var(--border);padding:16px;display:flex;flex-direction:column;gap:12px;overflow:hidden;height:100vh}
.right{padding:16px;overflow-y:auto;height:100vh}
h1{font-size:18px;margin:0 0 4px 0}
.searchbar{display:flex;gap:8px}
input[type=text]{flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:12px;font-size:14px}
button{padding:10px 14px;border-radius:999px;border:1px solid var(--border);background:#fff;cursor:pointer}
.small{font-size:12px;color:var(--muted)}
.results{display:flex;flex-direction:column;gap:10px;overflow-y:auto;min-height:0;flex:1}
.card{border:1px solid var(--border);border-radius:var(--radius);padding:12px;background:#fff;cursor:pointer}
.card h3{margin:0 0 4px 0;font-size:15px}
.meta{font-size:12px;color:var(--muted)}
.snippet mark{background:#fff3a3}
.badges{display:inline-flex;gap:6px;margin-left:6px}
.badge{font-size:11px;border:1px solid var(--border);padding:2px 8px;border-radius:999px;background:#f9fafb}
.viewer{border:1px solid var(--border);border-radius:var(--radius);padding:16px;background:#fff;min-height:120px}
.viewer h2{margin:0 0 6px 0;font-size:18px}
.viewer pre{white-space:pre-wrap;line-height:1.45;font-size:14px}
.toolbar{display:flex;gap:8px;align-items:center;justify-content:space-between}
kbd{background:#f1f5f9;border:1px solid var(--border);border-bottom-width:2px;border-radius:6px;padding:1px 6px;font-size:12px}
.headerline{display:flex;align-items:center;justify-content:space-between}
.row{display:flex;gap:8px}
@media (max-width: 900px){
  .app{grid-template-columns:1fr;grid-template-rows:auto 1fr;height:100vh}
  .left{border-right:0;border-bottom:1px solid var(--border);height:auto;max-height:40vh;overflow-y:auto}
  .right{height:auto;max-height:60vh}
}
</style>
</head>
<body>
<div class="app">
  <div class="left">
    <div class="headerline">
      <h1>KB Web Search</h1>
      <div class="row">
        <button id="reindexBtn" title="Rebuild index from disk">Reindex</button>
      </div>
    </div>
    <div class="searchbar">
      <input id="q" type="text" placeholder="Search KB… (quotes for exact phrase)" />
      <input id="fileFilter" type="text" placeholder="Filter by file name…" />
      <button id="go">Search</button>
    </div>
    <div class="small" style="display:flex;gap:12px;align-items:center;margin-top:4px;">
      <label style="display:flex;gap:4px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="regexMode" />
        <span>Regex mode</span>
      </label>
      <label style="display:flex;gap:4px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="caseSensitive" />
        <span>Case-sensitive</span>
      </label>
      <label style="display:flex;gap:4px;align-items:center;cursor:pointer;">
        <input type="checkbox" id="literalSearch" />
        <span>Literal search (ignore wildcards)</span>
      </label>
    </div>
    <div class="small">
      <span id="searchTips">Tips: <kbd>Enter</kbd> to search • <kbd>↑/↓</kbd> navigate • <kbd>Enter</kbd> open</span>
    </div>
    <div id="results" class="results"></div>
  </div>
  <div class="right">
    <div class="viewer" id="viewer">
      <div class="small">Type a query and press Search. Click a result to view the full article here.</div>
    </div>
  </div>
</div>
<script>
const qEl = document.getElementById('q');
const fEl = document.getElementById('fileFilter');
const goEl = document.getElementById('go');
const resultsEl = document.getElementById('results');
const viewer = document.getElementById('viewer');
const reindexBtn = document.getElementById('reindexBtn');
const regexModeEl = document.getElementById('regexMode');
const caseSensitiveEl = document.getElementById('caseSensitive');
const literalSearchEl = document.getElementById('literalSearch');
const searchTipsEl = document.getElementById('searchTips');

let selectedIndex = -1;
let currentResults = [];

// Update search tips based on mode
function updateSearchTips(){
  if(literalSearchEl.checked){
    searchTipsEl.innerHTML = 'Literal: Special chars like <kbd>.</kbd> <kbd>*</kbd> <kbd>|</kbd> are treated as plain text';
    qEl.placeholder = 'Literal search (192.168.1.1, *, etc.)';
  } else if(regexModeEl.checked){
    searchTipsEl.innerHTML = 'Regex: <kbd>.*</kbd> any chars • <kbd>\\d+</kbd> digits • <kbd>cat|dog</kbd> OR • <kbd>\\.</kbd> literal dot';
    qEl.placeholder = 'Regex pattern (e.g., boot.*error or grub|lilo)';
  } else {
    searchTipsEl.innerHTML = 'Tips: <kbd>Enter</kbd> to search • <kbd>↑/↓</kbd> navigate • <kbd>Enter</kbd> open';
    qEl.placeholder = 'Search KB… (quotes for exact phrase)';
  }
}

regexModeEl.addEventListener('change', updateSearchTips);
literalSearchEl.addEventListener('change', updateSearchTips);
updateSearchTips();

function renderResults(items){
  resultsEl.innerHTML = '';
  currentResults = items || [];
  selectedIndex = currentResults.length ? 0 : -1;

  currentResults.forEach((r,i)=>{
    const div = document.createElement('div');
    div.className = 'card';
    div.tabIndex = 0;
    div.dataset.index = i;
    div.innerHTML = `
      <h3>${escapeHtml(r.title)}
        <span class="badges">
          ${r.kb_id ? `<span class="badge">KB ${r.kb_id}</span>`:''}
          <span class="badge">${r.is_individual ? 'individual' : 'combined'}</span>
        </span>
      </h3>
      <div class="meta">${escapeHtml(r.source_file)}</div>
      <div class="snippet">${r.snippet_html}</div>
    `;
    div.addEventListener('click', ()=>openResult(i));
    resultsEl.appendChild(div);
  });
  highlight();
}

function setStatus(msg){
  viewer.innerHTML = `<div class="small">${msg}</div>`;
}

function openResult(i){
  if(i<0 || i>=currentResults.length) return;
  const r = currentResults[i];
  const url = r.kb_id ? `/api/article/kb/${r.kb_id}` : `/api/article/row/${r.id}`;
  fetch(url).then(res=>{
    if(!res.ok) throw new Error('Not found');
    return res.json();
  }).then(data=>{
    viewer.innerHTML = `
      <div class="toolbar">
        <div>
          <div class="small">${escapeHtml(data.source_file)}${data.kb_id ? ' • KB '+escapeHtml(data.kb_id):''}</div>
          <h2>${escapeHtml(data.title)}</h2>
        </div>
        <div>
          <a href="/api/article/raw/${data.kb_id ? data.kb_id : ('row-'+data.id)}" target="_blank">
            <button>Open Raw</button>
          </a>
        </div>
      </div>
      <pre>${escapeHtml(data.content)}</pre>
    `;
  }).catch(err=>{
    setStatus('Could not load article.');
  });
}

function doSearch(){
  const q = qEl.value.trim();
  const ff = fEl.value.trim();
  const mode = regexModeEl.checked ? 'regex' : 'fts5';
  const caseSensitive = caseSensitiveEl.checked ? 'true' : 'false';
  const literal = literalSearchEl.checked ? 'true' : 'false';

  if(!q){ setStatus('Enter a search query.'); return; }
  setStatus('Searching…');

  const url = `/api/search?q=${encodeURIComponent(q)}&file=${encodeURIComponent(ff)}&mode=${mode}&case=${caseSensitive}&literal=${literal}`;

  fetch(url)
    .then(r=>r.json())
    .then(data=>{
      if(data.error){
        setStatus(`Error: ${data.error}`);
        renderResults([]);
        return;
      }
      renderResults(data.results);
      if(data.results && data.results.length){ openResult(0); }
      else { setStatus('No results.'); }
    }).catch(()=>{
      setStatus('Error searching.');
    });
}

function escapeHtml(s){
  return (s||'').replace(/[&<>"']/g, m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]));
}

// keyboard
qEl.addEventListener('keydown', e=>{ if(e.key==='Enter'){ doSearch(); }});
fEl.addEventListener('keydown', e=>{ if(e.key==='Enter'){ doSearch(); }});
goEl.addEventListener('click', doSearch);

document.addEventListener('keydown', e=>{
  if(!currentResults.length) return;
  if(e.key==='ArrowDown'){ selectedIndex=Math.min(selectedIndex+1,currentResults.length-1); highlight(); e.preventDefault(); }
  if(e.key==='ArrowUp'){ selectedIndex=Math.max(selectedIndex-1,0); highlight(); e.preventDefault(); }
  if(e.key==='Enter' && document.activeElement.tagName!=='INPUT'){ openResult(selectedIndex); e.preventDefault(); }
});

function highlight(){
  Array.from(resultsEl.children).forEach((el,idx)=>{
    el.style.outline = idx===selectedIndex ? '2px solid #3b82f6' : 'none';
  });
}

// reindex
reindexBtn.addEventListener('click', ()=>{
  reindexBtn.disabled = true;
  reindexBtn.textContent = 'Reindexing…';
  fetch('/api/reindex', {method:'POST'}).then(r=>r.json()).then(j=>{
    reindexBtn.textContent = 'Reindex';
    reindexBtn.disabled = false;
    if(j.ok){ setStatus('Reindex complete. You can search now.'); }
    else { setStatus('Reindex failed. Check server logs.'); }
  }).catch(()=>{
    reindexBtn.textContent = 'Reindex';
    reindexBtn.disabled = false;
    setStatus('Reindex failed. Check server logs.');
  });
});
</script>
</body>
</html>
"""

def create_app(db_path: str, roots: List[str]):
    app = Flask(__name__)

    def get_con():
        return connect(db_path)

    @app.get("/")
    def home():
        return render_template_string(PAGE_HTML)

    # Optional tiny favicon to avoid 404 noise
    @app.get("/favicon.ico")
    def favicon():
        return Response(b"", mimetype="image/x-icon")

    @app.get("/api/search")
    def api_search():
        q = request.args.get("q","").strip()
        file_f = request.args.get("file","").strip() or None
        mode = request.args.get("mode","fts5").strip().lower()  # "fts5" or "regex"
        case_sensitive = request.args.get("case","false").strip().lower() == "true"
        literal = request.args.get("literal","false").strip().lower() == "true"

        if not q:
            return jsonify(results=[])

        try:
            with get_con() as con:
                if mode == "regex":
                    # Use regex search
                    rows = search_regex(con, q, file_f, limit=100, case_sensitive=case_sensitive, literal=literal)
                    # search_regex returns dicts, not Row objects
                    results = []
                    for r in rows:
                        results.append({
                            "id": r["id"],
                            "kb_id": r["kb_id"],
                            "title": r["title"],
                            "source_file": r["source_file"],
                            "is_individual": int(r["is_individual"]) == 1,
                            "snippet_html": r["snippet"],
                        })
                else:
                    # Use FTS5 search (default)
                    rows = search(con, q, file_f, limit=100)
                    results = []
                    for r in rows:
                        results.append({
                            "id": r["id"],
                            "kb_id": r["kb_id"],
                            "title": r["title"],
                            "source_file": r["source_file"],
                            "is_individual": int(r["is_individual"]) == 1,
                            "snippet_html": r["snippet"] if "snippet" in r.keys() else "",
                        })
            return jsonify(results=results)
        except ValueError as e:
            # Invalid regex pattern
            print(f"[ERROR] Invalid regex: {e}", file=sys.stderr)
            return jsonify(error=str(e), results=[]), 400
        except Exception as e:
            print(f"[ERROR] Search failed: {e}", file=sys.stderr)
            return jsonify(error=str(e), results=[]), 500

    @app.get("/api/article/kb/<kb_id>")
    def api_article_kb(kb_id):
        # Don't normalize - we now support alphanumeric KB IDs
        with get_con() as con:
            row = get_article_by_kb(con, kb_id)
            if not row:
                abort(404)
        return jsonify(
            id=row["id"], kb_id=row["kb_id"], title=row["title"],
            source_file=row["source_file"], content=row["content"]
        )

    @app.get("/api/article/row/<int:rowid>")
    def api_article_row(rowid: int):
        with get_con() as con:
            row = get_article_by_row(con, rowid)
            if not row:
                abort(404)
        return jsonify(
            id=row["id"], kb_id=row["kb_id"], title=row["title"],
            source_file=row["source_file"], content=row["content"]
        )

    @app.get("/api/article/raw/<id_or_row>")
    def api_article_raw(id_or_row: str):
        with get_con() as con:
            if id_or_row.startswith("row-"):
                rowid = int(id_or_row.split("-",1)[1])
                row = get_article_by_row(con, rowid)
            else:
                # Don't normalize - we now support alphanumeric KB IDs
                row = get_article_by_kb(con, id_or_row)
            if not row:
                abort(404)
        return app.response_class(row["content"], mimetype="text/plain")

    @app.post("/api/reindex")
    def api_reindex():
        try:
            rebuild_index(db_path, roots)
            return jsonify(ok=True)
        except Exception as e:
            print("[ERROR] reindex:", e, file=sys.stderr)
            return jsonify(ok=False), 500

    return app

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="KB Web Search (combined + individual files).")
    ap.add_argument("--db", default=DB_PATH_DEFAULT, help="SQLite index path")
    ap.add_argument("--index", action="store_true", help="(Re)build the index now")
    ap.add_argument("--root", action="append", help="Folder(s) to scan; default: current dir (can specify multiple times)")
    ap.add_argument("--serve", action="store_true", help="Start the web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)

    args = ap.parse_args()

    # If no --root specified, use current directory
    if args.root is None:
        args.root = ["."]

    # Show what we're scanning
    print(f"[INFO] Scanning directories: {', '.join(args.root)}")

    if args.index:
        # Show what files we're finding
        kb_count = 0
        combined_count = 0
        for root in args.root:
            kb_files = glob.glob(os.path.join(root, "KB_*.txt"))
            combined_files = glob.glob(os.path.join(root, "combined_*.txt"))
            kb_count += len(kb_files)
            combined_count += len(combined_files)
            print(f"[INFO] Found {len(kb_files)} KB_*.txt files in {root}")
            print(f"[INFO] Found {len(combined_files)} combined_*.txt files in {root}")

        if kb_count == 0 and combined_count == 0:
            print("\n[WARNING] No KB articles found!", file=sys.stderr)
            print(f"[INFO] Looking for files like: KB_*.txt or combined_*.txt", file=sys.stderr)
            print(f"[INFO] Current directory: {os.path.abspath('.')}", file=sys.stderr)
            print(f"[INFO] Specify a different directory with: --root /path/to/articles", file=sys.stderr)
        else:
            print(f"\n[INFO] Total: {kb_count} individual + {combined_count} combined files")

        rebuild_index(args.db, args.root)
        print(f"[INFO] Index rebuilt: {args.db}")

    if args.serve:
        if Flask is None:
            print("Flask is not installed. Run: pip install flask", file=sys.stderr)
            sys.exit(1)
        app = create_app(args.db, args.root)
        print(f"[INFO] Starting web server on http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)

    if not (args.index or args.serve):
        print("Nothing to do. Use --index and/or --serve. Example: python kb_searcher.py --index --serve")

if __name__ == "__main__":
    main()
