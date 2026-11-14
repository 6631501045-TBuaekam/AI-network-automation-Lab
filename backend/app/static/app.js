"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : "—");
const state = { planId: localStorage.getItem("planId"), plan: null, jobs: [], discovery: [], expanded: new Set(), cfgTab: null, file: null };

function toast(msg, err = false) {
  const d = document.createElement("div");
  d.textContent = msg; if (err) d.className = "err";
  $("#toast").appendChild(d); setTimeout(() => d.remove(), err ? 9000 : 4000);
}

async function api(method, path, body, admin = false) {
  const headers = {};
  if (body && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (admin) {
    const t = $("#adminToken").value.trim();
    if (!t) throw new Error("Enter the admin token (top right) first");
    headers["X-Admin-Token"] = t;
  }
  const r = await fetch(path, { method, headers, body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined });
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let d = data && data.detail !== undefined ? data.detail : data;
    if (d && d.errors) d = d.message + ": " + d.errors.map((e) => e.message).join("; ");
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return data;
}

async function busy(btn, fn) {
  btn.disabled = true; btn.classList.add("busy");
  try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.classList.remove("busy"); render(); }
}

/* ---------------- upload */
const drop = $("#drop");
["dragover", "dragenter"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("over")));
drop.addEventListener("drop", (ev) => { ev.preventDefault(); if (ev.dataTransfer.files[0]) pickFile(ev.dataTransfer.files[0]); });
$("#file").addEventListener("change", (ev) => ev.target.files[0] && pickFile(ev.target.files[0]));
function pickFile(f) {
  if (!/^image\/(png|jpeg)$/.test(f.type)) return toast("Only PNG or JPG images", true);
  state.file = f;
  $("#preview").innerHTML = `<img alt="diagram preview" src="${URL.createObjectURL(f)}">`;
  $("#btnUpload").disabled = false;
}
$("#btnUpload").onclick = () => busy($("#btnUpload"), async () => {
  const fd = new FormData(); fd.append("file", state.file); fd.append("requirements", $("#requirements").value);
  const p = await api("POST", "/api/diagrams", fd);
  setPlan(p); toast("Diagram uploaded - now click Generate");
});

/* ---------------- generate */
$("#btnGenerate").onclick = () => busy($("#btnGenerate"), async () => {
  $("#aiInfo").textContent = "AI is analysing the diagram…";
  setPlan(await api("POST", `/api/plans/${state.planId}/generate`));
  toast("AI analysis complete; interface discovery queued on Kafka");
});
$("#btnEditJson").onclick = () => {
  const ta = $("#topologyEdit"); ta.hidden = !ta.hidden; $("#btnSaveJson").hidden = ta.hidden;
  ta.value = JSON.stringify(state.plan.topology, null, 2);
};
$("#btnSaveJson").onclick = () => busy($("#btnSaveJson"), async () => {
  let t; try { t = JSON.parse($("#topologyEdit").value); } catch (e) { throw new Error("Invalid JSON: " + e.message); }
  setPlan(await api("PUT", `/api/plans/${state.planId}/topology`, { topology: t }, true));
  $("#topologyEdit").hidden = true; $("#btnSaveJson").hidden = true; toast("Topology updated - review again");
});
$("#btnDiscover").onclick = () => busy($("#btnDiscover"), async () => { await api("POST", "/api/discovery", {}); toast("Discovery jobs queued"); await refresh(); });

/* ---------------- review */
$("#btnReview").onclick = () => busy($("#btnReview"), async () => { setPlan(await api("POST", `/api/plans/${state.planId}/review`)); });
async function saveMapping(confirmed) {
  const map = {};
  document.querySelectorAll("select[data-router]").forEach((s) => { (map[s.dataset.router] ||= {})[s.dataset.net] = s.value; });
  setPlan(await api("PUT", `/api/plans/${state.planId}/interface-map`, { interface_map: map, confirmed }, true));
  toast(confirmed ? "Mapping confirmed and validated" : "Mapping saved");
}

/* ---------------- approve / deploy */
$("#approveConfirm").onchange = () => render();
$("#btnApprove").onclick = () => busy($("#btnApprove"), async () => {
  setPlan(await api("POST", `/api/plans/${state.planId}/approve`, { approver: $("#approver").value, confirm: $("#approveConfirm").checked }, true));
  toast("Configuration approved");
});
$("#btnDeploy").onclick = () => busy($("#btnDeploy"), async () => {
  if (!confirm("Deploy the APPROVED configuration to the routers via Kafka?")) return;
  const d = await api("POST", `/api/plans/${state.planId}/deploy`, null, true);
  toast(`Deployment ${d.id} queued: ${d.jobs.length} Kafka job(s)`); await refresh();
});

/* ---------------- state + polling */
function setPlan(p) { state.plan = p; state.planId = p.id; localStorage.setItem("planId", p.id); render(); }
async function refresh() {
  try {
    const [disc, jobs] = await Promise.all([api("GET", "/api/discovery"), state.planId ? api("GET", `/api/jobs?plan_id=${state.planId}`) : api("GET", "/api/jobs?limit=20")]);
    state.discovery = disc; state.jobs = jobs;
    if (state.planId) state.plan = await api("GET", `/api/plans/${state.planId}`);
  } catch (e) { if (/plan not found/.test(e.message)) { localStorage.removeItem("planId"); state.planId = null; } }
  render();
}
async function health() {
  try { const h = await api("GET", "/api/health"); $("#health").textContent = `API ok · AI: ${h.ai_provider} · topic: ${h.kafka_topic}${h.gns3 ? ` · GNS3: ${h.gns3.project}` : ""}`; $("#health").className = "pill ok"; }
  catch { $("#health").textContent = "API unreachable"; $("#health").className = "pill bad"; }
}
$("#kafkaUiLink").href = `${location.protocol}//${location.hostname}:8080/`;
$("#adminToken").value = sessionStorage.getItem("adminToken") || "";
$("#adminToken").oninput = (e) => sessionStorage.setItem("adminToken", e.target.value);

/* ---------------- rendering */
function render() {
  const p = state.plan, st = p?.status || "";
  $("#planBadge").textContent = p ? `${p.id} · ${st}` : "no plan";
  const order = ["upload", "generate", "review", "approve", "deploy", "verify"];
  const idx = { UPLOADED: 1, ANALYZED: 2, REVIEW_FAILED: 2, REVIEWED: 3, APPROVED: 4, DEPLOYING: 5, DEPLOY_FAILED: 5, DEPLOYED: 6 }[st] ?? 0;
  document.querySelectorAll("#rail div").forEach((d) => { const i = order.indexOf(d.dataset.step); d.className = i < idx ? "done" : i === idx ? "current" : ""; });

  if (p && !state.file && !$("#preview img")) $("#preview").innerHTML = `<img alt="diagram" src="/api/diagrams/${p.diagram_id}/image">`;
  $("#btnGenerate").disabled = !p || st === "DEPLOYING";
  $("#btnReview").disabled = !p?.topology || st === "DEPLOYING";
  const approvable = st === "REVIEWED" && p?.validation?.ok;
  $("#btnApprove").disabled = !approvable || !$("#approveConfirm").checked || !$("#approver").value.trim();
  $("#approver").oninput = () => render();
  $("#btnDeploy").disabled = !(p?.approval_hash && ["APPROVED", "DEPLOYED", "DEPLOY_FAILED"].includes(st));

  // AI
  if (p?.topology) {
    $("#aiInfo").textContent = `provider: ${p.ai_provider} · model: ${p.ai_model} · source: ${p.topology_source}`;
    $("#topologyJson").textContent = JSON.stringify(p.topology, null, 2);
    $("#jsonBox").open = true;
    const amb = p.topology.ambiguities || [];
    $("#ambiguities").innerHTML = amb.length ? amb.map((a) => `<div class="msg ${a.severity === "blocking" ? "error" : "warning"}"><b>${esc(a.severity)}</b> · ${esc(a.item)} - ${esc(a.issue)}</div>`).join("")
      : `<div class="msg ok">AI reported no ambiguities.</div>`;
  } else { $("#ambiguities").innerHTML = ""; $("#topologyJson").textContent = ""; }

  // discovery
  $("#discoveryTbl tbody").innerHTML = state.discovery.map((d) => {
    const s = d.last_job?.status || "—", r = d.result;
    return `<tr><td><b>${esc(d.device_id)}</b></td><td class="mono">${esc(d.host)}</td><td><span class="badge b-${esc(s)}">${esc(s)}</span>${d.last_job?.error ? `<div class="muted">${esc(d.last_job.error)}</div>` : ""}</td>
      <td class="mono">${esc(r?.mgmt_interface || "")} ${r?.mgmt_network ? `<span class="muted">${esc(r.mgmt_network)}</span>` : ""}</td><td class="mono">${esc((d.data_interfaces || []).join(", "))}</td></tr>`;
  }).join("");

  renderReview(p); renderApproval(p); renderJobs();
}

const LABELS = { addressing: "Addressing", subnet: "Subnet consistency", interface_mapping: "Interface mapping",
  ospf: "OSPF settings", management: "Management reachability", safety: "Safety / allow-list", ai_ambiguity: "AI ambiguities",
  gns3_lab: "GNS3 lab cross-check (read-only)" };
function renderReview(p) {
  const v = p?.validation;
  if (!v) { $("#mapping").innerHTML = ""; $("#validation").innerHTML = ""; $("#configs").innerHTML = ""; $("#reviewStatus").textContent = ""; return; }
  $("#reviewStatus").innerHTML = v.ok ? `<span class="badge b-pass">VALID</span> ready for approval` : `<span class="badge b-fail">${v.errors.length} ERROR(S)</span> fix before approval`;
  const routers = v.model?.routers || {}, imap = p.interface_map || {};
  const discBy = Object.fromEntries(state.discovery.map((d) => [d.device_id, d.data_interfaces || []]));
  let h = `<h3>Interface mapping <span class="muted">(real GNS3 interface names discovered on each router - confirm they match your cabling)</span></h3>
    <table class="tbl"><thead><tr><th>Router</th><th>Network</th><th>Role</th><th>IP</th><th>Interface</th></tr></thead><tbody>`;
  for (const [rid, r] of Object.entries(routers)) {
    const opts = discBy[r.inventory_id] || [];
    for (const i of r.interfaces) {
      const cur = imap[rid]?.[i.network] || "";
      const all = [...new Set([cur, ...opts].filter(Boolean))];
      h += `<tr><td><b>${esc(rid)}</b> <span class="muted">→ ${esc(r.inventory_id || "not in inventory")}</span></td><td class="mono">${esc(i.network)}</td>
        <td>${i.role === "lan" ? "LAN (passive)" : "R-R link (adjacency)"}</td><td class="mono">${esc(i.ip)}/${i.prefix}</td>
        <td><select data-router="${esc(rid)}" data-net="${esc(i.network)}">${all.map((o) => `<option ${o === cur ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></td></tr>`;
    }
  }
  const lab = v.gns3 ? `<div class="muted"><b>GNS3 project ${esc(v.gns3.summary?.project || "")}</b>${v.gns3.summary?.api ? ` · API ${esc(v.gns3.summary.api)}${v.gns3.summary.server_version ? ` (GNS3 ${esc(v.gns3.summary.server_version)})` : ""}` : ""} (read-only - suggests ports from the real cables; you still confirm)</div>${(v.gns3.notes || []).map((n) => `<div class="muted">• ${esc(n)}</div>`).join("")}` : "";
  h += `</tbody></table>${lab}${(v.mapping_notes || []).map((n) => `<div class="muted">• ${esc(n)}</div>`).join("")}
    <div class="actions"><span class="muted">Mapping: ${p.mapping_confirmed ? "<b>confirmed</b>" : "<b>NOT confirmed</b>"}</span>
    <button class="btn small" id="btnSaveMap">Save mapping</button><button class="btn small primary" id="btnConfirmMap">Confirm mapping &amp; re-validate (admin)</button></div>`;
  $("#mapping").innerHTML = h;
  $("#btnSaveMap").onclick = () => busy($("#btnSaveMap"), () => saveMapping(false));
  $("#btnConfirmMap").onclick = () => busy($("#btnConfirmMap"), () => saveMapping(true));

  $("#validation").innerHTML = `<h3>Validation</h3><div class="checks">${v.checks.map((c) => `<div class="check-card"><b>${esc(LABELS[c.name] || c.name)}<span class="badge b-${c.status}">${c.status.toUpperCase()}</span></b>
      ${c.errors.length || c.warnings.length ? `<ul>${c.errors.map((e) => `<li style="color:var(--bad)">${esc(e)}</li>`).join("")}${c.warnings.map((w) => `<li style="color:var(--warn)">${esc(w)}</li>`).join("")}</ul>` : `<span class="muted">all checks passed</span>`}</div>`).join("")}</div>`;

  const cfgs = p.configs || {}, names = Object.keys(cfgs);
  if (!names.length) { $("#configs").innerHTML = `<div class="msg warning">No configuration generated yet (resolve the errors above).</div>`; return; }
  if (!names.includes(state.cfgTab)) state.cfgTab = names[0];
  const c = cfgs[state.cfgTab];
  $("#configs").innerHTML = `<h3>Proposed Cisco IOS configuration</h3><div class="tabs">${names.map((n) => `<button class="btn small ${n === state.cfgTab ? "on" : ""}" data-tab="${esc(n)}">${esc(n)}</button>`).join("")}</div>
    <div class="muted">sha256 ${esc(c.sha256)}</div><pre class="code">${esc(c.text)}</pre>`;
  document.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => { state.cfgTab = b.dataset.tab; render(); }));
}

function renderApproval(p) {
  if (p?.approved_by) $("#approvalInfo").innerHTML = `<span class="badge b-pass">APPROVED</span> by <b>${esc(p.approved_by)}</b> at ${new Date(p.approved_at * 1000).toLocaleString()} · approval hash <span class="mono">${esc(p.approval_hash.slice(0, 16))}…</span>`;
  else $("#approvalInfo").textContent = p?.status === "REVIEWED" ? "Validation passed - waiting for administrator approval." : "Approval becomes available after a successful review.";
}

function renderJobs() {
  const jobs = state.jobs;
  const latestDep = jobs.find((j) => j.deployment_id)?.deployment_id;
  const depJobs = jobs.filter((j) => j.deployment_id === latestDep);
  if (latestDep) {
    const latestVer = depJobs.filter((j) => j.job_type === "verify");
    const tiles = [["Deployment", latestDep, depJobs.some((j) => j.status === "FAILED") ? "FAILED" : depJobs.every((j) => j.status === "SUCCESS") && latestVer.length ? "SUCCESS" : "RUNNING"]];
    for (const j of latestVer) {
      const r = j.result || {};
      const full = (r.neighbors || []).filter((n) => n.full).map((n) => n.neighbor_id);
      const routes = (r.ospf_routes || []).map((x) => x.prefix);
      tiles.push([`${j.device_id} OSPF`, `neighbors FULL: ${full.join(", ") || "none"}<br>OSPF routes: ${routes.join(", ") || "none"}`, j.status]);
    }
    $("#summary").innerHTML = `<div class="summary">${tiles.map(([t, b, s]) => `<div class="tile"><span class="muted">${esc(t)}</span><b><span class="badge b-${s}">${s}</span></b><div class="mono">${b}</div></div>`).join("")}</div>`;
    $("#deployInfo").textContent = `latest deployment ${latestDep}`;
  } else $("#summary").innerHTML = "";
  $("#jobsTbl tbody").innerHTML = jobs.map((j) => {
    const k = j.kafka_meta || {};
    let row = `<tr class="job" data-job="${esc(j.id)}"><td class="mono">${esc(j.id)}</td><td>${esc(j.job_type)}</td><td><b>${esc(j.device_id)}</b></td>
      <td><span class="badge b-${esc(j.status)}">${esc(j.status)}</span></td><td>${fmt(j.created_at)}</td><td>${fmt(j.started_at)}</td><td>${fmt(j.finished_at)}</td>
      <td class="mono">${k.topic ? `${esc(k.topic)} p${k.partition} @${k.offset}` : "—"}</td></tr>`;
    if (state.expanded.has(j.id)) row += `<tr class="detail"><td colspan="8">${jobDetail(j)}</td></tr>`;
    return row;
  }).join("") || `<tr><td colspan="8" class="muted">No jobs yet</td></tr>`;
  document.querySelectorAll("tr.job").forEach((tr) => (tr.onclick = () => { const id = tr.dataset.job; state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id); renderJobs(); }));
}

function jobDetail(j) {
  let h = j.error ? `<div class="msg error">${esc(j.error)}</div>` : "";
  h += `<div class="muted">worker ${esc(j.worker_id || "—")} · attempts ${j.attempts || 0}</div><ul class="events">${(j.events || []).map((e) => `<li class="${esc(e.level)}">${fmt(e.ts)}  ${esc(e.message)}</li>`).join("")}</ul>`;
  const r = j.result;
  if (j.job_type === "verify" && r?.checks) {
    h += `<table class="tbl vchecks"><thead><tr><th>Verification check</th><th>Result</th><th>Detail</th></tr></thead><tbody>${r.checks.map((c) => `<tr><td>${esc(c.name)}</td><td><span class="badge b-${c.ok ? "pass" : "fail"}">${c.ok ? "PASS" : "FAIL"}</span></td><td class="mono">${esc(c.detail)}</td></tr>`).join("")}
      ${(r.ping_checks || []).map((p) => `<tr><td>router ping ${esc(p.source)} → ${esc(p.target)} (${esc(p.host)}) - informational</td><td><span class="badge b-${p.success_rate > 0 ? "pass" : "warn"}">${p.success_rate ?? "?"}%</span></td><td></td></tr>`).join("")}</tbody></table>`;
  }
  if (j.output) h += `<details><summary>Device output</summary><pre class="code">${esc(j.output)}</pre></details>`;
  return h;
}

health(); refresh();
setInterval(refresh, 2500); setInterval(health, 15000);
