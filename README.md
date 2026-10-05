# Website Archive Submitter & Automated Backup Repository

Python/Flask app that crawls a website, finds its public URLs, and submits
them to web archiving services (Wayback Machine + Archive.today), keeping
a record of what got archived and when.

## How it's put together

- **db.py** - sqlite schema + a small transaction helper. Used sqlite
  instead of just holding everything in memory because a site can have
  thousands of pages and the process shouldn't lose everything if it
  crashes.
- **crawler.py** - the actual url discovery. Crawls internal links (BFS),
  parses sitemap.xml / sitemap indexes, reads robots.txt for `Sitemap:`
  lines, grabs canonical urls and rss/atom feeds, follows pagination
  links. Stays on the same domain unless told otherwise.
- **archiver.py** - talks to the archive services. Wayback Machine has a
  simple public "save page now" endpoint so that one's straightforward.
  Archive.today doesn't really have an API and throws captchas at
  anything automated - see the note in that file, we don't try to get
  around it, it just fails with an explanation when that happens.
- **worker.py** - background thread(s) that actually do the crawling and
  submitting so the web server doesn't just hang while that's happening.
  Also handles retries and picking back up after a crash.
- **app.py** - the flask routes + dashboard.

## Running it

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

then open http://localhost:5000

## Basic flow

1. Add a domain (just type the hostname, e.g. `example.com`)
2. Hit "▶ Discover" - runs in the background, dashboard polls every few
   seconds so you can watch the count go up
3. once it's done finding urls, they automatically get queued for
   submission to both archive services
4. "↻ Re-archive all" forces a new submission round if you want an
   updated snapshot of everything
5. "📄 Browse URLs" opens every discovered url for that domain, filterable
   by discovery source and accessibility, with CSV/JSON export buttons
6. click any url (in the browse modal or the search results) to see its
   full submission history - every attempt, every service, every error
7. "📈 Scan history" shows every past scan for a domain and how many new
   urls turned up each time
8. the search bar at the top looks up any url/domain across all domains
   and shows its latest archive status + link
9. the 🗑 icon on a domain card removes it and everything tied to it

If you kill the process mid-scan or mid-submission, just start it again -
anything left half-done gets picked back up (see `recover_interrupted_work`
in worker.py). Re-scanning a domain you've already added only inserts
genuinely new urls, it doesn't duplicate what's already there.

## Stuff that's worth mentioning in the demo/writeup

- Archive.today submissions will fail sometimes because of the captcha
  thing mentioned above - that's expected, not a bug.
- JS-rendered pages aren't handled yet (requests just sees the raw html).
  Would need to swap in Playwright for that, which is one of the bonus
  items - the crawler is set up so a `discover_from_rendered_page()` type
  method could slot in fairly easily.
- Right now the worker is just threads in the same process. Fine for a
  single machine, but if this needed to actually scale to 50+ domains at
  once you'd want real distributed workers (celery/redis or similar) -
  the queue table is already structured so that wouldn't require a big
  rewrite, just swapping how jobs get claimed.

## Not done / could still add

- change-detection *comparison view* between two specific scans (the numbers are already tracked per scan and visible in the scan history modal, just no side-by-side diff UI yet)
- priority queue has a working "prioritize" button per url now, but there's no bulk "mark this whole domain as high priority" option
- distributed workers / scaling past a single process, as mentioned above
