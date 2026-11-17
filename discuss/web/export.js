// 把一场讨论导出成一个可以单独打开的网页文件:样式和图片都嵌在文件里,发给别人也能看。

const EXTRA_CSS = `
  html, body { height: auto; }
  body { overflow: auto; background: var(--bg); }
  .export { max-width: 860px; margin: 0 auto; padding: 32px 22px 64px; }
  .export > h1 { font: 700 26px/1.4 var(--serif); margin: 0 0 4px; }
  .export > .sub { color: var(--sub); font-size: 13px; margin-bottom: 8px; }
  .export > h2 { font: 700 17px/1.4 var(--serif); margin: 36px 0 10px; letter-spacing: .2em; }
  .export .msg { max-width: none; }
  .export .board { padding: 8px 14px; }
  .copy { display: none !important; }
`;

const escapeHtml = (text) => String(text)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const asDataUrl = (blob) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(reader.result);
  reader.onerror = () => reject(reader.error);
  reader.readAsDataURL(blob);
});

async function embedImage(image) {
  try {
    const response = await fetch(image.getAttribute("src"));
    if (response.ok) image.setAttribute("src", await asDataUrl(await response.blob()));
  } catch { /* 取不到就保留原地址:导出的文件里这张图会显示不出来,其余不受影响 */ }
}

// chatHtml / boardHtml 是界面上已经渲染好的内容
export async function buildExport({ title, subtitle, chatHtml, boardHtml, css }) {
  const page = document.implementation.createHTMLDocument(title);
  page.body.innerHTML = `<main class="export"><h1>${escapeHtml(title)}</h1>
    <div class="sub">${escapeHtml(subtitle)}</div>${chatHtml}
    <h2>白板</h2><div class="board">${boardHtml}</div></main>`;
  await Promise.all([...page.querySelectorAll("img")].map(embedImage));
  return `<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><style>${css}${EXTRA_CSS}</style></head>
<body>${page.body.innerHTML}</body></html>
`;
}
