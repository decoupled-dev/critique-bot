// crit desktop page. Everything from agents goes in with textContent; the only
// HTML built here is from fixed strings.
(function () {
  "use strict";
  const crit = window.crit;
  const $ = (id) => document.getElementById(id);

  const projects = new Map(); // id -> ProjectSummary
  const agents = new Map(); // id -> AgentSummary
  const frames = new Map(); // agent id -> iframe
  const terms = new Map(); // agent id -> { term, node }
  let selected = null; // agent id, or null for the overview

  // ------------------------------------------------------------ helpers

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  const STATE_LABEL = {
    starting: "starting",
    idle: "idle",
    working: "working",
    waiting: "needs you",
    done: "done",
    failed: "failed",
    stopped: "stopped",
  };

  function elapsed(agent) {
    const s = agent.status;
    if (!s.taskStartedAt) return "";
    const end = s.taskEndedAt || Date.now();
    const sec = Math.max(0, Math.round((end - s.taskStartedAt) / 1000));
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ${String(sec % 60).padStart(2, "0")}s`;
    return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, "0")}m`;
  }

  let toastTimer = 0;
  function toast(text, bad) {
    const node = $("toast");
    node.textContent = text;
    node.className = "toast" + (bad ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), bad ? 7000 : 3500);
  }

  function errorText(err) {
    return String((err && err.message) || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
  }

  function sortedAgents() {
    const order = { waiting: 0, working: 1, starting: 2, failed: 3, done: 4, idle: 5, stopped: 6 };
    return [...agents.values()].sort(
      (a, b) => order[a.status.state] - order[b.status.state] || b.createdAt - a.createdAt,
    );
  }

  // ------------------------------------------------------------ top bar

  function renderProjects() {
    const box = $("projects");
    box.textContent = "";
    for (const p of projects.values()) {
      const chip = el("div", "project-chip");
      chip.title = p.path + (p.host.message ? `\n${p.host.message}` : "");
      chip.appendChild(el("span", "", p.name));
      if (p.host.state !== "off") {
        const label = p.host.state === "starting" ? "starting…" : p.host.state === "failed" ? "error" : p.host.local ? "local model" : "browser";
        chip.appendChild(el("span", "host " + p.host.state, label));
      }
      const close = el("button", "", "✕");
      close.title = `Close ${p.name} (stops its agents)`;
      close.addEventListener("click", () => {
        const running = [...agents.values()].filter((a) => a.projectId === p.id && a.running).length;
        if (running && !confirm(`Close ${p.name}? This stops ${running} running agent(s).`)) return;
        crit.closeProject(p.id);
      });
      chip.appendChild(close);
      box.appendChild(chip);
    }
    const select = $("new-project");
    const current = select.value;
    select.textContent = "";
    for (const p of projects.values()) {
      const option = el("option", "", p.name);
      option.value = p.id;
      select.appendChild(option);
    }
    if (current && projects.has(current)) select.value = current;
  }

  function renderCounts() {
    const list = [...agents.values()];
    const count = (state) => list.filter((a) => a.status.state === state).length;
    const box = $("counts");
    box.textContent = "";
    const add = (n, label, cls) => {
      if (!n) return;
      const span = el("span", cls || "");
      span.appendChild(el("b", "", n));
      span.appendChild(document.createTextNode(" " + label));
      box.appendChild(span);
    };
    add(count("working") + count("starting"), "working");
    add(count("waiting"), "need you", "waiting");
    add(count("done"), "done");
    add(count("failed"), "failed");
    const waiting = count("waiting");
    document.title = waiting ? `(${waiting}) crit` : "crit";
  }

  // ------------------------------------------------------------ agent list

  function renderList() {
    const box = $("agent-list");
    box.textContent = "";
    for (const p of projects.values()) {
      const mine = sortedAgents().filter((a) => a.projectId === p.id);
      if (projects.size > 1 || !mine.length) box.appendChild(el("div", "group-title", p.name));
      for (const a of mine) box.appendChild(agentItem(a));
    }
    $("overview-link").classList.toggle("selected", selected === null);
  }

  function agentItem(a) {
    const item = el("button", `agent-item state-${a.status.state}` + (a.id === selected ? " selected" : ""));
    item.dataset.id = a.id;
    const line1 = el("div", "line1");
    line1.appendChild(el("span", "dot " + a.status.state));
    line1.appendChild(el("span", "name", a.name));
    if (a.status.state === "waiting") line1.appendChild(el("span", "badge", "needs you"));
    else line1.appendChild(el("span", "time", elapsed(a)));
    item.appendChild(line1);
    item.appendChild(el("div", "line2", a.status.activity));
    item.title = `${a.name} · ${STATE_LABEL[a.status.state]}\n${a.status.task || ""}\n${a.workspace}`;
    item.addEventListener("click", () => select(a.id));
    return item;
  }

  // ------------------------------------------------------------ overview

  function renderOverview() {
    const box = $("overview");
    box.textContent = "";
    if (!projects.size) {
      const empty = el("div", "empty");
      empty.appendChild(el("h2", "", "Open a project to start"));
      empty.appendChild(
        el("p", "", "crit runs coding agents on a folder. Each agent can get its own copy of the repository, so several can work at once."),
      );
      const open = el("button", "primary", "Open project…");
      open.addEventListener("click", openProject);
      empty.appendChild(open);
      box.appendChild(empty);
      return;
    }
    if (!agents.size) {
      const empty = el("div", "empty");
      empty.appendChild(el("h2", "", "No agents yet"));
      empty.appendChild(el("p", "", "Start one with a task. Start more to work on other tasks at the same time."));
      const start = el("button", "primary", "New agent");
      start.addEventListener("click", () => showNewAgent());
      empty.appendChild(start);
      box.appendChild(empty);
      return;
    }
    box.appendChild(el("h2", "", "All agents"));
    const list = [...agents.values()];
    const waiting = list.filter((a) => a.status.state === "waiting").length;
    box.appendChild(
      el("p", "sub", `${list.length} agent${list.length === 1 ? "" : "s"}` + (waiting ? ` · ${waiting} waiting for you` : "")),
    );
    const cards = el("div", "cards");
    for (const a of sortedAgents()) {
      const card = el("button", `card state-${a.status.state}`);
      const head = el("div", "head");
      head.appendChild(el("span", "dot " + a.status.state));
      head.appendChild(el("span", "name", a.name));
      head.appendChild(el("span", "state", STATE_LABEL[a.status.state]));
      card.appendChild(head);
      card.appendChild(el("div", "task", a.status.task || "No task yet"));
      card.appendChild(el("div", "activity", a.status.activity));
      const meta = el("div", "meta");
      const project = projects.get(a.projectId);
      if (projects.size > 1 && project) meta.appendChild(el("span", "", project.name));
      meta.appendChild(el("span", "", a.branch || "project folder"));
      if (a.status.changedFiles.length) meta.appendChild(el("span", "", `${a.status.changedFiles.length} file(s) changed`));
      if (elapsed(a)) meta.appendChild(el("span", "", elapsed(a)));
      card.appendChild(meta);
      card.addEventListener("click", () => select(a.id));
      cards.appendChild(card);
    }
    box.appendChild(cards);
  }

  // ------------------------------------------------------------ agent view

  function ensureFrame(id) {
    let frame = frames.get(id);
    if (!frame) {
      frame = document.createElement("iframe");
      frame.src = "chat-frame.html";
      frame.title = "Agent chat";
      frame.classList.add("hidden");
      frames.set(id, frame);
      $("frames").appendChild(frame);
    }
    return frame;
  }

  function ensureTerm(id) {
    let entry = terms.get(id);
    if (!entry) {
      const node = el("div", "term hidden");
      $("terminals").appendChild(node);
      const style = getComputedStyle(document.documentElement);
      const term = new window.Terminal({
        convertEol: false,
        disableStdin: true,
        cursorBlink: false,
        cursorStyle: "bar",
        cursorInactiveStyle: "none",
        fontFamily: style.getPropertyValue("--mono").trim() || "monospace",
        fontSize: 12,
        scrollback: 5000,
        theme: { background: "#0f1012", foreground: "#d7dae0" },
      });
      term.open(node);
      entry = { term, node, filled: false };
      terms.set(id, entry);
      crit.agent(id, "terminal").then((text) => {
        entry.filled = true;
        if (text) term.write(text);
        else term.write("\x1b[90mCommands this agent runs show here.\x1b[0m\r\n");
      });
    }
    return entry;
  }

  function fitTerm(entry) {
    const box = entry.node.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const core = entry.term._core;
    const cell = core && core._renderService && core._renderService.dimensions && core._renderService.dimensions.css.cell;
    const cw = (cell && cell.width) || 7.2;
    const ch = (cell && cell.height) || 15;
    const cols = Math.max(20, Math.floor(box.width / cw));
    const rows = Math.max(4, Math.floor(box.height / ch));
    if (cols !== entry.term.cols || rows !== entry.term.rows) entry.term.resize(cols, rows);
  }

  function select(id) {
    selected = id && agents.has(id) ? id : null;
    $("overview").classList.toggle("hidden", selected !== null);
    $("agent-view").classList.toggle("hidden", selected === null);
    $("side").classList.toggle("hidden", selected === null);
    document.querySelector(".layout").style.gridTemplateColumns = selected === null ? "260px minmax(0,1fr)" : "";
    for (const [fid, frame] of frames) frame.classList.toggle("hidden", fid !== selected);
    for (const [tid, entry] of terms) entry.node.classList.toggle("hidden", tid !== selected);
    if (selected) {
      ensureFrame(selected).classList.remove("hidden");
      const entry = ensureTerm(selected);
      entry.node.classList.remove("hidden");
      requestAnimationFrame(() => fitTerm(entry));
      renderAgentBar();
      void refreshChanges();
      const frame = frames.get(selected);
      setTimeout(() => frame && frame.contentWindow && frame.focus(), 0);
    } else {
      renderOverview();
    }
    renderList();
  }

  function renderAgentBar() {
    const a = agents.get(selected);
    if (!a) return;
    $("av-dot").className = "dot " + a.status.state;
    $("av-name").textContent = a.name;
    $("av-branch").textContent = a.branch ? a.branch : "project folder";
    $("av-branch").title = a.workspace;
    $("av-activity").textContent = a.status.activity;
    $("av-stop").disabled = !(a.status.state === "working" || a.status.state === "waiting");
    $("av-apply").classList.toggle("hidden", !a.isolated);
  }

  let changesFor = null;
  async function refreshChanges() {
    const id = selected;
    if (!id) return;
    const box = $("changes");
    let list;
    try {
      list = await crit.agent(id, "changes");
    } catch (err) {
      if (id === selected) {
        box.textContent = "";
        box.appendChild(el("div", "hint", errorText(err)));
      }
      return;
    }
    if (id !== selected) return;
    changesFor = id;
    box.textContent = "";
    if (!list.length) {
      box.appendChild(el("div", "hint", "No changes yet."));
      return;
    }
    for (const change of list) {
      const row = el("button", "change");
      row.title = change.path;
      row.appendChild(el("span", "st " + change.status, change.status));
      row.appendChild(el("span", "p", change.path));
      row.addEventListener("click", () => showFileDiff(id, change.path));
      box.appendChild(row);
    }
  }

  async function showFileDiff(id, file) {
    const a = agents.get(id);
    if (!a) return;
    if (a.isolated) {
      const patch = await crit.agent(id, "worktree-diff");
      const parts = String(patch).split(/^(?=diff --git )/m);
      const part = parts.find((p) => p.startsWith(`diff --git a/${file} `) || p.includes(` b/${file}\n`));
      showDiff(file, part || "(no difference)");
    } else {
      showDiff(file, (await crit.agent(id, "file-diff", file)) || "(no diff recorded for this file)");
    }
  }

  function showDiff(title, text) {
    $("diff-title").textContent = title;
    const body = $("diff-body");
    body.textContent = "";
    for (const line of String(text).split("\n")) {
      let cls = "ln";
      if (line.startsWith("@@")) cls += " hunk";
      else if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(line)) cls += " meta";
      else if (line.startsWith("+")) cls += " add";
      else if (line.startsWith("-")) cls += " del";
      body.appendChild(el("span", cls, line || " "));
    }
    $("diff-overlay").classList.remove("hidden");
    $("diff-close").focus();
  }

  // ------------------------------------------------------------ actions

  async function openProject() {
    try {
      const project = await crit.openProject();
      if (project) {
        $("new-project").value = project.id;
        if (!agents.size) showNewAgent(project.id);
      }
    } catch (err) {
      toast(errorText(err), true);
    }
  }

  function showNewAgent(projectId) {
    if (!projects.size) {
      void openProject();
      return;
    }
    const select = $("new-project");
    if (projectId) select.value = projectId;
    else if (selected && agents.get(selected)) select.value = agents.get(selected).projectId;
    updateIsolation();
    $("new-error").textContent = "";
    $("new-overlay").classList.remove("hidden");
    $("new-task").focus();
  }

  function updateIsolation() {
    const project = projects.get($("new-project").value);
    const box = $("new-isolated");
    box.disabled = !project || !project.isGit;
    if (box.disabled) box.checked = false;
  }

  function hideNewAgent() {
    $("new-overlay").classList.add("hidden");
  }

  async function submitNewAgent(event) {
    event.preventDefault();
    const spec = {
      projectId: $("new-project").value,
      name: $("new-name").value.trim(),
      task: $("new-task").value.trim(),
      mode: $("new-mode").value,
      isolated: $("new-isolated").checked,
    };
    if (!spec.task) {
      $("new-error").textContent = "Give the agent a task.";
      return;
    }
    $("new-error").textContent = "";
    try {
      const agent = await crit.createAgent(spec);
      hideNewAgent();
      $("new-task").value = "";
      $("new-name").value = "";
      agents.set(agent.id, agent);
      select(agent.id);
    } catch (err) {
      $("new-error").textContent = errorText(err);
    }
  }

  function bindActions() {
    $("open-project").addEventListener("click", openProject);
    $("new-agent").addEventListener("click", () => showNewAgent());
    $("overview-link").addEventListener("click", () => select(null));
    $("new-close").addEventListener("click", hideNewAgent);
    $("new-cancel").addEventListener("click", hideNewAgent);
    $("new-form").addEventListener("submit", submitNewAgent);
    $("new-project").addEventListener("change", updateIsolation);
    $("new-task").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("new-form").requestSubmit();
    });
    $("diff-close").addEventListener("click", () => $("diff-overlay").classList.add("hidden"));
    $("refresh-changes").addEventListener("click", refreshChanges);
    $("av-stop").addEventListener("click", () => selected && crit.agent(selected, "interrupt"));
    $("av-restart").addEventListener("click", () => selected && crit.agent(selected, "restart"));
    $("av-folder").addEventListener("click", () => selected && crit.agent(selected, "reveal"));
    $("av-apply").addEventListener("click", async () => {
      const id = selected;
      if (!id) return;
      try {
        const files = await crit.agent(id, "apply");
        toast(files.length ? `Applied ${files.length} file(s) to the project folder (not committed).` : "Nothing to apply.");
      } catch (err) {
        toast("Could not apply: " + errorText(err), true);
      }
    });
    $("av-delete").addEventListener("click", async () => {
      const id = selected;
      if (!id) return;
      try {
        await crit.agent(id, "discard");
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        $("diff-overlay").classList.add("hidden");
        hideNewAgent();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        showNewAgent();
      }
    });
    window.addEventListener("resize", () => {
      const entry = selected && terms.get(selected);
      if (entry) fitTerm(entry);
    });
    // Messages from the chat iframes.
    window.addEventListener("message", (event) => {
      const data = event.data || {};
      if (data.source !== "crit-chat") return;
      for (const [id, frame] of frames) {
        if (frame.contentWindow === event.source) {
          crit.chat(id, data.message);
          return;
        }
      }
    });
  }

  // ------------------------------------------------------------ from main

  function postToFrame(id, message) {
    const frame = ensureFrame(id);
    if (frame.contentWindow) frame.contentWindow.postMessage(message, "crit://app");
  }

  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      renderList();
      renderCounts();
      if (selected === null) renderOverview();
      else renderAgentBar();
    });
  }

  function bindEvents() {
    crit.on("projects", (list) => {
      projects.clear();
      for (const p of list) projects.set(p.id, p);
      renderProjects();
      scheduleRender();
    });
    crit.on("agent", (agent) => {
      const before = agents.get(agent.id);
      agents.set(agent.id, agent);
      ensureFrame(agent.id);
      scheduleRender();
      if (agent.id === selected && (!before || before.status.changedFiles.length !== agent.status.changedFiles.length || before.status.state !== agent.status.state)) {
        void refreshChanges();
      }
    });
    crit.on("removed", (id) => {
      agents.delete(id);
      const frame = frames.get(id);
      if (frame) frame.remove();
      frames.delete(id);
      const entry = terms.get(id);
      if (entry) {
        entry.term.dispose();
        entry.node.remove();
      }
      terms.delete(id);
      if (selected === id) select(null);
      scheduleRender();
    });
    crit.on("chat", (id, message) => postToFrame(id, message));
    crit.on("term", (id, data) => {
      const entry = terms.get(id);
      if (entry && entry.filled) entry.term.write(data);
    });
    crit.on("show-diff", (_id, file) => showDiff(file.path, file.text));
    crit.on("show-log", (id, text) => {
      const a = agents.get(id);
      showDiff(`${a ? a.name : "agent"} log`, text || "(empty)");
    });
    crit.on("show-terminal", () => {
      const entry = selected && terms.get(selected);
      if (entry) entry.term.focus();
    });
    crit.on("select-agent", (id) => select(id));
  }

  async function init() {
    bindActions();
    bindEvents();
    const state = await crit.state();
    for (const p of state.projects) projects.set(p.id, p);
    for (const a of state.agents) {
      agents.set(a.id, a);
      ensureFrame(a.id);
    }
    renderProjects();
    select(null);
    renderCounts();
    // Elapsed times tick.
    setInterval(() => {
      if ([...agents.values()].some((a) => a.status.state === "working" || a.status.state === "waiting")) scheduleRender();
    }, 1000);
  }

  void init();
})();
