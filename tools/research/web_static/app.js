import {makeWorkspaces} from "./workspaces.js";
const NAV = [
  ["overview", "Overview"], ["jobs", "Jobs"], ["collaboration", "Collaboration"],
  ["models", "Models"], ["endpoints", "Endpoints"], ["capabilities", "Skills & Tools"], ["results", "Results"], ["activity", "Activity"],
];

const S = {
  bootstrap: null, state: null, route: {section: "overview"}, detail: null,
  detailKey: "", detailSignature: "", pollController: null, polling: false,
  loadingDetail: "", filterStates: {}, dirty: false, collabTab: "critique", filters: {}, connected: false,
  capabilities: {page: null, detail: null, instructions: null, loading: false, error: "",
    query: "", kind: "", availability: "", offset: 0, limit: 40, reload: false},
};

const $ = (selector, root = document) => root.querySelector(selector);
const view = $("#view");

function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "htmlFor") node.htmlFor = value;
    else if (key.startsWith("data-")) node.setAttribute(key, String(value));
    else if (key === "checked") node.checked = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, action, attrs = {}) {
  return h("button", {type: "button", "data-action": action, ...attrs}, label);
}

function fmtDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString([], {dateStyle: "medium", timeStyle: "medium"});
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return "—";
  const value=Math.max(0,Number(ms));
  if(value<1000)return `${Math.round(value)} ms`;
  if(value<60000)return `${(value/1000).toFixed(value<10000?1:0)} s`;
  const seconds=Math.round(value/1000);return `${Math.floor(seconds/60)}m ${seconds%60}s`;
}

function pretty(value) {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function titleize(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
}

function stateBadge(value) {
  const state = String(value || "unknown");
  return h("span", {class: `state ${state}`, text: state});
}

let fieldSerial=0;
function field(label, control, hint = "") {
  const id=`field-${++fieldSerial}`,labelNode=h("span",{id:`${id}-label`,text:label});
  control.id=control.id||id;
  control.setAttribute("aria-labelledby",`${id}-label`);
  const children=[labelNode,control];
  if(hint){const hintId=`${id}-hint`;control.setAttribute("aria-describedby",hintId);children.push(h("small",{id:hintId,class:"hint",text:hint}));}
  return h("label",{htmlFor:control.id},children);
}

function section(title, body, action = null) {
  return h("section", {class: "section"}, [
    h("div", {class: "section-head"}, [h("h2", {text: title}), action]), body,
  ]);
}

function routeFromHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(x=>{try{return decodeURIComponent(x);}catch{return x;}});
  const section = NAV.some(([key]) => key === parts[0]) ? parts[0] : "overview";
  if (section === "endpoints") return {section, database:null, id:parts[1]||null};
  if (section === "capabilities") return {section, database: null, id: parts[1] || null};
  return {section, database: parts[1] || null, id: parts[2] || null, session:parts[3]||null};
}

function recordHref(job) {
  const sectionName = job.kind === "collaboration" ? "collaboration" : "jobs";
  return `#/${sectionName}/${encodeURIComponent(job.database)}/${encodeURIComponent(job.id)}`;
}

function setNotice(message, kind = "") {
  const node = $("#notice");
  node.textContent = message || "";
  node.className = kind;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    headers.set("X-CSRF-Token", S.bootstrap?.csrf_token || "");
  }
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  let payload = null;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    if (response.status === 403 && options.body !== undefined) {
      await loadBootstrap();
      throw new Error(`${payload.error || "Authorization expired"}. Security token refreshed; review and retry.`);
    }
    throw new Error(payload.error || `${response.status} ${response.statusText}`);
  }
  return payload;
}

async function loadBootstrap() {
  S.bootstrap = await request("/api/bootstrap");
  renderNav();
}

function isRecordRoute() {return Boolean(S.route.id && ["jobs","results","collaboration"].includes(S.route.section));}

function stateSignatureForRoute() {
  if (!S.route.id || !S.state) return "";
  const row = S.state.jobs.find(x => x.database === S.route.database && x.id === S.route.id);
  return row ? [row.state, row.completed_at, row.cancel_requested, row.stop_reason, row.event_seq, row.progress?.stage?.key,row.progress?.usage?.output_tokens,row.progress?.current_call?.status, S.state.lifecycle?.current?.task_id===row.id?S.state.lifecycle.current.updated_at:""].join("|") : "missing";
}

async function poll({manual = false} = {}) {
  if (S.polling || (document.hidden && !manual)) return;
  S.polling = true;
  S.pollController?.abort();
  S.pollController = new AbortController();
  try {
    const next = await request("/api/state", {signal: S.pollController.signal});
    if (!S.connected) {await loadBootstrap();if(S.state)setNotice("Connection restored. Live state refreshed.");}
    const priorSignature = stateSignatureForRoute();
    S.state = next; S.connected = true;
    updateConnection(); renderNav();
    const nextSignature = stateSignatureForRoute();
    if (isRecordRoute() && (manual || !S.detail || priorSignature !== nextSignature)) await loadDetail(true);
    if(S.route.section==="models"&&S.route.id){await W.refreshMetadata();await W.refreshConversation();}
    if (!formIsActive()) render(); else refreshLivePanels();
  } catch (error) {
    if (error.name !== "AbortError") {
      S.connected = false; updateConnection();
      if (!S.state) renderError(`Command Center is disconnected: ${error.message}`);
      else setNotice(`Live state unavailable: ${error.message}`, "error");
    }
  } finally { S.polling = false; }
}

function formIsActive() {
  return Boolean(document.activeElement?.closest("form")) || S.dirty;
}

function updateConnection() {
  const dot = $("#connection-dot");
  dot.className = `status-dot ${S.connected ? "live" : "error"}`;
  $("#connection-label").textContent = S.connected ? "Live state" : "Disconnected";
  const mainWorker = S.state?.workers?.find(w=>w.database==="research");
  $("#worker-state").textContent = S.connected ? (mainWorker?.active ? "Worker online" : "Worker offline · jobs wait") : "Worker state unverified";
  $("#lease-state").textContent = S.connected ? (S.state?.lease?.held ? "Resource lease held" : "Resource lease free") : "Resources unverified";
  $("#observed-at").textContent = S.state?.observed_at ? `Observed ${fmtDate(S.state.observed_at)}` : "No observation yet";
}

function renderNav() {
  const counts = S.state?.counts || {};
  const countFor = key => key === "jobs" ? (counts.pending || 0) + (counts.running || 0)
    : key === "results" ? (counts.completed || 0) + (counts.failed || 0) + (counts.rejected || 0) + (counts.abstained || 0)
    : key === "models" ? (S.state?.models?.length || S.bootstrap?.models?.length || 0)
    : key === "capabilities" ? (S.capabilities.page?.summary?.total || "") : "";
  const nav = $("#primary-nav"); nav.replaceChildren();
  const mobile = $("#mobile-nav"); mobile.replaceChildren();
  for (const [key, label] of NAV) {
    const active = S.route.section === key;
    nav.append(h("a", {class: `nav-link${active ? " active" : ""}`, href: `#/${key}`}, [
      h("span", {text: label}), h("span", {class: "nav-count", text: countFor(key)}),
    ]));
    mobile.append(h("option", {value: key, selected: active ? "selected" : null, text: label}));
  }
  nav.append(h("a", {class:"nav-link",href:"#/jobs","data-action":"new-job",title:"Open the durable job submission form"},[h("span",{text:"New job"}),h("span",{class:"nav-count",text:"QUEUE"})]));
}

function renderHeader(title, eyebrow, meta = "") {
  $("#route-header").replaceChildren(h("div", {class: "route-heading"}, [
    h("div", {}, [h("h1", {text: title})]),
    h("div", {class: "route-meta", text: meta}),
  ]));
}

function renderError(message) {
  view.setAttribute("aria-busy", "false");
  view.replaceChildren(h("div", {class: "error-state", text: message}));
}

function render() {
  if (!S.bootstrap || !S.state) return;
  view.setAttribute("aria-busy", "false");
  const fn = {overview: renderOverview, jobs: renderJobs, collaboration: renderCollaboration,
    models: renderModels, endpoints: W.renderEndpoint, capabilities: renderCapabilities, results: renderResults, activity: renderActivity}[S.route.section];
  fn();
  restoreFilters();
}

function metric(label, value) { return h("div", {class: "metric"}, [h("strong", {text: value}), h("span", {text: label})]); }

function jobTable(rows, {compact = false} = {}) {
  if (!rows.length) return h("div", {class: "empty", text: "No records match this view."});
  const body = h("tbody");
  for (const row of rows) {
    body.append(h("tr", {"data-filter-text": [row.name,row.id,row.model_identifier,row.state,row.kind].join(" ").toLowerCase(), "data-state": row.state, "data-kind": row.kind, "data-model": row.model_identifier || "", "data-name":row.name||row.id, "data-created":row.created_at||"", "data-scheduled":row.scheduled_at||""}, [
      h("td", {}, [h("a", {href: recordHref(row), text: row.name || row.id}),row.name?h("small",{class:"row-id",text:row.id}):null,compact?null:h("small",{class:"row-created",text:`Created ${fmtDate(row.created_at)}`})]),
      h("td", {}, stateBadge(row.state)),
      h("td", {text: titleize(row.kind)}),
      h("td", {class: "mono", text: row.model_identifier || (row.kind === "collaboration" ? "Qwen + Bonsai" : "—")}),
      h("td", {class: "nowrap", text: fmtDate(row.scheduled_at)}),
      ...(compact ? [] : [h("td", {text: fmtDuration(row.duration_ms)}),h("td",{text:row.has_result?(row.kind==="collaboration"?"Accepted":"Response"):(row.kind==="collaboration"?"Evidence":"—")}), h("td", {class: "mono", text: row.database})]),
    ]));
  }
  return h("div", {class: "table-wrap",tabindex:"0",role:"region","aria-label":"Job records"}, h("table", {class: "data-table"}, [
    h("thead", {}, h("tr", {}, ["Name / ID","State","Kind","Model","Scheduled",...(compact ? [] : ["Duration","Artifact","Database"])].map(x => h("th", {text:x})))), body,
  ]));
}

function modelFor(identifier) {
  const configured=(S.state?.models||[]).find(m=>m.identifier===identifier);if(configured)return configured;
  const job=(S.state?.jobs||[]).find(j=>j.model_identifier===identifier&&j.endpoint);
  const endpoint=S.bootstrap?.endpoints?.find(e=>e.endpoint===job?.endpoint);
  return endpoint?{identifier,access:{workspace_url:`#/models/${endpoint.id}/${encodeURIComponent(identifier)}`,endpoint_workspace_url:`#/endpoints/${endpoint.id}`,endpoint_url:endpoint.endpoint}}:null;
}

function modelActions(model, {selected=false}={}) {
  if(!model?.access)return null;
  const access=model.access;
  return h("div",{class:"actions model-actions"},[
    !selected?h("a",{class:"button-link",href:access.workspace_url||access.details_url,
      title:"Open a conversation with this exact selected model",text:"Open Model"}):null,
    access.endpoint_url?h("a",{class:"button-link quiet",href:access.endpoint_workspace_url,title:"Open configured endpoint health, discovery and model interaction",text:"Open Endpoint"}):null,
  ]);
}

function counter(value) {return value===null||value===undefined?"Unavailable":Number(value).toLocaleString();}
function generationInfo(progress, {compact=false}={}) {
  if(!progress)return h("p",{class:"hint",text:"Generation evidence unavailable."});
  const call=progress.current_call,latest=call||progress.calls?.at(-1);
  const usage=progress.usage||latest?.usage||{};
  const inference=progress.inference_elapsed_ms??latest?.inference_elapsed_ms;
  const worker=S.state?.workers?.find(w=>w.database==="research")?.active;
  const sessionId=progress.session_id||call?.session_id;
  const ownSession=sessionId&&S.state?.lifecycle?.current?.id===sessionId;
  const animated=progress.indeterminate&&progress.state!=="pending"&&S.connected&&worker&&ownSession
    &&["STARTING","LOADING","READY","BUSY","UNLOADING","STOPPING"].includes(S.state.lifecycle.current.state);
  const stage=progress.stage?.label||"Unknown";
  const rate=usage.output_tokens_per_second;
  const budget=progress.output_budget;
  const source=usage.status==="provider_reported"?
    `Input: ${usage.input_source?.replaceAll("_","-")||"unavailable"}. Output: ${usage.output_source?.replaceAll("_","-")||"unavailable"}. Total: ${usage.total_source==="provider_reported"?"provider-reported":usage.total_source?"calculated sum of provider counts":"unavailable"}.`:
    progress.terminal?"No valid provider token usage was persisted for this call. No text-based token estimates.":
    "Token counts unavailable during this non-streaming call; shown after a response is persisted. No text-based token estimates.";
  return h("div",{class:`generation-info${compact?" compact":""}`,"data-stage":progress.stage?.key||"unknown"},[
    h("div",{class:"generation-heading"},[h("strong",{text:stage}),h("span",{class:"mono muted",text:`Job ${fmtDuration(progress.elapsed_ms)} · Inference ${fmtDuration(inference)}`})]),
    progress.indeterminate?h("div",{class:`progress-track${animated?" indeterminate":""}`,role:"progressbar","aria-label":`Recorded stage: ${stage}`,"aria-valuetext":`${stage}; completion amount unknown`},h("span",{class:"progress-motion"})):null,
    h("dl",{class:"generation-metrics"},[
      ...[["Output tokens",counter(usage.output_tokens)],["Input",counter(usage.input_tokens)],["Total",counter(usage.total_tokens)],["Average output rate",Number.isFinite(rate)?`${rate.toFixed(1)} tokens/s`:"Unavailable"]].flatMap(([a,b])=>[h("dt",{text:a}),h("dd",{text:b})]),
    ]),
    !compact?h("p",{class:"mono muted",text:`${progress.terminal?"Last call model":"Model"} · ${call?.model||progress.model||latest?.model||"Not yet recorded"}`}):null,
    h("p",{class:"hint",text:source}),
    progress.cancel_requested&&!progress.terminal?h("p",{class:"hint",text:"Cancellation requested. The bounded model call and lifecycle cleanup must finish before the worker stops this workflow."}):null,
    Number.isFinite(rate)?h("p",{class:"hint",text:usage.throughput_source==="provider_eval_duration"?"Rate calculated over Ollama's reported generation duration; not instantaneous throughput.":"Rate calculated over the measured inference request interval, including prefill; not instantaneous throughput."}):null,
    budget?.status==="available"?h("div",{class:"budget-progress"},[
      h("label",{},[h("span",{text:`Output budget used · ${budget.used} / ${budget.limit} tokens`}),h("progress",{max:budget.limit,value:Math.min(budget.used,budget.limit),"aria-label":"Output budget used"})]),
      h("p",{class:"hint",text:"Budget consumption measures tokens generated, not percentage of task completion."}),
    ]):null,
    progress.kind==="collaboration"?h("p",{class:"hint",text:`Calculated sum of persisted role-response counts: ${counter(progress.usage_totals?.output_tokens)} output tokens across ${progress.usage_totals?.calls_with_usage||0} calls with usage. Counts exclude readiness requests.`}):null,
    !compact?h("p",{class:"hint",text:`Progress observed ${fmtDate(progress.as_of)}. ${progress.state==="running"&&!worker?"Worker absent; recorded stage may be stale.":""}`}):null,
  ]);
}

function runningJobList(rows) {
  const wrapper=h("div",{"data-active-jobs":"true",class:"active-jobs"});
  if(!rows.length){wrapper.append(h("p",{class:"hint",text:"No running jobs."}));return wrapper;}
  for(const job of rows){
    const progress=job.progress,identifier=progress?.current_call?.model||progress?.model;
    const model=modelFor(identifier||job.model_identifier);
    wrapper.append(h("article",{class:"active-job"},[
      h("div",{class:"record-head"},[h("div",{},[h("h3",{text:job.name||job.id}),h("p",{class:"hint mono",text:identifier||"Active model not yet recorded"})]),
        h("div",{class:"actions"},[modelActions(model),h("a",{class:"button-link",href:recordHref(job),text:"View Details"})])]),
      generationInfo(progress,{compact:true}),
    ]));
  }
  return wrapper;
}

function refreshLivePanels() {
  const panel=view.querySelector("[data-active-jobs]");
  if(panel)panel.replaceWith(runningJobList(S.state.jobs.filter(j=>j.state==="running"||j.lifecycle_active)));
}

function renderOverview() {
  renderHeader("Operational overview", "Command Center", S.state.observed_at ? `State ${fmtDate(S.state.observed_at)}` : "");
  const c = S.state.counts;
  const active = S.state.jobs.filter(x => x.state === "running"||x.lifecycle_active);
  const pending = S.state.jobs.filter(x => x.state === "pending").sort((a,b)=>a.scheduled_at.localeCompare(b.scheduled_at)).slice(0, 8);
  const recentCollabs = S.state.jobs.filter(x => x.kind === "collaboration").slice(0, 6);
  const activity = S.state.activity.slice(0, 8);
  view.replaceChildren(
    h("div", {class:"metrics"}, [metric("Pending",c.pending||0),metric("Running",c.running||0),metric("Completed",c.completed||0),metric("Needs attention",(c.failed||0)+(c.interrupted||0)+(c.rejected||0))]),
    h("div", {class:"grid-2"}, [
      h("div", {}, [section("Current work", runningJobList(active), h("a", {href:"#/jobs",class:"section-note",text:"Open jobs"})), section("Next in queue", jobTable(pending,{compact:true}))]),
      h("div", {}, [section("Resource coordination", resourcePanel()), section("Recent activity", activityTimeline(activity))]),
    ]),
    section("Recent collaborations", jobTable(recentCollabs,{compact:true}), h("a", {href:"#/collaboration",class:"section-note",text:"Open workspace"})),
  );
}

function resourcePanel() {
  const workers = S.state.workers || [];
  return h("div", {class:"panel"}, [
    h("dl", {class:"keyvals"}, [
      h("dt", {text:"Heavy-model lease"}), h("dd", {text:S.state.lease?.held ? "Held" : "Free"}),
      h("dt", {text:"Lifecycle blocker"}), h("dd", {text:S.state.lease?.blocker || S.state.lifecycle?.blocker || "None recorded"}),
      ...workers.flatMap(w => [h("dt", {text:`${w.database} worker`}), h("dd", {}, stateBadge(w.active ? "running" : "stopped"))]),
    ]),
  ]);
}

function activityTimeline(rows) {
  if (!rows.length) return h("div", {class:"empty",text:"No persisted activity yet."});
  return h("ol", {class:"timeline"}, rows.map(row => h("li", {}, [
    h("strong", {text:row.title}), h("time", {dateTime:row.at,text:fmtDate(row.at)}),
    row.detail ? h("p", {text:row.detail}) : null,
    row.record_id ? h("a", {class:"section-note",href:recordHref({database:row.database,id:row.record_id,kind:row.kind}),text:"Open record"}) : null,
  ])));
}

function filtersToolbar({results = false} = {}) {
  const modelOptions=[...new Set((S.state.models||[]).map(x=>x.identifier).filter(Boolean))];
  return h("div", {class:"toolbar", "data-filter-scope":results ? "results" : "jobs"}, [
    field("Search", h("input", {type:"search",name:"search",placeholder:"Name, ID, model…",value:S.filters.search||""})),
    field("State", h("select", {name:"state"}, [h("option", {value:"",text:"All states"}), ...["pending","running","completed","failed","cancelled","abstained","interrupted","rejected"].map(x=>h("option",{value:x,text:titleize(x)}))])),
    field("Kind", h("select", {name:"kind"}, [h("option",{value:"",text:"All kinds"}),h("option",{value:"research",text:"Research"}),h("option",{value:"collaboration",text:"Collaboration"})])),
    field("Model", h("select", {name:"model"}, [h("option",{value:"",text:"All models"}),...modelOptions.map(x=>h("option",{value:x,text:x}))])),
    field("Sort", h("select", {name:"sort"}, [h("option",{value:"scheduled",text:"Scheduled"}),h("option",{value:"created",text:"Created"}),h("option",{value:"name",text:"Name"})])),
  ]);
}

function filterRows(scope) {
  const toolbar = $(`[data-filter-scope="${scope}"]`);
  if (!toolbar) return;
  const search = toolbar.elements?.search?.value?.trim().toLowerCase() || $("[name=search]",toolbar)?.value.trim().toLowerCase() || "";
  const state = $("[name=state]",toolbar)?.value || "", kind = $("[name=kind]",toolbar)?.value || "", model=$("[name=model]",toolbar)?.value||"";
  for (const row of view.querySelectorAll("tbody tr[data-filter-text]")) {
    row.hidden = Boolean((search && !row.dataset.filterText.includes(search)) || (state && row.dataset.state !== state) || (kind && row.dataset.kind !== kind) || (model&&row.dataset.model!==model));
  }
  const sort=$("[name=sort]",toolbar)?.value||"scheduled",tbody=view.querySelector("tbody");
  S.filterStates[scope]={search,state,kind,model,sort};
  if(tbody){let empty=toolbar.parentNode.querySelector(".filter-empty");if(!empty){empty=h("p",{class:"filter-empty hint",text:"No records match these filters."});toolbar.parentNode.append(empty);}empty.hidden=[...tbody.children].some(row=>!row.hidden);}
  if(tbody)[...tbody.children].sort((a,b)=>(sort==="created"?-1:1)*String(a.dataset[sort]||"").localeCompare(String(b.dataset[sort]||""))).forEach(row=>tbody.append(row));
}

function restoreFilters() {
  const toolbar=view.querySelector("[data-filter-scope]"); if(!toolbar)return;
  const saved=S.filterStates[toolbar.dataset.filterScope]||{};
  for(const [key,value] of Object.entries(saved)){const control=toolbar.querySelector(`[name="${key}"]`);if(control)control.value=value;}
  filterRows(toolbar.dataset.filterScope);
}

function renderJobs() {
  if (S.route.id) return renderRecord("research");
  renderHeader("Jobs", "Durable queue", `${S.state.jobs.filter(x=>x.kind==="research").length} records loaded`);
  const jobs = S.state.jobs.filter(x => x.kind === "research");
  view.replaceChildren(h("div", {class:"grid-2"}, [
    h("div",{},[section("Running jobs",runningJobList(S.state.jobs.filter(j=>j.state==="running"||j.lifecycle_active))),section("Queue and history", h("div", {}, [filtersToolbar(), jobTable(jobs)]))]),
    section("New job", newJobForm()),
  ]));
}

function newJobForm() {
  const form = h("form", {id:"job-form"});
  const models = S.bootstrap.models || [];
  form.append(
    field("Model", h("select", {name:"model_identifier",required:"required"}, models.map(m=>h("option",{value:m.identifier,text:`${m.family} — ${m.identifier}`})))),
    field("Name (optional)", h("input", {name:"name",maxlength:"80",autocomplete:"off"})),
    field("Prompt", h("textarea", {name:"prompt",class:"prompt-input",maxlength:"32000",required:"required",placeholder:"Describe the research task…"})),
    field("Run time (local)", h("input", {name:"scheduled_at",type:"datetime-local"}), "Blank schedules immediately. The browser converts this local time to an ISO timestamp."),
    h("div", {class:"field-row"}, [field("Max output tokens",h("input",{name:"max_tokens",type:"number",min:"1",placeholder:"Model default"})),field("Timeout seconds",h("input",{name:"timeout_seconds",type:"number",min:"1",step:"1",placeholder:"Model default"}))]),
    h("p", {class:"hint",text:"Submission writes only to the durable research queue. The independent worker controls all model loading and inference."}),
    h("div", {class:"form-error",id:"job-form-error",role:"status"}),
    h("div", {class:"form-actions"}, [h("button",{class:"primary",type:"submit",text:"Queue job"}),button("Clear","clear-job",{class:"quiet"})]),
  );
  return h("div", {class:"panel"}, form);
}

async function loadDetail(force = false) {
  if (!isRecordRoute()) return;
  const key = `${S.route.section}/${S.route.database}/${S.route.id}`;
  if (S.loadingDetail === key || (!force && S.detailKey === key && S.detail)) return;
  S.loadingDetail = key;
  S.detailKey = key; S.detail = null;
  if (!formIsActive()) render();
  try {
    const path = S.route.section === "collaboration"
      ? `/api/collaborations/${encodeURIComponent(S.route.database)}/${encodeURIComponent(S.route.id)}`
      : `/api/jobs/${encodeURIComponent(S.route.database)}/${encodeURIComponent(S.route.id)}`;
    const data = await request(path);
    if (S.detailKey === key) { S.detail = data; render(); }
  } catch (error) { if (S.detailKey === key) renderError(error.message); }
  finally { if (S.loadingDetail === key) S.loadingDetail = ""; }
}

function renderRecord(kind) {
  const summary = S.state.jobs.find(x=>x.id===S.route.id&&x.database===S.route.database);
  renderHeader(summary?.name || "Record", kind === "research" ? "Research job" : "Result", `${S.route.database} / ${S.route.id}`);
  if (!S.detail || S.detailKey !== `${S.route.section}/${S.route.database}/${S.route.id}`) {
    view.replaceChildren(h("div",{class:"loading",text:"Loading record…"})); loadDetail(); return;
  }
  const job = S.detail.job;
  const canCancel = Boolean(summary?.can_cancel && S.bootstrap.databases.find(x=>x.key===S.route.database)?.writable);
  const actions = h("div",{class:"actions"},[
    h("a",{href:`/api/export/research/${encodeURIComponent(S.route.database)}/${encodeURIComponent(job.id)}`,download:"",class:"button-link",text:"Download JSON"}),
    job.result ? button("Copy result","copy",{"data-copy":job.result}) : null,
    canCancel ? button("Cancel","cancel-job",{class:"danger","data-db":S.route.database,"data-id":job.id}) : null,
    modelActions(modelFor(job.model_identifier)),
    button("Refresh","refresh-detail",{class:"quiet"}),
  ]);
  view.replaceChildren(h("div",{class:"record-head"},[h("div",{},[h("h2",{class:"record-title",text:job.name||job.id}),stateBadge(job.state)]),actions]),
    h("div",{class:"grid-2"},[
      h("div",{},[
        section("Generation",generationInfo(summary?.progress||S.detail.progress)),
        section("Prompt",h("div",{class:"panel prose",text:job.prompt||"—"})),
        job.result!==null&&job.result!==undefined?section("Result",h("div",{class:"panel prose",text:job.result})):null,
        job.error?section("Failure",h("div",{class:"panel prose",text:job.error})):null,
      ]),
      h("div",{},[section("Record",h("div",{class:"panel"},h("dl",{class:"keyvals"},[
        ...[["ID",job.id],["Database",S.detail.database],["Model",job.model_identifier],["Endpoint",job.endpoint],["Created",fmtDate(job.created_at)],["Scheduled",fmtDate(job.scheduled_at)],["Started",fmtDate(job.started_at)],["Completed",fmtDate(job.completed_at)],["Lifecycle cleanup",job.lifecycle_error || (S.detail.lifecycle?.state==="STOPPED" && S.detail.lifecycle?.last_verified_unload ? "Verified unload" : "Not verified for this job")]].flatMap(([a,b])=>[h("dt",{text:a}),h("dd",{text:b})])
      ]))), S.detail.lifecycle?section("Lifecycle session",technicalDetails(S.detail.lifecycle)):null, S.detail.stage_history?.length?section("Recorded stages",stageHistory(S.detail.stage_history)):null]),
    ]),
    job.raw_response_json?section("Raw transport",h("details",{},[h("summary",{text:"Show retained response JSON"}),h("pre",{class:"code-block",text:job.raw_response_json})])):null);
}

function technicalDetails(value) { return h("details",{},[h("summary",{text:"Technical details"}),h("pre",{class:"code-block",text:pretty(value)})]); }

function renderCollaboration() {
  if (S.route.id) return renderCollaborationDetail();
  renderHeader("Collaboration", "Measured Qwen + Bonsai workflow", "Persisted evidence, deterministic decisions");
  const rows = S.state.jobs.filter(x=>x.kind==="collaboration");
  view.replaceChildren(h("div",{class:"grid-2"},[
    section("Experiments",jobTable(rows)), section("New experiment",collaborationForm()),
  ]));
}

function collaborationForm() {
  const form=h("form",{id:"collaboration-form",novalidate:"novalidate"});
  const examples=S.bootstrap.examples||[];
  form.append(
    field("Example",h("select",{name:"example"},[h("option",{value:"",text:"Start blank"}),...examples.map(x=>h("option",{value:x.id,text:x.name}))])),
    field("Experiment name",h("input",{name:"name",required:"required",maxlength:"120"})),
    field("Instruction",h("textarea",{name:"instruction",required:"required",maxlength:"16000",placeholder:"Describe the bounded editorial goal…"})),
    field("Source",h("textarea",{name:"source",required:"required",placeholder:"Source material or JSON object…"})),
    field("Original",h("textarea",{name:"original",required:"required",placeholder:"Artifact to improve…"})),
    h("div",{class:"field-row"},[
      field("Artifact format",h("select",{name:"format"},[h("option",{value:"text",text:"Text"}),h("option",{value:"json",text:"JSON object"})])),
      field("Run time (local)",h("input",{name:"scheduled_at",type:"datetime-local"})),
    ]),
    field("Semantic verification",h("select",{name:"semantic"},[h("option",{value:"true",text:"Required — abstain without independent verification"}),h("option",{value:"false",text:"Deterministic constraints only"})])),
    h("details",{},[h("summary",{text:"Advanced specification"}),h("div",{class:"panel"},[
      h("div",{class:"field-row three"},[
        field("Rounds",h("input",{name:"rounds",type:"number",min:"1",max:"4",value:"2"})),field("Calls",h("input",{name:"calls",type:"number",min:"1",max:"48",value:"20"})),field("Runtime seconds",h("input",{name:"runtime",type:"number",min:"1",max:"3600",value:"900"})),
        field("Output chars",h("input",{name:"output_chars",type:"number",min:"1",value:"12000"})),field("Max tokens",h("input",{name:"max_tokens",type:"number",min:"1",max:"4096",value:"1024"})),field("Schema retries",h("input",{name:"retries",type:"number",min:"0",max:"2",value:"0"})),
      ]),
      field("Constraints JSON",h("textarea",{name:"constraints",placeholder:'{"required_content": ["…"]}'})),field("Metrics JSON",h("textarea",{name:"metrics",placeholder:'{"target_words": 120}'})),
      field("Full specification JSON (optional override)",h("textarea",{name:"full_spec",placeholder:"When present, this complete JSON object replaces the fields above."})),
    ])]),
    h("p",{class:"hint",text:"Model review is evidence of editorial feedback, not proof of correctness. Required semantic verification causes abstention: this system has no independent semantic verifier."}),
    h("div",{class:"form-error",id:"collaboration-form-error",role:"status"}),
    h("div",{class:"form-actions"},[h("button",{class:"primary",type:"submit",text:"Schedule experiment"}),button("Clear","clear-collaboration",{class:"quiet"})]),
  );
  return h("div",{class:"panel"},form);
}

function artifactText(value) { return typeof value === "string" ? value : JSON.stringify(value,null,2); }

function stageHistory(events) {
  return h("ol",{class:"stage-history"},events.map(event=>h("li",{},[
    h("strong",{text:event.to_state}),h("time",{dateTime:event.at,text:fmtDate(event.at)}),
  ])));
}

function stageStrip(data) {
  const progress=S.state.jobs.find(j=>j.database===data.database&&j.id===data.run.id)?.progress||data.progress;
  if(!progress)return h("p",{class:"hint",text:"Stage evidence unavailable."});
  return h("div",{},[
    h("p",{class:"hint",text:`Bounded adaptive path · up to ${progress.plan.max_rounds} revision rounds and ${progress.plan.max_role_calls} role calls. Upcoming stages are conditional; the workflow may stop early.`}),
    h("div",{class:"stage-list"},progress.stages.map(stage=>h("div",{class:`stage stage-${stage.status}`},[
      h("strong",{text:stage.label}),h("small",{text:`${stage.role}${stage.round!==null?` · round ${stage.round}`:""}`}),h("span",{class:"stage-status",text:titleize(stage.status)}),
    ]))),
    h("p",{class:"hint",text:`${progress.stages.filter(s=>s.status==="completed").length} recorded stages completed · ${progress.stages.filter(s=>s.status==="upcoming").length} conditional stages remaining. No overall completion percentage is inferred.`}),
  ]);
}

function boundedDiff(leftArtifact,rightArtifact) {
  const leftText=artifactText(leftArtifact),rightText=artifactText(rightArtifact);
  const a=leftText.split("\n"),b=rightText.split("\n");
  if(a.length===1&&b.length===1){
    const x=leftText.match(/\s+|[^\s]+/g)||[],y=rightText.match(/\s+|[^\s]+/g)||[];
    if(x.length*y.length<=250000){
      const dp=Array.from({length:x.length+1},()=>new Uint16Array(y.length+1));
      for(let i=x.length-1;i>=0;i--)for(let j=y.length-1;j>=0;j--)dp[i][j]=x[i]===y[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
      const left=[],right=[];let i=0,j=0;
      while(i<x.length||j<y.length){if(i<x.length&&j<y.length&&x[i]===y[j]){left.push({text:x[i],kind:""});right.push({text:y[j],kind:""});i++;j++;}else if(j<y.length&&(i===x.length||dp[i][j+1]>=dp[i+1][j]))right.push({text:y[j++],kind:"changed"});else left.push({text:x[i++],kind:"removed"});}
      return {left,right,bounded:false,tokens:true};
    }
  }
  if(a.length*b.length>120000||a.length>500||b.length>500) return {left:a.map((text,i)=>({text,kind:a[i]===b[i]?"":"removed"})),right:b.map((text,i)=>({text,kind:a[i]===b[i]?"":"changed"})),bounded:true};
  const dp=Array.from({length:a.length+1},()=>new Uint16Array(b.length+1));
  for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
  const leftLines=[],rightLines=[];let i=0,j=0;
  while(i<a.length||j<b.length){
    if(i<a.length&&j<b.length&&a[i]===b[j]){leftLines.push({text:a[i],kind:""});rightLines.push({text:b[j],kind:""});i++;j++;}
    else if(j<b.length&&(i===a.length||dp[i][j+1]>=dp[i+1][j])){rightLines.push({text:b[j++],kind:"changed"});}
    else{leftLines.push({text:a[i++],kind:"removed"});}
  }
  return {left:leftLines,right:rightLines,bounded:false};
}

function diffPane(label,lines,tokens=false) { return h("div",{class:"compare-pane"},[h("div",{class:"compare-label",text:label}),h("pre",{class:"diff-body"},lines.map(x=>h("span",{class:`${tokens?"diff-token":"diff-line"} ${x.kind}`,text:x.text||" "}))) ]); }

function comparisonWorkspace(data) {
  const revs=data.revisions;if(!revs.length)return h("div",{class:"empty",text:"No revisions persisted."});
  const leftId=S.filters.compareLeft||revs[0].id,rightId=S.filters.compareRight||data.run.accepted_revision_id||data.run.outcome?.best_revision_id||revs[Math.min(1,revs.length-1)].id;
  const left=revs.find(x=>x.id===leftId)||revs[0],right=revs.find(x=>x.id===rightId)||revs.at(-1);const diff=boundedDiff(left.artifact,right.artifact);
  const revisionLabel=x=>{const decision=data.events.find(e=>e.kind==="decision"&&e.data.revision_id===x.id);return titleize(x.label)+(data.run.accepted_revision_id===x.id?" · accepted":data.run.outcome?.best_revision_id===x.id?" · best retained, not accepted":decision?.data.kept_as_incumbent===false?" · not retained":"");};
  const opts=selected=>revs.map(x=>h("option",{value:x.id,selected:x.id===selected?"selected":null,text:revisionLabel(x)}));
  return h("div",{},[
    h("div",{class:"comparison-controls"},[field("Left revision",h("select",{name:"compare-left","data-action":"compare-select"},opts(left.id))),field("Right revision",h("select",{name:"compare-right","data-action":"compare-select"},opts(right.id))),button("Copy right","copy",{"data-copy":artifactText(right.artifact)})]),
    h("p",{class:"diff-legend",text:"Removed text is red; added text is amber. Unmarked text is unchanged."}),
    diff.bounded?h("p",{class:"hint",text:"Large output: bounded positional highlighting is shown instead of full LCS."}):null,
    h("div",{class:"compare-grid"},[diffPane(left.label,diff.left,diff.tokens),diffPane(right.label,diff.right,diff.tokens)]),
  ]);
}

function evidenceTabs(data) {
  const types={critique:["review","evaluation"],validation:["validation","decision","finished"],provenance:["call_started","call_finished","parsed_output","schema_rejected","recovery"]};
  const events=data.events.filter(x=>types[S.collabTab].includes(x.kind));
  const labelFor=id=>data.revisions.find(r=>r.id===id)?.label||"Candidate";
  const list=(title,values)=>values?.length?h("div",{},[h("h4",{text:title}),h("ul",{class:"evidence-list"},values.map(text=>h("li",{text}))) ]):null;
  const content=events.map(event=>{
    const value=event.data,heading=h("div",{class:"evidence-head"},[h("h3",{text:`${titleize(event.kind)} · ${labelFor(value.revision_id)}`}),h("time",{text:fmtDate(event.at)})]);
    let body;
    if(event.kind==="review"){
      const r=value.review;
      body=[h("p",{class:"review-verdict",text:`Bonsai recommendation: ${titleize(r.verdict)}`}),list("Reviewer comments",r.comments),list("Suggested changes",r.suggestions),list("Reported unsupported changes",r.unsupported_changes),h("p",{class:"hint",text:"Editorial opinion. This review does not establish factual or semantic correctness."})];
    }else if(event.kind==="evaluation"){
      const e=value.evaluation;
      body=[h("p",{class:"prose",text:e.rationale}),list("Feedback accepted by Qwen",e.accepted_feedback),list("Feedback rejected by Qwen",e.rejected_feedback)];
    }else if(event.kind==="validation"){
      const v=value.validation;
      body=[h("p",{class:`validation-result ${v.valid?"valid":"invalid"}`,text:v.valid?"Declared deterministic checks passed":"Declared checks failed"}),list("Violations",v.violations.map(x=>`${x.code}: ${x.detail}${x.protected?" · protected requirement":""}`)),h("dl",{class:"keyvals"},Object.entries(v.metrics||{}).flatMap(([key,n])=>[h("dt",{text:titleize(key)}),h("dd",{text:n})])),h("p",{class:"hint",text:`Scope: ${v.scope}. Semantic verification: ${v.semantic_verified?"recorded":"not established"}. Counts are diagnostics, not quality scores.`})];
    }else if(event.kind==="decision"){
      body=[h("p",{class:"prose",text:titleize(value.reason)}),h("p",{class:"hint",text:value.kept_as_incumbent===true?"Candidate retained for declared measurable improvement.":value.kept_as_incumbent===false?"Candidate not retained; the prior candidate remains the incumbent.":value.kept_revision_id?`Retained candidate: ${labelFor(value.kept_revision_id)}`:value.scope||"Recorded independent workflow decision."})];
    }else if(event.kind==="finished"){
      body=[h("p",{class:"prose",text:`${titleize(value.state)} · ${titleize(value.stop_reason)}`}),h("p",{class:"hint",text:value.outcome?.scope||"No acceptance scope recorded."})];
    }else if(event.kind==="call_started"){
      body=[h("p",{class:"prose",text:`${titleize(value.purpose)} · ${value.role}`}),h("p",{class:"mono",text:value.model?.identifier||value.model}),h("p",{class:"hint",text:`Call ${value.call} · attempt ${value.attempt} · bounded runtime remaining ${Math.round(value.remaining_seconds)}s`})];
    }else if(event.kind==="call_finished"){
      const l=value.lifecycle||{},call=data.progress?.calls.find(c=>c.call===value.call);
      body=[h("p",{class:"prose",text:value.error||value.cleanup_error||"Response and cleanup recorded"}),h("dl",{class:"keyvals"},[["Output tokens",counter(call?.usage.output_tokens)],["Input tokens",counter(call?.usage.input_tokens)],["Total tokens",counter(call?.usage.total_tokens)],["Session",value.session_id],["Lifecycle",l.state||"Unknown"],["Load",fmtDuration(l.load_ms)],["Inference",fmtDuration(l.infer_ms)],["Unload",fmtDuration(l.unload_ms)],["Last verified unload",fmtDate(l.last_verified_unload)]].flatMap(([key,v])=>[h("dt",{text:key}),h("dd",{text:v||"—"})]))];
    }else body=[h("p",{class:"hint",text:titleize(event.kind)})];
    return h("article",{class:"evidence-record"},[heading,...body,technicalDetails(value)]);
  });
  return h("div",{},[
    h("div",{class:"tabs",role:"tablist","aria-label":"Experiment evidence"},Object.keys(types).map(key=>h("button",{type:"button",role:"tab",class:`tab ${S.collabTab===key?"active":""}`,"aria-selected":S.collabTab===key,"data-action":"collab-tab","data-tab":key,text:titleize(key)}))),
    h("div",{class:"tab-panel",role:"tabpanel"},events.length?content:h("div",{class:"empty",text:"No persisted evidence in this category."})),
  ]);
}

function renderCollaborationDetail() {
  renderHeader("Collaboration evidence", "Experiment workspace", `${S.route.database} / ${S.route.id}`);
  const key=`${S.route.section}/${S.route.database}/${S.route.id}`;
  if(!S.detail||S.detailKey!==key){view.replaceChildren(h("div",{class:"loading",text:"Loading collaboration evidence…"}));loadDetail();return;}
  const data=S.detail,run=data.run,db=S.bootstrap.databases.find(x=>x.key===S.route.database);
  const accepted=data.revisions.find(x=>x.id===run.accepted_revision_id);
  const summary=S.state.jobs.find(x=>x.id===run.id&&x.database===S.route.database);
  const actions=h("div",{class:"actions"},[
    h("a",{href:`/api/export/collaboration/${encodeURIComponent(S.route.database)}/${encodeURIComponent(run.id)}`,download:"",class:"button-link",text:"Download JSON"}),
    accepted?button("Copy accepted artifact","copy",{"data-copy":artifactText(accepted.artifact)}):null,
    modelActions(modelFor(summary?.progress?.current_call?.model||data.progress?.current_call?.model)),
    db?.writable&&summary?.can_cancel&&!run.cancel_requested?button("Cancel","cancel-collaboration",{class:"danger","data-db":S.route.database,"data-id":run.id}):null,
    button("Refresh","refresh-detail",{class:"quiet"}),
  ]);
  const outcome=run.outcome||{};
  view.replaceChildren(
    h("div",{class:"record-head"},[h("div",{},[h("h2",{class:"record-title",text:run.spec.name}),stateBadge(run.state)]),actions]),
    h("div",{class:"decision"},[h("strong",{text:accepted?`Accepted: ${accepted.label}`:`Outcome: ${titleize(run.state)}`}),h("p",{text:run.stop_reason?titleize(run.stop_reason):"No terminal decision has been recorded."}),h("p",{text:outcome.scope||"Model agreement is not correctness evidence."})]),
    section("Generation",generationInfo(S.state.jobs.find(j=>j.database===data.database&&j.id===data.run.id)?.progress||data.progress)),stageStrip(data),section("Revision comparison",comparisonWorkspace(data)),section("Evidence",evidenceTabs(data)),
    h("div",{class:"grid-equal"},[
      section("Experiment brief",h("div",{class:"panel"},[h("h3",{text:"Instruction"}),h("div",{class:"prose",text:run.spec.instruction}),h("details",{},[h("summary",{text:"Source and original"}),h("h3",{text:"Source"}),h("pre",{class:"code-block",text:pretty(run.spec.source)}),h("h3",{text:"Original"}),h("pre",{class:"code-block",text:pretty(run.spec.original)})])])),
      section("Specification",h("div",{class:"panel"},[h("dl",{class:"keyvals"},[["Semantic verification",run.spec.requires_semantic_verification?"Required":"Not required"],["Created",fmtDate(run.created_at)],["Completed",fmtDate(run.completed_at)],["Cancellation",run.cancel_requested?"Requested":"Not requested"]].flatMap(([a,b])=>[h("dt",{text:a}),h("dd",{text:b})])),technicalDetails({constraints:run.spec.constraints,metrics:run.spec.metrics,limits:run.spec.limits,models:run.models})])),
    ])
  );
}

function renderModels() {
  if(S.route.id)return W.renderModel();
  const selected=S.route.id?modelFor(S.route.id):null;
  renderHeader(selected?"Model details":"Models", "Observed local resources", "Read-only; no start or stop controls");
  const rows=selected?[selected]:(S.state.models||[]);
  const content=rows.length?h("div",{class:"panel"},rows.map(model=>{
    const session=model.last_session,obs=model.observation;
    const isCurrent=session?.id===S.state.lifecycle?.current?.id;
    const activeJob=isCurrent?S.state.jobs.find(j=>j.state==="running"&&j.database==="research"&&(j.id===session?.task_id||(j.kind==="collaboration"&&session?.task_id?.startsWith(`collab:${j.id}:`)))):null;
    const active=model.observed_active===true?"Resident":model.observed_active===false?"Not resident":"Unknown";
    return h("article",{class:"model-row"},[
      h("div",{},[h("h3",{text:model.runtime||model.identifier}),h("div",{class:"mono muted",text:model.identifier}),h("p",{class:"availability",text:`${titleize(model.availability)} — ${model.availability_reason||"No reason supplied"}`} ),modelActions(model,{selected:Boolean(selected)}),h("p",{class:"hint",text:model.access?.reason||"Browser interface unknown"})]),
      h("div",{},[h("span",{class:"eyebrow",text:"Live observation"}),h("strong",{text:active}),h("p",{class:"hint",text:obs?.observed_at?fmtDate(obs.observed_at):"Not observed"}),h("p",{class:"hint",text:obs?.ok?(model.provider==="ollama"?"Inference service responded":"Listener/identity observation complete"):obs?.error||"Connection not verified"}),activeJob?h("a",{class:"section-note",href:recordHref(activeJob),text:`${S.state.workers.find(w=>w.database==="research")?.active?"Active job":"Recorded running job"}: ${activeJob.name||activeJob.id}`}):h("p",{class:"hint",text:"No matching active job"})]),
      h("div",{},[h("span",{class:"eyebrow",text:"Ownership policy"}),h("strong",{text:model.owner_mode||"unknown"}),h("p",{class:"hint",text:`Last recorded ownership: ${session?.owner_mode||"unknown"}. Current process ownership is not inferred from a stale PID.`}),h("p",{class:"hint mono",text:model.endpoint})]),
      h("div",{},[h("span",{class:"eyebrow",text:"Recorded lifecycle"}),h("strong",{text:session?.state||"None"}),h("p",{class:"hint mono",text:`Recorded job: ${session?.task_id||"none"}`}),h("p",{class:"hint",text:"Last persisted session state; it may be stale when the worker is absent."}),h("p",{class:"hint",text:`Load ${fmtDuration(session?.load_ms)} · Infer ${fmtDuration(session?.infer_ms)} · Unload ${fmtDuration(session?.unload_ms)}`}),h("p",{class:"hint",text:`Last recorded activity ${fmtDate(session?.updated_at)} · successful inference ${fmtDate(session?.last_successful_inference)}`}),h("p",{class:"hint",text:`Verified unload ${fmtDate(session?.last_verified_unload)}`}),technicalDetails({observation:obs,session})]),
    ]);
  })):h("div",{class:"empty",text:"No configured model records."});
  view.replaceChildren(...(selected?[h("a",{href:"#/models",class:"section-note",text:"← All models"})]:[]),content);
}

function capabilityList(values, emptyText = "None recorded") {
  const items=(values||[]).filter(value=>value!==null&&value!==undefined&&String(value).trim());
  return items.length?h("ul",{class:"capability-points"},items.map(value=>h("li",{text:typeof value==="string"?value:pretty(value)}))):h("p",{class:"hint",text:emptyText});
}

async function loadCapabilities({detail = true} = {}) {
  const state=S.capabilities;if(state.loading){state.reload=true;return;}state.loading=true;state.error="";
  try {
    const params=new URLSearchParams({query:state.query,kind:state.kind,availability:state.availability,
      offset:String(state.offset),limit:String(state.limit)});
    state.page=await request(`/api/capabilities?${params}`);
    if(detail&&S.route.section==="capabilities"&&S.route.id){
      const value=await request(`/api/capabilities/${encodeURIComponent(S.route.id)}`);
      if(S.route.section==="capabilities"&&S.route.id===value.capability.id)state.detail=value.capability;
    } else if(!S.route.id) state.detail=null;
  } catch(error) {state.error=error.message;}
  finally {state.loading=false;if(S.route.section==="capabilities")render();renderNav();if(state.reload){state.reload=false;loadCapabilities();}}
}

function capabilityInspector(record) {
  if(!record)return h("div",{class:"capability-inspector empty",text:"Select a capability to inspect its verified purpose, limits, and access policy."});
  const callable=Boolean(record.callable),readable=Boolean(record.instruction_access);
  const access=callable?"Bounded adapter enabled":readable?"Instructions readable; execution requires a separate authorized workflow":"Catalog information only";
  const actions=[];
  if(readable)actions.push(button(S.capabilities.instructions?"Reload instructions":"Read instructions","capability-instructions",{"data-id":record.id}));
  if(S.capabilities.instructions)actions.push(button("Copy instructions","copy",{"data-copy":S.capabilities.instructions,class:"quiet"}));
  const schemas=(record.mcp_tools||[]).map(tool=>({name:tool.name,description:tool.description||"",inputSchema:tool.inputSchema||null,
    permitted:(record.permitted_operations||[]).includes(tool.name)}));
  return h("article",{class:"capability-inspector"},[
    h("div",{class:"record-head"},[h("div",{},[h("p",{class:"eyebrow",text:titleize(record.kind)}),h("h2",{class:"record-title",text:record.name}),h("p",{class:"mono muted",text:record.id})]),h("div",{class:"actions"},actions)]),
    h("p",{class:"prose",text:record.description||"No bounded description was available."}),
    h("div",{class:"capability-access"},[h("strong",{text:access}),h("p",{text:callable?"Only the displayed fixed operation and validated argument contract may execute. Model text never becomes a shell command or arbitrary URL.":"Reading guidance does not grant permission for side effects or make this capability model-callable."})]),
    h("dl",{class:"keyvals"},[["Availability",record.availability],["Configuration",record.configuration_status],["Execution transport",record.execution_transport],...(record.configured_transport?[["Agent host transport",record.configured_transport]]:[]),...(record.configured_endpoint?[["Configured endpoint",record.configured_endpoint]]:[]),["Version / source",record.version_or_source],["Last verified",fmtDate(record.last_verified)]].flatMap(([key,value])=>[h("dt",{text:key}),h("dd",{text:value||"—"})])),
    h("div",{class:"capability-detail-grid"},[
      h("div",{},[h("h3",{text:"Use when"}),capabilityList(record.when_to_use),h("h3",{text:"Do not use when"}),capabilityList(record.when_not_to_use)]),
      h("div",{},[h("h3",{text:record.kind==="MCP_SERVER"?"Runtime operations (permission required)":"Supported operations"}),capabilityList(record.supported_operations),h("h3",{text:"Dependencies & resources"}),capabilityList([...(record.required_dependencies||[]),...(record.resource_requirements||[])])]),
      h("div",{},[h("h3",{text:"Security constraints"}),capabilityList(record.security_constraints),h("h3",{text:"Provenance requirements"}),capabilityList(record.provenance_requirements)]),
    ]),
    record.kind==="MCP_SERVER"?h("div",{class:"capability-contract"},[h("h3",{text:"MCP operation policy"}),h("p",{class:"hint",text:`Discovered schemas: ${schemas.length}. Permitted through Command Center: ${(record.permitted_operations||[]).join(", ")||"none"}. Discovery does not grant execution.`}),technicalDetails({permitted_operations:record.permitted_operations||[],discovered_tools:schemas})]):null,
    record.invocation?technicalDetails({invocation:record.invocation,side_effects:record.side_effects,sources:record.source_paths||[],documentation_path:record.documentation_path,runtime_diagnostics:record.runtime_diagnostics}):technicalDetails({side_effects:record.side_effects,sources:record.source_paths||[],documentation_path:record.documentation_path}),
    S.capabilities.instructions?h("section",{class:"instruction-reader"},[h("h3",{text:"Bounded skill instructions"}),h("p",{class:"hint",text:"Reference guidance only. Instructions are displayed as plain text and do not execute."}),h("pre",{class:"code-block",text:S.capabilities.instructions})]):null,
  ]);
}

function renderCapabilities() {
  const state=S.capabilities,page=state.page;
  renderHeader("Skills & Tools","Capability catalog",page?`${page.total.toLocaleString()} matching · ${page.summary.total.toLocaleString()} inventoried`:"Read-only local inventory");
  const search=h("input",{name:"query",type:"search",value:state.query,placeholder:"Search skills, tools or purpose",autocomplete:"off"});
  const kind=h("select",{name:"kind"},[["","All kinds"],["SKILL","Skills"],["MCP_SERVER","MCP servers"],["CLI_TOOL","CLI tools"],["LOCAL_SERVICE","Local services"],["NATIVE_TOOL","Native adapters"],["WORKFLOW","Workflows"],["VALIDATOR","Validators"]].map(([value,label])=>h("option",{value,selected:state.kind===value?"selected":null,text:label})));
  const availability=h("select",{name:"availability"},[["","All statuses"],["DISABLED","Disabled"],["CALLABLE","Callable adapter"],["READY","Ready"],["CONFIGURED","Configured"],["DISCOVERED","Discovered"],["UNVERIFIED","Unverified"],["UNAVAILABLE","Unavailable"]].map(([value,label])=>h("option",{value,selected:state.availability===value?"selected":null,text:label})));
  const filter=h("form",{id:"capability-filter",class:"toolbar capability-filter"},[field("Search",search),field("Kind",kind),field("Availability",availability),h("button",{type:"submit",class:"primary",text:"Apply"})]);
  let listBody;
  if(state.error)listBody=h("div",{class:"error-state",text:state.error});
  else if(!page)listBody=h("div",{class:"loading",text:"Loading the bounded local catalog…"});
  else if(!page.items.length)listBody=h("div",{class:"empty",text:"No capabilities match these filters."});
  else listBody=h("div",{class:"capability-list"},page.items.map(item=>h("a",{class:`capability-item${S.route.id===item.id?" active":""}`,href:`#/capabilities/${encodeURIComponent(item.id)}`},[
    h("span",{class:"capability-item-head"},[h("strong",{text:item.name}),h("span",{class:"state",text:item.availability||"unknown"})]),
    h("span",{class:"capability-kind",text:titleize(item.kind)}),h("span",{class:"capability-description",text:item.description}),
    h("span",{class:"capability-access-label",text:item.callable?"Bounded adapter":item.instruction_access?"Readable guidance":"Inventory only"}),
  ])));
  const previous=page&&page.offset>0?button("Previous","capability-page",{"data-offset":Math.max(0,page.offset-page.limit)}):null;
  const next=page&&page.offset+page.items.length<page.total?button("Next","capability-page",{"data-offset":page.offset+page.limit}):null;
  const pages=h("div",{class:"capability-pages"},[previous,page?h("span",{text:page.total?`${page.offset+1}–${page.offset+page.items.length} of ${page.total}`:"0 results"}):null,next]);
  const categories=h("div",{class:"catalog-categories","aria-label":"Quick capability filters"},[["","All",page?.summary.total],["SKILL","Skills",page?.summary.kinds.SKILL],["MCP_SERVER","MCP servers",page?.summary.kinds.MCP_SERVER]].map(([value,label,count])=>h("button",{type:"button",class:"catalog-category","data-action":"capability-kind","data-kind":value,"aria-pressed":String(state.kind===value)},[label,h("small",{text:count??"—"})])));
  view.replaceChildren(categories,filter,h("div",{class:"capability-workspace"},[h("section",{class:"capability-results"},[listBody,pages]),capabilityInspector(state.detail)]));
}

function renderResults() {
  if(S.route.id)return renderRecord("result");
  renderHeader("Results", "Evidence library", "Completed and terminal records");
  const terminal=new Set(["completed","failed","cancelled","abstained","interrupted","rejected"]);
  const rows=S.state.jobs.filter(x=>terminal.has(x.state));
  view.replaceChildren(section("Library",h("div",{},[filtersToolbar({results:true}),jobTable(rows)])));
}

function renderActivity() {
  renderHeader("Activity", "Persisted event chronology", "Latest 200 persisted or timestamp-derived events");
  const rows=[...(S.state.activity||[])].sort((a,b)=>new Date(b.at)-new Date(a.at));
  view.replaceChildren(section("Recent operations",h("div",{class:"activity-scroll",tabindex:"0",role:"region","aria-label":"Recorded activity"},activityTimeline(rows))),technicalDetails({observed_at:S.state.observed_at,truncated:S.state.truncated}));
}

function parseJSONField(form,name,fallback={}) {
  const raw=form.elements[name].value.trim();return raw?JSON.parse(raw):fallback;
}

async function submitJob(form) {
  const error=$("#job-form-error");error.textContent="";
  const selected=S.bootstrap.models.find(x=>x.identifier===form.elements.model_identifier.value);
  const payload={model_identifier:selected.identifier,prompt:form.elements.prompt.value,name:form.elements.name.value||undefined};
  if(form.elements.scheduled_at.value)payload.scheduled_at=new Date(form.elements.scheduled_at.value).toISOString();
  if(form.elements.max_tokens.value)payload.max_tokens=Number(form.elements.max_tokens.value);
  if(form.elements.timeout_seconds.value)payload.timeout_seconds=Number(form.elements.timeout_seconds.value);
  const submit=form.querySelector("[type=submit]");submit.disabled=true;
  try{const result=await request("/api/jobs",{method:"POST",body:JSON.stringify(payload)});S.dirty=false;location.hash=`#/jobs/${result.database}/${result.id}`;await poll({manual:true});}
  catch(exc){error.textContent=exc.message;}
  finally{submit.disabled=false;}
}

async function submitCollaboration(form) {
  const error=$("#collaboration-form-error");error.textContent="";
  const submit=form.querySelector("[type=submit]");submit.disabled=true;
  try{
    let spec;
    if(form.elements.full_spec.value.trim())spec=JSON.parse(form.elements.full_spec.value);
    else{
      const format=form.elements.format.value;
      const parseArtifact=value=>format==="json"?JSON.parse(value):value;
      const sourceRaw=form.elements.source.value;
      let source=sourceRaw;if(format==="json"){try{source=JSON.parse(sourceRaw);}catch{source=sourceRaw;}}
      spec={name:form.elements.name.value,instruction:form.elements.instruction.value,source,original:parseArtifact(form.elements.original.value),format,
        requires_semantic_verification:form.elements.semantic.value==="true",constraints:parseJSONField(form,"constraints"),metrics:parseJSONField(form,"metrics"),limits:{rounds:Number(form.elements.rounds.value),calls:Number(form.elements.calls.value),runtime:Number(form.elements.runtime.value),output_chars:Number(form.elements.output_chars.value),total_output_chars:80000,max_tokens:Number(form.elements.max_tokens.value),retries:Number(form.elements.retries.value)}};
    }
    const payload={spec};if(form.elements.scheduled_at.value)payload.scheduled_at=new Date(form.elements.scheduled_at.value).toISOString();
    const result=await request("/api/collaborations",{method:"POST",body:JSON.stringify(payload)});S.dirty=false;location.hash=`#/collaboration/${result.database}/${result.id}`;await poll({manual:true});
  }catch(exc){error.textContent=exc.message;}
  finally{submit.disabled=false;}
}

async function cancelRecord(kind,db,id,buttonNode) {
  buttonNode.disabled=true;
  try{const path=kind==="research"?`/api/jobs/${encodeURIComponent(db)}/${encodeURIComponent(id)}/cancel`:`/api/collaborations/${encodeURIComponent(db)}/${encodeURIComponent(id)}/cancel`;await request(path,{method:"POST",body:"{}"});setNotice(kind==="research"?"Cancellation applied if the job was still pending.":"Cancellation recorded; any in-flight call will complete bounded cleanup.");await poll({manual:true});}
  catch(exc){setNotice(exc.message,"error");buttonNode.disabled=false;}
}

async function copyText(value) {
  try{await navigator.clipboard.writeText(value);setNotice("Copied to clipboard.");}
  catch{const area=h("textarea",{class:"sr-only"});area.value=value;document.body.append(area);area.select();const ok=document.execCommand("copy");area.remove();setNotice(ok?"Copied to clipboard.":"Clipboard access failed; select and copy from the record.",ok?"":"error");}
}

async function loadCapabilityInstructions(identifier, control) {
  control.disabled=true;
  try {
    const value=await request(`/api/capabilities/${encodeURIComponent(identifier)}/instructions`);
    if(S.route.section==="capabilities"&&S.route.id===identifier){S.capabilities.instructions=value.content;renderCapabilities();}
  } catch(error) {setNotice(error.message,"error");control.disabled=false;}
}

function applyExample(id) {
  const example=S.bootstrap.examples.find(x=>x.id===id);if(!example)return;
  const form=$("#collaboration-form"),spec=example.spec;form.elements.name.value=spec.name||"";form.elements.instruction.value=spec.instruction||"";form.elements.source.value=pretty(spec.source);form.elements.original.value=pretty(spec.original);form.elements.format.value=spec.format||"text";form.elements.semantic.value=String(spec.requires_semantic_verification!==false);form.elements.constraints.value=JSON.stringify(spec.constraints||{},null,2);form.elements.metrics.value=JSON.stringify(spec.metrics||{},null,2);for(const [key,value] of Object.entries(spec.limits||{})){if(form.elements[key])form.elements[key].value=value;}S.dirty=true;
}

document.addEventListener("input",event=>{if(event.target.closest("form"))S.dirty=true;if(event.target.closest("[data-filter-scope]"))filterRows(event.target.closest("[data-filter-scope]").dataset.filterScope);});
document.addEventListener("change",event=>{
  if(event.target.closest("form"))S.dirty=true;
  if(event.target.id==="mobile-nav")location.hash=`#/${event.target.value}`;
  if(event.target.name==="example")applyExample(event.target.value);
  if(event.target.dataset.action==="compare-select"){if(event.target.name==="compare-left")S.filters.compareLeft=event.target.value;else S.filters.compareRight=event.target.value;renderCollaborationDetail();}
  if(event.target.closest("[data-filter-scope]"))filterRows(event.target.closest("[data-filter-scope]").dataset.filterScope);
});
document.addEventListener("submit",event=>{event.preventDefault();if(event.target.id==="job-form")submitJob(event.target);if(event.target.id==="collaboration-form")submitCollaboration(event.target);if(event.target.id==="capability-filter"){const data=new FormData(event.target);S.capabilities.query=String(data.get("query")||"");S.capabilities.kind=String(data.get("kind")||"");S.capabilities.availability=String(data.get("availability")||"");S.capabilities.offset=0;S.capabilities.detail=null;S.capabilities.instructions=null;S.dirty=false;if(S.route.id)location.hash="#/capabilities";else loadCapabilities({detail:false});}});
document.addEventListener("click",event=>{
  const target=event.target.closest("[data-action]");if(!target)return;const action=target.dataset.action;
  if(action==="new-job"){event.preventDefault();const focus=()=>{const form=$("#job-form");form?.scrollIntoView({block:"start",behavior:"auto"});form?.querySelector("select,input,textarea")?.focus({preventScroll:true});};if(S.route.section!=="jobs"){location.hash="#/jobs";requestAnimationFrame(()=>requestAnimationFrame(focus));}else focus();}
  else if(action==="capability-kind"){S.capabilities.kind=target.dataset.kind||"";S.capabilities.query="";S.capabilities.offset=0;S.capabilities.detail=null;S.capabilities.instructions=null;S.dirty=false;if(S.route.id)location.hash="#/capabilities";else loadCapabilities({detail:false});}
  else if(action==="cancel-job")cancelRecord("research",target.dataset.db,target.dataset.id,target);
  else if(action==="cancel-collaboration")cancelRecord("collaboration",target.dataset.db,target.dataset.id,target);
  else if(action==="refresh-detail")loadDetail(true);
  else if(action==="copy")copyText(target.dataset.copy||"");
  else if(action==="capability-instructions")loadCapabilityInstructions(target.dataset.id,target);
  else if(action==="capability-page"){S.capabilities.offset=Number(target.dataset.offset)||0;loadCapabilities({detail:false});}
  else if(action==="collab-tab"){S.collabTab=target.dataset.tab;renderCollaborationDetail();}
  else if(action==="clear-job"){target.closest("form").reset();S.dirty=false;}
  else if(action==="clear-collaboration"){target.closest("form").reset();S.dirty=false;}
});

const W=makeWorkspaces({h,view,field,button,request,renderHeader,stateBadge,fmtDate,fmtDuration,pretty,setNotice,recordHref,getState:()=>S});

window.addEventListener("hashchange",()=>{W.reset();setNotice("");const priorCapability=S.route.section==="capabilities"?S.route.id:null;S.route=routeFromHash();S.detail=null;S.detailKey="";S.dirty=false;S.collabTab="critique";S.filters.compareLeft=null;S.filters.compareRight=null;if(priorCapability!==S.route.id){S.capabilities.instructions=null;S.capabilities.detail=null;}renderNav();render();if(isRecordRoute())loadDetail();if(S.route.section==="capabilities")loadCapabilities();if(S.route.section==="endpoints"||(S.route.section==="models"&&S.route.id))W.loadRoute();});
document.addEventListener("visibilitychange",()=>{if(!document.hidden)poll();});

async function start() {
  S.route=routeFromHash();renderNav();
  setInterval(async()=>{if(!S.bootstrap){try{await loadBootstrap();}catch{return;}}poll();},5000);
  try{await loadBootstrap();await poll({manual:true});if(S.route.section==="capabilities")loadCapabilities();}
  catch(error){S.connected=false;updateConnection();renderError(`Unable to initialize Command Center: ${error.message}`);}
}
start();
