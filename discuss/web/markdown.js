// 把 Markdown 渲染成 HTML。所有文字先转义再加标签,AI 输出里的 HTML 不会被执行。
//
// renderMarkdown(source, { resolveImage, resolveFile }) → { html, links }
//   resolveImage(路径) 返回本机图片地址;返回 null 的图片不显示,只给一个链接。
//   resolveFile(路径) 返回附件的文件名;是附件的链接画成一枚文件签(button.file,data-file 里是文件名),
//   点它要做什么由界面决定。
//   links 是正文里出现过的网址,供界面做"链接卡片"。

const escapeHtml = (text) => text
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const FENCE = /^\s*(```|~~~)/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const BARE_URL = /https?:\/\/[^\s<>()（）[\]【】"'“”‘’`　-〿＀-￯一-鿿]+/g;

const indentOf = (line) => line.match(/^\s*/)[0].replace(/\t/g, "    ").length;
const isTableStart = (lines, i) =>
  lines[i].includes("|") && i + 1 < lines.length && TABLE_DIVIDER.test(lines[i + 1]);
const startsBlock = (lines, i) =>
  FENCE.test(lines[i]) || HEADING.test(lines[i]) || RULE.test(lines[i])
  || /^\s*>/.test(lines[i]) || LIST_ITEM.test(lines[i]) || isTableStart(lines, i);

export function renderMarkdown(source, { resolveImage = () => null, resolveFile = () => null } = {}) {
  const context = { links: [], resolveImage, resolveFile };
  const lines = source.replace(/\r\n?/g, "\n").replace(/^&lt;!--/gm, "<!--").split("\n");
  return { html: blocks(lines, context), links: context.links };
}

// ── 块级 ──────────────────────────────────────────────────────────────────
function blocks(lines, context) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    const fence = line.match(FENCE);
    if (fence) {
      const code = [];
      for (i += 1; i < lines.length && !lines[i].trim().startsWith(fence[1]); i += 1) code.push(lines[i]);
      i += 1;
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inline(heading[2], context)}</h${level}>`);
      i += 1;
      continue;
    }
    if (RULE.test(line)) { out.push("<hr>"); i += 1; continue; }
    if (/^\s*>/.test(line)) {
      const quoted = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i += 1) quoted.push(lines[i].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${blocks(quoted, context)}</blockquote>`);
      continue;
    }
    if (isTableStart(lines, i)) { i = table(lines, i, out, context); continue; }
    if (LIST_ITEM.test(line)) { i = list(lines, i, out, context); continue; }

    const paragraph = [line];
    for (i += 1; i < lines.length && lines[i].trim() && !startsBlock(lines, i); i += 1) paragraph.push(lines[i]);
    out.push(`<p>${paragraph.map((text) => inline(text.trim(), context)).join("<br>")}</p>`);
  }
  return out.join("");
}

function table(lines, start, out, context) {
  const cells = (line) => line.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((cell) => cell.trim());
  const row = (line, tag) => `<tr>${cells(line).map((cell) => `<${tag}>${inline(cell, context)}</${tag}>`).join("")}</tr>`;
  let i = start + 2;
  const body = [];
  for (; i < lines.length && lines[i].includes("|") && lines[i].trim(); i += 1) body.push(row(lines[i], "td"));
  out.push(`<div class="table-wrap"><table><thead>${row(lines[start], "th")}</thead><tbody>${body.join("")}</tbody></table></div>`);
  return i;
}

function list(lines, start, out, context) {
  const base = indentOf(lines[start]);
  const first = lines[start].match(LIST_ITEM);
  const ordered = /\d/.test(first[2]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    const item = line.match(LIST_ITEM);
    if (item && indentOf(line) <= base + 1) {
      if (/\d/.test(item[2]) !== ordered) break;            // 换了列表类型:另起一个列表
      items.push({ head: item[3], rest: [] });
    } else if (!line.trim()) {
      const next = lines.slice(i + 1).find((later) => later.trim());
      if (next === undefined || (indentOf(next) <= base && !LIST_ITEM.test(next))) break;
      items[items.length - 1].rest.push("");
    } else if (indentOf(line) > base) {
      items[items.length - 1].rest.push(line);
    } else {
      break;
    }
    i += 1;
  }
  const html = items.map(({ head, rest }) => {
    const depth = Math.min(...rest.filter((line) => line.trim()).map(indentOf), Infinity);
    const children = rest.map((line) => line.replace(/\t/g, "    ").slice(Number.isFinite(depth) ? depth : 0));
    return `<li>${inline(head, context)}${blocks(children, context)}</li>`;
  }).join("");
  const startAt = ordered && parseInt(first[2], 10) !== 1 ? ` start="${parseInt(first[2], 10)}"` : "";
  out.push(ordered ? `<ol${startAt}>${html}</ol>` : `<ul>${html}</ul>`);
  return i;
}

// ── 行内 ──────────────────────────────────────────────────────────────────
function inline(text, context) {
  const stash = [];                                          // 已生成的 HTML 先收起来,免得被二次处理
  const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
  const emphasis = (html) => html
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  const link = (url, labelHtml) => {
    if (!context.links.includes(url)) context.links.push(url);
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${labelHtml}</a>`;
  };

  let rest = text.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${escapeHtml(code)}</code>`));
  rest = rest.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (whole, alt, url) => {
    const local = context.resolveImage(url);
    if (local) return keep(`<img class="pic" src="${escapeHtml(local)}" alt="${escapeHtml(alt)}" loading="lazy">`);
    if (/^https?:\/\//.test(url)) return keep(link(url, `🖼 ${escapeHtml(alt || "图片")}`));
    return keep(escapeHtml(whole));
  });
  rest = rest.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, path) => {
    const file = context.resolveFile(path);
    return file ? keep(`<button class="file" data-file="${escapeHtml(file)}" title="在访达里显示">${escapeHtml(label)}</button>`) : whole;
  });
  rest = rest.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_, label, url) => keep(link(url, emphasis(escapeHtml(label)))));
  rest = rest.replace(BARE_URL, (match) => {
    const url = match.replace(/[.,;:!?*_~]+$/, "");
    return keep(link(url, escapeHtml(url))) + match.slice(url.length);
  });
  return emphasis(escapeHtml(rest)).replace(/\u0000(\d+)\u0000/g, (_, index) => stash[Number(index)]);
}
