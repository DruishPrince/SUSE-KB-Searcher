This is a way to quickly search a large amount of the SUSE KB articles as they have made the normal website horribly slow.

I will work on getting the latest KB articles added as I am about a year behind currently.

To get this running, you will need python 3.9+, then pip install flask.

From your KB folder with the script and articles extracted -
  python .\kb_searcher.py --index
  python .\kb_searcher.py --serve
  then open http://127.0.0.1:8000


Additional instructions -

KB Web Searcher (combined dumps + individual KB files)

• Web UI: left panel shows search/results, right panel shows the full article.
• Instant search via /api/search; click a result to load /api/article/<...>.
• Prefers individual KB files (KB_*.txt) when present.
• Indexes combined_*.txt as fallback.
• Reindex button in UI (POST /api/reindex).
• SQLite FTS5 (BM25); no external deps besides Flask (only needed for --serve).

Usage (Windows PowerShell example):
  cd D:\suse_kb_texts_Processed
  python .\kb_searcher.py --index
  python .\kb_searcher.py --serve
  # open http://127.0.0.1:8000

Multiple roots:
  python .\kb_searcher.py --index --root "D:\\suse_kb_texts_Processed" --root "D:\\more_kb_dumps"

Notes:
• If you previously ran a buggy version, you may delete kb_index.db and reindex.
• Flask is only required when using --serve (pip install flask).
