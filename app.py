# app.py
# main flask app - just the api routes + serving the dashboard page.
# run with `python app.py`, then go to localhost:5000

import threading
import urllib.parse as urlparse
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import db
import worker

app = Flask(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ setup

@app.before_request
def _ensure_db():
    db.init_db()


# -------------------------------------------------------------- dashboard

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
def api_dashboard():
    conn = db.get_conn()
    domains = conn.execute("SELECT * FROM domains ORDER BY added_at DESC").fetchall()
    out = []
    for d in domains:
        total_urls = conn.execute(
            "SELECT COUNT(*) AS n FROM urls WHERE domain_id=?", (d["id"],)).fetchone()["n"]
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM queue q JOIN urls u ON q.url_id=u.id "
            "WHERE u.domain_id=? AND q.status IN ('queued','processing')",
            (d["id"],)).fetchone()["n"]
        submitted = conn.execute(
            "SELECT COUNT(*) AS n FROM submissions s JOIN urls u ON s.url_id=u.id "
            "WHERE u.domain_id=?", (d["id"],)).fetchone()["n"]
        success = conn.execute(
            "SELECT COUNT(*) AS n FROM submissions s JOIN urls u ON s.url_id=u.id "
            "WHERE u.domain_id=? AND s.status='success'", (d["id"],)).fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM submissions s JOIN urls u ON s.url_id=u.id "
            "WHERE u.domain_id=? AND s.status='failed'", (d["id"],)).fetchone()["n"]
        pending = total_urls - success
        progress = worker.get_progress(d["id"])
        out.append({
            "id": d["id"], "domain": d["domain"], "base_url": d["base_url"],
            "status": d["status"], "last_scan_at": d["last_scan_at"],
            "last_submit_at": d["last_submit_at"],
            "total_urls": total_urls, "queued": queued, "submitted": submitted,
            "successful": success, "failed": failed, "pending": max(pending, 0),
            "live_progress": progress,
        })
    return jsonify(out)


# ---------------------------------------------------------------- domains

@app.route("/api/domains", methods=["POST"])
def add_domain():
    payload = request.get_json(force=True)
    raw = (payload.get('domain') or '').strip()
    if not raw:
        return jsonify({"error": "domain is required"}), 400

    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse.urlparse(raw)
    domain_key = parsed.netloc.lower()
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    conn = db.get_conn()
    existing = conn.execute("SELECT id FROM domains WHERE domain=?", (domain_key,)).fetchone()
    if existing:
        # already tracking this one, just point back at it
        return jsonify({"error": "domain already added", "id": existing["id"]}), 409

    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO domains (domain, base_url, added_at, status) VALUES (?,?,?,?)",
            (domain_key, base_url, now(), "idle"))
        domain_id = cur.lastrowid

    return jsonify({"id": domain_id, "domain": domain_key, "base_url": base_url}), 201


@app.route("/api/domains/<int:domain_id>/scan", methods=["POST"])
def start_scan(domain_id):
    conn = db.get_conn()
    d = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if not d:
        return jsonify({"error": "domain not found"}), 404

    # resume an interrupted scan if one exists, else start a fresh one
    interrupted = conn.execute(
        "SELECT id FROM scans WHERE domain_id=? AND status='interrupted' "
        "ORDER BY id DESC LIMIT 1", (domain_id,)).fetchone()
    resume_id = interrupted["id"] if interrupted else None

    t = threading.Thread(target=worker.scan_domain, args=(domain_id,), kwargs={
        "resume_scan_id": resume_id}, daemon=True)
    t.start()
    return jsonify({"started": True, "resumed": bool(resume_id)})


@app.route("/api/domains/<int:domain_id>/progress")
def domain_progress(domain_id):
    return jsonify(worker.get_progress(domain_id))


@app.route('/api/domains/<int:domain_id>/urls')
def domain_urls(domain_id):
    conn = db.get_conn()
    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 100)), 500)
    offset = (page - 1) * per_page
    source = request.args.get("source")
    accessible = request.args.get("accessible")  # "1" or "0"

    sql = "SELECT * FROM urls WHERE domain_id=?"
    params = [domain_id]
    if source:
        sql += " AND discovery_source=?"
        params.append(source)
    if accessible in ("0", "1"):
        sql += " AND accessible=?"
        params.append(int(accessible))
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [per_page, offset]

    rows = conn.execute(sql, params).fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM urls WHERE domain_id=?",
                          (domain_id,)).fetchone()["n"]
    return jsonify({
        "total": total, "page": page, "per_page": per_page,
        "urls": [dict(r) for r in rows],
    })


# ----------------------------------------------------------------- queue

@app.route("/api/queue/status")
def queue_status():
    conn = db.get_conn()
    counts = {}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM queue GROUP BY status"):
        counts[row["status"]] = row["n"]
    return jsonify(counts)


@app.route("/api/domains/<int:domain_id>/enqueue-all", methods=["POST"])
def enqueue_all(domain_id):
    # lets you force a re-archive of every known url for a domain
    # (button on the dashboard calls this)
    services = request.get_json(silent=True) or {}
    service_list = services.get("services", ["wayback", "archive_today"])
    conn = db.get_conn()
    url_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM urls WHERE domain_id=?", (domain_id,))]
    with db.tx() as c:
        for uid in url_ids:
            for service in service_list:
                c.execute(
                    "INSERT INTO queue (url_id, service, status, created_at) VALUES (?,?,?,?)",
                    (uid, service, "queued", now()))
    return jsonify({"enqueued_urls": len(url_ids), "services": service_list})


# ---------------------------------------------------------------- search

@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    service = request.args.get('service')
    status = request.args.get('status')
    domain_id = request.args.get("domain_id")
    #print("search hit", q, service, status)  # was debugging the filter, leaving it commented just in case

    sql = ("SELECT u.*, d.domain AS domain_name, "
           "(SELECT status FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) "
           "AS latest_status, "
           "(SELECT archive_url FROM submissions s WHERE s.url_id=u.id AND s.status='success' "
           "ORDER BY s.id DESC LIMIT 1) AS latest_archive_url "
           "FROM urls u JOIN domains d ON u.domain_id = d.id WHERE 1=1")
    params = []

    if q:
        sql += " AND (u.normalized_url LIKE ? OR d.domain LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if domain_id:
        sql += " AND u.domain_id = ?"
        params.append(domain_id)
    sql += " ORDER BY u.id DESC LIMIT 200"

    conn = db.get_conn()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if service or status:
        filtered = []
        for r in rows:
            sub_sql = "SELECT * FROM submissions WHERE url_id=?"
            subs = conn.execute(sub_sql, (r["id"],)).fetchall()
            match = False
            for s in subs:
                if service and s["service"] != service:
                    continue
                if status and s["status"] != status:
                    continue
                match = True
                break
            if match:
                filtered.append(r)
        rows = filtered

    return jsonify(rows)


@app.route("/api/urls/<int:url_id>/history")
def url_history(url_id):
    conn = db.get_conn()
    url_row = conn.execute("SELECT * FROM urls WHERE id=?", (url_id,)).fetchone()
    if not url_row:
        return jsonify({"error": "not found"}), 404
    subs = conn.execute(
        "SELECT * FROM submissions WHERE url_id=? ORDER BY id DESC", (url_id,)).fetchall()
    return jsonify({"url": dict(url_row), "submissions": [dict(s) for s in subs]})


# --------------------------------------------------------- scans / changes

@app.route("/api/domains/<int:domain_id>/scans")
def domain_scans(domain_id):
    # scan history - this is what powers the "website changes" bit, you
    # can see how many new urls turned up on each re-scan
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM scans WHERE domain_id=? ORDER BY scan_number DESC", (domain_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ------------------------------------------------------------------ delete

@app.route("/api/domains/<int:domain_id>", methods=["DELETE"])
def delete_domain(domain_id):
    # cascades to urls/submissions/queue/scans/frontier via the FK
    # ON DELETE CASCADE in the schema
    with db.tx() as c:
        c.execute("DELETE FROM domains WHERE id=?", (domain_id,))
    return jsonify({"deleted": True})


# ------------------------------------------------------------------ export

@app.route("/api/domains/<int:domain_id>/export.csv")
def export_csv(domain_id):
    import csv
    import io
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT u.normalized_url, u.original_url, u.discovery_source, "
        "u.discovery_timestamp, u.http_status, u.accessible, "
        "(SELECT service FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) AS last_service, "
        "(SELECT status FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) AS last_status, "
        "(SELECT archive_url FROM submissions s WHERE s.url_id=u.id AND s.status='success' "
        "ORDER BY s.id DESC LIMIT 1) AS archive_url "
        "FROM urls u WHERE u.domain_id=? ORDER BY u.id", (domain_id,)).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["normalized_url", "original_url", "discovery_source", "discovery_timestamp",
                      "http_status", "accessible", "last_service", "last_status", "archive_url"])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])

    domain_row = conn.execute("SELECT domain FROM domains WHERE id=?", (domain_id,)).fetchone()
    fname = f"{(domain_row['domain'] if domain_row else 'export')}_archive.csv"
    resp = app.response_class(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}"
    return resp


@app.route("/api/domains/<int:domain_id>/export.json")
def export_json(domain_id):
    conn = db.get_conn()
    urls = conn.execute("SELECT * FROM urls WHERE domain_id=?", (domain_id,)).fetchall()
    out = []
    for u in urls:
        subs = conn.execute("SELECT * FROM submissions WHERE url_id=? ORDER BY id",
                             (u["id"],)).fetchall()
        row = dict(u)
        row["submissions"] = [dict(s) for s in subs]
        out.append(row)
    return jsonify(out)


# -------------------------------------------------------------- priority

@app.route("/api/urls/<int:url_id>/prioritize", methods=["POST"])
def prioritize_url(url_id):
    # bumps this url's still-queued jobs to the front of the line.
    # nothing fancy, just sets priority higher than the default 0
    with db.tx() as c:
        c.execute("UPDATE queue SET priority=10 WHERE url_id=? AND status='queued'", (url_id,))
    return jsonify({"prioritized": True})


def boot():
    # starts the sqlite schema + background worker (used by app.py and wsgi.py)
    db.init_db()
    worker.recover_interrupted_work()
    worker.start_background_worker()


if __name__ == "__main__":
    import os
    boot()
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1",
            use_reloader=False)
