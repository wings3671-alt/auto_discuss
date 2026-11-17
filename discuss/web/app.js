// 胡说八道群界面。不保存任何数据:一切以后台为准,后台一有变化就重新取一遍再画。
import { renderMarkdown } from "/web/markdown.js";
import { buildExport } from "/web/export.js";

const $ = (id) => document.getElementById(id);
const esc = (text) => String(text ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const enc = encodeURIComponent;

const state = {
  list: [],
  id: null,            // 当前打开的讨论
  view: null,          // 它的全部内容
  settings: null,
  matches: null,       // 搜索结果:讨论 → 命中的上下文;没在搜索时为 null
  drafts: new Map(),   // 讨论 → 没发出去的文字
  attachments: new Map(),   // 讨论 → 已上传、待随发言发出的附件(图片和文件)
  sending: false,
  phases: new Map(),   // 讨论 → 上次看到的状态,用来发现"轮到你了"
};

// ── 与后台通信 ────────────────────────────────────────────────────────────
async function call(method, path, body, headers = {}) {
  const isJson = body !== undefined && !(body instanceof Blob);
  const response = await fetch(path, {
    method,
    headers: isJson ? { "Content-Type": "application/json", ...headers } : headers,
    body: isJson ? JSON.stringify(body) : body,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败(${response.status})`);
  return data;
}

const at = (id) => `/api/discussions/${enc(id)}`;
const api = {
  list: () => call("GET", "/api/discussions"),
  view: (id) => call("GET", at(id)),
  create: (title) => call("POST", "/api/discussions", { title }),
  send: (id, body) => call("POST", `${at(id)}/send`, body),
  act: (id, action, body = {}) => call("POST", `${at(id)}/${action}`, body),
  attach: (id, file) => call("POST", `${at(id)}/attachments`, file, { "X-Filename": enc(file.name || "附件") }),
  search: (query) => call("GET", `/api/search?q=${enc(query)}`),
  exportFile: (id, html) => call("POST", `${at(id)}/export`, new Blob([html], { type: "text/html" })),
  settings: () => call("GET", "/api/settings"),
  saveSettings: (settings) => call("PUT", "/api/settings", settings),
  setAvatar: (memberId, file) => call("POST", `/api/avatars/${enc(memberId)}`, file),
};

// 运行在「胡说八道群.app」里时,把状态告诉它(用于退出确认、程序坞提醒)
const tellApp = (message) => window.webkit?.messageHandlers?.app?.postMessage(message);

// ── 小工具 ────────────────────────────────────────────────────────────────
// 1 → 一、12 → 十二、25 → 二十五;用在"第几回"上
function chineseNumber(n) {
  const digits = "零一二三四五六七八九";
  if (n < 10) return digits[n];
  if (n < 20) return `十${n % 10 ? digits[n % 10] : ""}`;
  if (n < 100) return `${digits[Math.floor(n / 10)]}十${n % 10 ? digits[n % 10] : ""}`;
  return String(n);
}

// 1234 → 1.2k、56789 → 57k、1000000 → 1M
const tokens = (n) => (n >= 999500 ? `${Number((n / 1e6).toFixed(1))}M`
  : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));
const pad = (n) => String(n).padStart(2, "0");

function clock(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.toDateString() === new Date().toDateString()
    ? time : `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

function elapsed(startedSeconds) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedSeconds));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function avatar(person, small = false) {
  const css = `av${small ? " small" : ""}`;
  if (person?.image) return `<img class="${css}" src="/api/avatars/${enc(person.image)}" alt="">`;
  const text = person?.avatar || (person?.name || "?").slice(0, 1);
  const color = person?.color || "#888888";
  return `<span class="${css}" style="background:${esc(color)};color:${readableOn(color)}">${esc(text)}</span>`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {                                              // 剪贴板接口不可用时的老办法
    const holder = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(holder);
    holder.select();
    document.execCommand("copy");
    holder.remove();
  }
}

// 压在某个底色上能看清的文字颜色:底色亮就用深字,底色暗就用白字
function readableOn(hex) {
  const [red, green, blue] = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16));
  return 0.299 * red + 0.587 * green + 0.114 * blue > 160 ? "#1d1d1b" : "#ffffff";
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 3200);
}

const guarded = (work) => async (...args) => {
  try { return await work(...args); } catch (error) { toast(error.message); return undefined; }
};

// ── 对话框 ────────────────────────────────────────────────────────────────
function dialog({ title, body, actions, large = false }) {
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.innerHTML = `<div class="dialog${large ? " large" : ""}"><header>${esc(title)}</header>
    <div class="content"></div><footer><span class="problem"></span></footer></div>`;
  const content = veil.querySelector(".content");
  if (typeof body === "string") content.innerHTML = body; else content.append(body);
  const footer = veil.querySelector("footer");
  const close = () => { veil.remove(); document.removeEventListener("keydown", onKey); };
  const problem = (message) => { footer.querySelector(".problem").textContent = message; };
  for (const action of actions) {
    const button = document.createElement("button");
    button.className = `btn ${action.kind || ""}`;
    button.textContent = action.label;
    button.onclick = async () => {
      try { if ((await action.run?.({ close, problem, content })) !== false) close(); }
      catch (error) { problem(error.message); }
    };
    footer.append(button);
  }
  const onKey = (event) => {
    if (event.key === "Escape") close();
    if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.type !== "color") {
      footer.querySelector(".primary")?.click();
    }
  };
  document.addEventListener("keydown", onKey);
  veil.addEventListener("mousedown", (event) => { if (event.target === veil) close(); });
  $("overlay").append(veil);
  content.querySelector("input, textarea")?.focus();
  return close;
}

// 弹出菜单(右键菜单和「⋯」按钮共用)。entries 里的空项会被忽略。
function popup(x, y, entries) {
  document.querySelector(".menu")?.remove();
  const menu = document.createElement("div");
  menu.className = "menu";
  for (const entry of entries.filter(Boolean)) {
    const button = document.createElement("button");
    button.textContent = entry.label;
    button.className = entry.danger ? "danger" : "";
    button.onclick = entry.run;
    menu.append(button);
  }
  document.body.append(menu);
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - height - 8))}px`;
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
}

const confirmDialog = (title, text, label, run, kind = "primary") => dialog({
  title, body: `<p>${esc(text)}</p>`,
  actions: [{ label: "取消" }, { label, kind, run }],
});

// ── 列表 ──────────────────────────────────────────────────────────────────
const LIST_TEXT = {
  new: () => "还没写议题",
  running: (row) => `${row.turn} ${row.hands_on ? "正在干活" : "正在发言"}`,
  user: (row) => `第 ${row.round} 轮 · 等你发言`,
  paused: (row) => `第 ${row.round} 轮 · 停在 ${row.turn}`,
};

function renderList() {
  const rows = state.matches ? state.list.filter((row) => state.matches.has(row.id)) : state.list;
  const item = (row) => {
    const date = new Date(row.modified * 1000);
    const meta = state.matches?.get(row.id)                       // 搜索时显示命中的那句话
      || (LIST_TEXT[row.phase]?.(row) ?? `${row.round} 轮 · ${date.getMonth() + 1}月${date.getDate()}日`);
    return `<button class="item${row.id === state.id ? " on" : ""}" data-act="open" data-id="${esc(row.id)}">
      <div class="t">${row.pinned ? "📌 " : ""}${esc(row.title)}</div>
      <div class="m"><span class="dot ${row.phase}"></span><span class="meta">${esc(meta)}</span></div></button>`;
  };
  const group = ([key, title, items]) => {
    const folded = !state.matches && state.settings.folded_groups.includes(key);   // 搜索时一律展开,免得搜到了却看不见
    return `<button class="group" data-act="foldGroup" data-group="${key}" title="${folded ? "展开" : "收起"}这一组">
      <i>${folded ? "▸" : "▾"}</i>${title}<span class="count">${items.length}</span></button>${folded ? "" : items.map(item).join("")}`;
  };
  const groups = [
    ["pinned", "置顶", rows.filter((row) => row.pinned)],
    ["active", "进行中", rows.filter((row) => !row.pinned && row.phase !== "done")],
    ["done", "已收工", rows.filter((row) => !row.pinned && row.phase === "done")],
  ].filter(([, , items]) => items.length);
  $("list").innerHTML = rows.length
    ? groups.map(group).join("")
    : `<div class="empty-list">${state.matches ? "标题和发言里都没有找到。" : "还没有讨论。点上面的按钮新建一场。"}</div>`;
}

async function refreshList() {
  state.list = await api.list();
  for (const row of state.list) {
    const before = state.phases.get(row.id);
    if (before === "running" && (row.phase === "user" || row.phase === "paused") && state.settings?.notify) {
      tellApp({ type: "attention", title: row.phase === "user" ? "轮到你了" : "讨论暂停了", body: row.title });
    }
    state.phases.set(row.id, row.phase);
  }
  tellApp({ type: "state", running: state.list.filter((row) => row.phase === "running").length });
  renderList();
}

// ── 一场讨论 ──────────────────────────────────────────────────────────────
const person = (id) => state.view.people[id] || { name: id, color: "#888888" };
const nameOf = (id) => person(id).name;

function renderHead() {
  const view = state.view;
  if (!view) { $("head").innerHTML = ""; return; }
  const status = { new: "还没开始", done: "已收工" }[view.phase] ?? `第 ${view.round} 轮 / 上限 ${view.max_rounds} 轮`;
  const speaking = view.phase === "running" || view.phase === "paused" ? view.turn : (view.phase === "user" ? "USER" : "");
  const chips = [...view.members, "USER"].map((id) =>
    `<span class="chip${id === speaking ? " now" : ""}">${esc(id === "USER" ? "我" : nameOf(id))}${id === view.assignee ? " · 干活" : ""}</span>`).join("›");
  $("head").innerHTML = `
    <div><h1 data-act="rename" title="点击改名">${esc(view.title)}</h1><div class="sub">${esc(status)}</div></div>
    <div class="order">发言顺序 ${chips}</div>
    <button class="more" data-act="menu" title="更多">⋯</button>`;
}

function messageHtml(message, index) {
  if (message.kind === "note") return `<div class="notice">${esc(message.text)}</div>`;
  const mine = message.kind !== "ai";
  const who = mine ? person("USER") : { ...person(message.speaker), name: nameOf(message.speaker) || message.name };
  const { html, links } = renderMarkdown(message.text, { resolveImage: imageUrl, resolveFile: attachmentName });
  const label = message.kind === "topic" ? "议题" : clock(message.time);
  const bubble = message.text.trim()
    ? `<div class="bub md">${html}</div>`
    : `<div class="bub quiet">没有写内容,直接继续</div>`;
  const cards = !mine && links.length
    ? `<div class="cards">${links.slice(0, 4).map(linkCard).join("")}</div>` : "";
  const tags = [];
  if (message.hands_on) tags.push(`<span class="tag job" title="这一棒是你派的活:它可以改文件、跑命令">派活</span>`);
  if (message.note) tags.push(`<span class="tag${message.note.startsWith("⚠") ? " bad" : ""}">${esc(message.note)}</span>`);
  if (message.ctx) tags.push(`<span class="tag">上下文 ${tokens(message.ctx)} · 输出 ${tokens(message.out)}</span>`);
  return `<div class="msg${mine ? " me" : ""}" style="--who:${esc(who.color)}">${avatar(who)}<div class="body">
    <div class="who"><b>${esc(who.name)}</b><span>${esc(label)}</span>
      <button class="copy" data-act="copy" data-index="${index}">复制</button></div>
    ${bubble}${cards}${tags.length ? `<div class="foot">${tags.join("")}</div>` : ""}</div></div>`;
}

// 发言里引用的路径如果指向本讨论的附件夹,给出附件的文件名;否则是 null
function attachmentName(path) {
  const prefix = `${state.view.attachments}/`;
  const name = path.startsWith(prefix) ? path.slice(prefix.length) : "";
  return name && !name.includes("/") ? name : null;
}

// 发言里引用的附图 → 能在界面里显示的地址;不是本讨论附件夹里的图片一律不显示
function imageUrl(path) {
  const name = attachmentName(path);
  return name ? `${at(state.view.id)}/files/${enc(name)}` : null;
}

function linkCard(url) {
  let host = url;
  try { host = new URL(url).hostname.replace(/^www\./, ""); } catch { /* 保留原样 */ }
  return `<a class="card-link" href="${esc(url)}" target="_blank" rel="noopener">
    <span class="ic">↗</span><span><div class="host">${esc(host)}</div><div class="url">${esc(url)}</div></span></a>`;
}

// 后台此刻在做的事:整理白板、被派活的成员在干活,或者照常发言
const speakerNow = (view) => view.live?.speaker || view.turn;
const jobNow = (view) => (view.job === "tidy" ? "正在整理白板"
  : view.assignee && view.assignee === speakerNow(view) ? "正在干活" : "正在发言");

function liveHtml() {
  const view = state.view;
  const live = view.live || { speaker: view.turn, text: "", status: "", started: Date.now() / 1000 };
  const doing = live.status || (live.text || view.job === "tidy" ? jobNow(view) : "正在思考");
  const body = live.text
    ? `<div class="bub md">${renderMarkdown(live.text).html}</div>`
    : `<div class="bub"><span class="dots"><span></span><span></span><span></span></span></div>`;
  return `<div class="msg" id="live" style="--who:${esc(person(live.speaker).color)}">${avatar(person(live.speaker))}<div class="body">
    <div class="who"><b>${esc(nameOf(live.speaker))}</b><span>${esc(doing)} · 已用 <i id="elapsed" data-started="${live.started}">${elapsed(live.started)}</i></span></div>
    ${body}</div></div>`;
}

function troubleHtml() {
  const view = state.view;
  const name = nameOf(view.turn);
  const stopped = view.error.startsWith("你停止");
  const detail = stopped ? `<b>已停止。</b>${esc(name)} 这一棒没有说完,已经说过的内容都还在。`
    : view.error ? `<b>这一棒没成功。</b>已经说过的内容都还在。<pre>${esc(view.error)}</pre>`
    : `<b>讨论停在这里。</b>轮到 ${esc(name)} 发言,还没说。`;
  return `<div class="msg trouble" style="--who:${esc(person(view.turn).color)}">${avatar(person(view.turn))}<div class="body">
    <div class="who"><b>${esc(name)}</b></div>
    <div class="bub">${detail}<div class="actions">
      <button class="btn primary" data-act="resume">${view.error ? "重试这一棒" : `让 ${esc(name)} 发言`}</button>
      <button class="btn" data-act="skip">跳过 ${esc(name)}</button></div></div></div></div>`;
}

function messagesHtml(view) {
  const parts = [];
  let round = 0;
  view.messages.forEach((message, index) => {
    if (message.round > round) {
      round = message.round;
      parts.push(`<div class="round"><i>◆</i>第${chineseNumber(round)}回<i>◆</i></div>`);
    }
    parts.push(messageHtml(message, index));
  });
  return parts.join("");
}

function boardHtml(view) {
  const fresh = new RegExp(`^r${view.round}-`);                  // 本轮刚动过的条目高亮
  return view.board.sections.map((section) => `<h3>${esc(section.title)}</h3>${section.items.length
    ? `<ul>${section.items.map((item) => `<li class="${fresh.test(item.touched) ? "fresh" : ""}"><span class="id">${esc(item.id)}</span><span>${esc(item.text)}</span></li>`).join("")}</ul>`
    : `<div class="none">暂无</div>`}`).join("");
}

const SYNC_TEXT = {
  synced: (sync) => `已同步到 NAS(${clock(sync.at)})`,
  pending: () => "NAS 没连上,连上后会自动补同步",
  none: () => "还没同步到 NAS",
  off: () => "",
};

function renderChat({ toEnd = false } = {}) {
  const chat = $("chat");
  const view = state.view;
  if (!view) {
    chat.innerHTML = `<div class="welcome"><div><b>胡说八道群</b>在左边选一场讨论,或者新建一场。</div></div>`;
    return;
  }
  const nearEnd = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  const parts = [messagesHtml(view)];
  if (view.phase === "running") parts.push(liveHtml());
  if (view.phase === "paused") parts.push(troubleHtml());
  if (view.phase === "done") {
    const sync = SYNC_TEXT[view.sync.state]?.(view.sync) ?? "";
    parts.push(`<div class="notice">— 已收工${sync ? ` · ${esc(sync)}` : ""} —</div>`);
  }
  if (view.phase === "new" && !view.messages.length) {
    parts.push(`<div class="welcome"><div><b>${esc(view.title)}</b>在下面写下你想讨论的问题。<br>发送后,${esc(view.members.map(nameOf).join("、"))} 会依次发言,然后轮到你。</div></div>`);
  }
  chat.innerHTML = parts.join("");
  if (toEnd || nearEnd) chat.scrollTop = chat.scrollHeight;
}

function updateLive(live) {
  const view = state.view;
  if (!view || view.phase !== "running") return;
  view.live = live;
  const chat = $("chat");
  const nearEnd = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  const holder = document.createElement("div");
  holder.innerHTML = liveHtml();
  const old = $("live");
  if (old) old.replaceWith(holder.firstElementChild); else chat.append(holder.firstElementChild);
  if (nearEnd) chat.scrollTop = chat.scrollHeight;
}

// ── 输入区(只建一次,之后原地更新,打字不会被打断)────────────────────────────
const COMPOSER = {
  new: { turn: () => "写下议题,发送后 AI 们开始发言", you: true, placeholder: "你想讨论什么?写得越具体,讨论越到点。", primary: "开始讨论" },
  user: { turn: (view) => `轮到你了 · 第 ${view.round} 轮`, you: true, placeholder: "写下你的意见…", primary: "发送,进入下一轮", finish: true },
  running: { turn: (view) => `${nameOf(speakerNow(view))} ${jobNow(view)}。想补充就直接写`, placeholder: "想插一句?写在这里,点「插入发言」,它会带着你的话重说这一棒…", primary: "插入发言", interrupt: true },
  paused: { turn: () => "讨论暂停了。可以补充一句再继续,也可以直接点上面的按钮", placeholder: "补充说明(可选)…", primary: "发送并继续", finish: true },
  done: { turn: () => "已收工。想接着聊,直接写下新想法", placeholder: "又想到新的了?写在这里,接着聊…", primary: "继续讨论" },
};

function buildComposer() {
  $("compose").innerHTML = `
    <div class="turn" id="turnText"><span></span><button class="btn small danger" data-act="stop" hidden>停止这一棒</button></div>
    <div class="box" id="box">
      <textarea id="input" rows="3"></textarea>
      <div class="thumbs" id="thumbs" hidden></div>
      <div class="bar">
        <button class="btn" data-act="pick">＋ 文件</button>
        <input type="file" id="picker" multiple hidden>
        <select class="btn" id="assignee" title="这一回只讨论,还是把活派给一位成员" hidden></select>
        <span class="hint" id="hint"></span>
        <button class="btn danger" data-act="finish">收工</button>
        <button class="btn primary" data-act="send"></button>
      </div>
    </div>`;
  const input = $("input");
  input.addEventListener("input", () => {
    state.drafts.set(state.id, input.value);
    autosize();
    updateButtons();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
  });
  input.addEventListener("paste", (event) => {
    const files = [...event.clipboardData.files];
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  $("picker").addEventListener("change", (event) => { addFiles([...event.target.files]); event.target.value = ""; });
  $("assignee").addEventListener("change", () => updateAssignee());
  acceptDroppedFiles();
}

// 文件拖到窗口任何位置都收进输入区。同时拦住浏览器的默认做法:它会把拖进来的文件直接打开,顶掉整个界面
function acceptDroppedFiles() {
  const box = $("box");
  const carriesFiles = (event) => [...event.dataTransfer.types].includes("Files");
  let fading;
  document.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    box.classList.add("drop");
    clearTimeout(fading);
    fading = setTimeout(() => box.classList.remove("drop"), 150);     // 拖走了就自己熄灭
  });
  document.addEventListener("drop", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    if (state.view) addFiles([...event.dataTransfer.files]);
  });
}

// 标题栏和输入区浮在发言上面(毛玻璃),发言从它们下面滑过。
// 所以要把两者的高度告诉样式,让发言区留出上下边距;原本停在末尾的,高度变了以后仍然停在末尾
function trackBars() {
  const [mid, chat] = [$("mid"), $("chat")];
  const measure = () => {
    const atEnd = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
    mid.style.setProperty("--head-h", `${$("head").offsetHeight}px`);
    mid.style.setProperty("--compose-h", `${$("compose").offsetHeight}px`);
    if (atEnd) chat.scrollTop = chat.scrollHeight;
  };
  const observer = new ResizeObserver(measure);
  observer.observe($("head"));
  observer.observe($("compose"));
}

function autosize() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 260)}px`;
}

function updateComposer({ switched = false } = {}) {
  const view = state.view;
  $("compose").hidden = !view;
  if (!view) return;
  const mode = COMPOSER[view.phase];
  const input = $("input");
  if (switched) {
    input.value = state.drafts.get(view.id) ?? view.draft ?? "";
    autosize();
  }
  $("turnText").className = `turn${mode.you ? " you" : ""}`;
  $("turnText").firstElementChild.textContent = mode.turn(view);
  $("box").className = `box${mode.you ? " you" : ""}`;
  input.placeholder = mode.placeholder;
  document.querySelector('[data-act="stop"]').hidden = view.phase !== "running";
  renderThumbs();
  updateAssignee({ reset: switched });
  updateButtons();
}

// 派活选择:这一回只讨论,还是把活派给一位能动手的成员。选项没变就不重画,免得打断正在选的人
const HINT = "文件和图片可以直接拖进来,也可以粘贴 · ⌘↩ 发送";

function updateAssignee({ reset = false } = {}) {
  const picker = $("assignee");
  const options = state.view.members.filter((id) => person(id).assignable)
    .map((id) => `<option value="${esc(id)}">派活给 ${esc(nameOf(id))}</option>`).join("");
  if (picker.dataset.options !== options) {
    const chosen = picker.value;
    picker.innerHTML = `<option value="">只讨论</option>${options}`;
    picker.dataset.options = options;
    picker.value = [...picker.options].some((option) => option.value === chosen) ? chosen : "";
  }
  if (reset) picker.value = "";
  picker.hidden = !options;
  const assignee = picker.value;
  $("box").classList.toggle("assigning", Boolean(assignee));
  $("hint").textContent = assignee
    ? `这一回 ${nameOf(assignee)} 可以改文件、跑命令;删除、覆盖这类事它会先问你。其他成员只审查。`
    : HINT;
}

// 发送 / 收工按钮此刻能不能点(每敲一个字都会重新判断)
function updateButtons() {
  const view = state.view;
  if (!view) return;
  const mode = COMPOSER[view.phase];
  const hasContent = Boolean($("input").value.trim()) || (state.attachments.get(view.id) || []).length > 0;
  const cannotInterrupt = mode.interrupt && (view.job !== "turn" || !hasContent);
  const sendButton = document.querySelector('[data-act="send"]');
  sendButton.textContent = mode.primary;
  sendButton.disabled = state.sending || cannotInterrupt;
  const finishButton = document.querySelector('[data-act="finish"]');
  finishButton.hidden = !mode.finish;
  finishButton.disabled = state.sending;
}

// 待发的附件:图片显示缩略图,其余显示一枚文件签
function renderThumbs() {
  const attachments = state.attachments.get(state.id) || [];
  const thumbs = $("thumbs");
  thumbs.hidden = !attachments.length;
  thumbs.innerHTML = attachments.map((attachment, index) => `<div class="thumb${attachment.image ? "" : " file"}">
    ${attachment.image ? `<img src="${at(state.id)}/files/${enc(attachment.name)}" alt="">` : `<span>${esc(attachment.label)}</span>`}
    <button data-act="unpick" data-index="${index}" title="移除">×</button></div>`).join("");
}

async function addFiles(files) {
  const id = state.id;
  for (const file of files) {
    try {
      const saved = await api.attach(id, file);
      state.attachments.set(id, [...(state.attachments.get(id) || []), saved]);
    } catch (error) { toast(`「${file.name}」没加上:${error.message}`); }
  }
  if (id === state.id) updateComposer();
}

async function send({ finish = false, extend = false } = {}) {
  const view = state.view;
  if (!view || state.sending) return;
  const interrupt = Boolean(COMPOSER[view.phase].interrupt);
  const typed = $("input").value.trim();
  const attachments = state.attachments.get(view.id) || [];
  if (view.phase === "paused" && !finish && !typed && !attachments.length) { act("resume"); return; }
  const text = [typed, ...attachments.map((attachment) => attachment.markdown)].filter(Boolean).join("\n\n");
  state.sending = true;
  updateComposer();
  try {
    const assignee = finish ? "" : $("assignee").value;
    const answer = await api.send(view.id, { text, finish, extend, interrupt, assignee });
    if (answer.need_extend) {
      confirmDialog("到轮数上限了", `这场讨论已经到了 ${answer.max_rounds} 轮的上限。要再延长 ${answer.extend_by} 轮继续吗?`,
        `延长 ${answer.extend_by} 轮`, () => { send({ finish, extend: true }); });
      return;
    }
    $("input").value = "";
    autosize();
    state.drafts.delete(view.id);
    state.attachments.delete(view.id);
    $("assignee").value = "";                               // 派活只管这一回,下一回默认只讨论
    await refreshView({ toEnd: true });
  } catch (error) {
    toast(error.message);
  } finally {
    state.sending = false;
    updateComposer();
  }
}

// ── 右栏:上下文、白板、成员 ────────────────────────────────────────────────
function daysLeft(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : Math.floor((date - Date.now()) / 86400000);
}

function renderSide() {
  const view = state.view;
  const side = $("side");
  if (!view) { side.innerHTML = ""; return; }
  const board = view.board;
  const over = board.tokens > board.limit;
  const hasItems = board.sections.some((section) => section.items.length);
  const canTidy = hasItems && view.phase !== "running";
  const usage = view.members.filter((id) => view.health[id]?.ctx).map((id) => {
    const { ctx, window } = view.health[id];
    return `<div class="row">${esc(nameOf(id))} 的上下文 <span class="v">${tokens(ctx)}</span>${window ? ` / ${tokens(window)}` : ""}</div>`;
  }).join("");
  const members = view.members.map((id) => {
    const who = person(id);
    const left = view.health[id]?.expires ? daysLeft(view.health[id].expires) : null;
    const hint = who.missing ? `<div class="h warn">已不在成员名单里</div>`
      : left !== null && left <= 7 ? `<div class="h warn">登录还有 ${Math.max(left, 0)} 天到期,记得重新登录</div>`
      : `<div class="h">${esc(KIND_LABEL[who.kind] || who.kind)}${who.model ? ` · ${esc(who.model)}` : ""}</div>`;
    return `<div class="member">${avatar(who, true)}<div><div class="n">${esc(who.name)}</div>${hint}</div></div>`;
  }).join("");
  side.innerHTML = `
    <div class="sec">上下文</div>
    <div class="panel${over ? " over" : ""}">
      白板长度 <span class="v">${tokens(board.tokens)}</span> / 建议上限 ${tokens(board.limit)}
      <div class="track"><i style="width:${Math.min(100, (board.tokens / board.limit) * 100)}%"></i></div>
      ${over ? `<div class="warn">白板偏长了,AI 每一棒都要读它。建议整理一下。</div>` : "AI 每一棒只读白板和最近的发言,不读全文。"}
      ${board.problem ? `<div class="warn">${esc(board.problem)}</div>` : ""}
      ${usage}
      ${usage ? `<div class="row fine">每一棒都从头读起:这就是它最近一棒的全部占用,不会一轮轮累加。</div>` : ""}
      ${canTidy && over ? `<div class="actions"><button class="btn primary small" data-act="tidy">让 AI 整理白板</button></div>` : ""}
    </div>
    <div class="sec">成员<button class="link" data-act="settings">编辑</button></div>
    <div class="members">${members}
      <div class="member">${avatar(person("USER"), true)}<div><div class="n">${esc(nameOf("USER"))}</div><div class="h">决策者 · 压轴发言</div></div></div></div>
    <div class="sec">白板${canTidy && !over ? `<button class="link" data-act="tidy">整理</button>` : ""}</div>
    <div class="board">${boardHtml(view)}</div>`;
}

async function refreshView({ toEnd = false, switched = false } = {}) {
  if (!state.id) { state.view = null; } else {
    try { state.view = await api.view(state.id); }
    catch { state.id = null; state.view = null; }
  }
  renderHead();
  renderChat({ toEnd: toEnd || switched });
  updateComposer({ switched });
  renderSide();
}

async function open(id) {
  state.id = id;
  history.replaceState(null, "", id ? `#${enc(id)}` : "#");
  renderList();
  await refreshView({ switched: true });
  if (state.view) $("input").focus();
}

// ── 操作 ──────────────────────────────────────────────────────────────────
const act = guarded(async (action, body) => { await api.act(state.id, action, body); });

function newDiscussion() {
  dialog({
    title: "新建讨论",
    body: `<p>先起个名字。真正要讨论的问题,下一步在输入框里写。</p>
      <input class="field" id="newTitle" placeholder="例:阳台要不要装遮阳棚" maxlength="60">`,
    actions: [{ label: "取消" }, {
      label: "新建", kind: "primary",
      run: async () => { const { id } = await api.create($("newTitle").value); await refreshList(); await open(id); },
    }],
  });
}

const titleOf = (id) => state.list.find((row) => row.id === id)?.title ?? "";

function rename(id) {
  dialog({
    title: "改名",
    body: `<p>只改显示的标题,不改文件名。</p><input class="field" id="renameTitle" maxlength="60" value="${esc(titleOf(id))}">`,
    actions: [{ label: "取消" }, {
      label: "保存", kind: "primary", run: () => api.act(id, "rename", { title: $("renameTitle").value }),
    }],
  });
}

// 一场讨论的操作菜单:右键点列表里的讨论,或点右上角的「⋯」
function discussionMenu(id) {
  const row = state.list.find((item) => item.id === id);
  const canSync = row.phase === "done" && state.settings.nas_sync;
  return [
    { label: row.pinned ? "取消置顶" : "置顶", run: guarded(() => api.act(id, "pin", { pinned: !row.pinned })) },
    { label: "改名", run: () => rename(id) },
    { label: "导出为网页文件", run: () => exportDiscussion(id) },
    canSync && { label: "立即同步到 NAS", run: guarded(async () => { await api.act(id, "sync"); toast("已开始同步。"); }) },
    { label: "移到废纸篓", danger: true, run: () => trash(id) },
  ];
}

const TRASH_RESULT = {
  kept: "已移到废纸篓。",
  moved: "已移到废纸篓。NAS 上的备份已挪进「已删除」文件夹。",
  none: "已移到废纸篓。NAS 上没有它的备份。",
  offline: "已移到废纸篓。NAS 没连上,NAS 上的备份没有动。",
  failed: "已移到废纸篓。挪动 NAS 上的备份时出错,NAS 上的备份没有动。",
};

function trash(id) {
  const hasNas = state.settings.nas_sync && state.settings.nas_dir;
  dialog({
    title: "移到废纸篓",
    body: `<p>「${esc(titleOf(id))}」连同它的白板、附图会被移进废纸篓。后悔了可以从废纸篓里放回来。</p>
      ${hasNas ? `<label class="switch"><input type="checkbox" id="trashNas">NAS 上的备份也一起移走</label>
      <p class="fine">不会直接删除:会挪进 NAS 备份文件夹里的「已删除_日期」,你确认不要了再自己清空。不勾则 NAS 上原样保留。</p>` : ""}`,
    actions: [{ label: "取消" }, {
      label: "移到废纸篓", kind: "danger",
      run: async () => {
        const result = await api.act(id, "trash", { nas: Boolean($("trashNas")?.checked) });
        toast(TRASH_RESULT[result.nas] ?? TRASH_RESULT.kept);
        if (id === state.id) await open(null);
      },
    }],
  });
}

const exportDiscussion = guarded(async (id) => {
  if (id !== state.id) await open(id);                    // 导出的就是界面上看到的这一场
  const view = state.view;
  const html = await buildExport({
    title: view.title,
    subtitle: `${[...view.members, "USER"].map(nameOf).join("、")} · 共 ${view.round} 轮 · 导出于 ${new Date().toLocaleString("zh-CN")}`,
    chatHtml: messagesHtml(view),
    boardHtml: boardHtml(view),
    css: await (await fetch("/web/app.css")).text(),
  });
  const { path } = await api.exportFile(view.id, html);
  toast(`已导出到「下载」文件夹:${path.split("/").pop()}`);
});

// ── 外观:主题、字号、你的气泡颜色 ───────────────────────────────────────────
const ZOOM = { small: 0.9, medium: 1, large: 1.12, xlarge: 1.25 };

function applyAppearance() {
  const { theme, text_size: size, list_hidden: hidden } = state.settings;
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme"); else root.dataset.theme = theme;
  document.querySelector(".app").classList.toggle("list-hidden", hidden);
  Object.assign($("foldButton"), hidden ? { textContent: "›", title: "展开讨论目录" }
    : { textContent: "‹", title: "收起讨论目录(不让旁人看到聊过什么)" });
  const zoom = ZOOM[size] ?? 1;
  if (window.webkit?.messageHandlers?.app) tellApp({ type: "appearance", theme, zoom });
  else document.body.style.zoom = zoom;                     // 在普通浏览器里的替代做法
}

function zoom(src) {
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.innerHTML = `<img class="zoom" src="${esc(src)}" alt="">`;
  veil.onclick = () => veil.remove();
  $("overlay").append(veil);
}

// 改一两项设置并立刻生效。界面的折叠状态也记在设置里:重启后还是原样,收起来的不会自己露出来
const saveSetting = guarded(async (changes) => {
  state.settings = await api.saveSettings({ ...state.settings, ...changes });
  applyAppearance();
  renderList();
});

// ── 设置 ──────────────────────────────────────────────────────────────────
const KIND_LABEL = { claude: "订阅 · Claude Code", codex: "订阅 · Codex", ollama: "本地模型 · Ollama", command: "命令行工具", user: "你自己" };
const KIND_PREFIX = { claude: "CLAUDE", codex: "CODEX", ollama: "LOCAL", command: "CLI" };
const KIND_COLOR = { claude: "#c9822b", codex: "#2f8f6b", ollama: "#7a5ea8", command: "#6b7fa8" };

function openSettings() {
  const draft = structuredClone(state.settings);
  const form = document.createElement("div");
  form.className = "set";
  const everyone = () => [draft.user, ...draft.members];

  const personRow = (who, index) => {
    const isUser = index === 0;
    const position = index - 1;
    const extra = who.kind === "command"
      ? `<input class="field" data-key="command" value="${esc(who.command)}" placeholder="命令行:提示词从标准输入进,回复从标准输出出">`
      : isUser ? `<span>你的名字和头像会显示在每条发言上</span>`
      : `<input class="field" data-key="model" value="${esc(who.model)}" placeholder="${who.kind === "ollama" ? "模型名(必填),如 qwen3:32b" : "指定模型(留空用默认)"}">`;
    return `<div class="person" data-index="${index}">
      <div class="top">
        <span data-act="avatar" title="点击上传头像图片(PNG / JPG)">${avatar(who)}</span>
        <input class="field short" data-key="avatar" value="${esc(who.avatar)}" maxlength="2" title="文字头像:一两个字或 emoji">
        <input type="color" data-key="color" value="${esc(who.color)}" title="头像底色">
        <input class="field" data-key="name" value="${esc(who.name)}" maxlength="24" placeholder="名字">
        ${isUser ? "" : `<label class="switch" title="新建讨论时是否入群"><input type="checkbox" data-key="enabled"${who.enabled ? " checked" : ""}>入群</label>
        <button class="icon" data-move="-1" title="上移"${position === 0 ? " disabled" : ""}>↑</button>
        <button class="icon" data-move="1" title="下移"${position === draft.members.length - 1 ? " disabled" : ""}>↓</button>
        <button class="icon" data-remove title="移除">✕</button>`}
      </div>
      <div class="more-row"><span class="kind">${esc(KIND_LABEL[who.kind] || who.kind)}</span>${extra}
        ${who.image ? `<button class="link" data-clear-image>改用文字头像</button>` : ""}</div>
    </div>`;
  };

  const render = () => {
    form.innerHTML = `
      <h4>成员(从上到下就是发言顺序,你永远压轴)</h4>
      <div id="people">${everyone().map(personRow).join("")}</div>
      <div class="line"><select class="field num" id="addKind" style="width:170px">
        <option value="ollama">本地模型(Ollama)</option><option value="command">命令行工具</option>
        <option value="claude">Claude Code</option><option value="codex">Codex</option></select>
        <button class="btn" data-add>＋ 添加成员</button></div>
      <div class="tip" style="margin-left:0">成员名单只影响之后新建的讨论。其他有订阅版命令行工具的模型,用「命令行工具」接入。</div>

      <h4>外观</h4>
      <div class="line"><span>主题</span><select class="field num" data-setting="theme" style="width:170px">
        ${[["auto", "跟随系统"], ["light", "浅色"], ["dark", "深色"]].map(([value, label]) => `<option value="${value}"${draft.theme === value ? " selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="line"><span>字号</span><select class="field num" data-setting="text_size" style="width:170px">
        ${[["small", "小"], ["medium", "标准"], ["large", "大"], ["xlarge", "特大"]].map(([value, label]) => `<option value="${value}"${draft.text_size === value ? " selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="tip">每个人发言笺的颜色就是他头像的底色,在上面成员那一栏里改。</div>

      <h4>网络</h4>
      <div class="line"><span>AI 联网走代理</span>
        <input class="field" data-setting="proxy" value="${esc(draft.proxy)}" placeholder="留空 = 直连">
        <span class="state ${draft.proxy ? (draft.proxy_alive ? "good" : "bad") : ""}">${draft.proxy ? (draft.proxy_alive ? "代理可用" : "代理没有响应,会改为直连") : "直连"}</span></div>

      <h4>讨论</h4>
      <div class="line"><span>轮数上限</span><input class="field num" type="number" min="1" max="200" data-setting="max_rounds" value="${draft.max_rounds}"><span class="state">到上限时会问你要不要延长</span></div>
      <div class="line"><span>白板长度提醒</span><input class="field num" type="number" min="500" step="500" data-setting="whiteboard_limit" value="${draft.whiteboard_limit}"><span class="state">超过这么多 token 就建议整理</span></div>
      <div class="line"><span>单棒最长等待</span><input class="field num" type="number" min="30" max="3600" step="30" data-setting="turn_timeout" value="${draft.turn_timeout}"><span class="state">秒;超时自动中止这一棒</span></div>
      <div class="line"><span>派活时最长等待</span><input class="field num" type="number" min="60" max="14400" step="60" data-setting="hands_on_timeout" value="${draft.hands_on_timeout}"><span class="state">秒;被派活的那一棒要动手,给它更长的时间</span></div>
      <div class="line" style="align-items:flex-start"><span>派活时还能改的文件夹</span><textarea class="field short" data-setting="hands_on_folders" data-lines placeholder="一行一个,例:/Volumes/外置硬盘">${esc(draft.hands_on_folders.join("\n"))}</textarea></div>
      <div class="tip">只对 Codex 类型的成员有用:它干活时只能改你主目录里的东西,别处由系统拦住;想让它改外置硬盘上的项目,把那个文件夹写在这里。Claude Code 类型的成员不受这个限制。</div>
      <div class="line"><span>轮到我时提醒</span><label class="switch"><input type="checkbox" data-setting="notify"${draft.notify ? " checked" : ""}>程序坞图标跳动并响一声</label></div>
      <div class="line" style="align-items:flex-start"><span>讨论守则</span><textarea class="field" data-setting="rules">${esc(draft.rules)}</textarea></div>
      <div class="tip">每一棒都会交给 AI。改了立刻对之后的发言生效。</div>

      <h4>备份</h4>
      <div class="line"><span>收工时同步到 NAS</span><label class="switch"><input type="checkbox" data-setting="nas_sync"${draft.nas_sync ? " checked" : ""}>开启</label>
        <span class="state ${draft.nas_online ? "good" : "bad"}">${draft.nas_online ? "NAS 已连接" : "NAS 没连上(连上后自动补)"}</span></div>
      <div class="line"><span>NAS 挂载点</span><input class="field" data-setting="nas_mount" value="${esc(draft.nas_mount)}" placeholder="例:/Volumes/home"></div>
      <div class="line"><span>同步到的文件夹</span><input class="field" data-setting="nas_dir" value="${esc(draft.nas_dir)}" placeholder="挂载点下面的某个文件夹"></div>
      <div class="tip">只加不删:电脑上删掉的讨论,NAS 上仍然保留。</div>`;
  };

  form.addEventListener("input", (event) => {
    const field = event.target;
    const value = field.type === "checkbox" ? field.checked
      : field.type === "number" ? Number(field.value)
      : "lines" in field.dataset ? field.value.split("\n").map((line) => line.trim()).filter(Boolean)   // 一行一项
      : field.value;
    const row = field.closest(".person");
    if (row && field.dataset.key) {
      const who = everyone()[Number(row.dataset.index)];
      who[field.dataset.key] = value;
      if (["avatar", "color"].includes(field.dataset.key)) row.querySelector("[data-act=avatar]").innerHTML = avatar(who);
    } else if (field.dataset.setting) {
      draft[field.dataset.setting] = value;
    }
  });
  form.addEventListener("click", guarded(async (event) => {
    const target = event.target.closest("button, [data-act=avatar]");
    if (!target) return;
    const row = target.closest(".person");
    const position = row ? Number(row.dataset.index) - 1 : -1;
    if (target.dataset.move) {
      const other = position + Number(target.dataset.move);
      [draft.members[position], draft.members[other]] = [draft.members[other], draft.members[position]];
    } else if ("remove" in target.dataset) {
      draft.members.splice(position, 1);
    } else if ("clearImage" in target.dataset) {
      everyone()[position + 1].image = "";
    } else if ("add" in target.dataset) {
      const kind = form.querySelector("#addKind").value;
      const used = new Set(draft.members.map((member) => member.id));
      let id = KIND_PREFIX[kind];
      for (let n = 2; used.has(id) || id === "USER"; n += 1) id = `${KIND_PREFIX[kind]}${n}`;
      draft.members.push({ id, name: kind === "ollama" ? "本地模型" : "新成员", kind, avatar: "", color: KIND_COLOR[kind], image: "", enabled: true, model: "", command: "" });
    } else if (target.dataset.act === "avatar") {
      const who = everyone()[position + 1];
      if (!state.settings.members.some((member) => member.id === who.id) && who.id !== "USER") {
        toast("新成员要先保存一次,才能上传头像图片。");
        return;
      }
      const picker = Object.assign(document.createElement("input"), { type: "file", accept: "image/png,image/jpeg" });
      picker.onchange = guarded(async () => {
        who.image = (await api.setAvatar(who.id, picker.files[0])).image;
        render();
      });
      picker.click();
      return;
    } else {
      return;
    }
    render();
  }));

  render();
  dialog({
    title: "设置", body: form, large: true,
    actions: [{ label: "取消" }, {
      label: "保存", kind: "primary",
      run: async () => { state.settings = await api.saveSettings(draft); applyAppearance(); await refreshView(); },
    }],
  });
}

// ── 事件 ──────────────────────────────────────────────────────────────────
const ACTIONS = {
  open: (target) => open(target.dataset.id),
  toggleList: () => saveSetting({ list_hidden: !state.settings.list_hidden }),
  foldGroup: (target) => {
    const folded = state.settings.folded_groups;
    const key = target.dataset.group;
    saveSetting({ folded_groups: folded.includes(key) ? folded.filter((group) => group !== key) : [...folded, key] });
  },
  new: newDiscussion,
  settings: openSettings,
  send: () => send(),
  finish: () => send({ finish: true }),
  pick: () => $("picker").click(),
  unpick: (target) => {
    state.attachments.get(state.id).splice(Number(target.dataset.index), 1);
    updateComposer();
  },
  menu: (target) => {
    const box = target.getBoundingClientRect();
    popup(box.right - 150, box.bottom + 4, discussionMenu(state.id));
  },
  rename: () => rename(state.id),
  copy: async (target) => {
    await copyText(state.view.messages[Number(target.dataset.index)].text);
    toast("已复制这条发言。");
  },
  resume: () => act("resume"),
  skip: () => act("skip"),
  stop: () => act("stop"),
  tidy: () => act("tidy"),
};

document.addEventListener("click", (event) => {
  const image = event.target.closest("img.pic");
  if (image) { zoom(image.src); return; }
  const file = event.target.closest("button.file");                 // 发言里的文件签:在访达里显示那份文件
  if (file) { act("reveal", { name: file.dataset.file }); return; }
  const target = event.target.closest("[data-act]");
  if (target && ACTIONS[target.dataset.act] && !target.closest(".set")) ACTIONS[target.dataset.act](target);
});
$("foldButton").dataset.act = "toggleList";
$("newButton").dataset.act = "new";
$("settingsButton").dataset.act = "settings";
let searchTimer;
$("search").addEventListener("input", (event) => {                 // 搜标题,也搜每一条发言
  const query = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(guarded(async () => {
    state.matches = query ? new Map((await api.search(query)).map((hit) => [hit.id, hit.snippet])) : null;
    renderList();
  }), 180);
});

document.addEventListener("contextmenu", (event) => {
  const item = event.target.closest(".item");
  if (item) {
    event.preventDefault();
    popup(event.clientX, event.clientY, discussionMenu(item.dataset.id));
    return;
  }
  // 其余地方:选中了文字、或在输入框里,才保留系统菜单(拷贝 / 粘贴);
  // 否则不弹——系统菜单里的"重新载入"容易让人以为是对讨论的操作
  const editing = event.target.closest("input, textarea");
  if (!editing && !String(getSelection()).trim()) event.preventDefault();
});

setInterval(() => {
  const timer = $("elapsed");
  if (timer) timer.textContent = elapsed(Number(timer.dataset.started));
}, 1000);

function listen() {
  const source = new EventSource("/api/events");
  let wasDown = false;
  source.onmessage = guarded(async ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "list") await refreshList();
    else if (event.type === "view" && event.id === state.id) await refreshView();
    else if (event.type === "live" && event.id === state.id) updateLive(event.live);
    else if (event.type === "settings") { state.settings = await api.settings(); applyAppearance(); await refreshView(); }
  });
  source.onerror = () => { wasDown = true; };
  source.onopen = guarded(async () => {                 // 断线重连后,把错过的变化补回来
    if (wasDown) { wasDown = false; await refreshList(); await refreshView(); }
  });
}

guarded(async () => {
  buildComposer();
  trackBars();
  state.settings = await api.settings();
  applyAppearance();
  await refreshList();
  const wanted = decodeURIComponent(location.hash.slice(1));
  const first = state.list.find((row) => row.id === wanted)
    || state.list.find((row) => row.phase !== "done") || state.list[0];
  await open(first ? first.id : null);
  listen();
})();
