// Math Finder web page. Talks to mathfinder/server.py: REST for actions, a WebSocket for live events.

const $ = (id) => document.getElementById(id);

const state = {
  latex: "",
  sessionId: null,
  plans: [],          // [{sketch, status, reason, lemmas: [{name, statement, status, feedback}]}]
  current: null,      // the plan being worked on right now
  question: null,     // the open question from the search, if any
  paused: false,
  startedAt: 0,
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

// ---------- math and markdown ----------

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

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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

function showText(el, text) {
  el.textContent = text;
  renderMath(el);
}

// ---------- step 1: LaTeX in, live preview ----------

function updatePreview() {
  showText($("preview"), $("latex").value);
}

async function formalize() {
  state.latex = $("latex").value.trim();
  $("formalize-btn").disabled = true;
  $("formalize-status").textContent = "Translating to Lean and checking it (about 30 s)…";
  try {
    const f = await post("/api/formalize", { latex: state.latex });
    if (!f.ok) {
      $("formalize-status").textContent = "Lean couldn't accept any translation: " + (f.errors[0] || "");
      return;
    }
    $("formalize-status").textContent = "";
    $("statement").value = f.statement;
    showText($("back"), f.back_translation);
    const v = $("verdict");
    v.className = "verdict " + (f.faithful ? "ok" : "warn");
    v.textContent = (f.faithful ? "✓ Same meaning: " : "⚠ The meaning may differ: ") + f.judgment;
    $("check-errors").classList.add("hidden");
    $("step-approve").classList.remove("hidden");
    $("step-approve").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("formalize-status").textContent = "Error: " + e.message;
  } finally {
    $("formalize-btn").disabled = false;
  }
}

// ---------- step 2: approve ----------

async function checkEdit() {
  const box = $("check-errors");
  const { errors } = await post("/api/check", { statement: $("statement").value });
  box.classList.toggle("hidden", errors.length === 0);
  box.textContent = errors.length ? "Lean rejected it:\n" + errors[0] : "";
  if (!errors.length) {
    box.classList.remove("hidden");
    box.textContent = "✓ Lean accepts this statement.";
  }
  return errors.length === 0;
}

async function approve() {
  $("approve-btn").disabled = true;
  const hints = $("start-hints").value.split("\n").map((h) => h.trim()).filter(Boolean);
  const metric = $("metric").value;
  const { id } = await post("/api/sessions", { statement: $("statement").value, latex: state.latex, hints, metric });
  startWatching(id);
  hints.forEach((h) => addMessage(`hint: ${h}`, "you"));
}

// ---------- step 3: the live search ----------

function startWatching(id) {
  Object.assign(state, { sessionId: id, plans: [], current: null, question: null, startedAt: Date.now(),
                         attempts: 0, attemptsOk: 0, paused: false });
  $("log").innerHTML = "";
  $("step-result").classList.add("hidden");
  $("step-search").classList.remove("hidden");
  setStatus("running");
  renderTree();
  $("step-search").scrollIntoView({ behavior: "smooth" });

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${location.host}/ws/${id}`);
  ws.onmessage = (message) => handleEvent(JSON.parse(message.data));
  ws.onclose = () => addMessage("(connection closed)", "sys");
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
      if (!deep) addMessage("Search started.");
      break;
    case "refuted":
      addMessage(`The statement is FALSE. Counterexample: ${e.counterexample}`);
      break;
    case "round":
      if (!deep) addMessage(`Round ${e.number}: asking the planner…`);
      break;
    case "plan_rejected":
      if (!deep) state.plans.push({ sketch: e.sketch, status: "rejected", reason: e.reason, lemmas: [] });
      break;
    case "plan_start":
      if (deep) { addMessage(`  sub-plan: ${e.sketch}`); break; }
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
      addMessage(`${e.name} is stuck: starting a smaller search on it.`);
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
      addMessage("✓ Lean verified a proof.");
      break;
    case "ask_user":
      state.question = e.question;
      $("question").textContent = "❓ " + e.question;
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
      addMessage(`Looking for a better proof (${e.metric}, current cost ${e.cost})…`);
      break;
    case "optimize_candidate":
      state.attempts += 1;
      if (e.ok) state.attemptsOk += 1;
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
}

function renderTree() {
  const tree = $("tree");
  tree.innerHTML = "";
  if (!state.plans.length) {
    tree.innerHTML = '<div class="muted">Waiting for the first plans…</div>';
    return;
  }
  state.plans.forEach((plan) => {
    const div = document.createElement("div");
    div.className = "plan " + plan.status;
    const sketch = document.createElement("div");
    sketch.className = "sketch";
    sketch.textContent = plan.sketch;
    div.appendChild(sketch);
    if (plan.reason) {
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = plan.reason;
      div.appendChild(reason);
    }
    const ul = document.createElement("ul");
    ul.className = "lemmas";
    plan.lemmas.forEach((l) => {
      const li = document.createElement("li");
      li.className = "lemma " + l.status;
      li.innerHTML = '<span class="name"></span> <span class="stmt"></span><div class="fb"></div>';
      li.querySelector(".name").textContent = l.name;
      li.querySelector(".stmt").textContent = l.statement;
      li.querySelector(".fb").textContent = l.feedback;
      li.onclick = (event) => openLemmaMenu(event, l, plan);
      ul.appendChild(li);
    });
    div.appendChild(ul);
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
    ["Reject: false or useless", () => send("reject", { name: lemma.name }, `reject ${lemma.name}`)],
    ["Pin this plan's approach", () => send("pin", { text: plan.sketch }, `pin: ${plan.sketch}`)],
  ];
  choices.forEach(([label, onClick]) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => { menu.classList.add("hidden"); onClick(); };
    menu.appendChild(b);
  });
  menu.style.left = `${event.pageX}px`;
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
    $("chat-input").placeholder = "Type a hint, e.g. try induction on n";
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
  setStatus(e.status);
  const r = e.result || {};
  $("result-title").textContent = r.proved ? "Proved, and verified by Lean" : "Not proved";
  const seconds = Math.round((Date.now() - state.startedAt) / 1000);
  const lemmas = state.current && r.proved ? state.current.lemmas.length : 0;
  const metrics = [
    [`${seconds} s`, "time"],
    [String(r.rounds ?? 0), "planning rounds"],
    [String(state.plans.length), "plans considered"],
    [String(lemmas), "lemmas in the proof"],
    [`${state.attemptsOk}/${state.attempts}`, "proof attempts that worked"],
  ];
  if (r.optimization) {
    const o = r.optimization;
    metrics.push([`${o.before.cost} → ${o.after.cost}`, `proof cost (${o.metric})`]);
  }
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
  $("lean-code").textContent = r.lean_code || "(no proof)";
  $("saved").textContent = r.folder ? `Saved in ${r.folder}` : "";
  $("step-result").classList.remove("hidden");
  $("step-result").scrollIntoView({ behavior: "smooth" });
}

// ---------- wire up the buttons ----------

window.addEventListener("DOMContentLoaded", () => {
  $("latex").addEventListener("input", updatePreview);
  $("formalize-btn").onclick = formalize;
  $("check-btn").onclick = checkEdit;
  $("approve-btn").onclick = approve;
  $("send-btn").onclick = sendChat;
  $("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
  $("lemma-btn").onclick = suggestLemma;
  $("pause-btn").onclick = togglePause;
  $("stop-btn").onclick = () => send("stop", {}, "stop");
  document.addEventListener("click", () => $("lemma-menu").classList.add("hidden"));
  updatePreview();
});
