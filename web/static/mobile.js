/* 手机端页面 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let lastRev = -1;
let queue = [];
let uploading = false;
let dead = false;

const EXT_COLORS = {
  pdf: '#FF7A85', doc: '#7FA8FF', docx: '#7FA8FF', txt: '#9AA8BC', md: '#9AA8BC',
  xls: '#63D68C', xlsx: '#63D68C', csv: '#63D68C',
  ppt: '#FFA86B', pptx: '#FFA86B',
  mp4: '#B78BFF', mov: '#B78BFF', mkv: '#B78BFF', avi: '#B78BFF', webm: '#B78BFF',
  jpg: '#FFC46B', jpeg: '#FFC46B', png: '#FFC46B', gif: '#FFC46B', webp: '#FFC46B',
  heic: '#FFC46B', bmp: '#FFC46B',
  mp3: '#6BE0E8', wav: '#6BE0E8', flac: '#6BE0E8', m4a: '#6BE0E8',
  zip: '#C0A16B', rar: '#C0A16B', '7z': '#C0A16B',
  apk: '#8FD98F', exe: '#8FD98F', hap: '#8FD98F',
};

function toast(msg, isErr = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 260);
  }, 2000);
}

function fmtSize(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + u[i];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 45) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString('zh-CN');
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fallthrough */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

/* ---------- 上传队列 ---------- */
function addFiles(files) {
  for (const f of files) {
    queue.push({
      uid: Math.random().toString(36).slice(2),
      name: f.name,
      size: f.size,
      done: 0,
      file: f,
      status: 'pending',
      lastPaint: 0,
    });
  }
  renderQueue();
  pump();
}

let lastQueueKey = '';
function renderQueue() {
  const host = $('#queueHost');
  if (!queue.length) {
    if (lastQueueKey !== 'empty') {
      host.innerHTML = `<div class="empty" style="padding:18px 0">还没有选择文件</div>`;
      lastQueueKey = 'empty';
    }
    return;
  }
  lastQueueKey = queue.map((t) => t.uid + t.status + Math.round(t.done / 4096)).join('|');

  host.innerHTML = queue.map((t) => {
    const pct = t.size ? Math.min(100, Math.round((t.done / t.size) * 100)) : 0;
    const color = t.status === 'error' ? '#FF6B7A'
      : t.status === 'done' ? '#4ADE80' : '#38E1C4';
    const label = t.status === 'pending' ? '排队中'
      : t.status === 'doing' ? `${pct}% · ${fmtSize(t.done)} / ${fmtSize(t.size)}`
      : t.status === 'done' ? `已完成 · ${fmtSize(t.size)}`
      : '上传失败';
    return `<div class="m-file" style="flex-direction:column;align-items:stretch;gap:7px">
      <div style="display:flex;gap:9px;align-items:center">
        <div class="dl" style="background:rgba(255,255,255,.06);color:${color};font-size:13px">
          ${t.status === 'done' ? '✓' : t.status === 'error' ? '!' : '↑'}
        </div>
        <div style="flex:1;min-width:0">
          <div style="font-size:13.5px;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(t.name)}</div>
          <div style="font-size:11.5px;color:var(--dim);margin-top:2px">${label}</div>
        </div>
      </div>
      ${t.status === 'doing' ? `<div class="bar" style="margin-top:0"><i style="width:${pct}%"></i></div>` : ''}
    </div>`;
  }).join('');
}

async function pump() {
  if (uploading) return;
  uploading = true;
  while (true) {
    const task = queue.find((t) => t.status === 'pending');
    if (!task) break;
    task.status = 'doing';
    renderQueue();
    try {
      await doUpload(task);
      task.status = 'done';
      task.done = task.size;
    } catch (e) {
      task.status = 'error';
      toast(`「${task.name}」上传失败`, true);
    }
    renderQueue();
  }
  uploading = false;
  renderQueue();
  poll();
}

function doUpload(task) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', '/api/upload?name=' + encodeURIComponent(task.name));
    xhr.timeout = 0;
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      task.done = e.loaded;
      const now = performance.now();
      if (now - task.lastPaint > 140) {
        task.lastPaint = now;
        renderQueue();
      }
    };
    xhr.onload = () => {
      if (xhr.status === 200) resolve();
      else reject(new Error('HTTP ' + xhr.status));
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.onabort = () => reject(new Error('已取消'));
    xhr.send(task.file);
  });
}

$('#pickBtn').addEventListener('click', () => $('#fileInput').click());
$('#cameraBtn').addEventListener('click', () => $('#cameraInput').click());
$('#fileInput').addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (files.length) addFiles(files);
});
$('#cameraInput').addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (files.length) addFiles(files);
});

/* ---------- 下载 ---------- */
const IMG_EXTS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp',
  'heic', 'heif', 'avif']);

function downloadFile(id) {
  const a = document.createElement('a');
  a.href = '/api/download/' + id;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('已开始下载，可在通知栏查看进度');
}

function renderDown(files) {
  const host = $('#downList');
  const pc = files.filter((f) => f.origin === 'pc');
  $('#downHint').style.display = pc.length ? 'none' : 'block';
  if (!pc.length) {
    host.innerHTML = '';
    return;
  }
  host.innerHTML = pc.map((f) => {
    const ext = (f.ext || '').toLowerCase();
    const color = EXT_COLORS[ext] || '#7B8CA3';
    const tag = (f.ext || 'file').slice(0, 3).toUpperCase();
    const isImg = IMG_EXTS.has(ext);
    const left = isImg
      ? `<img class="thumb" src="/api/thumb/${f.id}?w=120" alt="" loading="lazy">`
      : `<div class="file-icon" style="background:${color};width:34px;height:34px;border-radius:9px">${tag}</div>`;
    return `<div class="m-file" data-dl="${f.id}" data-img="${isImg ? '1' : '0'}" data-name="${escapeHtml(f.name)}">
      ${left}
      <div style="flex:1;min-width:0">
        <div style="font-size:13.5px;font-weight:550;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(f.name)}</div>
        <div style="font-size:11.5px;color:var(--dim);margin-top:2px">${f.sizeText} · ${ago(f.created)}</div>
      </div>
      <div class="dl">${isImg ? '⤢' : '↓'}</div>
    </div>`;
  }).join('');
}

/* 图片预览：内联渲染，长按由系统「保存图片」写入相册 */
let viewerTipShown = false;

function openViewer(id, name) {
  const img = $('#viewerImg');
  img.onerror = () => toast('这张图浏览器解不了，请点下方「下载到手机文件夹」', true);
  img.src = '/api/img/' + id;
  $('#viewerName').textContent = name || '';
  $('#viewerDl').href = '/api/download/' + id;
  $('#viewer').hidden = false;
  document.body.classList.add('viewer-open');
  if (!viewerTipShown) {
    viewerTipShown = true;
    toast('长按图片 → 「保存图片」即可进相册');
  }
}

function closeViewer() {
  $('#viewer').hidden = true;
  $('#viewerImg').removeAttribute('src');
  document.body.classList.remove('viewer-open');
}

$('#viewerClose').addEventListener('click', closeViewer);
$('.viewer-stage').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeViewer();
});
$('#viewerDl').addEventListener('click', () => toast('已保存到手机下载文件夹'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#viewer').hidden) closeViewer();
});

$('#downList').addEventListener('click', (e) => {
  const row = e.target.closest('[data-dl]');
  if (!row) return;
  if (row.dataset.img === '1') {
    openViewer(row.dataset.dl, row.dataset.name);
  } else {
    downloadFile(row.dataset.dl);
  }
});

// 缩略图生成失败（比如 HEIC 解不了）时退化成彩色图标，别显示破图
document.addEventListener('error', (e) => {
  const el = e.target;
  if (el && el.tagName === 'IMG' && el.classList.contains('thumb')) {
    el.outerHTML = '<div class="file-icon" style="background:#FFC46B;width:42px;height:42px;border-radius:9px">IMG</div>';
  }
}, true);

/* ---------- 文字 ---------- */
function renderTexts(texts) {
  const host = $('#textList');
  if (!texts.length) {
    host.innerHTML = `<div class="empty" style="padding:20px 0">还没有文字记录</div>`;
    return;
  }
  host.innerHTML = texts.map((t) => `
    <div class="m-file" data-copy="${t.id}" style="display:block;padding:12px 13px">
      <div style="font-size:11px;color:var(--dim);margin-bottom:5px">
        ${t.origin === 'pc' ? '电脑发来' : '手机发出'} · ${ago(t.created)} · 点击复制
      </div>
      <div style="font-size:13.5px;white-space:pre-wrap;word-break:break-word">${escapeHtml(t.content)}</div>
    </div>`).join('');
}

let currentTexts = [];
$('#textList').addEventListener('click', async (e) => {
  const row = e.target.closest('[data-copy]');
  if (!row) return;
  const t = currentTexts.find((x) => x.id === row.dataset.copy);
  if (!t) return;
  (await copyText(t.content)) ? toast('已复制') : toast('复制失败，可长按手动选择', true);
});

$('#sendText').addEventListener('click', async () => {
  const box = $('#textBox');
  const content = box.value.trim();
  if (!content) return;
  const r = await fetch('/api/text', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, origin: 'phone' }),
  });
  if (r.ok) {
    box.value = '';
    toast('已发送到电脑剪贴板');
    poll();
  } else {
    toast('发送失败', true);
  }
});

/* ---------- 轮询 ---------- */
let polling = false;
async function poll() {
  if (polling || dead) return;
  polling = true;
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (r.status === 403) {
      dead = true;
      $('#dot').className = 'dot pending';
      $('#status').textContent = '链接已失效，请在电脑上重新扫码';
      return;
    }
    const s = await r.json();
    $('#dot').className = 'dot on';
    $('#status').textContent = '已连接电脑 · 高速直连中';

    if (s.rev !== lastRev) {
      lastRev = s.rev;
      renderDown(s.files);
      renderTexts(s.texts);
      currentTexts = s.texts;
    }
  } catch (e) {
    $('#dot').className = 'dot pending';
    $('#status').textContent = '与电脑连接中断，正在重试…';
  } finally {
    polling = false;
  }
}

/* ---------- 标签 ---------- */
function activateTab(view) {
  $$('.m-tab').forEach((t) => t.classList.toggle('active', t.dataset.v === view));
  ['up', 'down', 'text'].forEach((v) => {
    $('#v-' + v).hidden = v !== view;
  });
}

$$('.m-tab').forEach((tab) => {
  tab.addEventListener('click', () => activateTab(tab.dataset.v));
});

const initialTab = location.hash.replace('#', '');
if (['up', 'down', 'text'].includes(initialTab)) activateTab(initialTab);

/* ---------- 启动 ---------- */
renderQueue();
poll();
setInterval(poll, 1200);
