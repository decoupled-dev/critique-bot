// crit chat webview. Talks to ChatController (src/chatView.ts) with postMessage.
// All agent text goes through textContent or escape(); nothing from the agent is
// inserted as raw HTML.
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");

  // ------------------------------------------------------------------ helpers

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(label, cls, onClick, title) {
    const b = el("button", cls, label);
    if (title) b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function escape(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(text) {
    // `code`, **bold**, *italic* on already-escaped text.
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }

  // Small markdown: fenced code, headings, lists, paragraphs. Input is escaped first.
  function markdown(source) {
    const out = [];
    const lines = String(source).replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    let para = [];
    let list = null;
    const flushPara = () => {
      if (para.length) out.push("<p>" + inline(escape(para.join("\n"))).replace(/\n/g, "<br>") + "</p>");
      para = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>` + list.items.map((t) => "<li>" + inline(escape(t)) + "</li>").join("") + `</${list.tag}>`);
      list = null;
    };
    while (i < lines.length) {
      const line = lines[i];
      const fence = /^\s*(```|~~~)\s*([\w+-]*)/.exec(line);
      if (fence) {
        flushPara();
        flushList();
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++;
        const lang = fence[2] || "";
        const cls = lang === "diff" ? " class=\"diff\"" : "";
        out.push(`<pre${cls}><code>` + (lang === "diff" ? diffHtml(body) : escape(body.join("\n"))) + "</code></pre>");
        continue;
      }
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        flushPara();
        flushList();
        const level = Math.min(heading[1].length + 2, 6);
        out.push(`<h${level}>` + inline(escape(heading[2])) + `</h${level}>`);
        i++;
        continue;
      }
      const bullet = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (bullet) {
        flushPara();
        const tag = /\d/.test(bullet[1]) ? "ol" : "ul";
        if (!list || list.tag !== tag) {
          flushList();
          list = { tag, items: [] };
        }
        list.items.push(bullet[2]);
        i++;
        continue;
      }
      if (!line.trim()) {
        flushPara();
        flushList();
        i++;
        continue;
      }
      flushList();
      para.push(line);
      i++;
    }
    flushPara();
    flushList();
    return out.join("");
  }

  function diffHtml(lines) {
    return lines
      .map((line) => {
        let cls = "ctx";
        if (line.startsWith("+++ ") || line.startsWith("--- ")) cls = "meta";
        else if (line.startsWith("@@")) cls = "hunk";
        else if (line.startsWith("+")) cls = "add";
        else if (line.startsWith("-")) cls = "del";
        return `<span class="${cls}">${escape(line)}</span>`;
      })
      .join("\n");
  }

  function diffBlock(text, maxLines) {
    const pre = el("pre", "diff");
    const code = el("code");
    let lines = String(text).split("\n");
    let more = 0;
    if (maxLines && lines.length > maxLines) {
      more = lines.length - maxLines;
      lines = lines.slice(0, maxLines);
    }
    code.innerHTML = diffHtml(lines) + (more ? `\n<span class="meta">… ${more} more lines</span>` : "");
    pre.appendChild(code);
    return pre;
  }

  function looksLikeDiff(text) {
    return /^(---|\+\+\+|@@)/m.test(text);
  }

  // ------------------------------------------------------------------ layout

  const header = el("div", "header");
  const info = el("div", "info", "crit");
  const modeSelect = el("select", "mode");
  modeSelect.title = "Permission mode";
  const roleSelect = el("select", "role");
  roleSelect.title = "Role (applies from the next task)";
  const tools = el("div", "tools");
  tools.append(
    button("New chat", "icon", () => vscode.postMessage({ type: "new_chat" }), "Start a fresh chat"),
    button("Undo", "icon", () => vscode.postMessage({ type: "undo" }), "Restore files changed by the last task"),
    button("Terminal", "icon", () => vscode.postMessage({ type: "show_terminal" }), "Show commands crit ran"),
    button("Log", "icon", () => vscode.postMessage({ type: "show_log" }), "Show the agent log"),
    button("Restart", "icon", () => vscode.postMessage({ type: "restart" }), "Restart the agent"),
  );
  header.append(info, modeSelect, roleSelect, tools);

  const log = el("div", "log");
  const thinking = el("div", "thinking hidden");
  const composer = el("div", "composer");
  const input = el("textarea");
  input.rows = 3;
  input.placeholder = "Ask crit to do something… (Enter to send, Shift+Enter for a new line)";
  const send = button("Send", "primary", submit);
  const stop = button("Stop", "danger hidden", () => vscode.postMessage({ type: "interrupt" }), "Stop the running task");
  const actions = el("div", "actions");
  actions.append(stop, send);
  composer.append(input, actions);
  app.append(header, log, thinking, composer);

  for (const id of ["ask", "edits", "auto", "plan"]) {
    const opt = el("option", "", id);
    opt.value = id;
    modeSelect.appendChild(opt);
  }
  roleSelect.classList.add("hidden");

  modeSelect.addEventListener("change", () => vscode.postMessage({ type: "set_mode", mode: modeSelect.value }));
  roleSelect.addEventListener("change", () => vscode.postMessage({ type: "set_role", role: roleSelect.value }));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  function submit() {
    const text = input.value.trim();
    if (!text) return;
    vscode.postMessage({ type: "prompt", text });
    input.value = "";
    add(el("div", "user", text));
  }

  function add(node) {
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    log.appendChild(node);
    if (nearBottom) log.scrollTop = log.scrollHeight;
    return node;
  }

  function setBusy(busy) {
    stop.classList.toggle("hidden", !busy);
    if (!busy) thinking.classList.add("hidden");
  }

  // ------------------------------------------------------------------ tools

  const openTools = new Map(); // name -> [card]

  function toolTitle(name, args) {
    args = args || {};
    const where = args.path || (Array.isArray(args.paths) ? args.paths.slice(0, 3).join(", ") : "") || "";
    switch (name) {
      case "run_command":
        return ["Run", args.command || ""];
      case "read_files":
        return ["Read", where];
      case "list_files":
        return ["List", where || "."];
      case "find_files":
        return ["Find", args.glob || args.pattern || ""];
      case "search_code":
        return ["Search", args.pattern || ""];
      case "edit_file":
        return ["Edit", where];
      case "write_files":
        return ["Write", where || (Array.isArray(args.files) && args.files[0] ? args.files[0].path : "")];
      case "delete_file":
        return ["Delete", where];
      default:
        return [name, where];
    }
  }

  function toolStart(event) {
    const card = el("div", "tool running");
    const [verb, target] = toolTitle(event.name, event.args);
    const head = el("div", "tool-head");
    head.append(el("span", "dot"), el("span", "verb", verb), el("span", "target", target));
    card.appendChild(head);
    if (event.name === "run_command") {
      card.output = el("pre", "output hidden");
      card.appendChild(card.output);
    }
    const list = openTools.get(event.name) || [];
    list.push(card);
    openTools.set(event.name, list);
    add(card);
  }

  function lastRunning() {
    let found = null;
    for (const list of openTools.values()) for (const card of list) if (card.output) found = card;
    return found;
  }

  function toolOutput(event) {
    const card = lastRunning();
    if (!card) return;
    card.output.classList.remove("hidden");
    const text = (card.output.textContent + String(event.text || "")).slice(-4000);
    card.output.textContent = text;
    card.output.scrollTop = card.output.scrollHeight;
  }

  function toolDone(event) {
    const list = openTools.get(event.name) || [];
    let card = list.shift();
    if (!list.length) openTools.delete(event.name);
    if (!card) {
      toolStart(event);
      card = (openTools.get(event.name) || []).shift();
      openTools.delete(event.name);
    }
    card.classList.remove("running");
    card.classList.add(event.ok ? "ok" : "err");
    if (!event.ok) {
      card.appendChild(el("div", "tool-error", event.friendly || event.error || "failed"));
    }
    const result = String(event.result || "");
    if (event.ok && result && (event.name === "edit_file" || event.name === "write_files") && looksLikeDiff(result)) {
      const start = result.search(/^--- /m);
      card.appendChild(diffBlock(start >= 0 ? result.slice(start) : result, 30));
    }
  }

  // ------------------------------------------------------------------ requests

  const requests = new Map(); // id -> card

  function settle(card, text, cls) {
    card.querySelectorAll("button, textarea, input").forEach((n) => (n.disabled = true));
    const body = card.querySelector(".choices");
    if (body) body.replaceWith(el("div", "settled " + (cls || ""), text));
  }

  function reasonInput(placeholder) {
    const box = el("textarea", "reason");
    box.rows = 2;
    box.placeholder = placeholder;
    return box;
  }

  function approval(event) {
    const card = el("div", "request approval" + (event.risky ? " risky" : ""));
    const title = { edit: "Edit", command: "Run command", network: "Web request", outside: "Outside the project" }[event.kind] || "Approve";
    card.appendChild(el("div", "request-title", title));
    card.appendChild(el("div", "summary", event.summary));
    const detail = String(event.detail || "");
    if (detail.trim() && detail.trim() !== String(event.summary).trim()) {
      card.appendChild(looksLikeDiff(detail) ? diffBlock(detail, 40) : el("pre", "detail", detail));
    }
    if (event.risky) card.appendChild(el("div", "warning", `⚠ This command ${event.risky}.`));
    const choices = el("div", "choices");
    const reason = reasonInput("What should crit do instead? (optional)");
    reason.classList.add("hidden");
    for (const option of event.options || []) {
      const cls = option.value === "no" ? "secondary" : option.value === "yes" ? "primary" : "";
      choices.appendChild(
        button(option.label.replace(/\s*\(esc\)$/, ""), cls, () => {
          if (option.value === "no" && reason.classList.contains("hidden")) {
            reason.classList.remove("hidden");
            reason.focus();
            choices.appendChild(
              button("Decline", "secondary", () => answer(event.id, "no", reason.value.trim(), "Declined" + (reason.value.trim() ? ": " + reason.value.trim() : ""), "bad")),
            );
            return;
          }
          answer(event.id, option.value, "", option.value === "no" ? "Declined" : "✓ " + option.label, option.value === "no" ? "bad" : "good");
        }),
      );
    }
    choices.appendChild(reason);
    card.appendChild(choices);
    requests.set(event.id, card);
    add(card);
  }

  function planReview(event) {
    const card = el("div", "request plan");
    card.appendChild(el("div", "request-title", "Plan ready"));
    card.appendChild(el("div", "summary", "Ready to code? crit will carry out the plan above."));
    const choices = el("div", "choices");
    const feedback = reasonInput("What should change in the plan?");
    feedback.classList.add("hidden");
    for (const option of event.options || []) {
      choices.appendChild(
        button(option.label, option.value === "auto" ? "primary" : "", () => {
          if (option.value === "no") {
            if (feedback.classList.contains("hidden")) {
              feedback.classList.remove("hidden");
              feedback.focus();
              return;
            }
            const text = feedback.value.trim();
            answer(event.id, text ? "no" : "stop", text, text ? "Revising: " + text : "Kept the plan; nothing changed", "");
            return;
          }
          answer(event.id, option.value, "", "✓ " + option.label, "good");
        }),
      );
    }
    choices.appendChild(button("Stop here", "secondary", () => answer(event.id, "stop", "", "Kept the plan; nothing changed", "")));
    choices.appendChild(feedback);
    card.appendChild(choices);
    requests.set(event.id, card);
    add(card);
  }

  function question(event) {
    const card = el("div", "request question");
    card.appendChild(el("div", "request-title", "crit has a question"));
    const text = el("div", "markdown");
    text.innerHTML = markdown(event.text || "?");
    card.appendChild(text);
    const choices = el("div", "choices");
    for (const option of event.options || []) {
      choices.appendChild(button(option, "", () => answer(event.id, option, "", "Answered: " + option, "")));
    }
    const free = el("input", "free");
    free.type = "text";
    free.placeholder = "Type an answer and press Enter";
    free.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && free.value.trim()) answer(event.id, free.value.trim(), "", "Answered: " + free.value.trim(), "");
    });
    choices.appendChild(free);
    card.appendChild(choices);
    requests.set(event.id, card);
    add(card);
    free.focus();
  }

  function answer(id, value, reason, label, cls) {
    vscode.postMessage({ type: "answer", id, value, reason: reason || undefined });
    const card = requests.get(id);
    if (card) {
      settle(card, label, cls);
      requests.delete(id);
    }
  }

  // ------------------------------------------------------------------ events

  function onEvent(event) {
    switch (event.type) {
      case "hello": {
        const parts = [event.model, event.shell].filter(Boolean);
        info.textContent = "crit " + (event.version || "");
        info.title = [event.workspace, ...parts].join(" · ");
        modeSelect.value = event.mode;
        roleSelect.textContent = "";
        for (const role of event.roles || []) {
          const opt = el("option", "", role.label);
          opt.value = role.id;
          opt.title = role.summary || "";
          roleSelect.appendChild(opt);
        }
        roleSelect.value = event.role;
        roleSelect.classList.remove("hidden");
        add(el("div", "system", "Connected" + (parts.length ? " · " + parts.join(" · ") : "")));
        break;
      }
      case "ready":
        setBusy(false);
        break;
      case "note":
        if (event.kind === "task" || /^Ready\. Type a task/.test(event.text)) break;
        add(el("div", "note " + (event.kind || ""), event.text));
        break;
      case "assistant": {
        const node = el("div", "assistant markdown");
        node.innerHTML = markdown(String(event.text || "").replace(/\s*\b(COMPLETED|FINISHED|DONE)\.?\s*$/, ""));
        if (node.textContent.trim()) add(node);
        break;
      }
      case "thinking":
        thinking.textContent = (event.label || "Thinking") + "…";
        thinking.classList.toggle("hidden", !event.active);
        break;
      case "tool_start":
        toolStart(event);
        break;
      case "tool_output":
        toolOutput(event);
        break;
      case "tool_done":
        toolDone(event);
        break;
      case "active":
        if (event.count > 0) thinking.textContent = `Running ${event.count} helper chat${event.count === 1 ? "" : "s"}…`;
        break;
      case "approval":
        approval(event);
        break;
      case "plan_review":
        planReview(event);
        break;
      case "question":
        question(event);
        break;
      case "cancel": {
        const card = requests.get(event.id);
        if (card) {
          settle(card, "No longer waiting", "");
          requests.delete(event.id);
        }
        break;
      }
      case "diff": {
        const card = el("div", "changes");
        card.appendChild(el("div", "request-title", "Changed files"));
        (event.files || []).forEach((file, index) => {
          const row = el("div", "file");
          const name = el("span", "path", file.path + (file.added ? " (new)" : file.deleted ? " (deleted)" : ""));
          const open = button("Open diff", "", () => vscode.postMessage({ type: "open_diff", diff: event.index, file: index }));
          const show = button("Show", "", () => details.classList.toggle("hidden"));
          const details = diffBlock(file.text, 200);
          details.classList.add("hidden");
          row.append(name, show, open);
          card.append(row, details);
          if (!file.deleted) name.addEventListener("click", () => vscode.postMessage({ type: "open_file", path: file.path }));
        });
        add(card);
        break;
      }
      case "undone":
        add(el("div", "note good", "Restored " + (event.paths || []).join(", ")));
        break;
      case "mode":
        modeSelect.value = event.mode;
        add(el("div", "system", "Mode: " + event.label));
        break;
      case "role":
        roleSelect.value = event.role;
        add(el("div", "system", `Role: ${event.label}. The next task starts a new chat with it.`));
        break;
      case "status": {
        const labels = { COMPLETED: "✓ Done", FINISHED: "✓ Done", DONE: "✓ Done", INTERRUPTED: "⏹ Interrupted", STOPPED: "⏹ Stopped", BLOCKED: "✗ Blocked" };
        const text = labels[event.code] || (event.ok ? "✓ Done" : "✗ Failed");
        add(el("div", "status " + (event.ok ? "good" : "bad"), text + (event.reason ? ": " + event.reason : "")));
        break;
      }
      case "error":
        add(el("div", "note bad", event.message));
        break;
    }
  }

  window.addEventListener("message", (message) => {
    const data = message.data || {};
    switch (data.kind) {
      case "event":
        onEvent(data.event || {});
        break;
      case "busy":
        setBusy(!!data.busy);
        break;
      case "state":
        setBusy(!!data.busy);
        if (data.hello) onEvent(data.hello);
        break;
      case "starting":
        add(el("div", "system", data.source === "bundled" ? "Starting crit…" : `Starting crit (${data.source})…`));
        break;
      case "stopped": {
        setBusy(false);
        for (const [id, card] of requests) {
          settle(card, "No longer waiting", "");
          requests.delete(id);
        }
        const row = el("div", "system " + (data.reason ? "bad" : ""), data.reason ? "crit stopped: " + data.reason : "crit stopped.");
        if (data.reason) row.appendChild(button("Show log", "link", () => vscode.postMessage({ type: "show_log" })));
        add(row);
        break;
      }
      case "cleared":
        log.textContent = "";
        openTools.clear();
        requests.clear();
        break;
    }
  });

  add(el("div", "system", "Type a task to start. crit opens its browser on the first one."));
  vscode.postMessage({ type: "ready" });
  input.focus();
})();
