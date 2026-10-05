# db.py
# sqlite layer for everything - domains, discovered urls, submission
# history, the job queue etc. went with sqlite instead of just keeping
# stuff in memory because otherwise a crash = lose the whole crawl, and
# some of these sites have thousands of pages so it needs to not choke.
#
# quick note on the schema: urls table is unique on (domain_id, normalized_url)
# so re-scanning a site doesn't just keep duplicating rows, and submissions
# is its own table (not a column on urls) so we keep the full history of
# every archive attempt instead of overwriting the last one.

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "archive_submitter.db"
DB_PATH.parent.mkdir(exist_ok=True)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT UNIQUE NOT NULL,      -- e.g. example.com
    base_url        TEXT NOT NULL,             -- e.g. https://example.com
    added_at        TEXT NOT NULL,
    last_scan_at    TEXT,
    last_submit_at  TEXT,
    status          TEXT NOT NULL DEFAULT 'idle'  -- idle|discovering|queued|submitting|done|error
);

CREATE TABLE IF NOT EXISTS urls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id           INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    original_url        TEXT NOT NULL,
    normalized_url       TEXT NOT NULL,
    discovery_source    TEXT,                  -- html_link|sitemap|sitemap_index|robots_txt|feed|pagination|canonical
    discovery_timestamp TEXT NOT NULL,
    http_status         INTEGER,
    is_redirect         INTEGER NOT NULL DEFAULT 0,
    redirect_target     TEXT,
    accessible          INTEGER NOT NULL DEFAULT 1,
    last_checked_at     TEXT,
    first_seen_scan     INTEGER,                -- scan number this url first appeared in
    UNIQUE(domain_id, normalized_url)
);

CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain_id);

CREATE TABLE IF NOT EXISTS submissions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id              INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    service             TEXT NOT NULL,          -- wayback|archive_today
    submitted_at        TEXT,
    completed_at        TEXT,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|success|failed|retrying
    archive_url          TEXT,
    archive_identifier  TEXT,
    error_message       TEXT,
    attempt_number      INTEGER NOT NULL DEFAULT 1,
    last_attempted_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_submissions_url ON submissions(url_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id          INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    service         TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|processing|done|failed
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);

CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id       INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    scan_number     INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    urls_found      INTEGER NOT NULL DEFAULT 0,
    urls_new        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running'  -- running|completed|interrupted|error
);

CREATE TABLE IF NOT EXISTS crawl_frontier (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id       INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    scan_id         INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|done|failed
    UNIQUE(scan_id, url)
);
CREATE INDEX IF NOT EXISTS idx_frontier_scan_status ON crawl_frontier(scan_id, status);
"""


def get_conn():
    # need one connection per thread since flask + the worker thread
    # both touch the db at the same time
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")   # lets reads + writes happen together
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return _local.conn


@contextmanager
def tx():
    # basic tx wrapper, retries a couple times if sqlite complains about
    # being locked (happens sometimes even with WAL under load)
    conn = get_conn()
    attempts = 0
    while True:
        try:
            yield conn
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            conn.rollback()
            attempts += 1
            if "locked" in str(e).lower() and attempts < 5:
                time.sleep(0.2 * attempts)
                continue
            raise


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
