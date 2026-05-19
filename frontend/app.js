(() => {
  const STATES = ["form", "waiting", "mfa", "docs", "error", "boring"];
  const SERVER_STATE_LABELS = {
    IDLE: "Initializing",
    LOGGING_IN: "Signing in to the carrier",
    MFA_REQUIRED: "Waiting for your MFA code",
    AUTHENTICATING: "Verifying MFA code",
    FETCHING_DOCS: "Fetching documents",
    DONE: "Done",
    ERROR: "Error",
  };

  let sessionId = null;
  let eventSource = null;
  let mfaStartTs = null;
  let devCredentials = {};
  let lastAttemptedCarrier = null;

  function show(name) {
    for (const s of STATES) {
      document.getElementById(`state-${s}`).classList.toggle("hidden", s !== name);
    }
  }

  function setStatus(state, detail) {
    document.getElementById("waiting-state").textContent = state;
    document.getElementById("waiting-detail").textContent =
      detail || SERVER_STATE_LABELS[state] || state;
  }

  function showError(msg) {
    document.getElementById("error-msg").textContent = msg || "Unknown error";
    show("error");
  }

  function clearRepairPanel() {
    document.getElementById("repair-panel").classList.add("hidden");
    document.getElementById("repair-log").innerHTML = "";
    const verdict = document.getElementById("repair-verdict");
    verdict.classList.add("hidden");
    verdict.classList.remove("done", "need_human");
    verdict.textContent = "";
    document.getElementById("repair-status-label").textContent =
      "Claude is debugging";
  }

  function resetUI() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    sessionId = null;
    mfaStartTs = null;
    document.getElementById("login-form").reset();
    applyDevCredentials();
    document.getElementById("mfa-form").reset();
    document.getElementById("docs-list").innerHTML = "";
    document.getElementById("docs-summary").textContent = "";
    document.getElementById("docs-latency").textContent = "";
    clearRepairPanel();
    show("form");
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    if (!r.ok) {
      let payload = null;
      try { payload = await r.json(); } catch (_) { /* noop */ }
      const err = new Error(payload?.detail || `${r.status} ${r.statusText}`);
      err.status = r.status;
      err.payload = payload;
      throw err;
    }
    return r.json();
  }

  function listenForStatus(id) {
    eventSource = new EventSource(`/api/status/${id}`);

    const handler = (evt) => {
      let payload;
      try { payload = JSON.parse(evt.data); } catch (e) { return; }
      const { state, detail, docs, error, timings_ms, event } = payload;

      if (event === "repair_log") {
        appendRepairChunk(payload.repair_chunk);
        return;
      }
      if (event === "repair_done") {
        showRepairVerdict(payload.repair_chunk);
        return;
      }

      setStatus(state, detail);

      if (state === "MFA_REQUIRED") {
        show("mfa");
      } else if (state === "AUTHENTICATING" || state === "FETCHING_DOCS" || state === "LOGGING_IN") {
        show("waiting");
      } else if (docs && docs.length) {
        renderDocs(docs, timings_ms || null, state === "DONE");
        if (state === "DONE" && eventSource) { eventSource.close(); eventSource = null; }
      } else if (state === "ERROR") {
        showError(error || "Server error");
        // Don't close the EventSource immediately — auto-repair may stream
        // repair_log events into the same stream.
      }
    };

    eventSource.addEventListener("state_change", handler);
    eventSource.addEventListener("docs_ready", handler);
    eventSource.addEventListener("repair_log", handler);
    eventSource.addEventListener("repair_done", handler);
    eventSource.addEventListener("error", (e) => {
      if (e.data) handler(e);
    });
  }

  function appendRepairChunk(chunk) {
    if (!chunk) return;
    const panel = document.getElementById("repair-panel");
    panel.classList.remove("hidden");
    const list = document.getElementById("repair-log");
    const li = document.createElement("li");
    li.classList.add(`chunk-${chunk.kind || "text"}`);
    const turnTag = document.createElement("span");
    turnTag.className = "chunk-turn-label";
    turnTag.textContent = `t${chunk.turn || "?"}`;
    li.appendChild(turnTag);
    const body = document.createElement("span");
    body.textContent = renderChunkText(chunk);
    li.appendChild(body);
    list.appendChild(li);
    list.scrollTop = list.scrollHeight;
  }

  function renderChunkText(chunk) {
    if (chunk.kind === "tool_use") {
      return `[${chunk.tool}] ${chunk.input_preview || ""}`;
    }
    if (chunk.kind === "tool_result") {
      return `[tool result] ${chunk.text_preview || ""}`;
    }
    if (chunk.kind === "turn_end") {
      return `[turn end] ${chunk.text || ""}`;
    }
    return chunk.text || "";
  }

  function showRepairVerdict(chunk) {
    if (!chunk) return;
    const panel = document.getElementById("repair-panel");
    panel.classList.remove("hidden");
    const verdict = document.getElementById("repair-verdict");
    const label = document.getElementById("repair-status-label");
    const cls = (chunk.verdict || "").toLowerCase() === "done" ? "done" : "need_human";
    verdict.classList.remove("hidden", "done", "need_human");
    verdict.classList.add(cls);
    verdict.textContent = chunk.first_line || `Verdict: ${chunk.verdict}`;
    label.textContent =
      cls === "done"
        ? "Repair complete — retry your login when ready."
        : "Repair handed off — needs a human.";
    if (eventSource) { eventSource.close(); eventSource = null; }
  }

  function renderDocs(docs, timingsMs, complete = true) {
    const list = document.getElementById("docs-list");
    list.innerHTML = "";
    document.getElementById("docs-summary").textContent =
      `${docs.length} document${docs.length === 1 ? "" : "s"} retrieved${complete ? "." : "; still fetching."}`;

    const timingParts = [];
    const serverOrigin =
      timingsMs && timingsMs.mfa_code_received != null ? "Server MFA submit" : "Server fetch start";
    if (timingsMs && timingsMs.doc_pdf_bytes != null) {
      timingParts.push(`${serverOrigin} → first PDF bytes: ${timingsMs.doc_pdf_bytes} ms`);
    }
    if (timingsMs && timingsMs.docs_ready_publish != null) {
      timingParts.push(`${serverOrigin} → all documents ready: ${timingsMs.docs_ready_publish} ms`);
    }
    if (!complete && timingsMs && timingsMs.docs_progress_publish != null) {
      timingParts.push(`${serverOrigin} → first document visible: ${timingsMs.docs_progress_publish} ms`);
    }
    if (mfaStartTs != null) {
      const elapsedMs = Math.round(performance.now() - mfaStartTs);
      timingParts.push(`Browser MFA submit → docs rendered: ${elapsedMs} ms`);
    }
    document.getElementById("docs-latency").textContent = timingParts.join(" | ");

    for (const d of docs) {
      const url = `/api/docs/${sessionId}/${d.id}`;
      const li = document.createElement("li");
      const sizeKb = (d.size_bytes / 1024).toFixed(1);
      li.innerHTML = `
        <div class="doc-name">${escapeHtml(d.name)}</div>
        <embed src="${url}" type="${d.content_type || "application/pdf"}" />
        <div class="doc-meta">${sizeKb} KB &middot; ${escapeHtml(d.content_type || "application/pdf")}</div>
        <div class="doc-actions"><a href="${url}" download="${escapeHtml(d.name)}">Download</a></div>
      `;
      list.appendChild(li);
    }
    show("docs");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  async function showBoringFallback(carrier) {
    show("boring");
    const list = document.getElementById("boring-cache-list");
    const empty = document.getElementById("boring-cache-empty");
    const detail = document.getElementById("boring-detail");
    detail.textContent = `${carrier ? carrier.toUpperCase() : "This carrier"} is currently being driven by another browser. Here's what's cached for your browser; click "Try again" to retake the slot when it frees up.`;
    list.innerHTML = "";
    empty.classList.add("hidden");
    try {
      const r = await fetch("/api/cache", { credentials: "same-origin" });
      if (!r.ok) {
        empty.classList.remove("hidden");
        empty.textContent = "Couldn't load your cache. Try again in a moment.";
        return;
      }
      const data = await r.json();
      if (!data.results || data.results.length === 0) {
        empty.classList.remove("hidden");
        return;
      }
      for (const entry of data.results) {
        const li = document.createElement("li");
        const carrierName = entry.carrier;
        const savedDate = entry.saved_at
          ? new Date(entry.saved_at * 1000).toLocaleString()
          : "";
        const docsHtml = entry.docs
          .map(
            (d) =>
              `<li><a href="/api/docs/${entry.session_id}/${encodeURIComponent(d.id)}" target="_blank">${escapeHtml(d.name)}</a> <span class="cache-meta">(${(d.size_bytes / 1024).toFixed(1)} KB)</span></li>`
          )
          .join("");
        li.innerHTML = `
          <div class="cache-carrier">${escapeHtml(carrierName.toUpperCase())}</div>
          <div class="cache-meta">${entry.docs.length} cached document${entry.docs.length === 1 ? "" : "s"} &middot; ${escapeHtml(savedDate)}</div>
          <ul class="cache-docs">${docsHtml}</ul>
        `;
        list.appendChild(li);
      }
    } catch (e) {
      empty.classList.remove("hidden");
      empty.textContent = "Couldn't load your cache: " + e.message;
    }
  }

  async function loadDevCredentials() {
    try {
      const r = await fetch("/api/dev/credentials", { cache: "no-store" });
      if (!r.ok) return;
      const payload = await r.json();
      devCredentials = payload.credentials || {};
      applyDevCredentials();
    } catch (_) { /* noop */ }
  }

  function applyDevCredentials() {
    const form = document.getElementById("login-form");
    const carrier = form.elements.carrier.value;
    const creds = devCredentials[carrier];
    if (!creds) return;
    form.elements.username.value = creds.username || "";
    form.elements.password.value = creds.password || "";
  }

  document
    .getElementById("login-form")
    .elements.carrier.addEventListener("change", applyDevCredentials);

  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector("button[type=submit]");
    if (submitBtn.disabled) return;
    submitBtn.disabled = true;
    const data = Object.fromEntries(new FormData(e.target).entries());
    lastAttemptedCarrier = data.carrier;
    clearRepairPanel();
    show("waiting");
    setStatus("LOGGING_IN", "Submitting credentials");
    try {
      const { session_id } = await postJSON("/api/login", data);
      sessionId = session_id;
      listenForStatus(sessionId);
    } catch (err) {
      if (err.status === 423 && err.payload?.detail === "carrier-busy") {
        await showBoringFallback(err.payload.carrier || lastAttemptedCarrier);
      } else {
        showError(err.message);
      }
    } finally {
      submitBtn.disabled = false;
    }
  });

  document.getElementById("mfa-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector("button[type=submit]");
    if (submitBtn.disabled) return;
    submitBtn.disabled = true;
    const data = Object.fromEntries(new FormData(e.target).entries());
    mfaStartTs = performance.now();
    show("waiting");
    setStatus("AUTHENTICATING", "Submitting MFA code");
    try {
      await postJSON(`/api/mfa/${sessionId}`, data);
    } catch (err) {
      showError(err.message);
    } finally {
      submitBtn.disabled = false;
    }
  });

  document.getElementById("restart-btn").addEventListener("click", resetUI);
  document.getElementById("error-restart-btn").addEventListener("click", resetUI);
  document.getElementById("boring-retry-btn").addEventListener("click", resetUI);
  loadDevCredentials();
})();
