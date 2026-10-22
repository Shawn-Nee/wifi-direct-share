/* 全屏截图框选。
 *
 * 坐标体系（关键，弄错就会"框的和截的不一致"）：
 *   原图按 object-fit:contain 铺在视口里 —— 单屏且比例一致时正好等于视口，
 *   多屏时居中等比缩小。所以图片的真实显示矩形要用 dispRect() 自己算，
 *   不能直接拿 img.getBoundingClientRect()（那个返回的是整个视口）。
 *   选区内部记为「相对显示矩形左上角」的 CSS 像素，发送时再统一乘回原图比例。
 */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const img = $('#shotImg');
  const veil = $('#veil');
  const sel = $('#sel');
  const tag = $('#tag');
  const hint = $('#hint');
  const bar = $('#bar');
  const btnSend = $('#btnSend');
  const btnFull = $('#btnFull');
  const btnCancel = $('#btnCancel');
  const toastEl = $('#toast');

  const qs = new URLSearchParams(location.search);
  const sid = qs.get('sid') || '';
  const port = qs.get('port') || '';
  document.title = '框选截图 · WIFI 直连' + (port ? ' (' + port + ')' : '');

  const MIN = 4;          // 小于它就当误点，不当作选区
  const HIT = 9;          // 手柄命中半径（像素）
  const CORNERS = ['nw', 'ne', 'se', 'sw', 'n', 'e', 's', 'w'];
  const CURSOR = {
    nw: 'nwse-resize', se: 'nwse-resize',
    ne: 'nesw-resize', sw: 'nesw-resize',
    n: 'ns-resize', s: 'ns-resize',
    e: 'ew-resize', w: 'ew-resize',
  };

  let W = 0;              // 原图像素宽
  let H = 0;              // 原图像素高
  let box = null;         // {x, y, w, h} 相对显示矩形
  let mode = null;        // 'new' | 'move' | 'resize'
  let grip = null;
  let anchor = null;      // 拖动起点
  let startBox = null;
  let frame = 0;
  let busy = false;
  let toastTimer = 0;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  /* ---------- 几何 ---------- */
  function dispRect() {
    const vw = innerWidth;
    const vh = innerHeight;
    if (!W || !H) return { left: 0, top: 0, width: vw, height: vh };
    const s = Math.min(vw / W, vh / H);
    const w = W * s;
    const h = H * s;
    return { left: (vw - w) / 2, top: (vh - h) / 2, width: w, height: h };
  }

  function loc(e) {
    const d = dispRect();
    return {
      x: clamp(e.clientX - d.left, 0, d.width),
      y: clamp(e.clientY - d.top, 0, d.height),
    };
  }

  // 选区 → 原图像素矩形。两条边各自取整，比只 round 宽高少丢一列/一行像素。
  function toPixels(b) {
    const d = dispRect();
    if (!d.width || !d.height) return { x: 0, y: 0, w: 0, h: 0 };
    const sx = W / d.width;
    const sy = H / d.height;
    const x0 = clamp(Math.round(b.x * sx), 0, W - 1);
    const y0 = clamp(Math.round(b.y * sy), 0, H - 1);
    const x1 = clamp(Math.round((b.x + b.w) * sx), x0 + 1, W);
    const y1 = clamp(Math.round((b.y + b.h) * sy), y0 + 1, H);
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  function handlePoints(b) {
    const d = dispRect();
    const L = d.left + b.x;
    const T = d.top + b.y;
    const R = L + b.w;
    const B = T + b.h;
    const cx = (L + R) / 2;
    const cy = (T + B) / 2;
    return {
      nw: [L, T], n: [cx, T], ne: [R, T], e: [R, cy],
      se: [R, B], s: [cx, B], sw: [L, B], w: [L, cy],
    };
  }

  function hitHandle(cx, cy) {
    if (!box || box.w < 1 || box.h < 1) return null;
    const pts = handlePoints(box);
    let best = null;
    let bestD = HIT * HIT;
    for (const k of CORNERS) {          // 角排在前面，同距离时角优先
      const dx = cx - pts[k][0];
      const dy = cy - pts[k][1];
      const dd = dx * dx + dy * dy;
      if (dd < bestD) {
        bestD = dd;
        best = k;
      }
    }
    return best;
  }

  const inside = (b, p, pad) => {
    const q = pad || 0;
    return p.x >= b.x - q && p.x <= b.x + b.w + q &&
           p.y >= b.y - q && p.y <= b.y + b.h + q;
  };

  /* ---------- 渲染 ---------- */
  function render() {
    const alive = box && box.w >= 1 && box.h >= 1;
    if (!alive) {
      sel.hidden = true;
      veil.hidden = false;
      hint.hidden = false;
      btnSend.disabled = true;
      return;
    }
    const d = dispRect();
    const l = d.left + box.x;
    const t = d.top + box.y;
    sel.style.left = l + 'px';
    sel.style.top = t + 'px';
    sel.style.width = box.w + 'px';
    sel.style.height = box.h + 'px';
    sel.hidden = false;
    veil.hidden = true;
    hint.hidden = true;
    btnSend.disabled = box.w < MIN || box.h < MIN;

    const px = toPixels(box);
    tag.textContent = px.w + ' × ' + px.h;
    tag.classList.toggle('inside', t < 34 || box.h < 40);
  }

  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      render();
    });
  }

  function hoverCursor(e) {
    if (!box) return 'crosshair';
    const g = hitHandle(e.clientX, e.clientY);
    if (g) return CURSOR[g];
    return inside(box, loc(e)) ? 'move' : 'crosshair';
  }

  /* ---------- 指针 ---------- */
  function onDown(e) {
    if (e.button !== 0 || busy) return;
    if (e.target && e.target.closest && e.target.closest('.bar, .toast')) return;

    const p = loc(e);
    if (box) {
      const g = hitHandle(e.clientX, e.clientY);
      if (g) {
        mode = 'resize';
        grip = g;
        startBox = { x: box.x, y: box.y, w: box.w, h: box.h };
        anchor = p;
        bar.classList.add('dim');
        e.preventDefault();
        return;
      }
      if (inside(box, p)) {
        mode = 'move';
        startBox = { x: box.x, y: box.y, w: box.w, h: box.h };
        anchor = p;
        bar.classList.add('dim');
        e.preventDefault();
        return;
      }
    }
    mode = 'new';
    grip = null;
    startBox = null;
    anchor = p;
    box = { x: p.x, y: p.y, w: 0, h: 0 };
    bar.classList.add('dim');
    e.preventDefault();
    schedule();
  }

  function onMove(e) {
    if (!mode) {
      document.body.style.cursor = hoverCursor(e);
      return;
    }
    const d = dispRect();
    const p = loc(e);

    if (mode === 'new') {
      box = {
        x: Math.min(anchor.x, p.x),
        y: Math.min(anchor.y, p.y),
        w: Math.abs(p.x - anchor.x),
        h: Math.abs(p.y - anchor.y),
      };
    } else if (mode === 'move') {
      box = {
        x: clamp(startBox.x + (p.x - anchor.x), 0, Math.max(0, d.width - startBox.w)),
        y: clamp(startBox.y + (p.y - anchor.y), 0, Math.max(0, d.height - startBox.h)),
        w: startBox.w,
        h: startBox.h,
      };
    } else if (mode === 'resize') {
      let l = startBox.x;
      let t = startBox.y;
      let r = startBox.x + startBox.w;
      let b = startBox.y + startBox.h;
      const dx = p.x - anchor.x;
      const dy = p.y - anchor.y;
      if (grip.indexOf('w') >= 0) l = clamp(startBox.x + dx, 0, r - MIN);
      if (grip.indexOf('e') >= 0) r = clamp(startBox.x + startBox.w + dx, l + MIN, d.width);
      if (grip.indexOf('n') >= 0) t = clamp(startBox.y + dy, 0, b - MIN);
      if (grip.indexOf('s') >= 0) b = clamp(startBox.y + startBox.h + dy, t + MIN, d.height);
      box = { x: l, y: t, w: r - l, h: b - t };
    }
    schedule();
  }

  function onUp(e) {
    if (!mode) return;
    const was = mode;
    mode = null;
    grip = null;
    anchor = null;
    startBox = null;
    bar.classList.remove('dim');
    if (was === 'new' && (!box || box.w < MIN || box.h < MIN)) box = null;
    document.body.style.cursor = hoverCursor(e);
    schedule();
  }

  /* ---------- 键盘 / 右键 ---------- */
  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (box && !btnSend.disabled) send(false);
      return;
    }
    if (!box) return;
    const step = e.shiftKey ? 10 : 1;
    let dx = 0;
    let dy = 0;
    if (e.key === 'ArrowLeft') dx = -step;
    else if (e.key === 'ArrowRight') dx = step;
    else if (e.key === 'ArrowUp') dy = -step;
    else if (e.key === 'ArrowDown') dy = step;
    if (!dx && !dy) return;
    e.preventDefault();
    const d = dispRect();
    box = {
      x: clamp(box.x + dx, 0, Math.max(0, d.width - box.w)),
      y: clamp(box.y + dy, 0, Math.max(0, d.height - box.h)),
      w: box.w,
      h: box.h,
    };
    schedule();
  }

  document.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (box) {
      box = null;
      schedule();
    } else {
      cancel();
    }
  });

  document.addEventListener('dblclick', (e) => {
    e.preventDefault();
    if (box && !btnSend.disabled) send(false);
  });

  /* ---------- 发送 / 取消 / 关闭 ---------- */
  function toast(msg, isErr) {
    toastEl.textContent = msg;
    toastEl.classList.toggle('err', !!isErr);
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, isErr ? 3400 : 1500);
  }

  function bye(msg) {
    toast(msg);
    // 命令行启动的窗口不一定允许脚本关闭，服务端收到响应后也会去关一次
    setTimeout(() => { try { window.close(); } catch (_) { /* 交给服务端 */ } }, 260);
  }

  async function send(full) {
    if (busy) return;
    let body;
    if (full) {
      body = { full: true };
    } else {
      if (!box || box.w < MIN || box.h < MIN) {
        toast('请先框选一块区域', true);
        return;
      }
      body = toPixels(box);
    }
    busy = true;
    btnSend.disabled = true;
    btnFull.disabled = true;
    try {
      const r = await fetch('/api/pc/screenshot/' + sid + '/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || '发送失败');
      // 服务端是先落盘、再把这张图放进剪贴板，最后才回话的，
      // 所以这里拿到的 clip 就是「粘贴可用与否」的确切结果。
      const clip = j.clip || {};
      bye(clip.ok === false
        ? '已发送，但复制到剪贴板失败：' + (clip.error || '未知原因')
        : '已发送，并已复制到剪贴板');
    } catch (err) {
      busy = false;
      btnSend.disabled = !box;
      btnFull.disabled = false;
      toast(err.message || '发送失败', true);
    }
  }

  let cancelled = false;
  function cancel() {
    if (busy || cancelled) return;
    cancelled = true;
    busy = true;
    try {
      fetch('/api/pc/screenshot/' + sid, { method: 'DELETE', keepalive: true })
        .catch(() => {});
    } catch (_) { /* 页面正在关闭 */ }
    try { window.close(); } catch (_) { /* 交给服务端 */ }
  }

  // 用户直接 Alt+F4 或点关闭时不会有回调，用 beacon 兜底让主窗口回来。
  // 进 bfcache 也会触发 pagehide，那时页面可能还会回来接着用，不能丢图。
  addEventListener('pagehide', (e) => {
    if (!sid || e.persisted) return;
    try { navigator.sendBeacon('/api/pc/screenshot/' + sid + '/close'); } catch (_) { /* ignore */ }
  });

  /* ---------- 启动 ---------- */
  img.addEventListener('load', () => {
    W = img.naturalWidth || 0;
    H = img.naturalHeight || 0;
    if (!W || !H) {
      toast('截图读取失败', true);
      return;
    }
    render();
  });

  img.addEventListener('error', () => toast('截图已过期，请重新截取', true));

  addEventListener('resize', schedule);
  addEventListener('pointerdown', onDown);
  addEventListener('pointermove', onMove);
  addEventListener('pointerup', onUp);
  addEventListener('pointercancel', onUp);
  addEventListener('keydown', onKey);

  btnCancel.addEventListener('click', cancel);
  btnFull.addEventListener('click', () => send(true));
  btnSend.addEventListener('click', () => send(false));

  if (!sid) {
    toast('缺少截图标识，请重新截取', true);
  } else {
    img.src = '/api/pc/screenshot/' + sid;
  }
})();
