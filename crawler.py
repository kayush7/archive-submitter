# crawler.py
# does the actual url discovery - combines a normal BFS crawl of the html
# with sitemap.xml parsing, robots.txt, canonical tags, rss feeds and
# pagination links. tries to stay on the same domain unless you tell it
# not to.
#
# the frontier stuff (see db.crawl_frontier) is so a scan can pick back up
# where it left off if it gets interrupted instead of starting over, which
# matters a lot once a site has a few thousand pages.

import time
import urllib.parse as urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

USER_AGENT = "ArchiveSubmitterBot/1.0 (+educational-project; respects robots.txt)"
REQUEST_TIMEOUT = 10
DEFAULT_MAX_PAGES = 500          # safety cap for the live-HTML crawl per scan
DEFAULT_MAX_SITEMAP_URLS = 20000


def _same_domain(url, root_netloc):
    try:
        netloc = urlparse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    netloc = netloc.split(":")[0]
    root = root_netloc.split(':')[0]
    return netloc == root or netloc.endswith("." + root)


def normalize_url(url):
    # lowercase scheme/host, drop the #fragment (doesn't change what the
    # server sends back), strip default ports, trailing slash, and sort
    # query params so ?a=1&b=2 and ?b=2&a=1 count as the same url.
    # original url (before normalizing) still gets kept separately in the db.
    parts = urlparse.urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = sorted(urlparse.parse_qsl(parts.query, keep_blank_values=True))
    query = urlparse.urlencode(query_pairs)
    # fragment intentionally dropped - fragments don't change server content
    return urlparse.urlunsplit((scheme, netloc, path, query, ""))


class Discovery:
    # represents one discovery run for a single domain. call run_full_discovery()
    # and it'll yield dicts for every url it finds (resumable via frontier)

    def __init__(self, base_url, allow_external=False, max_pages=DEFAULT_MAX_PAGES,
                 max_sitemap_urls=DEFAULT_MAX_SITEMAP_URLS, on_progress=None):
        self.base_url = base_url.rstrip("/")
        self.root_netloc = urlparse.urlparse(self.base_url).netloc.lower()
        self.allow_external = allow_external
        self.max_pages = max_pages
        self.max_sitemap_urls = max_sitemap_urls
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.on_progress = on_progress or (lambda **kw: None)
        self.seen = set()

    def _get(self, url):
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            return resp
        except requests.RequestException as e:
            self.on_progress(event="fetch_error", url=url, error=str(e))
            return None

    # robots.txt just for the Sitemap: lines
    def discover_from_robots(self):
        found = []
        resp = self._get(urlparse.urljoin(self.base_url + "/", "robots.txt"))
        if not resp or resp.status_code != 200:
            return found
        for line in resp.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                found.append(sitemap_url)
        return found

    # handles both plain sitemaps and sitemap index files (index -> nested sitemaps)
    def discover_from_sitemap(self, sitemap_url, depth=0):
        results = []
        if depth > 4 or len(results) >= self.max_sitemap_urls:
            return results
        resp = self._get(sitemap_url)
        if not resp or resp.status_code != 200:
            return results
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return results

        tag = root.tag.lower()
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        if tag.endswith("sitemapindex"):
            for sm in root.findall(".//sm:sitemap/sm:loc", ns) or root.findall(".//sitemap/loc"):
                if sm.text:
                    self.on_progress(event="sitemap_index_child", url=sm.text.strip())
                    results.extend(self.discover_from_sitemap(sm.text.strip(), depth + 1))
        elif tag.endswith("urlset"):
            locs = root.findall(".//sm:url/sm:loc", ns) or root.findall(".//url/loc")
            for loc in locs:
                if loc.text:
                    results.append((loc.text.strip(), "sitemap"))
                if len(results) >= self.max_sitemap_urls:
                    break
        return results

    def discover_feeds(self, html, page_url):
        soup = BeautifulSoup(html, "lxml")
        feeds = []
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            t = (link.get("type") or "").lower()
            if "rss" in t or "atom" in t or "xml" in t:
                href = link.get("href")
                if href:
                    feeds.append(urlparse.urljoin(page_url, href))
        return feeds

    # normal BFS crawl of the site. existing_frontier is passed in when we're
    # resuming a scan that got cut off, so we don't refetch pages we already did
    def crawl_html(self, seed_urls, existing_frontier=None):
        queue = list(existing_frontier) if existing_frontier else list(seed_urls)
        visited = set()
        pages_fetched = 0

        while queue and pages_fetched < self.max_pages:
            url = queue.pop(0)
            norm = normalize_url(url)
            if norm in visited:
                continue
            visited.add(norm)

            resp = self._get(url)
            pages_fetched += 1
            self.on_progress(event="page_fetched", url=url, count=pages_fetched)

            if not resp:
                yield {"url": url, "source": "html_link", "http_status": None,
                       "accessible": False, "redirect": False}
                continue

            final_url = resp.url
            is_redirect = final_url != url
            yield {"url": url, "source": "html_link", "http_status": resp.status_code,
                   "accessible": resp.status_code < 400, "redirect": is_redirect,
                   "redirect_target": final_url if is_redirect else None}

            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code >= 400 or "text/html" not in ctype:
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # canonical
            canonical = soup.find("link", rel="canonical")
            if canonical and canonical.get("href"):
                curl = urlparse.urljoin(final_url, canonical["href"])
                yield {"url": curl, "source": "canonical", "http_status": None,
                       "accessible": True, "redirect": False}

            # feeds
            for feed_url in self.discover_feeds(resp.text, final_url):
                yield {"url": feed_url, "source": "feed", "http_status": None,
                       "accessible": True, "redirect": False}

            # pagination
            for rel_name in ("next", "prev"):
                nav = soup.find("link", rel=rel_name)
                if nav and nav.get("href"):
                    purl = urlparse.urljoin(final_url, nav["href"])
                    yield {"url": purl, "source": "pagination", "http_status": None,
                           "accessible": True, "redirect": False}
                    if normalize_url(purl) not in visited:
                        queue.append(purl)

            # internal links
            for a in soup.find_all('a', href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                link = urlparse.urljoin(final_url, href)
                if not self.allow_external and not _same_domain(link, self.root_netloc):
                    continue  # skip external stuff unless told otherwise
                nlink = normalize_url(link)
                if nlink not in visited:
                    queue.append(link)

    # runs everything in order: robots.txt -> sitemaps -> then the actual
    # html crawl (which itself also picks up canonical/feed/pagination links)
    def run_full_discovery(self, existing_frontier=None):
        sitemap_urls = set(self.discover_from_robots())
        sitemap_urls.add(urlparse.urljoin(self.base_url + "/", "sitemap.xml"))

        for sm in sitemap_urls:
            for loc, source in self.discover_from_sitemap(sm):
                yield {"url": loc, "source": source, "http_status": None,
                       "accessible": True, "redirect": False}

        seeds = [self.base_url + "/"]
        yield from self.crawl_html(seeds, existing_frontier=existing_frontier)
