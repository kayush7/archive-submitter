# archiver.py
# actually submits urls to the archive services. only using public,
# documented ways of doing this - no captcha bypass, no auth bypass etc,
# since the assignment specifically says not to do that.
#
# wayback machine: they have a plain "save page now" endpoint at
# web.archive.org/save/<url>, no api key needed. response gives back a
# Content-Location header like /web/<timestamp>/<url> which is basically
# the archive link.
#
# archive.today: doesn't really have a public api, and their submit form
# throws a captcha at anything that looks automated. so this just tries
# the normal POST, and if it gets served a captcha page it just records
# that as a failed submission with a message explaining why - not going
# to try and get around it. mentioned this as a known limitation in the
# readme.

import re
import time

import requests

USER_AGENT = "ArchiveSubmitterBot/1.0 (+educational-project)"
REQUEST_TIMEOUT = 30


class SubmissionResult:
    def __init__(self, success, archive_url=None, archive_identifier=None,
                 error_message=None, http_status=None):
        self.success = success
        self.archive_url = archive_url
        self.archive_identifier = archive_identifier
        self.error_message = error_message
        self.http_status = http_status


def submit_wayback(url, session=None, retries=2, backoff=3):
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    save_endpoint = f"https://web.archive.org/save/{url}"

    last_error = None
    for attempt in range(1, retries + 2):
        try:
            resp = sess.get(save_endpoint, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(backoff * attempt)
            continue

        if resp.status_code == 429:
            last_error = "Rate limited (429) by web.archive.org"
            time.sleep(backoff * attempt * 2)
            continue

        content_location = resp.headers.get("Content-Location")
        if resp.status_code in (200, 302) and content_location:
            archive_url = f"https://web.archive.org{content_location}"
            m = re.search(r"/web/(\d+)/", content_location)
            identifier = m.group(1) if m else None
            return SubmissionResult(True, archive_url=archive_url,
                                     archive_identifier=identifier,
                                     http_status=resp.status_code)

        # Fallback: some responses land directly on the archived page URL.
        if resp.status_code == 200 and "/web/" in resp.url:
            m = re.search(r"/web/(\d+)/", resp.url)
            identifier = m.group(1) if m else None
            return SubmissionResult(True, archive_url=resp.url,
                                     archive_identifier=identifier,
                                     http_status=resp.status_code)

        last_error = f"Unexpected response (HTTP {resp.status_code}), no Content-Location header"
        time.sleep(backoff * attempt)

    return SubmissionResult(False, error_message=last_error or "Unknown error", http_status=None)


CAPTCHA_MARKERS = ("g-recaptcha", "cf-challenge", "captcha", "are you a robot", "verify you are human")


def submit_archive_today(url, session=None, retries=1, backoff=3):
    # best effort - see the note at the top of the file about captchas
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

    mirrors = ["https://archive.ph", "https://archive.today", "https://archive.is"]
    # archive.ph seems to be the most reliable mirror in my testing, keeping
    # it first in the list
    last_error = None

    for mirror in mirrors:
        try:
            resp = sess.post(f"{mirror}/submit/", data={"url": url},
                              timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            last_error = f"{mirror}: {e}"
            continue

        body_lower = resp.text.lower()
        if any(marker in body_lower for marker in CAPTCHA_MARKERS):
            last_error = (f"{mirror} presented a CAPTCHA/challenge page - "
                           "automated bypass is out of scope per assignment rules")
            continue

        # archive.today redirects to the finished snapshot URL, or embeds
        # a "wip/<id>" placeholder while the snapshot is being generated.
        if "archive.ph" in resp.url or "archive.today" in resp.url or "archive.is" in resp.url:
            if "/submit/" not in resp.url and resp.url.rstrip("/") != mirror:
                return SubmissionResult(True, archive_url=resp.url,
                                         archive_identifier=resp.url.rsplit("/", 1)[-1],
                                         http_status=resp.status_code)

        last_error = f"{mirror}: could not confirm a completed snapshot (HTTP {resp.status_code})"

    return SubmissionResult(False, error_message=last_error or "All mirrors unreachable")


SERVICES = {
    "wayback": submit_wayback,
    "archive_today": submit_archive_today,
}
