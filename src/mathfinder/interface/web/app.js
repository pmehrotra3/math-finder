// Math Finder web page. Talks to interface/server.py: REST for actions, a WebSocket for live changes.
//
// The page is laid out like a chat app:
//   left    your projects. Every theorem is a project, saved on the server.
//   right   the open project, its own space with two sides:
//           - the CHAT: discuss the proof with the assistant, or tell it what to do
//             ("stop, try induction on n"). What the proof itself reports appears here too.
//           - the PROOF: the theorem and its lemmas as a tree. Zoom in on one goal to see its
//             Lean and LaTeX, make or edit its plan, prove it; the final proof once it is checked.
// A new project starts on its own page: write the theorem in LaTeX, then confirm its Lean.

const $ = (id) => document.getElementById(id);

const state = {
  projects: [],       // the list on the left
  latex: "",          // the theorem being written on the new-project page
  projectId: null,    // the open project
  project: null,      // {id, title, latex, established, conversation, notes, result}
  library: null,      // established results the open project may cite (loaded when needed)
  ws: null,           // the workspace from the server: {root, goals: {name: goal}, busy}
  focus: "main",      // the goal open in the proof panel (zoomed in on)
  options: {},        // plans the model proposed, per goal: {name: [option, ...]}
  editing: null,      // the plan being edited: {goal, sketch, main_proof, lemmas: [{name, lean}]}
  shown: new Set(),   // ids of the chat lines already on screen
  attempts: 0,        // proof attempts Lean checked in the running job
  agent: { state: "idle", step: 0 },  // the assistant: idle | thinking | acting | waiting (for a job)
  sent: [],           // your earlier messages, for the up arrow
};

// ---------- talking to the server ----------

async function request(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}
const get = (path) => request("GET", path);
const post = (path, body = {}) => request("POST", path, body);

function goalAction(name, action, body = {}) {
  return post(`/api/projects/${state.projectId}/goals/${encodeURIComponent(name)}/${action}`, body);
}

// ---------- small helpers ----------

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMath(node) {
  if (window.renderMathInElement) {
    renderMathInElement(node, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
    });
  }
}

function showText(node, text) {
  node.textContent = text;
  renderMath(node);
}

// Markdown would mangle LaTeX (it treats _ and \ specially), so we hide the math first,
// let marked do the Markdown, put the math back, and let KaTeX draw it.
function renderMarkdown(node, text) {
  const math = [];
  const hidden = text.replace(/\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/g, (m) => {
    math.push(m);
    return `@@MATH${math.length - 1}@@`;
  });
  let html = window.marked ? marked.parse(escapeHtml(hidden)) : `<pre>${escapeHtml(hidden)}</pre>`;
  html = html.replace(/@@MATH(\d+)@@/g, (_, i) => escapeHtml(math[Number(i)]));
  node.innerHTML = html;
  renderMath(node);
}

// A light touch of Lean highlighting: keywords and comments.
const LEAN_KEYWORDS = /\b(theorem|lemma|example|by|namespace|end|have|show|exact|intro|intros|rw|simp|calc|fun|let|obtain|rcases|cases|induction|with|at|from|use|apply|refine|constructor|omega|ring|linarith|nlinarith|norm_num|positivity|decide|mod_cases)\b/g;
function highlightLean(code) {
  return code
    .split("\n")
    .map((line) => {
      const at = line.indexOf("--");
      const body = at === -1 ? line : line.slice(0, at);
      const comment = at === -1 ? "" : `<span class="cm">${escapeHtml(line.slice(at))}</span>`;
      return escapeHtml(body).replace(LEAN_KEYWORDS, '<span class="kw">$1</span>') + comment;
    })
    .join("\n");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, onClick, className = "btn") {
  const b = el("button", className, label);
  b.onclick = onClick;
  return b;
}

function spinnerText(node, text) {
  node.innerHTML = text ? `<span class="spinner"></span><span></span>` : "";
  if (text) node.lastChild.textContent = text;
}

function timeAgo(seconds) {
  const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}

// ---------- theme ----------

function toggleTheme() {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("mf-theme", document.documentElement.dataset.theme); } catch (e) { /* blocked */ }
}

// ---------- which page is showing: #new or #project=<id> ----------

function route() {
  const opened = location.hash.match(/project=([0-9a-f]+)/);
  closeSidebar();
  if (opened) {
    if (opened[1] !== state.projectId) openProjectById(opened[1]);
  } else {
    showNewProject();
  }
}

function showView(name) {
  $("view-new").classList.toggle("hidden", name !== "new");
  $("view-project").classList.toggle("hidden", name !== "project");
}

function openSidebar() { $("app").classList.add("sidebar-open"); }
function closeSidebar() { $("app").classList.remove("sidebar-open"); }

// ---------- left: the projects ----------

const PROJECT_STATUS = {
  established: "established", proved: "proved", working: "working", stuck: "stuck", assumed: "assumed", open: "open",
};

async function loadProjects() {
  try {
    state.projects = (await get("/api/projects")).projects;
  } catch (e) {
    return;
  }
  renderProjects();
  nameUnnamedProjects();
}

// Projects from before names existed get a suggested name, one at a time, once per visit.
const naming = new Set();
async function nameUnnamedProjects() {
  for (const p of state.projects.filter((x) => x.name === x.title && !naming.has(x.id))) {
    naming.add(p.id);
    try {
      p.name = (await post(`/api/projects/${p.id}/suggest-name`)).name;
    } catch (e) { continue; }
    renderProjects();
    if (p.id === state.projectId) {
      state.project.name = p.name;
      showText($("project-name-text"), p.name);
      render();
    }
  }
}

// Projects in two groups, newest first: the ones you are working on, and the established ones
// (finished; other projects can use their theorems). The search box filters both.
function renderProjects() {
  const list = $("project-list");
  list.innerHTML = "";
  if (!state.projects.length) {
    list.append(el("p", "sidebar-empty", "No projects yet. Write a theorem to start one."));
    return;
  }
  const query = $("project-search").value.trim().toLowerCase();
  const shown = state.projects.filter((p) => !query || `${p.name} ${p.title} ${p.statement}`.toLowerCase().includes(query));
  const groups = [["In progress", shown.filter((p) => !p.established)], ["Established", shown.filter((p) => p.established)]];
  groups.forEach(([label, projects]) => {
    if (!projects.length) return;
    list.append(el("span", "sidebar-label", label));
    projects.forEach((p) => list.append(projectItem(p)));
  });
  if (!shown.length) list.append(el("p", "sidebar-empty", "No project matches."));
}

function projectItem(p) {
  const item = el("a", "project-item" + (p.id === state.projectId ? " current" : ""));
  item.href = `#project=${p.id}`;
  const name = el("span", "project-item-name");
  showText(name, p.name);
  const meta = el("span", "project-item-meta");
  meta.append(el("i", "dot " + (p.established ? "proved" : p.status)));
  const statement = el("span", "project-item-statement math");
  if (p.name !== p.title) showText(statement, p.title);
  else statement.textContent = `${PROJECT_STATUS[p.status] || p.status} · ${timeAgo(p.updated)}`;
  meta.append(statement);
  const remove = button("×", (e) => { e.preventDefault(); deleteProject(p); }, "project-delete");
  remove.title = "Delete this project";
  remove.setAttribute("aria-label", "Delete this project");
  item.append(name, meta, remove);
  item.title = `${p.name} · ${PROJECT_STATUS[p.status] || p.status} · changed ${timeAgo(p.updated)}`;
  return item;
}

async function deleteProject(p) {
  if (!confirm(`Delete “${p.name}”? This cannot be undone.`)) return;
  try {
    await request("DELETE", `/api/projects/${p.id}`);
  } catch (e) {
    alert("Couldn't delete it: " + e.message);
    return;
  }
  if (p.id === state.projectId) location.hash = "new";
  loadProjects();
}

// ---------- a new project: write the theorem, then confirm its Lean ----------

function showNewProject() {
  closeProject();
  showView("new");
  renderProjects();
  updatePreview();
  $("latex").focus();
}

function updatePreview() {
  const text = $("latex").value.trim();
  if (text) showText($("preview"), text);
  else $("preview").innerHTML = '<span class="muted-text">Your theorem appears here</span>';
}

async function formalize() {
  state.latex = $("latex").value.trim();
  if (!state.latex) return;
  $("formalize-btn").disabled = true;
  spinnerText($("formalize-status"), "Translating to Lean and checking it…");
  try {
    const f = await post("/api/formalize", { latex: state.latex });
    if (!f.ok) {
      $("formalize-status").textContent = "Lean couldn't accept any translation: " + (f.errors[0] || "");
      return;
    }
    spinnerText($("formalize-status"), "");
    $("statement").value = f.statement;
    $("project-name").value = f.name || "";
    showText($("back"), f.back_translation);
    const v = $("verdict");
    v.className = "notice " + (f.faithful ? "ok" : "warn");
    v.textContent = (f.faithful ? "✓ Same meaning. " : "⚠ The meaning may differ. ") + f.judgment;
    $("check-errors").classList.add("hidden");
    $("confirm").classList.remove("hidden");
    $("confirm").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    $("formalize-status").textContent = "Error: " + e.message;
  } finally {
    $("formalize-btn").disabled = false;
  }
}

async function checkEdit() {
  const box = $("check-errors");
  $("check-btn").disabled = true;
  try {
    const { errors } = await post("/api/check", { statement: $("statement").value });
    box.className = "notice " + (errors.length ? "bad" : "ok");
    box.textContent = errors.length ? "Lean rejected it:\n" + errors[0] : "✓ Lean accepts this statement.";
  } finally {
    $("check-btn").disabled = false;
  }
}

async function approve() {
  $("approve-btn").disabled = true;
  try {
    const project = await post("/api/projects", {
      statement: $("statement").value, latex: state.latex, name: $("project-name").value.trim(),
    });
    $("confirm").classList.add("hidden");
    location.hash = `project=${project.id}`;  // route() opens it
  } catch (e) {
    $("check-errors").className = "notice bad";
    $("check-errors").textContent = "Error: " + e.message;
  } finally {
    $("approve-btn").disabled = false;
  }
}

// ---------- one project ----------

let socket = null;

async function openProjectById(id) {
  try {
    openProject(await get(`/api/projects/${id}`));
  } catch (e) {
    alert("Couldn't open the project: " + e.message);
    location.hash = "new";
  }
}

function closeProject() {
  if (socket) { socket.close(); socket = null; }
  Object.assign(state, { projectId: null, project: null, ws: null });
}

function openProject(project) {
  closeProject();
  Object.assign(state, {
    projectId: project.id, project, ws: project.workspace, focus: "main", options: {}, editing: null,
    library: null, shown: new Set(), attempts: 0, agent: project.agent || { state: "idle", step: 0 },
  });
  showView("project");
  showText($("project-title"), project.title);
  showText($("project-name-text"), project.name);
  $("messages").innerHTML = "";
  project.conversation.forEach(addLine);
  showResult(project.result);
  showProofTab("tree");
  showSide("chat");
  render();
  renderProjects();
  scrollChat();

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws/${project.id}`);
  socket.onmessage = (message) => handleEvent(JSON.parse(message.data));
}

function handleEvent(e) {
  if (e.workspace) state.ws = e.workspace;
  switch (e.kind) {
    case "chat":
      addLine(e.entry);
      scrollChat();
      if ((e.entry.actions || []).some((a) => a.do === "note" || a.do === "clear_notes")) refreshNotes();
      break;
    case "agent":
      state.agent = { state: e.state, step: e.step };
      renderAgent();
      break;
    case "attempt":
      state.attempts += 1;
      break;
    case "error":
      notice(e.text, "error");
      break;
    case "idle":
      state.attempts = 0;
      loadProjects();
      break;
    case "finished":
      state.project.result = e.result;
      if (e.established) state.project.established = true;
      showResult(e.result);
      showProofTab("final");
      loadProjects();
      break;
  }
  render();
}

function render() {
  if (!state.ws) return;
  if (!state.ws.goals[state.focus]) state.focus = "main";
  renderHead();
  renderAgent();
  renderNotes();
  renderProgress();
  renderBreadcrumb();
  renderOutline();
  renderFocus();
}

function renderHead() {
  const busyNow = state.ws.busy;
  $("project-title").classList.toggle("hidden", state.project.name === state.project.title);
  const root = goal("main");
  const status = state.project.established ? "established" : root.status;
  $("project-status").replaceChildren(statusBadgeFor(status, root));
  $("job").classList.toggle("hidden", !busyNow);
  const working = Object.values(state.ws.goals).find((g) => g.status === "working");
  const what = working ? `Working on ${working.name === "main" ? "the theorem" : working.name}` : "Working";
  $("job-text").textContent = what + (state.attempts ? ` · ${state.attempts} attempts checked` : "") + "…";
  $("stop-btn").classList.toggle("hidden", !busyNow || agentWorking());
  $("finish-btn").disabled = busyNow || root.status !== "proved" || root.assumes.length > 0;
}

// ---------- progress: how far the proof is, and what to do next ----------

function renderProgress() {
  const goals = Object.values(state.ws.goals);
  const proved = goals.filter((g) => g.status === "proved").length;
  const box = $("progress");
  box.innerHTML = "";
  const head = el("div", "progress-head");
  head.append(el("b", "", `${proved} of ${goals.length} goal${goals.length === 1 ? "" : "s"} proved`));
  const bar = el("div", "progress-bar");
  const fill = el("span");
  fill.style.width = `${Math.round((100 * proved) / goals.length)}%`;
  bar.append(fill);
  head.append(bar);
  box.append(head);

  const [text, label, act] = nextStep(goal("main"));
  if (!text) return;
  const next = el("div", "next-step");
  next.append(el("span", "", text));
  if (label) {
    const b = button(label, act, "btn tiny primary");
    b.disabled = state.ws.busy || agentWorking();
    next.append(b);
  }
  box.append(next);
}

// The most useful thing to do now, as [sentence, button label, what the button does].
function nextStep(root) {
  if (state.ws.busy || agentWorking()) return ["Working… you can keep chatting, or press Esc to stop.", "", null];
  if (state.project.established) return ["Established. Other projects can use this theorem as a lemma.", "", null];
  const assumed = root.assumes || [];
  if (root.status === "proved" && !assumed.length) {
    return ["Proved. Run the final check of the whole proof.", "Check whole proof", finishProof];
  }
  if (root.status === "proved") {
    return [`Proved, assuming ${assumed.join(", ")}. Prove ${assumed.length === 1 ? "it" : "them"} to finish.`,
            `Open ${assumed[0]}`, () => zoom(assumed[0])];
  }
  if (!root.children.length && !root.attempts.length) {
    return ["Start with a plan, or prove it directly. Or tell the assistant what to try.", "Suggest plans",
            () => { zoom("main"); suggestFor("main"); }];
  }
  const order = [...pathOrder()];
  const stuck = order.find((n) => ["stuck", "refuted"].includes(goal(n).status));
  if (stuck) {
    const label = stuck === "main" ? "The theorem" : stuck;
    return [`${label} is stuck. See what Lean said, change the plan, or ask the assistant.`, `Open ${stuck}`, () => zoom(stuck)];
  }
  const open = order.find((n) => goal(n).status === "open");
  if (open) {
    return [`Next: prove ${open === "main" ? "the theorem" : open}.`, `Prove ${open === "main" ? "it" : open}`,
            () => attempt(goalAction(open, "prove"))];
  }
  return ["", "", null];
}

// Every goal, lemmas before the goals that use them (the order they get proved in).
function* pathOrder(name = "main") {
  for (const child of goal(name).children) yield* pathOrder(child);
  yield name;
}

function statusBadgeFor(status, g) {
  if (status === "established") return el("span", "badge established", "established");
  if (g && status === "proved" && g.assumes.length) return el("span", "badge assumed", "proved, with assumptions");
  return el("span", "badge " + status, STATUS_TEXT[status] || status);
}

// ---------- the chat ----------

const ACTION_TEXT = {
  stop: "Stop", wait: "Wait", note: "Hint", clear_notes: "Hints", try_proof: "Lean", prove: "Prove",
  search: "Search", plan: "Plan", clear_plan: "Plan", statement: "Statement", assume: "Assume", cite: "Cite",
  finish: "Final check",
};

function agentWorking() {
  return state.agent.state !== "idle";
}

// While the assistant works: "Thinking…" at the end of the chat, and a status line with Stop.
function renderAgent() {
  const working = agentWorking();
  const status = $("agent-status");
  status.classList.toggle("hidden", !working && !(state.ws && state.ws.busy));
  const text = { thinking: "Thinking", acting: "Working on the proof", waiting: "Waiting for Lean" }[state.agent.state]
    || "A job is running";
  $("agent-text").textContent = text + (working && state.agent.step > 1 ? ` · step ${state.agent.step}` : "") + "…";
  $("chat-input").placeholder = working
    ? "Add a message to steer it. It reads it at its next step."
    : "Discuss the proof, or tell the assistant what to do. Type / for commands.";
  if (state.agent.state === "thinking") {
    spinnerText(thinking, "Thinking…");
    $("messages").append(thinking);
  } else {
    thinking.remove();
  }
}

function addLine(entry) {
  const box = $("messages");
  if (entry.id && state.shown.has(entry.id)) {
    // A line we show already: the assistant's reply, with the results of its actions coming in.
    const old = box.querySelector(`[data-id="${entry.id}"]`);
    if (old && entry.role !== "event") old.replaceWith(messageFor(entry));
    return;
  }
  if (entry.id) state.shown.add(entry.id);
  box.querySelector(".chat-welcome")?.remove();

  if (entry.role === "event") {
    // Updates from the proof are small lines; consecutive ones are grouped.
    let group = [...box.children].filter((c) => c !== thinking).pop();
    if (!group || !group.classList.contains("events")) {
      group = el("div", "events");
      box.append(group);
    }
    const line = el("div", "event" + (entry.kind === "error" ? " error" : ""));
    line.append(el("i", "event-dot " + eventKind(entry.text)), el("span", "", entry.text));
    group.append(line);
    foldEvents(group);
    if (thinking.isConnected) box.append(thinking);
    return;
  }

  box.append(messageFor(entry));
  if (thinking.isConnected) box.append(thinking);
}

function messageFor(entry) {
  const bubble = el("div", "msg " + entry.role);
  if (entry.id) bubble.dataset.id = entry.id;
  if (entry.role === "you") {
    bubble.textContent = entry.text;
  } else {
    const body = el("div", "msg-body");
    renderMarkdown(body, entry.text || "");
    bubble.append(body);
    if (entry.actions && entry.actions.length) {
      const done = el("div", "did");
      entry.actions.forEach((a) => {
        const running = a.ok === null || a.ok === undefined;
        const chip = el("div", "did-item " + (running ? "running" : a.ok ? "ok" : "bad"));
        const mark = running ? el("span", "spinner") : document.createTextNode(a.ok ? "✓ " : "✕ ");
        const label = el("b", "", ACTION_TEXT[a.do] || a.do);
        label.prepend(mark);
        chip.append(label, el("span", "", a.text));
        done.append(chip);
        if (a.lean) {
          const code = el("pre", "did-lean");
          code.innerHTML = highlightLean(a.lean);
          done.append(code);
        }
      });
      bubble.append(done);
    }
  }
  return bubble;
}

function eventKind(text) {
  if (/proved|passed|established/.test(text)) return "ok";
  if (/stuck|failed|refuted|error|doesn't/i.test(text)) return "bad";
  return "";
}

// A long run of updates shows its last few lines; the rest folds away.
function foldEvents(group) {
  const lines = [...group.querySelectorAll(":scope > .event, :scope > details .event")];
  if (lines.length <= 4) return;
  let details = group.querySelector(":scope > details");
  if (!details) {
    details = el("details", "older");
    details.append(el("summary"));
    group.prepend(details);
  }
  const visible = [...group.querySelectorAll(":scope > .event")];
  visible.slice(0, visible.length - 3).forEach((line) => details.append(line));
  const hidden = details.querySelectorAll(".event").length;
  details.querySelector("summary").textContent = `${hidden} earlier update${hidden === 1 ? "" : "s"}`;
}

// A message on this page only (not saved): an error from a button, say.
function notice(text, kind = "") {
  addLine({ role: "event", text, kind });
  scrollChat();
}

function scrollChat() {
  const box = $("messages");
  if (!box.children.length) showWelcome();
  // the suggestions help you start; once the conversation is going they are only in the way
  $("suggestions").classList.toggle("hidden", !box.querySelector(".chat-welcome"));
  box.scrollTop = box.scrollHeight;
}

function showWelcome() {
  const welcome = el("div", "chat-welcome");
  welcome.append(el("h3", "", "Work on this proof together"));
  welcome.append(el("p", "", "Ask about the math, or give the assistant a task: “plan this and prove it”, " +
    "“write the proof by induction”. It works step by step, waits for Lean, reads what Lean says, and keeps going " +
    "until the task is done. You can write while it works to steer it, and press Esc to stop it. " +
    "The proof next to the chat updates as it goes; its buttons and the / commands act at once."));
  $("messages").append(welcome);
}

// "Thinking…" while the assistant writes its reply; always the last thing in the chat.
const thinking = el("div", "msg assistant thinking");

async function sendMessage(text) {
  const message = (text || $("chat-input").value).trim();
  if (!message || !state.projectId) return;
  $("chat-input").value = "";
  hideCommands();
  state.sent.push(message);
  if (message.startsWith("/")) return runCommand(message);
  try {
    // The server sends your line, the assistant's steps and the proof's updates over the WebSocket.
    addLine((await post(`/api/projects/${state.projectId}/chat`, { message, looking_at: state.focus })).entry);
    scrollChat();
  } catch (e) {
    notice("Couldn't send: " + e.message, "error");
  }
}

async function refreshNotes() {
  try {
    state.project.notes = (await get(`/api/projects/${state.projectId}`)).notes;
    renderNotes();
  } catch (e) { /* shown again on the next change */ }
}

// Stop everything: the assistant's work and the running job (also the Esc key).
function stopAll() {
  if (!state.projectId || !(agentWorking() || (state.ws && state.ws.busy))) return;
  attempt(post(`/api/projects/${state.projectId}/stop`));
  notice("Stopping…");
}

// ---------- slash commands: act at once, without asking the assistant ----------

const COMMANDS = [
  { name: "/prove", args: "[goal]", help: "prove a goal (its lemmas first)", run: (g) => goalAction(g, "prove") },
  { name: "/search", args: "[goal]", help: "let the automatic search prove a goal", run: (g) => goalAction(g, "auto") },
  { name: "/assume", args: "[goal]", help: "assume a lemma for now", run: (g) => applyWorkspace(goalAction(g, "assume", { on: true })) },
  { name: "/open", args: "[goal]", help: "open a goal in the proof panel", run: (g) => zoom(g) },
  { name: "/stop", args: "", help: "stop the assistant and the running job", run: () => stopAll() },
  { name: "/finish", args: "", help: "final Lean check of the whole proof", run: () => finishProof() },
  { name: "/hint", args: "text", help: "a hint for every later plan and proof attempt", run: null },
];

async function runCommand(line) {
  const [name, ...rest] = line.split(/\s+/);
  const argument = rest.join(" ");
  const command = COMMANDS.find((c) => c.name === name);
  if (!command) {
    notice(`Unknown command ${name}. Commands: ${COMMANDS.map((c) => c.name).join(", ")}`, "error");
    return;
  }
  if (name === "/hint") {
    const notes = [...(state.project.notes || []), argument].filter(Boolean);
    state.project.notes = (await attempt(post(`/api/projects/${state.projectId}/notes`, { notes })))?.notes || state.project.notes;
    renderNotes();
    return;
  }
  const goalName = command.args ? (argument || state.focus) : "";
  if (command.args && !state.ws.goals[goalName]) {
    notice(`There is no goal called ${goalName}.`, "error");
    return;
  }
  await attempt(Promise.resolve(command.run(goalName)));
}

function showCommands() {
  const text = $("chat-input").value;
  const mention = text.match(/@([^@\[\]]*)$/);  // "@" and what you typed after it so far
  if (mention) return showMentions(mention[1]);
  const menu = $("commands");
  if (!text.startsWith("/") || text.includes(" ")) return hideCommands();
  const matches = COMMANDS.filter((c) => c.name.startsWith(text));
  menu.innerHTML = "";
  matches.forEach((c) => {
    const item = button("", () => {
      $("chat-input").value = c.name + " ";
      $("chat-input").focus();
      hideCommands();
    }, "command");
    item.append(el("b", "", c.name), el("span", "args", c.args), el("span", "help", c.help));
    menu.append(item);
  });
  menu.classList.toggle("hidden", !matches.length);
}

// "@" in the chat: pick one of your other projects. It goes in as @[name], and the assistant
// knows which project that is (it gets the list of your projects with their names).
async function showMentions(typed) {
  const menu = $("commands");
  const results = (await attempt(loadLibrary())) || [];
  const query = typed.trim().toLowerCase();
  const matches = results.filter((r) => r.name.toLowerCase().includes(query)).slice(0, 6);
  menu.innerHTML = "";
  matches.forEach((r) => {
    const item = button("", () => {
      const input = $("chat-input");
      input.value = input.value.replace(/@([^@\[\]]*)$/, `@[${r.name}] `);
      input.focus();
      hideCommands();
    }, "command mention");
    const name = el("span", "help");
    showText(name, r.name);
    item.append(el("b", "", "@"), name, el("span", "args", r.established ? "established" : "not established"));
    menu.append(item);
  });
  menu.classList.toggle("hidden", !matches.length);
}

function hideCommands() {
  $("commands").classList.add("hidden");
}

// On narrow screens the chat and the proof are two tabs.
function showSide(name) {
  $("split").dataset.side = name;
  $("show-chat").classList.toggle("active", name === "chat");
  $("show-proof").classList.toggle("active", name === "proof");
}

// ---------- the proof panel ----------

function showProofTab(name) {
  ["tree", "library", "final"].forEach((tab) => {
    $(`tab-${tab}`).classList.toggle("active", tab === name);
    $(`pane-${tab}`).classList.toggle("hidden", tab !== name);
  });
  if (name === "library") {
    state.library = null;  // load it fresh: other projects may have changed
    renderLibrary();
  }
  $("breadcrumb").classList.toggle("hidden", name !== "tree");
}

function renderNotes() {
  const box = $("notes");
  const notes = state.ws && state.project ? (state.project.notes || []) : [];
  box.classList.toggle("hidden", !notes.length);
  box.innerHTML = "";
  if (!notes.length) return;
  box.append(el("span", "field-label", "Your hints"));
  notes.forEach((n) => box.append(el("span", "note-chip", n)));
  box.append(button("Clear", async () => {
    try {
      state.project.notes = (await post(`/api/projects/${state.projectId}/notes`, { notes: [] })).notes;
    } catch (e) { notice(e.message, "error"); }
    render();
  }, "btn tiny"));
}

// Run a request; show its error in the chat instead of failing silently.
async function attempt(promise) {
  try {
    return await promise;
  } catch (e) {
    notice(e.message, "error");
    return null;
  }
}

function goal(name) {
  return state.ws.goals[name];
}

// The chain of goals from the theorem down to `name`.
function pathTo(name) {
  const path = [name];
  let current = name;
  for (;;) {
    const parent = Object.values(state.ws.goals).find((g) => g.children.includes(current));
    if (!parent) return path;
    path.unshift(parent.name);
    current = parent.name;
  }
}

function zoom(name) {
  state.focus = name;
  state.editing = null;
  showProofTab("tree");
  render();
  $("proof-pane").scrollTop = 0;
}

function renderBreadcrumb() {
  const crumbs = $("breadcrumb");
  crumbs.innerHTML = "";
  pathTo(state.focus).forEach((name, i, path) => {
    if (i > 0) crumbs.append(el("span", "sep", "›"));
    const label = name === "main" ? "Theorem" : name;
    crumbs.append(i === path.length - 1 ? el("span", "here", label) : button(label, () => zoom(name), ""));
  });
}

function renderOutline() {
  const list = $("outline");
  list.innerHTML = "";
  const walk = (name, depth) => {
    const g = goal(name);
    const b = button("", () => zoom(name), name === state.focus ? "selected" : "");
    b.style.paddingLeft = `${8 + depth * 16}px`;
    b.append(el("i", "dot " + g.status), el("span", "", name === "main" ? "Theorem" : name));
    b.title = `${name}: ${g.status}`;
    const li = el("li");
    li.append(b);
    list.append(li);
    g.children.forEach((child) => walk(child, depth + 1));
  };
  walk("main", 0);
}

const STATUS_TEXT = {
  open: "open", working: "working…", proved: "proved", assumed: "assumed for now", stuck: "stuck", refuted: "false",
};

function statusBadge(g) {
  return statusBadgeFor(g.status, g);
}

function leanCode(name, statement) {
  const code = el("code");
  code.innerHTML = `<span class="kw">${name === "main" ? "theorem" : "lemma"}</span> ${escapeHtml(name)} ${escapeHtml(statement)}`;
  return code;
}

function renderFocus() {
  const g = goal(state.focus);
  const busyNow = state.ws.busy;
  const box = $("focus");
  box.innerHTML = "";

  // --- heading
  const head = el("div", "goal-head");
  head.append(el("span", "kicker", g.name === "main" ? "Theorem" : "Lemma"));
  const goalName = el("span", "goal-name", g.name);
  head.append(goalName, statusBadge(g));
  if (g.name !== "main" && !busyNow) head.append(button("Rename", () => renameGoal(g, goalName), "btn tiny"));
  const path = pathTo(g.name);
  if (path.length > 1) {
    const parent = path[path.length - 2];
    head.append(button(`↑ Zoom out to ${parent === "main" ? "the theorem" : parent}`, () => zoom(parent), "btn tiny zoom-out"));
  }
  box.append(head);

  // --- the statement: Lean first, then the same thing in LaTeX
  const statement = el("div", "statement");
  const leanRow = el("div", "statement-row lean");
  leanRow.append(el("span", "lang", "Lean"), leanCode(g.name, g.lean));
  if (g.name !== "main" && !busyNow) leanRow.append(button("Edit", () => editStatement(g, leanRow), "btn tiny"));
  else leanRow.append(el("span"));
  const latexRow = el("div", "statement-row latex");
  const latex = el("div", "math");
  if (g.latex) showText(latex, g.latex);
  else latex.append(el("span", "muted-text", "(translating from the Lean…)"));
  latexRow.append(el("span", "lang", "LaTeX"), latex, el("span"));
  statement.append(leanRow, latexRow);
  box.append(statement);

  if (g.name === "main" && state.project.established) {
    box.append(el("div", "ok-box", "✓ Established: the whole proof passed Lean's final check. " +
      "Other projects can now use this theorem as a lemma."));
  }
  if (g.established_by) box.append(citedNote(g));
  if (g.assumes.length && g.status === "proved") {
    box.append(el("div", "warn-box", `Proved, but only if these assumed lemmas hold: ${g.assumes.join(", ")}. Prove them to finish.`));
  }
  if (g.feedback && ["stuck", "refuted"].includes(g.status)) box.append(el("div", "error-box", "Lean: " + g.feedback));

  // --- what you can do with this goal
  const actions = el("div", "goal-actions");
  const proveLabel = g.children.length ? "Prove its lemmas, then it" : "Prove it";
  const prove = button(proveLabel, () => attempt(goalAction(g.name, "prove")), "btn primary");
  const auto = button("Let the search find a proof", () => attempt(goalAction(g.name, "auto")));
  const assumeLabel = g.status === "assumed" ? "Stop assuming it" : "Assume it for now";
  const assume = button(assumeLabel, () => applyWorkspace(goalAction(g.name, "assume", { on: g.status !== "assumed" })));
  const cite = button("Use a previous result", () => showProofTab("library"));
  [prove, auto, assume, cite].forEach((b) => { b.disabled = busyNow; });
  if (g.status === "proved") prove.disabled = auto.disabled = cite.disabled = true;
  if (g.name === "main" || g.status === "proved") assume.disabled = true;
  actions.append(prove, auto, assume);
  if (g.name !== "main") actions.append(cite);
  box.append(actions);

  // --- the plan
  box.append(state.editing && state.editing.goal === g.name ? planEditor(g) : planSection(g, busyNow));
  if (state.options[g.name]) box.append(optionsSection(g, busyNow));
  if (!g.children.length && g.status === "proved" && g.proof) box.append(proofSection(g));
  if (g.attempts.length) box.append(attemptsSection(g));
}

function planSection(g, busyNow) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.append(el("h3", "", "Plan"));
  const buttons = el("div", "goal-actions");
  buttons.style.marginTop = "0";
  const suggest = button(g.children.length ? "Suggest other plans" : "Suggest plans", () => suggestPlans(g, suggest), "btn tiny");
  suggest.dataset.suggest = g.name;
  const edit = button(g.children.length ? "Edit plan" : "Write a plan", () => startEditing(g), "btn tiny");
  const remove = button("Remove plan", () => applyWorkspace(goalAction(g.name, "clear-plan")), "btn tiny danger");
  [suggest, edit, remove].forEach((b) => { b.disabled = busyNow || g.status === "proved"; });
  buttons.append(suggest, edit);
  if (g.children.length) buttons.append(remove);
  head.append(buttons);
  section.append(head);

  if (!g.children.length && !g.sketch) {
    section.append(el("p", "muted-text",
      "No plan yet. A goal can be proved directly, or split into smaller lemmas that are proved one at a time. " +
      "Ask for suggestions, write your own, or ask in the chat."));
    return section;
  }
  if (g.sketch) section.append(el("p", "idea", g.sketch));

  const list = el("ol", "lemma-list");
  g.children.forEach((name) => list.append(lemmaCard(goal(name), busyNow)));
  section.append(list);

  const main = el("div", "main-proof");
  main.append(el("span", "field-label", `How the lemmas prove ${g.name === "main" ? "the theorem" : g.name} (Lean)`));
  const pre = el("pre");
  pre.innerHTML = highlightLean(g.proof || "(no main proof yet)");
  main.append(pre);
  section.append(main);

  if (g.problems.length) section.append(el("div", "error-box", "This plan doesn't fit together yet:\n" + g.problems.join("\n")));
  else section.append(el("div", "ok-box", "✓ Lean checked: if the lemmas are true, this proof of the goal works."));
  return section;
}

function lemmaCard(lemma, busyNow) {
  const card = el("li", "lemma-card " + lemma.status);
  const title = el("div", "lemma-title");
  title.append(el("b", "", lemma.name), statusBadge(lemma));
  if (lemma.children.length) title.append(el("span", "muted-text", `· ${lemma.children.length} lemmas inside`));
  const latex = el("div", "lemma-latex math");
  if (lemma.latex) showText(latex, lemma.latex);
  const code = leanCode(lemma.name, lemma.lean);
  code.className = "lemma-lean";
  card.append(title, code, latex);
  if (lemma.established_by) card.append(citedNote(lemma));
  if (lemma.feedback && ["stuck", "refuted"].includes(lemma.status)) card.append(el("div", "lemma-feedback", lemma.feedback));
  const buttons = el("div", "lemma-buttons");
  buttons.append(button("Zoom in →", () => zoom(lemma.name), "btn tiny"));
  if (!["proved", "assumed"].includes(lemma.status)) {
    const prove = button("Prove", () => attempt(goalAction(lemma.name, "prove")), "btn tiny");
    const assume = button("Assume for now", () => applyWorkspace(goalAction(lemma.name, "assume", { on: true })), "btn tiny");
    prove.disabled = assume.disabled = busyNow;
    buttons.append(prove, assume);
  }
  card.append(buttons);
  return card;
}

async function suggestPlans(g, suggestButton) {
  suggestButton.disabled = true;
  spinnerText(suggestButton, "Asking for plans…");
  const answer = await attempt(goalAction(g.name, "propose"));
  if (answer) state.options[g.name] = answer.options;
  render();
}

function optionsSection(g, busyNow) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.append(el("h3", "", "Suggested plans"));
  head.append(button("Dismiss", () => { delete state.options[g.name]; render(); }, "btn tiny"));
  section.append(head);
  const grid = el("div", "options-grid");
  state.options[g.name].forEach((option, i) => {
    const fits = option.problems.length === 0;
    const card = el("div", "option" + (fits ? " fits" : ""));
    card.append(el("h4", "", `Plan ${String.fromCharCode(65 + i)}`), el("div", "", option.sketch));
    if (option.lemmas.length) {
      const list = el("ol");
      option.lemmas.forEach((lemma) => {
        const li = el("li");
        const latex = el("div", "math");
        showText(latex, lemma.latex || lemma.lean);
        li.append(latex, el("div", "lemma-lean", `${lemma.name} ${lemma.lean}`));
        list.append(li);
      });
      card.append(list);
    } else {
      card.append(el("p", "muted-text", "No lemmas: a direct proof."));
    }
    card.append(fits ? el("div", "ok-box", "✓ Lean: fits together")
                     : el("div", "error-box", "Lean: doesn't fit yet\n" + option.problems[0]));
    const buttons = el("div", "buttons");
    const use = button("Use this plan", () => usePlan(g, option), "btn tiny primary");
    const edit = button("Edit first", () => startEditing(g, option), "btn tiny");
    use.disabled = edit.disabled = busyNow;
    buttons.append(use, edit);
    card.append(buttons);
    grid.append(card);
  });
  section.append(grid);
  return section;
}

async function usePlan(g, plan) {
  const answer = await attempt(goalAction(g.name, "plan", {
    sketch: plan.sketch, main_proof: plan.main_proof,
    lemmas: plan.lemmas.map((l) => ({ name: l.name, lean: l.lean, latex: l.latex || "", established_by: l.established_by || "" })),
  }));
  if (!answer) return false;
  state.ws = answer.workspace;
  delete state.options[g.name];
  state.editing = null;
  render();
  return true;
}

function startEditing(g, from) {
  const source = from || { sketch: g.sketch, main_proof: g.proof,
                           lemmas: g.children.map((n) => ({ name: n, lean: goal(n).lean, latex: goal(n).latex,
                                                            established_by: goal(n).established_by })) };
  state.editing = { goal: g.name, sketch: source.sketch, main_proof: source.main_proof,
                    lemmas: source.lemmas.map((l) => ({ ...l })) };
  render();
}

function planEditor(g) {
  const draft = state.editing;
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.append(el("h3", "", `Edit the plan for ${g.name === "main" ? "the theorem" : g.name}`));
  section.append(head);
  const editor = el("div", "editor");
  editor.append(el("p", "note",
    "Write each lemma in Lean, as it would follow `lemma name`. When you save, Lean checks the plan " +
    "and the LaTeX is written from your Lean."));

  const idea = el("label", "field");
  const ideaInput = el("textarea");
  ideaInput.rows = 2;
  ideaInput.value = draft.sketch;
  ideaInput.oninput = () => { draft.sketch = ideaInput.value; };
  idea.append(el("span", "field-label", "Idea"), ideaInput);
  editor.append(idea);

  editor.append(el("span", "field-label", "Lemmas, in order (each may use the ones before it)"));
  draft.lemmas.forEach((lemma, i) => {
    const row = el("div", "editor-row");
    const name = el("input");
    name.value = lemma.name;
    name.placeholder = "name";
    name.oninput = () => { lemma.name = name.value; };
    const lean = el("input");
    lean.value = lemma.lean;
    lean.placeholder = "(a b : ℝ) : 0 ≤ (a - b) ^ 2";
    lean.oninput = () => { lemma.lean = lean.value; lemma.latex = ""; };
    if (lemma.established_by) { lean.readOnly = true; lean.className = "readonly"; lean.title = "From an established project"; }
    const remove = button("×", () => { draft.lemmas.splice(i, 1); render(); }, "btn editor-remove");
    remove.title = "Remove this lemma";
    row.append(name, lean, remove);
    editor.append(row);
  });
  editor.append(button("+ Add a lemma", () => {
    draft.lemmas.push({ name: `step_${draft.lemmas.length + 1}`, lean: "", latex: "" });
    render();
  }, "btn tiny"));
  const fromLibrary = el("div", "goal-actions");
  fromLibrary.style.marginTop = "0";
  fromLibrary.append(button("+ Add a result from another project", async () => {
    const results = await attempt(loadLibrary());
    if (!results) return;
    if (!results.length) {
      notice("No other projects yet.", "error");
      return;
    }
    const pick = el("select");
    results.forEach((r) => pick.append(new Option(`${r.name}${r.established ? "" : " (not established yet)"}`, r.id)));
    const add = button("Add", () => {
      const r = results.find((x) => x.id === pick.value);
      draft.lemmas.push({ name: lemmaNameFor(r), lean: r.statement, latex: r.latex, established_by: r.id });
      render();
    }, "btn tiny primary");
    fromLibrary.replaceChildren(pick, add);
  }, "btn tiny"));
  editor.append(fromLibrary);

  const main = el("label", "field");
  const mainInput = el("textarea");
  mainInput.rows = 3;
  mainInput.value = draft.main_proof;
  mainInput.placeholder = "e.g. nlinarith [step_1 a b]";
  mainInput.oninput = () => { draft.main_proof = mainInput.value; };
  main.append(el("span", "field-label", "How the lemmas prove the goal (Lean tactics)"), mainInput);
  editor.append(main);

  const buttons = el("div", "goal-actions");
  const save = button("Save and check with Lean", async () => {
    save.disabled = true;
    spinnerText(save, "Checking…");
    if (!(await usePlan(g, draft))) render();
  }, "btn primary");
  buttons.append(save, button("Cancel", () => { state.editing = null; render(); }));
  editor.append(buttons);
  section.append(editor);
  return section;
}

function editStatement(g, row) {
  row.innerHTML = "";
  const input = el("input");
  input.value = g.lean;
  const save = button("Save", async () => {
    save.disabled = true;
    const answer = await attempt(goalAction(g.name, "statement", { lean: input.value }));
    if (!answer) { save.disabled = false; return; }
    if (answer.errors.length) {
      notice(`Lean rejected the new statement of ${g.name}: ${answer.errors[0]}`, "error");
      save.disabled = false;
      return;
    }
    state.ws = answer.workspace;
    render();
  }, "btn tiny primary");
  row.append(el("span", "lang", "Lean"), input, save);
  input.focus();
}

function proofSection(g) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.append(el("h3", "", "Proof (Lean)"));
  section.append(head);
  const pre = el("pre");
  pre.innerHTML = highlightLean(g.proof);
  section.append(pre);
  return section;
}

function attemptsSection(g) {
  const details = el("details", "attempts");
  details.append(el("summary", "", `Attempts Lean checked for ${g.name} (${g.attempts.length})`));
  g.attempts.slice().reverse().forEach((a) => {
    const item = el("div", "attempt");
    const headLine = el("div", "attempt-head");
    headLine.append(el("b", a.ok ? "ok" : "bad", a.ok ? "accepted" : "rejected"), document.createTextNode(` · from ${a.source}`));
    const pre = el("pre");
    pre.innerHTML = highlightLean(a.proof);
    item.append(headLine, pre);
    if (a.error) item.append(el("div", "error-box", a.error));
    details.append(item);
  });
  return details;
}

async function applyWorkspace(promise) {
  const answer = await attempt(promise);
  if (answer && answer.workspace) state.ws = answer.workspace;
  render();
}

// ---------- citing established results from other projects ----------

async function loadLibrary() {
  if (!state.library) state.library = (await get(`/api/projects/${state.projectId}/library`)).results;
  return state.library;
}

function projectName(id) {
  const p = (state.library || []).find((x) => x.id === id) || state.projects.find((x) => x.id === id);
  return p ? p.name : "another project";
}

function citedNote(g) {
  const established = g.status === "proved";
  const note = el("div", "cited" + (established ? "" : " pending"));
  note.append(document.createTextNode(established ? "✓ From the established project " : "From the project "));
  const link = el("a", "", `“${projectName(g.established_by)}”`);
  link.href = `#project=${g.established_by}`;
  note.append(link);
  if (!established) note.append(document.createTextNode(", assumed until that project is established"));
  return note;
}

// A Lean name for a lemma taken from a project: its name in snake_case, not yet used here.
function lemmaNameFor(project) {
  let base = project.name.normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
  if (!base || /^[0-9]/.test(base)) base = "from_" + (base || project.id);
  let candidate = base;
  for (let i = 2; state.ws.goals[candidate]; i++) candidate = `${base}_${i}`;
  return candidate;
}

async function renderLibrary() {
  const box = $("library");
  let results;
  try {
    results = await loadLibrary();
  } catch (e) {
    box.replaceChildren(el("p", "muted-text", "Couldn't load your projects: " + e.message));
    return;
  }
  box.innerHTML = "";
  const query = $("library-search").value.trim().toLowerCase();
  const shown = results.filter((r) => !query || `${r.name} ${r.title} ${r.statement}`.toLowerCase().includes(query));
  if (!shown.length) {
    box.append(el("p", "muted-text", results.length ? "No project matches." :
      "No other projects yet. Every theorem you work on becomes one, and can then be used here."));
    return;
  }
  const target = goal(state.focus);
  const targetLabel = target.name === "main" ? "the theorem" : target.name;
  box.append(el("p", "library-target", `Using them for: ${targetLabel} (open another goal in the proof tree to change this)`));
  shown.forEach((r) => {
    const card = el("div", "library-item" + (r.established ? " established" : ""));
    const head = el("div", "library-head");
    const name = el("b");
    showText(name, r.name);
    head.append(name, el("span", "badge " + (r.established ? "established" : "assumed"),
                          r.established ? "established" : "not established yet"));
    const statement = el("div", "math");
    showText(statement, r.latex || r.title);
    const lean = el("div", "lemma-lean", r.statement);
    const buttons = el("div", "lemma-buttons");
    const add = button(`Add as a lemma of ${targetLabel}`, () => addFromLibrary(target, r), "btn tiny primary");
    const prove = button(`Prove ${targetLabel} with it`,
      () => applyWorkspace(goalAction(target.name, "use-established", { project: r.id })), "btn tiny");
    add.disabled = prove.disabled = state.ws.busy || target.status === "proved";
    prove.title = "When it states exactly this goal (its hypotheses may come from the goal's own)";
    buttons.append(add, prove);
    card.append(head, statement, lean, buttons);
    box.append(card);
  });
}

// Put a project's theorem into the plan of `g` as one more lemma (keeping the plan's other lemmas).
async function addFromLibrary(g, project) {
  const lemmas = g.children.map((n) => ({
    name: n, lean: goal(n).lean, latex: goal(n).latex, established_by: goal(n).established_by,
  }));
  const name = lemmaNameFor(project);
  lemmas.push({ name, lean: project.statement, latex: project.latex || "", established_by: project.id });
  if (await usePlan(g, { sketch: g.sketch, main_proof: g.proof, lemmas })) {
    notice(`Added “${project.name}” as the lemma ${name} of ${g.name === "main" ? "the theorem" : g.name}.` +
      (g.proof ? "" : " Now say how the lemmas prove it: Edit plan, or ask the assistant."));
    showProofTab("tree");
  }
}

function renameGoal(g, nameNode) {
  const input = el("input", "rename-input");
  input.value = g.name;
  let done = false;
  const save = async () => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    if (newName && newName !== g.name) {
      const answer = await attempt(goalAction(g.name, "rename", { new_name: newName }));
      if (answer) {
        state.ws = answer.workspace;
        state.focus = newName;
      }
    }
    render();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") { e.stopPropagation(); input.value = g.name; save(); }
  };
  input.onblur = save;
  nameNode.replaceWith(input);
  input.select();
}

function renameProject() {
  const heading = $("project-name-text");
  const input = el("input", "rename-input project-rename");
  input.value = state.project.name === state.project.title ? "" : state.project.name;
  input.placeholder = "Name this project";
  let done = false;
  const save = async () => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    if (name && name !== state.project.name) {
      const answer = await attempt(request("PATCH", `/api/projects/${state.projectId}`, { name }));
      if (answer) state.project.name = answer.name;
    }
    input.replaceWith(heading);
    showText(heading, state.project.name);
    render();
    loadProjects();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") { e.stopPropagation(); input.value = ""; save(); }
  };
  input.onblur = save;
  heading.replaceWith(input);
  input.focus();
}

function suggestFor(name) {
  const b = document.querySelector(`[data-suggest="${name}"]`);
  if (b) b.click();
}

// ---------- the final proof ----------

async function finishProof() {
  $("finish-btn").disabled = true;
  await attempt(post(`/api/projects/${state.projectId}/finish`));
}

function showResult(r) {
  $("final-empty").classList.toggle("hidden", !!r);
  $("final").classList.toggle("hidden", !r);
  if (!r) return;
  $("result-icon").className = "result-icon " + (r.proved ? "ok" : "bad");
  $("result-icon").textContent = r.proved ? "✓" : "✕";
  $("result-title").textContent = r.proved ? "Proved, and checked by Lean" : "The final check failed";
  $("result-sub").textContent = r.proved ? "theorem main " + state.ws.goals.main.lean : (r.message || "");
  if (r.explanation) renderMarkdown($("explanation"), r.explanation);
  else $("explanation").textContent = r.proved ? "" : "Fix what Lean says above, then check again.";
  $("lean-code").innerHTML = highlightLean(r.lean_code || "(no proof)");
  $("saved").textContent = r.folder ? `Also saved in ${r.folder}/` : "";
}

async function copyProof() {
  try {
    await navigator.clipboard.writeText($("lean-code").textContent);
    $("copy-btn").textContent = "Copied";
  } catch (e) {
    $("copy-btn").textContent = "Select and copy";
  }
  setTimeout(() => { $("copy-btn").textContent = "Copy"; }, 1500);
}

// ---------- the divider between the chat and the proof: drag it to give either more room ----------

function setUpDivider() {
  const split = $("split");
  const apply = (fraction) => split.style.setProperty("--chat-fraction", Math.min(0.7, Math.max(0.25, fraction)));
  try { apply(Number(localStorage.getItem("mf-chat-fraction")) || 0.42); } catch (e) { apply(0.42); }
  $("divider").addEventListener("pointerdown", (down) => {
    down.preventDefault();
    const box = split.getBoundingClientRect();
    const move = (e) => apply((e.clientX - box.left) / box.width);
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.body.classList.remove("resizing");
      try { localStorage.setItem("mf-chat-fraction", split.style.getPropertyValue("--chat-fraction")); } catch (e) { /* blocked */ }
    };
    document.body.classList.add("resizing");
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
}

// ---------- wire up the buttons ----------

window.addEventListener("DOMContentLoaded", () => {
  try {
    const saved = localStorage.getItem("mf-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* storage may be blocked */ }
  $("theme-btn").onclick = toggleTheme;
  $("menu-btn").onclick = openSidebar;
  $("scrim").onclick = closeSidebar;

  $("latex").addEventListener("input", updatePreview);
  $("latex").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) formalize(); });
  document.querySelectorAll("#examples .chip").forEach((chip) => {
    chip.onclick = () => { $("latex").value = chip.dataset.latex; updatePreview(); };
  });
  $("formalize-btn").onclick = formalize;
  $("check-btn").onclick = checkEdit;
  $("approve-btn").onclick = approve;

  $("chat-form").onsubmit = (e) => { e.preventDefault(); sendMessage(); };
  $("chat-input").addEventListener("input", showCommands);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    if (e.key === "ArrowUp" && !$("chat-input").value && state.sent.length) {
      e.preventDefault();
      $("chat-input").value = state.sent[state.sent.length - 1];
    }
  });
  $("agent-stop").onclick = stopAll;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { hideCommands(); stopAll(); }
  });
  document.querySelectorAll("#suggestions .chip").forEach((chip) => { chip.onclick = () => sendMessage(chip.dataset.q); });
  $("show-chat").onclick = () => showSide("chat");
  $("show-proof").onclick = () => showSide("proof");
  $("tab-tree").onclick = () => showProofTab("tree");
  $("tab-library").onclick = () => showProofTab("library");
  $("library-search").addEventListener("input", renderLibrary);
  $("project-search").addEventListener("input", renderProjects);
  $("project-name-text").onclick = renameProject;
  setUpDivider();
  $("tab-final").onclick = () => showProofTab("final");
  $("stop-btn").onclick = stopAll;
  $("finish-btn").onclick = finishProof;
  $("copy-btn").onclick = copyProof;

  window.addEventListener("hashchange", route);
  loadProjects();
  route();
});
// KaTeX loads with `defer`, so draw the math again once it is ready.
window.addEventListener("load", () => {
  updatePreview();
  renderProjects();
  if (state.project) {
    showText($("project-title"), state.project.title);
    render();
  }
});
