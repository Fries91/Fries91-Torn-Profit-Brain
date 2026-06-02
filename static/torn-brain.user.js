// ==UserScript==
// @name         Fries91 Torn Brain - Step 1 Shell
// @namespace    Fries91.TornBrain
// @version      1.8.11-login-stockfix
// @description  Self-learning Torn profit and war intel app. Step 8.11: limited key login fallback, stock scan PostgreSQL fix, smaller floating AI icon, and PDA fixes.
// @author       Fries91
// @match        https://www.torn.com/*
// @grant        GM_addStyle
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_xmlhttpRequest
// @connect      fries91-torn-profit-brain.onrender.com
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // CHANGE THIS AFTER RENDER DEPLOYMENT:
  const API_BASE = 'https://fries91-torn-profit-brain.onrender.com';

  const K_TOKEN = 'fries91_torn_brain_token_v1';
  const K_TOKEN_BACKUP = 'fries91_torn_brain_token_backup_v1';
  const K_APIKEY = 'fries91_torn_brain_api_key_v1';
  const K_OPEN = 'fries91_torn_brain_open_v1';
  const K_ICON_POS = 'fries91_torn_brain_icon_pos_v2';
    const TABS = ['Overview', 'Stock Brain', 'Item Market', 'Travel Profit', 'Points Watcher', 'Enemy Sleep', 'Notifications', 'Accuracy', 'Settings'];

  let state = null;
  let dashboard = null;
  let activeTab = GM_getValue('fries91_torn_brain_tab_v1', 'Overview');
  if (activeTab === 'Alerts') activeTab = 'Notifications';
  let mounted = false;
  let refreshTimer = null;
  let refreshBusy = false;
  let restoreBusy = false;
  let tabsTouchStartX = 0;
  let tabsTouchStartY = 0;
  let tabsMoved = false;
  let lastStateRefresh = 0;
  let authToken = '';

  GM_addStyle(`
    @keyframes tbPulseGlow {
      0%, 100% { box-shadow: 0 0 10px rgba(34,197,94,.45), 0 0 24px rgba(250,204,21,.20), 0 8px 24px rgba(0,0,0,.70); transform: translateZ(0) scale(1); }
      50% { box-shadow: 0 0 18px rgba(34,197,94,.85), 0 0 36px rgba(250,204,21,.38), 0 8px 30px rgba(0,0,0,.80); transform: translateZ(0) scale(1.04); }
    }
    @keyframes tbScanLine {
      0% { transform: translateX(-110%); opacity: .2; }
      40% { opacity: 1; }
      100% { transform: translateX(110%); opacity: .2; }
    }
    @keyframes tbFadeUp {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes tbGridMove {
      from { background-position: 0 0, 0 0; }
      to { background-position: 0 0, 38px 38px; }
    }
    @keyframes tbShimmer {
      0% { transform: translateX(-120%); }
      100% { transform: translateX(120%); }
    }
    #tb-icon {
      position: fixed;
      left: 14px;
      bottom: 74px;
      z-index: 2147483647;
      width: 34px;
      height: 34px;
      border-radius: 11px;
      background:
        radial-gradient(circle at 22% 20%, rgba(34,197,94,.36), transparent 34%),
        linear-gradient(135deg, #07110b, #111827 45%, #2a1d08);
      border: 1px solid rgba(250,204,21,.82);
      color: #dcfce7;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 15px;
      font-weight: 1000;
      line-height: 1;
      letter-spacing: 0;
      text-shadow: 0 0 8px rgba(34,197,94,.95), 0 0 10px rgba(250,204,21,.55);
      box-shadow: 0 6px 18px rgba(0,0,0,.70), 0 0 16px rgba(34,197,94,.34);
      cursor: pointer;
      user-select: none;
      touch-action: none;
      overflow: hidden;
    }
    #tb-icon:before {
      content: '';
      position: absolute;
      inset: 4px;
      border-radius: 8px;
      border: 1px solid rgba(34,197,94,.20);
      pointer-events: none;
    }
    #tb-panel {
      position: fixed;
      left: 8px;
      right: 8px;
      top: 50px;
      bottom: 10px;
      z-index: 2147483646;
      color: #f7fee7;
      border: 1px solid rgba(250,204,21,.70);
      border-radius: 16px;
      box-shadow: 0 20px 58px rgba(0,0,0,.78), 0 0 36px rgba(34,197,94,.12);
      display: none;
      overflow: hidden;
      font-family: Arial, Helvetica, sans-serif;
      max-width: 760px;
      margin: 0 auto;
      background:
        radial-gradient(circle at 10% 0%, rgba(34,197,94,.18), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(250,204,21,.12), transparent 30%),
        linear-gradient(180deg, rgba(5,10,7,.98), rgba(9,9,11,.98));
    }
    #tb-panel:before {
      content: '';
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(34,197,94,.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(34,197,94,.045) 1px, transparent 1px);
      background-size: 38px 38px;
      pointer-events: none;
      animation: tbGridMove 8s linear infinite;
    }
    #tb-panel.tb-show { display: flex; flex-direction: column; animation: tbFadeUp .18s ease-out; }
    .tb-head, .tb-tabs, .tb-body { position: relative; z-index: 1; }
    .tb-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 9px 10px;
      background: linear-gradient(90deg, rgba(5,20,12,.96), rgba(24,18,6,.96));
      border-bottom: 1px solid rgba(250,204,21,.40);
      overflow: hidden;
    }
    .tb-head:after {
      content: '';
      position: absolute;
      left: 0;
      bottom: 0;
      width: 100%;
      height: 2px;
      background: linear-gradient(90deg, transparent, #22c55e, #facc15, transparent);
      animation: tbScanLine 2.2s linear infinite;
    }
    .tb-title {
      font-size: 15px;
      font-weight: 1000;
      color: #fef08a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-shadow: 0 0 10px rgba(250,204,21,.35);
    }
    .tb-subtitle {
      display: block;
      color: #86efac;
      font-size: 10px;
      margin-top: 1px;
      letter-spacing: .7px;
      text-transform: uppercase;
    }
    .tb-close, .tb-btn {
      border: 1px solid rgba(250,204,21,.50);
      background: linear-gradient(180deg, rgba(22,30,18,.96), rgba(10,14,10,.96));
      color: #fef9c3;
      border-radius: 11px;
      padding: 8px 10px;
      font-weight: 900;
      cursor: pointer;
      touch-action: manipulation;
      box-shadow: inset 0 0 14px rgba(34,197,94,.07), 0 4px 12px rgba(0,0,0,.35);
    }
    .tb-close { min-width: 38px; }
    .tb-tabs {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding: 7px 8px;
      scrollbar-width: thin;
      position: sticky;
      top: 0;
      border-bottom: 1px solid rgba(34,197,94,.15);
      background: rgba(4,8,6,.82);
    }
    .tb-tab {
      flex: 0 0 auto;
      border: 1px solid rgba(74,222,128,.18);
      background: rgba(15,23,18,.88);
      color: #bbf7d0;
      border-radius: 999px;
      padding: 7px 9px;
      font-size: 11px;
      font-weight: 900;
      cursor: pointer;
      touch-action: manipulation;
    }
    .tb-tab.active {
      border-color: rgba(250,204,21,.80);
      background: linear-gradient(135deg, rgba(20,83,45,.88), rgba(113,63,18,.72));
      color: #fef08a;
      box-shadow: 0 0 14px rgba(34,197,94,.18);
    }
    .tb-body {
      overflow: auto;
      -webkit-overflow-scrolling: touch;
      overscroll-behavior: contain;
      padding: 10px;
      flex: 1;
    }
    .tb-card {
      position: relative;
      border: 1px solid rgba(74,222,128,.18);
      background: linear-gradient(180deg, rgba(15,23,18,.88), rgba(9,9,11,.90));
      border-radius: 16px;
      padding: 11px;
      margin-bottom: 10px;
      box-shadow: inset 0 0 18px rgba(34,197,94,.04), 0 8px 18px rgba(0,0,0,.32);
      overflow: hidden;
      animation: tbFadeUp .18s ease-out;
    }
    .tb-card:before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(34,197,94,.6), rgba(250,204,21,.45), transparent);
    }
    .tb-card h3 {
      margin: 0 0 8px;
      color: #fef08a;
      font-size: 14px;
      text-shadow: 0 0 10px rgba(250,204,21,.18);
    }
    .tb-muted { color: #c9d7bd; font-size: 12px; line-height: 1.38; }
    .tb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .tb-pill {
      display: inline-block;
      padding: 3px 7px;
      border-radius: 999px;
      background: rgba(20,83,45,.42);
      border: 1px solid rgba(74,222,128,.30);
      color: #bbf7d0;
      font-size: 11px;
      font-weight: 900;
    }
    .tb-ai-pill {
      background: rgba(113,63,18,.45);
      border-color: rgba(250,204,21,.55);
      color: #fef08a;
    }
    .tb-input, .tb-select {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid rgba(74,222,128,.28);
      background: rgba(3,7,5,.96);
      color: #f7fee7;
      border-radius: 11px;
      padding: 10px;
      margin: 6px 0;
      outline: none;
    }
    .tb-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    .tb-danger { border-color: rgba(248,113,113,.55); color: #fecaca; }
    .tb-ok { color: #86efac; }
    .tb-warn { color: #fde68a; }
    .tb-err { color: #fecaca; }
    .tb-link { color: #fef08a; text-decoration: underline; font-weight: 900; }
    .tb-scan {
      position: relative;
      overflow: hidden;
      border-radius: 12px;
      border: 1px solid rgba(74,222,128,.18);
      background: rgba(0,0,0,.22);
      padding: 8px;
      margin-top: 8px;
      color: #86efac;
      font-size: 12px;
      font-weight: 800;
    }
    .tb-scan:after {
      content: '';
      position: absolute;
      top: 0;
      bottom: 0;
      width: 42%;
      background: linear-gradient(90deg, transparent, rgba(34,197,94,.18), transparent);
      animation: tbShimmer 1.8s linear infinite;
    }
    .tb-price { font-size: 18px; font-weight: 1000; color: #fef08a; }
    .tb-score { font-size: 20px; font-weight: 1000; color: #86efac; text-shadow: 0 0 10px rgba(34,197,94,.35); }
    .tb-mini-row { display:flex; justify-content:space-between; gap:8px; border-top:1px solid rgba(74,222,128,.12); padding:7px 0; font-size:12px; }
    .tb-mini-row:first-child { border-top:0; }
    .tb-market-row { border-top:1px solid rgba(74,222,128,.12); padding:9px 0; }
    .tb-market-title { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; font-size:13px; }
    .tb-signal-buy { color:#86efac; text-shadow:0 0 10px rgba(34,197,94,.35); }
    .tb-signal-sell { color:#fef08a; text-shadow:0 0 10px rgba(250,204,21,.25); }
    .tb-signal-hold { color:#c9d7bd; }

    .tb-dashboard-card { border-color: rgba(250,204,21,.60); background: linear-gradient(180deg, rgba(20,83,45,.28), rgba(8,13,9,.92)); }
    .tb-hero { display:flex; gap:10px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
    .tb-hero-main { flex:1 1 170px; }
    .tb-hero-label { color:#fef08a; font-size:11px; font-weight:1000; letter-spacing:.8px; text-transform:uppercase; }
    .tb-hero-title { color:#dcfce7; font-size:20px; font-weight:1000; line-height:1.1; margin-top:4px; text-shadow:0 0 12px rgba(34,197,94,.24); }
    .tb-hero-detail { color:#bbf7d0; font-size:12px; margin-top:5px; }
    .tb-status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; box-shadow:0 0 10px rgba(34,197,94,.8); margin-right:5px; }
    .tb-kpi-grid { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:8px; }
    .tb-kpi { border:1px solid rgba(74,222,128,.18); border-radius:14px; padding:9px; background:rgba(2,6,4,.42); min-height:68px; }
    .tb-kpi small { display:block; color:#86efac; font-weight:900; font-size:10px; text-transform:uppercase; letter-spacing:.6px; }
    .tb-kpi strong { display:block; color:#fef9c3; font-size:15px; margin-top:5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tb-kpi span { display:block; color:#c9d7bd; font-size:11px; margin-top:3px; }
    .tb-compact-list { display:grid; gap:7px; }
    .tb-compact-row { border:1px solid rgba(74,222,128,.12); border-radius:12px; padding:8px; background:rgba(0,0,0,.18); font-size:12px; }
    .tb-refresh-line { font-size:10px; color:#86efac; margin-top:7px; }
    .tb-btn:active, #tb-icon:active { transform: scale(.97); }
    .tb-tab:active { transform:none; }
    .tb-note-dot { display:inline-flex; align-items:center; justify-content:center; min-width:18px; height:18px; border-radius:999px; padding:0 5px; background:#dc2626; color:#fff; font-size:11px; margin-left:5px; box-shadow:0 0 14px rgba(220,38,38,.6); }
    .tb-body { scroll-behavior:smooth; }
    .tb-sticky-actions { position:sticky; bottom:0; padding:8px 0 0; background:linear-gradient(180deg, transparent, rgba(5,10,7,.96) 42%); }

    @media (min-width: 820px) { #tb-panel { left: auto; width: 760px; right: 18px; } }
    @media (max-width: 420px) {
      #tb-panel { top: 46px; bottom: 8px; left: 6px; right: 6px; border-radius: 15px; }
      .tb-grid { grid-template-columns: 1fr; }
      .tb-title { font-size: 14px; }
      #tb-icon { bottom: 72px; left: 14px; width: 34px; height: 34px; font-size: 15px; }
    }
  `);

  function token() {
    if (authToken) return authToken;
    let gmTok = '';
    let lsTok = '';
    try { gmTok = GM_getValue(K_TOKEN, '') || ''; } catch (_) {}
    try { lsTok = window.localStorage.getItem(K_TOKEN_BACKUP) || ''; } catch (_) {}
    authToken = gmTok || lsTok || '';
    return authToken;
  }

  function setToken(v) {
    authToken = v || '';
    try {
      if (authToken) GM_setValue(K_TOKEN, authToken);
      else GM_deleteValue(K_TOKEN);
    } catch (_) {}
    try {
      if (authToken) window.localStorage.setItem(K_TOKEN_BACKUP, authToken);
      else window.localStorage.removeItem(K_TOKEN_BACKUP);
    } catch (_) {}
  }


  function savedApiKey() {
    try { return GM_getValue(K_APIKEY, '') || window.localStorage.getItem(K_APIKEY) || ''; } catch (_) {}
    try { return window.localStorage.getItem(K_APIKEY) || ''; } catch (_) {}
    return '';
  }

  function setSavedApiKey(v) {
    const key = v || '';
    try { if (key) GM_setValue(K_APIKEY, key); else GM_deleteValue(K_APIKEY); } catch (_) {}
    try { if (key) window.localStorage.setItem(K_APIKEY, key); else window.localStorage.removeItem(K_APIKEY); } catch (_) {}
  }

  async function restoreLogin(reason = 'restoring') {
    if (restoreBusy) return false;
    const key = savedApiKey();
    if (!key) return false;
    restoreBusy = true;
    try {
      const data = await api('/api/login', { method: 'POST', body: JSON.stringify({ api_key: key }), noAuth: true });
      if (data && data.token) {
        setToken(data.token);
      setSavedApiKey(key);
        try { await api('/api/auto/start', { method: 'POST', body: '{}' }); } catch (_) {}
        return true;
      }
    } catch (_) {
      return false;
    } finally {
      restoreBusy = false;
    }
    return false;
  }

  function api(path, options = {}) {
    const method = options.method || 'GET';
    const headers = Object.assign({ 'Content-Type': 'application/json', 'Accept': 'application/json' }, options.headers || {});
    if (!options.noAuth && token()) headers.Authorization = 'Bearer ' + token();
    const body = options.body || null;

    // TornPDA can be picky with normal cross-site fetch(). GM_xmlhttpRequest is smoother for userscripts.
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: API_BASE + path,
        headers,
        data: body,
        timeout: 45000,
        onload: (res) => {
          let data;
          try { data = JSON.parse(res.responseText || '{}'); }
          catch (_) { data = { ok: false, error: 'Bad server response from Render.' }; }
          if (res.status < 200 || res.status >= 300 || !data.ok) {
            const err = new Error((data && data.error) || ('Server returned HTTP ' + res.status));
            err.status = res.status;
            err.payload = data;
            reject(err);
            return;
          }
          resolve(data);
        },
        ontimeout: () => reject(new Error('Render timed out waking up. Wait 30 seconds and try again.')),
        onerror: () => reject(new Error('Could not reach Render backend: ' + API_BASE.replace('https://', '') + '. Check Render deploy/logs.'))
      });
    });
  }

  function el(id) { return document.getElementById(id); }

  function loadIconPosition(icon) {
    let pos = null;
    try { pos = JSON.parse(GM_getValue(K_ICON_POS, '') || 'null'); } catch (_) {}
    if (!pos) {
      try { pos = JSON.parse(window.localStorage.getItem(K_ICON_POS) || 'null'); } catch (_) {}
    }
    if (pos && Number.isFinite(pos.left) && Number.isFinite(pos.top)) {
      const maxLeft = Math.max(4, window.innerWidth - 42);
      const maxTop = Math.max(4, window.innerHeight - 42);
      icon.style.left = Math.min(Math.max(4, pos.left), maxLeft) + 'px';
      icon.style.top = Math.min(Math.max(4, pos.top), maxTop) + 'px';
      icon.style.bottom = 'auto';
      icon.style.right = 'auto';
    }
  }

  function saveIconPosition(left, top) {
    const pos = { left: Math.round(left), top: Math.round(top) };
    try { GM_setValue(K_ICON_POS, JSON.stringify(pos)); } catch (_) {}
    try { window.localStorage.setItem(K_ICON_POS, JSON.stringify(pos)); } catch (_) {}
  }

  function setupFloatingIcon(icon) {
    loadIconPosition(icon);
    let startX = 0, startY = 0, startLeft = 0, startTop = 0, moved = false, dragging = false, lastTouch = 0;
    const begin = (x, y, e) => {
      const r = icon.getBoundingClientRect();
      startX = x; startY = y; startLeft = r.left; startTop = r.top;
      moved = false; dragging = true;
      icon.style.bottom = 'auto'; icon.style.right = 'auto';
      if (e) { e.preventDefault(); e.stopPropagation(); }
    };
    const move = (x, y, e) => {
      if (!dragging) return;
      const dx = x - startX, dy = y - startY;
      if (Math.abs(dx) > 5 || Math.abs(dy) > 5) moved = true;
      if (!moved) return;
      const maxLeft = Math.max(4, window.innerWidth - icon.offsetWidth - 4);
      const maxTop = Math.max(4, window.innerHeight - icon.offsetHeight - 4);
      const left = Math.min(Math.max(4, startLeft + dx), maxLeft);
      const top = Math.min(Math.max(4, startTop + dy), maxTop);
      icon.style.left = left + 'px';
      icon.style.top = top + 'px';
      if (e) { e.preventDefault(); e.stopPropagation(); }
    };
    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      const r = icon.getBoundingClientRect();
      saveIconPosition(r.left, r.top);
      if (e) { e.preventDefault(); e.stopPropagation(); }
      if (!moved) togglePanel();
      lastTouch = Date.now();
      setTimeout(() => { moved = false; }, 0);
    };
    icon.addEventListener('touchstart', e => { const t = e.touches && e.touches[0]; if (t) begin(t.clientX, t.clientY, e); }, { passive:false });
    icon.addEventListener('touchmove', e => { const t = e.touches && e.touches[0]; if (t) move(t.clientX, t.clientY, e); }, { passive:false });
    icon.addEventListener('touchend', end, { passive:false });
    icon.addEventListener('mousedown', e => begin(e.clientX, e.clientY, e));
    document.addEventListener('mousemove', e => move(e.clientX, e.clientY, e));
    document.addEventListener('mouseup', end);
    icon.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      if (Date.now() - lastTouch < 600) return;
      togglePanel();
    });
  }

  function mount() {
    if (mounted || el('tb-icon')) return;
    mounted = true;

    const icon = document.createElement('div');
    icon.id = 'tb-icon';
    icon.innerHTML = 'AI🫰';
    document.body.appendChild(icon);

    const panel = document.createElement('div');
    panel.id = 'tb-panel';
    panel.innerHTML = `
      <div class="tb-head">
        <div class="tb-title">AI🫰 Fries91 Torn Brain <span class="tb-pill tb-ai-pill">Step 8.11 Fix</span><span class="tb-subtitle">Self-Learning Profit Engine</span></div>
        <button class="tb-close" id="tb-close">✕</button>
      </div>
      <div class="tb-tabs" id="tb-tabs"></div>
      <div class="tb-body" id="tb-body"></div>
    `;
    document.body.appendChild(panel);

    setupFloatingIcon(icon);
    el('tb-close').addEventListener('click', closePanel);
    el('tb-close').addEventListener('touchend', function (e) { e.preventDefault(); closePanel(); }, { passive: false });

    renderTabs();
    render();

    if (GM_getValue(K_OPEN, false)) openPanel();
  }

  function openPanel() {
    el('tb-panel')?.classList.add('tb-show');
    GM_setValue(K_OPEN, true);
    refreshState();
  }
  function closePanel() {
    el('tb-panel')?.classList.remove('tb-show');
    GM_setValue(K_OPEN, false);
  }
  function togglePanel() {
    if (el('tb-panel')?.classList.contains('tb-show')) closePanel(); else openPanel();
  }

  function selectTab(name) {
    if (!name || name === activeTab) return;
    activeTab = name;
    GM_setValue('fries91_torn_brain_tab_v1', activeTab);
    renderTabs();
    render();
  }

  function renderTabs() {
    const tabs = el('tb-tabs');
    if (!tabs) return;
    const unread = Number(state?.unread_alerts || 0);
    tabs.innerHTML = TABS.map(t => {
      const label = t === 'Notifications' && unread > 0 ? `${t}<span class="tb-note-dot">${unread > 99 ? '99+' : unread}</span>` : t;
      return `<button class="tb-tab ${t === activeTab ? 'active' : ''}" data-tab="${t}">${label}</button>`;
    }).join('');

    tabs.ontouchstart = (e) => {
      const t = e.touches && e.touches[0];
      tabsTouchStartX = t ? t.clientX : 0;
      tabsTouchStartY = t ? t.clientY : 0;
      tabsMoved = false;
    };
    tabs.ontouchmove = (e) => {
      const t = e.touches && e.touches[0];
      if (!t) return;
      if (Math.abs(t.clientX - tabsTouchStartX) > 10 || Math.abs(t.clientY - tabsTouchStartY) > 10) tabsMoved = true;
    };
    tabs.querySelectorAll('.tb-tab').forEach(btn => {
      btn.onclick = (e) => {
        if (tabsMoved) { e.preventDefault(); e.stopPropagation(); tabsMoved = false; return; }
        selectTab(btn.dataset.tab);
      };
      btn.ontouchend = (e) => {
        if (tabsMoved) { tabsMoved = false; return; }
        e.preventDefault();
        selectTab(btn.dataset.tab);
      };
    });
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>'"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[c]));
  }

  function comingSoon(name, step) {
    return `
      <div class="tb-card">
        <h3>${escapeHtml(name)}</h3>
        <div class="tb-muted">Coming in ${escapeHtml(step)}. The tab is wired now so we can add this feature cleanly without rebuilding the whole app.</div>
      </div>
    `;
  }

  function renderLogin() {
    return `
      <div class="tb-card">
        <h3>Login</h3>
        <div class="tb-muted">Enter your Torn API key. Limited keys are supported, but they must allow basic user/profile reads. This app only reads Torn data. It does not auto-buy, auto-sell, auto-attack, or change your Torn account.</div>
        <input class="tb-input" id="tb-api-key" type="password" placeholder="Paste Torn API key" autocomplete="off" />
        <div class="tb-actions">
          <button class="tb-btn" id="tb-login">Connect</button>
        </div>
        <div class="tb-muted" id="tb-login-msg"></div>
      </div>
      <div class="tb-card">
        <h3>Step 8.11 Includes</h3>
        <div class="tb-muted">Quiet notification mode, Notifications tab count only, smaller floating movable AI🫰 icon, item price fallback fix, auto re-login, swipe-safe tabs, PostgreSQL storage, backend-first dashboard, Stock Brain, Item Market, Points Watcher, Travel Profit, Enemy Sleep, and Accuracy Learning.</div>
        <div class="tb-scan">Stock + Item + Points + Travel + Enemy watcher active · backend scanning online</div>
      </div>
    `;
  }

  async function renderOverviewLive() {
    const body = el('tb-body');
    if (!body) return;
    if (!state) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card tb-dashboard-card"><h3>AI🫰 Dashboard</h3><div class="tb-scan">Loading saved backend results...</div></div>`;
    try {
      dashboard = await api('/api/dashboard');
      const u = dashboard.user || state.user || {};
      const auto = dashboard.auto_scan || state.auto_scan || {};
      const best = dashboard.best_move || {};
      const stock = dashboard.stock_pick || {};
      const points = dashboard.points || {};
      const travel = dashboard.travel_best || {};
      const enemyReport = dashboard.enemy?.report || {};
      const enemySession = dashboard.enemy?.session || {};
      const items = dashboard.items || [];
      const alerts = dashboard.latest_alerts || [];
      body.innerHTML = `
        <div class="tb-card tb-dashboard-card">
          <div class="tb-hero">
            <div class="tb-hero-main">
              <div class="tb-hero-label"><span class="tb-status-dot"></span>Best Move Right Now</div>
              <div class="tb-hero-title">${escapeHtml(best.label || 'Learning')}</div>
              <div class="tb-hero-detail">${escapeHtml(best.detail || 'Waiting for more backend snapshots')}</div>
            </div>
            <span class="tb-pill ${signalClass(best.signal)}">${escapeHtml(best.signal || 'WATCH')}</span>
          </div>
          <div class="tb-refresh-line">Backend-first display · last scan ${escapeHtml(shortTime(auto.last_scan_at))} · next ${escapeHtml(shortTime(auto.next_scan_at))}</div>
        </div>
        <div class="tb-kpi-grid">
          ${kpi('Stock Pick', stock.acronym || 'Learning', stock.confidence ? 'Confidence ' + Number(stock.confidence).toFixed(0) + '%' : 'Need snapshots')}
          ${kpi('Points', points.signal || 'WAITING', points.latest?.lowest_price ? fmtMoney(points.latest.lowest_price) : 'Need scan')}
          ${kpi('Travel', travel.signal || 'WAITING', travel.country ? travel.country + ' · ' + travel.item_name : 'Need route data')}
          ${kpi('Enemy', enemySession.enemy_faction_name || 'Not tracking', enemyReport.best_attack_window || 'Start during war')}
        </div>
        <div class="tb-card">
          <h3>Quick Market Watch</h3>
          <div class="tb-compact-list">
            ${(items || []).slice(0,4).map(it => `<div class="tb-compact-row"><b>${escapeHtml(it.name)}</b> <span class="${signalClass(it.signal)}">${escapeHtml(it.signal)}</span><br><span class="tb-muted">Current ${it.latest?.lowest_price ? fmtMoney(it.latest.lowest_price) : 'waiting'} · buy zone ${it.buy_zone ? fmtMoney(it.buy_zone) : 'auto'}</span></div>`).join('') || '<div class="tb-muted">Add watched items in Item Market to fill this section.</div>'}
          </div>
        </div>
        <div class="tb-card">
          <h3>Notifications</h3>
          <div class="tb-muted">Quiet mode is on. Alerts stay inside the Notifications tab only. Open the overlay and use the Notifications tab number to see unread alerts.</div>
          <div class="tb-scan">Unread notifications: ${escapeHtml(dashboard.unread_alerts || 0)}</div>
        </div>
        <div class="tb-card">
          <h3>Status</h3>
          <div class="tb-grid">
            <div><span class="tb-pill">User</span><br>${escapeHtml(u.name)} [${escapeHtml(u.torn_id)}]</div>
            <div><span class="tb-pill">Notifications</span><br>${escapeHtml(dashboard.unread_alerts || 0)} unread</div>
            <div><span class="tb-pill">Auto Scanner</span><br>${auto.enabled ? 'Running' : 'Off'} · ${escapeHtml(auto.scans_completed || 0)} scans</div>
            <div><span class="tb-pill">Server</span><br>${escapeHtml(shortTime(dashboard.server_time))}</div>
          </div>
          <div class="tb-sticky-actions"><button class="tb-btn" id="tb-overview-refresh">Refresh Dashboard</button></div>
        </div>
      `;
      el('tb-overview-refresh')?.addEventListener('click', () => renderOverviewLive());
      await refreshState(true);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Overview</h3><div class="tb-err">${escapeHtml(e.message)}</div><div class="tb-muted">The backend may be waking up. Try refresh again after Render responds.</div></div>`;
    }
  }

  function renderOverview() {
    if (!state) return renderLogin();
    const u = state.user || {};
    const auto = state.auto_scan || {};
    return `
      <div class="tb-card tb-dashboard-card">
        <h3>Overview</h3>
        <div class="tb-grid">
          <div><span class="tb-pill">User</span><br>${escapeHtml(u.name)} [${escapeHtml(u.torn_id)}]</div>
          <div><span class="tb-pill">Notifications</span><br>${escapeHtml(state.unread_alerts || 0)} unread</div>
          <div><span class="tb-pill">Auto Scanner</span><br>${auto.enabled ? 'Running' : 'Off'} · ${escapeHtml(auto.scans_completed || 0)} scans</div>
          <div><span class="tb-pill">Last Scan</span><br>${escapeHtml(shortTime(auto.last_scan_at))}</div>
        </div>
        <div class="tb-scan">AI learning engine online · backend-first Step 8 smooth mode</div>
      </div>`;
  }


  async function renderNotifications() {
    const body = el('tb-body');
    if (!token() && savedApiKey()) {
      body.innerHTML = `<div class="tb-card"><h3>Restoring login...</h3><div class="tb-scan">Using saved key on this device. No need to paste it again.</div></div>`;
      const ok = await restoreLogin('notifications');
      if (ok) return renderNotifications();
    }
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Notifications</h3><div class="tb-muted">Loading notifications...</div></div>`;
    try {
      const data = await api('/api/alerts');
      const cards = (data.alerts || []).map(a => `
        <div class="tb-card ${a.is_read ? '' : 'tb-unread'}">
          <h3>${escapeHtml(a.title)} ${a.is_read ? '' : '<span class="tb-pill">new</span>'}</h3>
          <div class="tb-muted">${escapeHtml(a.created_at)} · ${escapeHtml(a.alert_type)}</div>
          <p>${escapeHtml(a.body)}</p>
          <div class="tb-actions">
            ${a.link ? `<a class="tb-link tb-open-alert" data-id="${escapeHtml(a.id)}" href="${escapeHtml(a.link)}">Open + mark read</a>` : ''}
            ${a.is_read ? '' : `<button class="tb-btn tb-mark-one" data-id="${escapeHtml(a.id)}">Mark read</button>`}
          </div>
        </div>
      `).join('') || `<div class="tb-card"><h3>No notifications yet</h3><div class="tb-muted">Buy zones, stock changes, points moves, travel chances, and enemy windows will show here.</div></div>`;
      body.innerHTML = `
        <div class="tb-card">
          <h3>Notifications</h3>
          <div class="tb-muted">Unread: ${escapeHtml(data.unread || state?.unread_alerts || 0)}. Quiet mode keeps alerts inside this tab only.</div>
          <div class="tb-actions">
            <button class="tb-btn" id="tb-mark-read">Mark all read</button>
            <button class="tb-btn" id="tb-refresh-notes">Refresh</button>
            <button class="tb-btn" id="tb-test-alert">Test notification</button>
          </div>
        </div>
        ${cards}
      `;
      el('tb-mark-read')?.addEventListener('click', async () => { await api('/api/alerts/read', { method: 'POST', body: '{}' }); await refreshState(true); await renderNotifications(); });
      el('tb-refresh-notes')?.addEventListener('click', async () => { await refreshState(true); await renderNotifications(); });
      el('tb-test-alert')?.addEventListener('click', async () => { await api('/api/dev/test-alert', { method: 'POST', body: '{}' }); await refreshState(true); await renderNotifications(); });
      document.querySelectorAll('.tb-mark-one').forEach(btn => btn.addEventListener('click', async () => {
        await api('/api/alerts/read', { method: 'POST', body: JSON.stringify({ id: btn.dataset.id }) });
        await refreshState(true);
        await renderNotifications();
      }));
      document.querySelectorAll('.tb-open-alert').forEach(link => link.addEventListener('click', async (e) => {
        e.preventDefault();
        const url = link.getAttribute('href');
        const id = link.dataset.id;
        try {
          if (id) await api('/api/alerts/read', { method: 'POST', body: JSON.stringify({ id }) });
          await refreshState(true);
        } catch (_) {}
        if (url) window.location.href = url;
      }));
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Notifications</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function renderSettings() {
    const body = el('tb-body');
    if (!token() && savedApiKey()) { body.innerHTML = `<div class="tb-card"><h3>Restoring login...</h3><div class="tb-scan">Using saved key on this device. No need to paste it again.</div></div>`; const ok = await restoreLogin('settings'); if (ok) return renderSettings(); }
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Settings</h3><div class="tb-muted">Loading settings...</div></div>`;
    try {
      const data = await api('/api/settings');
      const s = data.settings || {};
      const u = data.user || {};
      body.innerHTML = `
        <div class="tb-card">
          <h3>Account</h3>
          <div class="tb-muted">Logged in as ${escapeHtml(u.name)} [${escapeHtml(u.torn_id)}]</div>
          <div class="tb-muted">API key: ${escapeHtml(u.masked_key || 'saved')}</div>
          <div class="tb-actions"><button class="tb-btn tb-danger" id="tb-logout">Logout</button></div>
        </div>
        <div class="tb-card">
          <h3>Scan Settings</h3>
          <label class="tb-muted">Scan interval minutes</label>
          <input class="tb-input" id="set-scan" value="${escapeHtml(s.scan_interval_minutes)}">
          <label class="tb-muted">Stock pick change score gap</label>
          <input class="tb-input" id="set-gap" value="${escapeHtml(s.stock_pick_change_score_gap)}">
          <label class="tb-muted">Enemy tracking window hours</label>
          <input class="tb-input" id="set-enemy" value="${escapeHtml(s.enemy_tracking_window_hours)}">
          <label class="tb-muted">Enemy window alerts enabled</label>
          <select class="tb-select" id="set-enemy-alerts">
            <option value="true" ${s.enemy_alerts_enabled !== 'false' ? 'selected' : ''}>true</option>
            <option value="false" ${s.enemy_alerts_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <label class="tb-muted">Auto backend scanning</label>
          <select class="tb-select" id="set-auto">
            <option value="true" ${s.auto_scan_enabled === 'true' ? 'selected' : ''}>true</option>
            <option value="false" ${s.auto_scan_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <label class="tb-muted">Notifications enabled</label>
          <select class="tb-select" id="set-alerts">
            <option value="true" ${s.alerts_enabled === 'true' ? 'selected' : ''}>true</option>
            <option value="false" ${s.alerts_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <label class="tb-muted">Share market learning data</label>
          <select class="tb-select" id="set-share">
            <option value="true" ${s.share_market_learning !== 'false' ? 'selected' : ''}>true</option>
            <option value="false" ${s.share_market_learning === 'false' ? 'selected' : ''}>false</option>
          </select>
          <label class="tb-muted">Item alerts enabled</label>
          <select class="tb-select" id="set-item-alerts">
            <option value="true" ${s.item_alerts_enabled !== 'false' ? 'selected' : ''}>true</option>
            <option value="false" ${s.item_alerts_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <label class="tb-muted">Default item buy discount %</label>
          <input class="tb-input" id="set-item-buy-discount" value="${escapeHtml(s.item_default_buy_discount_pct || '3')}">
          <label class="tb-muted">Default item sell markup %</label>
          <input class="tb-input" id="set-item-sell-markup" value="${escapeHtml(s.item_default_sell_markup_pct || '6')}">
          <label class="tb-muted">Points alerts enabled</label>
          <select class="tb-select" id="set-points-alerts">
            <option value="true" ${s.points_alerts_enabled !== 'false' ? 'selected' : ''}>true</option>
            <option value="false" ${s.points_alerts_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <div class="tb-grid">
            <input class="tb-input" id="set-points-buy-zone" placeholder="Points buy zone optional" value="${escapeHtml(s.points_buy_zone || '')}">
            <input class="tb-input" id="set-points-sell-zone" placeholder="Points sell zone optional" value="${escapeHtml(s.points_sell_zone || '')}">
          </div>
          <div class="tb-grid">
            <input class="tb-input" id="set-points-buy-discount" placeholder="Auto buy discount %" value="${escapeHtml(s.points_default_buy_discount_pct || '2')}">
            <input class="tb-input" id="set-points-sell-markup" placeholder="Auto sell markup %" value="${escapeHtml(s.points_default_sell_markup_pct || '4')}">
          </div>
          <label class="tb-muted">Travel alerts enabled</label>
          <select class="tb-select" id="set-travel-alerts">
            <option value="true" ${s.travel_alerts_enabled !== 'false' ? 'selected' : ''}>true</option>
            <option value="false" ${s.travel_alerts_enabled === 'false' ? 'selected' : ''}>false</option>
          </select>
          <div class="tb-grid">
            <input class="tb-input" id="set-travel-min-profit" placeholder="Travel minimum profit" value="${escapeHtml(s.travel_min_profit || '50000')}">
            <input class="tb-input" id="set-travel-min-chance" placeholder="Min arrival chance %" value="${escapeHtml(s.travel_min_arrival_chance || '45')}">
          </div>
          <input class="tb-input" id="set-travel-items" placeholder="Items carried per trip" value="${escapeHtml(s.travel_items_per_trip || '29')}">
          <div class="tb-actions"><button class="tb-btn" id="tb-save-settings">Save Settings</button></div>
          <div class="tb-muted" id="tb-settings-msg"></div>
        </div>
        <div class="tb-card">
          <h3>API Use & Torn Compliance</h3>
          <div class="tb-muted">
            This app only uses Torn's read-only API. It stores your key on your own Render app so it can read data for analysis. It does not request your Torn password, does not auto-buy/sell, and does not perform in-game actions.
          </div>
        </div>
      `;
      el('tb-logout')?.addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (_) {} setToken(''); setSavedApiKey(''); state = null; renderTabs(); render(); });
      el('tb-save-settings')?.addEventListener('click', saveSettings);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Settings</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function saveSettings() {
    const msg = el('tb-settings-msg');
    msg.textContent = 'Saving...';
    try {
      await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          scan_interval_minutes: el('set-scan')?.value || '15',
          stock_pick_change_score_gap: el('set-gap')?.value || '15',
          enemy_tracking_window_hours: el('set-enemy')?.value || '72',
          enemy_alerts_enabled: el('set-enemy-alerts')?.value || 'true',
          alerts_enabled: el('set-alerts')?.value || 'true',
          share_market_learning: el('set-share')?.value || 'true',
          item_alerts_enabled: el('set-item-alerts')?.value || 'true',
          item_default_buy_discount_pct: el('set-item-buy-discount')?.value || '3',
          item_default_sell_markup_pct: el('set-item-sell-markup')?.value || '6',
          points_alerts_enabled: el('set-points-alerts')?.value || 'true',
          points_buy_zone: el('set-points-buy-zone')?.value || '',
          points_sell_zone: el('set-points-sell-zone')?.value || '',
          points_default_buy_discount_pct: el('set-points-buy-discount')?.value || '2',
          points_default_sell_markup_pct: el('set-points-sell-markup')?.value || '4',
          auto_scan_enabled: el('set-auto')?.value || 'true'
        })
      });
      msg.innerHTML = '<span class="tb-ok">Saved.</span>';
    } catch (e) {
      msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }


  function fmtMoney(n) {
    const v = Number(n || 0);
    if (!isFinite(v)) return '$0';
    return '$' + Math.round(v).toLocaleString();
  }
  function fmtPct(n) {
    const v = Number(n || 0);
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function shortTime(s) {
    if (!s) return 'waiting';
    return String(s).replace('T', ' ').replace('+00:00', '');
  }
  function signalClass(sig) {
    const s = String(sig || '').toUpperCase();
    if (['BUY','GO','PICK'].includes(s)) return 'tb-signal-buy';
    if (['SELL','RISKY','WAIT'].includes(s)) return 'tb-signal-sell';
    return 'tb-signal-hold';
  }
  function kpi(title, value, detail) {
    return `<div class="tb-kpi"><small>${escapeHtml(title)}</small><strong>${escapeHtml(value || '—')}</strong><span>${escapeHtml(detail || '')}</span></div>`;
  }

  async function renderStockBrain() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Stock Brain</h3><div class="tb-scan">Loading shared stock intelligence...</div></div>`;
    try {
      const data = await api('/api/stocks/brain');
      const p = data.pick;
      const ranked = data.ranked || [];
      body.innerHTML = `
        <div class="tb-card">
          <h3>Stock Brain <span class="tb-pill tb-ai-pill">Global Learning</span></h3>
          <div class="tb-muted">This uses shared stock snapshots. The backend auto scanner starts after login and keeps collecting data without making TornPDA do the heavy work.</div>
          <div class="tb-actions">
            <button class="tb-btn" id="tb-auto-start">Start Auto Watcher</button>
            <button class="tb-btn" id="tb-scan-stocks">Scan Now</button>
            <button class="tb-btn" id="tb-stock-history">Prediction History</button>
          </div>
          <div class="tb-muted" id="tb-stock-msg">Snapshots stored: ${escapeHtml(data.snapshot_count || 0)}</div>
        </div>
        ${p ? `
        <div class="tb-card">
          <h3>Today's 24h Pick</h3>
          <div class="tb-grid">
            <div><span class="tb-pill">Stock</span><br><span class="tb-price">${escapeHtml(p.acronym)}</span><br><span class="tb-muted">${escapeHtml(p.name)}</span></div>
            <div><span class="tb-pill">Pick Price</span><br><span class="tb-price">${fmtMoney(p.pick_price)}</span></div>
            <div><span class="tb-pill">Score</span><br><span class="tb-score">${escapeHtml(p.score)}</span></div>
            <div><span class="tb-pill">Confidence</span><br><span class="tb-score">${escapeHtml(p.confidence)}%</span></div>
            <div><span class="tb-pill">Expected 24h</span><br><span class="tb-score">${fmtPct(Number(p.expected_24h_pct || 0))}</span></div>
            <div><span class="tb-pill">Status</span><br>${escapeHtml(p.status)}</div>
          </div>
          <p class="tb-muted"><b>Reason:</b> ${escapeHtml(p.reason)}</p>
          <div class="tb-muted">Created: ${escapeHtml(p.created_at)}</div>
          <a class="tb-link" href="https://www.torn.com/page.php?sid=stocks">Open Torn Stocks</a>
        </div>` : `
        <div class="tb-card">
          <h3>No Pick Yet</h3>
          <div class="tb-muted">Press Scan Stocks Now. After the first scan, the app will choose one 24h stock pick. More scans make it smarter.</div>
        </div>`}
        <div class="tb-card">
          <h3>Top Ranked Stocks</h3>
          ${ranked.length ? ranked.map(r => `
            <div class="tb-mini-row">
              <span><b>${escapeHtml(r.acronym)}</b> <span class="tb-muted">${escapeHtml(r.name)}</span></span>
              <span>${fmtMoney(r.current_price)} · score ${escapeHtml(r.score)} · ${escapeHtml(r.confidence)}%</span>
            </div>`).join('') : `<div class="tb-muted">No ranked data yet. Run a scan first.</div>`}
        </div>
        <div class="tb-card">
          <h3>Drastic Change Rule</h3>
          <div class="tb-muted">The pick only changes if a new stock beats the current one by your score gap setting, or the old pick becomes weak. That stops tiny market wiggles from constantly changing the call.</div>
        </div>
      `;
      el('tb-auto-start')?.addEventListener('click', startAutoWatcher);
      el('tb-scan-stocks')?.addEventListener('click', scanStocksNow);
      el('tb-stock-history')?.addEventListener('click', renderStockHistory);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Stock Brain</h3><div class="tb-err">${escapeHtml(e.message)}</div><div class="tb-actions"><button class="tb-btn" id="tb-scan-stocks">Try Scan</button></div></div>`;
      el('tb-scan-stocks')?.addEventListener('click', scanStocksNow);
    }
  }

  async function startAutoWatcher() {
    const msg = el('tb-stock-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Starting backend watcher...</span>';
    try {
      await api('/api/auto/start', { method: 'POST', body: '{}' });
      await refreshState();
      if (msg) msg.innerHTML = '<span class="tb-ok">Backend watcher started. It will scan server-side.</span>';
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function scanStocksNow() {
    const msg = el('tb-stock-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Scanning Torn stocks and updating the global brain...</span>';
    try {
      const data = await api('/api/stocks/scan', { method: 'POST', body: '{}' });
      await refreshState();
      const changed = data.changed ? ' Pick changed by drastic-change rule.' : '';
      if (msg) msg.innerHTML = '<span class="tb-ok">Scan complete. Stocks seen: ' + escapeHtml(data.stocks_seen) + '.' + escapeHtml(changed) + '</span>';
      renderStockBrain();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
      else el('tb-body').innerHTML = `<div class="tb-card"><h3>Stock Scan Failed</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function renderStockHistory() {
    const body = el('tb-body');
    body.innerHTML = `<div class="tb-card"><h3>Stock Prediction History</h3><div class="tb-scan">Loading predictions...</div></div>`;
    try {
      const data = await api('/api/stocks/predictions');
      const rows = data.predictions || [];
      body.innerHTML = `
        <div class="tb-card">
          <h3>Stock Prediction History</h3>
          <div class="tb-actions"><button class="tb-btn" id="tb-back-stock">Back to Stock Brain</button></div>
        </div>
        ${rows.length ? rows.map(p => `
          <div class="tb-card">
            <h3>${escapeHtml(p.acronym)} <span class="tb-pill">${escapeHtml(p.status)}</span></h3>
            <div class="tb-muted">${escapeHtml(p.created_at)} · score ${escapeHtml(p.score)} · confidence ${escapeHtml(p.confidence)}%</div>
            <div class="tb-muted">Pick price: ${fmtMoney(p.pick_price)} · Expected: ${fmtPct(Number(p.expected_24h_pct || 0))}</div>
            <p class="tb-muted">${escapeHtml(p.reason)}</p>
          </div>`).join('') : `<div class="tb-card"><h3>No predictions yet</h3><div class="tb-muted">Run your first stock scan.</div></div>`}
      `;
      el('tb-back-stock')?.addEventListener('click', renderStockBrain);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Prediction History</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }


  async function renderItemMarket() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Item Market</h3><div class="tb-scan">Loading watched item signals...</div></div>`;
    try {
      const data = await api('/api/items/market');
      const watch = data.watchlist || [];
      const signals = data.signals || [];
      body.innerHTML = `
        <div class="tb-card">
          <h3>Item Market Scanner <span class="tb-pill tb-ai-pill">Step 3 Active</span></h3>
          <div class="tb-muted">Add items to watch. The backend stores price snapshots, learns buy/sell zones over time, and alerts when a watched item hits your target zone.</div>
          <input class="tb-input" id="tb-item-query" placeholder="Item name or item ID" autocomplete="off">
          <div class="tb-grid">
            <input class="tb-input" id="tb-item-buy" placeholder="Buy zone optional">
            <input class="tb-input" id="tb-item-sell" placeholder="Sell zone optional">
          </div>
          <div class="tb-actions">
            <button class="tb-btn" id="tb-item-search">Find</button>
            <button class="tb-btn" id="tb-item-add">Add Watch</button>
            <button class="tb-btn" id="tb-item-scan">Scan Now</button>
          </div>
          <div class="tb-muted" id="tb-item-msg">Global item snapshots stored: ${escapeHtml(data.snapshot_count || 0)}</div>
        </div>
        <div class="tb-card" id="tb-item-results" style="display:none"></div>
        <div class="tb-card">
          <h3>Watched Items</h3>
          ${watch.length ? watch.map(w => itemWatchRow(w)).join('') : `<div class="tb-muted">No watched items yet. Add one above, then press Scan Now.</div>`}
        </div>
        <div class="tb-card">
          <h3>Latest Buy/Sell Signals</h3>
          ${signals.length ? signals.map(s => `
            <div class="tb-market-row">
              <div class="tb-market-title"><b>${escapeHtml(s.signal)} · ${escapeHtml(s.name)}</b><span>${fmtMoney(s.current_price)}</span></div>
              <div class="tb-muted">${escapeHtml(s.created_at)} · Buy ${fmtMoney(s.buy_zone)} · Sell ${fmtMoney(s.sell_zone)}</div>
              <div class="tb-muted">${escapeHtml(s.reason)}</div>
              ${s.link ? `<a class="tb-link" href="${escapeHtml(s.link)}">Go Buy / Open Market</a>` : ''}
            </div>`).join('') : `<div class="tb-muted">No signals yet. Notifications will appear when watched items hit buy or sell zones.</div>`}
        </div>
      `;
      el('tb-item-search')?.addEventListener('click', searchItems);
      el('tb-item-add')?.addEventListener('click', addItemWatch);
      el('tb-item-scan')?.addEventListener('click', scanItemsNow);
      body.querySelectorAll('[data-unwatch]').forEach(btn => btn.addEventListener('click', async () => {
        await api('/api/items/unwatch', { method:'POST', body: JSON.stringify({ item_id: btn.dataset.unwatch }) });
        renderItemMarket();
      }));
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Item Market</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  function itemWatchRow(w) {
    const latest = w.latest || {};
    const stats = w.stats || {};
    const sig = String(w.signal || 'WAITING');
    const sigClass = sig === 'BUY' ? 'tb-signal-buy' : (sig === 'SELL' ? 'tb-signal-sell' : 'tb-signal-hold');
    return `
      <div class="tb-market-row">
        <div class="tb-market-title">
          <span><b>${escapeHtml(w.name)}</b> <span class="tb-muted">#${escapeHtml(w.item_id)}</span></span>
          <span class="${sigClass}"><b>${escapeHtml(sig)}</b></span>
        </div>
        <div class="tb-grid" style="margin-top:6px">
          <div><span class="tb-pill">Current</span><br><span class="tb-price">${latest.lowest_price ? fmtMoney(latest.lowest_price) : 'Waiting'}</span></div>
          <div><span class="tb-pill">Buy Zone</span><br>${w.buy_zone ? fmtMoney(w.buy_zone) : 'Auto-learning'}</div>
          <div><span class="tb-pill">Sell Zone</span><br>${w.sell_zone ? fmtMoney(w.sell_zone) : 'Auto-learning'}</div>
          <div><span class="tb-pill">1Y Change</span><br>${Number(stats.count365 || 0) > 1 ? fmtPct(Number(stats.year_change_pct || 0)) : 'Learning'}</div>
        </div>
        <div class="tb-muted">24h low/high: ${fmtMoney(stats.min24)} / ${fmtMoney(stats.max24)} · 7d low/high: ${fmtMoney(stats.min7)} / ${fmtMoney(stats.max7)} · Listings: ${escapeHtml(latest.listing_count || 0)} · Source: ${escapeHtml(latest.source || 'market')}</div>
        <div class="tb-actions">
          <a class="tb-link" href="https://www.torn.com/imarket.php#/p=shop&step=shop&type=&searchname=${encodeURIComponent(w.name)}">Go Buy / Open Market</a>
          <button class="tb-btn tb-danger" data-unwatch="${escapeHtml(w.item_id)}">Remove</button>
        </div>
      </div>`;
  }

  async function searchItems() {
    const box = el('tb-item-results');
    const msg = el('tb-item-msg');
    const q = el('tb-item-query')?.value || '';
    if (msg) msg.innerHTML = '<span class="tb-warn">Searching Torn item catalog...</span>';
    try {
      const data = await api('/api/items/catalog?q=' + encodeURIComponent(q));
      const items = data.items || [];
      box.style.display = 'block';
      box.innerHTML = `<h3>Search Results</h3>${items.length ? items.map(i => `
        <div class="tb-mini-row">
          <span><b>${escapeHtml(i.name)}</b> <span class="tb-muted">#${escapeHtml(i.item_id)} · ${escapeHtml(i.item_type || '')}</span></span>
          <button class="tb-btn" data-additem="${escapeHtml(i.item_id)}" data-addname="${escapeHtml(i.name)}">Watch</button>
        </div>`).join('') : '<div class="tb-muted">No items found. Try item ID.</div>'}`;
      box.querySelectorAll('[data-additem]').forEach(btn => btn.addEventListener('click', async () => {
        await api('/api/items/watch', { method:'POST', body: JSON.stringify({ item_id: btn.dataset.additem, name: btn.dataset.addname }) });
        renderItemMarket();
      }));
      if (msg) msg.innerHTML = '<span class="tb-ok">Search complete.</span>';
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function addItemWatch() {
    const msg = el('tb-item-msg');
    const q = el('tb-item-query')?.value || '';
    if (!q.trim()) { if (msg) msg.innerHTML = '<span class="tb-err">Enter an item name or ID first.</span>'; return; }
    if (msg) msg.innerHTML = '<span class="tb-warn">Adding watch item and scanning...</span>';
    try {
      await api('/api/items/watch', { method:'POST', body: JSON.stringify({ query: q, buy_zone: el('tb-item-buy')?.value || '', sell_zone: el('tb-item-sell')?.value || '' }) });
      try { await api('/api/items/scan', { method:'POST', body: '{}' }); } catch (_) {}
      if (msg) msg.innerHTML = '<span class="tb-ok">Item added and scanned. If live listings are unavailable, catalog market value is used until live prices arrive.</span>';
      renderItemMarket();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function scanItemsNow() {
    const msg = el('tb-item-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Scanning watched item markets...</span>';
    try {
      const data = await api('/api/items/scan', { method:'POST', body: '{}' });
      if (msg) msg.innerHTML = '<span class="tb-ok">Scan complete. Items seen: ' + escapeHtml(data.items_seen || 0) + '. Signals: ' + escapeHtml((data.signals || []).length) + '.</span>';
      renderItemMarket();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }


  async function renderPointsWatcher() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Points Watcher</h3><div class="tb-scan">Loading points market intelligence...</div></div>`;
    try {
      const data = await api('/api/points/market');
      const latest = data.latest || {};
      const stats = data.stats || {};
      const signals = data.signals || [];
      const sig = String(data.signal || 'WAITING');
      const sigClass = sig === 'BUY' ? 'tb-signal-buy' : (sig === 'SELL' ? 'tb-signal-sell' : 'tb-signal-hold');
      body.innerHTML = `
        <div class="tb-card">
          <h3>Points Market Watcher <span class="tb-pill tb-ai-pill">Step 4 Active</span></h3>
          <div class="tb-muted">The backend watches points prices, stores snapshots, learns normal ranges, and alerts when points hit your buy or sell zone.</div>
          <div class="tb-grid">
            <div><span class="tb-pill">Signal</span><br><span class="${sigClass}"><b>${escapeHtml(sig)}</b></span></div>
            <div><span class="tb-pill">Current</span><br><span class="tb-price">${latest.lowest_price ? fmtMoney(latest.lowest_price) : 'Waiting'}</span></div>
            <div><span class="tb-pill">Buy Zone</span><br>${data.buy_zone ? fmtMoney(data.buy_zone) : 'Auto-learning'}</div>
            <div><span class="tb-pill">Sell Zone</span><br>${data.sell_zone ? fmtMoney(data.sell_zone) : 'Auto-learning'}</div>
            <div><span class="tb-pill">Listings</span><br>${escapeHtml(latest.listing_count || 0)}</div>
            <div><span class="tb-pill">Snapshots</span><br>${escapeHtml(data.snapshot_count || 0)}</div>
          </div>
          <div class="tb-actions">
            <button class="tb-btn" id="tb-points-scan">Scan Now</button>
            <a class="tb-link" href="https://www.torn.com/pmarket.php">Open Points Market</a>
          </div>
          <div class="tb-muted" id="tb-points-msg">Last snapshot: ${escapeHtml(latest.created_at || 'waiting for first scan')}</div>
        </div>
        <div class="tb-card">
          <h3>Points Trend</h3>
          <div class="tb-grid">
            <div><span class="tb-pill">24h Low/High</span><br>${fmtMoney(stats.min24)} / ${fmtMoney(stats.max24)}</div>
            <div><span class="tb-pill">7d Low/High</span><br>${fmtMoney(stats.min7)} / ${fmtMoney(stats.max7)}</div>
            <div><span class="tb-pill">30d Low/High</span><br>${fmtMoney(stats.min30)} / ${fmtMoney(stats.max30)}</div>
            <div><span class="tb-pill">1Y Change</span><br>${fmtPct(Number(stats.year_change_pct || 0))}</div>
          </div>
          <div class="tb-muted">With only a little data, the zones are rough. More backend scans make the buy/sell zone smarter.</div>
        </div>
        <div class="tb-card">
          <h3>Latest Points Signals</h3>
          ${signals.length ? signals.map(s => `
            <div class="tb-market-row">
              <div class="tb-market-title"><b>${escapeHtml(s.signal)} · Points</b><span>${fmtMoney(s.current_price)}</span></div>
              <div class="tb-muted">${escapeHtml(s.created_at)} · Buy ${fmtMoney(s.buy_zone)} · Sell ${fmtMoney(s.sell_zone)}</div>
              <div class="tb-muted">${escapeHtml(s.reason)}</div>
              ${s.link ? `<a class="tb-link" href="${escapeHtml(s.link)}">Open Points Market</a>` : ''}
            </div>`).join('') : `<div class="tb-muted">No points signals yet. Notifications will appear when points hit buy or sell zones.</div>`}
        </div>
      `;
      el('tb-points-scan')?.addEventListener('click', scanPointsNow);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Points Watcher</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function scanPointsNow() {
    const msg = el('tb-points-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Scanning points market...</span>';
    try {
      const data = await api('/api/points/scan', { method:'POST', body: '{}' });
      const sig = data.signal ? (' Signal: ' + data.signal.signal) : '';
      if (msg) msg.innerHTML = '<span class="tb-ok">Scan complete. Listings seen: ' + escapeHtml(data.points_seen || 0) + '.' + escapeHtml(sig) + '</span>';
      renderPointsWatcher();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function renderTravelProfit() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Travel Profit</h3><div class="tb-scan">Loading travel profit intelligence...</div></div>`;
    try {
      const data = await api('/api/travel/profit');
      const best = data.best || null;
      const routes = data.routes || [];
      const recs = data.recommendations || [];
      const sig = best ? String(best.signal || 'WAIT') : 'WAITING';
      const sigClass = sig === 'GO' ? 'tb-signal-buy' : (sig === 'RISKY' ? 'tb-signal-sell' : 'tb-signal-hold');
      body.innerHTML = `
        <div class="tb-card">
          <h3>Travel Profit Predictor <span class="tb-pill tb-ai-pill">Step 5 Active</span></h3>
          <div class="tb-muted">The backend compares travel items against home market values, estimates profit, scores travel time, and gives a GO / RISKY / WAIT signal.</div>
          ${best ? `
            <div class="tb-grid">
              <div><span class="tb-pill">Signal</span><br><span class="${sigClass}"><b>${escapeHtml(sig)}</b></span></div>
              <div><span class="tb-pill">Best Country</span><br>${escapeHtml(best.country)}</div>
              <div><span class="tb-pill">Item</span><br>${escapeHtml(best.item_name)}</div>
              <div><span class="tb-pill">Est Profit</span><br><span class="tb-price">${fmtMoney(best.estimated_profit)}</span></div>
              <div><span class="tb-pill">Arrival Chance</span><br>${escapeHtml(best.arrival_chance)}%</div>
              <div><span class="tb-pill">Travel Time</span><br>${escapeHtml(best.minutes || '?')} min one way</div>
            </div>
            <div class="tb-muted" style="margin-top:8px;">${escapeHtml(best.reason || '')}</div>
            <div class="tb-actions">
              <button class="tb-btn" id="tb-travel-scan">Scan Now</button>
              <a class="tb-link" href="${escapeHtml(best.link || 'https://www.torn.com/travelagency.php')}">Open Travel Agency</a>
              ${best.market_link ? `<a class="tb-link" href="${escapeHtml(best.market_link)}">Check Home Market</a>` : ''}
            </div>
          ` : `
            <div class="tb-muted">No travel data yet. Run a scan after your item catalog has loaded.</div>
            <div class="tb-actions"><button class="tb-btn" id="tb-travel-scan">Scan Now</button></div>
          `}
          <div class="tb-muted" id="tb-travel-msg">Snapshots learned: ${escapeHtml(data.snapshot_count || 0)} · ${escapeHtml(data.server_time || '')}</div>
        </div>
        <div class="tb-card">
          <h3>Top Travel Routes</h3>
          ${routes.length ? routes.map(r => {
            const cls = r.signal === 'GO' ? 'tb-signal-buy' : (r.signal === 'RISKY' ? 'tb-signal-sell' : 'tb-signal-hold');
            return `
              <div class="tb-market-row">
                <div class="tb-market-title"><b>${escapeHtml(r.country)} · ${escapeHtml(r.item_name)}</b><span class="${cls}">${escapeHtml(r.signal)}</span></div>
                <div class="tb-muted">Profit ${fmtMoney(r.estimated_profit)} · Arrival ${escapeHtml(r.arrival_chance)}% · Score ${escapeHtml(r.score)} · ${escapeHtml(r.minutes)}m one way</div>
                <div class="tb-muted">Home ${fmtMoney(r.home_price)} · Abroad ${fmtMoney(r.abroad_cost)} · ${escapeHtml(r.reason || '')}</div>
                <div class="tb-actions">
                  <a class="tb-link" href="${escapeHtml(r.link || 'https://www.torn.com/travelagency.php')}">Travel</a>
                  ${r.market_link ? `<a class="tb-link" href="${escapeHtml(r.market_link)}">Market</a>` : ''}
                </div>
              </div>`;
          }).join('') : `<div class="tb-muted">No routes ranked yet. The backend will learn as item/market snapshots build.</div>`}
        </div>
        <div class="tb-card">
          <h3>Latest Travel Notifications</h3>
          ${recs.length ? recs.map(r => `
            <div class="tb-market-row">
              <div class="tb-market-title"><b>${escapeHtml(r.signal)} · ${escapeHtml(r.country)}</b><span>${fmtMoney(r.estimated_profit)}</span></div>
              <div class="tb-muted">${escapeHtml(r.created_at)} · ${escapeHtml(r.item_name)} · Arrival ${escapeHtml(r.arrival_chance)}%</div>
              <div class="tb-muted">${escapeHtml(r.reason)}</div>
              ${r.link ? `<a class="tb-link" href="${escapeHtml(r.link)}">Open Travel Agency</a>` : ''}
            </div>`).join('') : `<div class="tb-muted">No GO alerts yet. Notifications appear when a route beats your profit/chance settings.</div>`}
        </div>
      `;
      el('tb-travel-scan')?.addEventListener('click', scanTravelNow);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Travel Profit</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function scanTravelNow() {
    const msg = el('tb-travel-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Scanning travel profit routes...</span>';
    try {
      const data = await api('/api/travel/scan', { method:'POST', body: '{}' });
      if (msg) msg.innerHTML = '<span class="tb-ok">Scan complete. Routes checked: ' + escapeHtml(data.routes_seen || 0) + '.</span>';
      renderTravelProfit();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }



  async function renderEnemySleep() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Enemy Sleep</h3><div class="tb-scan">Loading enemy activity brain...</div></div>`;
    try {
      const data = await api('/api/enemy/activity');
      const sess = data.session || null;
      const report = data.report || null;
      const hourly = report?.hourly || [];
      const latest = report?.latest || [];
      body.innerHTML = `
        <div class="tb-card">
          <h3>Enemy Sleep / Activity Tracker <span class="tb-pill tb-ai-pill">Step 6 Active</span></h3>
          <div class="tb-muted">Faction-scoped war intel. This tracks your current ranked-war enemy only, learns about 72 hours of activity, and keeps the report private to this login/faction data.</div>
          <div class="tb-actions">
            <button class="tb-btn" id="tb-enemy-start">Start Tracking Current War Enemy</button>
            <button class="tb-btn" id="tb-enemy-scan">Scan Now</button>
            <button class="tb-btn" id="tb-enemy-stop">Stop</button>
          </div>
          <div class="tb-muted" id="tb-enemy-msg">${sess ? 'Tracking ' + escapeHtml(sess.enemy_faction_name || 'enemy faction') : 'Not tracking yet. Press Start Tracking while you are in a ranked war.'}</div>
        </div>
        <div class="tb-card">
          <h3>Current Report</h3>
          ${report ? `
            <div class="tb-grid">
              <div><span class="tb-pill">Enemy</span><br>${escapeHtml(report.enemy_faction_name || 'Enemy')}</div>
              <div><span class="tb-pill">Confidence</span><br><b>${escapeHtml(report.confidence || 'Low')}</b></div>
              <div><span class="tb-pill">Samples</span><br>${escapeHtml(report.sample_count || 0)}</div>
              <div><span class="tb-pill">Members Seen</span><br>${escapeHtml(report.member_count || 0)}</div>
              <div><span class="tb-pill">Active Ratio</span><br>${escapeHtml(report.active_ratio || 0)}%</div>
              <div><span class="tb-pill">Inactive Ratio</span><br>${escapeHtml(report.inactive_ratio || 0)}%</div>
            </div>
            <div class="tb-market-row">
              <div class="tb-market-title"><b>Best Attack Window</b><span class="tb-signal-buy">${escapeHtml(report.best_attack_window || 'Learning')}</span></div>
              <div class="tb-muted">Lowest enemy active window in the recorded 72h pattern.</div>
            </div>
            <div class="tb-market-row">
              <div class="tb-market-title"><b>Best Turtle Window</b><span class="tb-signal-sell">${escapeHtml(report.best_turtle_window || 'Learning')}</span></div>
              <div class="tb-muted">Highest enemy active window. Good time to defend, turtle, or avoid wasting energy.</div>
            </div>
            <div class="tb-muted">${escapeHtml(report.summary || '')}</div>
          ` : `<div class="tb-muted">No report yet. Start tracking and the backend will save activity snapshots automatically.</div>`}
        </div>
        <div class="tb-card">
          <h3>Hourly Activity Pattern</h3>
          ${hourly.length ? hourly.map(h => `
            <div class="tb-market-row">
              <div class="tb-market-title"><b>${String(h.hour).padStart(2,'0')}:00 Torn</b><span>${escapeHtml(h.active_pct)}% active</span></div>
              <div class="tb-muted">Samples: ${escapeHtml(h.samples)}</div>
            </div>`).join('') : `<div class="tb-muted">Hourly pattern appears after snapshots collect. Three days gives a much better result.</div>`}
        </div>
        <div class="tb-card">
          <h3>Latest Seen Enemy States</h3>
          ${latest.length ? latest.map(r => `
            <div class="tb-market-row">
              <div class="tb-market-title"><b>${escapeHtml(r.enemy_name || 'Enemy')}</b><span>${escapeHtml(r.activity_bucket || '')}</span></div>
              <div class="tb-muted">${escapeHtml(r.captured_at || '')} · ${escapeHtml(r.online_status || '')} · ${escapeHtml(r.status_description || '')}</div>
            </div>`).join('') : `<div class="tb-muted">No latest enemy states yet.</div>`}
        </div>
      `;
      el('tb-enemy-start')?.addEventListener('click', enemyStart);
      el('tb-enemy-scan')?.addEventListener('click', enemyScan);
      el('tb-enemy-stop')?.addEventListener('click', enemyStop);
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Enemy Sleep</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  async function enemyStart() {
    const msg = el('tb-enemy-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Finding current ranked war enemy and starting tracker...</span>';
    try {
      const data = await api('/api/enemy/start', { method:'POST', body: '{}' });
      if (msg) msg.innerHTML = '<span class="tb-ok">Tracking started. Members seen: ' + escapeHtml(data.members_seen || 0) + '.</span>';
      renderEnemySleep();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function enemyScan() {
    const msg = el('tb-enemy-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Scanning enemy activity...</span>';
    try {
      const data = await api('/api/enemy/scan', { method:'POST', body: '{}' });
      if (msg) msg.innerHTML = '<span class="tb-ok">Enemy scan complete. Members seen: ' + escapeHtml(data.members_seen || 0) + '.</span>';
      renderEnemySleep();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function enemyStop() {
    const msg = el('tb-enemy-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Stopping enemy tracker...</span>';
    try {
      await api('/api/enemy/stop', { method:'POST', body: '{}' });
      if (msg) msg.innerHTML = '<span class="tb-ok">Enemy tracking stopped.</span>';
      renderEnemySleep();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function runAccuracyNow() {
    const msg = el('tb-accuracy-msg');
    if (msg) msg.innerHTML = '<span class="tb-warn">Checking prediction results...</span>';
    try {
      const data = await api('/api/accuracy/run', { method:'POST', body:'{}' });
      if (msg) msg.innerHTML = `<span class="tb-ok">Checked: stock ${data.stock || 0}, items ${data.item || 0}, points ${data.points || 0}, travel ${data.travel || 0}.</span>`;
      renderAccuracy();
    } catch (e) {
      if (msg) msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function renderAccuracy() {
    const body = el('tb-body');
    if (!token()) { body.innerHTML = renderLogin(); bindLogin(); return; }
    body.innerHTML = `<div class="tb-card"><h3>Accuracy</h3><div class="tb-scan">Loading learning brain...</div></div>`;
    try {
      const data = await api('/api/accuracy');
      const summary = data.summary || [];
      const weights = data.weights || [];
      const recent = data.recent || [];
      const adjustments = data.adjustments || [];
      const summaryCards = summary.map(r => {
        const total = Number(r.total || 0);
        const correct = Number(r.correct || 0);
        const rate = total ? ((correct / total) * 100) : 0;
        return `
          <div class="tb-card">
            <h3>${escapeHtml(r.module)}</h3>
            <div class="tb-grid">
              <div><span class="tb-pill">Correct</span><br><span class="tb-score">${correct}/${total}</span></div>
              <div><span class="tb-pill">Rate</span><br><span class="tb-score">${rate.toFixed(0)}%</span></div>
              <div><span class="tb-pill">Avg Result</span><br><span class="tb-score">${fmtPct(Number(r.avg_pct || 0))}</span></div>
            </div>
          </div>
        `;
      }).join('') || `<div class="tb-card"><h3>No checked results yet</h3><div class="tb-muted">The app needs predictions or alerts to be at least 24h old before it can score them. Travel GO calls can be checked sooner once enough route snapshots exist.</div></div>`;
      const weightCards = weights.map(w => `
        <div class="tb-market-row">
          <div class="tb-market-title"><b>${escapeHtml(w.module)} · ${escapeHtml(w.signal_key)}</b><span>${Number(w.weight_value || 1).toFixed(2)}x</span></div>
          <div class="tb-muted">Updated ${escapeHtml(w.updated_at)}</div>
        </div>
      `).join('') || `<div class="tb-muted">No weight changes yet. After enough results, the app will raise what works and lower what fails.</div>`;
      const recentRows = recent.map(r => `
        <div class="tb-market-row">
          <div class="tb-market-title"><b>${escapeHtml(r.module)} · ${escapeHtml(r.target_name)}</b><span class="${r.was_correct ? 'tb-ok' : 'tb-err'}">${r.was_correct ? 'RIGHT' : 'WRONG'}</span></div>
          <div class="tb-muted">${escapeHtml(r.signal)} · predicted ${fmtMoney(r.predicted_value)} → actual ${fmtMoney(r.actual_value)} · ${fmtPct(Number(r.result_pct || 0))}</div>
          <div class="tb-muted">${escapeHtml(r.notes || '')}</div>
        </div>
      `).join('') || `<div class="tb-muted">No scored events yet.</div>`;
      const adjustRows = adjustments.map(a => `
        <div class="tb-market-row">
          <div class="tb-market-title"><b>${escapeHtml(a.module)} · ${escapeHtml(a.signal_key)}</b><span>${Number(a.old_weight || 1).toFixed(2)}x → ${Number(a.new_weight || 1).toFixed(2)}x</span></div>
          <div class="tb-muted">${escapeHtml(a.reason)}</div>
        </div>
      `).join('') || `<div class="tb-muted">No learning adjustments yet.</div>`;
      body.innerHTML = `
        <div class="tb-card">
          <h3>Self-Learning Accuracy</h3>
          <div class="tb-muted">This checks old predictions against saved backend results, then adjusts global learning weights so new users can benefit from your recorded data later.</div>
          <div class="tb-actions"><button class="tb-btn" id="tb-accuracy-run">Check Learning Now</button></div>
          <div id="tb-accuracy-msg" class="tb-muted">Last refresh: ${escapeHtml(data.server_time || '')}</div>
        </div>
        ${summaryCards}
        <div class="tb-card"><h3>Global Brain Weights</h3>${weightCards}</div>
        <div class="tb-card"><h3>Recent Scored Predictions</h3>${recentRows}</div>
        <div class="tb-card"><h3>Recent Learning Adjustments</h3>${adjustRows}</div>
      `;
      el('tb-accuracy-run')?.addEventListener('click', runAccuracyNow);
      el('tb-accuracy-run')?.addEventListener('touchend', (e) => { e.preventDefault(); runAccuracyNow(); }, { passive:false });
    } catch (e) {
      body.innerHTML = `<div class="tb-card"><h3>Accuracy</h3><div class="tb-err">${escapeHtml(e.message)}</div></div>`;
    }
  }

  function render() {
    const body = el('tb-body');
    if (!body) return;

    if (activeTab === 'Notifications') { renderNotifications(); return; }
    if (activeTab === 'Settings') { renderSettings(); return; }

    if (!token()) {
      if (savedApiKey()) {
        body.innerHTML = `<div class="tb-card"><h3>Restoring login...</h3><div class="tb-scan">Using saved key on this device. No need to paste it again.</div></div>`;
        restoreLogin('render').then(ok => { if (ok) render(); else { body.innerHTML = renderLogin(); bindLogin(); } });
        return;
      }
      body.innerHTML = renderLogin();
      bindLogin();
      return;
    }

    if (activeTab === 'Overview') { renderOverviewLive(); return; }
    else if (activeTab === 'Stock Brain') { renderStockBrain(); return; }
    else if (activeTab === 'Item Market') { renderItemMarket(); return; }
    else if (activeTab === 'Points Watcher') { renderPointsWatcher(); return; }
    else if (activeTab === 'Travel Profit') { renderTravelProfit(); return; }
    else if (activeTab === 'Enemy Sleep') { renderEnemySleep(); return; }
    else if (activeTab === 'Accuracy') { renderAccuracy(); return; }
    else body.innerHTML = renderOverview();
  }

  function bindLogin() {
    const btn = el('tb-login');
    if (!btn) return;
    btn.addEventListener('click', doLogin);
    btn.addEventListener('touchend', (e) => { e.preventDefault(); doLogin(); }, { passive: false });
  }

  async function doLogin() {
    const msg = el('tb-login-msg');
    const input = el('tb-api-key');
    const key = input?.value?.trim() || '';
    if (!key) { msg.innerHTML = '<span class="tb-err">Paste your API key first.</span>'; return; }
    msg.textContent = 'Connecting...';
    try {
      const data = await api('/api/login', { method: 'POST', body: JSON.stringify({ api_key: key }) });
      if (!data.token) throw new Error('Backend login worked but no session token came back.');
      setToken(data.token);
      try { await api('/api/auto/start', { method: 'POST', body: '{}' }); } catch (_) {}
      state = await api('/api/state');
      activeTab = 'Overview';
      GM_setValue('fries91_torn_brain_tab_v1', activeTab);
      renderTabs();
      render();
      updateBadge();
    } catch (e) {
      msg.innerHTML = '<span class="tb-err">' + escapeHtml(e.message) + '</span>';
    }
  }

  async function refreshState(silent = false) {
    const now = Date.now();
    if (refreshBusy) return;
    if (silent && now - lastStateRefresh < 12000) return;
    refreshBusy = true;
    if (!token()) { state = null; updateBadge(); if (!silent) render(); refreshBusy = false; return; }
    try {
      state = await api('/api/state');
      lastStateRefresh = now;
      updateBadge();
      if (!silent && activeTab === 'Overview') render();
    } catch (e) {
      // Do NOT log the user out for a normal Render wake-up/network hiccup.
      // Only clear the saved token when the backend clearly says the token is invalid.
      if (e.status === 401 || e.status === 403) {
        setToken('');
        const restored = await restoreLogin('expired');
        if (restored) {
          try { state = await api('/api/state'); lastStateRefresh = now; updateBadge(); if (!silent && activeTab === 'Overview') render(); } catch (_) {}
        } else {
          state = null;
          updateBadge();
          if (!silent) render();
        }
      } else {
        updateBadge();
        if (!silent && activeTab === 'Overview') {
          const body = el('tb-body');
          if (body && token()) {
            body.innerHTML = `<div class="tb-card"><h3>Still Logged In</h3><div class="tb-err">${escapeHtml(e.message)}</div><div class="tb-muted">Render may be waking up or TornPDA blocked a refresh. Your saved login was kept. Change tabs or tap refresh again.</div></div>`;
          }
        }
      }
    } finally {
      refreshBusy = false;
    }
  }

  function updateBadge() {
    // Quiet notification mode: no red bubble on the AI icon.
    // The unread number appears only on the Notifications tab while the overlay is open.
    if (el('tb-panel')?.classList.contains('tb-show')) renderTabs();
  }

  function boot() {
    mount();
    refreshState(true);
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(() => {
      if (!el('tb-icon')) { mounted = false; mount(); }
      if (el('tb-panel')?.classList.contains('tb-show')) refreshState(true);
    }, 60000);
  }

  boot();
})();
