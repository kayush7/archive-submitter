# worker.py
# background stuff so flask doesn't sit there blocked on network calls.
# two main pieces:
#   - scan_domain(): runs one discovery pass and enqueues new urls for submission
#   - queue_worker_loop(): keeps draining the submission queue in the background
#
# everything's saved to sqlite as it goes so if the process dies mid-scan
# or mid-submission, recover_interrupted_work() on next startup just
# re-queues whatever was left half-done instead of losing it.

import queue as pyqueue
import threading
import time
from datetime import datetime, timezone

import db
import archiver
from crawler import Discovery, normalize_url

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30     # not actually used yet, backoff is handled in archiver.py itself
QUEUE_CONCURRENCY = 3          # keep this low-ish, don't want to hammer the archive services

_progress = {}                  # domain_id -> dict of live progress (in-memory, best-effort)
_progress_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def set_progress(domain_id, **kw):
    with _progress_lock:
        _progress.setdefault(domain_id, {}).update(kw)


def get_progress(domain_id):
    with _progress_lock:
        return dict(_progress.get(domain_id, {}))

# not persisted anywhere, just so the dashboard has something to poll while a
# scan is running. fine to lose this on restart, the actual data is all in sqlite

def scan_domain(domain_id, resume_scan_id=None):
    # runs (or resumes) one discovery pass, saves urls, queues the new ones
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if not row:
        return

    with db.tx() as c:
        c.execute("UPDATE domains SET status='discovering' WHERE id=?", (domain_id,))

    if resume_scan_id:
        scan_id = resume_scan_id
        scan_row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        scan_number = scan_row["scan_number"]
        frontier = [r["url"] for r in conn.execute(
            "SELECT url FROM crawl_frontier WHERE scan_id=? AND status='pending'", (scan_id,))]
    else:
        prev = conn.execute(
            "SELECT COALESCE(MAX(scan_number),0) AS n FROM scans WHERE domain_id=?",
            (domain_id,)).fetchone()
        scan_number = prev["n"] + 1
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO scans (domain_id, scan_number, started_at, status) VALUES (?,?,?,?)",
                (domain_id, scan_number, now(), "running"))
            scan_id = cur.lastrowid
        frontier = None

    set_progress(domain_id, phase="discovering", pages_fetched=0, urls_found=0, scan_id=scan_id)

    found_count = 0
    new_count = 0

    def on_progress(**kw):
        if kw.get("event") == "page_fetched":
            set_progress(domain_id, pages_fetched=kw.get("count", 0))

    disc = Discovery(row["base_url"], on_progress=on_progress)

    try:
        for item in disc.run_full_discovery(existing_frontier=frontier):
            url = item["url"]
            norm = normalize_url(url)
            found_count += 1
            set_progress(domain_id, urls_found=found_count)

            with db.tx() as c:
                existing = c.execute(
                    "SELECT id FROM urls WHERE domain_id=? AND normalized_url=?",
                    (domain_id, norm)).fetchone()
                if existing:
                    url_id = existing["id"]
                    c.execute(
                        "UPDATE urls SET http_status=?, is_redirect=?, redirect_target=?, "
                        "accessible=?, last_checked_at=? WHERE id=?",
                        (item.get("http_status"), int(bool(item.get("redirect"))),
                         item.get("redirect_target"), int(bool(item.get("accessible", True))),
                         now(), url_id))
                else:
                    new_count += 1
                    cur = c.execute(
                        "INSERT INTO urls (domain_id, original_url, normalized_url, "
                        "discovery_source, discovery_timestamp, http_status, is_redirect, "
                        "redirect_target, accessible, last_checked_at, first_seen_scan) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (domain_id, url, norm, item.get("source"), now(),
                         item.get("http_status"), int(bool(item.get("redirect"))),
                         item.get("redirect_target"), int(bool(item.get("accessible", True))),
                         now(), scan_number))
                    url_id = cur.lastrowid
                    # newly discovered -> enqueue for archival (Incremental Backup)
                    for service in ("wayback", "archive_today"):
                        c.execute(
                            "INSERT INTO queue (url_id, service, status, created_at) "
                            "VALUES (?,?,?,?)", (url_id, service, "queued", now()))

                c.execute(
                    "INSERT OR IGNORE INTO crawl_frontier (domain_id, scan_id, url, status) "
                    "VALUES (?,?,?,?)", (domain_id, scan_id, url, "done"))
                c.execute(
                    "UPDATE crawl_frontier SET status='done' WHERE scan_id=? AND url=?",
                    (scan_id, url))

        with db.tx() as c:
            c.execute(
                "UPDATE scans SET finished_at=?, urls_found=?, urls_new=?, status='completed' "
                "WHERE id=?", (now(), found_count, new_count, scan_id))
            c.execute(
                "UPDATE domains SET status='queued', last_scan_at=? WHERE id=?",
                (now(), domain_id))
        set_progress(domain_id, phase="discovery_complete", urls_found=found_count,
                     urls_new=new_count)

    except Exception as e:
        # don't let one bad scan take down the whole app
        with db.tx() as c:
            c.execute("UPDATE scans SET status='error', finished_at=? WHERE id=?", (now(), scan_id))
            c.execute("UPDATE domains SET status='error' WHERE id=?", (domain_id,))
        set_progress(domain_id, phase="error", error=str(e))


# ------------------------------------------------------------- submission
# (queue processing stuff starts here)

def _process_queue_item(item_id):
    conn = db.get_conn()
    item = conn.execute("SELECT * FROM queue WHERE id=?", (item_id,)).fetchone()
    if not item or item["status"] != "processing":
        return

    url_row = conn.execute("SELECT * FROM urls WHERE id=?", (item["url_id"],)).fetchone()
    service = item["service"]
    submit_fn = archiver.SERVICES.get(service)

    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO submissions (url_id, service, submitted_at, status, attempt_number, "
            "last_attempted_at) VALUES (?,?,?,?,?,?)",
            (url_row["id"], service, now(), "pending", item["retry_count"] + 1, now()))
        submission_id = cur.lastrowid

    result = submit_fn(url_row["normalized_url"]) if submit_fn else archiver.SubmissionResult(
        False, error_message=f"Unknown service '{service}'")

    with db.tx() as c:
        if result.success:
            c.execute(
                "UPDATE submissions SET completed_at=?, status='success', archive_url=?, "
                "archive_identifier=? WHERE id=?",
                (now(), result.archive_url, result.archive_identifier, submission_id))
            c.execute("UPDATE queue SET status='done', finished_at=? WHERE id=?", (now(), item_id))
        else:
            c.execute(
                "UPDATE submissions SET completed_at=?, status='failed', error_message=? WHERE id=?",
                (now(), result.error_message, submission_id))
            retry_count = item["retry_count"] + 1
            if retry_count <= MAX_RETRIES:
                c.execute(
                    "UPDATE queue SET status='queued', retry_count=?, started_at=NULL WHERE id=?",
                    (retry_count, item_id))
            else:
                c.execute("UPDATE queue SET status='failed', finished_at=? WHERE id=?",
                          (now(), item_id))


def _claim_next_jobs(n):
    # grabs up to n queued jobs and marks them processing so two workers
    # can't both pick up the same job
    conn = db.get_conn()
    claimed = []
    with db.tx() as c:
        rows = c.execute(
            "SELECT id FROM queue WHERE status='queued' ORDER BY priority DESC, id ASC LIMIT ?",
            (n,)).fetchall()
        for r in rows:
            c.execute("UPDATE queue SET status='processing', started_at=? WHERE id=?",
                      (now(), r["id"]))
            claimed.append(r["id"])
    return claimed


def queue_worker_loop(stop_event):
    # keeps pulling batches off the queue and processing them with a
    # few threads at a time (QUEUE_CONCURRENCY) so we're not hammering
    # the archive services
    while not stop_event.is_set():
        job_ids = _claim_next_jobs(QUEUE_CONCURRENCY)
        if not job_ids:
            time.sleep(2)
            continue

        threads = []
        for jid in job_ids:
            t = threading.Thread(target=_process_queue_item, args=(jid,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # update domain-level status once its queue empties
        _sync_domain_statuses()


def _sync_domain_statuses():
    conn = db.get_conn()
    domains = conn.execute("SELECT id FROM domains").fetchall()
    for d in domains:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM queue q JOIN urls u ON q.url_id=u.id "
            "WHERE u.domain_id=? AND q.status IN ('queued','processing')",
            (d["id"],)).fetchone()["n"]
        with db.tx() as c:
            if pending == 0:
                c.execute(
                    "UPDATE domains SET status='done', last_submit_at=? "
                    "WHERE id=? AND status NOT IN ('discovering','error')",
                    (now(), d["id"]))
            else:
                c.execute(
                    "UPDATE domains SET status='submitting' WHERE id=? AND status NOT IN "
                    "('discovering','error')", (d["id"],))


def recover_interrupted_work():
    # run this on startup - anything stuck "processing" from a crash
    # goes back in the queue, scans that got cut off get flagged so
    # they resume instead of restarting from scratch
    with db.tx() as c:
        c.execute("UPDATE queue SET status='queued', started_at=NULL WHERE status='processing'")
        c.execute("UPDATE scans SET status='interrupted' WHERE status='running'")
        c.execute("UPDATE domains SET status='queued' WHERE status='discovering'")


def start_background_worker():
    stop_event = threading.Event()
    t = threading.Thread(target=queue_worker_loop, args=(stop_event,), daemon=True)
    t.start()
    return stop_event
