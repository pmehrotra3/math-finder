// Math Finder web page. Talks to interface/server.py: REST for actions, a WebSocket for live changes.
//
// 1. Write   - you type a theorem in LaTeX.
// 2. Confirm - it is translated into Lean; you check that it says what you mean.
// 3. Plan & prove - the workspace: the theorem and its lemmas as a tree. You zoom in on one goal,
//    make or edit its plan, prove it, or ask the side assistant a question.
// 4. Proof   - the whole tree goes through Lean's final check and is explained.

const $ = (id) => document.getElementById(id);

const state = {
  latex: "",          // the theorem as you wrote it
  workspaceId: null,
  ws: null,           // the workspace from the server: {root, goals: {name: goal}, busy}
  focus: "main",      // the goal you are looking at (zoomed in on)
  options: {},        // plans the model proposed, per goal: {name: [option, ...]}
  editing: null,      // the plan being edited: {goal, sketch, main_proof, lemmas: [{name, lean}]}
  activity: 0,
};

// ---------- talking to the server ----------

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

function action(name, body = {}) {
  return post(`/api/sessions/${state.sessionId}/${name}`, body);
}

// ---------- small helpers ----------

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMath(el) {
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
    });
  }
}

function showText(el, text) {
  el.textContent = text;
  renderMath(el);
}

// Markdown would mangle LaTeX (it treats _ and \ specially), so we hide the math first,
// let marked do the Markdown, put the math back, and let KaTeX draw it.
function renderMarkdown(el, text) {
  const math = [];
  const hidden = text.replace(/\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/g, (m) => {
    math.push(m);
    return `@@MATH${math.length - 1}@@`;
  });
  let html = window.marked ? marked.parse(escapeHtml(hidden)) : `<pre>${escapeHtml(hidden)}</pre>`;
  html = html.replace(/@@MATH(\d+)@@/g, (_, i) => escapeHtml(math[Number(i)]));
  el.innerHTML = html;
  renderMath(el);
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

const STEPS = ["write", "confirm", "workspace", "proof"];
function setStep(step) {
  const now = STEPS.indexOf(step);
  document.querySelectorAll("#stepper li").forEach((li) => {
    const i = STEPS.indexOf(li.dataset.step);
    li.classList.toggle("done", i < now);
    li.classList.toggle("current", i === now);
  });
}

function reveal(id) {
  $(id).classList.remove("hidden");
  $(id).scrollIntoView({ behavior: "smooth", block: "start" });
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

function busy(el, text) {
  el.innerHTML = text ? `<span class="spinner"></span><span></span>` : "";
  if (text) el.lastChild.textContent = text;
}

// ---------- theme ----------

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("mf-theme", theme); } catch (e) { /* storage may be blocked */ }
}

function toggleTheme() {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  setTheme(dark ? "light" : "dark");
}

// ---------- step 1: write the theorem ----------

function updatePreview() {
  const text = $("latex").value.trim();
  if (text) showText($("preview"), text);
  else $("preview").innerHTML = '<span class="field-label">Your theorem appears here</span>';
}

async function formalize() {
  state.latex = $("latex").value.trim();
  if (!state.latex) return;
  $("formalize-btn").disabled = true;
  busy($("formalize-status"), "Translating to Lean and checking it…");
  try {
    const f = await post("/api/formalize", { latex: state.latex });
    if (!f.ok) {
      $("formalize-status").textContent = "Lean couldn't accept any translation: " + (f.errors[0] || "");
      return;
    }
    busy($("formalize-status"), "");
    $("statement").value = f.statement;
    showText($("back"), f.back_translation);
    const v = $("verdict");
    v.className = "notice " + (f.faithful ? "ok" : "warn");
    v.textContent = (f.faithful ? "✓ Same meaning. " : "⚠ The meaning may differ. ") + f.judgment;
    $("check-errors").classList.add("hidden");
    setStep("confirm");
    reveal("step-approve");
  } catch (e) {
    $("formalize-status").textContent = "Error: " + e.message;
  } finally {
    $("formalize-btn").disabled = false;
  }
}

// ---------- step 2: confirm ----------

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
    const { id, workspace } = await post("/api/workspaces", { statement: $("statement").value, latex: state.latex });
    openWorkspace(id, workspace);
  } catch (e) {
    $("check-errors").className = "notice bad";
    $("check-errors").textContent = "Error: " + e.message;
  } finally {
    $("approve-btn").disabled = false;
  }
}

// ---------- step 3: the workspace ----------

function openWorkspace(id, workspace) {
  Object.assign(state, { workspaceId: id, ws: workspace, focus: "main", options: {}, editing: null, activity: 0 });
  $("activity").innerHTML = "";
  $("conversation").innerHTML = "";
  $("step-result").classList.add("hidden");
  setStep("workspace");
  render();
  reveal("step-workspace");

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/${id}`);
  socket.onmessage = (message) => handleEvent(JSON.parse(message.data));
  socket.onclose = () => addActivity("Connection to the server closed.", "error");
}

function goalAction(name, action, body = {}) {
  return post(`/api/workspaces/${state.workspaceId}/goals/${encodeURIComponent(name)}/${action}`, body);
}

function handleEvent(e) {
  if (e.workspace) state.ws = e.workspace;
  switch (e.kind) {
    case "log":
      addActivity(e.text);
      break;
    case "attempt":
      addActivity(`${e.ok ? "✓" : "✗"} proof attempt for ${e.name}${e.source ? ` (${e.source})` : ""}`);
      break;
    case "error":
      addActivity(e.text, "error");
      break;
    case "finished":
      showResult(e.result);
      break;
  }
  render();
}

function addActivity(text, className = "") {
  const li = el("li", className);
  li.append(el("time", "", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), el("span", "", text));
  $("activity").prepend(li);
  state.activity += 1;
  $("activity-count").textContent = state.activity;
}

// Run a request; show its error in the activity list instead of failing silently.
async function attempt(promise) {
  try {
    return await promise;
  } catch (e) {
    addActivity(e.message, "error");
    showTab("activity");
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
  render();
}

function render() {
  if (!state.ws) return;
  if (!state.ws.goals[state.focus]) state.focus = "main";
  renderBar();
  renderOutline();
  renderFocus();
}

function renderBar() {
  const crumbs = $("breadcrumb");
  crumbs.innerHTML = "";
  pathTo(state.focus).forEach((name, i, path) => {
    if (i > 0) crumbs.append(el("span", "sep", "›"));
    const label = name === "main" ? "Theorem" : name;
    crumbs.append(i === path.length - 1 ? el("span", "here", label) : button(label, () => zoom(name), ""));
  });
  const busyNow = state.ws.busy;
  $("job").classList.toggle("hidden", !busyNow);
  $("stop-btn").classList.toggle("hidden", !busyNow);
  const root = goal("main");
  $("finish-btn").disabled = busyNow || root.status !== "proved" || root.assumes.length > 0;
}

function renderOutline() {
  const list = $("outline");
  list.innerHTML = "";
  const walk = (name, depth) => {
    const g = goal(name);
    const b = button("", () => zoom(name), name === state.focus ? "selected" : "");
    b.style.paddingLeft = `${8 + depth * 14}px`;
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
  const text = g.status === "proved" && g.assumes.length ? "proved, with assumptions" : STATUS_TEXT[g.status];
  return el("span", "badge " + (g.status === "proved" && g.assumes.length ? "assumed" : g.status), text);
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
  head.append(el("span", "goal-name", g.name === "main" ? "main" : g.name), statusBadge(g));
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
  [prove, auto, assume].forEach((b) => { b.disabled = busyNow; });
  if (g.status === "proved") prove.disabled = auto.disabled = true;
  if (g.name === "main" || g.status === "proved") assume.disabled = true;
  actions.append(prove, auto, assume);
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
      "Ask for suggestions or write your own."));
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
  card.append(title, leanCode(lemma.name, lemma.lean), latex);
  card.querySelector("code").className = "lemma-lean";
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
  busy(suggestButton, "Asking for plans…");
  addActivity(`Asking for plans for ${g.name}…`);
  const answer = await attempt(goalAction(g.name, "propose"));
  if (answer) {
    state.options[g.name] = answer.options;
    addActivity(`${answer.options.length} plans proposed for ${g.name}.`);
  }
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
  addActivity(`Using a plan for ${g.name}; Lean is checking it and the LaTeX is being written…`);
  const answer = await attempt(goalAction(g.name, "plan", {
    sketch: plan.sketch, main_proof: plan.main_proof,
    lemmas: plan.lemmas.map((l) => ({ name: l.name, lean: l.lean, latex: l.latex || "" })),
  }));
  if (!answer) return false;
  state.ws = answer.workspace;
  delete state.options[g.name];
  state.editing = null;
  addActivity(answer.problems.length ? `The plan for ${g.name} doesn't fit together yet.` : `Plan for ${g.name} saved; Lean says it fits.`);
  render();
  return true;
}

function startEditing(g, from) {
  const source = from || { sketch: g.sketch, main_proof: g.proof,
                           lemmas: g.children.map((n) => ({ name: n, lean: goal(n).lean, latex: goal(n).latex })) };
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
    const remove = button("×", () => { draft.lemmas.splice(i, 1); render(); }, "btn editor-remove");
    remove.title = "Remove this lemma";
    row.append(name, lean, remove);
    editor.append(row);
  });
  editor.append(button("+ Add a lemma", () => {
    draft.lemmas.push({ name: `step_${draft.lemmas.length + 1}`, lean: "", latex: "" });
    render();
  }, "btn tiny"));

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
    busy(save, "Checking…");
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
    if (!answer) return;
    if (answer.errors.length) {
      addActivity(`Lean rejected the new statement of ${g.name}: ${answer.errors[0]}`, "error");
      showTab("activity");
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
  section.append(el("div", "section-head")).append(el("h3", "", "Proof (Lean)"));
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

async function applyWorkspace(request) {
  const answer = await attempt(request);
  if (answer && answer.workspace) state.ws = answer.workspace;
  render();
}

// ---------- the side assistant ----------

function showTab(name) {
  ["ask", "activity"].forEach((tab) => {
    $(`tab-${tab}`).classList.toggle("active", tab === name);
    $(`pane-${tab}`).classList.toggle("hidden", tab !== name);
  });
}

async function askQuestion(text) {
  const question = (text || $("ask-input").value).trim();
  if (!question || !state.workspaceId) return;
  $("ask-input").value = "";
  const focusNote = state.focus === "main" ? "" : ` (I am looking at ${state.focus}.)`;
  $("conversation").append(el("div", "bubble you", question));
  const answerBubble = el("div", "bubble assistant");
  busy(answerBubble, "Thinking…");
  $("conversation").append(answerBubble);
  answerBubble.scrollIntoView({ block: "nearest" });
  $("ask-btn").disabled = true;
  try {
    const { answer } = await post(`/api/workspaces/${state.workspaceId}/ask`, { question: question + focusNote });
    renderMarkdown(answerBubble, answer);
  } catch (e) {
    answerBubble.textContent = "Couldn't answer: " + e.message;
  } finally {
    $("ask-btn").disabled = false;
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }
}

async function finishProof() {
  $("finish-btn").disabled = true;
  await attempt(post(`/api/workspaces/${state.workspaceId}/finish`));
}

// ---------- step 4: the result ----------

function showResult(r) {
  $("result-icon").className = "result-icon " + (r.proved ? "ok" : "bad");
  $("result-icon").textContent = r.proved ? "✓" : "✕";
  $("result-title").textContent = r.proved ? "Proved, and checked by Lean" : "The final check failed";
  $("result-sub").textContent = r.proved ? "theorem main " + goal("main").lean : (r.message || "");
  const lemmaCount = Object.keys(state.ws.goals).length - 1;
  const attempts = Object.values(state.ws.goals).reduce((n, g) => n + g.attempts.length, 0);
  const metrics = [
    [String(lemmaCount), "lemmas"],
    [String(Math.max(...Object.keys(state.ws.goals).map((n) => pathTo(n).length)) - 1), "levels deep"],
    [String(attempts), "attempts Lean checked"],
  ];
  $("metrics").innerHTML = "";
  metrics.forEach(([value, label]) => {
    const tile = el("div", "metric");
    tile.append(el("div", "v", value), el("div", "k", label));
    $("metrics").append(tile);
  });
  if (r.explanation) renderMarkdown($("explanation"), r.explanation);
  else $("explanation").textContent = r.message || "";
  $("lean-code").innerHTML = highlightLean(r.lean_code || "(no proof)");
  $("saved").textContent = r.folder ? `Saved in ${r.folder}/` : "";
  setStep("proof");
  reveal("step-result");
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

function startOver() {
  ["step-approve", "step-workspace", "step-result"].forEach((id) => $(id).classList.add("hidden"));
  setStep("write");
  window.scrollTo({ top: 0, behavior: "smooth" });
  $("latex").focus();
}

// ---------- wire up the buttons ----------

window.addEventListener("DOMContentLoaded", () => {
  $("latex").addEventListener("input", updatePreview);
  $("latex").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) formalize(); });
  document.querySelectorAll("#examples .chip").forEach((chip) => {
    chip.onclick = () => { $("latex").value = chip.dataset.latex; updatePreview(); };
  });
  $("formalize-btn").onclick = formalize;
  $("check-btn").onclick = checkEdit;
  $("approve-btn").onclick = approve;
  $("stop-btn").onclick = () => attempt(post(`/api/workspaces/${state.workspaceId}/stop`));
  $("finish-btn").onclick = finishProof;
  $("tab-ask").onclick = () => showTab("ask");
  $("tab-activity").onclick = () => showTab("activity");
  $("ask-form").onsubmit = (e) => { e.preventDefault(); askQuestion(); };
  $("ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askQuestion(); }
  });
  document.querySelectorAll("#suggestions .chip").forEach((chip) => { chip.onclick = () => askQuestion(chip.dataset.q); });
  $("copy-btn").onclick = copyProof;
  $("again-btn").onclick = startOver;
  $("theme-btn").onclick = toggleTheme;
  try {
    const saved = localStorage.getItem("mf-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* storage may be blocked */ }
  setStep("write");
  updatePreview();
});
// KaTeX loads with `defer`, so render the preview again once it is ready.
window.addEventListener("load", () => { updatePreview(); render(); });
