// app.js - dashboard logic. nothing fancy, just fetch calls + DOM updates.
// (thought about pulling in a frontend framework for this but honestly
// vanilla js is fine for something this size)

const domainsListEl = document.getElementById("domainsList");
const addDomainForm = document.getElementById("addDomainForm");
const domainInput = document.getElementById("domainInput");
const searchResultsEl = document.getElementById("searchResults");
const queuePill = document.getElementById("queuePill");

let urlModalState = { domainId: null, page: 1, perPage: 40 };

async function api(path, opts) {
  const resp = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" }
  }, opts));
  if (!resp.ok && resp.status !== 409) {
    let msg = "something went wrong";
    try { msg = (await resp.json()).error || msg; } catch (e) {}
    toast(msg, true);
  }
  return resp.json();
}

function toast(msg, isErr) {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function timeAgo(iso) {
  if (!iso) return "never";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
  // could add "weeks/months ago" too but honestly didn't need it for testing
}

// ---------------------------------------------------------------- dashboard

async function refreshDashboard() {
  const domains = await api("/api/dashboard");
  domainsListEl.innerHTML = "";
  if (!Array.isArray(domains) || domains.length === 0) {
    domainsListEl.innerHTML = '<p class="empty-hint">No domains yet — add one above to get started.</p>';
    return;
  }
  domains.forEach(d => domainsListEl.appendChild(renderDomainCard(d)));

  const q = await api("/api/queue/status");
  const active = (q.queued || 0) + (q.processing || 0);
  queuePill.textContent = active > 0 ? `queue: ${active} pending` : "queue: idle";
}

function renderDomainCard(d) {
  const card = document.createElement("div");
  card.className = "domain-card";

  const progress = d.live_progress || {};
  let progressLine = "";
  if (progress.phase === "discovering") {
    progressLine = `<span class="live-line">● discovering — ${progress.pages_fetched || 0} pages fetched, ${progress.urls_found || 0} urls found</span>`;
  } else if (progress.phase === "discovery_complete") {
    progressLine = `last scan found ${progress.urls_found || 0} urls (${progress.urls_new || 0} new)`;
  }

  const pct = d.total_urls > 0 ? Math.round((d.successful / d.total_urls) * 100) : 0;

  card.innerHTML = `
    <div class="domain-card-head">
      <div>
        <div class="domain-name">${d.domain}</div>
        <a class="domain-url" href="${d.base_url}" target="_blank">${d.base_url}</a>
      </div>
      <div class="head-right">
        <span class="status-pill status-${d.status}">${d.status}</span>
        <button class="icon-btn" data-action="delete" data-id="${d.id}" title="remove domain">🗑</button>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-box"><div class="stat-num">${d.total_urls}</div><div class="stat-label">Found</div></div>
      <div class="stat-box"><div class="stat-num">${d.queued}</div><div class="stat-label">Queued</div></div>
      <div class="stat-box stat-success"><div class="stat-num">${d.successful}</div><div class="stat-label">Archived</div></div>
      <div class="stat-box stat-failed"><div class="stat-num">${d.failed}</div><div class="stat-label">Failed</div></div>
      <div class="stat-box stat-pending"><div class="stat-num">${d.pending}</div><div class="stat-label">Pending</div></div>
    </div>

    <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
    ${progressLine ? `<div class="progress-line">${progressLine}</div>` : ""}
    <div class="progress-line">last scan ${timeAgo(d.last_scan_at)} · last submission ${timeAgo(d.last_submit_at)}</div>

    <div class="domain-actions">
      <button data-action="scan" data-id="${d.id}">▶ Discover</button>
      <button data-action="reenqueue" data-id="${d.id}">↻ Re-archive all</button>
      <a class="ghost-btn" data-action="view" data-id="${d.id}">📄 Browse URLs</a>
      <a class="ghost-btn" data-action="scans" data-id="${d.id}">📈 Scan history</a>
    </div>
  `;

  card.querySelector('[data-action="scan"]').addEventListener("click", async () => {
    const r = await api(`/api/domains/${d.id}/scan`, { method: "POST" });
    toast(r.resumed ? "resuming previous scan…" : "discovery started…");
    refreshDashboard();
  });
  card.querySelector('[data-action="reenqueue"]').addEventListener("click", async () => {
    await api(`/api/domains/${d.id}/enqueue-all`, {
      method: "POST", body: JSON.stringify({ services: ["wayback", "archive_today"] })
    });
    toast("re-queued every url for this domain");
    refreshDashboard();
  });
  card.querySelector('[data-action="view"]').addEventListener("click", () => openUrlModal(d));
  card.querySelector('[data-action="scans"]').addEventListener("click", () => openScansModal(d));
  card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
    if (!confirm(`Stop tracking ${d.domain} and delete all its data? This can't be undone.`)) return;
    await api(`/api/domains/${d.id}`, { method: "DELETE" });
    toast(`removed ${d.domain}`);
    refreshDashboard();
  });

  return card;
}

addDomainForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const domain = domainInput.value.trim();
  if (!domain) return;
  const res = await api("/api/domains", { method: "POST", body: JSON.stringify({ domain }) });
  if (res.error) {
    toast(res.error, true);
  } else {
    domainInput.value = "";
    toast(`tracking ${res.domain}`);
    refreshDashboard();
  }
});

// ------------------------------------------------------------------ search

document.getElementById("searchBtn").addEventListener("click", runSearch);
document.getElementById("searchQ").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

async function runSearch() {
  const q = document.getElementById("searchQ").value.trim();
  const status = document.getElementById("searchStatus").value;
  const service = document.getElementById("searchService").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (service) params.set("service", service);

  const rows = await api(`/api/search?${params.toString()}`);
  searchResultsEl.innerHTML = "";
  if (!Array.isArray(rows) || rows.length === 0) {
    searchResultsEl.innerHTML = '<p class="empty-hint">No matching URLs found.</p>';
    return;
  }
  rows.forEach(r => {
    const row = document.createElement("div");
    row.className = "search-result-row";
    const statusBadge = r.latest_status
      ? `<span class="badge ${r.latest_status === 'success' ? 'badge-ok' : r.latest_status === 'failed' ? 'badge-bad' : ''}">${r.latest_status}</span>`
      : `<span class="badge">not submitted</span>`;
    row.innerHTML = `
      <span>${r.domain_name} — ${r.normalized_url}</span>
      <span>${statusBadge} ${r.latest_archive_url ? `· <a href="${r.latest_archive_url}" target="_blank" onclick="event.stopPropagation()">view archive</a>` : ""}</span>
    `;
    row.addEventListener("click", () => openHistoryModal(r.id, r.normalized_url));
    searchResultsEl.appendChild(row);
  });
}

// -------------------------------------------------------------- url modal

function openUrlModal(domain) {
  urlModalState = { domainId: domain.id, domainName: domain.domain, page: 1, perPage: 40 };
  document.getElementById("urlModalTitle").textContent = `URLs — ${domain.domain}`;
  document.getElementById("exportCsvLink").href = `/api/domains/${domain.id}/export.csv`;
  document.getElementById("exportJsonLink").href = `/api/domains/${domain.id}/export.json`;
  document.getElementById("urlModalBackdrop").classList.add("open");
  loadUrlPage();
}

async function loadUrlPage() {
  const source = document.getElementById("filterSource").value;
  const accessible = document.getElementById("filterAccessible").value;
  const params = new URLSearchParams({
    page: urlModalState.page, per_page: urlModalState.perPage
  });
  if (source) params.set("source", source);
  if (accessible !== "") params.set("accessible", accessible);

  const data = await api(`/api/domains/${urlModalState.domainId}/urls?${params.toString()}`);
  const body = document.getElementById("urlModalBody");
  body.innerHTML = "";
  if (!data.urls || data.urls.length === 0) {
    body.innerHTML = '<p class="empty-hint">No urls match this filter yet.</p>';
  } else {
    data.urls.forEach(u => {
      const row = document.createElement("div");
      row.className = "url-row";
      const badge = u.accessible
        ? `<span class="badge badge-ok">${u.http_status || 'ok'}</span>`
        : `<span class="badge badge-bad">${u.http_status || 'failed'}</span>`;
      row.innerHTML = `<span>${u.normalized_url}</span><span>${badge}</span>`;
      row.addEventListener("click", () => openHistoryModal(u.id, u.normalized_url));
      body.appendChild(row);
    });
  }
  const pages = Math.max(1, Math.ceil(data.total / urlModalState.perPage));
  document.getElementById("urlPageLabel").textContent = `page ${urlModalState.page} of ${pages} (${data.total} total)`;
}

document.getElementById("urlModalClose").addEventListener("click", () =>
  document.getElementById("urlModalBackdrop").classList.remove("open"));
document.getElementById("urlPrevPage").addEventListener("click", () => {
  if (urlModalState.page > 1) { urlModalState.page--; loadUrlPage(); }
});
document.getElementById("urlNextPage").addEventListener("click", () => {
  urlModalState.page++; loadUrlPage();
});
document.getElementById("filterSource").addEventListener("change", () => { urlModalState.page = 1; loadUrlPage(); });
document.getElementById("filterAccessible").addEventListener("change", () => { urlModalState.page = 1; loadUrlPage(); });

// ---------------------------------------------------------- history modal

async function openHistoryModal(urlId, urlText) {
  document.getElementById("historyModalBackdrop").classList.add("open");
  const body = document.getElementById("historyModalBody");
  body.innerHTML = "<p class='empty-hint'>loading…</p>";

  const data = await api(`/api/urls/${urlId}/history`);
  body.innerHTML = "";

  const head = document.createElement("div");
  head.style.cssText = "font-size:12.5px;color:var(--muted);margin-bottom:12px;word-break:break-all;";
  head.textContent = urlText;
  body.appendChild(head);

  const prioBtn = document.createElement("button");
  prioBtn.textContent = "⚡ prioritize any pending submissions";
  prioBtn.style.cssText = "font-size:11.5px;padding:7px 12px;margin-bottom:12px;";
  prioBtn.addEventListener("click", async () => {
    await api(`/api/urls/${urlId}/prioritize`, { method: "POST" });
    toast("bumped to front of the queue");
  });
  body.appendChild(prioBtn);

  if (!data.submissions || data.submissions.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-hint";
    p.textContent = "no submission attempts yet.";
    body.appendChild(p);
    return;
  }

  data.submissions.forEach(s => {
    const row = document.createElement("div");
    row.className = "history-row";
    const badgeClass = s.status === "success" ? "badge-ok" : s.status === "failed" ? "badge-bad" : "";
    row.innerHTML = `
      <span>${s.service} · attempt #${s.attempt_number} · ${timeAgo(s.submitted_at)}</span>
      <span>
        <span class="badge ${badgeClass}">${s.status}</span>
        ${s.archive_url ? ` · <a href="${s.archive_url}" target="_blank">open</a>` : ""}
      </span>
    `;
    if (s.error_message) {
      const err = document.createElement("div");
      err.style.cssText = "color:var(--fail);font-size:11px;margin-top:-4px;margin-bottom:6px;";
      err.textContent = s.error_message;
      body.appendChild(row);
      body.appendChild(err);
    } else {
      body.appendChild(row);
    }
  });
}
document.getElementById("historyModalClose").addEventListener("click", () =>
  document.getElementById("historyModalBackdrop").classList.remove("open"));

// ------------------------------------------------------------- scans modal

async function openScansModal(domain) {
  document.getElementById("scansModalBackdrop").classList.add("open");
  const body = document.getElementById("scansModalBody");
  body.innerHTML = "<p class='empty-hint'>loading…</p>";

  const scans = await api(`/api/domains/${domain.id}/scans`);
  body.innerHTML = "";
  if (!Array.isArray(scans) || scans.length === 0) {
    body.innerHTML = '<p class="empty-hint">no scans run yet for this domain.</p>';
    return;
  }
  scans.forEach(s => {
    const row = document.createElement("div");
    row.className = "history-row";
    row.innerHTML = `
      <span>scan #${s.scan_number} · ${timeAgo(s.started_at)}</span>
      <span>${s.urls_found} found <strong style="color:var(--success)">(+${s.urls_new} new)</strong> · ${s.status}</span>
    `;
    body.appendChild(row);
  });
}
document.getElementById("scansModalClose").addEventListener("click", () =>
  document.getElementById("scansModalBackdrop").classList.remove("open"));

// close modals by clicking the backdrop
document.querySelectorAll(".modal-backdrop").forEach(bd => {
  bd.addEventListener("click", (e) => { if (e.target === bd) bd.classList.remove("open"); });
});

refreshDashboard();
setInterval(refreshDashboard, 3000);
