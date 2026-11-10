/* ============================================================
   取证智能体 · 前端
   纯原生 JS，零依赖、零 CDN（断网可用）
   ============================================================ */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  tools: [],
  connected: false,     // 有没有一台 MCP 取证服务器连上（只影响「能查什么」，不影响「能不能说话」）
  agentReady: false,    // 后端 agent 起来了 → 就能对话（技能、问答都走这条路）
  streaming: false,
  selTool: null,
  cases: [],
  modelLabel: '',
  mcpHint: '',          // 「取证服务未连接」时拼在底部提示前面
  localRun: false,      // 本页面自己发起的推理（区别于「后端在跑，但不是我点的」）
};

/* ============================================================
   Markdown 渲染（够用就行：标题/表格/列表/代码/强调）
   ============================================================ */
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = (s) => esc(s).replace(/"/g, '&quot;');

function inline(s) {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, (_, c) => '<code>' + c + '</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,，。]|$)/g, '$1<em>$2</em>');
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, txt, url) => '<a href="' + escAttr(url) + '" target="_blank" rel="noopener">' + txt + '</a>');
  return t;
}

function md(src) {
  const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; }
  };

  while (i < lines.length) {
    const ln = lines[i];

    // 代码块
    const fence = ln.match(/^\s*```([\w+-]*)\s*$/);
    if (fence) {
      flush();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
      continue;
    }

    if (!ln.trim()) { flush(); i++; continue; }

    // 分隔线
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)) { flush(); out.push('<hr>'); i++; continue; }

    // 标题
    const h = ln.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flush();
      const lv = Math.min(h[1].length, 6);
      out.push('<h' + lv + '>' + inline(h[2].replace(/\s*#+\s*$/, '')) + '</h' + lv + '>');
      i++;
      continue;
    }

    // 表格
    if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?[\s:|-]{3,}\|?\s*$/.test(lines[i + 1])) {
      flush();
      const splitRow = (row) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
      const head = splitRow(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(splitRow(lines[i])); i++; }
      let t = '<table><thead><tr>' + head.map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';
      for (const r of rows) {
        t += '<tr>' + head.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>';
      }
      out.push(t + '</tbody></table>');
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(ln)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push('<blockquote>' + inline(buf.join(' ')) + '</blockquote>');
      continue;
    }

    // 列表
    const li = ln.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        if (m) { items.push(m[3]); i++; continue; }
        if (/^\s{2,}\S/.test(lines[i]) && items.length) { items[items.length - 1] += ' ' + lines[i].trim(); i++; continue; }
        break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.map(x => '<li>' + inline(x) + '</li>').join('') + '</' + tag + '>');
      continue;
    }

    para.push(ln.trim());
    i++;
  }
  flush();
  return out.join('\n');
}

/* ============================================================
   小工具
   ============================================================ */
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}
function scrollDown(force) {
  const st = $('#stream');
  const near = st.scrollHeight - st.scrollTop - st.clientHeight < 220;
  if (force || near) st.scrollTop = st.scrollHeight;
}
function shortName(full) {
  const k = full.lastIndexOf('.');
  if (k < 0) return { ns: '', name: full };
  return { ns: full.slice(0, k), name: full.slice(k + 1) };
}
/* 工具名渲染：namespace 灰、方法名亮，中间加分隔点 */
function nameHtml(full) {
  const { ns, name } = shortName(full);
  return (ns ? '<span class="ns">' + esc(ns) + '</span>' : '') + esc(name);
}
function hostOf(url) {
  if (!url) return '';
  try { return new URL(url).host; } catch (e) { return String(url).replace(/^https?:\/\//, '').split('/')[0]; }
}

function pretty(text) {
  try {
    const j = JSON.parse(text);
    return JSON.stringify(j, null, 2);
  } catch (e) { return text; }
}

/* ============================================================
   状态栏
   ============================================================ */
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    S.connected = !!j.connected;
    S.agentReady = !!j.agentReady;
    S.status = j;
    const dot = $('#dot'), ct = $('#connText'), pill = $('#pillServer');
    const srv = j.servers || [];
    const ready = srv.filter(x => x.ready);
    const broken = srv.filter(x => x.state === 'error');
    ct.onclick = null;
    ct.style.cursor = 'default';

    if (j.connected && j.myosReady) {
      dot.className = 'dot ok';
      ct.textContent = '已连接';
      pill.textContent = (j.server || 'mcp') + ' v' + (j.version || '?');
      pill.classList.add('live');
      $('#meta').textContent = ready.length + '/' + srv.length + ' 服务器 · protocol ' + (j.protocol || '?');
    } else if (j.connected) {
      dot.className = 'dot busy';
      ct.textContent = '取证运行时未就绪 · 点此重连';
      ct.style.cursor = 'pointer';
      ct.onclick = reconnect;
      pill.textContent = (j.server || 'mcp') + ' v' + (j.version || '?');
      pill.classList.remove('live');
      $('#meta').textContent = ready.length + '/' + srv.length + ' 服务器（取证服务未响应）';
    } else {
      // 一台取证服务器都没连上。这不等于「不能用」——技能（sys_skill_*）和纯问答
      // 走的都是 agent 自己，不依赖 MCP，所以这里只标「取证服务未连接」，不封输入框。
      const allOff = srv.length > 0 && srv.every(x => x.enabled === false);
      dot.className = 'dot busy';
      ct.textContent = allOff ? 'MCP 已全部停用 · 点此启用' : '取证服务未连接 · 仍可对话';
      ct.title = allOff
        ? '两台服务器都在「设置 → MCP」里关掉了。点这里一键全部启用；技能和问答不受影响。'
        : '技能（厂商 CLI）和纯问答不受影响；要查案件/检材请先启动取证软件，再点这里重连';
      ct.style.cursor = 'pointer';
      ct.onclick = allOff ? enableAllServers : reconnect;
      pill.textContent = '未连接';
      pill.classList.remove('live');
      $('#meta').textContent = broken.length
        ? broken[0].name + '：' + String(broken[0].error || '').slice(0, 34)
        : (j.error ? String(j.error).slice(0, 46) : '');
    }

    $('#toolCount').textContent = j.toolCount || 0;
    $('#srvCount').textContent = ready.length + '/' + srv.length;
    $('#skillCount').textContent = j.skillCount || 0;

    const model = j.model || '未配置';
    const host = hostOf(j.baseUrl);
    $('#pillModel').textContent = host ? model + ' @ ' + host : model;
    $('#pillModel').classList.toggle('live', !!j.hasKey);
    $('#pillModel').title = j.hasKey
      ? 'Base URL：' + (j.baseUrl || '—') + '\nKey：' + (j.keyMask || '—') + '\n点击修改'
      : '还没配模型 Key，点击去设置';
    $('#pillModel').style.cursor = 'pointer';
    $('#pillModel').onclick = () => openSettings('model');
    S.mcpHint = j.connected ? '' : '取证服务未连接（技能 / 问答可用） · ';
    S.modelLabel = j.hasKey ? ('模型 ' + model + (host ? ' · ' + host : '')) : '未配置模型';
    $('#hintModel').textContent = S.mcpHint + (j.hasKey
      ? S.modelLabel
      : '未配置 API Key → 点右上「设置」填上');
    // 后端还在跑（刷新页面 / 换标签页回来）：把中断按钮摆出来，
    // 不然用户看不到任何「它还在干活」的信号，只以为卡住了。
    // 注意别踩本地正在进行的对话——那个由 send() 自己管。
    if (!S.localRun) {
      if (j.running && !S.streaming) setComposerMode('running');
      else if (!j.running && S.streaming) setComposerMode('idle');
    }
    $('#chk-write').checked = !!j.autoYes;

    // 后端代码改过但没重启：浏览器刷新不会重新加载 Python，这个提示能省掉
    // 大量「明明改了怎么还是老样子」的困惑
    const meta = $('#meta');
    const old = meta.querySelector('.restart-flag');
    if (old) old.remove();
    if (j.needRestart) {
      const flag = document.createElement('b');
      flag.className = 'restart-flag';
      flag.textContent = ' · 后端代码已更新，重启才生效';
      flag.title = '改的是 Python 后端文件，刷新浏览器没用。\n关掉控制台进程，重新 python webui.py。';
      meta.appendChild(flag);
    }
    return j;
  } catch (e) {
    $('#dot').className = 'dot bad';
    $('#connText').textContent = '服务未响应';
    return null;
  }
}

async function reconnect() {
  $('#dot').className = 'dot busy';
  $('#connText').textContent = '重连中…';
  await fetch('/api/reconnect', { method: 'POST' });
  await refreshStatus();
  loadCases();
}

/* 一键把所有「未启用」的服务器打开。误关开关是常见操作（就在面板第一行），
   以前只能自己去设置里一台台拨，现在点状态栏就行。 */
async function enableAllServers() {
  const srv = (S.status && S.status.servers) || [];
  const off = srv.filter(x => x.enabled === false).map(x => x.name);
  if (!off.length) return reconnect();
  $('#dot').className = 'dot busy';
  $('#connText').textContent = '正在启用…';
  for (const name of off) {
    try {
      await fetch('/api/server/toggle', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, on: true }),
      });
    } catch (e) { /* 逐台失败不影响其它 */ }
  }
  await refreshStatus();
  await loadServers();
  loadCases();
  const left = ((S.status && S.status.servers) || []).filter(x => x.ready).length;
  toast(left ? `已启用 · ${left} 台服务器在线` : '已启用，但软件那边还没连上');
}

/* ============================================================
   案件
   ============================================================ */
async function loadCases() {
  const box = $('#cases');
  if (!S.connected) { box.innerHTML = '<div class="muted small">取证服务未连接</div>'; return; }
  box.innerHTML = '<div class="muted small">读取中…</div>';
  try {
    // 走专用接口：内部拉数据，不写会话日志
    const r = await fetch('/api/cases');
    const j = await r.json();
    if (!j.ok || /【工具异常】/.test(j.result || '')) {
      box.innerHTML = '<div class="muted small">读取失败 · 点侧栏 ↻ 重试</div>';
      refreshStatus();
      return;
    }
    let data = null;
    try { data = JSON.parse(j.result); } catch (e) {
      const m = j.result.match(/\{[\s\S]*\}/);
      if (m) { try { data = JSON.parse(m[0]); } catch (_) { } }
    }
    const cases = (data && data.cases) || [];
    S.cases = cases;
    if (!cases.length) { box.innerHTML = '<div class="muted small">暂无案件</div>'; return; }
    box.innerHTML = '';
    cases.forEach(c => {
      const b = el('button', 'case-item');
      b.innerHTML = '<div class="ci-name">' + esc(c.displayName || c.caseName || '未命名') + '</div>' +
        '<div class="ci-meta">' + esc(String(c.caseId).slice(0, 8)) + ' · ' + esc((c.createOn || '').slice(0, 10)) + '</div>';
      b.title = c.caseId;
      b.onclick = () => {
        $('#input').value = '案件「' + (c.displayName || c.caseName) + '」(' + c.caseId + ') 里有哪些检材？';
        autoGrow(); $('#input').focus();
      };
      box.appendChild(b);
    });
  } catch (e) {
    box.innerHTML = '<div class="muted small">' + esc(String(e)) + '</div>';
  }
}

/* ============================================================
   体检
   ============================================================ */
const HEALTH_LABELS = [
  ['ready', '整体就绪'],
  ['forensicDesktopRunning', '超级取证大师'],
  ['dbusDaemonRunning', 'D-Bus 守护'],
  ['dbusInterfaceInitialized', 'D-Bus 接口'],
  ['sessionBusConnected', '会话总线'],
  ['casemanagerRegistered', 'CaseManager'],
  ['taskmanagerRegistered', 'TaskManager'],
  ['phoneforensicsRegistered', 'PhoneForensics'],
  ['probeCallOk', '探针调用'],
];

async function doHealth() {
  const box = el('div', 'sysbox');
  box.innerHTML = '<div class="sg-title">🩺 体检中…</div>';
  $('#stream').appendChild(box);
  scrollDown(true);
  try {
    const r = await fetch('/api/health', { method: 'POST' });
    const j = await r.json();
    if (!r.ok) {
      box.innerHTML = '<div class="sg-title">🩺 体检失败</div><div class="muted">' + esc(j.error || '未连接') + '</div>';
      return;
    }
    const d = j.detail || {};
    let g = '<div class="health-grid">';
    for (const [k, label] of HEALTH_LABELS) {
      if (!(k in d)) continue;
      const ok = !!d[k];
      g += '<div class="hitem ' + (ok ? 'ok' : 'bad') + '"><span class="hm">' + (ok ? '✓' : '✕') + '</span>' +
        esc(label) + '<span class="hk">' + k + '</span></div>';
    }
    g += '</div>';
    box.innerHTML = '<div class="sg-title">🩺 ' + esc(j.text || 'D-Bus 探针') + '</div>' + g;
    scrollDown(true);
  } catch (e) {
    box.innerHTML = '<div class="sg-title">🩺 体检异常</div><div class="muted">' + esc(String(e)) + '</div>';
  }
}

/* ============================================================
   工具抽屉
   ============================================================ */
async function loadTools() {
  if (S.tools.length) { renderTools(''); return; }
  const r = await fetch('/api/tools');
  if (!r.ok) { $('#tool-list').innerHTML = '<div class="muted small" style="padding:12px">未连接，先启动取证软件再点「重连」</div>'; return; }
  const j = await r.json();
  S.tools = j.tools || [];
  $('#toolCount').textContent = S.tools.length;
  renderTools('');
}

function renderTools(q) {
  const box = $('#tool-list');
  const kw = q.trim().toLowerCase();
  const list = S.tools.filter(t =>
    !kw || t.name.toLowerCase().includes(kw) || (t.description || '').toLowerCase().includes(kw));
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<div class="muted small" style="padding:12px">没匹配到工具</div>'; return; }
  list.forEach(t => {
    const b = el('button', 'titem' + (S.selTool === t.name ? ' sel' : ''));
    b.dataset.name = t.name;
    b.innerHTML = '<div class="titem-t"><span class="titem-n">' + nameHtml(t.name) + '</span>' +
      (t.dangerous ? '<span class="badge">写操作</span>' : '') + '</div>' +
      '<div class="titem-d">' + esc((t.description || '').replace(/\s+/g, ' ').slice(0, 130)) + '</div>';
    b.onclick = () => openRunner(t);
    box.appendChild(b);
  });
}

function openRunner(t) {
  S.selTool = t.name;
  $$('.titem').forEach(x => x.classList.toggle('sel', x.dataset.name === t.name));
  const { ns, name } = shortName(t.name);
  $('#runner-name').innerHTML = nameHtml(t.name) +
    (t.dangerous ? ' <span class="badge">写操作</span>' : '');
  const form = $('#runner-form');
  form.innerHTML = '';
  const props = (t.schema && t.schema.properties) || {};
  const required = t.required || [];
  const keys = Object.keys(props);
  if (!keys.length) {
    form.innerHTML = '<div class="muted small">该工具不需要参数</div>';
  }
  keys.forEach(k => {
    const p = props[k] || {};
    const isReq = required.includes(k);
    const f = el('div', 'field');
    const lab = '<label>' + esc(k) + (isReq ? ' <span class="req">*</span>' : '') +
      (p.type ? ' · ' + esc(p.type) : '') + '</label>';
    let input;
    if (Array.isArray(p.enum)) {
      input = '<select data-k="' + escAttr(k) + '"><option value="">（不传）</option>' +
        p.enum.map(v => '<option value="' + escAttr(v) + '">' + esc(v) + '</option>').join('') + '</select>';
    } else if (p.type === 'boolean') {
      input = '<select data-k="' + escAttr(k) + '"><option value="">（不传）</option><option value="true">true</option><option value="false">false</option></select>';
    } else if (p.type === 'integer' || p.type === 'number') {
      input = '<input data-k="' + escAttr(k) + '" type="number" placeholder="' + escAttr(p.description || '') + '">';
    } else {
      input = '<input data-k="' + escAttr(k) + '" type="text" placeholder="' + escAttr((p.description || '').slice(0, 90)) + '">';
    }
    f.innerHTML = lab + input;
    f.title = p.description || '';
    form.appendChild(f);
  });
  $('#runner-out').hidden = true;
  $('#runner').hidden = false;
  $('#runner-tip').textContent = required.length ? '必填：' + required.join(', ') : '无必填参数';
}

function collectArgs() {
  const args = {};
  $$('#runner-form [data-k]').forEach(inp => {
    const k = inp.dataset.k;
    const v = inp.value;
    if (v === '') return;
    const t = inp.getAttribute('type');
    if (t === 'number') args[k] = Number(v);
    else if (v === 'true' || v === 'false') args[k] = (v === 'true');
    else if (v.startsWith('{') || v.startsWith('[')) {
      try { args[k] = JSON.parse(v); } catch (e) { args[k] = v; }
    } else args[k] = v;
  });
  return args;
}

async function runTool() {
  if (!S.selTool) return;
  const out = $('#runner-out');
  out.hidden = false;
  out.textContent = '执行中…';
  const btn = $('#btn-run');
  btn.disabled = true;
  try {
    const r = await fetch('/api/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: S.selTool, args: collectArgs() }),
    });
    const j = await r.json();
    out.textContent = j.ok ? pretty(j.result) : ('执行失败：' + (j.error || ''));
    if (j.ok && j.blocked) out.textContent = j.result;
  } catch (e) {
    out.textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false;
  }
}

/* ============================================================
   对话
   ============================================================ */
function clearWelcome() {
  const w = $('#welcome');
  if (w) w.remove();
  // 进了对话就把内容列压成一块稳的玻璃，免得正文糊在背景图的人脸上
  const s = $('#stream');
  if (s) s.classList.add('chatting');
}

function addUser(text) {
  const d = el('div', 'msg user');
  const b = el('div', 'bubble');
  b.textContent = text;
  d.appendChild(b);
  $('#stream').appendChild(d);
  scrollDown(true);
}

function addBotShell() {
  const d = el('div', 'msg bot');
  d.innerHTML = '<div class="bot-head"><span class="bot-ava">AI</span><span>取证分析师</span></div><div class="content"></div>';
  $('#stream').appendChild(d);
  return d.querySelector('.content');
}

function srvTag(server) {
  if (!server) return '';
  const label = server === '__sys__' ? '内置' : server;
  return '<span class="tool-srv' + (server === '__sys__' ? ' sys' : '') + '">' +
    esc(label) + '</span>';
}

function toolCard(name, args, dangerous, server) {
  const card = el('div', 'tool' + (dangerous ? ' warn' : ''));
  card.innerHTML =
    '<div class="tool-head">' +
    '<span class="tool-ico">⚙</span>' +
    '<span class="tool-name">' + nameHtml(name) + '</span>' +
    srvTag(server) +
    '<span class="tool-prev">' + esc(JSON.stringify(args || {}).slice(0, 90)) + '</span>' +
    '<span class="tool-dur">运行中</span>' +
    '<span class="tool-chev">›</span>' +
    '</div>' +
    '<div class="tool-body">' +
    '<div class="kv"><span>参数</span><pre>' + esc(JSON.stringify(args || {}, null, 2)) + '</pre></div>' +
    '<div class="kv out"><span>返回</span><pre>等待中…</pre></div>' +
    '</div>';
  card.querySelector('.tool-head').onclick = () => card.classList.toggle('open');
  return card;
}

function fillToolCard(card, ev) {
  const dur = card.querySelector('.tool-dur');
  dur.textContent = (ev.elapsed != null ? ev.elapsed + 's' : '完成');
  card.querySelector('.tool-prev').textContent = ev.preview || '';
  const pre = card.querySelector('.kv.out pre');
  pre.textContent = pretty(ev.full || ev.preview || '');
  if (ev.error) { card.classList.add('err'); card.querySelector('.tool-ico').textContent = '✕'; }
}

function blockedCard(ev) {
  const card = el('div', 'tool blocked open');
  card.innerHTML =
    '<div class="tool-head">' +
    '<span class="tool-ico">⊘</span>' +
    '<span class="tool-name">' + nameHtml(ev.name || '') + '</span>' +
    srvTag(ev.server) +
    '<span class="tool-prev">未授权，已拦截</span>' +
    '<span class="tool-chev">›</span>' +
    '</div>' +
    '<div class="tool-body"><div class="kv"><span>原因</span><pre>' +
    esc(ev.reason || ('该工具会改动案件/检材状态。要执行请打开右上角「允许写操作」。')) +
    '</pre></div><div class="kv"><span>参数</span><pre>' + esc(JSON.stringify(ev.args || {}, null, 2)) +
    '</pre></div></div>';
  card.querySelector('.tool-head').onclick = () => card.classList.toggle('open');
  return card;
}

/* 演示用：?demo=chat 渲染一段固定对话（走的是真渲染函数，只喂假数据）
   用途：截图 / 演示 / 主题调试，不连模型也不碰 MCP */
function demoChat() {
  clearWelcome();
  addUser('看看本机有哪些案件');
  const bot = addBotShell();
  bot.appendChild(el('div', 'turn-bar',
    '<span class="tb-pill"><b>第 2/40 轮</b><i>工具 2/120</i></span>'));
  bot.appendChild(el('div', 'final', md('先把手上的案件列出来。')));

  const card = toolCard('query_cases', { args: '', caseId: '' }, false, 'myos-forensics');
  bot.appendChild(card);
  fillToolCard(card, {
    elapsed: 0.68,
    preview: '2 条记录',
    full: JSON.stringify({ code: 0, data: [
      { caseId: '00681e94', caseName: '案件20261012143420', createdAt: '2026-10-12 14:34', opened: 0 },
      { caseId: '7b3c19d2', caseName: '诈骗案·李某手机', createdAt: '2026-10-20 09:12', opened: 1 },
    ] }, null, 2),
  });

  bot.appendChild(el('div', 'final', md(
    '### 案件概览\n' +
    '本机共有 **2 个案件**，最近一次创建于 2026-10-12。\n\n' +
    '| 案件名 | caseId | 创建时间 | 状态 |\n' +
    '| --- | --- | --- | --- |\n' +
    '| 案件20261012143420 | `00681e94` | 2026-10-12 14:34 | 未打开 |\n' +
    '| 诈骗案·李某手机 | `7b3c19d2` | 2026-10-20 09:12 | 已打开 |\n\n' +
    '### 下一步建议\n' +
    '1. 先 `load_case` 打开目标案件（会改状态，需要授权）\n' +
    '2. 用 `query_materials` 看检材清单\n' +
    '3. 取证跑完**必须**再 `load_case` 一次，否则后检查节点全是空\n\n' +
    '> 本次只做了只读查询，没有改动任何案件状态。'
  )));

  bot.appendChild(blockedCard({
    name: 'load_case',
    server: 'myos-forensics',
    args: { caseId: '00681e94' },
  }));
  bot.appendChild(el('div', 'turn-bar renew',
    '<span class="tb-pill"><b>额度续期 ×1</b><i>第 40 轮</i><i>工具 40 次</i>' +
    '<i class="tb-note">仍在推进，继续…</i></span>'));
  scrollDown(true);
}

async function send(text) {
  if (S.streaming || !text.trim()) return;
  // 只有「后端 agent 都没起来」才真的没法说话；MCP 没连只是少了一类工具，
  // 技能（厂商 CLI）和纯问答照样走 agent，不该把输入框锁掉。
  if (!S.agentReady) {
    const b = addBotShell();
    b.appendChild(el('div', 'errbox',
      '<b>后端没起来。</b><br>' + (S.status && S.status.error ? esc(S.status.error) : '') +
      '<br>看一眼控制台窗口的报错；改过配置的话点右上「重连」再试。'));
    return;
  }

  S.streaming = true;
  S.localRun = true;
  setComposerMode('running');
  $('#dot').className = 'dot busy';
  $('#connText').textContent = '推理中…';
  clearWelcome();
  addUser(text);
  $('#input').value = '';
  autoGrow();

  const body = addBotShell();
  // 取证服务没连上：先摆一条黄条说明「这次只能干什么」，对话继续跑。
  // 放黄条而不是直接拦，是因为很多人就想问技能怎么用 / 让模型解释原理。
  if (!S.connected) {
    const srv = (S.status && S.status.servers) || [];
    const allOff = srv.length > 0 && srv.every(x => x.enabled === false);
    body.appendChild(el('div', 'warnbox', allOff
      ? '<b>取证服务已全部停用。</b>（设置 → MCP 里的开关是关的）' +
        '本次只能用技能（厂商 CLI）和纯问答——查案件、检材、插件结果这些 MCP 工具用不了。<br>' +
        '要用取证工具：设置 → MCP，把开关打开，再点右上角重连。'
      : '<b>取证服务未连接。</b>本次只能用技能（厂商 CLI）和纯问答——' +
        '查案件、检材、插件结果这些 MCP 工具会失败。<br>' +
        '要查数据：先启动取证软件，再点右上角重连。'));
  }
  let loading = null;
  const setLoading = (on, label) => {
    if (loading) { loading.remove(); loading = null; }
    if (on) {
      loading = el('div', 'think loading', '<span class="muted">' + (label || '思考中') + '</span><span class="dots"></span>');
      body.appendChild(loading);
    }
    scrollDown();
  };
  setLoading(true);

  const pending = [];
  let gotSomething = false;

  const handle = (ev) => {
    switch (ev.type) {
      case 'turn':
        setLoading(true, ev.n > 1 ? '继续推理' : '思考中');
        if (ev.n > 1) {
          const near = ev.max && ev.n >= ev.max - 2;
          const label = '<span class="tb-pill"><b>第 ' + ev.n +
            (ev.max ? '/' + ev.max : '') + ' 轮</b>' +
            (ev.callMax ? '<i>工具 ' + ev.calls + '/' + ev.callMax + '</i>' : '') +
            '</span>';
          body.appendChild(el('div', 'turn-bar' + (near ? ' near' : ''), label));
        }
        break;
      case 'renew':
        gotSomething = true;
        body.appendChild(el('div', 'turn-bar renew',
          '<span class="tb-pill"><b>额度续期 ×' + ev.renews + '</b>' +
          '<i>第 ' + ev.turns + ' 轮</i><i>工具 ' + ev.calls + ' 次</i>' +
          '<i class="tb-note">仍在推进，继续…</i></span>'));
        scrollDown();
        break;
      case 'think':
        setLoading(false);
        gotSomething = true;
        body.appendChild(el('div', 'think', esc(ev.text)));
        scrollDown();
        break;
      case 'selfcheck':
        // 模型空口说「没有工具」，后端发现后强制它核实——把这件事摆到台面上，
        // 用户才知道「刚才那句不可用」不是事实，是被纠正过的。
        gotSomething = true;
        body.appendChild(el('div', 'warnbox', esc(ev.text || '已要求模型重新核实工具状态')));
        scrollDown();
        break;
      case 'tool_start': {
        setLoading(false);
        gotSomething = true;
        const t = S.tools.find(x => x.name === ev.name);
        const card = toolCard(ev.name, ev.args, t && t.dangerous, ev.server);
        body.appendChild(card);
        pending.push(card);
        scrollDown();
        break;
      }
      case 'tool_end': {
        const card = pending.shift();
        if (card) fillToolCard(card, ev);
        else {
          const c = toolCard(ev.name, {}, false, ev.server);
          fillToolCard(c, ev);
          body.appendChild(c);
        }
        scrollDown();
        break;
      }
      case 'tool_blocked':
        setLoading(false);
        gotSomething = true;
        body.appendChild(blockedCard(ev));
        scrollDown();
        break;
      case 'gate':
        // 证据门禁：模型想给「密码是 xxx」这类硬结论，但本轮一条工具结果都没有，
        // 被拦回去重写。必须让用户看见——否则他只会觉得回答变慢了。
        gotSomething = true;
        body.appendChild(el('div', 'warnbox',
          '<b>已拦下一条无证据结论</b>：' + esc(ev.text || '') +
          '<br><span class="hint2" style="display:inline">没有工具返回就不允许给具体值，已要求它要么去查、要么改成「未取得」。</span>'));
        scrollDown();
        break;
      case 'tool_fused':
        // 某个工具连续失败被停用：不是整轮结束，任务还在往下走，所以只提示一句
        gotSomething = true;
        body.appendChild(el('div', 'sysbox warnbox',
          '<b>' + esc(ev.name) + ' 已停用</b>：' + esc(ev.why || '反复失败') +
          '。它会从工具清单里摘掉，改走别的路子。'));
        scrollDown();
        break;
      case 'inject':
        // 后端确认这条话已经进到模型上下文里了（不是刚排队那一刻）
        gotSomething = true;
        {
          const n = el('div', 'inject-note live');
          n.innerHTML = '<span class="inject-ico">↳</span><b>引导已生效</b>' +
            '<span class="inject-x">' + esc(ev.text) + '</span>' +
            '<span class="muted small">第 ' + (ev.turn || 1) + ' 轮起按这个方向走</span>';
          body.appendChild(n);
        }
        scrollDown();
        break;
      case 'stopped':
        setLoading(false);
        body.appendChild(el('div', 'sysbox stop-box',
          '已中断 —— 停在第 ' + (ev.turn || 0) + ' 轮、工具 ' + (ev.calls || 0) + ' 次。' +
          '上面已经跑出的结果都有效，接着问就行。'));
        scrollDown(true);
        break;
      case 'final':
        setLoading(false);
        body.appendChild(el('div', 'final', md(ev.text)));
        scrollDown();
        break;
      case 'error':
        setLoading(false);
        body.appendChild(el('div', 'errbox', '<b>' + esc(ev.message || '出错了') + '</b>' +
          (ev.detail ? '<pre style="margin:8px 0 0;white-space:pre-wrap;font-size:11px">' + esc(ev.detail) + '</pre>' : '')));
        scrollDown(true);
        break;
      case 'saved':
        body.appendChild(el('div', 'sysbox', '报告已保存：<code>' + esc(ev.path) + '</code>'));
        break;
      case 'done':
        setLoading(false);
        break;
      default: break;
    }
  };

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); msg = j.error || msg; } catch (e) { }
      setLoading(false);
      body.appendChild(el('div', 'errbox', '<b>' + esc(msg) + '</b>'));
    } else {
      const reader = res.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let k;
        while ((k = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, k);
          buf = buf.slice(k + 2);
          const line = chunk.split('\n').find(l => l.startsWith('data:'));
          if (!line) continue;
          try { handle(JSON.parse(line.slice(5).trim())); } catch (e) { }
        }
      }
    }
  } catch (e) {
    setLoading(false);
    body.appendChild(el('div', 'errbox', '<b>请求失败：</b>' + esc(String(e))));
  } finally {
    S.streaming = false;
    S.localRun = false;
    setComposerMode('idle');
    setLoading(false);
    refreshStatus();
    scrollDown();
  }
}

/* ------------------------------------------------------------
   运行中干预：中断 / 插话
   ------------------------------------------------------------ */
function setComposerMode(mode) {
  const running = mode === 'running';
  S.streaming = running;
  const stop = $('#btn-stop');
  const sendB = $('#btn-send');
  const input = $('#input');
  if (stop) stop.hidden = !running;
  if (sendB) {
    sendB.classList.toggle('steer', running);
    sendB.title = running ? '插入引导（Enter）' : '发送（Enter）';
  }
  if (input) {
    input.placeholder = running
      ? '它正在跑 —— 这里输入的话会作为「引导」插进去，下一轮生效…'
      : '例如：看看本机有哪些案件，第二个案子有哪些检材？';
  }
  const hint = $('#hint-bar');
  if (hint) {
    hint.innerHTML = running
      ? '<b style="color:var(--warm-2)">引导模式</b> · Enter 插入 · 点 ■ 中断 · <span id="hintModel">' +
        esc(S.mcpHint + (S.modelLabel || '')) + '</span>'
      : 'Enter 发送 · Shift+Enter 换行 · <span id="hintModel">' +
        esc(S.mcpHint + (S.modelLabel || '未配置模型')) + '</span>';
  }
}

async function stopRun() {
  const btn = $('#btn-stop');
  if (btn) { btn.disabled = true; btn.classList.add('stopping'); }
  try {
    const r = await postJson('/api/chat/stop', {});
    if (r && r.error) toast(r.error);
    else toast('已发出中断，正在收手…');
  } catch (e) {
    toast('中断失败：' + e.message);
  } finally {
    setTimeout(() => { if (btn) { btn.disabled = false; btn.classList.remove('stopping'); } }, 1200);
  }
}

/* 插话：不新开一轮，只把话塞进正在跑的那一轮 */
async function inject(text) {
  const t = (text || '').trim();
  if (!t) return;
  const input = $('#input');
  try {
    const r = await postJson('/api/chat/inject', { text: t });
    if (r.error) { toast(r.error); return; }
    input.value = '';
    autoGrow();
    // 界面上留个痕：这条话已经进队了，但要等它这一轮做完才被看到
    const st = $('#stream');
    const box = el('div', 'inject-note');
    box.innerHTML = '<span class="inject-ico">↳</span><b>已插入引导</b>' +
      '<span class="inject-x">' + esc(t) + '</span>' +
      '<span class="muted small">下一轮生效' + (r.queued > 1 ? '（队列 ' + r.queued + ' 条）' : '') + '</span>';
    st.appendChild(box);
    scrollDown();
  } catch (e) {
    toast('插入失败：' + e.message);
  }
}

/* ============================================================
   输入框
   ============================================================ */
function autoGrow() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 180) + 'px';
}

/* ============================================================
   初始化
   ============================================================ */
const QUICK = [
  '看看本机有哪些案件',
  '列出已打开案件的检材和取证状态',
  '这个案子的微信记录里有哪些可疑内容',
  '手机取证结果里有哪些社交应用',
];

function initQuick() {
  const box = $('#quick');
  QUICK.forEach(q => {
    const b = el('button', null, esc(q));
    b.onclick = () => { $('#input').value = q; autoGrow(); $('#input').focus(); };
    box.appendChild(b);
  });

  const cards = $('#welcomeCards');
  [
    ['查案件', '看看本机有哪些案件'],
    ['看检材', '列出已打开案件的检材和取证状态'],
    ['搜内容', '在已加载的检材里搜索「转账」'],
    ['出结论', '把这个案子的关键时间线整理成表格'],
  ].forEach(([t, q]) => {
    const b = el('button', 'card', '<b>' + esc(t) + '</b>' + esc(q));
    b.onclick = () => { $('#input').value = q; autoGrow(); $('#input').focus(); };
    cards.appendChild(b);
  });
}

function initTheme() {
  const urlTheme = new URLSearchParams(location.search).get('theme');
  const saved = urlTheme || localStorage.getItem('myos-theme');
  if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  $('#btn-theme').onclick = () => {
    const cur = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = cur;
    localStorage.setItem('myos-theme', cur);
  };
}

/* ============================================================
   连接器（多 MCP）
   ============================================================ */
const SRV_STATE = {
  ready:      { txt: '已连接',   cls: 'ok'   },
  error:      { txt: '连接失败', cls: 'bad'  },
  disabled:   { txt: '未启用',   cls: 'off'  },
  connecting: { txt: '连接中',   cls: 'busy' },
  idle:       { txt: '未连接',   cls: 'off'  },
};

function closeAll() {
  ['#drawer', '#drawer-servers', '#drawer-skills', '#drawer-settings', '#drawer-history'].forEach(sel => {
    const n = $(sel); if (n) n.classList.remove('open');
  });
  $('#scrim').classList.remove('on');
  const rn = $('#runner'); if (rn) rn.hidden = true;
}

async function openServers() {
  closeAll();
  $('#drawer-servers').classList.add('open');
  $('#scrim').classList.add('on');
  await loadServers();
}

async function loadServers() {
  const box = $('#server-list');
  box.innerHTML = '<div class="muted small">读取中…</div>';
  try {
    const j = await (await fetch('/api/servers')).json();
    S.servers = j.servers || [];
    renderServers();
  } catch (e) {
    box.innerHTML = '<div class="muted small">读取失败：' + esc(e.message) + '</div>';
  }
}

function renderServers() {
  const box = $('#server-list');
  const list = S.servers || [];
  if (!list.length) {
    box.innerHTML = '<div class="empty">还没配置 MCP 服务器。<br>在 <code>config.json</code> → <code>mcpServers</code> 里加一条即可。</div>';
    return;
  }
  box.innerHTML = '';
  list.forEach(s => {
    const st = SRV_STATE[s.state] || { txt: s.state, cls: 'off' };
    const card = el('div', 'srv' + (s.ready ? ' live' : ''));
    card.innerHTML =
      '<div class="srv-top">' +
        '<span class="srv-dot ' + st.cls + '"></span>' +
        '<span class="srv-name">' + esc(s.label || s.name) + '</span>' +
        '<span class="srv-badge ' + st.cls + '">' + esc(st.txt) + '</span>' +
        '<span class="grow"></span>' +
        '<span class="srv-n">' + (s.toolCount || 0) + ' 工具</span>' +
      '</div>' +
      '<div class="srv-id">' + esc(s.name) +
        (s.server ? ' · ' + esc(s.server) + ' v' + esc(s.version || '?') : '') + '</div>' +
      (s.note ? '<div class="srv-note">' + esc(s.note) + '</div>' : '') +
      (s.error ? '<div class="srv-err">' + esc(String(s.error).slice(0, 260)) + '</div>' : '') +
      '<div class="srv-cmd">' + esc(s.command || '—') + '</div>';

    const bar = el('div', 'srv-bar');
    const sw = el('label', 'switch small');
    sw.innerHTML = '<input type="checkbox"' + (s.enabled ? ' checked' : '') +
      '><span class="sw"></span><span class="sw-t">启用</span>';
    sw.querySelector('input').onchange = (e) => {
      if (!e.target.checked && !allowDisable(s.name)) { e.target.checked = true; return; }
      toggleServer(s.name, e.target.checked);
    };
    bar.appendChild(sw);
    const rc = el('button', 'btn small', '重连');
    rc.onclick = () => reconnectServer(s.name, rc);
    bar.appendChild(rc);
    card.appendChild(bar);
    box.appendChild(card);
  });
}

async function toggleServer(name, on) {
  await fetch('/api/server/toggle', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, on }),
  });
  await loadServers();
  await refreshStatus();
}

// 关掉最后一台在用的服务器 = 全站再也没有取证工具。
// 这个开关离「重连」按钮很近，手滑点掉的代价是整轮对话直接死（模型只能回
// 「服务器处于禁用状态」）——所以关最后一台必须先确认一次。
function allowDisable(name) {
  let list = S.servers || [];
  if (!list.length) {
    list = Object.entries((SET.data && SET.data.servers) || {})
      .map(([k, v]) => ({ name: k, enabled: v.enabled !== false }));
  }
  const left = list.filter(x => x.enabled && x.name !== name).length;
  if (left > 0) return true;
  return confirm('关掉「' + name + '」之后就没有任何取证工具可用了\n' +
    '（技能检索和纯问答仍然能用，但读镜像/查案件全部做不了）。\n\n' +
    '如果你只是想修连接，请点旁边的「重连」。确认关闭？');
}

async function reconnectServer(name, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '重连中…'; }
  try {
    await fetch('/api/server/reconnect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
  } catch (e) { /* 忽略 */ }
  await loadServers();
  await refreshStatus();
  if (btn) { btn.disabled = false; btn.textContent = '重连'; }
}

/* ============================================================
   技能
   ============================================================ */
async function openSkills() {
  closeAll();
  $('#drawer-skills').classList.add('open');
  $('#scrim').classList.add('on');
  await loadSkills();
}

async function loadSkills() {
  const box = $('#skill-groups');
  box.innerHTML = '<div class="muted small">读取中…</div>';
  try {
    const j = await (await fetch('/api/skills')).json();
    S.skillGroups = j.groups || [];
    renderSkills('');
  } catch (e) {
    box.innerHTML = '<div class="muted small">读取失败</div>';
  }
}

function renderSkills(q) {
  const box = $('#skill-groups');
  const kw = (q || '').trim().toLowerCase();
  const groups = (S.skillGroups || []).map(g => ({
    ...g,
    skills: (g.skills || []).filter(s => !kw ||
      (s.name + ' ' + s.brief + ' ' + (s.tags || []).join(' ')).toLowerCase().includes(kw)),
  })).filter(g => g.skills.length);

  if (!groups.length) {
    box.innerHTML = '<div class="empty">没有匹配的技能。<br>把技能包（含 <code>SKILL.md</code>）放进 <code>skills/</code> 或 <code>~/.workbuddy/skills/</code> 即可。</div>';
    return;
  }
  box.innerHTML = '';
  const jump = $('#skill-jump');
  if (jump) jump.innerHTML = '';
  groups.forEach((g, i) => {
    if (jump) {
      const b = el('button', null, esc(g.title) + '<b>' + g.skills.length + '</b>');
      b.onclick = () => {
        const t = $('#grp-' + i);
        if (t) t.scrollIntoView({ block: 'start', behavior: 'smooth' });
      };
      jump.appendChild(b);
    }
    const h = el('div', 'grp-h');
    h.id = 'grp-' + i;
    h.innerHTML = '<span>' + esc(g.title) + '</span><span class="grp-n">' + g.skills.length + '</span>' +
      (g.subtitle ? '<span class="grp-sub">' + esc(g.subtitle) + '</span>' : '');
    box.appendChild(h);
    g.skills.forEach(s => {
      const b = el('button', 'sk');
      b.dataset.name = s.name;
      b.innerHTML =
        '<div class="sk-t"><span class="sk-n">' + esc(s.name) + '</span>' +
        (s.hasCli ? '<span class="badge cli">CLI</span>' : '') +
        '</div>' +
        '<div class="sk-d">' + esc(s.brief) + '</div>';
      b.onclick = () => openSkill(s.name);
      box.appendChild(b);
    });
  });
}

async function openSkill(name) {
  const panel = $('#skill-panel');
  panel.hidden = false;
  $('#skill-title').textContent = name;
  $('#skill-meta').innerHTML = '<span class="muted small">读取中…</span>';
  $('#skill-body').textContent = '';
  $('#skill-out').hidden = true;
  $$('.sk').forEach(x => x.classList.toggle('sel', x.dataset.name === name));
  try {
    const d = await (await fetch('/api/skill?name=' + encodeURIComponent(name))).json();
    if (d.error) { $('#skill-meta').innerHTML = '<span class="bad">' + esc(d.error) + '</span>'; return; }
    S.curSkill = d;
    const bits = [];
    if (d.title) bits.push('<span class="pill tiny">' + esc(d.title) + '</span>');
    if (d.executables && d.executables.length) {
      bits.push('<span class="sk-k">CLI</span><span class="muted small">' + esc(d.executables.join(' · ')) + '</span>');
    }
    if (d.bins && d.bins.length) {
      bits.push('<span class="sk-k">依赖</span><span class="muted small">' + esc(d.bins.join(' · ')) + '</span>');
    }
    if (d.dir) bits.push('<span class="sk-k">目录</span><span class="muted small">' + esc(d.dir) + '</span>');
    $('#skill-meta').innerHTML = bits.join(' ') || '<span class="muted small">无附加信息</span>';
    $('#skill-body').textContent = d.body || '（无正文）';
    const suggest = (d.bins && d.bins.length)
      ? d.bins[0]
      : ((d.executables || []).find(x => x.endsWith('.exe')) || (d.executables || [])[0]);
    $('#skill-cmd').placeholder = suggest
      ? '例如：' + suggest + ' --help'
      : '例如：ges status（需在白名单内）';
  } catch (e) {
    $('#skill-meta').innerHTML = '<span class="bad">读取失败</span>';
  }
}

async function runSkill() {
  const d = S.curSkill;
  if (!d) return;
  const cmd = $('#skill-cmd').value.trim();
  if (!cmd) return;
  const out = $('#skill-out');
  out.hidden = false;
  out.className = 'runner-out';
  out.textContent = '执行中…';
  const btn = $('#btn-skill-run');
  btn.disabled = true;
  try {
    const j = await (await fetch('/api/skill/exec', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: d.name, command: cmd }),
    })).json();
    out.textContent = j.blocked ? '⊘ ' + j.error : (j.result || j.error || '（无输出）');
    if (j.blocked || j.ok === false) out.classList.add('bad');
  } catch (e) {
    out.textContent = '执行失败：' + e.message;
    out.classList.add('bad');
  } finally {
    btn.disabled = false;
  }
}

/* ============================================================
   历史会话（列表 / 打开 / 重命名 / 置顶 / 归档 / 删除）
   ============================================================ */
const HIST = {
  archived: false, rows: [], q: '', currentId: '', stats: {},
  mode: false,              // 多选模式
  sel: new Set(),           // 选中的会话 id
  visible: [],              // 当前筛选出来的行（全选只作用于可见项）
};

async function postJson(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

async function openHistory() {
  closeAll();
  $('#drawer-history').classList.add('open');
  $('#scrim').classList.add('on');
  await loadHistory();
}

async function loadHistory() {
  const list = $('#hist-list');
  list.innerHTML = '<div class="hist-empty">读取中…</div>';
  try {
    const j = await (await fetch('/api/sessions?archived=' + (HIST.archived ? 1 : 0))).json();
    if (j.error) throw new Error(j.error);
    HIST.rows = j.sessions || [];
    HIST.currentId = j.currentId || '';
    HIST.stats = j.stats || {};
  } catch (e) {
    list.innerHTML = '<div class="hist-empty">读取失败：' + esc(e.message || e) + '</div>';
    return;
  }
  renderHistory();
}

function renderHistory() {
  const list = $('#hist-list');
  const q = HIST.q.trim().toLowerCase();
  // 一次都没提过问的会话（纯工具调用 / 早期空文件）不进列表，只在「清理」里统一处理
  let rows = HIST.rows.filter(r => r.turns > 0 || q);
  const idleCount = HIST.rows.filter(r => !r.turns).length;
  if (q) {
    rows = rows.filter(r => (r.title + ' ' + (r.preview || '') + ' ' + r.id).toLowerCase().includes(q));
  }
  const st = HIST.stats || {};
  let tip = HIST.archived
    ? rows.length + ' 个已归档 · 点 ↩ 恢复'
    : rows.length + ' 个会话 · 共 ' + (st.turns || 0) + ' 轮问答';
  if (idleCount) tip += ' · ' + idleCount + ' 个无对话待清理';
  $('#hist-tip').textContent = tip;

  HIST.visible = rows;
  // 筛选变化后把已经不可见的选中项剔掉，否则「删除」会删到看不见的东西
  const alive = new Set(rows.map(r => r.id));
  [...HIST.sel].forEach(id => { if (!alive.has(id)) HIST.sel.delete(id); });
  syncBulkBar();

  if (!rows.length) {
    list.innerHTML = '<div class="hist-empty">' +
      (q ? '没有匹配的会话' : (HIST.archived ? '归档夹是空的' : '还没有对话记录，问一句就有了')) + '</div>';
    return;
  }
  list.innerHTML = '';
  rows.forEach(r => list.appendChild(histCard(r)));
}

/* ---- 多选：底部操作条的计数与按钮态 ---- */
function syncBulkBar() {
  const bar = $('#hist-bulk');
  if (!bar) return;
  bar.hidden = !HIST.mode;
  $('#drawer-history').classList.toggle('picking', HIST.mode);
  $('#btn-hist-select').classList.toggle('on', HIST.mode);
  $('#btn-hist-select').title = HIST.mode ? '退出多选' : '多选';

  const n = HIST.sel.size;
  $('#hist-sel').textContent = n ? ('已选 ' + n + ' 项') : '还没选';
  const del = $('#btn-bulk-del'), arch = $('#btn-bulk-arch');
  if (del) del.disabled = !n;
  if (arch) arch.disabled = !n;

  const all = $('#hist-all');
  if (all) {
    const vis = HIST.visible || [];
    all.checked = vis.length > 0 && vis.every(r => HIST.sel.has(r.id));
    all.indeterminate = n > 0 && !all.checked;
  }
}

function togglePick(id, force) {
  const has = HIST.sel.has(id);
  const want = force === undefined ? !has : force;
  if (want) HIST.sel.add(id); else HIST.sel.delete(id);
  const card = document.querySelector('.hcard[data-id="' + CSS.escape(id) + '"]');
  if (card) {
    card.classList.toggle('picked', want);
    const cb = card.querySelector('input[type=checkbox]');
    if (cb) cb.checked = want;
  }
  syncBulkBar();
}

function setPickMode(on) {
  HIST.mode = !!on;
  if (!HIST.mode) HIST.sel.clear();
  HIST.rows && renderHistory();
}

function histCard(r) {
  const on = r.id === HIST.currentId;
  const picked = HIST.sel.has(r.id);
  const c = el('div', 'hcard' + (on ? ' on' : '') + (HIST.mode ? ' pickable' : '') +
    (picked ? ' picked' : ''));
  c.dataset.id = r.id;
  c.innerHTML =
    '<div class="hcard-h">' +
      (HIST.mode ? '<input type="checkbox" class="hcard-cb"' + (picked ? ' checked' : '') + '>' : '') +
      (r.pinned ? '<span class="hcard-pin">★</span>' : '') +
      '<span class="hcard-t" title="' + escAttr(r.title) + '">' + esc(r.title) + '</span>' +
      (HIST.mode ? '' :
      '<span class="hcard-act">' +
        '<button class="mini-btn" data-act="rename" title="重命名">✎</button>' +
        '<button class="mini-btn" data-act="pin" title="' + (r.pinned ? '取消置顶' : '置顶') + '">' +
          (r.pinned ? '★' : '☆') + '</button>' +
        '<button class="mini-btn" data-act="arch" title="' + (r.archived ? '恢复到活跃' : '归档') + '">' +
          (r.archived ? '↩' : '⇩') + '</button>' +
        '<button class="mini-btn del" data-act="del" title="删除">✕</button>' +
      '</span>') +
    '</div>' +
    '<div class="hcard-meta">' +
      '<span>' + esc(r.updatedText || r.startedText || '') + '</span>' +
      '<span>' + (r.turns || 0) + ' 轮</span>' +
      '<span>' + (r.tools || 0) + ' 工具</span>' +
      (on ? '<span style="color:var(--cool-2)">当前</span>' : '') +
    '</div>' +
    (r.preview ? '<div class="hcard-pp">' + esc(r.preview) + '</div>' : '');

  c.onclick = (ev) => {
    if (HIST.mode) {
      // 多选模式下整张卡就是复选框，点哪都算选中（点 checkbox 本身会冒泡上来）
      ev.preventDefault();
      togglePick(r.id);
      return;
    }
    const hit = ev.target.closest('[data-act]');
    if (hit) { ev.stopPropagation(); histAction(hit.dataset.act, r).catch(e => alert('操作失败：' + e.message)); return; }
    openSession(r.id).catch(e => alert('打开失败：' + e.message));
  };
  return c;
}

async function histAction(act, r) {
  if (act === 'rename') {
    const t = prompt('给这个会话起个名字（留空则按首句自动命名）', r.title);
    if (t === null) return;
    await postJson('/api/session/rename', { id: r.id, title: t });
  } else if (act === 'pin') {
    await postJson('/api/session/pin', { id: r.id, on: !r.pinned });
  } else if (act === 'arch') {
    await postJson('/api/session/archive', { id: r.id, on: !r.archived });
    if (!r.archived) toast('已归档，可在「归档」页找回来');
  } else if (act === 'del') {
    const ok = confirm('删除会话「' + r.title + '」？\n\n' +
      '会删掉 logs 里的 ' + r.id + '.jsonl（连带同名报告），删了找不回来。\n' +
      '如果只是想收拾一下，建议用「归档」。');
    if (!ok) return;
    await postJson('/api/session/delete', { id: r.id });
  }
  await loadHistory();
}

/* ---- 批量：一次处理勾选的所有会话 ---- */
async function bulkAction(kind) {
  const ids = [...HIST.sel];
  if (!ids.length) return;

  if (kind === 'delete') {
    const ok = confirm('删除选中的 ' + ids.length + ' 个会话？\n\n' +
      '会删掉 logs 里对应的 .jsonl（连带同名报告），删了找不回来。\n' +
      '如果只是想收拾一下，用「归档」更稳妥——随时能翻回来。');
    if (!ok) return;
    const r = await postJson('/api/session/delete', { ids });
    HIST.sel.clear();
    toast('已删除 ' + (r.deleted || 0) + ' 个' +
      ((r.failed && r.failed.length) ? ('，' + r.failed.length + ' 个失败') : ''));
    // 删掉的正好是当前会话 → 对话区清掉，不然还挂着已删除的内容继续聊
    if (ids.includes(HIST.currentId)) {
      $('#stream').innerHTML = '';
      await refreshStatus();
    }
  } else if (kind === 'arch') {
    const to = !HIST.archived;   // 从活跃页点归档、从归档页点恢复
    for (const id of ids) {
      try { await postJson('/api/session/archive', { id, on: to }); } catch (e) { /* 单条失败不挡后面的 */ }
    }
    HIST.sel.clear();
    toast(to ? ('已归档 ' + ids.length + ' 个，可在「归档」页找回') : ('已恢复 ' + ids.length + ' 个'));
  }
  await loadHistory();
}

/* 把某个历史会话渲染到对话区，并设成当前会话（接着聊就续写这个文件） */
async function openSession(id) {
  const d = await (await fetch('/api/session?id=' + encodeURIComponent(id))).json();
  if (d.error) throw new Error(d.error);
  await postJson('/api/session/open', { id });
  renderHistoryStream(d);
  closeAll();
  await refreshStatus();
  $('#input').focus();
}

function safeArgs(raw) {
  if (raw && typeof raw === 'object') return raw;
  const s = String(raw || '').trim();
  if (!s) return {};
  try { return JSON.parse(s); } catch (e) { /* 日志里可能是 python repr */ }
  try { return JSON.parse(s.replace(/'/g, '"')); } catch (e) { /* 放弃 */ }
  return { args: s };
}

function renderHistoryStream(d) {
  const st = $('#stream');
  st.innerHTML = '';
  st.classList.add('chatting');

  const banner = el('div', 'hist-banner');
  banner.innerHTML =
    '<span>历史会话 · <b>' + esc(d.title) + '</b></span>' +
    '<span class="muted small">' + (d.turns || []).length + ' 轮 · ' +
      esc(String(d.started || '').replace('T', ' ')) + '</span>' +
    (d.archived ? '<span class="pill tiny">已归档</span>' : '') +
    '<span class="sp"></span>' +
    '<span class="muted small">接着输入就续在这个会话里</span>';
  st.appendChild(banner);

  (d.turns || []).forEach(t => {
    if (t.role === 'user') {
      const wrap = el('div', 'msg user');
      wrap.appendChild(el('div', 'bubble', esc(t.text || '')));
      st.appendChild(wrap);
      if (t.missingAnswer) {
        st.appendChild(el('div', 'sysbox warnbox',
          '这次提问没有留下回答 —— 是旧版本只写提问的缺陷（当时的答案压根没写进文件，' +
          '找不回来了）。现在已经修好，重新问一次就会有完整记录。'));
      }
      return;
    }
    const box = el('div', 'msg bot');
    box.innerHTML = '<div class="bot-head"><span class="bot-ava">AI</span>' +
      '<span>取证分析师</span></div><div class="content"></div>';
    const content = box.querySelector('.content');
    (t.tools || []).forEach(tc => {
      if (tc.blocked) {
        content.appendChild(blockedCard({ name: tc.tool, args: safeArgs(tc.args) }));
        return;
      }
      const srv = String(tc.tool || '').includes('.') ? tc.tool.split('.')[0] : '';
      const card = toolCard(tc.tool, safeArgs(tc.args), false, srv);
      fillToolCard(card, { elapsed: tc.elapsed, preview: '', full: tc.result || '' });
      content.appendChild(card);
    });
    if (t.text) content.appendChild(el('div', 'final', md(t.text)));
    st.appendChild(box);
  });

  st.scrollTop = 0;
}

function toast(text) {
  let n = $('#toast');
  if (!n) {
    n = el('div', 'toast');
    n.id = 'toast';
    document.body.appendChild(n);
  }
  n.textContent = text;
  n.classList.add('on');
  clearTimeout(n._t);
  n._t = setTimeout(() => n.classList.remove('on'), 2600);
}

/* ============================================================
   设置：模型 / MCP / 通用
   ============================================================ */
const SET = { tab: 'model', data: null, status: [], editing: null };

async function openSettings(tab) {
  closeAll();
  $('#drawer-settings').classList.add('open');
  $('#scrim').classList.add('on');
  if (tab) SET.tab = tab;
  SET.editing = null;
  $('#set-body').innerHTML = '<div class="empty">读取配置…</div>';
  await loadSettings();
  renderSettings();
}

async function loadSettings() {
  try {
    SET.data = await (await fetch('/api/settings')).json();
  } catch (e) { SET.data = null; }
  try {
    const j = await (await fetch('/api/servers')).json();
    SET.status = j.servers || [];
  } catch (e) { SET.status = []; }
}

function renderSettings() {
  $$('#set-tabs .seg-b').forEach(b => b.classList.toggle('on', b.dataset.tab === SET.tab));
  const body = $('#set-body');
  body.innerHTML = '';
  if (!SET.data || SET.data.error) {
    body.innerHTML = '<div class="empty">读取配置失败：' + esc((SET.data && SET.data.error) || '服务无响应') + '</div>';
    return;
  }
  if (SET.tab === 'model') renderModelPane(body);
  else if (SET.tab === 'mcp') renderMcpPane(body);
  else renderGeneralPane(body);
}

/* ---------------- 模型 ---------------- */
function renderModelPane(body) {
  const llm = SET.data.llm || {};
  const presets = SET.data.presets || [];
  const pane = el('div', 'pane');

  pane.appendChild(el('div', 'pane-h', '接口预设'));
  const grid = el('div', 'preset-grid');
  const curBase = String(llm.base_url || '').replace(/\/+$/, '');
  presets.forEach(p => {
    const on = p.base_url && p.base_url.replace(/\/+$/, '') === curBase;
    const b = el('button', 'preset' + (on ? ' on' : ''));
    b.innerHTML = '<span class="preset-n">' + esc(p.name) + '</span>' +
      '<span class="preset-u">' + esc(p.base_url || '手动填写') + '</span>';
    b.title = p.note || '';
    b.onclick = () => {
      $$('.preset').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      $('#f-base').value = p.base_url || '';
      if (p.models && p.models.length) {
        $('#f-model').value = p.models[0];
        const dl = $('#model-list');
        if (dl) dl.innerHTML = p.models.map(m => '<option value="' + escAttr(m) + '">').join('');
      }
    };
    grid.appendChild(b);
  });
  pane.appendChild(grid);

  pane.appendChild(el('div', 'pane-h', '连接参数'));
  const form = el('div', 'pane');
  form.style.gap = '11px';
  form.innerHTML =
    '<div class="field"><label>接口地址 Base URL</label>' +
      '<input id="f-base" value="' + escAttr(llm.base_url || '') + '" placeholder="https://api.deepseek.com/v1" autocomplete="off"></div>' +
    '<div class="field"><label>API Key</label>' +
      '<div class="key-row">' +
        '<input id="f-key" type="password" value="" placeholder="' +
          (llm.hasKey ? '已保存，留空则沿用原 Key' : 'sk-...') + '" autocomplete="off" spellcheck="false">' +
        '<button class="btn" id="btn-key-eye">显示</button>' +
      '</div>' +
      '<div class="hint2">当前：' + (llm.hasKey
          ? '<b>' + esc(llm.api_key || (llm.fromEnv ? '来自环境变量 MYOS_LLM_API_KEY' : '已设置')) + '</b>'
          : '未设置') + ' · 只存在本机 config.json，前端只拿掩码' +
          (llm.hasKey && !llm.fromEnv ? ' <button class="btn small danger" id="btn-clear-key">清除</button>' : '') +
        '</div>' +
    '</div>' +
    '<div class="field"><label>模型名</label>' +
      '<div class="key-row">' +
        '<input id="f-model" list="model-list" value="' + escAttr(llm.model || '') + '" placeholder="deepseek-chat" autocomplete="off">' +
        '<button class="btn" id="btn-fetch-models">拉取列表</button>' +
      '</div>' +
      '<datalist id="model-list"></datalist>' +
    '</div>' +
    '<div class="row2">' +
      '<div class="field"><label>温度</label><input id="f-temp" type="number" min="0" max="2" step="0.1" value="' +
        escAttr(llm.temperature != null ? llm.temperature : 0.2) + '"></div>' +
      '<div class="field"><label>超时（秒）</label><input id="f-timeout" type="number" min="10" max="600" value="' +
        escAttr(llm.timeout || 180) + '"></div>' +
    '</div>';
  pane.appendChild(form);

  const probe = el('div', 'probe');
  probe.id = 'probe-llm';
  probe.hidden = true;
  pane.appendChild(probe);

  const acts = el('div', 'set-actions');
  acts.innerHTML = '<button class="btn" id="btn-test-llm">测试连接</button>' +
    '<span class="sp"></span>' +
    '<button class="btn primary" id="btn-save-llm">保存</button>';
  pane.appendChild(acts);

  body.appendChild(pane);

  // ---- 交互 ----
  $('#btn-key-eye').onclick = () => {
    const i = $('#f-key');
    const show = i.type === 'password';
    i.type = show ? 'text' : 'password';
    $('#btn-key-eye').textContent = show ? '隐藏' : '显示';
  };
  const clr = $('#btn-clear-key');
  if (clr) {
    clr.onclick = async () => {
      if (!confirm('清除已保存的 API Key？清掉之后就不能对话了，只能手动调工具。')) return;
      const r = await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: '' }),
      });
      const j = await r.json();
      if (j.ok) {
        SET.data = Object.assign(SET.data || {}, j.settings);
        showProbe(probe, 'ok', '已清除 API Key');
        setTimeout(renderSettings, 700);
        refreshStatus();
      } else {
        showProbe(probe, 'bad', '清除失败：' + (j.error || ''));
      }
    };
  }
  $('#btn-test-llm').onclick = () => testLlm(probe, $('#btn-test-llm'));
  $('#btn-save-llm').onclick = () => saveLlm($('#btn-save-llm'));
  $('#btn-fetch-models').onclick = () => fetchModels($('#btn-fetch-models'));

  $('#f-key').addEventListener('keydown', e => { if (e.key === 'Enter') testLlm(probe, $('#btn-test-llm')); });
}

function llmForm() {
  const f = {
    base_url: $('#f-base').value.trim(),
    model: $('#f-model').value.trim(),
    temperature: parseFloat($('#f-temp').value || '0.2'),
    timeout: parseInt($('#f-timeout').value || '180', 10),
  };
  // 只有真输了才带上——留空表示"沿用已存的 Key"，不能误清
  const k = $('#f-key').value.trim();
  if (k) f.api_key = k;
  return f;
}

function showProbe(node, cls, text) {
  node.hidden = false;
  node.className = 'probe ' + cls;
  node.textContent = text;
}

async function testLlm(probe, btn) {
  const f = llmForm();
  if (!f.base_url) { showProbe(probe, 'bad', '先填 Base URL'); return; }
  if (!f.model) { showProbe(probe, 'bad', '先填模型名'); return; }
  showProbe(probe, 'busy', '正在向 ' + f.base_url + ' 发一条最小请求…');
  if (btn) { btn.disabled = true; btn.textContent = '测试中…'; }
  try {
    const r = await fetch('/api/llm/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(f),
    });
    const j = await r.json();
    if (j.ok) {
      const u = j.usage && j.usage.total_tokens ? ' · tokens ' + j.usage.total_tokens : '';
      showProbe(probe, 'ok',
        '连接成功 ' + j.latencyMs + 'ms\n' +
        'endpoint  ' + j.endpoint + '\n' +
        '服务端模型 ' + j.serverModel + u +
        (j.reply ? '\n回复：' + j.reply : ''));
      return true;
    }
    showProbe(probe, 'bad', '连接失败\n' + (j.error || '未知错误') +
      (j.endpoint ? '\nendpoint  ' + j.endpoint : ''));
    return false;
  } catch (e) {
    showProbe(probe, 'bad', '请求异常：' + e.message);
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试连接'; }
  }
}

async function fetchModels(btn) {
  const f = llmForm();
  const probe = $('#probe-llm');
  if (!f.base_url) { showProbe(probe, 'bad', '先填 Base URL'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '拉取中…'; }
  try {
    const r = await fetch('/api/llm/models', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url: f.base_url, api_key: f.api_key }),
    });
    const j = await r.json();
    if (!j.ok) {
      showProbe(probe, 'bad', '拉不到模型列表（很多中转站不开放 /models，直接手填模型名即可）\n' + (j.error || ''));
      return;
    }
    const dl = $('#model-list');
    dl.innerHTML = j.models.map(m => '<option value="' + escAttr(m) + '">').join('');
    if (j.models.length && !$('#f-model').value) $('#f-model').value = j.models[0];
    showProbe(probe, 'ok', '拉到 ' + j.models.length + ' 个模型，输入框里可以直接选\n' +
      j.models.slice(0, 12).join('\n') + (j.models.length > 12 ? '\n…' : ''));
  } catch (e) {
    showProbe(probe, 'bad', '请求异常：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '拉取列表'; }
  }
}

async function saveLlm(btn) {
  const f = llmForm();
  const probe = $('#probe-llm');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(f),
    });
    const j = await r.json();
    if (!j.ok) { showProbe(probe, 'bad', '保存失败：' + (j.error || '未知')); return; }
    SET.data = Object.assign(SET.data || {}, j.settings);
    showProbe(probe, 'ok', '已保存到 config.json' + (j.changed ? ' · 新配置下一条消息立即生效' : ' · 内容无变化'));
    await refreshStatus();
    // 只刷新表单里的掩码提示，不整页重绘（避免清掉刚输入的内容）
    const keyInput = $('#f-key');
    if (keyInput) { keyInput.value = ''; keyInput.placeholder = '已保存，留空则沿用原 Key'; }
  } catch (e) {
    showProbe(probe, 'bad', '请求异常：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '保存'; }
  }
}

/* ---------------- MCP ---------------- */
function renderMcpPane(body) {
  const servers = SET.data.servers || {};
  const status = {};
  (SET.status || []).forEach(s => { status[s.name] = s; });

  const pane = el('div', 'pane');
  pane.appendChild(el('div', 'pane-h', 'MCP 服务器（stdio）'));
  const tip = el('div', 'pane-tip');
  tip.innerHTML = '改完立刻写进 <code>config.json</code> 并重连，不用重启控制台。' +
    '一台连不上不影响其它台，工具会合并成一个池子给模型用。';
  pane.appendChild(tip);

  const names = Object.keys(servers);
  if (!names.length && SET.editing !== '__new__') {
    pane.appendChild(el('div', 'empty', '还没有配置服务器。点下面新增一台。'));
  }

  names.forEach(name => {
    const cfg = servers[name] || {};
    const st = status[name] || {};
    const editing = SET.editing === name;
    const dark = el('div', 'mcard' + (editing ? ' editing' : ''));

    const stTxt = { ready: '已连接', error: '连接失败', disabled: '未启用', connecting: '连接中', idle: '未连接' }[st.state] || st.state || '未知';
    const stCls = st.ready ? 'ok' : (st.state === 'error' ? 'bad' : (st.state === 'connecting' ? 'busy' : ''));
    dark.innerHTML =
      '<div class="mcard-h">' +
        '<span class="srv-dot ' + stCls + '"></span>' +
        '<span class="mcard-name">' + esc(name) + '</span>' +
        '<span class="mcard-label">' + esc(cfg.label || '') + '</span>' +
        '<span class="grow"></span>' +
        '<span class="srv-badge ' + stCls + '">' + esc(stTxt) + '</span>' +
        '<span class="srv-n">' + (st.toolCount || 0) + ' 工具</span>' +
      '</div>' +
      (st.error ? '<div class="srv-err">' + esc(String(st.error).slice(0, 240)) + '</div>' : '') +
      '<div class="srv-cmd">' + esc(cfg.command || '—') +
        ((cfg.args && cfg.args.length) ? ' ' + esc(cfg.args.join(' ')) : '') + '</div>';

    const bar = el('div', 'srv-bar');
    const editBtn = el('button', 'btn small', editing ? '收起' : '编辑');
    editBtn.onclick = () => { SET.editing = editing ? null : name; renderSettings(); };
    bar.appendChild(editBtn);
    if (!editing) {
      const rc = el('button', 'btn small', '重连');
      rc.onclick = async () => { rc.disabled = true; rc.textContent = '…'; await reconnectServer(name, null); await loadSettings(); renderSettings(); };
      bar.appendChild(rc);
      const sw = el('label', 'switch small');
      sw.innerHTML = '<input type="checkbox"' + (cfg.enabled !== false ? ' checked' : '') +
        '><span class="sw"></span><span class="sw-t">启用</span>';
      sw.querySelector('input').onchange = async (e) => {
        if (!e.target.checked && !allowDisable(name)) {
          e.target.checked = true;
          return;
        }
        await toggleServer(name, e.target.checked);
        await loadSettings(); renderSettings();
      };
      bar.appendChild(sw);
    }
    dark.appendChild(bar);

    if (editing) dark.appendChild(mcpForm(name, cfg));
    pane.appendChild(dark);
  });

  if (SET.editing === '__new__') {
    const c = el('div', 'mcard editing');
    c.innerHTML = '<div class="mcard-h"><span class="mcard-name">新增服务器</span></div>';
    c.appendChild(mcpForm(null, { enabled: true, timeout: 120 }));
    pane.appendChild(c);
  } else {
    const add = el('button', 'btn dashed', '＋ 新增 MCP 服务器');
    add.onclick = () => { SET.editing = '__new__'; renderSettings(); };
    pane.appendChild(add);
  }

  body.appendChild(pane);
}

function mcpForm(originalName, cfg) {
  const isNew = !originalName;
  const wrap = el('div', 'mcard-form');
  const envText = Object.entries(cfg.env || {}).map(([k, v]) => k + '=' + v).join('\n');
  wrap.innerHTML =
    '<div class="row2">' +
      '<div class="field"><label>名字 <span class="req">*</span></label>' +
        '<input id="m-name" value="' + escAttr(originalName || '') + '" placeholder="myos-forensics" autocomplete="off" spellcheck="false"></div>' +
      '<div class="field"><label>显示名</label>' +
        '<input id="m-label" value="' + escAttr(cfg.label || '') + '" placeholder="超级取证大师"></div>' +
    '</div>' +
    '<div class="field"><label>command <span class="req">*</span></label>' +
      '<input id="m-cmd" value="' + escAttr(cfg.command || '') + '" placeholder="D:/.../myos_mcp_server.exe" autocomplete="off" spellcheck="false"></div>' +
    '<div class="field"><label>args（空格分隔）</label>' +
      '<input id="m-args" value="' + escAttr((cfg.args || []).join(' ')) + '" placeholder="留空即可" autocomplete="off" spellcheck="false"></div>' +
    '<div class="field"><label>env（每行 KEY=VALUE）</label>' +
      '<textarea id="m-env" rows="2" placeholder="QT_PLUGIN_PATH=D:/...">' + esc(envText) + '</textarea></div>' +
    '<div class="row2">' +
      '<div class="field"><label>超时（秒）</label>' +
        '<input id="m-timeout" type="number" min="5" max="600" value="' + escAttr(cfg.timeout || 120) + '"></div>' +
      '<div class="field"><label>说明</label>' +
        '<input id="m-note" value="' + escAttr(cfg.note || '') + '" placeholder="需要先启动什么"></div>' +
    '</div>' +
    '<div class="probe" id="m-probe" hidden></div>' +
    '<div class="form-actions">' +
      '<label class="switch small"><input type="checkbox" id="m-enabled"' +
        (cfg.enabled !== false ? ' checked' : '') + '><span class="sw"></span><span class="sw-t">启用</span></label>' +
      '<span class="sp"></span>' +
      '<button class="btn" id="m-test">测试</button>' +
      '<button class="btn primary" id="m-save">保存</button>' +
      '<button class="btn" id="m-cancel">取消</button>' +
      (isNew ? '' : '<button class="btn danger" id="m-del">删除</button>') +
    '</div>';

  const probe = wrap.querySelector('#m-probe');

  const collect = () => ({
    originalName: originalName || '',
    name: (wrap.querySelector('#m-name').value || '').trim(),
    label: (wrap.querySelector('#m-label').value || '').trim(),
    command: (wrap.querySelector('#m-cmd').value || '').trim(),
    args: (wrap.querySelector('#m-args').value || '').trim(),
    env: wrap.querySelector('#m-env').value || '',
    timeout: parseInt(wrap.querySelector('#m-timeout').value || '120', 10),
    note: (wrap.querySelector('#m-note').value || '').trim(),
    enabled: wrap.querySelector('#m-enabled').checked,
    kind: 'mcp-stdio',
  });

  wrap.querySelector('#m-test').onclick = async (e) => {
    const btn = e.currentTarget;
    const body = collect();
    if (!body.name || !body.command) { showProbe(probe, 'bad', '名字和 command 都要填'); return; }
    showProbe(probe, 'busy', '正在拉起进程并握手（最多 60 秒）…');
    btn.disabled = true; btn.textContent = '测试中…';
    try {
      const r = await fetch('/api/server/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (j.ok) {
        const s = j.server || {};
        showProbe(probe, 'ok', '握手成功 · ' + (s.server || 'mcp') + ' v' + (s.version || '?') +
          '\n工具 ' + (s.toolCount || 0) + ' 个 · ' + (j.elapsedMs || 0) + 'ms · protocol ' + (s.protocol || '?'));
      } else {
        const s = j.server || {};
        showProbe(probe, 'bad', '连接失败\n' + (j.error || s.error || '未知错误') +
          '\n如果是本地取证软件，先确认它已经启动。');
      }
    } catch (err) {
      showProbe(probe, 'bad', '请求异常：' + err.message);
    } finally {
      btn.disabled = false; btn.textContent = '测试';
    }
  };

  wrap.querySelector('#m-save').onclick = async (e) => {
    const btn = e.currentTarget;
    const body = collect();
    if (!body.name) { showProbe(probe, 'bad', '名字不能为空'); return; }
    if (!body.command) { showProbe(probe, 'bad', 'command 不能为空'); return; }
    showProbe(probe, 'busy', '保存并连接中…');
    btn.disabled = true; btn.textContent = '保存中…';
    try {
      const r = await fetch('/api/server/upsert', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!j.ok) { showProbe(probe, 'bad', '保存失败：' + (j.error || '')); return; }
      const s = j.server || {};
      if (s.ready) {
        showProbe(probe, 'ok', '已保存并连上 · 工具 ' + (s.toolCount || 0) + ' 个');
      } else {
        showProbe(probe, 'busy', '配置已保存，但连接没成功：' + (s.error || '见连接器面板'));
      }
      SET.editing = null;
      await loadSettings();
      await refreshStatus();
      setTimeout(() => { renderSettings(); }, 900);
    } catch (err) {
      showProbe(probe, 'bad', '请求异常：' + err.message);
    } finally {
      btn.disabled = false; btn.textContent = '保存';
    }
  };

  wrap.querySelector('#m-cancel').onclick = () => { SET.editing = null; renderSettings(); };

  const del = wrap.querySelector('#m-del');
  if (del) {
    del.onclick = async () => {
      if (!confirm('从配置里删除「' + originalName + '」？\n只是移出 config.json，不会卸载软件本身。')) return;
      del.disabled = true;
      try {
        const r = await fetch('/api/server/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: originalName }),
        });
        const j = await r.json();
        if (!j.ok) { showProbe(probe, 'bad', '删除失败：' + (j.error || '')); return; }
        SET.editing = null;
        await loadSettings();
        await refreshStatus();
        renderSettings();
      } catch (err) {
        showProbe(probe, 'bad', '请求异常：' + err.message);
      } finally {
        del.disabled = false;
      }
    };
  }

  return wrap;
}

/* ---------------- 通用 ---------------- */
function renderGeneralPane(body) {
  const d = SET.data;
  const pane = el('div', 'pane');
  // 扫描目录的真实落点：配置里可能写的是 ./skills，光看它不知道指到哪去了
  const resolved = ((d.skills && d.skills.resolved) || []);
  const rootsInfo = resolved.length
    ? resolved.map(r =>
        '目录 ' + r.order + '：<code>' + esc(r.path) + '</code> → ' +
        (r.exists ? r.count + ' 个技能' : '<b>目录不存在</b>')
      ).join('<br>') +
      '<br>同名技能按上面的顺序取第一个，当前共 <b>' + ((d.skills && d.skills.count) || 0) + '</b> 个。'
    : '技能目录尚未扫描。';
  pane.innerHTML =
    '<div class="pane-h">行为</div>' +
    '<label class="switch"><input type="checkbox" id="g-write"' + (d.autoYes ? ' checked' : '') +
      '><span class="sw"></span><span class="sw-t">允许写操作（解密分区 / 提交取证 / 导出 / 关案件）</span></label>' +
    '<div class="pane-tip">默认关闭。关着的时候，模型碰到会改动证据状态的操作会被拦下来并告诉你，不会自己重试。</div>' +
    '<div class="field"><label>补充上下文（每轮都会拼进系统提示词）</label>' +
      '<textarea id="g-ctx" rows="3" placeholder="例如：本机只做电子数据检验，不涉及现场勘查">' +
      esc((d.agent && d.agent.context) || '') + '</textarea></div>' +
    '<div class="field"><label>工具超时（秒）</label>' +
      '<input id="g-tt" type="number" min="10" max="1800" value="' + escAttr(d.tool_timeout || 300) + '"></div>' +
    '<div class="field"><label>单批轮数（模型「想」多少次算一批）</label>' +
      '<input id="g-turns" type="number" min="1" max="200" value="' +
      escAttr((d.agent && d.agent.max_turns) || 40) + '"></div>' +
    '<div class="field"><label>单批工具调用次数</label>' +
      '<input id="g-calls" type="number" min="10" max="1000" value="' +
      escAttr((d.agent && d.agent.tool_budget) || 120) + '"></div>' +
    '<div class="field"><label>自动续期批数（0 = 用尽即停）</label>' +
      '<input id="g-renew" type="number" min="0" max="10" value="' +
      escAttr((d.agent && d.agent.renew_budget != null) ? d.agent.renew_budget : 3) + '"></div>' +
    '<div class="pane-tip">一轮 = 模型想一次 + 这一轮要的工具全跑完。一批额度用尽时，' +
      '只要还在推进（没报错、没重复调用、没重复结果）就自动再续一批，最多续 N 批。' +
      '默认 40 轮 + 120 次工具 + 续 3 批 ≈ 最多 160 轮，长任务跑得完，' +
      '真在原地打转仍会提前停。</div>' +
    '<div class="pane-h">技能</div>' +
    '<div class="field"><label>扫描目录（每行一个，相对路径按项目根解析）</label>' +
      '<textarea id="g-roots" rows="3">' + esc(((d.skills && d.skills.roots) || []).join('\n')) + '</textarea></div>' +
    '<div class="roots-info">' + rootsInfo + '</div>' +
    '<div class="field"><label>命令白名单（每行一个）</label>' +
      '<textarea id="g-allow" rows="4">' + esc(((d.skills && d.skills.allow_commands) || []).join('\n')) + '</textarea></div>' +
    '<div class="pane-h">关于</div>' +
    '<div class="pane-tip">配置文件：<code>' + esc(d.configPath || '') + '</code><br>' +
      '模型 Key 只写本地文件，接口只回掩码；本控制台默认只监听 127.0.0.1。<br>' +
      '深链接：<code>#settings</code> 设置 · <code>#mcp</code> 服务器 · <code>?tab=general</code> 本页</div>' +
    '<div class="probe" id="g-probe" hidden></div>' +
    '<div class="set-actions"><span class="sp"></span><button class="btn primary" id="g-save">保存</button></div>';
  body.appendChild(pane);

  const probe = $('#g-probe');
  $('#g-write').onchange = async (e) => {
    const on = e.target.checked;
    if (on && !confirm('开启后，智能体可以执行解密分区、提交取证、导出、关闭案件等会改动状态的操作。\n确认开启？')) {
      e.target.checked = false;
      return;
    }
    await fetch('/api/autoyes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on }),
    });
    SET.data.autoYes = on;
    showProbe(probe, 'ok', on ? '已开启写操作' : '已关闭写操作');
    refreshStatus();
  };

  $('#g-save').onclick = async (e) => {
    const btn = e.currentTarget;
    const split = v => v.split('\n').map(x => x.trim()).filter(Boolean);
    btn.disabled = true;
    try {
      const r = await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          context: $('#g-ctx').value,
          tool_timeout: parseInt($('#g-tt').value || '300', 10),
          max_turns: parseInt($('#g-turns').value || '40', 10),
          tool_budget: parseInt($('#g-calls').value || '120', 10),
          renew_budget: parseInt($('#g-renew').value || '0', 10),
          skills: {
            roots: split($('#g-roots').value),
            allow_commands: split($('#g-allow').value),
          },
        }),
      });
      const j = await r.json();
      if (!j.ok) { showProbe(probe, 'bad', '保存失败：' + (j.error || '')); return; }
      SET.data = Object.assign(SET.data || {}, j.settings);
      showProbe(probe, 'ok', '已保存（技能目录改动需要重启控制台才重新扫描）');
    } catch (err) {
      showProbe(probe, 'bad', '请求异常：' + err.message);
    } finally {
      btn.disabled = false;
    }
  };
}

function initEvents() {
  const input = $('#input');
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      // 跑着的时候 Enter 不是「再问一句」，而是「插句话引导它」——
      // 这时候后端还锁着，硬发就是「上一条还在跑」的报错。
      if (S.streaming) inject(input.value);
      else send(input.value);
    }
  });
  $('#btn-send').onclick = () => (S.streaming ? inject(input.value) : send(input.value));
  $('#btn-stop').onclick = () => stopRun();

  $('#btn-new').onclick = async () => {
    await fetch('/api/reset', { method: 'POST' });
    $('#stream').innerHTML = '';
    location.reload();
  };

  $('#btn-health').onclick = doHealth;
  $('#btn-reload-cases').onclick = loadCases;

  $('#chk-write').onchange = async (e) => {
    const on = e.target.checked;
    if (on && !confirm('开启后，智能体可以执行解密分区、提交取证、导出、关闭案件等会改动状态的操作。\n确认开启？')) {
      e.target.checked = false;
      return;
    }
    await fetch('/api/autoyes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on }),
    });
    refreshStatus();
  };

  $('#btn-tools').onclick = async () => {
    closeAll();
    $('#drawer').classList.add('open');
    $('#scrim').classList.add('on');
    await loadTools();
  };
  $('#btn-servers').onclick = openServers;
  $('#btn-skills').onclick = openSkills;
  $('#btn-history').onclick = openHistory;

  $('#btn-close-drawer').onclick = closeAll;
  $('#btn-close-servers').onclick = closeAll;
  $('#btn-close-skills').onclick = closeAll;
  $('#btn-close-history').onclick = closeAll;
  $('#scrim').onclick = closeAll;

  // 历史会话：活跃 / 归档 切换
  $$('#hist-tabs .seg-b').forEach(b => {
    b.onclick = () => {
      HIST.archived = b.dataset.arch === '1';
      $$('#hist-tabs .seg-b').forEach(x => x.classList.toggle('on', x === b));
      loadHistory();
    };
  });
  $('#hist-search').oninput = (e) => { HIST.q = e.target.value; renderHistory(); };

  // 多选：进/出、全选、批量归档/删除/取消
  $('#btn-hist-select').onclick = () => setPickMode(!HIST.mode);
  $('#hist-all').onchange = (e) => {
    const on = e.target.checked;
    HIST.sel.clear();
    if (on) (HIST.visible || []).forEach(r => HIST.sel.add(r.id));
    renderHistory();
  };
  $('#btn-bulk-del').onclick = () => bulkAction('delete').catch(e => toast('删除失败：' + e.message));
  $('#btn-bulk-arch').onclick = () => bulkAction('arch').catch(e => toast('归档失败：' + e.message));
  $('#btn-bulk-cancel').onclick = () => setPickMode(false);
  $('#btn-hist-new').onclick = async () => {
    await fetch('/api/reset', { method: 'POST' });
    location.reload();
  };
  $('#btn-hist-purge').onclick = async () => {
    const n = HIST.rows.filter(r => !r.turns).length;
    if (!n) { toast('没有可清理的会话'); return; }
    if (!confirm('删掉 ' + n + ' 个「没提过问」的会话？\n\n' +
      '包括打开页面时自动拉数据留下的工具调用记录和早期空文件。\n' +
      '有过对话的会话不受影响。')) return;
    try {
      const j = await postJson('/api/session/purge', {});
      toast('已清理 ' + (j.removed || 0) + ' 个会话');
      await loadHistory();
    } catch (e) { alert('清理失败：' + e.message); }
  };

  $('#btn-runner-close').onclick = () => { $('#runner').hidden = true; S.selTool = null; $$('.titem').forEach(x => x.classList.remove('sel')); };
  $('#tool-search').oninput = (e) => renderTools(e.target.value);
  $('#btn-run').onclick = runTool;

  $('#btn-reconnect-all').onclick = () => reconnectServer(null);

  $('#btn-settings').onclick = () => openSettings();
  $('#btn-close-settings').onclick = closeAll;
  $$('#set-tabs .seg-b').forEach(b => {
    b.onclick = () => { SET.tab = b.dataset.tab; SET.editing = null; renderSettings(); };
  });
  $('#btn-manage-mcp').onclick = () => openSettings('mcp');
  $('#skill-search').oninput = (e) => renderSkills(e.target.value);
  $('#btn-skill-run').onclick = runSkill;
  $('#skill-cmd').addEventListener('keydown', e => { if (e.key === 'Enter') runSkill(); });
  $('#btn-skill-close').onclick = () => { $('#skill-panel').hidden = true; S.curSkill = null; $$('.sk').forEach(x => x.classList.remove('sel')); };

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    // Esc 优先干最该干的事：退出多选 > 中断推理 > 关抽屉
    if (HIST.mode) { setPickMode(false); return; }
    if (S.streaming) { stopRun(); return; }
    closeAll();
  });
}

(async function boot() {
  // ?snap=1 关掉所有过渡动画（截图、演示、低配机器用）
  if (new URLSearchParams(location.search).get('snap') === '1') {
    document.documentElement.dataset.snap = '1';
  }
  initTheme();
  initQuick();
  initEvents();
  const st = await refreshStatus();
  // 后端 agent 没起来才算「不能用」；只是取证服务没连上时 refreshStatus 已经
  // 写成「取证服务未连接 · 仍可对话」，这里别再拿红色「未连接」盖掉它。
  if (!st || !st.agentReady) {
    $('#dot').className = 'dot bad';
    $('#connText').textContent = '后端未就绪 · 点此重试';
    $('#connText').style.cursor = 'pointer';
    $('#connText').onclick = reconnect;
  }
  loadCases();
  // 深链接：#tools 直接开工具面板
  if (location.hash === '#tools') setTimeout(() => $('#btn-tools').click(), 300);
  if (location.hash === '#servers') setTimeout(() => $('#btn-servers').click(), 300);
  if (location.hash === '#skills') setTimeout(() => $('#btn-skills').click(), 300);
  if (location.hash === '#history') setTimeout(() => $('#btn-history').click(), 300);
  // ?composer=running 直接把输入区摆成「运行中」（截图 / 演示用）
  if (new URLSearchParams(location.search).get('composer') === 'running') {
    setTimeout(() => setComposerMode('running'), 400);
  }
  // ?ask=文字 自动发一句（截图 / 冒烟测试用，省得手点）
  const askParam = new URLSearchParams(location.search).get('ask');
  if (askParam) setTimeout(() => send(askParam), 500);
  // ?pick=1 打开历史抽屉并直接进多选（演示 / 截图用，也方便手快直接删）
  if (new URLSearchParams(location.search).get('pick') === '1') {
    setTimeout(async () => {
      $('#btn-history').click();
      await new Promise(r => setTimeout(r, 400));
      setPickMode(true);
      // ?pickall=1 顺便全选，方便看批量条长什么样
      if (new URLSearchParams(location.search).get('pickall') === '1') {
        (HIST.visible || []).slice(0, 3).forEach(r => HIST.sel.add(r.id));
        renderHistory();
      }
    }, 300);
  }
  if (location.hash === '#settings') setTimeout(() => openSettings(), 300);
  if (location.hash === '#mcp') setTimeout(() => openSettings('mcp'), 300);
  if (location.hash === '#model') setTimeout(() => openSettings('model'), 300);
  // ?tab=model|mcp|general 直接落到某个设置页
  const tabParam = new URLSearchParams(location.search).get('tab');
  if (tabParam) setTimeout(() => openSettings(tabParam), 300);
  // ?mcpedit=名字 直接展开某台服务器的编辑表单
  const mcpEdit = new URLSearchParams(location.search).get('mcpedit');
  if (mcpEdit) setTimeout(async () => {
    await openSettings('mcp');
    SET.editing = mcpEdit;
    renderSettings();
  }, 400);
  // ?skill=名称 直接展开某个技能（方便分享 / 排障）
  const skParam = new URLSearchParams(location.search).get('skill');
  if (skParam) setTimeout(async () => { await openSkills(); openSkill(skParam); }, 350);
  // ?hist=archived 打开历史抽屉并落到「归档」页
  if (new URLSearchParams(location.search).get('hist') === 'archived') {
    setTimeout(() => {
      $('#btn-history').click();
      setTimeout(() => {
        const b = $('#hist-tabs .seg-b[data-arch="1"]');
        if (b) b.click();
      }, 250);
    }, 300);
  }
  // ?session=<id> 直接打开某个历史会话
  const sessParam = new URLSearchParams(location.search).get('session');
  if (sessParam) setTimeout(() => openSession(sessParam).catch(e => console.warn(e)), 400);
  // ?q=... 自动提问（方便做脚本截图/分享固定问法）
  const q = new URLSearchParams(location.search).get('q');
  if (q) setTimeout(() => send(q), 200);
  // ?demo=chat 渲染一段固定对话（截图/演示用，不连模型）
  if (new URLSearchParams(location.search).get('demo') === 'chat') setTimeout(demoChat, 300);
  // 定时校准状态（MCP 可能因 D-Bus 断开而自行退出）
  setInterval(() => { if (!S.streaming) refreshStatus(); }, 15000);
})();

/* ============================================================
   侧栏：拖右边缘改宽度 / 点左上角 ☰（或 Ctrl+B）收起展开
   ------------------------------------------------------------
   宽度走 CSS 变量 --side-w，grid 列宽和背景图的居中偏移都跟着它走，
   所以拉宽、收窄、收起，后面那张星空图的重心始终对着聊天区中线。
   ============================================================ */
(function sidePanel() {
  const app = $('.app'), side = $('.sidebar');
  const grip = $('#side-grip'), btn = $('#btn-side');
  if (!app || !side || !grip || !btn) return;

  const KEY_W = 'side.width', KEY_OFF = 'side.off';
  const MIN = 200, MAX = 520, DEFAULT = 256;
  const root = document.documentElement;

  const curW = () => parseFloat(getComputedStyle(root).getPropertyValue('--side-w')) || 0;
  const setW = (w) => Math.round(Math.min(MAX, Math.max(MIN, w)));

  let prevW = DEFAULT;      // 收起前记住的宽度，展开时恢复

  /* ---- 恢复上次的状态 ---- */
  try {
    const w = parseInt(localStorage.getItem(KEY_W) || '', 10);
    if (w > 0) {
      prevW = setW(w);
      root.style.setProperty('--side-w', prevW + 'px');
    }
  } catch (_) { }

  /* ---- 拖右边缘改宽度 ---- */
  let drag = false, startX = 0, startW = 0;

  grip.addEventListener('pointerdown', (e) => {
    if (app.classList.contains('side-off')) toggle(false);   // 收起状态下直接拖 = 一边拖一边展开
    drag = true;
    startX = e.clientX;
    startW = side.getBoundingClientRect().width || 0;
    grip.classList.add('dragging');
    side.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    try { grip.setPointerCapture(e.pointerId); } catch (_) { }
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const w = setW(startW + (e.clientX - startX));
    root.style.setProperty('--side-w', w + 'px');
  });
  const stopDrag = (e) => {
    if (!drag) return;
    drag = false;
    grip.classList.remove('dragging');
    side.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { grip.releasePointerCapture(e.pointerId); } catch (_) { }
    prevW = setW(side.getBoundingClientRect().width || DEFAULT);
    try { localStorage.setItem(KEY_W, String(prevW)); } catch (_) { }
  };
  grip.addEventListener('pointerup', stopDrag);
  grip.addEventListener('pointercancel', stopDrag);
  grip.addEventListener('dblclick', () => {
    prevW = DEFAULT;
    root.style.setProperty('--side-w', DEFAULT + 'px');
    try { localStorage.setItem(KEY_W, String(DEFAULT)); } catch (_) { }
  });

  /* ---- 收起 / 展开 ---- */
  function toggle(force) {
    const off = force === undefined ? !app.classList.contains('side-off') : !!force;
    if (off) {
      const w = curW();
      if (w > 0) prevW = w;                        // 记下来，展开时还原
      root.style.setProperty('--side-w', '0px');
    } else {
      root.style.setProperty('--side-w', Math.max(MIN, prevW || DEFAULT) + 'px');
    }
    app.classList.toggle('side-off', off);
    btn.title = (off ? '展开' : '收起') + '侧栏（Ctrl+B）';
    try { localStorage.setItem(KEY_OFF, off ? '1' : '0'); } catch (_) { }
  }

  btn.addEventListener('click', () => toggle());
  // 侧栏自己那个 « 只负责收起（展开交给顶栏的 ☰ 或 Ctrl+B）
  const btnIn = $('#btn-side-in');
  if (btnIn) btnIn.addEventListener('click', () => toggle(true));
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      toggle();
    }
  });

  /* ---- 开机就按上次的状态摆好 ---- */
  try {
    if (localStorage.getItem(KEY_OFF) === '1') toggle(true);
    else btn.title = '收起侧栏（Ctrl+B）';
  } catch (_) { }
})();

/* ============================================================
   历史面板：拖右边缘改宽度（扩展）/ 双击复位（收起默认宽）
   ------------------------------------------------------------
   宽度存 localStorage，下次打开还是上次的尺寸。
   上限跟随窗口算，保证拖到最宽也不会顶出屏幕右边。
   ============================================================ */
(function histResize() {
  const d = $('#drawer-history'), grip = $('#hist-grip');
  if (!d || !grip) return;

  const KEY = 'hist.panel.width';
  const MIN = 300;
  const DEFAULT = 440;

  const maxW = () => {
    // 侧栏宽度是可拖的，面板的上限要跟着它算，别顶出右边
    const sw = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue('--side-w')) || 0;
    return Math.max(MIN + 80, window.innerWidth - sw - 16);
  };
  const applyW = (w) => {
    d.style.width = Math.round(Math.min(maxW(), Math.max(MIN, w))) + 'px';
  };

  try {
    const saved = parseInt(localStorage.getItem(KEY) || '', 10);
    if (saved > 0) applyW(saved);
  } catch (_) { /* 隐私模式禁 localStorage 就算了 */ }

  let dragging = false, startX = 0, startW = 0;

  grip.addEventListener('pointerdown', (e) => {
    dragging = true;
    startX = e.clientX;
    startW = d.getBoundingClientRect().width;
    grip.classList.add('dragging');
    d.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    try { grip.setPointerCapture(e.pointerId); } catch (_) { }
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    applyW(startW + (e.clientX - startX));
  });
  const stopDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('dragging');
    d.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { grip.releasePointerCapture(e.pointerId); } catch (_) { }
    try { localStorage.setItem(KEY, String(Math.round(d.getBoundingClientRect().width))); } catch (_) { }
  };
  grip.addEventListener('pointerup', stopDrag);
  grip.addEventListener('pointercancel', stopDrag);
  grip.addEventListener('dblclick', () => {
    d.style.width = DEFAULT + 'px';
    try { localStorage.removeItem(KEY); } catch (_) { }
  });

  // 窗口变窄时把超宽的面板收回来
  window.addEventListener('resize', () => {
    if (d.getBoundingClientRect().width > maxW()) applyW(maxW());
  });

  // Esc 收起（面板打开时才有意义，别的抽屉不抢这个键）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && d.classList.contains('open') && !dragging) {
      d.classList.remove('open');
      const sc = $('#scrim'); if (sc) sc.classList.remove('on');
    }
  });
})();
