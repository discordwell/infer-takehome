(() => {
  const STATES = ["form", "waiting", "mfa", "docs", "error"];
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
    show("form");
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const j = await r.json();
        if (j.detail) msg = j.detail;
      } catch (_) { /* noop */ }
      throw new Error(msg);
    }
    return r.json();
  }

  function listenForStatus(id) {
    eventSource = new EventSource(`/api/status/${id}`);

    const handler = (evt) => {
      let payload;
      try { payload = JSON.parse(evt.data); } catch (e) { return; }
      const { state, detail, docs, error, timings_ms } = payload;
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
        if (eventSource) { eventSource.close(); eventSource = null; }
      }
    };

    eventSource.addEventListener("state_change", handler);
    eventSource.addEventListener("docs_ready", handler);
    eventSource.addEventListener("error", (e) => {
      // SSE 'error' event fires on connection error too — distinguish via data
      if (e.data) handler(e);
    });
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
    show("waiting");
    setStatus("LOGGING_IN", "Submitting credentials");
    try {
      const { session_id } = await postJSON("/api/login", data);
      sessionId = session_id;
      listenForStatus(sessionId);
    } catch (err) {
      showError(err.message);
    } finally {
      submitBtn.disabled = false;
    }
  });

  document.getElementById("mfa-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector("button[type=submit]");
    if (submitBtn.disabled) return; // double-click guard
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
  loadDevCredentials();
})();
