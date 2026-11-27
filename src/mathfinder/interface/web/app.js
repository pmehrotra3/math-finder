// Math Finder web page. Talks to interface/server.py: REST for actions, a WebSocket for live events.

const $ = (id) => document.getElementById(id);

const state = {
  latex: "",
  sessionId: null,
  plans: [],          // [{sketch, status, reason, lemmas: [{name, statement, status, feedback}]}]
  current: null,      // the plan being worked on right now
  question: null,     // the open question from the search, if any
  paused: false,
  startedAt: 0,
  timer: null,
  attempts: 0,
  attemptsOk: 0,
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

const STEPS = ["write", "confirm", "search", "proof"];
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
  const hints = $("start-hints").value.split("\n").map((h) => h.trim()).filter(Boolean);
  const metric = document.querySelector('input[name="metric"]:checked').value;
  try {
    const { id } = await post("/api/sessions", { statement: $("statement").value, latex: state.latex, hints, metric });
    startWatching(id);
    hints.forEach((h) => addMessage(`hint: ${h}`, "you"));
  } catch (e) {
    $("check-errors").className = "notice bad";
    $("check-errors").textContent = "Error: " + e.message;
  } finally {
    $("approve-btn").disabled = false;
  }
}

// ---------- step 3: the live search ----------

function startWatching(id) {
  Object.assign(state, { sessionId: id, plans: [], current: null, question: null, startedAt: Date.now(),
                         attempts: 0, attemptsOk: 0, paused: false });
  $("log").innerHTML = "";
  $("pause-btn").textContent = "Pause";
  $("search-statement").textContent = "theorem main " + $("statement").value;
  $("step-result").classList.add("hidden");
  setStatus("running");
  $("search-title").textContent = "Searching";
  setStep("search");
  renderTree();
  reveal("step-search");
  clearInterval(state.timer);
  state.timer = setInterval(updateStats, 1000);

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${location.host}/ws/${id}`);
  ws.onmessage = (message) => handleEvent(JSON.parse(message.data));
  ws.onclose = () => clearInterval(state.timer);
}

function elapsed() {
  const s = Math.round((Date.now() - state.startedAt) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function updateStats() {
  $("stat-time").textContent = elapsed();
  $("stat-plans").textContent = state.plans.length;
  $("stat-attempts").textContent = state.attempts;
}

function setStatus(status) {
  const badge = $("search-status");
  badge.textContent = status.replace("_", " ");
  badge.className = "badge " + status;
}

function addMessage(text, who = "sys") {
  const div = document.createElement("div");
  div.className = "msg " + who;
  div.textContent = text;
  $("log").appendChild(div);
  $("log").scrollTop = $("log").scrollHeight;
}

function findLemma(name) {
  return state.current && state.current.lemmas.find((l) => l.name === name);
}

function handleEvent(e) {
  const deep = (e.depth || 0) > 0; // events from a sub-search on a stuck lemma
  switch (e.kind) {
    case "start":
      if (!deep) addMessage("Search started. Trying automation first…");
      break;
    case "refuted":
      addMessage(`The statement is FALSE. Counterexample: ${e.counterexample}`);
      break;
    case "round":
      if (!deep) addMessage(`Round ${e.number}: asking for proof plans…`);
      break;
    case "plan_rejected":
      if (!deep) state.plans.push({ sketch: e.sketch, status: "rejected", reason: e.reason, lemmas: [] });
      break;
    case "plan_start":
      if (deep) { addMessage(`  sub-plan: ${e.sketch}`); break; }
      if (state.current && state.current.status === "active") state.current.status = "failed";
      state.current = { sketch: e.sketch, status: "active", reason: "",
                        lemmas: e.lemmas.map((name) => ({ name, statement: "", status: "open", feedback: "" })) };
      state.plans.push(state.current);
      break;
    case "lemma_start": {
      if (deep) break;
      const l = findLemma(e.name);
      if (l) Object.assign(l, { statement: e.statement, status: "working" });
      break;
    }
    case "subsearch_start":
      addMessage(`${e.name} is stuck: splitting it into smaller steps.`);
      break;
    case "lemma_proved":
    case "lemma_refuted":
    case "lemma_stuck": {
      const status = e.kind.replace("lemma_", "");
      if (deep) { addMessage(`  sub-lemma ${e.name}: ${status}`); break; }
      let l = findLemma(e.name);
      if (!l && state.current) { // a lemma spliced in from a sub-search
        l = { name: e.name, statement: e.statement, status, feedback: "" };
        state.current.lemmas.push(l);
      }
      if (l) Object.assign(l, { statement: e.statement, status, feedback: e.feedback });
      break;
    }
    case "attempt":
    case "optimize_candidate":
      state.attempts += 1;
      if (e.ok) state.attemptsOk += 1;
      break;
    case "plan_failed":
      if (!deep && state.current) state.current.status = "failed";
      break;
    case "done":
      if (deep) break;
      if (state.current) state.current.status = "proved";
      else { // proved directly by automation: there was no plan, so show one node for it
        state.current = { sketch: e.plan, status: "proved", reason: "", lemmas: [] };
        state.plans.push(state.current);
      }
      addMessage("✓ Lean verified a proof.", "good");
      break;
    case "ask_user":
      state.question = e.question;
      $("question").textContent = e.question;
      $("question").classList.remove("hidden");
      $("chat-input").placeholder = "Type your answer…";
      $("chat-input").focus();
      break;
    case "user_answer":
      addMessage("Trying again with your idea.");
      break;
    case "no_answer":
      addMessage("(no answer)");
      break;
    case "stopped":
      addMessage(`Stopped: ${e.reason}`);
      break;
    case "gave_up":
      addMessage("No verified proof found.");
      break;
    case "optimize_start":
      addMessage(`Looking for a better proof (current cost ${e.cost})…`);
      break;
    case "optimized":
      addMessage(e.improved ? `Found a better proof: cost ${e.before.cost} → ${e.after.cost}.`
                            : "Kept the first proof (no better one found).");
      break;
    case "explaining":
      addMessage("Writing the explanation…");
      break;
    case "finished":
      showResult(e);
      break;
  }
  renderTree();
  updateStats();
}

const PLAN_TAGS = { active: "Working", proved: "Proved", failed: "Failed", rejected: "Doesn't fit" };
const DOTS = { proved: "✓", refuted: "✕", stuck: "!", working: "", open: "" };

function renderTree() {
  const tree = $("tree");
  tree.innerHTML = "";
  if (!state.plans.length) {
    tree.innerHTML = '<div class="empty"><span class="spinner"></span>Waiting for the first plans…</div>';
    return;
  }
  state.plans.forEach((plan) => {
    const div = document.createElement("div");
    div.className = "plan " + plan.status;
    div.innerHTML = '<div class="plan-top"><span class="plan-tag"></span><div class="sketch"></div></div>';
    div.querySelector(".plan-tag").textContent = PLAN_TAGS[plan.status] || plan.status;
    div.querySelector(".sketch").textContent = plan.sketch;
    if (plan.reason) {
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = plan.reason;
      div.appendChild(reason);
    }
    if (plan.lemmas.length) {
      const ul = document.createElement("ul");
      ul.className = "lemmas";
      plan.lemmas.forEach((l) => {
        const li = document.createElement("li");
        li.className = "lemma " + l.status;
        li.innerHTML = '<span class="dot"></span><span class="name"></span><span class="stmt"></span><span class="fb"></span>';
        li.querySelector(".dot").textContent = DOTS[l.status] ?? "";
        li.querySelector(".name").textContent = l.name;
        li.querySelector(".stmt").textContent = l.statement;
        li.querySelector(".fb").textContent = l.feedback;
        li.onclick = (event) => openLemmaMenu(event, l, plan);
        ul.appendChild(li);
      });
      div.appendChild(ul);
    }
    tree.appendChild(div);
  });
}

function openLemmaMenu(event, lemma, plan) {
  event.stopPropagation();
  const menu = $("lemma-menu");
  menu.innerHTML = "";
  const choices = [
    ["Give a hint about this lemma", () => {
      $("chat-input").value = `For ${lemma.name} (${lemma.statement}): `;
      $("chat-input").focus();
    }],
    ["Reject it: false or useless", () => send("reject", { name: lemma.name }, `reject ${lemma.name}`)],
    ["Keep this plan's approach", () => send("pin", { text: plan.sketch }, `pin: ${plan.sketch}`)],
  ];
  choices.forEach(([label, onClick]) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => { menu.classList.add("hidden"); onClick(); };
    menu.appendChild(b);
  });
  menu.style.left = `${Math.min(event.pageX, window.innerWidth - 250)}px`;
  menu.style.top = `${event.pageY}px`;
  menu.classList.remove("hidden");
}

async function send(name, body, shown) {
  try {
    await action(name, body);
    addMessage(shown, "you");
  } catch (e) {
    addMessage("Error: " + e.message);
  }
}

async function sendChat() {
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  if (state.question) { // an open question: this is the answer
    state.question = null;
    $("question").classList.add("hidden");
    $("chat-input").placeholder = "Give a hint, e.g. try induction on n";
    await send("answer", { text }, `answer: ${text}`);
  } else {
    await send("hint", { text }, `hint: ${text}`);
  }
}

async function suggestLemma() {
  const name = $("lemma-name").value.trim();
  const statement = $("lemma-statement").value.trim();
  if (!name || !statement) return;
  await send("lemma", { name, statement }, `lemma ${name} : ${statement}`);
  $("lemma-name").value = "";
  $("lemma-statement").value = "";
}

async function togglePause() {
  state.paused = !state.paused;
  await send(state.paused ? "pause" : "resume", {}, state.paused ? "pause" : "resume");
  $("pause-btn").textContent = state.paused ? "Resume" : "Pause";
}

// ---------- step 4: the result ----------

function showResult(e) {
  clearInterval(state.timer);
  setStatus(e.status);
  $("search-title").textContent = "Search";
  const r = e.result || {};
  $("result-icon").className = "result-icon " + (r.proved ? "ok" : "bad");
  $("result-icon").textContent = r.proved ? "✓" : "✕";
  $("result-title").textContent = r.proved ? "Proved, and checked by Lean" : "No proof found";
  $("result-sub").textContent = r.proved ? $("search-statement").textContent : (r.message || "");
  // count the lemmas in the final proof (the optimizer may have removed some)
  const lemmas = r.proved ? Math.max(0, (r.lean_code.match(/^theorem /gm) || []).length - 1) : 0;
  const metrics = [
    [elapsed(), "time"],
    [String(r.rounds ?? 0), "planning rounds"],
    [String(state.plans.length), "plans considered"],
    [String(lemmas), "lemmas used"],
    [`${state.attemptsOk}/${state.attempts}`, "attempts that worked"],
  ];
  if (r.optimization) metrics.push([`${r.optimization.before.cost} → ${r.optimization.after.cost}`, "proof cost"]);
  $("metrics").innerHTML = "";
  metrics.forEach(([v, k]) => {
    const div = document.createElement("div");
    div.className = "metric";
    div.innerHTML = '<div class="v"></div><div class="k"></div>';
    div.querySelector(".v").textContent = v;
    div.querySelector(".k").textContent = k;
    $("metrics").appendChild(div);
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
  ["step-approve", "step-search", "step-result"].forEach((id) => $(id).classList.add("hidden"));
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
  $("send-btn").onclick = sendChat;
  $("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
  $("lemma-btn").onclick = suggestLemma;
  $("pause-btn").onclick = togglePause;
  $("stop-btn").onclick = () => send("stop", {}, "stop");
  $("copy-btn").onclick = copyProof;
  $("again-btn").onclick = startOver;
  $("theme-btn").onclick = toggleTheme;
  document.addEventListener("click", () => $("lemma-menu").classList.add("hidden"));
  try {
    const saved = localStorage.getItem("mf-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* storage may be blocked */ }
  setStep("write");
  updatePreview();
});
// KaTeX loads with `defer`, so render the preview again once it is ready.
window.addEventListener("load", updatePreview);
