// Panella operator console (WP-B3). Security spine: stored XSS, not CSRF.
//
// Every field that can contain attacker-influenced content (content_preview, search hit fields,
// audit detail rows) is rendered with textContent / createElement ONLY. This file must never
// contain: innerHTML, outerHTML, insertAdjacentHTML, document.write, eval, new Function, or an
// inline event-handler attribute written into generated markup. tests/test_console.py greps this
// exact file for those tokens on every test run — if you're tempted to add one, don't; render the
// same data with document.createElement + textContent instead.
//
// Secrets (the owner bearer + the approval token) live ONLY in the two module-scope variables
// below. Never localStorage, never sessionStorage, never document.cookie, never a query string or
// URL fragment — closing the tab or reloading forgets both, on purpose.

(function () {
  "use strict";

  let ownerBearer = null;
  let approvalToken = null;

  const BADGE_POLL_MS = 30000;
  let badgePollHandle = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function setStatus(node, message, kind) {
    // kind is "ok" | "error" | "" — never attacker-controlled, only used to pick a CSS class.
    clearChildren(node);
    node.className = kind === "error" ? "status-error" : kind === "ok" ? "status-ok" : "";
    node.appendChild(document.createTextNode(message));
  }

  // Fixed-string error rendering: the server's uniform "approval refused" (and every other error
  // message) is safe to show verbatim (it never echoes caller-supplied content — see
  // panella/http/errors.py), but we still only ever place it via textContent, and we always pair
  // it with the raw HTTP status code so the operator can tell "refused" apart from "unreachable".
  function describeError(status, bodyMessage) {
    const suffix = bodyMessage ? ": " + bodyMessage : "";
    return "HTTP " + String(status) + suffix;
  }

  async function apiFetch(path, { method, headers, body, needsApprovalToken } = {}) {
    const reqHeaders = Object.assign({}, headers);
    if (ownerBearer) {
      reqHeaders["Authorization"] = "Bearer " + ownerBearer;
    }
    if (needsApprovalToken && approvalToken) {
      reqHeaders["X-Approval-Token"] = approvalToken;
    }
    const response = await fetch(path, { method: method || "GET", headers: reqHeaders, body: body, credentials: "omit" });
    let parsed = null;
    try {
      parsed = await response.json();
    } catch (_err) {
      parsed = null;
    }
    return { status: response.status, ok: response.ok, data: parsed };
  }

  // --- connect --------------------------------------------------------------------------------

  function onConnectSubmit(event) {
    event.preventDefault();
    const bearerInput = byId("owner-bearer-input");
    const tokenInput = byId("approval-token-input");
    ownerBearer = bearerInput.value.trim() || null;
    approvalToken = tokenInput.value.trim() || null;
    // Never re-render the secret back into the DOM (no "confirm your bearer" echo) — only clear
    // the visible inputs so the values don't linger on-screen after connect.
    bearerInput.value = "";
    tokenInput.value = "";

    const statusNode = byId("connect-status");
    if (!ownerBearer) {
      setStatus(statusNode, "owner bearer is required", "error");
      return;
    }
    probeConnection(statusNode);
  }

  async function probeConnection(statusNode) {
    setStatus(statusNode, "connecting...", "");
    // Validate the bearer against a BEARER-ONLY endpoint (stats), NOT the approvals surface. The
    // approval routes 404 on a box whose governance transport is not local_cli, so gating the whole
    // console on /v1/approvals/count would hide search/audit/stats — which need only the owner
    // bearer — from an entire class of deployments (GH-bot B3 P2). Approvals are revealed separately,
    // only if their surface actually exists on this box.
    let bearerResult;
    try {
      bearerResult = await apiFetch("/v1/memory/stats");
    } catch (_err) {
      setStatus(statusNode, "network error contacting this box", "error");
      return;
    }
    if (!bearerResult.ok) {
      const message = bearerResult.data && typeof bearerResult.data.message === "string" ? bearerResult.data.message : "";
      setStatus(statusNode, describeError(bearerResult.status, message), "error");
      return;
    }
    setStatus(statusNode, describeError(bearerResult.status, "connected"), "ok");
    revealBearerPanels();
    refreshAudit();
    refreshStats();
    // Probe the approval surface independently: reveal + poll it only where it exists.
    let approvalResult = null;
    try {
      approvalResult = await apiFetch("/v1/approvals/count");
    } catch (_err) {
      approvalResult = null;
    }
    byId("approvals-panel").hidden = false;
    if (approvalResult && approvalResult.ok) {
      startBadgePoll();
      refreshApprovals();
    } else {
      // Surface exists in the UI but this deployment's transport is not local_cli — say so plainly
      // instead of leaving a dead panel or hiding the whole console.
      setStatus(byId("approvals-status"), "approvals are not available on this deployment", "");
    }
  }

  function revealBearerPanels() {
    // The owner-bearer-only surfaces (approvals is revealed separately in probeConnection).
    for (const id of ["search-panel", "audit-panel", "stats-panel"]) {
      byId(id).hidden = false;
    }
  }

  // --- pending approvals + badge ---------------------------------------------------------------

  function startBadgePoll() {
    if (badgePollHandle !== null) {
      return;
    }
    refreshBadge();
    badgePollHandle = window.setInterval(refreshBadge, BADGE_POLL_MS);
  }

  async function refreshBadge() {
    if (!ownerBearer) {
      return;
    }
    try {
      const result = await apiFetch("/v1/approvals/count");
      if (result.ok && result.data && typeof result.data.pending_count === "number") {
        byId("pending-badge").textContent = String(result.data.pending_count);
      }
    } catch (_err) {
      // Silent — the badge is a best-effort poll; the approvals panel surfaces real errors.
    }
  }

  async function refreshApprovals() {
    const statusNode = byId("approvals-status");
    const list = byId("pending-list");
    if (!approvalToken) {
      setStatus(statusNode, "paste the approval token above to list pending candidates", "");
      clearChildren(list);
      return;
    }
    setStatus(statusNode, "loading...", "");
    try {
      const result = await apiFetch("/v1/approvals/pending?limit=20", { needsApprovalToken: true });
      if (!result.ok) {
        const message = result.data && typeof result.data.message === "string" ? result.data.message : "";
        setStatus(statusNode, describeError(result.status, message), "error");
        clearChildren(list);
        return;
      }
      renderPendingList(list, (result.data && result.data.pending) || []);
      setStatus(statusNode, "", "");
    } catch (_err) {
      setStatus(statusNode, "network error", "error");
    }
  }

  function renderPendingList(list, items) {
    clearChildren(list);
    for (const item of items) {
      list.appendChild(buildPendingRow(item));
    }
  }

  function buildPendingRow(item) {
    const row = document.createElement("li");

    const meta = document.createElement("div");
    meta.className = "item-meta";
    meta.appendChild(
      document.createTextNode(
        "#" + String(item.approval_id) + " · " + String(item.wing || "") + "/" + String(item.room || "") +
          " · " + String(item.memory_type || "") + " · " + String(item.created_at || "") +
          // proposed_by is candidate-derived data — same stored-XSS rule as content_preview:
          // textContent only, never innerHTML.
          " · by " + String(item.proposed_by || "unknown"),
      ),
    );
    row.appendChild(meta);

    const preview = document.createElement("div");
    // content_preview is attacker-influenced (it is candidate content awaiting approval) — textContent
    // only, never innerHTML. This is the exact field the XSS round-trip test seeds.
    preview.appendChild(document.createTextNode(String(item.content_preview || "")));
    row.appendChild(preview);

    const actions = document.createElement("div");
    actions.className = "item-actions";

    const approveButton = document.createElement("button");
    approveButton.type = "button";
    approveButton.appendChild(document.createTextNode("Approve"));
    approveButton.addEventListener("click", function () {
      decideApproval(item.approval_id, "approve");
    });
    actions.appendChild(approveButton);

    const rejectButton = document.createElement("button");
    rejectButton.type = "button";
    rejectButton.appendChild(document.createTextNode("Reject"));
    rejectButton.addEventListener("click", function () {
      decideApproval(item.approval_id, "reject");
    });
    actions.appendChild(rejectButton);

    row.appendChild(actions);
    return row;
  }

  async function decideApproval(approvalId, verb) {
    const statusNode = byId("approvals-status");
    setStatus(statusNode, verb + "ing #" + String(approvalId) + "...", "");
    try {
      const result = await apiFetch("/v1/approvals/" + encodeURIComponent(String(approvalId)) + "/" + verb, {
        method: "POST",
        needsApprovalToken: true,
      });
      if (!result.ok) {
        const message = result.data && typeof result.data.message === "string" ? result.data.message : "";
        setStatus(statusNode, describeError(result.status, message), "error");
        return;
      }
      setStatus(statusNode, "#" + String(approvalId) + " " + verb + "d", "ok");
      refreshApprovals();
      refreshBadge();
    } catch (_err) {
      setStatus(statusNode, "network error", "error");
    }
  }

  // --- search ----------------------------------------------------------------------------------

  async function onSearchSubmit(event) {
    event.preventDefault();
    const query = byId("search-query-input").value.trim();
    const statusNode = byId("search-status");
    const list = byId("search-results");
    if (!query) {
      setStatus(statusNode, "enter a query", "error");
      return;
    }
    setStatus(statusNode, "searching...", "");
    try {
      const result = await apiFetch("/v1/memory/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query }),
      });
      if (!result.ok) {
        const message = result.data && typeof result.data.message === "string" ? result.data.message : "";
        setStatus(statusNode, describeError(result.status, message), "error");
        clearChildren(list);
        return;
      }
      renderSearchResults(list, (result.data && result.data.hits) || []);
      setStatus(statusNode, "", "");
    } catch (_err) {
      setStatus(statusNode, "network error", "error");
    }
  }

  function renderSearchResults(list, hits) {
    clearChildren(list);
    for (const hit of hits) {
      const row = document.createElement("li");
      // Search hits are stored memory content — same untrusted-content rule as content_preview.
      row.appendChild(document.createTextNode(JSON.stringify(hit)));
      list.appendChild(row);
    }
  }

  // --- audit -----------------------------------------------------------------------------------

  async function refreshAudit() {
    const statusNode = byId("audit-status");
    const list = byId("audit-list");
    setStatus(statusNode, "loading...", "");
    try {
      const result = await apiFetch("/v1/memory/audit?limit=50");
      if (!result.ok) {
        const message = result.data && typeof result.data.message === "string" ? result.data.message : "";
        setStatus(statusNode, describeError(result.status, message), "error");
        clearChildren(list);
        return;
      }
      renderAuditRows(list, (result.data && result.data.entries) || []);
      setStatus(statusNode, "", "");
    } catch (_err) {
      setStatus(statusNode, "network error", "error");
    }
  }

  function renderAuditRows(list, entries) {
    clearChildren(list);
    for (const entry of entries) {
      const row = document.createElement("li");
      row.appendChild(
        document.createTextNode(
          String(entry.seq) + " · " + String(entry.ts_iso) + " · " + String(entry.op) + " · " +
            String(entry.principal_id) + " · " + String(entry.tenant_accessed),
        ),
      );
      list.appendChild(row);
    }
  }

  // --- stats -----------------------------------------------------------------------------------

  async function refreshStats() {
    const statusNode = byId("stats-status");
    const summary = byId("stats-summary");
    setStatus(statusNode, "loading...", "");
    try {
      const result = await apiFetch("/v1/memory/stats");
      if (!result.ok) {
        const message = result.data && typeof result.data.message === "string" ? result.data.message : "";
        setStatus(statusNode, describeError(result.status, message), "error");
        clearChildren(summary);
        return;
      }
      renderStats(summary, result.data || {});
      setStatus(statusNode, "", "");
    } catch (_err) {
      setStatus(statusNode, "network error", "error");
    }
  }

  function renderStats(summary, data) {
    clearChildren(summary);
    appendStatRow(summary, "total_drawers", String(data.total_drawers != null ? data.total_drawers : ""));
    const wingBreakdown = Array.isArray(data.wing_breakdown) ? data.wing_breakdown : [];
    for (const wing of wingBreakdown) {
      appendStatRow(summary, "wing:" + String(wing.wing), String(wing.drawer_count));
    }
    appendStatRow(summary, "last_synced_ts", String(data.last_synced_ts != null ? data.last_synced_ts : ""));
  }

  function appendStatRow(dl, key, value) {
    const dt = document.createElement("dt");
    dt.appendChild(document.createTextNode(key));
    const dd = document.createElement("dd");
    dd.appendChild(document.createTextNode(value));
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // --- wiring ----------------------------------------------------------------------------------

  function init() {
    byId("connect-form").addEventListener("submit", onConnectSubmit);
    byId("search-form").addEventListener("submit", onSearchSubmit);
    byId("refresh-approvals-button").addEventListener("click", refreshApprovals);
    byId("refresh-audit-button").addEventListener("click", refreshAudit);
    byId("refresh-stats-button").addEventListener("click", refreshStats);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
