/* fx.js — 背景飘雪 + 光标光幕
 *
 * 只做两件纯视觉的事，不碰任何业务逻辑：
 *   ① #snow      背景层里的一片雪：按面积自适应数量，有景深、有风、靠近指针会被吹开
 *   ② #cursorFx  最上层的一层柔光：跟着指针走的径向光 + 速度拉伸 + 拖尾光点
 *
 * 性能上的两点取舍：
 *   - 光斑和雪花都用**离屏预渲染的小图** drawImage 缩放，不用 createRadialGradient
 *     （后者每帧每点新建对象，几十个拖尾点就会明显掉帧）。
 *   - 两个画布在同一个 requestAnimationFrame 里画，避免两个循环抢帧。
 *   - prefers-reduced-motion 时直接不启动。
 */
(function () {
  'use strict';

  var TAU = Math.PI * 2;
  var snowCv = document.getElementById('snow');
  var fxcv = document.getElementById('cursorFx');
  if (!snowCv && !fxcv) return;

  var reduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);

  function fit(cv) {
    if (!cv) return null;
    var w = cv.clientWidth || window.innerWidth;
    var h = cv.clientHeight || window.innerHeight;
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.max(1, Math.round(h * dpr));
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  /* ---------- 预渲染光斑 ---------- */
  function sprite(rgb) {
    var S = 64, c = document.createElement('canvas');
    c.width = c.height = S;
    var x = c.getContext('2d');
    var g = x.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
    g.addColorStop(0, 'rgba(' + rgb + ',.88)');
    g.addColorStop(0.35, 'rgba(' + rgb + ',.42)');
    g.addColorStop(0.7, 'rgba(' + rgb + ',.14)');
    g.addColorStop(1, 'rgba(' + rgb + ',0)');
    x.fillStyle = g;
    x.fillRect(0, 0, S, S);
    return c;
  }
  var GLOW = sprite('168,210,255');   // 冷白偏蓝
  var FLAKE_D = sprite('255,255,255');
  var FLAKE_L = sprite('116,158,206');

  function theme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }
  var curTheme = theme();
  if (window.MutationObserver) {
    new MutationObserver(function () { curTheme = theme(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  /* ---------- 雪 ---------- */
  var S = { ctx: null, w: 0, h: 0, flakes: [] };
  var F = { ctx: null, w: 0, h: 0 };   // 光标层

  function flake(w, h, fresh) {
    var z = Math.random();                       // 0 远 → 1 近
    return {
      x: Math.random() * w,
      y: fresh ? Math.random() * h : -10 - Math.random() * 40,
      z: z,
      r: 0.9 + z * 2.2,
      vy: 0.18 + z * 0.72,
      vx: (Math.random() - 0.5) * 0.30,
      phase: Math.random() * TAU,
      spin: 0.004 + Math.random() * 0.012,
      a: 0.26 + z * 0.60
    };
  }

  function buildSnow(w, h) {
    var n = Math.max(50, Math.min(190, Math.round((w * h) / 11000)));
    S.flakes = [];
    for (var i = 0; i < n; i++) S.flakes.push(flake(w, h, true));
  }

  /* ---------- 指针 ---------- */
  var M = {
    x: 0, y: 0, tx: 0, ty: 0,
    speed: 0, seen: false, inside: false,
    trail: []
  };
  var wind = 0, windTarget = 0;

  function onMove(e) {
    var x = e.clientX, y = e.clientY;
    if (!M.seen) { M.x = M.tx = x; M.y = M.ty = y; M.seen = true; }
    M.tx = x; M.ty = y;
    M.speed = Math.min(72, M.speed + Math.hypot(x - M.x, y - M.y) * 0.55);
    M.inside = true;
    windTarget = ((x / (window.innerWidth || 1)) - 0.5) * 0.9;
    M.trail.push({ x: x, y: y, t: performance.now() });
    if (M.trail.length > 30) M.trail.shift();
  }

  window.addEventListener('pointermove', onMove, { passive: true });
  window.addEventListener('mousemove', onMove, { passive: true });
  document.addEventListener('pointerleave', function () { M.inside = false; });
  document.addEventListener('mouseleave', function () { M.inside = false; });

  /* ---------- 画雪 ---------- */
  function drawSnow(dt) {
    var ctx = S.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, S.w, S.h);

    wind += (windTarget - wind) * 0.02;
    var img = curTheme === 'light' ? FLAKE_L : FLAKE_D;
    var mx = M.x, my = M.y;

    for (var i = 0; i < S.flakes.length; i++) {
      var f = S.flakes[i];
      f.phase += f.spin;
      f.x += (f.vx + wind * (0.5 + f.z) + Math.sin(f.phase) * 0.42) * (dt / 16.7);
      f.y += f.vy * (dt / 16.7);

      // 指针掠过时把附近的雪轻轻吹开：像有人从画面里走过
      if (M.inside && M.seen) {
        var dx = f.x - mx, dy = f.y - my;
        var d2 = dx * dx + dy * dy;
        if (d2 < 15000 && d2 > 1) {
          var d = Math.sqrt(d2);
          var push = (1 - d / 122) * 1.5;
          f.x += (dx / d) * push;
          f.y += (dy / d) * push * 0.4;
        }
      }

      if (f.y > S.h + 8) { S.flakes[i] = flake(S.w, S.h, false); continue; }
      if (f.x < -12) f.x = S.w + 10;
      else if (f.x > S.w + 12) f.x = -10;

      var s = f.r * 4.2;
      ctx.globalAlpha = f.a;
      ctx.drawImage(img, f.x - s / 2, f.y - s / 2, s, s);
      // 近景的雪多叠一层柔光，像镜头前虚化的雪片（有景深才不假）
      if (f.z > 0.78) {
        var g2 = s * 3.4;
        ctx.globalAlpha = f.a * 0.20;
        ctx.drawImage(img, f.x - g2 / 2, f.y - g2 / 2, g2, g2);
      }
    }
    ctx.globalAlpha = 1;
  }

  /* ---------- 画光标光幕 ---------- */
  function drawCursor() {
    var ctx = F.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, F.w, F.h);
    if (!M.seen) return;

    var sp = Math.min(1, M.speed / 26);

    ctx.globalCompositeOperation = 'lighter';

    // ① 主光：一小团跟着指针的柔光（半径 26~42px，别再糊成一片大白）
    var R = 23 + sp * 19;
    var a = (M.inside ? 1 : 0) * (0.52 + sp * 0.26);
    ctx.globalAlpha = a;
    ctx.drawImage(GLOW, M.x - R, M.y - R, R * 2, R * 2);

    // ② 移动时沿方向轻微拉长一点点，保持"动"的感觉，但不做光幕
    var vx = M.tx - M.x, vy = M.ty - M.y;
    var sp2 = Math.hypot(vx, vy);
    if (sp2 > 0.8) {
      var ang = Math.atan2(vy, vx);
      var lenX = R * (1 + Math.min(sp2, 30) * 0.045);
      var lenY = R * 0.72;
      ctx.save();
      ctx.translate(M.x, M.y);
      ctx.rotate(ang);
      ctx.globalAlpha = 0.16 + Math.min(0.18, sp2 * 0.012);
      ctx.drawImage(GLOW, -lenX, -lenY, lenX * 2, lenY * 2);
      ctx.restore();
    }

    // ③ 拖尾：几个很小的渐隐光点，短而细
    var now = performance.now();
    var keep = [], i, t;
    for (i = 0; i < M.trail.length; i++) {
      if (now - M.trail[i].t < 320) keep.push(M.trail[i]);
    }
    M.trail = keep;
    for (i = 0; i < M.trail.length; i++) {
      t = M.trail[i];
      var age = (now - t.t) / 320;            // 0 新 → 1 老
      var rr = (1 - age) * (3.4 + sp * 3.4);
      if (rr < 0.5) continue;
      ctx.globalAlpha = (1 - age) * 0.28;
      ctx.drawImage(GLOW, t.x - rr, t.y - rr, rr * 2, rr * 2);
    }

    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
  }

  /* ---------- 主循环 ---------- */
  var last = 0, raf = 0;

  function loop(now) {
    var dt = last ? Math.min(48, now - last) : 16.7;
    last = now;

    // 指针平滑跟随（直接跟会抖，尾随一点更"糯"）；速度逐帧衰减
    if (M.seen) {
      M.x += (M.tx - M.x) * 0.42;
      M.y += (M.ty - M.y) * 0.42;
    }
    M.speed *= 0.9;

    drawSnow(dt);
    drawCursor();
    raf = requestAnimationFrame(loop);
  }

  function resize() {
    var a = fit(snowCv), b = fit(fxcv);
    if (a) { S.ctx = a.ctx; S.w = a.w; S.h = a.h; buildSnow(a.w, a.h); }
    if (b) { F.ctx = b.ctx; F.w = b.w; F.h = b.h; }
  }

  resize();
  window.addEventListener('resize', function () {
    clearTimeout(resize._t);
    resize._t = setTimeout(resize, 160);
  });

  if (reduce) {
    // 不想要动画：只画一帧静态雪，光标光幕关掉
    drawSnow(16.7);
    if (fxcv) fxcv.style.display = 'none';
  } else {
    raf = requestAnimationFrame(loop);
  }

  // 调试入口：地址后加 ?fx=demo 会自动模拟指针来回移动，
  // 方便截图核对光幕/拖尾（不影响正常使用，不加这个参数就完全不会跑）。
  if (/\bfx=demo\b/.test(location.search)) {
    var _t0 = performance.now();
    (function demo() {
      var t = performance.now() - _t0;
      var x = 430 + Math.cos(t / 300) * 90;
      var y = 690 + Math.sin(t / 300) * 46;
      onMove({ clientX: x, clientY: y });
      if (t < 20000) requestAnimationFrame(demo);
    })();
  }

  // 页面切到后台时停掉，回来再开（省电，也避免长时间累积 delta）
  document.addEventListener('visibilitychange', function () {
    if (reduce) return;
    if (document.hidden) {
      cancelAnimationFrame(raf);
      raf = 0;
    } else if (!raf) {
      last = 0;
      raf = requestAnimationFrame(loop);
    }
  });
})();
