This is a way to quickly search a large amount of the SUSE KB articles as they have made the normal website horribly slow.

I will work on getting the latest KB articles added as I am about a year behind currently.

To get this running, you will need python 3.9+, then pip install flask.

From your KB folder with the script and articles extracted -
  python .\kb_searcher.py --index
  python .\kb_searcher.py --serve
  then open http://127.0.0.1:8000
