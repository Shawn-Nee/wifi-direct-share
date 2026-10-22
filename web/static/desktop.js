/* 桌面端控制台 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let session = null;
let lastRev = -1;
let currentIp = '';
let textSeen = new Set();
let textInited = false;

const EXT_COLORS = {
  pdf: '#FF7A85', doc: '#7FA8FF', docx: '#7FA8FF', txt: '#9AA8BC', md: '#9AA8BC',
  xls: '#63D68C', xlsx: '#63D68C', csv: '#63D68C',
  ppt: '#FFA86B', pptx: '#FFA86B', key: '#FFA86B',
  mp4: '#B78BFF', mov: '#B78BFF', mkv: '#B78BFF', avi: '#B78BFF', webm: '#B78BFF',
  jpg: '#FFC46B', jpeg: '#FFC46B', png: '#FFC46B', gif: '#FFC46B', webp: '#FFC46B',
  heic: '#FFC46B', bmp: '#FFC46B', svg: '#FFC46B',
  mp3: '#6BE0E8', wav: '#6BE0E8', flac: '#6BE0E8', m4a: '#6BE0E8', aac: '#6BE0E8',
  zip: '#C0A16B', rar: '#C0A16B', '7z': '#C0A16B', tar: '#C0A16B', gz: '#C0A16B',
  apk: '#8FD98F', exe: '#8FD98F', hap: '#8FD98F', dmg: '#8FD98F',
  psd: '#7FA8FF', ai: '#FFA86B', sketch: '#FFC46B',
};

/* ---------- 基础工具 ---------- */
function toast(msg, isErr = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 260);
  }, 2200);
}

function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 45) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString('zh-CN');
}

function extOf(name, ext) {
  if (ext) return ext.slice(0, 3);
  const m = /\.([^.]+)$/.exec(name);
  return m ? m[1].slice(0, 3) : 'file';
}

function iconStyle(f) {
  const ext = (f.ext || '').toLowerCase();
  const color = EXT_COLORS[ext] || '#7B8CA3';
  return `background:${color}`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

/* ---------- 会话初始化 ---------- */
function renderDir(path, error) {
  if (!path) return;
  $('#dlDir').textContent = path.split(/[\\/]/).filter(Boolean).pop() || path;
  $('#dlDir').title = path;
  const warn = $('#dlWarn');
  if (error) {
    warn.textContent = error;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }
}

async function loadSession() {
  const r = await fetch('/api/session');
  session = await r.json();
  currentIp = session.ip;
  // 标题带上端口：多开时按标题定位窗口才不会认错，用户也能看出是哪一份
  document.title = `WIFI 直连 · 文件快传 (${session.port})`;
  $('#qr').src = '/api/qr.png';
  $('#addrText').textContent = `${session.ip}:${session.port}`;
  $('#addr').title = '点击复制完整链接：' + session.url;
  $('#hostname').textContent = session.hostname || '—';
  $('#ipText').textContent = session.ip;
  renderDir(session.downloadDir, session.downloadDirError);
  if (session.ips && session.ips.length > 1) {
    $('#ipText').style.cursor = 'pointer';
    $('#ipText').title = '点击切换网卡地址：' + session.ips.join(' / ');
  }
}

$('#ipText').addEventListener('click', async () => {
  if (!session || !session.ips || session.ips.length < 2) return;
  const list = session.ips;
  const idx = list.indexOf(currentIp);
  currentIp = list[(idx + 1) % list.length];
  $('#ipText').textContent = currentIp;
  const url = `http://${currentIp}:${session.port}/j/${session.token}`;
  $('#addrText').textContent = `${currentIp}:${session.port}`;
  $('#addr').title = '点击复制完整链接：' + url;
  $('#qr').src = `/api/qr.png?ip=${encodeURIComponent(currentIp)}&r=${Date.now()}`;
  session.url = url;
  toast('已切换到 ' + currentIp);
});

$('#addr').addEventListener('click', async () => {
  if (!session) return;
  (await copyText(session.url)) ? toast('链接已复制') : toast('复制失败', true);
});

/* ---------- 渲染 ---------- */
const ICON_REVEAL = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>`;
const ICON_COPY = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.2"/><path d="M15 5.8A1.8 1.8 0 0 0 13.2 4H5.8A1.8 1.8 0 0 0 4 5.8v7.4A1.8 1.8 0 0 0 5.8 15"/></svg>`;
const ICON_COPIED = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="m4.5 12.6 5 5L19.5 7"/></svg>`;
const ICON_TRASH = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>`;

function fileRow(f, side) {
  const sub = [
    f.sizeText,
    ago(f.created),
    f.downloads > 0 ? `已下载 ${f.downloads} 次` : '',
    f.missing ? '源文件已不在' : '',
  ].filter(Boolean).join(' · ');

  const delTitle = side === 'out' ? '从列表移除' : '删除文件';
  // 「复制文件」只给手机传上来的文件：发给手机的那些本来就在本机，直接去文件夹复制更顺
  const copyBtn = side === 'in'
    ? `<button class="icon-btn" data-act="copy" data-id="${f.id}" title="复制文件（可粘贴到聊天、文档、资源管理器）">${ICON_COPY}</button>`
    : '';
  const acts = `
    ${copyBtn}
    <button class="icon-btn" data-act="reveal" data-id="${f.id}" title="在文件夹中显示">${ICON_REVEAL}</button>
    <button class="icon-btn danger" data-act="del" data-id="${f.id}" title="${delTitle}">${ICON_TRASH}</button>`;

  return `<div class="file">
    <div class="file-icon" style="${iconStyle(f)}">${extOf(f.name, f.ext)}</div>
    <div class="file-meta">
      <div class="file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
      <div class="file-sub" ${f.missing ? 'style="color:#FF6B7A"' : ''}>${escapeHtml(sub)}</div>
    </div>
    <div class="file-actions">${acts}</div>
  </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function emptyBox(text, icon = '◫') {
  return `<div class="empty"><div><div class="big">${icon}</div>${text}</div></div>`;
}

function render(state) {
  const out = state.files.filter((f) => f.origin === 'pc');
  const inc = state.files.filter((f) => f.origin === 'phone');

  $('#cntOut').textContent = out.length;
  $('#cntIn').textContent = inc.length;
  $('#cntText').textContent = state.texts.length;

  $('#listOut').innerHTML = out.length
    ? out.map((f) => fileRow(f, 'out')).join('')
    : '';

  $('#listIn').innerHTML = inc.length
    ? inc.map((f) => fileRow(f, 'in')).join('')
    : emptyBox('手机传上来的文件会出现在这里');

  // 上传进度（手机正在发）
  if (state.uploads && state.uploads.length) {
    $('#uploadArea').innerHTML = state.uploads.map((u) => {
      const pct = u.size ? Math.min(100, Math.round((u.done / u.size) * 100)) : 0;
      return `<div class="upload-row">
        <div style="display:flex;justify-content:space-between;font-size:12.5px">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">手机正在发送 · ${escapeHtml(u.name)}</span>
          <span class="mono" style="color:var(--accent)">${pct}%</span>
        </div>
        <div class="bar"><i style="width:${pct}%"></i></div>
      </div>`;
    }).join('');
  } else {
    $('#uploadArea').innerHTML = '';
  }

  renderTexts(state.texts);
}

function renderTexts(texts) {
  if (!texts.length) {
    $('#textList').innerHTML = emptyBox('双向文字剪贴板，手机上复制的文字可以直接出现在电脑上', '⌨');
    return;
  }
  $('#textList').innerHTML = texts.map((t) => `
    <div class="text-item">
      <div class="tag">
        <i class="dot ${t.origin === 'pc' ? '' : 'on'}"></i>
        ${t.origin === 'pc' ? '电脑发出' : '手机发来'} · ${ago(t.created)}
      </div>
      <div class="text-body">${escapeHtml(t.content)}</div>
      <button class="btn btn-sm copy" data-copy="${t.id}">复制</button>
    </div>`).join('');

  // 手机发来的新文字自动进 Windows 剪贴板
  if (textInited) {
    for (const t of texts) {
      if (!textSeen.has(t.id) && t.origin === 'phone') {
        fetch('/api/pc/clipboard', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: t.content }),
        }).then(() => toast('手机文字已复制到电脑剪贴板'));
      }
    }
  }
  texts.forEach((t) => textSeen.add(t.id));
  textInited = true;
}

/* ---------- 轮询 ---------- */
let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const s = await r.json();
    const online = s.phoneOnline;
    $('#phoneDot').className = 'dot' + (online ? ' on' : '');
    $('#phoneText').textContent = online ? '手机已连接' : '等待手机连接';

    if (s.rev !== lastRev) {
      lastRev = s.rev;
      render(s);
    } else if (s.uploads !== undefined) {
      // 上传进度不改变 rev，单独刷新进度区
      if (s.uploads.length) renderUploads(s.uploads);
      else if ($('#uploadArea').innerHTML) $('#uploadArea').innerHTML = '';
    }
  } catch (e) {
    $('#phoneDot').className = 'dot pending';
    $('#phoneText').textContent = '连接已断开';
  } finally {
    polling = false;
  }
}

function renderUploads(list) {
  $('#uploadArea').innerHTML = list.map((u) => {
    const pct = u.size ? Math.min(100, Math.round((u.done / u.size) * 100)) : 0;
    return `<div class="upload-row">
      <div style="display:flex;justify-content:space-between;font-size:12.5px">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">手机正在发送 · ${escapeHtml(u.name)}</span>
        <span class="mono" style="color:var(--accent)">${pct}%</span>
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  }).join('');
}

/* ---------- 文件交互 ---------- */
async function pickFiles() {
  const btn = $('#pickBtn');
  if (btn.dataset.busy === '1') return;
  btn.dataset.busy = '1';
  const label = btn.textContent;
  btn.textContent = '选择中…';
  try {
    const r = await fetch('/api/pc/pick', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) {
      toast(j.error || '打开文件框失败', true);
      return;
    }
    if (j.added.length) toast(`已添加 ${j.added.length} 个文件`);
    poll();
  } catch (e) {
    toast('添加失败：' + e.message, true);
  } finally {
    btn.dataset.busy = '0';
    btn.textContent = label;
  }
}

$('#pickBtn').addEventListener('click', pickFiles);

function uploadLocal(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', '/api/upload?name=' + encodeURIComponent(file.name));
    xhr.onload = () => (xhr.status === 200 ? resolve() : reject(new Error(xhr.status)));
    xhr.onerror = () => reject(new Error('network'));
    xhr.send(file);
  });
}

/* 拖拽 */
const drop = $('#drop');
['dragenter', 'dragover'].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add('hot');
  }));
['dragleave', 'drop'].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === 'dragleave' && drop.contains(e.relatedTarget)) return;
    drop.classList.remove('hot');
  }));

drop.addEventListener('drop', async (e) => {
  const files = Array.from(e.dataTransfer.files || []);
  if (!files.length) return;
  if (files.length > 1) toast(`正在添加 ${files.length} 个文件…`);
  for (const f of files) {
    try {
      await uploadLocal(f);
    } catch (err) {
      toast(`「${f.name}」添加失败`, true);
    }
  }
  poll();
});

window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

/* 列表操作 */
async function onListClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;

  if (act === 'del') {
    const r = await fetch('/api/file/' + id, { method: 'DELETE' });
    const j = await r.json();
    if (j.ok) toast('已移除');
    poll();
  } else if (act === 'reveal') {
    const r = await fetch('/api/pc/reveal/' + id, { method: 'POST' });
    const j = await r.json();
    if (!j.ok) toast(j.error || '打开失败', true);
  } else if (act === 'copy') {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    let j = {};
    try {
      const r = await fetch('/api/pc/copyfile/' + id, { method: 'POST' });
      j = await r.json().catch(() => ({}));
    } catch (err) {
      j = { ok: false, error: '网络异常，复制失败' };
    } finally {
      btn.dataset.busy = '';
      btn.disabled = false;
    }
    if (!j.ok) {
      toast(j.error || '复制失败', true);
      return;
    }
    // 就地给个「已复制」的瞬时反馈，鼠标不用离开那一行
    btn.classList.add('ok');
    btn.innerHTML = ICON_COPIED;
    btn.title = '已复制，可直接粘贴';
    setTimeout(() => {
      // 期间列表可能已经重绘，节点被换掉了就别再动它
      if (!btn.isConnected) return;
      btn.classList.remove('ok');
      btn.innerHTML = ICON_COPY;
      btn.title = '复制文件（可粘贴到聊天、文档、资源管理器）';
    }, 1300);
    toast('已复制，到其他软件里 Ctrl+V 即可');
  }
}

$('#listOut').addEventListener('click', onListClick);
$('#listIn').addEventListener('click', onListClick);

$('#openDir').addEventListener('click', async () => {
  const r = await fetch('/api/pc/opendir', { method: 'POST' });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok) toast(d.error || '打不开接收目录', true);
});

/* 更改接收目录：原生文件夹框选 → 校验入库 → 立即生效（不用重启） */
$('#pickDir').addEventListener('click', async () => {
  const btn = $('#pickDir');
  if (btn.dataset.busy === '1') return;
  btn.dataset.busy = '1';
  btn.textContent = '…';
  try {
    const r = await fetch('/api/pc/pickdir', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) {
      toast(d.error || '打开选择框失败', true);
      return;
    }
    if (d.cancelled || !d.path) return;   // 用户取消了，什么都不做

    const saved = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ downloadDir: d.path }),
    });
    const s = await saved.json().catch(() => ({}));
    if (!saved.ok || !s.ok) {
      const msg = s.error || '这个目录不能用';
      renderDir(s.downloadDir || (session && session.downloadDir), msg);
      toast(msg, true);
      return;
    }
    if (session) session.downloadDir = s.downloadDir;
    renderDir(s.downloadDir, '');
    toast('接收目录已改为 ' + s.downloadDir);
  } catch (e) {
    toast('更改失败：' + e.message, true);
  } finally {
    btn.dataset.busy = '';
    btn.textContent = '更改';
  }
});

$('#clearIn').addEventListener('click', async () => {
  const r = await fetch('/api/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ origin: 'phone' }),
  });
  const j = await r.json();
  toast(j.removed ? `已清空 ${j.removed} 条记录` : '没有可清空的');
  poll();
});

/* ---------- 文字 ---------- */
$('#sendText').addEventListener('click', sendText);
$('#textBox').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendText();
  }
});

async function sendText() {
  const box = $('#textBox');
  const content = box.value.trim();
  if (!content) return;
  const r = await fetch('/api/text', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, origin: 'pc' }),
  });
  if (r.ok) {
    box.value = '';
    toast('已发送到手机');
    poll();
  } else {
    toast('发送失败', true);
  }
}

const textList = $('#textList');
textList.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy]');
  if (btn) {
    // 直接从 DOM 的文本节点取内容不可靠，改为重新请求
    const r = await fetch('/api/state');
    const s = await r.json();
    const t = s.texts.find((x) => x.id === btn.dataset.copy);
    if (t) {
      (await copyText(t.content)) ? toast('已复制') : toast('复制失败', true);
    }
  }
});

/* ---------- 标签页 ---------- */
function activateTab(view) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  $$('.view').forEach((v) => (v.hidden = v.id !== 'view-' + view));
}

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => activateTab(tab.dataset.view));
});

const initialView = location.hash.replace('#', '');
if (['out', 'in', 'text'].includes(initialView)) activateTab(initialView);

/* ---------- 截图发送 ---------- */
// 框选在独立的整屏覆盖层里完成（/shot 页面，Edge --app 全屏窗口）：
// 主窗口只负责发起抓屏。服务端抓完会把主窗口最小化，再开覆盖层接管整个屏幕，
// 用户于是在完整桌面上、按真实分辨率框选，而不是在窗口里的小缩略图上比划。
let shotBusy = false;

async function openShot() {
  if (shotBusy) return;
  shotBusy = true;
  const btn = $('#shotBtn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/pc/screenshot', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '抓屏失败');
    // 覆盖层此时已经在屏幕上；选中并发送后，文件会通过轮询出现在列表里
  } catch (e) {
    toast(e.message || '抓屏失败，可能是当前会话没有桌面权限', true);
  } finally {
    shotBusy = false;
    btn.disabled = false;
  }
}

$('#shotBtn').addEventListener('click', openShot);

document.addEventListener('keydown', (e) => {
  if (e.key === 'PrintScreen' || (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 's')) {
    e.preventDefault();
    activateTab('out');
    openShot();
  }
});

/* ---------- 启动 ---------- */
(async function boot() {
  await loadSession();
  await poll();
  setInterval(poll, 700);
})();
