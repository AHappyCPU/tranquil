const state = {
  runs: [],
  selectedRunId: null,
  refreshTimer: null,
  refreshPending: false,
};

const runsBody = document.querySelector("#runs-body");
const runDetail = document.querySelector("#run-detail");
const fixtureButton = document.querySelector("#fixture-button");
const replayButton = document.querySelector("#replay-button");
const diffButton = document.querySelector("#diff-button");
const diffTarget = document.querySelector("#diff-target");
const statusFilter = document.querySelector("#status-filter");
const agentFilter = document.querySelector("#agent-filter");
const repoFilter = document.querySelector("#repo-filter");
const branchFilter = document.querySelector("#branch-filter");
const labelFilter = document.querySelector("#label-filter");
const evalsList = document.querySelector("#evals-list");

document.querySelector("#refresh-button").addEventListener("click", refresh);
statusFilter.addEventListener("change", refresh);
agentFilter.addEventListener("change", refresh);
repoFilter.addEventListener("input", debounceRefresh);
branchFilter.addEventListener("input", debounceRefresh);
labelFilter.addEventListener("input", debounceRefresh);
fixtureButton.addEventListener("click", saveFixture);
replayButton.addEventListener("click", replayRun);
diffButton.addEventListener("click", loadDiff);
runDetail.addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-path]");
  if (button) {
    openFile(button.dataset.openPath, button);
  }
});

async function fetchJSON(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

async function refresh() {
  if (state.refreshPending) {
    return;
  }
  state.refreshPending = true;
  const suffix = runFilterSuffix();
  try {
    const [stats, runs, signals, evals] = await Promise.all([
      fetchJSON("/api/stats"),
      fetchJSON(`/api/runs${suffix}`),
      fetchJSON("/api/signals"),
      fetchJSON("/api/evals?limit=8"),
    ]);
    renderStats(stats);
    state.runs = runs.runs;
    renderRuns();
    renderSignals(signals.signals);
    renderEvals(evals.eval_runs);
    if (state.selectedRunId) {
      await loadRun(state.selectedRunId, false);
    }
  } finally {
    state.refreshPending = false;
  }
}

function runFilterSuffix() {
  const params = new URLSearchParams();
  if (statusFilter.value) {
    params.set("status", statusFilter.value);
  }
  if (agentFilter.value) {
    params.set("agent", agentFilter.value);
  }
  if (repoFilter.value.trim()) {
    params.set("repo", repoFilter.value.trim());
  }
  if (branchFilter.value.trim()) {
    params.set("branch", branchFilter.value.trim());
  }
  if (labelFilter.value.trim()) {
    params.set("label", labelFilter.value.trim());
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

function debounceRefresh() {
  clearTimeout(state.filterTimer);
  state.filterTimer = setTimeout(refresh, 250);
}

function renderStats(stats) {
  document.querySelector("#stat-runs").textContent = stats.runs ?? 0;
  document.querySelector("#stat-live").textContent = stats.live ?? 0;
  document.querySelector("#stat-cost").textContent = money(stats.cost_usd_est ?? 0);
  document.querySelector("#stat-signals").textContent = stats.signaled ?? 0;
}

function renderRuns() {
  if (!state.runs.length) {
    runsBody.innerHTML = `<tr><td colspan="7" class="empty">Waiting for events</td></tr>`;
    return;
  }
  runsBody.innerHTML = state.runs.map((run) => `
    <tr data-run-id="${escapeHTML(run.run_id)}" class="${run.run_id === state.selectedRunId ? "selected" : ""}">
      <td><span class="agent-badge agent-${cssName(run.agent)}">${agentLabel(run.agent)}</span></td>
      <td><strong>${escapeHTML(run.repo || "unknown")}</strong><br><span class="muted">${escapeHTML(run.branch || run.run_id)}</span></td>
      <td><span class="status-badge status-${cssName(run.status)}">${escapeHTML(run.status)}</span></td>
      <td>${money(run.total_cost_usd_est || 0)}</td>
      <td>${spark(run.activity || [])}</td>
      <td>${run.signals_count ? `<span class="signal-badge">${run.signals_count}</span>` : `<span class="muted">none</span>`}</td>
      <td>${runNote(run)}</td>
    </tr>
  `).join("");
  for (const row of runsBody.querySelectorAll("tr[data-run-id]")) {
    row.addEventListener("click", () => loadRun(row.dataset.runId, true));
  }
  renderDiffOptions();
}

async function loadRun(runId, updateSelection) {
  const payload = await fetchJSON(`/api/runs/${encodeURIComponent(runId)}`);
  if (updateSelection) {
    state.selectedRunId = runId;
    renderRuns();
  }
  fixtureButton.disabled = false;
  replayButton.disabled = false;
  renderDiffOptions();
  renderRunDetail(payload.run, payload.events, payload.signals, payload.subagents || [], payload.files || [], payload.scores || []);
}

function renderRunDetail(run, events, signals, subagents, files, scores) {
  runDetail.innerHTML = `
    <div class="run-meta">
      <div><span>Status</span>${escapeHTML(run.status)}</div>
      <div><span>Cost est.</span>${money(run.total_cost_usd_est || 0)}</div>
      <div><span>Tools</span>${run.tool_calls || 0}</div>
      <div><span>Files</span>${run.files_touched || 0}</div>
      <div><span>Subagents</span>${run.subagents_count || 0}</div>
      <div><span>Max depth</span>${run.max_depth || 0}</div>
    </div>
    ${scores.length ? renderRunScores(scores) : ""}
    <div id="diff-result" class="diff-summary"></div>
    <div id="replay-result" class="replay-summary"></div>
    ${subagents.length ? renderSubagents(subagents) : ""}
    ${files.length ? renderFiles(files) : ""}
    ${signals.length ? `<p class="muted">${signals.length} active signal${signals.length === 1 ? "" : "s"} on this run.</p>` : ""}
    <div class="event-list">
      ${events.map(renderEvent).join("")}
    </div>
  `;
}

function renderRunScores(scores) {
  return `
    <section class="run-scores">
      <h3>Eval scores</h3>
      <div class="score-list">
        ${scores.slice(0, 12).map((score) => `
          <span class="score-chip ${score.passed ? "passed" : "failed"}" title="${escapeHTML(score.eval_run_id)}">
            ${escapeHTML(score.scorer)} · ${score.passed ? "pass" : "fail"}
          </span>
        `).join("")}
      </div>
    </section>
  `;
}

async function replayRun() {
  if (!state.selectedRunId) {
    return;
  }
  replayButton.disabled = true;
  replayButton.textContent = "Replaying";
  try {
    const payload = await fetchJSON(`/api/runs/${encodeURIComponent(state.selectedRunId)}/replay`, { method: "POST" });
    await refresh();
    renderReplay(payload);
  } catch (error) {
    renderReplayError(error);
  } finally {
    replayButton.textContent = "Replay";
    replayButton.disabled = false;
  }
}

function renderReplay(payload) {
  const target = document.querySelector("#replay-result");
  if (!target) {
    return;
  }
  const scores = payload.scores || [];
  const failed = scores.filter((score) => !score.passed).length;
  target.classList.add("active");
  target.innerHTML = `
    <strong>Replay ${failed ? "failed" : "passed"}</strong>
    <div class="muted">${escapeHTML(payload.eval_run_id || "")} · fixture ${escapeHTML((payload.fixture || {}).fixture_id || "")}</div>
    <div>${scores.length - failed} passed / ${failed} failed</div>
  `;
}

function renderReplayError(error) {
  const target = document.querySelector("#replay-result");
  if (!target) {
    return;
  }
  target.classList.add("active", "failed");
  target.innerHTML = `<strong>Replay unavailable</strong><div class="muted">${escapeHTML(error.message || error)}</div>`;
}

function renderSubagents(subagents) {
  return `
    <section class="subagents">
      <h3>Subagents</h3>
      ${subagents.map((subagent) => `
        <details class="subagent" ${subagent.status !== "completed" ? "open" : ""}>
          <summary>
            <strong>${escapeHTML(subagent.session_id)}</strong>
            <span>${escapeHTML(subagent.status)} · depth ${subagent.depth || 0} · ${money(subagent.cost_usd_est || 0)} est.</span>
          </summary>
          <div class="subagent-meta">
            <div><span>Parent</span>${escapeHTML(subagent.parent_session_id || "-")}</div>
            <div><span>Model</span>${escapeHTML(subagent.model || "-")}</div>
            <div><span>Events</span>${subagent.event_count || 0}</div>
            <div><span>Tools</span>${subagent.tool_calls || 0}</div>
          </div>
          ${subagent.latest_message ? `<p>${escapeHTML(subagent.latest_message)}</p>` : ""}
        </details>
      `).join("")}
    </section>
  `;
}

function renderFiles(files) {
  return `
    <section class="files-touched">
      <h3>Files</h3>
      ${files.map((file) => `
        <article class="file-touch ${file.reread_thrash ? "thrash" : ""}">
          <div class="file-head">
            <strong>${escapeHTML(file.path)}</strong>
            <button type="button" data-open-path="${escapeHTML(file.path)}">Open</button>
          </div>
          <div class="file-stats">
            <span>${file.reads || 0} read${file.reads === 1 ? "" : "s"}</span>
            <span>${file.writes || 0} write${file.writes === 1 ? "" : "s"}</span>
            <span>${file.events || 0} event${file.events === 1 ? "" : "s"}</span>
          </div>
          <div class="muted">${escapeHTML((file.tools || []).join(", ") || "-")}</div>
        </article>
      `).join("")}
    </section>
  `;
}

async function openFile(path, button) {
  if (!state.selectedRunId || !path) {
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Opening";
  try {
    await fetchJSON(`/api/runs/${encodeURIComponent(state.selectedRunId)}/open`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path}),
    });
    button.textContent = "Opened";
  } catch (error) {
    button.textContent = "Failed";
    console.error(error);
  } finally {
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 1400);
  }
}

function renderDiffOptions() {
  const others = state.runs.filter((run) => run.run_id !== state.selectedRunId);
  diffTarget.innerHTML = `<option value="">Diff against</option>${others.map((run) => `
    <option value="${escapeHTML(run.run_id)}">${escapeHTML(shortRunLabel(run))}</option>
  `).join("")}`;
  const disabled = !state.selectedRunId || !others.length;
  diffTarget.disabled = disabled;
  diffButton.disabled = disabled;
}

async function loadDiff() {
  const targetRunId = diffTarget.value;
  if (!state.selectedRunId || !targetRunId) {
    return;
  }
  diffButton.disabled = true;
  try {
    const payload = await fetchJSON(
      `/api/runs/${encodeURIComponent(targetRunId)}/diff/${encodeURIComponent(state.selectedRunId)}`
    );
    renderDiff(payload.diff);
  } finally {
    diffButton.disabled = false;
  }
}

function renderDiff(diff) {
  const target = document.querySelector("#diff-result");
  if (!target) {
    return;
  }
  const delta = diff.delta || {};
  target.classList.add("active");
  target.innerHTML = `
    <strong>Compared with ${escapeHTML(shortRunLabel(diff.a || {}))}</strong>
    <div class="diff-grid">
      <div><span>Cost delta</span>${signedMoney(delta.cost_usd_est || 0)}</div>
      <div><span>Tool delta</span>${signedNumber(delta.tool_calls || 0)}</div>
      <div><span>File delta</span>${signedNumber(delta.files_touched || 0)}</div>
      <div><span>Signal delta</span>${signedNumber(delta.signals_count || 0)}</div>
    </div>
  `;
}

function renderEvent(event) {
  const tool = event.tool || {};
  const usage = event.usage || {};
  const body = tool.input || event.message || event.raw || {};
  return `
    <article class="event">
      <div class="event-head">
        <strong>${escapeHTML(event.event_type)}</strong>
        <span>${escapeHTML(event.ts || "")}</span>
      </div>
      <div>${escapeHTML(tool.name || event.message || "")}</div>
      ${usage.cost_usd_est ? `<div class="muted">${money(usage.cost_usd_est)} est.</div>` : ""}
      <pre>${escapeHTML(JSON.stringify(body, null, 2))}</pre>
      ${event.diff ? renderInlineDiff(event.diff) : ""}
    </article>
  `;
}

function renderInlineDiff(diff) {
  return `
    <details class="inline-diff" open>
      <summary>${escapeHTML(diff.kind || "diff")}${diff.path ? ` · ${escapeHTML(diff.path)}` : ""}${diff.truncated ? " · truncated" : ""}</summary>
      <pre>${escapeHTML(diff.text || "")}</pre>
    </details>
  `;
}

function renderSignals(signals) {
  const target = document.querySelector("#signals-list");
  if (!signals.length) {
    target.textContent = "No active signals.";
    return;
  }
  target.innerHTML = signals.map((signal) => `
    <article class="signal-item ${cssName(signal.severity)}">
      <strong>${escapeHTML(signal.type)}</strong>
      <div class="muted">${escapeHTML(signal.severity)} · ${escapeHTML(signal.run_id)}</div>
      <pre>${escapeHTML(JSON.stringify(signal.evidence || {}, null, 2))}</pre>
    </article>
  `).join("");
}

function renderEvals(evalRuns) {
  if (!evalRuns.length) {
    evalsList.textContent = "No evals yet.";
    return;
  }
  evalsList.innerHTML = evalRuns.map((evalRun) => `
    <article class="eval-item ${cssName(evalRun.status)}">
      <strong>${escapeHTML(evalRun.suite)}</strong>
      <div class="muted">${escapeHTML(evalRun.status)} · ${escapeHTML(evalRun.eval_run_id)}</div>
      <div>${evalRun.passed_count || 0} passed / ${evalRun.failed_count || 0} failed</div>
    </article>
  `).join("");
}

function runNote(run) {
  const notes = [];
  if (run.subagents_count) {
    notes.push(`${run.subagents_count} sub${run.subagents_count === 1 ? "" : "s"}`);
  }
  if (run.produced_pr) {
    notes.push("shipped");
  }
  if (run.checks_ran) {
    notes.push("checked");
  }
  const labels = runLabelText(run.labels || {});
  if (labels) {
    notes.push(labels);
  }
  return notes.length ? escapeHTML(notes.join(" · ")) : `<span class="muted">-</span>`;
}

function runLabelText(labels) {
  const parts = [];
  for (const [key, values] of Object.entries(labels)) {
    const value = Array.isArray(values) && values.length ? values[0] : "";
    parts.push(value ? `${key}=${value}` : key);
    if (parts.length === 2) {
      break;
    }
  }
  return parts.join(", ");
}

async function saveFixture() {
  if (!state.selectedRunId) {
    return;
  }
  fixtureButton.disabled = true;
  try {
    const payload = await fetchJSON(`/api/fixtures/${encodeURIComponent(state.selectedRunId)}`, { method: "POST" });
    fixtureButton.textContent = payload.fixture.fixture_id ? "Saved" : "Save Fixture";
  } catch (error) {
    fixtureButton.textContent = "Failed";
    console.error(error);
  } finally {
    setTimeout(() => {
      fixtureButton.textContent = "Save Fixture";
      fixtureButton.disabled = false;
    }, 1200);
  }
}

function spark(values) {
  const max = Math.max(1, ...values);
  const bars = Array.from({ length: 12 }, (_, index) => values[index] || 0);
  return `<div class="spark" aria-hidden="true">${bars.map((value) => `<span style="height:${Math.max(2, Math.round((value / max) * 22))}px"></span>`).join("")}</div>`;
}

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function signedMoney(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? "+" : "-"}$${Math.abs(number).toFixed(2)}`;
}

function signedNumber(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? "+" : ""}${number}`;
}

function agentLabel(agent) {
  return agent === "codex" ? "CX" : "CC";
}

function cssName(value) {
  return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function shortRunLabel(run) {
  const branch = run.branch || run.run_id || "unknown";
  const repo = run.repo || "unknown";
  return `${repo} / ${branch}`.slice(0, 80);
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

refresh();
state.refreshTimer = setInterval(refresh, 5000);
connectLiveUpdates();

function connectLiveUpdates() {
  if (!("WebSocket" in window)) {
    return;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
  socket.addEventListener("open", () => {
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer);
      state.refreshTimer = null;
    }
  });
  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === "event") {
        refresh();
      }
    } catch (_error) {
      // Ignore malformed frames and keep the dashboard usable.
    }
  });
  socket.addEventListener("close", () => {
    if (!state.refreshTimer) {
      state.refreshTimer = setInterval(refresh, 5000);
    }
    setTimeout(connectLiveUpdates, 3000);
  });
  socket.addEventListener("error", () => {
    socket.close();
  });
}
