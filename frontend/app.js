/* ==========================================================================
   TokenLens · 前端应用
   ========================================================================== */
(() => {
  'use strict';

  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const S = {
    boot: null, data: null, range: localStorage.getItem('tl.range') || '30d',
    view: localStorage.getItem('tl.view') || 'overview',
    metric: 'total', recOffset: 0, recTool: '', recOrder: 'ts', query: '', recRequest: 0,
    recProject: '', recModel: '', recProjectPaths: '',
    recColumns: (() => { try { return JSON.parse(localStorage.getItem('tl.rec.columns') || 'null') || {
      model: true, project: true, input: true, output: true, cache_read: true,
      cache_write: true, total: true, cost: true, source: true,
    }; } catch (_) { return { model:true, project:true, input:true, output:true, cache_read:true, cache_write:true, total:true, cost:true, source:true }; } })(),
    // 明细流水的时间筛选（YYYY-MM-DD，空串表示不额外收窄）
    recFrom: '', recTo: '', ledgerCur: 'usd',
    timer: null, rendered: {}, forceTheme: null, budgetWarned: 0,
    // 扫描进度走 SSE：scanAbort 用来关掉上一条流，避免重连时叠出多条
    scanAbort: null,
    scanState: null,
    autoTimer: null, drawerOpen: false, lastLoad: 0,
    dashboardCache: new Map(), dashboardAbort: null, dashboardRequest: 0,
    themePreference: null,
    // 启动语言：**只认后端 config.json**，不读 localStorage。
    // localStorage 里存的是"上一次会话"的值，会在 bootstrap 返回之前抢先渲染，
    // 造成"设置里是中文、界面却是英文"。真正的语言在 main() 里由
    // applyLanguage(boot.config.language) 统一下发。
    language: 'zh-CN',
    rangeMotion: false,
    warmEpoch: 0,
    // 今日分时段的显示粒度（30 / 60 分钟）；后端统一给 30 分钟桶
    intradayBucket: 60,
    // 渲染纪元：数据/配置一变就 +1，已经渲染过的视图凭它判断是否要重建
    epoch: 0, recCache: {},
    // 本次会话内改过的配置项。后端已经持久化了，但 /api/dashboard 的响应可能
    // 还是改动前的那一份（命中缓存、或请求在改配置之前就发出了）。设置页回填
    // 控件时以这里为准，否则「切成英文 → 去别的页 → 回设置」会读回旧值，
    // 下拉框被打回简体中文。刷新页面即清空，不会长期遮蔽服务端配置。
    cfgLocal: {},
    analysisContext: null, returnId: null,
  };

  const DASHBOARD_CACHE_LIMIT = 8;

  function cacheDashboard(range, data) {
    if (S.dashboardCache.has(range)) S.dashboardCache.delete(range);
    S.dashboardCache.set(range, data);
    if (S.dashboardCache.size > DASHBOARD_CACHE_LIMIT) {
      S.dashboardCache.delete(S.dashboardCache.keys().next().value);
    }
  }

  function clearDashboardCache() {
    S.dashboardCache.clear();
  }

  /* 记下"本次会话里用户刚改过的配置项"。
     为什么要本地记账：改配置走的是 POST /api/config，而设置页的控件是从
     /api/dashboard 的响应里回填的。如果那次响应在改配置之前就已经生成
     （命中服务端缓存，或请求早于这次修改发出），它带回来的就是旧值，
     离开设置页再回来，控件会被打回原样——用户明明选了英文，回来却又是
     「简体中文」。本地记账优先级高于服务端下发的值，刷新页面即失效。 */
  function rememberCfg(key, value) {
    if (key) S.cfgLocal[key] = value;
  }

  let preferenceWrites = Promise.resolve();
  const preferenceSaved = {}, preferenceRevision = {};
  function persistPreference(key, value, apply) {
    if (!(key in preferenceSaved)) {
      preferenceSaved[key] = cfgValue((S.data || S.boot || {}).config || {}, key, value);
    }
    const revision = preferenceRevision[key] = (preferenceRevision[key] || 0) + 1;
    rememberCfg(key, value);
    preferenceWrites = preferenceWrites.then(async () => {
      try {
        await api('/api/config', {method: 'POST', body: {[key]: value}});
        preferenceSaved[key] = value;
        if (S.boot && S.boot.config) S.boot.config[key] = value;
        if (S.data && S.data.config) S.data.config[key] = value;
        clearDashboardCache();
      } catch (e) {
        if (preferenceRevision[key] === revision) {
          rememberCfg(key, preferenceSaved[key]);
          apply(preferenceSaved[key], false);
          const input = $('#' + key + 'Select');
          if (input) input.value = preferenceSaved[key];
        }
        toast((isEn() ? 'Setting was not saved: ' : '设置未保存：') + e.message, 'err');
      }
    });
  }

  function bindAutoSettings() {
    const fields = {
      quickToggle:['codex_quick','bool'],scanOnStartToggle:['scan_on_start','bool'],rateInput:['usd_to_cny','number'],
      workerInput:['workers','integer'],maxMbInput:['max_file_mb','integer'],autoScanInput:['auto_scan_minutes','integer'],
      budgetTokensInput:['budget_tokens','integer'],budgetCostInput:['budget_cost','number'],budgetPeriodSelect:['budget_period','text'],
      warnRatioInput:['warn_ratio','percent'],bigLibToggle:['big_lib_mode','bool'],recPageInput:['records_page','integer'],
      listCapInput:['list_cap','integer'],defaultRangeSelect:['default_range','text'],plMaxTokensInput:['pl_max_tokens','integer'],
      plThinkToggle:['pl_show_thinking','bool'],aiWarmupToggle:['ai_warmup_enabled','bool'],aiHedgeToggle:['ai_hedge_enabled','bool']
    };
    const status = $('#settingsSaveStatus');
    const timers = new Map(), versions = new Map(), invalid = new Set(), failures = new Set(), pending = new Set();
    const showStatus = () => {
      if (!status) return;
      status.classList.toggle('error', invalid.size > 0 || failures.size > 0);
      status.textContent = invalid.size ? (isEn()?'Enter a valid value':'请输入有效的设置值')
        : failures.size ? (isEn()?'Save failed. Please retry.':'保存失败，请重试')
        : pending.size || timers.size ? (isEn()?'Saving…':'正在保存…')
        : (isEn()?'Saved automatically':'已自动保存');
    };
    const effects = (key,next) => {
      invalidateViews();
      if(key==='auto_scan_minutes')scheduleAutoScan(next);
      if(key.startsWith('budget_')||key==='warn_ratio')S.budgetWarned=0;
      if(key==='usd_to_cny')applyDisplayCurrency(cfgValue((S.data||S.boot||{}).config||{},'display_currency','auto'),false);
    };
    for (const [id,[key,kind]] of Object.entries(fields)) {
      const input = $('#'+id); if (!input) continue;
      const save = () => {
        clearTimeout(timers.get(id));timers.delete(id);
        const revision=versions.get(id)||0;
        let value=kind==='bool'?input.checked:kind==='text'?input.value:Number(input.value);
        const valid=kind==='bool'||(input.checkValidity()&&input.value.trim()!==''
          &&(kind==='text'||Number.isFinite(value))&&(kind!=='integer'||Number.isInteger(value)));
        if(!valid){invalid.add(id);input.setAttribute('aria-invalid','true');showStatus();return;}
        invalid.delete(id);input.removeAttribute('aria-invalid');
        if(kind==='percent')value/=100;
        failures.delete(id);
        if(cfgValue((S.data||S.boot||{}).config||{},key,null)===value){
          // A blur/change after the debounce may repeat the in-flight value.
          // Keep that write responsible for rollback of this identical edit.
          const repeated=[...pending].reverse().find(row=>row.id===id&&row.value===value);
          if(repeated)repeated.revision=revision;
          showStatus();return;
        }
        const token={id,revision,value};pending.add(token);showStatus();
        // The shared preference queue rolls back only the latest committed
        // write. Do not let its rollback replace a newer, still-debounced edit.
        persistPreference(key,value,next=>{
          if(versions.get(id)!==token.revision)return;
          if(kind==='bool')input.checked=!!next;else input.value=kind==='percent'?next*100:next;
          effects(key,next);
        });
        const write=preferenceWrites;
        write.then(()=>{
          pending.delete(token);
          const saved=preferenceSaved[key]===value;
          if(saved){
            // Side effects belong to each successful setting, even when a
            // different control was edited while this request was in flight.
            effects(key,value);
            if(versions.get(id)===token.revision){failures.delete(id);if(kind==='bool')input.checked=!!value;else input.value=kind==='percent'?value*100:value;}
          }else if(versions.get(id)===token.revision)failures.add(id);
          showStatus();
        });
      };
      input.addEventListener('change',()=>{versions.set(id,(versions.get(id)||0)+1);save();});
      if(['number','integer','percent'].includes(kind))input.addEventListener('input',()=>{
        versions.set(id,(versions.get(id)||0)+1);clearTimeout(timers.get(id));
        timers.set(id,setTimeout(save,450));showStatus();
      });
    }
  }

  /** 读取一项配置：本地刚改过的优先，其次服务端下发的，最后是兜底默认值。 */
  function cfgValue(c, key, fallback) {
    if (Object.prototype.hasOwnProperty.call(S.cfgLocal, key)) return S.cfgLocal[key];
    if (c && c[key] !== undefined && c[key] !== null) return c[key];
    return fallback;
  }

  function setDashboardBusy(busy) {
    document.documentElement.classList.toggle('dashboard-loading', !!busy);
    const meter = $('#fpsMeter');
    if (meter) meter.dataset.loading = busy ? '1' : '0';
  }
  function uiHidden() {return document.hidden || !!window.TLDesktop?.hidden;}

  function initFpsMeter() {
    const meter = $('#fpsMeter'), value = $('#fpsValue'), state = $('#fpsState');
    if (!meter || !value || !state) return;
    // rAF callback cadence, never a guessed refresh rate or a capped/faked FPS.
    // No geometry reads in this loop: reading offsetParent can force layout.
    const intervals = new Float64Array(1200);
    let count = 0, cursor = 0, previous = 0, windowStart = 0, frames = 0, raf = 0;
    let latest = { fps: null, frameP95Ms: null, frameMaxMs: null, cadenceFps: null, slowFrames: 0 };
    let longTaskCount = 0, longTaskMs = 0;
    const longTasks = [], renders = [];
    const samples = () => Array.from(intervals.subarray(0, count)).sort((a, b) => a - b);
    const percentile = (sorted, p) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * p) - 1)] : null;
    const shouldSample = () => !uiHidden() && !document.documentElement.classList.contains('no-fps');
    const diagnostics = {
      snapshot() {
        const sorted = samples();
        return { ...latest, frameSamples: count, frameP95Ms: percentile(sorted, .95),
          frameP99Ms: percentile(sorted, .99), frameMaxMs: sorted.length ? sorted[sorted.length - 1] : null,
          longTaskSupported: !!longObserver, longTaskCount, longTaskMs,
          longTasks: longTasks.slice(), renders: renders.slice(),
          visible: !uiHidden(), sampling: !!raf, view: S.view,
          domNodes: document.getElementsByTagName('*').length,
          recordRows: $('#recordTable') ? Math.max(0, $('#recordTable').rows.length - 1) : 0,
          translation: window.TL_I18N && window.TL_I18N.stats ? window.TL_I18N.stats() : null };
      },
      reset() { count = cursor = frames = 0; previous = windowStart = 0;
        longTaskCount = longTaskMs = 0; longTasks.length = renders.length = 0;
        latest = { fps: null, frameP95Ms: null, frameMaxMs: null, cadenceFps: null, slowFrames: 0 };
      },
      recordRender(view, duration) {
        renders.push({ view, durationMs: +duration.toFixed(2), at: +performance.now().toFixed(1) });
        if (renders.length > 80) renders.shift();
      },
    };
    let longObserver = null;
    try {
      if (typeof PerformanceObserver === 'function' && PerformanceObserver.supportedEntryTypes.includes('longtask')) {
        longObserver = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            if (uiHidden()) continue;
            longTaskCount++; longTaskMs += entry.duration;
            longTasks.push({ start: +entry.startTime.toFixed(1), durationMs: +entry.duration.toFixed(2) });
            if (longTasks.length > 80) longTasks.shift();
          }
        });
        longObserver.observe({ type: 'longtask' });
      }
    } catch (_) { longObserver = null; }
    window.__TL_PERF__ = diagnostics;
    const stop = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = frames = previous = windowStart = 0;
    };
    const tick = (now) => {
      raf = 0;
      if (!shouldSample()) return;
      if (!previous) { previous = windowStart = now; raf = requestAnimationFrame(tick); return; }
      const elapsed = now - previous;
      previous = now;
      if (elapsed > 0) { intervals[cursor] = elapsed; cursor = (cursor + 1) % intervals.length; count = Math.min(count + 1, intervals.length); }
      frames++;
      if (now - windowStart >= 500) {
        const fps = frames * 1000 / (now - windowStart);
        const sorted = samples(), baseline = percentile(sorted, .2), p95 = percentile(sorted, .95);
        const slowFrames = sorted.filter(ms => ms > baseline * 1.6).length;
        const smooth = count >= 20 && fps >= (1000 / baseline) * .85 && p95 <= baseline * 1.6;
        latest = { fps: +fps.toFixed(1), frameP95Ms: +p95.toFixed(2),
          frameMaxMs: +sorted[sorted.length - 1].toFixed(2), cadenceFps: +(1000 / baseline).toFixed(1), slowFrames };
        const text = String(Math.round(fps));
        if (value.textContent !== text) value.textContent = text;
        meter.classList.toggle('below-target', count >= 20 && !smooth);
        const label = count < 20 ? (isEn() ? 'Sampling' : '采样中') : smooth ? (isEn() ? 'Smooth' : '流畅') : (isEn() ? 'Busy' : '繁忙');
        if (state.textContent !== label) state.textContent = label;
        const title = `${fps.toFixed(1)} FPS · ${isEn() ? 'Frame p95' : '帧间隔 p95'} ${p95.toFixed(1)} ms`;
        if (meter.title !== title) meter.title = title;
        if (meter.dataset.fps !== String(latest.fps)) meter.dataset.fps = String(latest.fps);
        frames = 0; windowStart = now;
      }
      raf = requestAnimationFrame(tick);
    };
    const sync = () => {
      stop();
      if (shouldSample()) {
        value.textContent = '--';
        state.textContent = isEn() ? 'Sampling' : '采样中';
        raf = requestAnimationFrame(tick);
      }
    };
    document.addEventListener('visibilitychange', sync, { passive: true });
    // applyBoolPref calls this when the FPS preference changes.
    fpsMeterSync = sync;
    sync();
  }

  let fpsMeterSync = null;
  let uiReady = false;

  /* ---------------- 数字与格式化 ----------------
     中文用「万 / 亿 / 万亿」，英文必须换成 K / M / B / T。
     这不能交给 i18n 的文本替换层：单位是跟着数字拼出来的，
     让「13.3亿」在英文界面里变成「13.3亿」是最扎眼的漏翻。
     isEn() 直接读当前语言，所以两套数字写法共用一个入口。 */
  const isEn = () => S.language === 'en-US';
  const NUMBER_FORMATS = {
    'zh-CN': new Intl.NumberFormat('zh-CN'),
    'en-US': new Intl.NumberFormat('en-US'),
  };
  // 区间标签由后端算（含"近 N 天"这类带插值的文案），后端给中英两份。
  const rangeLabel = (D) => (isEn() && D && D.range_label_en) ? D.range_label_en : ((D || {}).range_label || '');
  function humanNum(n) {
    n = +n || 0;
    const a = Math.abs(n);
    const trim = (s) => s.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
    if (isEn()) {
      if (a >= 1e12) return { v: trim((n / 1e12).toFixed(2)), u: 'T' };
      if (a >= 1e9)  return { v: trim((n / 1e9).toFixed(2)),  u: 'B' };
      if (a >= 1e6)  return { v: trim((n / 1e6).toFixed(2)),  u: 'M' };
      if (a >= 1e3)  return { v: trim((n / 1e3).toFixed(1)),  u: 'K' };
      return { v: NUMBER_FORMATS['en-US'].format(Math.round(n)), u: '' };
    }
    if (a >= 1e12) return { v: trim((n / 1e12).toFixed(2)), u: '万亿' };
    if (a >= 1e8)  return { v: trim((n / 1e8).toFixed(2)),  u: '亿' };
    if (a >= 1e4)  return { v: trim((n / 1e4).toFixed(1)),  u: '万' };
    return { v: NUMBER_FORMATS['zh-CN'].format(Math.round(n)), u: '' };
  }
  const fmt    = (n) => { const h = humanNum(n); return h.v + h.u; };
  const fmtExact = (n) => NUMBER_FORMATS[isEn() ? 'en-US' : 'zh-CN'].format(+n || 0);
  function configureMoney() {
    const c=S.data?.config || S.boot?.config || {};
    window.TLMoney?.configure?.({language:S.language,rate:cfgValue(c,'usd_to_cny',7.2),currency:cfgValue(c,'display_currency','auto')});
    S.ledgerCur=window.TLMoney?.currency?.()==='CNY'?'cny':'usd';
    $$('#ledgerSeg button').forEach(button=>button.classList.toggle('active',button.dataset.cur===S.ledgerCur));
  }
  function money(usd, rate) {
    if(window.TLMoney)return window.TLMoney.format(usd);
    const c = (+usd || 0) * (rate || 7.2);
    // 英文界面用 $ 计价更自然；汇率换算只对中文用户有意义。
    if (isEn()) {
      const v = +usd || 0;
      if (v >= 1000) return '$' + (v / 1000).toFixed(1) + 'k';
      if (v >= 1)    return '$' + v.toFixed(2);
      return '$' + v.toFixed(3);
    }
    if (c >= 10000) return '¥' + (c / 10000).toFixed(2) + ' 万';
    if (c >= 100)   return '¥' + c.toFixed(1);
    if (c >= 1)     return '¥' + c.toFixed(2);
    return '¥' + c.toFixed(3);
  }
  function applyDisplayCurrency(value, persist = true) {
    value=['auto','usd','cny'].includes(value)?value:'auto';rememberCfg('display_currency',value);
    if($('#displayCurrencySelect'))$('#displayCurrencySelect').value=value;
    configureMoney();invalidateViews();renderNavigationContext();
    if(S.data && uiReady)scheduleViewRender(S.view);
    if(persist)persistPreference('display_currency',value,applyDisplayCurrency);
  }
  const pct = (x) => (x == null ? '—' : (x > 0 ? '+' : '') + x.toFixed(1) + '%');
  const dd = (ts) => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  const dtime = (ts) => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  /* ---------------- 提示气泡 ---------------- */
  const tipEl = $('#tooltip');
  let tipFrame = 0;
  let tipPending = null;
  function paintTip() {
    tipFrame = 0;
    const pending = tipPending;
    tipPending = null;
    if (!pending || !tipEl) return;
    const { html, x, y } = pending;
    tipEl.innerHTML = html;
    tipEl.classList.add('on');
    const r = tipEl.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > innerWidth - 10) left = x - r.width - 14;
    if (top + r.height > innerHeight - 10) top = y - r.height - 14;
    tipEl.style.left = Math.max(8, left) + 'px';
    tipEl.style.top = Math.max(8, top) + 'px';
  }
  function showTip(html, x, y) {
    tipPending = { html, x, y };
    if (!tipFrame) tipFrame = requestAnimationFrame(paintTip);
  }
  const hideTip = () => {
    tipPending = null;
    if (tipFrame) { cancelAnimationFrame(tipFrame); tipFrame = 0; }
    if (tipEl) tipEl.classList.remove('on');
  };

  function toast(msg, kind = 'info', ms = 3200) {
    const n = el('div', 'toast ' + kind, esc(msg));
    $('#toastWrap').appendChild(n);
    setTimeout(() => { n.style.transition = 'opacity .3s,transform .3s'; n.style.opacity = 0;
      n.style.transform = 'translateX(20px)'; setTimeout(() => n.remove(), 320); }, ms);
  }

  /* ---------------- 应用内确认 ----------------
     浏览器原生 confirm 会脱离主题、阻塞页面线程，也无法跟随中英文界面。
     统一用 Promise 驱动的内置弹层，调用方仍保持「确认后继续」的线性写法。 */
  let confirmResolve = null;
  let confirmPreviousFocus = null;
  function closeConfirm(result) {
    const mask = $('#confirmMask');
    if (!mask || !confirmResolve) return;
    const resolve = confirmResolve;
    confirmResolve = null;
    mask.classList.remove('on');
    mask.setAttribute('aria-hidden', 'true');
    const previous = confirmPreviousFocus;
    confirmPreviousFocus = null;
    if (previous && typeof previous.focus === 'function') {
      try { previous.focus({ preventScroll: true }); } catch (_) { previous.focus(); }
    }
    resolve(!!result);
  }
  function confirmDialog(message, options = {}) {
    const mask = $('#confirmMask');
    const modal = mask && $('.confirm-modal', mask);
    const title = $('#confirmTitle');
    const copy = $('#confirmMessage');
    const kicker = $('#confirmKicker');
    const accept = $('#confirmAccept');
    const cancel = $('#confirmCancel');
    if (!mask || !modal || !title || !copy || !accept || !cancel) return Promise.resolve(false);
    if (confirmResolve) closeConfirm(false);
    const en = isEn();
    title.textContent = options.title || (en ? 'Confirm action' : '确认操作');
    copy.textContent = String(message == null ? '' : message);
    kicker.textContent = en ? 'CONFIRM ACTION' : '确认操作';
    accept.textContent = options.acceptLabel || (en ? 'Confirm' : '确定');
    cancel.textContent = options.cancelLabel || (en ? 'Cancel' : '取消');
    modal.classList.toggle('is-danger', !!options.danger);
    confirmPreviousFocus = document.activeElement;
    // 确认层可能是在配置/历史等弹窗之上打开。把它移到 body 最后，
    // 配合独立的高层级，确保不会被其它固定定位浮层或动态弹窗盖住。
    document.body.appendChild(mask);
    mask.classList.add('on');
    mask.setAttribute('aria-hidden', 'false');
    requestAnimationFrame(() => (options.focus === 'cancel' ? cancel : accept).focus());
    return new Promise((resolve) => { confirmResolve = resolve; });
  }
  function initConfirmDialog() {
    const mask = $('#confirmMask');
    if (!mask) return;
    $('#confirmAccept').onclick = () => closeConfirm(true);
    $('#confirmCancel').onclick = () => closeConfirm(false);
    mask.onclick = (e) => { if (e.target === mask) closeConfirm(false); };
    mask.addEventListener('keydown', (e) => {
      if (!mask.classList.contains('on')) return;
      if (e.key === 'Escape') { e.preventDefault(); closeConfirm(false); }
      else if (e.key === 'Enter' && e.target !== $('#confirmCancel')) { e.preventDefault(); closeConfirm(true); }
    });
  }

  /* ---------------- 调色 ----------------
     图表配色必须跟着主题走。水墨主题用一套高饱和的蓝紫会立刻破功——
     画面上是宣纸和松烟墨，图里却是霓虹色，看起来像贴错了图。
     所以水墨单独给一组「国画颜料」色：花青、赭石、朱砂、藤黄、石绿、
     胭脂、黛、赭、靛——全部降饱和，能落在纸色上而不喧宾夺主。 */
  const PALETTE = ['#7c8cf8', '#a855f7', '#22d3ee', '#34d399', '#fbbf24', '#fb7185',
                   '#60a5fa', '#f97316', '#c084fc', '#2dd4bf', '#facc15', '#f472b6',
                   '#38bdf8', '#a3e635', '#e879f9', '#fda4af'];
  /* 水墨（烟雨远山）图表色：全部是冷调的青灰 / 黛 / 苔，饱和度压低，
     让图表"长"在山雾底色上，而不是浮在上面的一块霓虹。 */
  const PALETTE_INK = ['#2c5f6e', '#4a7a8c', '#7a9e6d', '#8c6d3a', '#6b7f99',
                       '#8c4a52', '#5f8a7a', '#7d939c', '#a3b6a0', '#4a6d7d',
                       '#96a8a0', '#6d8c99', '#b0795e', '#7a8ca3', '#8fa3ad', '#9c8c7a'];
  function palette() {
    return document.documentElement.getAttribute('data-theme') === 'ink'
      ? PALETTE_INK : PALETTE;
  }
  const colorOf = (i, meta) => (meta && meta.color) || palette()[i % palette().length];

  /* 图表里代表「某个工具」的系列色。
     后端给的是厂商品牌色（Codex 绿、DeepSeek 蓝…），这在普通主题下很好，
     但放到水墨主题里就是一排霓虹，和纸墨底色直接冲突。
     所以水墨主题下改用本主题的国画色板，工具身份由位置 + 图例文字承担；
     非水墨主题保持品牌色不变。 */
  function seriesColor(brandColor, i) {
    if (INK()) return palette()[i % palette().length];
    return brandColor || palette()[i % palette().length];
  }

  /* 语义色：图表里「总量 / 输入 / 输出 / 缓存读 / 缓存写 / 成本」这些固定角色
     也不能写死蓝紫——换到水墨主题就会和纸墨底色打架。统一从这里取。 */
  const INK = () => document.documentElement.getAttribute('data-theme') === 'ink';
  const ROLE = {
    total:  () => (INK() ? '#2c5f6e' : '#7c8cf8'),
    input:  () => (INK() ? '#4a7a8c' : '#22d3ee'),
    output: () => (INK() ? '#6b7f99' : '#a855f7'),
    cRead:  () => (INK() ? '#7a9e6d' : '#34d399'),
    cWrite: () => (INK() ? '#8c6d3a' : '#fbbf24'),
    cost:   () => (INK() ? '#5f8a7a' : '#059669'),
    ok:     () => (INK() ? '#4a7a5a' : '#34d399'),
    warn:   () => (INK() ? '#8c6d3a' : '#fbbf24'),
    danger: () => (INK() ? '#8c4a52' : '#fb7185'),
  };

  /* ---------------- SVG 工具 ---------------- */
  const NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs = {}) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const e = Math.pow(10, Math.floor(Math.log10(v)));
    const m = v / e;
    const s = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
    return s * e;
  }

  /* ======================================================================
     趋势面积图
     ====================================================================== */
  function renderTrend(host, daily, metric, rate) {
    host.innerHTML = '';
    if (!daily || !daily.length) { host.innerHTML = '<div class="empty">该时间范围内暂无数据</div>'; return; }

    const W = Math.max(320, host.clientWidth || 640);
    const H = Math.max(190, host.clientHeight || 250);
    const pad = { l: 56, r: 16, t: 24, b: 28 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    const specs = {
      total: [{ k: 'total', name: '总 Token', c: ROLE.total() }],
      io:    [{ k: 'input', name: '输入', c: ROLE.input() }, { k: 'output', name: '输出', c: ROLE.output() }],
      cache: [{ k: 'cache_read', name: '缓存读', c: ROLE.cRead() }, { k: 'cache_write', name: '缓存写', c: ROLE.cWrite() }],
      cost:  [{ k: 'cost', name: isEn() ? `Cost (${window.TLMoney?.currency() || 'USD'})` : `成本(${window.TLMoney?.currency() || 'CNY'})`, c: ROLE.danger() }],
    }[metric] || [{ k: 'total', name: '总 Token', c: ROLE.total() }];

    const n = daily.length;
    let max = 0;
    daily.forEach((d) => specs.forEach((s) => { max = Math.max(max, +d[s.k] || 0); }));
    max = niceMax(max);
    const x = (i) => pad.l + (n === 1 ? iw / 2 : (i * iw) / (n - 1));
    const y = (v) => pad.t + ih - ((+v || 0) / max) * ih;
    // Keep exact daily data for axes/tooltips; only simplify drawn paths. Each
    // bucket retains extrema, so a single expensive day cannot disappear.
    const drawIndices = new Set([0, n - 1]);
    const bucketWidth = Math.max(1, Math.ceil(n / Math.min(320, Math.max(80, Math.floor(iw / 2)))));
    for (let start = 0; start < n; start += bucketWidth) {
      const end = Math.min(n, start + bucketWidth);
      for (const spec of specs) {
        let lo = start, hi = start;
        for (let i = start + 1; i < end; i++) {
          if ((+daily[i][spec.k] || 0) < (+daily[lo][spec.k] || 0)) lo = i;
          if ((+daily[i][spec.k] || 0) > (+daily[hi][spec.k] || 0)) hi = i;
        }
        drawIndices.add(lo); drawIndices.add(hi);
      }
    }
    const drawPoints = Array.from(drawIndices).sort((a, b) => a - b);

    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    const defs = svg('defs');
    specs.forEach((s, i) => {
      const g = svg('linearGradient', { id: 'tg' + i + metric, x1: 0, y1: 0, x2: 0, y2: 1 });
      g.appendChild(svg('stop', { offset: '0%', 'stop-color': s.c, 'stop-opacity': .52 }));
      g.appendChild(svg('stop', { offset: '62%', 'stop-color': s.c, 'stop-opacity': .14 }));
      g.appendChild(svg('stop', { offset: '100%', 'stop-color': s.c, 'stop-opacity': 0 }));
      defs.appendChild(g);
    });
    root.appendChild(defs);

    // 网格
    const grid = svg('g');
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = y(v);
      grid.appendChild(svg('line', { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy,
        stroke: 'currentColor', 'stroke-opacity': i === 0 ? .18 : .075, 'stroke-width': 1 }));
      const t = svg('text', { x: pad.l - 9, y: yy + 3.5, 'text-anchor': 'end',
        'font-size': 10, fill: 'currentColor', 'fill-opacity': .5,
        'font-family': 'ui-monospace,monospace' });
      t.textContent = metric === 'cost' ? (window.TLMoney?.format(v,{precision:2}) || money(v)) : fmt(v);
      grid.appendChild(t);
    }
    grid.setAttribute('color', 'rgba(150,165,200,1)');
    grid.style.color = 'currentColor';
    root.appendChild(grid);

    // 面积 + 线（基础态即最终态，入场只做淡入）
    const drawGrp = svg('g', { class: 'anim-fade' });
    root.appendChild(drawGrp);
    specs.forEach((s, si) => {
      let dline = '';
      drawPoints.forEach((i, point) => {
        const d = daily[i];
        const px = x(i), py = y(d[s.k]);
        dline += (point ? 'L' : 'M') + px.toFixed(1) + ' ' + py.toFixed(1) + ' ';
      });
      const darea = dline + `L${x(n - 1).toFixed(1)} ${pad.t + ih} L${x(0).toFixed(1)} ${pad.t + ih} Z`;
      drawGrp.appendChild(svg('path', { d: darea, fill: `url(#tg${si}${metric})` }));
      drawGrp.appendChild(svg('path', { d: dline, fill: 'none', stroke: s.c, 'stroke-width': 2.1,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    });

    // 底部日期标签
    const step = Math.max(1, Math.ceil(n / 8));
    daily.forEach((d, i) => {
      if (i % step && i !== n - 1) return;
      const t = svg('text', { x: x(i), y: H - 8, 'text-anchor': 'middle', 'font-size': 10,
        fill: 'currentColor', 'fill-opacity': .45 });
      t.textContent = (d.day || '').slice(5);
      root.appendChild(t);
    });

    // 悬停
    const guide = svg('line', { y1: pad.t, y2: pad.t + ih, stroke: 'currentColor',
      'stroke-opacity': .28, 'stroke-dasharray': '3 3', opacity: 0 });
    root.appendChild(guide);
    const dots = specs.map((s) => {
      const c = svg('circle', { r: 4, fill: s.c, stroke: 'var(--bg-0)', 'stroke-width': 2, opacity: 0 });
      root.appendChild(c); return c;
    });
    const hit = svg('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent' });
    root.appendChild(hit);
    hit.addEventListener('mousemove', (e) => {
      const box = root.getBoundingClientRect();
      const rx = ((e.clientX - box.left) / box.width) * W;
      let i = Math.round(((rx - pad.l) / (iw || 1)) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      const d = daily[i];
      guide.setAttribute('x1', x(i)); guide.setAttribute('x2', x(i));
      guide.setAttribute('opacity', 1);
      specs.forEach((s, si) => { dots[si].setAttribute('cx', x(i)); dots[si].setAttribute('cy', y(d[s.k]));
        dots[si].setAttribute('opacity', 1); });
      let html = `<div class="tt-title">${esc(d.day || '')}</div>`;
      specs.forEach((s) => {
        const v = d[s.k] || 0;
        html += `<div class="tt-row"><span>${s.name}</span><b>${metric === 'cost' ? money(v) : fmtExact(v)}</b></div>`;
      });
      html += `<div class="tt-row"><span>调用次数</span><b>${fmtExact(d.requests || 0)}</b></div>`;
      showTip(html, e.clientX, e.clientY);
    });
    hit.addEventListener('mouseleave', () => {
      guide.setAttribute('opacity', 0); dots.forEach((c) => c.setAttribute('opacity', 0)); hideTip();
    });

    host.appendChild(root);
  }

  /* ======================================================================
     环形图（用路径弧绘制，避免 dasharray 在整圆上的歧义）
     ====================================================================== */
  function arcPath(cx, cy, r, a0, a1) {
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    const large = (a1 - a0) > Math.PI ? 1 : 0;
    return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }

  function renderDonut(host, legendHost, items) {
    host.innerHTML = '';
    if (!items || !items.length) { host.innerHTML = '<div class="empty">暂无数据</div>'; return; }
    const size = 196, r = 70, sw = 19, cx = size / 2, cy = size / 2;
    const total = items.reduce((a, b) => a + b.total, 0) || 1;
    const N = items.length;
    const gap = N > 1 ? Math.min(0.03, 0.9 / N) : 0;

    const root = svg('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
    root.appendChild(svg('circle', { cx, cy, r, fill: 'none', stroke: 'currentColor',
      'stroke-opacity': .07, 'stroke-width': sw }));
    const grp = svg('g', { class: 'anim-donut' });
    root.appendChild(grp);

    let a = -Math.PI / 2;
    items.forEach((it, i) => {
      const span = (it.total / total) * Math.PI * 2;
      const a0 = a + gap / 2, a1 = Math.max(a0 + 0.0008, a + span - gap / 2);
      a += span;
      const p = svg('path', {
        d: arcPath(cx, cy, r, a0, a1), fill: 'none', stroke: it.color,
        'stroke-width': sw, 'stroke-linecap': 'butt',
      });
      p.style.cursor = 'default';
      p.style.transition = 'stroke-width .18s ease';
      p.addEventListener('mousemove', (e) => {
        showTip(`<div class="tt-title">${esc(it.name)}</div>
          <div class="tt-row"><span>Token</span><b>${fmtExact(it.total)}</b></div>
          <div class="tt-row"><span>占比</span><b>${(it.total / total * 100).toFixed(2)}%</b></div>
          <div class="tt-row"><span>调用</span><b>${fmtExact(it.requests || 0)}</b></div>`,
          e.clientX, e.clientY);
        p.setAttribute('stroke-width', sw + 5);
      });
      p.addEventListener('mouseleave', () => { hideTip(); p.setAttribute('stroke-width', sw); });
      grp.appendChild(p);
    });

    const h = humanNum(total);
    const t1 = svg('text', { x: cx, y: cy - 8, 'text-anchor': 'middle', 'font-size': 10.5,
      fill: 'currentColor', 'fill-opacity': .5 });
    t1.textContent = isEn() ? 'TOTAL TOKENS' : 'TOKEN 总量';
    const t2 = svg('text', { x: cx, y: cy + 15, 'text-anchor': 'middle', 'font-size': 22,
      'font-weight': 650, fill: 'currentColor', 'font-family': 'ui-monospace,monospace' });
    t2.textContent = h.v + h.u;
    t2.setAttribute('data-countup', '');
    const t3 = svg('text', { x: cx, y: cy + 32, 'text-anchor': 'middle', 'font-size': 10,
      fill: 'currentColor', 'fill-opacity': .45 });
    t3.textContent = N + ' 个来源';
    t3.setAttribute('data-countup', '');
    root.append(t1, t2, t3);
    host.appendChild(root);

    legendHost.innerHTML = '';
    items.forEach((it) => {
      const li = el('div', 'li',
        `<span class="sw" style="background:${it.color}"></span>
         <span>${esc(it.name)}</span><span class="vv">${(it.total / total * 100).toFixed(1)}%</span>`);
      legendHost.appendChild(li);
    });
  }

  /* ======================================================================
     日历热力图
     ====================================================================== */
  function renderHeatmap(host, hm, dataVersion) {
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    // 热力图与时间范围无关。范围切换和首屏二次布局时复用现有 SVG，避免重建
    // 近一年的数百个节点；扫描入库、主题或**界面语言**改变后才重新绘制。
    // 语言必须进缓存键：月份与星期标签是渲染时定下来的（9月 / Sep），
    // 漏掉它就会出现"切了英文但热力图上仍是中文月份"。
    const cacheKey = `${theme}:${S.language}:${dataVersion || ''}:${hm && hm.start || ''}:${hm && hm.end || ''}`;
    if (host._heatmapKey === cacheKey && host.childElementCount) return;
    host.innerHTML = '';
    if (!hm || !hm.days || !hm.days.length) {
      host.innerHTML = `<div class="empty">${isEn() ? 'No data' : '暂无数据'}</div>`;
      return;
    }
    const days = hm.days.slice(-371);
    const cell = 12, gap = 3.5, dowW = 26, monH = 16;
    const weeks = Math.ceil(days.length / 7);
    const W = dowW + weeks * (cell + gap), H = monH + 7 * (cell + gap);
    // 用非零数据的分位数做分级，避免个别极端值把整年压成同一档
    const nz = days.map((d) => d.total).filter((v) => v > 0).sort((a, b) => a - b);
    const q = (p) => (nz.length ? nz[Math.min(nz.length - 1, Math.floor(nz.length * p))] : 0);
    const t1 = q(.34), t2 = q(.62), t3 = q(.84), t4 = q(.955);
    const lv = (v) => {
      if (!v) return 0;
      if (v <= t1) return 1;
      if (v <= t2) return 2;
      if (v <= t3) return 3;
      return 4;
    };
    // 热力图的五档色阶也要跟主题走。水墨主题用「薄雾 → 黛青 → 峰青」
    // 的浓淡表达强度，最高档落一点苔绿——像雨季山色由浅入深；
    // 其余主题沿用蓝紫青。
    const light = theme === 'light' || theme === 'mint' || theme === 'solar';
    const COLORS = theme === 'ink'
      ? ['rgba(62,96,108,.07)', 'rgba(44,95,110,.22)', 'rgba(44,95,110,.42)',
         'rgba(44,95,110,.64)', 'rgba(74,122,90,.85)']
      : (light
        ? ['rgba(120,135,170,.14)', 'rgba(99,102,241,.34)', 'rgba(99,102,241,.58)',
           'rgba(139,92,246,.80)', 'rgba(8,145,178,.92)']
        : ['rgba(140,155,190,.10)', 'rgba(124,140,248,.34)', 'rgba(124,140,248,.58)',
           'rgba(168,85,247,.80)', 'rgba(34,211,238,.95)']);

    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    root.setAttribute('aria-label', '近一年每日 Token 用量热力图');
    const start = new Date(days[0].day + 'T00:00:00');
    const firstDow = start.getDay();
    let lastMonth = -1, lastWeek = -1;
    days.forEach((d, i) => {
      const idx = i + firstDow;
      const wk = Math.floor(idx / 7), dow = idx % 7;
      const x = dowW + wk * (cell + gap), y = monH + dow * (cell + gap);
      const l = lv(d.total);
      const rect = svg('rect', { x, y, width: cell, height: cell, rx: 3, fill: COLORS[l], class: 'hm-cell' });
      if (d.total) rect.dataset.index = String(i);
      root.appendChild(rect);
      const m = new Date(d.day + 'T00:00:00').getMonth();
      if (m !== lastMonth && wk !== lastWeek && dow <= 3) {
        lastMonth = m; lastWeek = wk;
        const t = svg('text', { x, y: monH - 5, class: 'hm-month' });
        // 热力图的月份标签：英文用三字母缩写，中文用「N月」。
        // 这两种写法长度差别大，所以标签宽度也跟着语言走。
        t.textContent = isEn()
          ? ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m]
          : (m + 1) + '月';
        root.appendChild(t);
      }
    });
    // 热力图的星期标签：中文用「日一二…」，英文用三字母缩写。
    (isEn() ? ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
            : ['日', '一', '二', '三', '四', '五', '六']).forEach((w, i) => {
      if (i % 2) return;
      const t = svg('text', { x: dowW - 7, y: monH + i * (cell + gap) + cell - 2,
        'text-anchor': 'end', class: 'hm-dow' });
      t.textContent = w;
      root.appendChild(t);
    });
    // 只在 SVG 根节点监听一次。此前每个格子都注册 mousemove，并在 hover 时缩放，
    // 缩放会让指针离开原命中区，造成 tooltip 与格子连续闪烁。
    let activeIndex = '';
    root.addEventListener('pointermove', (e) => {
      const index = e.target && e.target.dataset ? e.target.dataset.index : undefined;
      if (index == null) {
        activeIndex = '';
        hideTip();
        return;
      }
      if (index === activeIndex) return;
      activeIndex = index;
      const d = days[Number(index)];
      showTip(`<div class="tt-title">${esc(d.day)}</div>
        <div class="tt-row"><span>Token</span><b>${fmtExact(d.total)}</b></div>
        <div class="tt-row"><span>调用</span><b>${fmtExact(d.requests || 0)}</b></div>
        <div class="tt-row"><span>成本</span><b>${money(d.cost || 0)}</b></div>`, e.clientX, e.clientY);
    }, { passive: true });
    root.addEventListener('pointerleave', () => { activeIndex = ''; hideTip(); }, { passive: true });
    const wrap = el('div', 'hm-wrap');
    wrap.appendChild(root);
    host.appendChild(wrap);
    host._heatmapKey = cacheKey;
  }

  /* ======================================================================
     横向排行
     ====================================================================== */
  function renderRank(host, items, opt) {
    host.innerHTML = '';
    if (!items || !items.length) { host.innerHTML = '<div class="empty">暂无数据</div>'; return; }
    const max = Math.max(1, ...items.map((i) => i.total));
    items.forEach((it, i) => {
      const c = it.color || palette()[i % palette().length];
      const row = el('div', 'rank-row');
      row.innerHTML = `
        <div class="rank-badge" style="background:${c}">${i < 3 ? (i + 1) : ''}</div>
        <div class="rank-main">
          <div class="rank-top">
            <span class="rank-name">${esc(it.name)}</span>
            <span class="rank-val">${fmt(it.total)}</span>
          </div>
          <div class="rank-track"><div class="rank-fill" style="width:${Math.max(2, (it.total / max) * 100)}%;
            animation-delay:${i * 45}ms;background:linear-gradient(90deg,${c},${c}88)"></div></div>
          <div class="rank-sub">
            ${opt && opt.sub ? opt.sub(it) : ''}
          </div>
        </div>
        <div style="font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right">
          ${it.share != null ? it.share.toFixed(1) + '%' : ''}
        </div>`;
      host.appendChild(row);
      if (opt && opt.tip) {
        row.addEventListener('mousemove', (e) => showTip(opt.tip(it), e.clientX, e.clientY));
        row.addEventListener('mouseleave', hideTip);
      }
    });
  }

  /* ======================================================================
     时段柱状图
     ====================================================================== */
  function renderHours(host, hourly) {
    host.innerHTML = '';
    const data = Array.from({ length: 24 }, (_, h) => {
      const f = (hourly || []).find((x) => x.hour === h);
      return { h, total: f ? f.total : 0, requests: f ? f.requests : 0 };
    });
    const W = Math.max(260, host.clientWidth || 320), H = 200;
    const pad = { l: 34, r: 8, t: 12, b: 22 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const max = niceMax(Math.max(1, ...data.map((d) => d.total)));
    const bw = iw / 24;
    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    const defs = svg('defs');
    const g1 = svg('linearGradient', { id: 'hg', x1: 0, y1: 0, x2: 0, y2: 1 });
    g1.appendChild(svg('stop', { offset: '0%', 'stop-color': ROLE.input(), 'stop-opacity': .95 }));
    g1.appendChild(svg('stop', { offset: '100%', 'stop-color': ROLE.total(), 'stop-opacity': .35 }));
    defs.appendChild(g1); root.appendChild(defs);

    for (let i = 0; i <= 3; i++) {
      const v = (max / 3) * i, yy = pad.t + ih - (v / max) * ih;
      root.appendChild(svg('line', { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy,
        stroke: 'currentColor', 'stroke-opacity': i === 0 ? .18 : .07 }));
      const t = svg('text', { x: pad.l - 6, y: yy + 3.5, 'text-anchor': 'end', 'font-size': 9.5,
        fill: 'currentColor', 'fill-opacity': .45, 'font-family': 'ui-monospace,monospace' });
      t.textContent = fmt(v); root.appendChild(t);
    }
    data.forEach((d, i) => {
      const hh = Math.max(d.total ? 2.5 : 0, (d.total / max) * ih);
      const x = pad.l + i * bw + bw * .18;
      const r = svg('rect', { x, y: pad.t + ih - hh, width: bw * .64, height: hh,
        rx: 3, fill: 'url(#hg)', class: 'bar-anim' });
      r.style.animationDelay = (i * 20) + 'ms';
      root.appendChild(r);
      r.addEventListener('mousemove', (e) => showTip(
        `<div class="tt-title">${String(d.h).padStart(2, '0')}:00 - ${String(d.h).padStart(2, '0')}:59</div>
         <div class="tt-row"><span>Token</span><b>${fmtExact(d.total)}</b></div>
         <div class="tt-row"><span>调用</span><b>${fmtExact(d.requests)}</b></div>`, e.clientX, e.clientY));
      r.addEventListener('mouseleave', hideTip);
      if (i % 3 === 0) {
        const t = svg('text', { x: pad.l + i * bw + bw / 2, y: H - 6, 'text-anchor': 'middle',
          'font-size': 9.5, fill: 'currentColor', 'fill-opacity': .45 });
        t.textContent = isEn() ? (i + ':00') : (i + '时'); root.appendChild(t);
      }
    });
    host.appendChild(root);
  }

  /* ======================================================================
     迷你趋势线
     ====================================================================== */
  function sparkline(values, color, w = 74, h = 26) {
    if (!values || !values.length) return '';
    const max = values.reduce((maximum, value) => Math.max(maximum, +value || 0), 1);
    const n = values.length;
    // A tiny chart should not carry years of path coordinates. Retain each
    // bucket's low/high values and the endpoints, including isolated spikes.
    const indices = new Set([0, n - 1]);
    const bucket = Math.max(1, Math.ceil(n / Math.min(80, Math.max(8, Math.floor(w)))));
    for (let start = 0; start < n; start += bucket) {
      let low = start, high = start;
      for (let i = start + 1; i < Math.min(n, start + bucket); i++) {
        if (values[i] < values[low]) low = i;
        if (values[i] > values[high]) high = i;
      }
      indices.add(low); indices.add(high);
    }
    let d = '';
    Array.from(indices).sort((a, b) => a - b).forEach((i, point) => {
      const x = n === 1 ? w / 2 : (i * w) / (n - 1);
      const y = h - (values[i] / max) * (h - 3) - 1.5;
      d += (point ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1) + ' ';
    });
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none"
      stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" opacity=".85"/></svg>`;
  }

  /* ======================================================================
     总览视图
     ====================================================================== */
  function renderOverview() {
    renderOverviewStatus();
    renderKpis();
    renderCharts();
  }

  function renderOverviewStatus() {
    if (!window.TLOverview || !S.data) return;
    window.TLOverview.render(S.data, S.scanState, isEn(), (action) => {
      if (action === 'scan') startScan(false);
      else if (action === 'all') $('#rangeSeg button[data-range="all"]').click();
      else if (action === 'quality' || action === 'prices') {
        switchView('manage');
        if (window.TLManage && window.TLManage.selectTab) window.TLManage.selectTab(action === 'quality' ? 'quality' : 'prices');
      } else switchView(action);
    });
  }

  function renderKpis() {
    const D = S.data; if (!D) return;
    const s = D.summary, rate = D.config.usd_to_cny;
    const metric = (v, unit = '') => {
      const h = humanNum(v);
      return h.v + h.u + `<span class="unit">${unit}</span>`;
    };
    const en = isEn();
    // KPI 的角落辉光同样跟随主题：水墨下用花青 / 朱砂 / 石绿，不用霓虹蓝紫。
    const kpis = [
      { label: en ? 'Total tokens' : '总 Token 消耗', val: s.total, format: (v) => metric(v), glow: ROLE.total(),
        sub: `<span>${en ? 'Exact' : '精确'} ${fmtExact(s.total)}</span>` },
      { label: en ? 'Daily average' : '日均消耗', val: s.avg_per_day, format: (v) => metric(v), glow: ROLE.output(),
        sub: `<span>${en ? `Active ${s.active_days} days` : `活跃 ${s.active_days} 天`}</span><span>·</span><span>${fmtExact(s.avg_per_request)}${en ? ' / call' : ' / 次'}</span>` },
      { label: en ? 'Model calls' : '模型调用次数', val: s.requests, format: (v) => metric(v, en ? '' : '次'), glow: ROLE.input(),
        sub: `<span>${en ? `${s.sessions} sessions` : `${s.sessions} 个会话`}</span><span>·</span><span>${en ? `${s.models} models` : `${s.models} 个模型`}</span>` },
      { label: en ? 'Recorded cost' : '记录费用', val: s.cost, format: (v) => money(v, rate), glow: ROLE.cRead(),
        sub: `<span>${en?'Actual':'实付'} ${money(s.actual_cost || 0,rate)} / ${en?'Estimated':'估算'} ${money(s.estimated_cost || 0,rate)}</span><span>·</span><span>${s.unknown_records==null?(en?'See cost coverage in Cost management':'完整性见成本管理'):window.TLMoney?.coverage(s) || ''}</span>` },
    ];
    const countUp = motionEnabled();
    const kpiHost = $('#kpis'); kpiHost.innerHTML = '';
    kpis.forEach((k) => {
      const node = el('div', 'kpi');
      node.innerHTML = `
        <div class="glow" style="--gc:${k.glow}"></div>
        <div class="label">${k.label}</div>
        <div class="value" data-count="${k.val}">${k.format(countUp ? 0 : k.val)}</div>
        <div class="sub">${k.sub}</div>`;
      kpiHost.appendChild(node);
      if (countUp) animateKpiValue(node.querySelector('.value'), k.val, k.format);
    });
    if (s.delta != null) {
      const chip = el('span', 'delta ' + (s.delta >= 0 ? 'up' : 'down'), pct(s.delta));
      chip.title = '与上一个等长周期相比';
      kpiHost.children[0].querySelector('.sub').appendChild(chip);
    }
  }

  /* 单日区间（今天 / 自定义同一天）专属：把一天拆成 24 个小时看。
     按天聚合在"今天"这种区间上只会得到一根柱子，看不出几点在忙。 */
  function renderIntraday(D) {
    const card = $('#intradayCard'); if (!card) return;
    const intra = D.intraday || {};
    const show = !!D.range_is_single_day && (intra.buckets || []).length > 0;
    card.hidden = !show;
    if (!show) return;

    const all = intra.buckets;
    // 后端统一给 30 分钟粒度。
    // ⚠️ 按小时看时必须把两个半小时桶**合并**，不能只挑 minute===0 的那些：
    // 过滤会把每小时的后半小时整段丢掉，于是"按小时"的总量只有真实值的一半
    // （实测 166M vs 322M）。这个 bug 会让副标题的合计和柱高同时偏小。
    const srcBm = intra.bucket_minutes || 30;
    const bm = S.intradayBucket === 30 ? 30 : 60;
    let buckets = all;
    if (bm === 60 && srcBm < 60) {
      const per = Math.round(60 / srcBm);
      buckets = [];
      for (let i = 0; i < all.length; i += per) {
        const head = all[i];
        if (!head) continue;
        const merged = Object.assign({}, head, { minute: 0, label: head.label });
        for (let k = 1; k < per; k++) {
          const nx = all[i + k];
          if (!nx) continue;
          merged.total += nx.total;
          merged.input += nx.input;
          merged.output += nx.output;
          merged.cache_read += nx.cache_read;
          merged.requests += nx.requests;
          merged.cost += nx.cost;
        }
        buckets.push(merged);
      }
    }
    const host = $('#intradayChart');
    const total = buckets.reduce((a, b) => a + b.total, 0);
    $('#intradaySub').textContent = isEn()
      ? `${rangeLabel(D)} · by ${bm === 30 ? 'half hour' : 'hour'} · ${fmtExact(total)} tokens`
      : `${D.range_label} · 按${bm === 30 ? '半小时' : '小时'}统计 · 共 ${fmtExact(total)} tokens`;
    // 顶部小结：最忙的时段、峰值、活跃时段数
    const active = buckets.filter((b) => b.total > 0);
    const peak = active.reduce((a, b) => (b.total > (a ? a.total : -1) ? b : a), null);
    const stats = [];
    if (peak) {
      stats.push(`<div class="intraday-stat hot"><span>${isEn() ? 'Busiest slot' : '最忙时段'}</span><b>${esc(peak.label)}</b></div>`);
      stats.push(`<div class="intraday-stat"><span>${isEn() ? 'Peak usage' : '峰值用量'}</span><b>${fmtExact(peak.total)}</b></div>`);
    }
    stats.push(`<div class="intraday-stat"><span>${isEn() ? 'Active slots' : '活跃时段'}</span><b>${active.length} / ${buckets.length}</b></div>`);
    stats.push(`<div class="intraday-stat"><span>${isEn() ? 'Calls' : '调用次数'}</span><b>${fmtExact(buckets.reduce((a, b) => a + b.requests, 0))}</b></div>`);
    $('#intradayStats').innerHTML = stats.join('');

    host.innerHTML = '';
    if (!active.length) {
      host.innerHTML = `<div class="empty">${isEn() ? 'No usage recorded today' : '今天还没有用量记录'}</div>`;
      return;
    }

    const W = Math.max(320, host.clientWidth || 900);
    const H = Math.max(180, host.clientHeight || 230);
    const pad = { l: 58, r: 14, t: 18, b: 30 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const max = niceMax(Math.max.apply(null, buckets.map((b) => b.total)) || 1);
    const n = buckets.length;
    const slot = iw / n;
    const bw = Math.max(4, slot * 0.62);
    const y = (v) => pad.t + ih - ((+v || 0) / max) * ih;

    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    const defs = svg('defs');
    const gid = 'igrad' + bm;
    const grad = svg('linearGradient', { id: gid, x1: 0, y1: 1, x2: 0, y2: 0 });
    grad.appendChild(svg('stop', { offset: '0%', 'stop-color': ROLE.total(), 'stop-opacity': .32 }));
    grad.appendChild(svg('stop', { offset: '100%', 'stop-color': ROLE.input(), 'stop-opacity': .95 }));
    defs.appendChild(grad);
    root.appendChild(defs);

    const grid = svg('g');
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = y(v);
      grid.appendChild(svg('line', { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy,
        stroke: 'currentColor', 'stroke-opacity': i === 0 ? .18 : .07, 'stroke-width': 1 }));
      const t = svg('text', { x: pad.l - 9, y: yy + 3.5, 'text-anchor': 'end',
        'font-size': 10, fill: 'currentColor', 'fill-opacity': .5 });
      t.textContent = fmt(v);
      grid.appendChild(t);
    }
    root.appendChild(grid);

    const bars = svg('g', { class: 'anim-fade' });
    buckets.forEach((b, i) => {
      const x = pad.l + slot * i + (slot - bw) / 2;
      const h = Math.max(b.total > 0 ? 2 : 0, pad.t + ih - y(b.total));
      if (h <= 0) return;
      const r = svg('rect', {
        x: x.toFixed(1), y: (pad.t + ih - h).toFixed(1),
        width: bw.toFixed(1), height: h.toFixed(1), rx: Math.min(3, bw / 3),
        fill: `url(#${gid})`,
      });
      r.style.cursor = 'default';
      r.addEventListener('mousemove', (e) => showTip(
        `<div class="tt-title">${b.label} – ${nextLabel(b, bm)}</div>
        <div class="tt-row"><span>Token 合计</span><b>${fmtExact(b.total)}</b></div>
        <div class="tt-row"><span>输入</span><b>${fmtExact(b.input)}</b></div>
        <div class="tt-row"><span>输出</span><b>${fmtExact(b.output)}</b></div>
        <div class="tt-row"><span>缓存读</span><b>${fmtExact(b.cache_read)}</b></div>
        <div class="tt-row"><span>调用次数</span><b>${fmtExact(b.requests)}</b></div>`,
        e.clientX, e.clientY));
      r.addEventListener('mouseleave', hideTip);
      bars.appendChild(r);
    });
    root.appendChild(bars);

    // x 轴：按粒度决定标注密度，避免 24/48 根柱子标签重叠
    const step = bm === 30 ? 6 : 3;
    buckets.forEach((b, i) => {
      if (b.minute !== 0 || b.hour % step !== 0) return;
      const t = svg('text', {
        x: (pad.l + slot * i + slot / 2).toFixed(1), y: H - 9,
        'text-anchor': 'middle', 'font-size': 10, fill: 'currentColor', 'fill-opacity': .55 });
      t.textContent = `${String(b.hour).padStart(2, '0')}:00`;
      root.appendChild(t);
    });
    host.appendChild(root);
  }

  function nextLabel(b, bm) {
    const m = b.hour * 60 + b.minute + bm;
    return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
  }

  function renderCharts() {
    const D = S.data; if (!D) return;
    const rate = D.config.usd_to_cny, s = D.summary;

    $('#trendSub').textContent = `${rangeLabel(D)}${isEn() ? ' · by day' : ' · 按天统计'}`;
    renderTrend($('#trendChart'), D.daily, S.metric, rate);
    renderIntraday(D);

    const top6 = D.tools.slice(0, 6);
    const other = D.tools.slice(6).reduce((a, b) => a + b.total, 0);
    const donutItems = top6.map((t, i) => ({ name: t.name, total: t.total, share: t.share,
      color: seriesColor(t.color, i), requests: t.requests }));
    if (other > 0) {
      const grand = D.summary.total || 1;
      donutItems.push({ name: '其他工具', total: other, share: (other * 100) / grand,
        color: INK() ? '#96a8a0' : '#94a3b8', requests: 0 });
    }
    renderDonut($('#donutChart'), $('#donutLegend'), donutItems);

    renderHeatmap($('#heatmap'), D.heatmap, D.data_version);
    $('#heatSub').textContent = `近一年每日用量强度 · 共 ${fmtExact(s.total)} tokens`;

    renderRank($('#toolRank'), D.tools.slice(0, 8).map((t, i) => ({
      name: t.name, total: t.total, share: t.share, color: seriesColor(t.color, i),
      sub: isEn() ? `${fmtExact(t.requests)} calls · ${t.sessions} sessions · ${money(t.cost, rate)}` : `${fmtExact(t.requests)} 次调用 · ${t.sessions} 会话 · ${money(t.cost, rate)}`,
      raw: t,
    })), {
      sub: (it) => it.sub ? `<span>${esc(it.sub)}</span>` : '',
      tip: (it) => `<div class="tt-title">${esc(it.name)}</div>
        <div class="tt-row"><span>Token 合计</span><b>${fmtExact(it.total)}</b></div>
        <div class="tt-row"><span>输入</span><b>${fmtExact(it.raw.input)}</b></div>
        <div class="tt-row"><span>输出</span><b>${fmtExact(it.raw.output)}</b></div>
        <div class="tt-row"><span>缓存读</span><b>${fmtExact(it.raw.cache_read)}</b></div>
        <div class="tt-row"><span>记录费用</span><b>${money(it.raw.cost, D.config.usd_to_cny)}</b></div>`,
    });

    renderRank($('#modelRank'), D.models.slice(0, 8).map((m, i) => ({
      name: m.model, total: m.total, share: m.share, color: palette()[(i + 3) % palette().length],
      sub: isEn() ? `${fmtExact(m.requests)} calls · ${(m.tool_list || []).length} tools` : `${fmtExact(m.requests)} 次 · ${(m.tool_list || []).length} 个工具`,
      raw: m,
    })), {
      sub: (it) => `<span>${esc(it.sub)}</span>`,
      tip: (it) => `<div class="tt-title">${esc(it.name)}</div>
        <div class="tt-row"><span>Token</span><b>${fmtExact(it.total)}</b></div>
        <div class="tt-row"><span>输出</span><b>${fmtExact(it.raw.output)}</b></div>
        <div class="tt-row"><span>记录费用</span><b>${money(it.raw.cost, D.config.usd_to_cny)}</b></div>
        <div class="tt-row"><span>来源工具</span><b>${esc((it.raw.tool_list || []).join(', '))}</b></div>`,
    });

    renderHours($('#hourChart'), D.hourly);

    renderRank($('#projectRank'), D.projects.slice(0, 8).map((p, i) => ({
      name: p.project, total: p.total, share: p.share, color: palette()[(i + 6) % palette().length],
      sub: isEn() ? `${(p.tool_list || []).length} tools · ${p.sessions} sessions · last ${dd(p.last_ts)}` : `${(p.tool_list || []).length} 个工具 · ${p.sessions} 个会话 · 最近 ${dd(p.last_ts)}`,
      raw: p,
    })), {
      sub: (it) => `<span>${esc(it.sub)}</span>`,
      tip: (it) => `<div class="tt-title">${esc(it.name)}</div>
        <div class="tt-row"><span>Token</span><b>${fmtExact(it.total)}</b></div>
        <div class="tt-row"><span>记录费用</span><b>${money(it.raw.cost, D.config.usd_to_cny)}</b></div>
        <div class="tt-row"><span>工具</span><b>${esc((it.raw.tool_list || []).join(', '))}</b></div>`,
    });
  }

  const MOTION_MEDIA = (() => {
    try { return matchMedia('(prefers-reduced-motion: reduce)'); } catch (_) { return null; }
  })();

  // A render starts all its visible metrics together. Rows outside the viewport
  // keep their final values; scrolling never starts a second wave of counters.
  const NUMBER_MOTION_SELECTORS = [
    '[data-countup]', '.countup-value', '.kpi .value', '.kpi .sub',
    '.tool-total', '.tool-meta b', '.tool-foot>span:not(.go)', '.dkpi .value',
    '.cmp-num', '.cmp-delta', '.cmp-name>span:last-child', '.rank-val', '.rank-sub',
    '.rank-row>div:last-child', '.turn-total', '.turn-meta', '.intraday-stat b',
    '.lg-total', '.lg-sub b', '.ic-value', '.ic-desc', '.cs-row b', '.src-kv b', '.src-kpi b',
    '.tbl td.num', '.manage-stats article>b', '.manage-stats article>small',
    '.manage-preview>p', '#budgetMeta .rowline b', '#budgetMeta .rowline>span:last-child',
    '#heatSub', '.legend .vv',
  ].join(',');
  const NUMBER_MOTION_DURATION = 240;
  function motionEnabled() {
    return !(MOTION_MEDIA && MOTION_MEDIA.matches) && readBoolPref('motion', true);
  }
  const numberMotionNodes = new WeakMap(), numberMotionTexts = new WeakMap();
  const activeNumberMotions = new Set(), numberMotionBatches = new WeakMap();
  const numberMotionFormats = new Map();
  let numberMotionFrame = 0;

  function numberMotionFormatter(decimals, grouped) {
    const locale = isEn() ? 'en-US' : 'zh-CN';
    const precision = Math.min(20, decimals), key = `${locale}:${precision}:${grouped}`;
    if (!numberMotionFormats.has(key)) {
      if (numberMotionFormats.size >= 64) numberMotionFormats.delete(numberMotionFormats.keys().next().value);
      numberMotionFormats.set(key, new Intl.NumberFormat(locale, {
        minimumFractionDigits:precision, maximumFractionDigits:precision, useGrouping:grouped,
      }));
    }
    return numberMotionFormats.get(key);
  }

  function numberScope(node) {
    return {view:node.closest('.view'), drawer:node.closest('.drawer'), panel:node.closest('.iq-panel')};
  }
  function numberScopeActive(node, scope = numberScope(node)) {
    return node.isConnected && (!scope.view || scope.view.classList.contains('active')) &&
      (!scope.drawer || scope.drawer.classList.contains('on')) &&
      (!scope.panel || scope.panel.classList.contains('active'));
  }
  function finishNumberMotion(motion, completed = true) {
    if (motion.status === 'running') motion.finish();
    motion.status = completed ? 'done' : 'cancelled';
    motion.node.dataset.numberMotion = motion.status;
    motion.signature = motion.node.textContent;
    activeNumberMotions.delete(motion);
  }
  function pruneNumberMotions() {
    for (const motion of activeNumberMotions) {
      if (!numberScopeActive(motion.node, motion.scope)) finishNumberMotion(motion, false);
    }
  }
  function registerNumberMotion(node, render, finish, parts, scope = numberScope(node), visible = true) {
    const previous = numberMotionNodes.get(node);
    if (previous && activeNumberMotions.has(previous)) finishNumberMotion(previous, false);
    const batchRoot = scope.panel || scope.drawer || scope.view || node;
    let batch = numberMotionBatches.get(batchRoot);
    if (!batch || (batch.start != null && performance.now() - batch.start >= NUMBER_MOTION_DURATION)) {
      batch = {start:null}; numberMotionBatches.set(batchRoot, batch);
    }
    const motion = {node, render, finish, parts, scope, batch, status:visible ? 'running' : 'done', signature:node.textContent};
    numberMotionNodes.set(node, motion);
    if (parts) parts.forEach((part) => numberMotionTexts.set(part.node, {motion, text:part.text}));
    node.dataset.numberMotion = motion.status;
    if (!visible || !numberScopeActive(node, scope)) { motion.status = 'done'; node.dataset.numberMotion = 'done'; return; }
    activeNumberMotions.add(motion);
    motion.render(0);
    scheduleNumberMotions();
  }
  function runNumberMotions(now) {
    numberMotionFrame = 0;
    if (uiHidden() || !motionEnabled()) { stopNumberMotions(); return; }
    for (const motion of activeNumberMotions) {
      if (!numberScopeActive(motion.node, motion.scope)) { finishNumberMotion(motion, false); continue; }
      if (motion.batch.start == null) motion.batch.start = now;
      const p = Math.min(1, (now - motion.batch.start) / NUMBER_MOTION_DURATION);
      if (p === 1) finishNumberMotion(motion);
      else motion.render(1 - Math.pow(1 - p, 3));
    }
    scheduleNumberMotions();
  }
  function scheduleNumberMotions() {
    if (!numberMotionFrame && activeNumberMotions.size) {
      numberMotionFrame = requestAnimationFrame(runNumberMotions);
    }
  }
  function stopNumberMotions() {
    if (numberMotionFrame) cancelAnimationFrame(numberMotionFrame);
    numberMotionFrame = 0;
    for (const motion of activeNumberMotions) finishNumberMotion(motion, false);
  }
  function syncNumberMotions() {
    if (uiHidden() || !motionEnabled()) stopNumberMotions();
    else pruneNumberMotions();
  }
  function numericTextParts(node) {
    const parts = [];
    function visit(parent) {
      for (const child of parent.childNodes || []) {
        if (child.nodeType === 1) {
          if (!child.matches('input,textarea,select,option,code,pre,time,[data-no-countup]')) visit(child);
          continue;
        }
        if (child.nodeType !== 3) continue;
        const text = child.nodeValue || '', owner = numberMotionTexts.get(child);
        if (owner && (activeNumberMotions.has(owner.motion) || (owner.motion.status === 'done' && owner.text === text))) continue;
        const protectedRanges = Array.from(text.matchAll(/\b\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|\b\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}月(?:\d{1,2}日)?|\d{1,2}日/g),
          (match) => [match.index, match.index + match[0].length]);
        const segments = [];
        for (const match of text.matchAll(/[-+]?\d+(?:,\d{3})*(?:\.\d+)?/g)) {
          const start = match.index, end = start + match[0].length;
          // Model IDs, version strings, dates and times are labels, not metrics.
          if (protectedRanges.some(([a,b]) => start < b && end > a)) continue;
          if (/[A-Za-z0-9_]/.test(text[start - 1] || '')) continue;
          const tail = text.slice(end);
          if (/^[A-Za-z_]/.test(tail) && !/^(?:[KMB]|ms|s|tokens?)\b/.test(tail)) continue;
          const raw = match[0].replace(/,/g, ''), target = Number(raw);
          if (!Number.isFinite(target)) continue;
          const decimals = (raw.split('.')[1] || '').length;
          const formatter = numberMotionFormatter(decimals, match[0].includes(','));
          const plus = raw[0] === '+';
          segments.push({start, end, target, format:(value) => (plus ? '+' : '') + formatter.format(value === 0 ? 0 : value)});
        }
        if (!segments.length || !segments.some((segment) => segment.target !== 0)) continue;
        parts.push({node:child, text, segments});
      }
    }
    visit(node);
    return parts;
  }
  function animateKpiValue(node, target, format) {
    node.innerHTML = format(target);
    if (!motionEnabled() || uiHidden()) return;
    registerNumberMotion(node, (progress) => { node.innerHTML = format(target * progress); },
      () => { node.innerHTML = format(target); });
  }
  function animateNumericElements(root, replay = false) {
    if (!root || !root.querySelectorAll) return;
    pruneNumberMotions();
    if (!motionEnabled() || uiHidden()) { stopNumberMotions(); return; }
    const nodes = Array.from(root.querySelectorAll(NUMBER_MOTION_SELECTORS));
    if (root.matches && root.matches(NUMBER_MOTION_SELECTORS)) nodes.unshift(root);
    const prepared = [], clipRects = new Map(), groupRects = new Map(), claimed = new Set();
    const excludedGroups = new Set(), belowViewportTables = new Set();
    if (replay) {
      const scope = numberScope(root);
      numberMotionBatches.delete(scope.panel || scope.drawer || scope.view || root);
    }
    // Complete all geometry reads before changing any numeric text. This is a
    // single render-time visibility snapshot, never a scroll listener/observer.
    const viewport = {top:0, left:0, right:window.innerWidth || Infinity, bottom:window.innerHeight || Infinity};
    for (const node of nodes) {
      if (node.closest('input,textarea,select,option,code,pre,time,[data-no-countup],thead')) continue;
      const scope = numberScope(node);
      if (!numberScopeActive(node, scope)) continue;
      const group = node.closest('tbody tr,.tool-card,.src-card,.kpi,.ic-card');
      if(group && excludedGroups.has(group))continue;
      const tbody = group?.parentElement?.tagName === 'TBODY' ? group.parentElement : null;
      if (tbody && belowViewportTables.has(tbody)) continue;
      const clip=node.closest('.table-wrap') || node.closest('.drawer-body') || node.closest('.scroll');
      if(clip && !clipRects.has(clip))clipRects.set(clip,clip.getBoundingClientRect());
      const bounds=clip ? clipRects.get(clip) : viewport;
      if(group && !groupRects.has(group))groupRects.set(group,group.getBoundingClientRect());
      const rect=group ? groupRects.get(group) : node.getBoundingClientRect();
      // Rows are vertically ordered. Measuring every offscreen row defeats
      // content-visibility and forces layout of the entire page of records.
      if (tbody && rect.top >= Math.min(viewport.bottom,bounds.bottom)) {
        belowViewportTables.add(tbody);continue;
      }
      const visible=rect.width>0 && rect.height>0 &&
        rect.bottom>Math.max(viewport.top,bounds.top) && rect.top<Math.min(viewport.bottom,bounds.bottom) &&
        rect.right>Math.max(viewport.left,bounds.left) && rect.left<Math.min(viewport.right,bounds.right);
      if(!visible){if(group)excludedGroups.add(group);continue;}
      const previous = numberMotionNodes.get(node);
      if (previous && (activeNumberMotions.has(previous) || (!replay && previous.status === 'done' && previous.signature === node.textContent))) continue;
      let render, finish, parts;
      if (previous && previous.signature === node.textContent &&
          (!previous.parts || previous.parts.every(part => part.node.isConnected && node.contains(part.node)))) {
        ({render, finish, parts} = previous);
        if (parts && parts.some(part => claimed.has(part.node))) continue;
      } else {
        parts = numericTextParts(node).filter(part => !claimed.has(part.node));
        if (!parts.length) continue;
        render = (progress) => parts.forEach((part) => {
          let value = '', from = 0;
          for (const segment of part.segments) {
            value += part.text.slice(from, segment.start) + segment.format(segment.target * progress);
            from = segment.end;
          }
          part.node.nodeValue = value + part.text.slice(from);
        });
        finish = () => parts.forEach((part) => { part.node.nodeValue = part.text; });
      }
      if (parts) parts.forEach(part => claimed.add(part.node));
      prepared.push({node, render, finish, parts, scope, visible});
    }
    // Overlapping selectors have already claimed their text during the read
    // phase, so each nested currency/token value gets only one writer.
    for (const item of prepared) {
      registerNumberMotion(item.node, item.render, item.finish, item.parts, item.scope, item.visible);
    }
  }
  // manage.js shares this short, single-rAF batch with the other views.
  window.TLMotion = { animate: animateNumericElements, replay:root => animateNumericElements(root, true), sync:syncNumberMotions };
  document.addEventListener('visibilitychange', syncNumberMotions);
  if (MOTION_MEDIA && MOTION_MEDIA.addEventListener) MOTION_MEDIA.addEventListener('change', syncNumberMotions);

  /* ======================================================================
     工具视图
     ====================================================================== */
  function renderTools() {
    const D = S.data, host = $('#toolsGrid');
    const cards = [];
    const rate = D.config.usd_to_cny;
    const trend = D.tool_trend || { tools: [], series: {} };
    const seriesDays = (D.daily || []).map((d) => d.day);

    D.tools.forEach((t, i) => {
      const card = el('div', 'tool-card');
      const vals = trend.series[t.tool]
        ? seriesDays.map((d) => (trend.series[t.tool] || {})[d] || 0) : [];
      // 卡片强调色统一走 seriesColor：非水墨主题保留厂商品牌色，
      // 水墨主题换成国画色，避免整页出现与纸墨冲突的霓虹色。
      const accent = INK() ? seriesColor(t.color, i) : t.color;
      card.innerHTML = `
        <div class="tc-glow" style="--gc:${accent}"></div>
        <div class="tool-head">
          <div class="tool-glyph">${window.TLSourceIcons?window.TLSourceIcons.html(t.tool,t.glyph):esc(t.glyph||'?')}</div>
          <div style="flex:1;min-width:0">
            <div class="tool-name">${esc(toolName(t, t.tool || t.name))}</div>
            <div class="tool-vendor">${esc(isEn() && t.vendor_en ? t.vendor_en : (t.vendor || ''))}${(() => {
              const cat = (isEn() && t.category_label_en) ? t.category_label_en : t.category_label;
              return cat ? ' · ' + cat : '';
            })()}</div>
          </div>
          ${sparkline(vals, accent)}
        </div>
        <div class="tool-total">${fmt(t.total)}<span class="unit">tokens</span></div>
        <div class="tool-meta">
          <div>${isEn() ? 'Input' : '输入'}<b>${fmt(t.input)}</b></div>
          <div>${isEn() ? 'Output' : '输出'}<b>${fmt(t.output)}</b></div>
          <div>${isEn() ? 'Cache read' : '缓存读'}<b>${fmt(t.cache_read)}</b></div>
        </div>
        <div class="tool-meta" style="margin-top:9px">
          <div>${isEn() ? 'Calls' : '调用'}<b>${fmtExact(t.requests)}</b></div>
          <div>${isEn() ? 'Sessions' : '会话'}<b>${fmtExact(t.sessions)}</b></div>
          <div>${isEn() ? 'Recorded cost' : '记录费用'}<b>${money(t.cost, rate)}</b></div>
        </div>
        <div class="tool-foot">
          <span>${isEn() ? 'share ' : '占比 '}${t.share.toFixed(2)}%</span>
          <span class="chip"><span class="sw" style="background:${t.color}"></span>${isEn() ? `${t.models} models` : `${t.models} 个模型`}</span>
          <span class="go" style="color:var(--accent);font-weight:600">${isEn() ? 'View records →' : '查看流水 →'}</span>
        </div>`;
      card.title = isEn() ? 'Click to view this tool’s records' : '点击查看该工具的调用流水';
      card.onclick = () => {
        navigateWithContext('records', {tool:t.tool,model:'',project:'',query:'',project_paths:''});
      };
      cards.push(card);
    });
    if (!cards.length) host.innerHTML = '<div class="empty">该时间范围内暂无工具用量</div>';
    else chunkMount(host, cards, undefined, () => animateNumericElements(host));
  }

  /* ======================================================================
     表格
     ====================================================================== */
  const tablePages = new Map();

  function tablePage(key) {
    const scope = S.range + ':' + ((S.data || {}).tool || '');
    let state = tablePages.get(key);
    if (!state) {
      state = {page: 1, size: 20, request: 0};
      tablePages.set(key, state);
    }
    if (state.scope !== scope) {
      state.scope = scope; state.page = 1; state.cacheKey = null;
    }
    return state;
  }

  function drawPager(host, {total, size, page, count, onPage, onSize}) {
    const pages = Math.max(1, Math.ceil(total / size));
    host.innerHTML = '';
    host.setAttribute('role', 'navigation');
    host.setAttribute('aria-label', isEn() ? 'Table pagination' : '表格分页');
    const first = total ? (page - 1) * size + 1 : 0;
    host.append(el('span', 'pager-info', isEn()
      ? `${fmtExact(first)}–${fmtExact(first ? first + count - 1 : 0)} of ${fmtExact(total)} records`
      : `第 ${fmtExact(first)}–${fmtExact(first ? first + count - 1 : 0)} 条 / 共 ${fmtExact(total)} 条`));
    const sizeLabel = el('label', 'pager-size', isEn() ? 'Rows' : '每页');
    const select = el('select', 'input sm');
    select.setAttribute('aria-label', isEn() ? 'Rows per page' : '每页条数');
    [...new Set([20, 50, 100, 200, size])].sort((a, b) => a - b).forEach(value => {
      const option = el('option', '', String(value));
      option.value = String(value); option.selected = value === size; select.append(option);
    });
    select.onchange = () => onSize(Number(select.value));
    sizeLabel.append(select); host.append(sizeLabel);
    const controls = el('div', 'pager-pages');
    const button = (label, target, disabled, current = false) => {
      const node = el('button', current ? 'btn btn-primary pager-page' : 'btn btn-ghost pager-page', label);
      node.type = 'button'; node.disabled = disabled;
      if (current) node.setAttribute('aria-current', 'page');
      node.onclick = () => onPage(target); return node;
    };
    controls.append(button(isEn() ? '‹ Previous' : '‹ 上一页', page - 1, page <= 1));
    const marks = pages <= 7 ? Array.from({length: pages}, (_, i) => i + 1)
      : [...new Set([1, pages, page - 1, page, page + 1])].filter(p => p >= 1 && p <= pages).sort((a, b) => a - b);
    let last = 0;
    for (const target of marks) {
      if (target - last > 1) controls.append(el('span', 'pager-gap', '…'));
      const node = button(String(target), target, false, target === page);
      node.title = isEn() ? `Go to page ${target}` : `跳到第 ${target} 页`;
      controls.append(node); last = target;
    }
    controls.append(button(isEn() ? 'Next ›' : '下一页 ›', page + 1, page >= pages));
    host.append(controls);
    if (pages > 1) {
      const jump = el('form', 'pager-jump');
      const input = el('input', 'input sm pager-jump-input');
      input.type = 'number'; input.min = '1'; input.max = String(pages); input.value = String(page);
      input.setAttribute('aria-label', isEn() ? 'Jump to page' : '跳转到页码');
      const label = el('label', '', isEn() ? 'Page ' : '跳至 '); label.append(input);
      jump.append(label, el('span', 'pager-summary', `/ ${pages}`));
      const submit = el('button', 'btn btn-ghost', isEn() ? 'Go' : '跳转'); submit.type = 'submit'; jump.append(submit);
      jump.onsubmit = event => {
        event.preventDefault();
        const value = Math.trunc(Number(input.value));
        if (Number.isFinite(value)) onPage(Math.min(pages, Math.max(1, value)));
      };
      host.append(jump);
    }
  }

  function mountTablePagers(table, state, total, count, render) {
    const wrap = table.parentElement;
    let top = document.getElementById(table.id + 'PagerTop');
    let bottom = document.getElementById(table.id + 'Pager');
    if (!top) { top = el('div', 'pager pager-top'); top.id = table.id + 'PagerTop'; wrap.before(top); }
    if (!bottom) { bottom = el('div', 'pager'); bottom.id = table.id + 'Pager'; wrap.after(bottom); }
    top.hidden = false;
    const go = value => {
      if (value === state.page) return;
      state.page = value; state.scroll = true; render();
    };
    const size = value => { state.size = value; state.page = 1; state.scroll = true; render(); };
    for (const host of [top, bottom]) {
      drawPager(host, {total, size: state.size, page: state.page, count, onPage: go, onSize: size});
    }
    bottom.hidden = total <= state.size;
    if (state.scroll) { top.scrollIntoView({block: 'start'}); state.scroll = false; }
    if (window.TLResponsive) window.TLResponsive.apply(table);
    animateNumericElements(table);
    if (table.id !== 'ledgerTable') restoreScroll();
    saveCurrentNavigation();
  }

  async function rankingPage(kind) {
    const state = tablePage(kind), epoch = S.epoch;
    const key = `${epoch}:${state.page}:${state.size}`;
    if (state.cacheKey === key) return state;
    const request = ++state.request;
    const table = $('#' + ({models: 'modelTable', projects: 'projectTable', sessions: 'sessionTable'}[kind]));
    table.setAttribute('aria-busy', 'true');
    const params = new URLSearchParams({kind, range: S.range, limit: state.size, offset: (state.page - 1) * state.size});
    if ((S.data || {}).tool) params.set('tool', S.data.tool);
    try {
      const response = await api('/api/rankings?' + params);
      if (request !== state.request || epoch !== S.epoch) return null;
      state.rows = response.rows; state.total = response.total;
      state.size = response.limit; state.page = Math.floor(response.offset / state.size) + 1;
      state.cacheKey = `${epoch}:${state.page}:${state.size}`;
      return state;
    } catch (error) {
      if (request === state.request && epoch === S.epoch) {
        delete S.rendered[kind];
        toast((isEn() ? 'List failed: ' : '列表加载失败：') + error.message, 'err');
        table.innerHTML = `<tbody><tr><td class="mut">${isEn() ? 'Unable to load this list. Open the page again to retry.' : '列表加载失败，请重新打开页面重试。'}</td></tr></tbody>`;
        for (const suffix of ['PagerTop', 'Pager']) {
          const pager = document.getElementById(table.id + suffix); if (pager) pager.hidden = true;
        }
      }
      return null;
    } finally {
      if (request === state.request) table.removeAttribute('aria-busy');
    }
  }

  async function renderModelTable() {
    const page = await rankingPage('models'); if (!page) return;
    const D = S.data, rate = D.config.usd_to_cny;
    const t = $('#modelTable');
    const max = D.models.reduce((value, model) => Math.max(value, model.total), 1);
    const shown = page.rows;
    let html = `<thead><tr>
      <th>${isEn() ? 'Model' : '模型'}</th><th class="num">${isEn() ? 'Total tokens' : 'Token 合计'}</th>
      <th class="num">${isEn() ? 'Input' : '输入'}</th><th class="num">${isEn() ? 'Output' : '输出'}</th>
      <th class="num">${isEn() ? 'Cache read' : '缓存读'}</th><th class="num">${isEn() ? 'Calls' : '调用'}</th>
      <th class="num" title="${isEn() ? 'Known costs only; unpriced records are excluded.' : '仅含已计价金额，未定价记录不计入。'}">${isEn() ? 'Known cost' : '已计价费用'}</th>
      <th style="width:130px">${isEn() ? 'Share' : '占比'}</th>
      <th>${isEn() ? 'Source tools' : '来源工具'}</th><th>${isEn()?'Actions':'操作'}</th></tr></thead><tbody>`;
    shown.forEach((m,i) => {
      html += `<tr>
        <td class="ell"><b>${esc(m.model)}</b></td>
        <td class="num">${fmtExact(m.total)}</td>
        <td class="num mut">${fmtExact(m.input)}</td>
        <td class="num mut">${fmtExact(m.output)}</td>
        <td class="num mut">${fmtExact(m.cache_read)}</td>
        <td class="num mut">${fmtExact(m.requests)}</td>
        <td class="num">${money(m.cost, rate)}</td>
        <td><div class="bar-mini"><i style="width:${(m.total / max) * 100}%;background:linear-gradient(90deg,${ROLE.total()},${ROLE.output()})"></i></div>
            <span class="mut" style="font-size:10.5px">${m.share.toFixed(2)}%</span></td>
        <td class="ell mut">${esc((m.tool_list || []).join(', '))}</td><td>${contextRowActions('model',i)}</td></tr>`;
    });
    if (!shown.length) html += `<tr><td colspan="10" class="mut">${isEn() ? 'No models in this date range.' : '当前时间范围内没有模型用量。'}</td></tr>`;
    t.innerHTML = html + '</tbody>';
    bindContextRowActions(t,shown,'model');
    mountTablePagers(t, page, page.total, shown.length, renderModelTable);
  }

  function contextRowActions(kind,index) {
    return `<div class="row-context-actions"><button type="button" class="btn btn-ghost btn-xs" data-context-row="${index}" data-context-target="records">${isEn()?'View records':'查看明细'}</button>`+
      (featureEnabled('nav_assistant')?`<button type="button" class="btn btn-ghost btn-xs" data-context-row="${index}" data-context-target="assistant">${isEn()?'Analyze':'分析'}</button>`:'')+'</div>';
  }
  function bindContextRowActions(table, rows, kind) {
    table.onclick=event=>{
      const button=event.target.closest('[data-context-row]');if(!button)return;
      const row=rows[Number(button.dataset.contextRow)];if(!row)return;
      navigateWithContext(button.dataset.contextTarget,{model:kind==='model'?row.model:'',
        project:kind==='project'?row.project:'',tool:S.data?.tool || '',query:'',project_paths:'',from:'',to:''});
    };
  }

  /* ---------------- 计费明细 ----------------
     把「估算成本」一个数字展开成可核对的账单：单价（USD / 百万 token）
     × 用量 = 金额，四类计费项分别列出，再给一行小计。这样用户能自己
     验算，也能看出钱主要花在输入、输出还是缓存上。 */
  async function renderLedger() {
    const D = S.data, t = $('#ledgerTable');
    if (!t) return;
    const page = tablePage('ledger'), epoch = S.epoch;
    let L = D.ledger;
    const total = Number(L?.total ?? L?.items?.length ?? 0);
    page.page = Math.min(page.page, Math.max(1, Math.ceil(total / page.size)));
    if (L?.paged) {
      const key = `${epoch}:${page.page}:${page.size}`;
      if (page.cacheKey !== key) {
        const request = ++page.request;
        t.setAttribute('aria-busy', 'true');
        const params = new URLSearchParams({range:S.range, limit:page.size, offset:(page.page - 1) * page.size});
        if (D.tool) params.set('tool', D.tool);
        try {
          const result = await api('/api/ledger?' + params);
          if (request !== page.request || epoch !== S.epoch) return;
          L = D.ledger = result;
          page.page = Math.floor(result.offset / result.limit) + 1;
          page.size = result.limit;
          page.cacheKey = `${epoch}:${page.page}:${page.size}`;
        } catch (error) {
          if (request === page.request && epoch === S.epoch) {
            delete S.rendered.ledger;
            toast((isEn() ? 'Ledger failed: ' : '计费明细加载失败：') + error.message, 'err');
          }
          return;
        } finally {
          if (request === page.request) t.removeAttribute('aria-busy');
        }
      }
    }
    if (!L || !L.items || !L.items.length) {
      t.innerHTML = '<tbody><tr><td class="mut" style="text-align:center;padding:26px 12px">当前时间范围内没有可计费的调用记录</td></tr></tbody>';
      const n0 = $('#ledgerNote'); if (n0) n0.textContent = '';
      page.page = 1;
      mountTablePagers(t, page, 0, 0, renderLedger);
      return;
    }
    const cny = S.ledgerCur === 'cny';
    const rate = L.totals.rate || D.config.usd_to_cny || 7.2;
    // 单价用固定小数位，避免 0.075 被显示成 0.08 而无法核对
    const price = (v) => {
      if (v == null) return '—';
      const n = cny ? v * rate : v;
      if (!n) return cny ? '¥0' : '$0';
      const s = n >= 1 ? n.toFixed(2) : (n >= 0.01 ? n.toFixed(3) : n.toFixed(4));
      return (cny ? '¥' : '$') + s;
    };
    const amt = (v) => {
      if (v == null) return '—';
      const n = cny ? v * rate : v;
      if (!n) return cny ? '¥0.00' : '$0.00';
      if (n < 0.005) return (cny ? '¥' : '$') + n.toFixed(4);
      return (cny ? '¥' : '$') + n.toFixed(2);
    };
    // 每一行是可展开的：主行给模型与小计，子行拆四个计费项
    const tModel = isEn() ? 'Model' : '模型';
    const tCalls = isEn() ? 'Calls' : '调用';
    const tInput = isEn() ? 'Input' : '输入';
    const tOutput = isEn() ? 'Output' : '输出';
    const tRead = isEn() ? 'Cache read' : '缓存读';
    const tWrite = isEn() ? 'Cache write' : '缓存写';
    const tSub = isEn() ? 'Subtotal' : '小计';
    const fExpr = isEn() ? 'unit price × usage' : '单价 × 用量';
    let html = `<thead><tr>
      <th>${tModel}</th>
      <th class="num">${tCalls}</th>
      <th class="num" title="${tInput} ${isEn() ? 'unit price / usage' : '单价 / 用量'}">${tInput} <span class="lg-k">${fExpr}</span></th>
      <th class="num">${tOutput} <span class="lg-k">${fExpr}</span></th>
      <th class="num">${tRead} <span class="lg-k">${fExpr}</span></th>
      <th class="num">${tWrite} <span class="lg-k">${fExpr}</span></th>
      <th class="num">${tSub}</th>
    </tr></thead><tbody>`;
    const ledgerItems = L.paged ? L.items : L.items.slice((page.page - 1) * page.size, page.page * page.size);
    ledgerItems.forEach((it) => {
      const p = it.price, b = it.tokens_by, a = it.amount;
      // 未收录的模型在别处一律按 0 计入成本（estimate_cost 的行为）。
      // 账单上如果写成"$1.00 × 500,000 = $0.00"，算术就是错的，所以这里
      // 明确显示"未计价"，保证本页合计与总览的估算成本始终对得上。
      const known = it.priced, partial = !!it.partial_price, tiered = !!it.tiered_price;
      // 手动单价：价目表跟不上厂商调价 / 中转站价格不同，允许逐模型覆盖。
      // AI 助手核验过的参考价同样存在 price_versions 里，但它不是手工价，
      // 标签必须区分开，否则用户会以为自己填过这个价。
      const custom = it.custom_price;
      const sourced = custom && String(it.price_source || '').startsWith('assistant-');
      const warn = tiered ? `<span class="lg-custom" title="${isEn()?'Rates are weighted across request pricing tiers':'每次请求按上下文或时段匹配价格，表内为加权单价'}">${isEn()?'Tiered price':'分档价格'}</span>` : partial ? `<span class="lg-warn" title="${esc(it.price_source||'')}">${isEn()?'Partial price':'部分单价'}</span>` : known ? (custom
        ? `<span class="lg-custom" title="${esc(it.price_source || '')}">${sourced
            ? (isEn() ? 'Reference price' : '核验参考价')
            : (isEn() ? 'Custom' : '自定义价')}</span>`
        : '') : `<span class="lg-warn" title="${isEn()
          ? 'This model is not in the price list, so it is excluded from the estimated cost'
          : '价目表未收录该模型，未计入估算成本'}">${isEn() ? 'Not priced' : '未计价'}</span>`;
      const pv = (v) => (known || partial ? price(v) : '—');
      const av = (v) => (known || partial ? amt(v) : '—');
      html += `<tr class="lg-row">
        <td><button class="lg-edit" data-model="${esc(it.model)}" title="${isEn()
            ? 'Edit unit price for this model'
            : '编辑该模型的单价'}"><b>${esc(it.model)}</b></button>${warn}
          <div class="lg-total">${isEn() ? 'Total' : '合计'} ${fmtExact(it.tokens)} tokens</div></td>
        <td class="num mut">${fmtExact(it.requests)}</td>
        <td class="num"><span class="lg-p">${pv(p.input)}</span><span class="lg-x">×</span>
          <span class="mut">${fmtExact(b.input)}</span><span class="lg-eq">=</span>
          <b>${av(a.input)}</b></td>
        <td class="num"><span class="lg-p">${pv(p.output)}</span><span class="lg-x">×</span>
          <span class="mut">${fmtExact(b.output)}</span><span class="lg-eq">=</span>
          <b>${av(a.output)}</b></td>
        <td class="num"><span class="lg-p">${pv(p.cache_read)}</span><span class="lg-x">×</span>
          <span class="mut">${fmtExact(b.cache_read)}</span><span class="lg-eq">=</span>
          <b>${av(a.cache_read)}</b></td>
        <td class="num"><span class="lg-p">${pv(p.cache_write)}</span><span class="lg-x">×</span>
          <span class="mut">${fmtExact(b.cache_write)}</span><span class="lg-eq">=</span>
          <b>${av(a.cache_write)}</b></td>
        <td class="num lg-sub"><b>${known||partial ? (cny ? '¥' + it.subtotal_cny.toFixed(2) : '$' + it.subtotal.toFixed(2)) : (isEn()?'Unknown':'未知')}</b>${!known&&partial?`<small>${isEn()?'Known rates only':'仅已知费项'}</small>`:''}</td>
      </tr>`;
    });
    // 合计覆盖完整计费数据，与当前选页无关。
    const T = L.totals;
    const R = L.reconciliation;
    html += `<tr class="lg-foot">
      <td><b>${isEn() ? 'Total' : '合计'}</b><div class="lg-total">${fmtExact(T.tokens)} tokens · ${fmtExact(T.requests)}${isEn() ? ' calls' : ' 次调用'}</div></td>
      <td class="num mut">${fmtExact(T.requests)}</td>
      <td class="num"><b>${amt(T.input)}</b></td>
      <td class="num"><b>${amt(T.output)}</b></td>
      <td class="num"><b>${amt(T.cache_read)}</b></td>
      <td class="num"><b>${amt(T.cache_write)}</b></td>
      <td class="num lg-sub"><b>${cny ? '¥' + T.cost_cny.toFixed(2) : '$' + T.cost.toFixed(2)}</b></td>
    </tr>`;
    // 对账行：明细是按价目表重算的，总览是账单里记录的实际花费，
    // 两者不等时必须说清楚差在哪，否则用户会以为算错了。
    if (R && Math.abs(R.delta) >= 0.01) {
      const it_ = cny ? '¥' + (R.itemized * rate).toFixed(2) : '$' + R.itemized.toFixed(2);
      const su = cny ? '¥' + (R.summary_cost * rate).toFixed(2) : '$' + R.summary_cost.toFixed(2);
      const dl = cny ? '¥' + (R.delta * rate).toFixed(2) : '$' + R.delta.toFixed(2);
      html += `<tr class="lg-recon"><td colspan="7">
        <i class="ico i-info"></i>${isEn()
          ? `This table uses the current price list: <b>${it_}</b>. The overview’s recorded cost
             keeps each record’s collected amount: <b>${su}</b>; difference <b>${dl}</b>.
             Actual bills, past price versions, subscriptions or reseller discounts can differ.
             See Cost management for cost types and unpriced usage.`
          : `本表按价目表逐项重算：<b>${it_}</b>；
             总览「记录费用」保留每条记录采集时的金额：<b>${su}</b>；差额 <b>${dl}</b>。
             实际账单、历史价格、订阅或中转折扣均可能产生差异；费用类型及未计价用量见成本管理。`}
      </td></tr>`;
    }
    html += '</tbody>';
    t.innerHTML = html;
    mountTablePagers(t, page, L.total ?? L.items.length, ledgerItems.length, renderLedger);
    const note = $('#ledgerNote');
    if (note) {
      const base = (isEn() && L.note_en) ? L.note_en : L.note;
      note.textContent = isEn()
        ? `${base} Exchange rate: 1 USD = ${rate.toFixed(2)} CNY.`
          + (T.unpriced ? ` ${T.unpriced} models are not in the price list (${fmtExact(T.unpriced_tokens)} tokens); their usage is not included in the amounts above.` : '')
        : `${base}　汇率 1 USD = ${rate.toFixed(2)} CNY。`
          + (T.unpriced ? `其中 ${T.unpriced} 个模型不在价目表内（共 ${fmtExact(T.unpriced_tokens)} tokens），其用量未列入上方金额。` : '');
    }
    const sub = $('#ledgerSub');
    if (sub) {
      sub.textContent = `${rangeLabel(S.data)} · ${isEn()
        ? `unit price is ${cny?'CNY':'USD'} per million tokens · current-price estimate`
        : `单价为「${cny?'人民币':'美元'} / 百万 token」 · 按现价估算`}`;
    }
    // 单价编辑入口：点模型名打开编辑框。事件挂在表上（一次绑定，
    // 行内容每次重渲染都会重建）。
    t.onclick = (e) => {
      const btn = e.target.closest('button.lg-edit');
      if (btn) openPriceEditor(btn.dataset.model);
    };
  }

  /* ---------------- 手动单价编辑 ----------------
     内置价目表会过时，中转站价格也和官牌不同。这里允许逐模型覆盖
     （输入 / 输出 / 缓存读 / 缓存写，单位 美元/百万 token），保存到
     data_dir/custom_prices.json，立即重算账单。 */
  async function openPriceEditor(model) {
    if ($('#priceEditorMask')) return;
    const en = isEn();
    const L = (S.data && S.data.ledger) || {};
    const item = (L.items || []).find((x) => x.model === model);
    // 分档/条件价模型在明细里的 price 是「按本区间用量加权」的结果，
    // 拿它当目录价回写会把均价变成新的固定价。有 base_price 时优先用它。
    const cur = (item && (item.base_price || item.price))
      || { input: '', output: '', cache_read: '', cache_write: '' };
    const isCustom = !!(item && item.custom_price);
    const tieredNote = item && item.tiered_price
      ? (item.pricing_segments || []).map((s) => {
          const lo = fmtExact(s.context_min), hi = s.context_max == null ? '∞' : fmtExact(s.context_max);
          const r = s.prices || [];
          const rp = (v) => (v == null ? '—' : '$' + v);
          return `<li>${lo} – ${hi} tokens：${rp(r[0])} / ${rp(r[1])} / ${rp(r[2])} / ${rp(r[3])}</li>`;
        }).join('') : '';
    const F = (label, key) => `
      <label class="field"><span>${label}</span>
        <input class="input sm" id="pe_${key}" type="number" step="0.000001" min="0"
          value="${cur[key] == null ? '' : cur[key]}" placeholder="—"></label>`;
    const mask = document.createElement('div');
    mask.className = 'modal-mask on';
    mask.id = 'priceEditorMask';
    mask.innerHTML = `
      <section class="modal" role="dialog" aria-modal="true" style="width:min(560px,calc(100vw - 32px))">
        <header>
          <div><p class="pl-kicker">PRICING</p>
            <h3>${en ? 'Edit unit price' : '编辑模型单价'}</h3></div>
          <button class="icon-btn" id="peClose"><i class="ico i-close"></i></button>
        </header>
        <p class="pl-config-intro">${en
          ? 'Prices are USD per million tokens. Saving creates a version effective today for newly collected calls. Historical costs are retained.'
          : '单价单位为「美元 / 百万 token」。保存后建立今日生效版本，用于后续新采集；历史金额保留。'}</p>
        <div style="display:flex;align-items:center;gap:9px;margin:4px 0 10px">
          <code style="font-size:12px;padding:4px 9px;background:var(--panel-3);border-radius:7px">${esc(model)}</code>
          ${isCustom ? `<span class="lg-custom" title="${esc(item.price_source || '')}">${String(item.price_source || '').startsWith('assistant-')
            ? (en ? 'Verified reference price active' : '当前为核验参考价')
            : (en ? 'Custom price active' : '当前为自定义价')}</span>` : ''}
          ${tieredNote ? `<span class="lg-custom" title="${en ? 'Rates are matched per request; the table shows usage-weighted rates' : '每次请求按上下文或时段匹配价格，表内为加权单价'}">${en ? 'Tiered price' : '分档价格'}</span>` : ''}
        </div>
        ${tieredNote ? `<div class="ledger-note" style="margin:0 0 10px">
          <b>${en ? 'Published rates for this model' : '该模型已发布的分档单价'}</b>
          <ul style="margin:6px 0 0;padding-left:18px">${tieredNote}</ul>
          <span>${en
            ? 'A fixed price saved here replaces every tier and applies to all new calls.'
            : '此处保存的是固定单价，会覆盖以上全部分档并适用于之后的所有调用。'}</span>
        </div>` : ''}
        <div class="settings-fields">
          ${F(en ? 'Input (uncached)' : '输入（未命中缓存）', 'input')}
          ${F(en ? 'Output' : '输出', 'output')}
          ${F(en ? 'Cache read' : '缓存读', 'cache_read')}
          ${F(en ? 'Cache write' : '缓存写', 'cache_write')}
        </div>
        <div class="pl-actions" style="margin-top:14px">
          <button class="btn btn-primary" id="peSave"><i class="ico i-check"></i>${en ? 'Save price version' : '保存价格版本'}</button>
          ${isCustom ? `<button class="btn btn-ghost btn-danger" id="peReset">${en ? 'Remove custom price' : '恢复内置价'}</button>` : ''}
          <button class="btn btn-ghost" id="peCancel">${en ? 'Cancel' : '取消'}</button>
        </div>
        <p class="ledger-note" style="margin-top:10px">${en
          ? 'Blank fields mean 0. Use the rates on your provider bill. Manage effective dates and compare estimates in Cost management.'
          : '留空按 0 计，请按供应商账单填写。在成本管理中可维护生效日期并比较估算结果。'}</p>
      </section>`;
    document.body.appendChild(mask);
    let saving = false;
    const close = () => { if (!saving) mask.remove(); };
    const lock = value => { saving = value; mask.querySelectorAll('button,input').forEach(node => node.disabled = value); };
    mask.onclick = (e) => { if (e.target === mask) close(); };
    $('#peClose').onclick = close;
    $('#peCancel').onclick = close;
    const num = (id) => {
      const v = $(id).value.trim();
      if (!v) return 0;
      const f = Number(v);
      if (!Number.isFinite(f) || f < 0) return NaN;
      return f;
    };
    $('#peSave').onclick = async () => {
      if (saving) return;
      const prices = [num('#pe_input'), num('#pe_output'), num('#pe_cache_read'), num('#pe_cache_write')];
      if (prices.some((v) => Number.isNaN(v))) {
        return toast(en ? 'Prices must be non-negative numbers' : '单价必须是非负数字', 'err');
      }
      lock(true);
      try {
        await api('/api/prices/set', { method: 'POST', body: { model, price: prices } });
        toast(en ? 'Price version saved; historical costs retained' : '价格版本已保存，历史金额保留', 'ok');
        mask.remove();
        clearDashboardCache();
        invalidateViews();
        await load(true, true);
      } catch (e2) {
        toast((e2 && e2.message) || (en ? 'Save failed' : '保存失败'), 'err', 5000);
      } finally { lock(false); }
    };
    const reset = $('#peReset');
    if (reset) reset.onclick = async () => {
      if (saving) return;
      lock(true);
      try {
        await api('/api/prices/remove', { method: 'POST', body: { model } });
        toast(en ? 'Custom price removed; using built-in table' : '已恢复内置价目表', 'ok');
        mask.remove();
        clearDashboardCache();
        invalidateViews();
        await load(true, true);
      } catch (e2) {
        toast((e2 && e2.message) || (en ? 'Operation failed' : '操作失败'), 'err', 5000);
      } finally { lock(false); }
    };
    $('#pe_input').focus();
  }

  async function renderProjectTable() {
    const page = await rankingPage('projects'); if (!page) return;
    const D = S.data, t = $('#projectTable');
    const max = D.projects.reduce((value, project) => Math.max(value, project.total), 1);
    const shown = page.rows;
    let html = `<thead><tr><th>${isEn() ? 'Project directory' : '项目目录'}</th>
      <th class="num">${isEn() ? 'Total tokens' : 'Token 合计'}</th><th class="num">${isEn() ? 'Input' : '输入'}</th>
      <th class="num">${isEn() ? 'Output' : '输出'}</th><th class="num" title="${isEn() ? 'Known costs only; unpriced records are excluded.' : '仅含已计价金额，未定价记录不计入。'}">${isEn() ? 'Known cost' : '已计价费用'}</th>
      <th class="num">${isEn() ? 'Sessions' : '会话'}</th>
      <th style="width:130px">${isEn() ? 'Share' : '占比'}</th><th>${isEn() ? 'Tools' : '工具'}</th>
      <th>${isEn() ? 'Last active' : '最近活跃'}</th><th>${isEn()?'Actions':'操作'}</th></tr></thead><tbody>`;
    shown.forEach((p,i) => {
      html += `<tr>
        <td class="ell"><b>${esc(p.project)}</b></td>
        <td class="num">${fmtExact(p.total)}</td>
        <td class="num mut">${fmtExact(p.input)}</td>
        <td class="num mut">${fmtExact(p.output)}</td>
        <td class="num">${money(p.cost, D.config.usd_to_cny)}</td>
        <td class="num mut">${p.sessions}</td>
        <td><div class="bar-mini"><i style="width:${(p.total / max) * 100}%;background:linear-gradient(90deg,${ROLE.input()},${ROLE.cRead()})"></i></div>
            <span class="mut" style="font-size:10.5px">${p.share.toFixed(2)}%</span></td>
        <td class="ell mut">${esc((p.tool_list || []).join(', '))}</td>
        <td class="mut">${dd(p.last_ts)}</td><td>${contextRowActions('project',i)}</td></tr>`;
    });
    if (!shown.length) html += `<tr><td colspan="10" class="mut">${isEn() ? 'No projects in this date range.' : '当前时间范围内没有项目用量。'}</td></tr>`;
    t.innerHTML = html + '</tbody>';
    bindContextRowActions(t,shown,'project');
    mountTablePagers(t, page, page.total, shown.length, renderProjectTable);
  }

  async function renderSessionTable() {
    const page = await rankingPage('sessions'); if (!page) return;
    const D = S.data, t = $('#sessionTable');
    const meta = D.tool_meta || {};
    t.classList.add('sessions-table');
    let html=`<colgroup><col style="width:25%"><col style="width:26%"><col style="width:24%"><col style="width:15%"><col style="width:10%"></colgroup><thead><tr>
      <th>${isEn()?'Session / tool / models':'会话 / 工具 / 模型'}</th><th>${isEn()?'Project':'项目'}</th>
      <th>${isEn()?'Usage breakdown':'用量拆分'}</th><th title="${isEn() ? 'Known costs only; unpriced records are excluded.' : '仅含已计价金额，未定价记录不计入。'}">${isEn()?'Total / known cost':'合计 / 已计价费用'}</th>
      <th>${isEn()?'Date range':'起止日期'}</th></tr></thead><tbody>`;
    page.rows.forEach((s, i) => {
      const m = meta[s.tool] || {};
      const fields=[['input',isEn()?'Input':'输入'],['output',isEn()?'Output':'输出'],
        ['cache_read',isEn()?'Cache read':'缓存读'],['requests',isEn()?'Calls':'调用']];
      html+=`<tr class="clickable" tabindex="0" data-session-index="${i}" title="${isEn()?'Open session details':'查看会话详情'}">
        <td class="session-identity"><code>${esc(s.session || '—')}</code><span class="chip"><span class="sw" style="background:${m.color || ROLE.total()}"></span>${esc(toolName(m,s.tool))}</span>
          <div class="session-models" data-no-countup>${esc((s.model_list || '').split(',').filter(Boolean).join(' · '))}</div></td>
        <td class="session-project">${esc(s.project || '—')}</td>
        <td class="session-usage"><div>${fields.map(([key,label])=>`<span class="session-metric"><span>${label}</span><b data-countup>${fmtExact(s[key])}</b></span>`).join('')}</div></td>
        <td class="session-totals"><strong data-countup>${fmtExact(s.total)}</strong><span data-countup>${money(s.cost,D.config.usd_to_cny)}</span></td>
        <td class="session-window" data-no-countup><time>${esc(s.start_day)}</time><span>→</span><time>${esc(s.end_day)}</time></td></tr>`;
    });
    if (!page.rows.length) html += `<tr><td colspan="5" class="mut">${isEn() ? 'No sessions in this date range.' : '当前时间范围内没有会话用量。'}</td></tr>`;
    t.innerHTML = html + '</tbody>';
    const open=(event)=>{const row=event.target.closest('tr[data-session-index]');const s=row && page.rows[Number(row.dataset.sessionIndex)];if(s)openSession(s.session,s.tool,s.project);};
    t.onclick=open;t.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open(event);}};
    mountTablePagers(t, page, page.total, page.rows.length, renderSessionTable);
  }

  async function renderRecords() {
    const request = ++S.recRequest;
    const epoch = S.epoch;
    if (S.recordsAbort) S.recordsAbort.abort();
    const controller = new AbortController();
    S.recordsAbort = controller;
    const q = new URLSearchParams({ range: S.range, limit: String(recPage()),
      offset: String(S.recOffset), order: S.recOrder });
    q.set('cost_basis',currentContext().cost_basis);
    if (S.recTool) q.set('tool', S.recTool);
    if (S.recProject) q.set('project', S.recProject);
    if (S.recProjectPaths) q.set('project_paths', S.recProjectPaths);
    if (S.recModel) q.set('model', S.recModel);
    if (S.query) q.set('q', S.query);
    // 明细页的时间筛选：只在与当前 range 不冲突时发出去（后端也会兜底校验）
    const from = (S.recFrom || '').trim(), to = (S.recTo || '').trim();
    if (from) q.set('from', from);
    if (to) q.set('to', to);
    const qkey = q.toString();
    let r = S.recCache[qkey];
    if (!r) {
      try { r = await api('/api/records?' + qkey, {signal: controller.signal}); }
      catch (error) {
        if (error.name !== 'AbortError' && request === S.recRequest) toast((isEn() ? 'Records failed: ' : '明细加载失败：') + error.message, 'err');
        return;
      } finally { if (S.recordsAbort === controller) S.recordsAbort = null; }
      if (request !== S.recRequest || epoch !== S.epoch || S.view !== 'records') return;
      // 只缓存「当前页」；逐出最旧一页而不是整表清空，翻页往返不反复回源
      const cacheKeys = Object.keys(S.recCache);
      if (cacheKeys.length >= 6) delete S.recCache[cacheKeys[0]];
      S.recCache[qkey] = r;
    }
    if (request !== S.recRequest || epoch !== S.epoch || S.view !== 'records') return;
    const renderStarted = performance.now();
    const t = $('#recordTable');
    let context = $('#recContext');
    if (!context) {
      context = el('div', 'rec-context'); context.id = 'recContext';
      t.parentElement.before(context);
    }
    context.hidden = !S.recProject && !S.recModel && !S.recProjectPaths;
    context.innerHTML = `<span>${esc([S.recProject, S.recModel, S.recProjectPaths ? JSON.parse(S.recProjectPaths).join(', ') : ''].filter(Boolean).join(' · '))}</span><button class="btn btn-ghost btn-xs" type="button">${isEn() ? 'Clear extra filters' : '清除项目/模型筛选'}</button>`;
    context.querySelector('button').onclick = () => { S.recProject = ''; S.recModel = ''; S.recProjectPaths = ''; S.recOffset = 0; renderNavigationContext(); renderRecords(); };
    const meta = (S.data && S.data.tool_meta) || {};
    const C = S.recColumns;
    const labels = {time:isEn()?'Time':'时间',tool:isEn()?'Tool / model':'工具 / 模型',
      project:isEn()?'Project / source':'项目 / 来源',usage:isEn()?'Usage breakdown':'用量拆分',
      totals:isEn()?'Tokens / cost':'合计 / 费用'};
    const hasUsage = C.input || C.output || C.cache_read || C.cache_write;
    const groups = [['time',11],['tool',21],...(C.project || C.source ? [['project',24]] : []),
      ...(hasUsage ? [['usage',28]] : []),...(C.total || C.cost ? [['totals',16]] : [])];
    const weight = groups.reduce((sum,group)=>sum+group[1],0);
    t.style.removeProperty('min-width');
    t.classList.add('records-table');
    let html = `<colgroup>${groups.map(([,width])=>`<col style="width:${width/weight*100}%">`).join('')}</colgroup>` +
      `<thead><tr>${groups.map(([key])=>`<th scope="col">${labels[key]}</th>`).join('')}</tr></thead><tbody>`;
    r.rows.forEach((x, i) => {
      const m = meta[x.tool] || {};
      const usage = [['input',isEn()?'Input':'输入'],['output',isEn()?'Output':'输出'],
        ['cache_read',isEn()?'Cache read':'缓存读'],['cache_write',isEn()?'Cache write':'缓存写']]
        .filter(([key])=>C[key]).map(([key,label])=>`<span class="rec-usage-item"><span>${label}</span><b data-countup>${fmtExact(x[key])}</b></span>`);
      if (C.output && x.reasoning) usage.push(`<span class="rec-usage-item"><span>${isEn()?'Reasoning':'推理'}</span><b data-countup>${fmtExact(x.reasoning)}</b></span>`);
      const costKind=({actual:isEn()?'Actual':'实付',estimated:isEn()?'Estimated':'估算',free:isEn()?'Free':'免费',unknown:isEn()?'Unpriced':'未定价'})[x.cost_kind] || '';
      html += `<tr ${x.session ? 'class="clickable" tabindex="0"' : ''} data-record-index="${i}" title="${x.session?(isEn()?'Open session details':'查看会话详情'):''}">
        <td data-label="${labels.time}" class="rec-time">${dtime(x.ts)}</td>
        <td data-label="${labels.tool}" class="rec-identity"><span class="chip"><span class="sw" style="background:${m.color || ROLE.total()}"></span>${esc(toolName(m, x.tool))}</span>${C.model?`<span class="rec-model" data-no-countup>${esc(x.model || '—')}</span>`:''}</td>
        ${C.project || C.source ? `<td data-label="${labels.project}" class="rec-project">${C.project?`<span>${esc(x.project || '—')}</span>`:''}${C.source?`<small class="rec-source" title="${esc(x.src || '')}">${esc((x.src || '').split(/[\\/]/).pop())}</small>`:''}</td>`:''}
        ${hasUsage ? `<td data-label="${labels.usage}" class="rec-usage"><div>${usage.join('')}</div></td>`:''}
        ${C.total || C.cost ? `<td data-label="${labels.totals}" class="rec-totals">${C.total?`<strong data-countup>${fmtExact(x.total)}</strong>`:''}${C.cost?`<span class="rec-cost" title="${esc([x.cost_source,x.price_version].filter(Boolean).join(' · '))}"><b data-countup>${x.cost_kind==='unknown'?(isEn()?'Unpriced':'未定价'):money(x.cost || 0)}</b><small>${esc(costKind)}</small></span>`:''}</td>`:''}
      </tr>`;
    });
    if (!r.rows.length) {
      html += `<tr><td colspan="${groups.length}" class="mut" style="text-align:center;padding:26px 12px">
        ${isEn()
          ? ((S.query || from || to)
              ? 'No records match the current filters. Widen the date range or clear the search.'
              : 'No records yet')
          : ((S.query || from || to)
              ? '当前筛选条件下没有记录，可放宽时间或清空搜索词'
              : '还没有调用记录')}</td></tr>`;
    }
    t.innerHTML = html + '</tbody>';
    animateNumericElements(t);
    // 计数封顶时如实说明，避免用户以为"总共就这么点"
    const scope = (from || to)
      ? `${from || '最早'} ~ ${to || '最新'}`
      : '';
    const base = S.query ? (isEn() ? `Matching “${S.query}”` : `匹配「${S.query}」`) : '';
    $('#recSub').innerHTML = [
      esc(scope), esc(base),
      `<span data-countup>${isEn() ? `${fmtExact(r.total)}${r.total_capped ? '+' : ''} records` : `共 ${fmtExact(r.total)}${r.total_capped ? '+' : ''} 条记录`}</span>`,
    ].filter(Boolean).join(' · ');
    animateNumericElements($('#recSub'));
    const openRecord = (event) => {
      const row=event.target.closest('tr[data-record-index]');
      const x=row && r.rows[Number(row.dataset.recordIndex)];
      if(x && x.session) openSession(x.session,x.tool,x.project);
    };
    t.onclick=openRecord;
    t.onkeydown=(event)=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openRecord(event);}};

    const size = Math.max(1, Number(r.limit) || recPage());
    S.recOffset = Math.max(0, Number(r.offset) || 0);
    S.recPageSize = size;
    const pages = Math.max(1, Math.ceil(r.total / size));
    const cur = Math.floor(S.recOffset / size) + 1;
    const go = page => {
      const target = Math.min(Math.max(1, page), pages);
      if (target === cur) return;
      S.recOffset = (target - 1) * size;
      S.recScroll = true; renderRecords();
    };
    const resize = value => {
      S.recPageSize = value; S.recOffset = 0;
      S.recScroll = true; renderRecords();
    };
    for (const host of [$('#pagerTop'), $('#pager')]) {
      drawPager(host, {total: r.total, size, page: cur, count: r.rows.length, onPage: go, onSize: resize});
    }
    $('#pager').hidden = r.total <= size;
    if (S.recScroll) { $('#pagerTop').scrollIntoView({block: 'start'}); S.recScroll = false; }
    S.rendered['records'] = S.epoch;             // 渲染完成，登记进缓存
    renderNavigationContext();restoreScroll();saveCurrentNavigation();
    if (window.__TL_PERF__) window.__TL_PERF__.recordRender('records:dom', performance.now() - renderStarted);
  }

  // 明细页的「按日期精修」：在右上角主导航（今天/7天/30天…）之上，
  // 再收窄到具体某几天。只管 from/to 两个输入与提示文案，
  // 主时间范围的切换由全局 rangeSeg 负责（见 switchView / applyRange）。
  function applyRecDays(from, to, rerender = true) {
    S.recFrom = from || '';
    S.recTo = to || '';
    const f = $('#recFrom'), t = $('#recTo'), clear = $('#recDayClear');
    if (f) f.value = S.recFrom;
    if (t) t.value = S.recTo;
    if (clear) clear.disabled = !S.recFrom && !S.recTo;
    const hint = $('#recDayHint');
    if (hint) {
      hint.textContent = S.recFrom || S.recTo
        ? (isEn()
            ? `Narrowed to ${S.recFrom || 'earliest'} ~ ${S.recTo || 'latest'}`
            : `将时间范围收窄到 ${S.recFrom || '最早'} ~ ${S.recTo || '最新'}`)
        : (isEn()
            ? 'No extra filter; follows the time range at the top'
            : '未额外筛选，跟随右上角时间范围');
    }
    if (!rerender) return;
    S.recOffset = 0;
    S.recCache = {};
    S.recRequest++;
    renderNavigationContext();if (S.view === 'records') renderRecords();
  }

  function readRecordPresets() {
    try { const x = JSON.parse(localStorage.getItem('tl.rec.presets') || '[]'); return Array.isArray(x) ? x : []; }
    catch (_) { return []; }
  }
  function renderRecordPresets() {
    const select = $('#recPresetSelect'); if (!select) return;
    const presets = readRecordPresets();
    const selected=select.value;
    select.innerHTML = `<option value="">${isEn()?'Saved filters':'筛选方案'}</option>` + presets.map((p, i) =>
      `<option value="${i}">${esc(p.name)}</option>`).join('');
    select.value=selected;$('#recEditPreset').disabled=select.value==='';
  }
  function saveRecordPreset() {
    const presets = readRecordPresets();
    const label = [S.recProject,S.recModel,S.recTool || (isEn()?'All tools':'全部工具'),S.range,S.query].filter(Boolean).join(' · ');
    openPresetEditor(label, name=>{
      presets.unshift({name,range:S.range,tool:S.recTool,model:S.recModel,project:S.recProject,project_paths:S.recProjectPaths,
        order:S.recOrder,query:S.query,from:S.recFrom,to:S.recTo,cost_basis:currentContext().cost_basis});
      writeRecordPresets(presets);$('#recPresetSelect').value='0';$('#recEditPreset').disabled=false;
    });
  }
  function writeRecordPresets(presets) {
    try { localStorage.setItem('tl.rec.presets',JSON.stringify(presets.slice(0,20))); }
    catch (_) {toast(isEn()?'Unable to save filters locally.':'无法在本机保存筛选方案。','err');return;}
    renderRecordPresets();toast(isEn()?'Saved filters updated':'筛选方案已更新','ok',1800);
  }
  function openPresetEditor(name, save, remove) {
    const mask=el('div','preset-editor');mask.setAttribute('role','dialog');mask.setAttribute('aria-modal','true');
    mask.innerHTML=`<div class="card"><h3>${isEn()?'Name this filter':'筛选方案名称'}</h3><label class="field"><span>${isEn()?'Name':'名称'}</span><input class="input" id="presetName" maxlength="120" value="${esc(name)}"></label><footer>${remove?`<button type="button" class="btn btn-ghost" data-preset-remove>${isEn()?'Delete':'删除'}</button>`:''}<button type="button" class="btn btn-ghost" data-preset-cancel>${isEn()?'Cancel':'取消'}</button><button type="button" class="btn btn-primary" data-preset-save>${isEn()?'Save':'保存'}</button></footer></div>`;
    const previous=document.activeElement;document.body.append(mask);
    const close=()=>{mask.remove();previous?.focus?.({preventScroll:true});};
    const input=$('#presetName',mask);input.focus();input.select();
    $('[data-preset-cancel]',mask).onclick=close;
    $('[data-preset-save]',mask).onclick=()=>{const text=input.value.trim();if(!text){input.focus();return;}save(text);close();};
    if(remove)$('[data-preset-remove]',mask).onclick=()=>{remove();close();};
    mask.onclick=event=>{if(event.target===mask)close();};
    mask.onkeydown=event=>{if(event.key==='Escape')close();else if(event.key==='Enter')$('[data-preset-save]',mask).click();
      else if(event.key==='Tab'){const nodes=$$('input,button',mask),first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}};
  }
  function editRecordPreset() {
    const index=Number($('#recPresetSelect').value),presets=readRecordPresets(),preset=presets[index];if(!preset)return;
    openPresetEditor(preset.name,name=>{preset.name=name;writeRecordPresets(presets);$('#recPresetSelect').value=String(index);},()=>{presets.splice(index,1);writeRecordPresets(presets);$('#recPresetSelect').value='';$('#recEditPreset').disabled=true;});
  }
  function applyRecordPreset(index) {
    const p = readRecordPresets()[Number(index)]; if (!p) return;
    saveCurrentNavigation();
    S.recTool = p.tool || ''; S.recOrder = p.order || 'ts'; S.query = p.query || '';
    S.recModel=p.model || '';S.recProject=p.project || '';S.recProjectPaths=p.project_paths || '';
    S.analysisContext={cost_basis:p.cost_basis || 'total_cost'};
    S.range = p.range || S.range; S.recFrom = p.from || ''; S.recTo = p.to || '';
    localStorage.setItem('tl.range', S.range);
    syncRangeButtons();
    if ($('#recTool')) $('#recTool').value = S.recTool;
    if ($('#recOrder')) $('#recOrder').value = S.recOrder;
    if ($('#searchInput')) $('#searchInput').value = S.query;
    applyRecDays(S.recFrom, S.recTo, false);
    S.recOffset = 0; S.recCache = {}; S.recRequest++;
    delete S.rendered.records;
    if (S.view !== 'records') switchView('records');
    renderNavigationContext();load();
  }
  function syncRecordColumns() {
    $$('#recColumnsPop input[data-rec-col]').forEach((input) => { input.checked = S.recColumns[input.dataset.recCol] !== false; });
  }
  function toggleRecordColumns() {
    const pop = $('#recColumnsPop'); if (!pop) return;
    pop.hidden = !pop.hidden;
    if (!pop.hidden) syncRecordColumns();
  }

  function isoDay(offsetDays) {
    const d = new Date();
    d.setDate(d.getDate() + (offsetDays || 0));
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  /* ======================================================================
     数据源视图
     ====================================================================== */
  const STATUS = {
    ok: ['已采集', 'b-ok'], partial: ['部分采集', 'b-partial'],
    detected: ['已安装 · 未落盘', 'b-detected'], missing: ['未安装', 'b-missing'],
    error: ['异常', 'b-error'],
  };
  // 状态徽标的英文文案；中文仍走上面的表，保持单一事实来源。
  const STATUS_EN = {
    ok: 'Collected', partial: 'Partial', detected: 'Installed · no data',
    missing: 'Not installed', error: 'Error',
  };
  // 数据源卡片上来自后端的文案（名字 / 厂商 / 说明）在英文界面要用英文版。
  //
  // note 是复合串："<适配器说明> · <扫描状态>"，整串不在词条表里，所以按
  // " · " 拆开逐段翻译：
  //   1) 先查 i18n 词条表（各条适配器说明与扫描状态都在其中，单一事实来源）；
  //   2) 再退回后端下发的 detail_en；
  //   3) 都不命中就保留原文——宁可留一句中文，也不要显示空白。
  function srcNote(s) {
    const raw = String(s.note || s.detail || '');
    if (!isEn()) return raw;
    const dict = window.TL_I18N;
    return raw.split(' · ').map((seg) => {
      const t = seg.trim();
      const hit = (dict && dict.translate) ? dict.translate(t) : null;
      if (hit) return hit;
      if (s.detail_en && t === String(s.detail || '').trim()) return s.detail_en;
      return t;
    }).join(' · ');
  }
  function srcName(s) {
    return (isEn() && s.name_en) ? s.name_en : (s.name || s.id || '');
  }
  function srcVendor(s) {
    return (isEn() && s.vendor_en) ? s.vendor_en : (s.vendor || '');
  }
  // 工具显示名：tool_meta 里的 name 对国产工具是中文（如「通义灵码」），
  // 英文界面取后端下发的 name_en。
  function toolName(m, fallback) {
    if (!m) return fallback || '';
    return (isEn() && m.name_en) ? m.name_en : (m.name || fallback || '');
  }
  function renderSourceScanSummary() {
    const box = $('#srcScanSummary');
    if (!box) return;
    const s = S.scanState || {};
    const en = isEn();
    const errors = Array.isArray(s.errors) ? s.errors.filter(Boolean) : [];
    const target = s.target ? String(s.target) : '';
    if (s.running) {
      box.className = 'src-scan-summary is-busy';
      box.innerHTML = `<i class="ico i-refresh"></i><span>${en ? 'Scanning' : '正在扫描'}${target ? ` · ${esc(target)}` : ''}：${esc(s.current || (en ? 'preparing…' : '准备中…'))}</span><b>${Number(s.percent || 0).toFixed(0)}%</b>`;
      return;
    }
    if (errors.length) {
      box.className = 'src-scan-summary is-error';
      box.innerHTML = `<i class="ico i-warning"></i><span>${en ? 'Last scan reported' : '最近扫描发现'} <b>${errors.length}</b> ${en ? 'issue(s)' : '条问题'}${target ? ` · ${esc(target)}` : ''}：${esc(errors[0])}</span><small>${en ? 'See each source card for details.' : '卡片中已列出对应来源的具体原因。'}</small>`;
      return;
    }
    if (s.phase === 'done') {
      box.className = 'src-scan-summary is-ok';
      box.innerHTML = `<i class="ico i-check"></i><span>${en ? 'Last scan completed' : '最近扫描已完成'}${target ? ` · ${esc(target)}` : ''}：${fmtExact(s.records || 0)} ${en ? 'new records' : '条新记录'}</span><small>${s.finished ? dtime(Math.round(s.finished)) : ''}</small>`;
      return;
    }
    box.className = 'src-scan-summary';
    box.innerHTML = `<i class="ico i-info"></i><span>${en ? 'Rescan one source from its card to refresh only that source.' : '可在每个来源卡片上单独重扫，只刷新该来源。'}</span>`;
  }
  function renderSources() {
    if(window.TLLSources)window.TLLSources.render(document.querySelector('section[data-view="sources"]'));
    const D = S.data;
    const src = D.sources || [];
    const ok = src.filter((s) => s.status === 'ok').length;
    const part = src.filter((s) => s.status === 'partial').length;
    const det = src.filter((s) => s.status === 'detected').length;
    const err = src.filter((s) => s.status === 'error').length;
    const sum = $('#srcSummary');
    sum.innerHTML = `
      ${srcKpi(isEn() ? 'Fully collected' : '已完整采集', ok, ROLE.ok())}
      ${srcKpi(isEn() ? 'Partially collected' : '部分采集', part, ROLE.warn())}
      ${srcKpi(isEn() ? 'Installed, no data' : '已安装未落盘', det, INK() ? '#4a7a8c' : '#60a5fa')}
      ${srcKpi(isEn() ? 'Scan errors' : '扫描异常', err, '#fb7185')}
      ${srcKpi(isEn() ? 'Not installed' : '未安装', src.filter((s) => s.status === 'missing').length, INK() ? '#7d939c' : '#94a3b8')}`;
    renderSourceScanSummary();

    const host = $('#srcGrid'); const cards = [];
    src.filter(s=>s.status!=='missing').forEach((s) => {
      const [label, cls] = STATUS[s.status] || ['未知', 'b-missing'];
      const statusText = isEn() ? (STATUS_EN[s.status] || 'Unknown') : label;
      const card = el('div', 'src-card');
      const side = s.side || {};
      let sideHtml = '';
      const en = isEn();
      if (s.id === 'cursor' && side.composers != null) {
        sideHtml = `<div class="src-kv"><span>${en ? 'Composer sessions' : 'Composer 会话'}</span><b>${fmtExact(side.composers)}</b></div>
          <div class="src-kv"><span>${en ? 'Tab completions accepted' : 'Tab 补全采纳行数'}</span><b>${fmtExact(side.tabAcceptedLines)}</b></div>
          <div class="src-kv"><span>${en ? 'Chat edits applied' : 'Chat 应用行数'}</span><b>${fmtExact(side.composerAcceptedLines)}</b></div>`;
      } else if (s.id === 'trae' && side.dbs != null) {
        sideHtml = `<div class="src-kv"><span>${en ? 'Encrypted session stores' : '加密会话库'}</span><b>${fmtExact(side.dbs)}${en ? "" : " 个"}</b></div>
          <div class="src-kv"><span>${en ? 'Disk usage' : '占用空间'}</span><b>${(side.size / 1048576).toFixed(1)} MB</b></div>`;
      } else if (s.id === 'ollama' && side.models != null) {
        sideHtml = `<div class="src-kv"><span>${en ? 'Local models' : '本地模型'}</span><b>${fmtExact(side.models)}${en ? "" : " 个"}</b></div>`;
      } else if (s.id === 'windsurf' && side.logs != null) {
        sideHtml = `<div class="src-kv"><span>${en ? 'Cache files' : '缓存文件'}</span><b>${fmtExact(side.logs)}</b></div>`;
      } else if (s.id === 'deepseek-harness' && side.sessions != null) {
        sideHtml = `<div class="src-kv"><span>${en ? 'Session projections' : '会话投影缓存'}</span><b>${fmtExact(side.sessions)}${en ? "" : " 个"}</b></div>
          <div class="src-kv"><span>${en ? 'zstd event streams' : 'zstd 事件流'}</span><b>${fmtExact(side.event_logs || 0)}${en ? "" : " 个"}</b></div>`;
      }
      card.innerHTML = `
        <div class="sc-glow" style="--gc:${s.color}"></div>
        <div class="src-head">
          <div class="tool-glyph" style="width:34px;height:34px;flex:0 0 34px;font-size:15px;
            background:transparent">${window.TLSourceIcons?window.TLSourceIcons.html(s.id,s.glyph):esc(s.glyph||'?')}</div>
          <div style="flex:1;min-width:0">
            <div class="tool-name">${esc(srcName(s))}</div>
            <div class="tool-vendor">${esc(srcVendor(s))}</div>
          </div>
          <span class="badge ${cls}">${statusText}</span>
        </div>
        <div class="src-body">
          ${s.tokens ? `<div class="src-kv"><span>${en ? 'Tokens collected' : '采集到 Token'}</span><b>${fmtExact(s.tokens)}</b></div>` : ''}
          <div class="src-kv"><span>${en ? 'Records' : '记录条数'}</span><b>${fmtExact(s.records)}</b></div>
          <div class="src-kv"><span>${en ? 'Data files' : '数据文件'}</span><b>${fmtExact(s.files)}</b></div>
          <div class="src-kv"><span>${en ? 'Disk usage' : '占用空间'}</span><b>${(s.size / 1048576).toFixed(1)} MB</b></div>
          <div class="src-kv"><span>${en ? 'Last activity' : '最近活动'}</span><b>${s.last_activity ? dd(s.last_activity) : '—'}</b></div>
          <div class="src-kv"><span>${en ? 'Last scan' : '最近扫描'}</span><b>${s.last_scan_at ? dtime(s.last_scan_at) : '—'}</b></div>
          <div class="src-kv"><span>${en ? 'Included in totals' : '是否计入合计'}</span><b class="src-health-${s.included ? 'yes' : 'no'}">${s.included ? (en ? 'Yes' : '已计入') : (en ? 'No' : '未计入')}</b></div>
          ${s.included ? `<div class="src-kv"><span>${en ? 'Cost basis' : '成本口径'}</span><b>${s.estimated ? (en ? 'Estimated' : '公开价估算') : (en ? 'Reported' : '来源实报')}</b></div>` : ''}
          ${sideHtml}
        </div>
        ${s.error ? `<div class="src-error"><i class="ico i-radar"></i>
          <div class="src-error-body">
            <span>${esc(s.error)}</span>
            <em>${s.last_scan_at ? (en ? `Previous snapshot kept (${dtime(s.last_scan_at)})` : `已保留上次快照（${dtime(s.last_scan_at)}）`) : (en ? 'No previous snapshot yet' : '尚无历史快照')}</em>
            <div class="src-error-actions">
              <button class="btn btn-ghost btn-xs" type="button" data-source-reveal="${esc(s.id)}"><i class="ico i-list"></i>${en ? 'Open data folder' : '打开数据目录'}</button>
              <button class="btn btn-ghost btn-xs" type="button" data-source-diag="1"><i class="ico i-info"></i>${en ? 'Export diagnostics' : '导出诊断'}</button>
            </div>
          </div></div>` : ''}
        ${srcNote(s) ? `<div class="src-note">${esc(srcNote(s))}</div>` : ''}
        <div class="src-actions"><button class="btn btn-ghost btn-xs src-rescan" type="button" data-source-scan="${esc(s.id)}" ${S.scanRunning ? 'disabled' : ''}><i class="ico i-refresh"></i>${en ? 'Rescan source' : '重扫此来源'}</button></div>`;
      cards.push(card);
    });
    chunkMount(host, cards, undefined, () => animateNumericElements(host));

    /* 未安装的来源：收敛成紧凑列表，避免刷屏 */
    const missing = src.filter((x) => x.status === 'missing');
    if (missing.length) {
      const box = el('div', 'src-card src-missing');
      box.innerHTML = `
        <div class="src-head">
          <div class="tool-glyph" style="width:30px;height:30px;flex:0 0 30px;font-size:13px;
            background:linear-gradient(140deg,#64748b,#475569)">—</div>
          <div style="flex:1;min-width:0">
            <div class="tool-name">${isEn()
              ? `${missing.length} sources not installed on this device`
              : `本机未安装的 ${missing.length} 个数据源`}</div>
            <div class="tool-vendor">${isEn()
              ? 'Install them and rescan to include them automatically'
              : '装上后重新扫描即可自动纳入统计'}</div>
          </div>
        </div>
        <div class="miss-chips">
          ${missing.map((x) => `<span class="chip"><span class="src-mini-icon">${window.TLSourceIcons?window.TLSourceIcons.html(x.id,x.glyph):''}</span>${esc(srcName(x))}</span>`).join('')}
        </div>`;
      host.appendChild(box);
    }

    /* 重要提示：哪些来源没有计入合计 */
    const excluded = src.filter((x) => x.status!=='missing' && (x.included === false || x.included == null));
    const anchor = $('#srcSummary');
    $$('.notice', anchor.parentNode).forEach((n) => n.remove());
    if (excluded.length) {
      const names = excluded.map(srcName).join(isEn() ? ', ' : '、');
      const n = el('div', 'notice', isEn()
        ? `<i class="ico i-radar"></i><span>Usage from the following sources cannot be read locally and is
           <b>excluded from the total</b>: ${esc(names)}. Refer to each vendor's dashboard for their
           actual consumption.</span>`
        : `<i class="ico i-radar"></i><span>以下来源的用量无法从本地取得，<b>未计入合计</b>：
           ${esc(names)}。它们的实际消耗请以厂商后台为准。</span>`);
      anchor.parentNode.insertBefore(n, anchor.nextSibling);
    }
  }
  // 数据源概览的小卡片；英文里没有量词，直接不给 unit 节点。
  const srcKpi = (label, val, c) => `
    <div class="kpi" style="padding:13px 16px">
      <div class="glow" style="background:${c}"></div>
      <div class="label">${label}</div>
      <div class="value" style="font-size:24px">${val}${isEn() ? '' : '<span class="unit">个</span>'}</div>
    </div>`;

  /* ======================================================================
     设置
     ====================================================================== */
  function renderSettings() {
    const c = (S.data && S.data.config) || S.boot.config || {};
    $('#budgetPeriodSelect').value = cfgValue(c, 'budget_period', 'month');
    $('#aiWarmupToggle').checked = !!cfgValue(c, 'ai_warmup_enabled', false);
    $('#aiHedgeToggle').checked = !!cfgValue(c, 'ai_hedge_enabled', false);
    $('#quickToggle').checked = !!cfgValue(c, 'codex_quick', false);
    $('#scanOnStartToggle').checked = cfgValue(c, 'scan_on_start', true) !== false;
    $('#rateInput').value = cfgValue(c, 'usd_to_cny', 7.2) || 7.2;
    $('#workerInput').value = cfgValue(c, 'workers', 4) || 4;
    $('#maxMbInput').value = cfgValue(c, 'max_file_mb', 400) || 400;
    $('#budgetTokensInput').value = cfgValue(c, 'budget_tokens', 0) || 0;
    $('#budgetCostInput').value = cfgValue(c, 'budget_cost', 0) || 0;
    $('#autoScanInput').value = cfgValue(c, 'auto_scan_minutes', 0) || 0;
    // 控件统一走 cfgValue()：本次会话刚改过的值优先，避免被服务端旧响应回滚。
    if ($('#themeSelect')) $('#themeSelect').value = cfgValue(c, 'theme', 'dark');
    $('#fontSelect').value = cfgValue(c, 'font', 'system');
    $('#densitySelect').value = cfgValue(c, 'density', 'compact');
    $('#languageSelect').value = cfgValue(c, 'language', 'zh-CN');
    if($('#displayCurrencySelect'))$('#displayCurrencySelect').value=cfgValue(c,'display_currency','auto');
    // 新增项：容错处理，旧配置文件里没有这些键时给合理默认
    if ($('#bigLibToggle')) $('#bigLibToggle').checked = cfgValue(c, 'big_lib_mode', true) !== false;
    if ($('#recPageInput')) $('#recPageInput').value = cfgValue(c, 'records_page', 100);
    if ($('#listCapInput')) $('#listCapInput').value = cfgValue(c, 'list_cap', 40);
    // 后端 warn_ratio 是 0~1 的比例，界面上按百分比填更好懂，这里做一次换算
    if ($('#warnRatioInput')) {
      const wr0 = cfgValue(c, 'warn_ratio', 0.8);
      const wr = wr0 == null ? 0.8 : Number(wr0);
      $('#warnRatioInput').value = Math.round((wr <= 1 ? wr * 100 : wr));
    }
    if ($('#defaultRangeSelect')) $('#defaultRangeSelect').value = cfgValue(c, 'default_range', '30d');
    if ($('#accentSelect')) $('#accentSelect').value = cfgValue(c, 'accent', 'theme');
    if ($('#radiusSelect')) $('#radiusSelect').value = cfgValue(c, 'radius', 'theme');
    if ($('#plMaxTokensInput')) $('#plMaxTokensInput').value = cfgValue(c, 'pl_max_tokens', 1024);
    if ($('#plThinkToggle')) $('#plThinkToggle').checked = cfgValue(c, 'pl_show_thinking', false) === true;
    if ($('#plEnabledToggle')) $('#plEnabledToggle').checked = cfgValue(c, 'pl_enabled', true) !== false;
    if ($('#iqEnabledToggle')) $('#iqEnabledToggle').checked = cfgValue(c, 'iq_enabled', true) !== false;
    $$('[data-nav-feature]').forEach(input => input.checked = cfgValue(c,input.dataset.navFeature,true)!==false);
    // 纯前端偏好存在 localStorage，不在后端配置里（极光开关已随动画一起移除）
    if ($('#motionToggle')) $('#motionToggle').checked = readBoolPref('motion', true);
    if ($('#fpsToggle')) $('#fpsToggle').checked = readBoolPref('fps', true);
    const list = $('#rootsList'); list.innerHTML = '';
    const rootsNow = cfgValue(c, 'api_log_roots', []) || [];
    rootsNow.forEach((r, i) => {
      const li = el('li', '', `<span>${esc(r)}</span>`);
      const b = el('button', '', '×');
      b.title = '移除';
      b.onclick = async () => {
        const roots = rootsNow.slice(); roots.splice(i, 1);
        await api('/api/config', { method: 'POST', body: { api_log_roots: roots } });
        rememberCfg('api_log_roots', roots);
        toast('已移除目录，下次扫描生效', 'ok');
        clearDashboardCache();
        await load(true, true);
      };
      li.appendChild(b); list.appendChild(li);
    });
    if (!rootsNow.length) {
      list.innerHTML = '<li style="justify-content:center;color:var(--muted)">尚未添加自定义目录</li>';
    }
  }

  /* 数据源适配器个数：不要在文案里写死数字，否则每加一个适配器都会漏改。
     以 bootstrap 下发的 tool_meta 为准，拿不到时回退到数据源视图里实际渲染的卡片数。 */
  function adapterCount() {
    const fromBoot = (S.boot && S.boot.tool_meta) || null;
    if (fromBoot && fromBoot.length) return fromBoot.length;
    const meta = (S.data && S.data.tool_meta) || null;
    if (meta) {
      const n = Array.isArray(meta) ? meta.length : Object.keys(meta).length;
      if (n) return n;
    }
    const cards = $$('#sourcesGrid .source-card, #sourceList .source-card').length;
    return cards || '若干';
  }

  /* 统计口径说明。这是给客户看的长文，必须两种语言都有完整版本——
     让客户在英文界面下读到一篇中文说明是很掉价的。 */
  function methodologyHTML() {
    if (isEn()) {
      return `
    <h4>1. How the token total is calculated</h4>
    <p>Each record uses <code>input + output + cache read + cache write + reasoning</code>.
    Cache-hit tokens are cheaper, but they do occupy the model's context window, so they are
    counted in the total and broken out separately to show where the cost comes from.</p>
    <h4>2. Where the data comes from</h4>
    <p>Everything is read from local files on this machine. The app <b>never goes online and
    never uploads anything</b>. It ships with ${adapterCount()} data-source adapters:</p>
    <ul>
      <li><span class="tag tag-green">Exact</span> Claude Code, Codex CLI, Gemini CLI, DeepSeek
        Harness, OpenCode / Mimocode, WorkBuddy, ZCode, Kimi Code CLI, Cline / Roo / Kilo, Continue,
        Copilot, LM Studio,
        Aider — the logs carry token counts directly, so records are parsed one by one.</li>
      <li><span class="tag tag-blue">Field detection</span> Tongyi Lingma, CodeBuddy Extension,
        JetBrains AI, Zed, Jan, Chatbox, Kiro, Qoder, Cody, Tabnine, Warp — these products iterate
        quickly with no fixed schema, so records are extracted with a rule requiring a prompt-like
        field and a completion-like field to appear together. Nothing is reported when that fails.</li>
      <li><span class="tag tag-blue">API logs</span> Automatically scans the default directories of
        gateways such as LiteLLM / one-api / new-api / gpt-load. You can also add any directory
        containing OpenAI / Anthropic-compatible usage logs in Settings.</li>
      <li><span class="tag tag-red">Detection only</span> Cursor, Trae and Windsurf meter usage in
        their own clouds (Trae's session store is also encrypted), so only installation state and
        side metrics are reported. They are <b>excluded from the total</b> rather than guessed.</li>
    </ul>
    <h4>3. Cost is an estimate</h4>
    <p>Converted using published price lists (USD per million tokens) and provided for reference
    only. Subscriptions (such as Cursor Pro or Codex plans), free tiers and reseller discounts are
    not reflected here; refer to the provider console for the actual bill. The exchange rate is
    configurable in Settings.</p>
    <h4>4. Incremental indexing and automation</h4>
    <p>The first scan walks all historical logs (Codex rollout files can reach hundreds of MB).
    After that, files are compared by <code>path + size + modification time</code> and only changed
    parts are re-parsed, so subsequent scans usually finish within seconds. You can enable an
    automatic incremental scan interval in Settings, or rescan manually at any time.</p>
    <h4>5. Measurement notes</h4>
    <p>Vendors define the same token differently (whether cache is listed separately, whether
    reasoning tokens count as output). This app normalises using the raw fields returned by each
    vendor's API. For example, Codex's <code>cached_input_tokens</code> is a subset of
    <code>input_tokens</code> and has been de-duplicated, and Claude / OpenAI reasoning tokens are
    already included in <code>output_tokens</code> and are not counted twice.</p>
    <h4>6. Keyboard shortcuts</h4>
    <p><code>1</code>–<code>9</code> switch views, <code>/</code> searches, <code>R</code> runs an
    incremental rescan, <code>Shift+R</code> forces a full rescan, <code>T</code> toggles the theme,
    <code>E</code> exports, <code>Esc</code> closes overlays, and <code>?</code> opens shortcut help.</p>`;
    }
    return `
    <h4>一、token 总量怎么算</h4>
    <p>每条记录取 <code>输入 + 输出 + 缓存读 + 缓存写 + 推理</code>。缓存命中的 token 虽然便宜，但确实占用了模型的
    上下文窗口，所以计入总量、并单独拆分展示，便于判断"贵在哪"。</p>
    <h4>二、数据从哪来</h4>
    <p>用量统计读取本机磁盘上的日志，<b>统计数据不上传</b>。可选的提示词优化与模型测试会向你配置的 API 发送所输入的内容；只有主动运行时才会请求。共内置 ${adapterCount()} 个数据源适配器：</p>
    <ul>
      <li><span class="tag tag-green">精确</span> Claude Code、Codex CLI、Gemini CLI、DeepSeek Harness、
        OpenCode / Mimocode、WorkBuddy、ZCode、Kimi Code CLI、Cline / Roo / Kilo、Continue、Copilot、
        LM Studio、Aider
        —— 日志里直接带 token 数，逐条解析。</li>
      <li><span class="tag tag-blue">字段探测</span> 通义灵码、CodeBuddy 插件、JetBrains AI、Zed、Jan、Chatbox、
        Kiro、Qoder、Cody、Tabnine、Warp —— 这些产品版本迭代快、结构不固定，统一用「prompt 类字段 + completion 类字段
        同时出现」的规则抽取，取不到就不报。</li>
      <li><span class="tag tag-blue">API 日志</span> 自动扫描 LiteLLM / one-api / new-api / gpt-load 等网关的默认目录，
        也可在设置里添加任意包含 OpenAI / Anthropic 兼容 usage 的目录。</li>
      <li><span class="tag tag-red">仅检测</span> Cursor、Trae、Windsurf 的用量由厂商云端计量（Trae 的会话库还是加密格式），
        因此只上报安装状态与侧写指标，<b>不计入合计</b>，以免捏造数字。</li>
    </ul>
    <h4>三、成本是估算</h4>
    <p>按公开价目表（美元 / 百万 token）折算，仅供参考。订阅制（如 Cursor Pro、Codex 套餐）、免费额度、
    第三方中转折扣都不会体现在这里，真实账单请以厂商后台为准。汇率可在设置中调整。</p>
    <h4>四、增量索引与自动化</h4>
    <p>首次扫描需要遍历全部历史日志（Codex 的 rollout 文件可达数百 MB），之后按
    <code>路径 + 大小 + 修改时间</code> 判断文件是否变化，只重解析变动部分，二次扫描通常在数秒内完成。
    设置里可开启「自动增量扫描间隔」，也可随时手动重扫。</p>
    <h4>五、口径提醒</h4>
    <p>不同厂商对同一个 token 的定义并不一致（缓存是否单列、推理 token 是否计入输出）。本程序已按各厂商
    API 返回的原始字段做归一化，例如 Codex 的 <code>cached_input_tokens</code> 是
    <code>input_tokens</code> 的子集，已做去重；Claude / OpenAI 的推理 token 已含在 <code>output_tokens</code> 内，
    不再二次累加。</p>
    <h4>六、快捷键</h4>
    <p><code>1</code>~<code>9</code> 切换视图，<code>/</code> 搜索，<code>R</code> 增量重扫，
    <code>Shift+R</code> 强制全量重扫，<code>T</code> 切换主题，<code>E</code> 导出，<code>Esc</code> 关闭弹层，
    <code>?</code> 打开快捷键帮助。</p>`;
  }

  /* ======================================================================
     洞察视图
     ====================================================================== */
  function renderInsights() {
    const D = S.data; if (!D) return;
    const rate = D.config.usd_to_cny;
    const host = $('#insightCards');
    const cards = [];
    // 洞察文案由后端生成（带插值），所以后端同时给中英两份，
    // 这里按当前语言取；缺英文时回退中文，不会出现空卡片。
    const en = S.language === 'en-US';
    const pick = (o, k) => (en && o[k + '_en'] ? o[k + '_en'] : o[k]);
    (D.insights || []).forEach((it) => {
      const card = el('div', 'insight-card');
      card.innerHTML = `
        <div class="ic-head">
          <div class="ic-dot" style="background:linear-gradient(140deg,${it.tone},${it.tone}aa)">${esc(it.glyph)}</div>
          <div class="ic-title">${esc(pick(it, 'title'))}</div>
        </div>
        <div class="ic-value" ${['model','up','down','peak'].includes(it.key) ? 'data-no-countup' : ''} style="color:${it.tone}">${esc(pick(it, 'value'))}</div>
        <div class="ic-desc" ${it.key === 'session' ? 'data-no-countup' : ''}>${esc(pick(it, 'desc'))}</div>`;
      if (it.action && it.action.type === 'session') {
        card.style.cursor = 'pointer';
        card.title = en ? 'Click to view this session' : '点击查看这次会话的明细';
        card.onclick = () => openSession(it.action.session, it.action.tool, it.action.project);
      }
      cards.push(card);
    });
    if (!cards.length) {
      host.innerHTML = `<div class="empty">${en
        ? 'No data to summarize in this range'
        : '当前时间范围内还没有可提炼的数据'}</div>`;
    } else {
      chunkMount(host, cards, undefined, () => animateNumericElements(host));
    }

    /* 周期对比 */
    const C = D.compare || {};
    const enC = S.language === 'en-US';
    $('#cmpSub').textContent = C.has_prev
      ? `${C.period[0]} ~ ${C.period[1]}${enC ? '  vs  ' : '　对比　'}${C.prev_period[0]} ~ ${C.prev_period[1]}`
      : (enC ? 'All time (no previous period to compare)' : '全部时间（无法对比上一周期）');
    const box = $('#compareBox');
    box.innerHTML = '';
    if (!C.has_prev) {
      box.innerHTML = `<div class="empty">${enC
        ? 'Choose a period such as last 7 / 30 / 90 days to compare with the previous one'
        : '选择「近 7 / 30 / 90 天」等周期后即可与上一个等长周期对比'}</div>`;
    } else {
      const head = el('div', 'cmp-row cmp-head',
        `<div>${enC ? 'Source' : '来源'}</div><div class="cmp-num">${enC ? 'Current' : '本期'}</div>` +
        `<div class="cmp-num">${enC ? 'Previous' : '上期'}</div><div class="cmp-delta">${enC ? 'Change' : '变化'}</div>`);
      box.appendChild(head);
      const mk = (name, color, cur, prev, delta) => {
        const row = el('div', 'cmp-row');
        const cls = delta == null ? 'cmp-flat' : (delta > 0 ? 'cmp-up' : (delta < 0 ? 'cmp-down' : 'cmp-flat'));
        row.innerHTML = `
          <div class="cmp-name"><span class="sw" style="width:8px;height:8px;border-radius:3px;
            background:${color};display:inline-block"></span>${esc(name)}</div>
          <div class="cmp-num">${fmtExact(cur)}</div>
          <div class="cmp-num mut" style="color:var(--muted)">${fmtExact(prev)}</div>
          <div class="cmp-delta ${cls}">${delta == null ? (enC ? 'New' : '新增') : pct(delta)}</div>`;
        return row;
      };
      box.appendChild(mk(enC ? 'Total' : '总计', ROLE.total(), C.current.total, C.current.total_prev, C.current.total_delta));
      box.appendChild(mk(enC ? 'Input' : '输入', ROLE.input(), C.current.input, C.current.input_prev, C.current.input_delta));
      box.appendChild(mk(enC ? 'Output' : '输出', ROLE.output(), C.current.output, C.current.output_prev, C.current.output_delta));
      box.appendChild(mk(enC ? 'Cache read' : '缓存读', ROLE.cRead(), C.current.cache_read, C.current.cache_read_prev, C.current.cache_read_delta));
      (C.tools || []).slice(0, 8).forEach((t) => {
        const meta = (D.tool_meta || {})[t.tool] || {};
        const c = meta.color || palette()[0];
        const row = el('div', 'cmp-row');
        const cls = t.delta == null ? 'cmp-flat' : (t.delta > 0 ? 'cmp-up' : (t.delta < 0 ? 'cmp-down' : 'cmp-flat'));
        row.innerHTML = `
          <div class="cmp-name"><span style="width:8px;height:8px;border-radius:3px;
            background:${c};display:inline-block"></span>${esc(toolName(meta, t.tool))}
            <span style="color:var(--muted);font-size:10.5px">${fmtExact(t.requests)}${enC ? ' calls' : ' 次'}</span></div>
          <div class="cmp-num">${fmtExact(t.current)}</div>
          <div class="cmp-num" style="color:var(--muted)">${fmtExact(t.previous)}</div>
          <div class="cmp-delta ${cls}">${t.delta == null ? (enC ? 'New' : '新增') : pct(t.delta)}</div>`;
        box.appendChild(row);
      });
    }

    /* 预算 */
    renderBudget(D.budget, rate);

    /* 用量高峰：按「日期 × 工具」找最集中的组合 */
    const peakHost = $('#peakList');
    peakHost.innerHTML = '';
    const daily = (D.daily || []).slice().sort((a, b) => b.total - a.total).slice(0, 5);
    if (!daily.length) peakHost.innerHTML = '<div class="empty">暂无数据</div>';
    const maxD = Math.max(1, ...daily.map((d) => d.total));
    daily.forEach((d, i) => {
      const row = el('div', 'rank-row');
      row.innerHTML = `
        <div class="rank-badge" style="background:linear-gradient(140deg,${ROLE.total()},${ROLE.output()})">${i + 1}</div>
        <div class="rank-main">
          <div class="rank-top"><span class="rank-name">${esc(d.day)}</span>
            <span class="rank-val">${fmt(d.total)}</span></div>
          <div class="rank-track"><div class="rank-fill" style="width:${Math.max(2, d.total / maxD * 100)}%;
            animation-delay:${i * 45}ms;background:linear-gradient(90deg,${ROLE.total()},${ROLE.input()})"></div></div>
          <div class="rank-sub"><span>${fmtExact(d.requests)}${isEn() ? " calls" : " 次调用"}</span><span>${money(d.cost, rate)}</span></div>
        </div>
        <div style="font-family:var(--mono);font-size:11px;color:var(--muted)">${(d.total * 100 / (D.summary.total || 1)).toFixed(1)}%</div>`;
      peakHost.appendChild(row);
    });

    /* 缓存效率 */
    const s = D.summary;
    const denom = (s.input || 0) + (s.cache_read || 0) + (s.cache_write || 0);
    const hit = denom ? (s.cache_read * 100 / denom) : 0;
    const cacheSave = (D.insights || []).find((x) => x.key === 'cache');
    $('#cacheStat').innerHTML = `
      <div class="cs-row"><span>缓存命中率</span><b>${hit.toFixed(1)}%</b></div>
      <div class="cs-bar"><i style="width:${Math.min(100, hit).toFixed(1)}%"></i></div>
      <div class="cs-row"><span>缓存读 Token</span><b>${fmt(s.cache_read)}</b></div>
      <div class="cs-row"><span>未缓存输入</span><b>${fmt(s.input)}</b></div>
      <div class="cs-row"><span>估算节省</span><b style="color:var(--ok)">${cacheSave ? cacheSave.value : '—'}</b></div>
      <div class="cs-row"><span>整体记录费用</span><b>${money(s.cost, rate)}</b></div>`;
  }

  /* 周报卡片：本周用量 + 本周期预算进度。纯本地计算、零 AI、零配置；
     没设预算时只显示本周摘要，顺带引导去设置预算。 */
  const weeklyDigestCache = { at: 0, html: '' };
  const compactTokens = (n) => {
    n = Number(n) || 0;
    const neg = n < 0; n = Math.abs(n);
    let u = 0;
    while (n >= 1000 && u < 4) { n /= 1000; u += 1; }
    const s = n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2).replace(/\.?0+$/, '');
    return (neg ? '-' : '') + s + ['', 'K', 'M', 'B', 'T'][u];
  };
  async function renderWeeklyDigest() {
    const host = $('#weeklyDigest');
    if (!host) return;
    if (weeklyDigestCache.html && Date.now() - weeklyDigestCache.at < 600000) {
      host.innerHTML = weeklyDigestCache.html; host.hidden = false; return;
    }
    let week = null, budget = null;
    try { [week, budget] = await Promise.all([api('/api/summary?range=7d'), api('/api/budget')]); }
    catch (_) { host.hidden = true; return; }
    const en = isEn();
    const s = (week && week.summary) || {};
    const b = (budget && budget.budget) || {};
    const hasBudget = b && b.metric && b.metric !== 'none' && b.limit > 0;
    const pct = hasBudget ? Math.min(1, b.ratio) : 0;
    const col = !hasBudget ? ROLE.ok() : (b.over ? ROLE.danger() : (b.ratio >= b.warn_ratio ? ROLE.warn() : ROLE.ok()));
    const periodLabel = b.period === 'week' ? (en ? 'This week' : '本周') : (en ? 'This month' : '本月');
    const amount = hasBudget
      ? (b.metric === 'cost'
          ? `$${Number(b.used).toFixed(2)} / $${Number(b.limit).toFixed(2)}`
          : `${compactTokens(b.used)} / ${compactTokens(b.limit)} tokens`)
      : '';
    const budgetHtml = hasBudget
      ? `<div class="wd-budget">
          <div class="wd-budget-top"><span>${periodLabel}${en ? ' · budget' : ' · 预算'} · ${amount}</span>
            <b style="color:${col}">${(b.ratio * 100).toFixed(0)}%</b></div>
          <div class="wd-bar"><i style="width:${(pct * 100).toFixed(1)}%;background:${col}"></i></div>
          ${b.forecast && b.limit
            ? `<small>${en ? `Projected end of period: ${b.metric === 'cost' ? '$' + Number(b.forecast).toFixed(2) : compactTokens(b.forecast) + ' tokens'}` : `按当前节奏预计期末 ${b.metric === 'cost' ? '$' + Number(b.forecast).toFixed(2) : compactTokens(b.forecast) + ' tokens'}`}</small>`
            : ''}
        </div>`
      : `<a href="#" class="wd-hint" data-view-goto="settings">${en ? 'Set a budget to track your pace →' : '设置预算，掌握用量节奏 →'}</a>`;
    host.innerHTML = `
      <div class="card-head">
        <div><h3>${en ? 'Weekly digest' : '本周速览'}</h3>
          <p>${en ? 'Local summary, no AI needed' : '本地计算，无需 AI'}</p></div>
      </div>
      <div class="wd-body">
        <div class="wd-kv"><span>${en ? 'Tokens (7d)' : '本周 Tokens'}</span><b data-countup>${fmtExact(s.total || 0)}</b></div>
        <div class="wd-kv"><span>${en ? 'Cost (7d)' : '本周费用'}</span><b data-countup>${money(s.cost || 0)}</b></div>
        <div class="wd-kv"><span>${en ? 'Active days' : '活跃天数'}</span><b>${s.days || '—'}</b></div>
        <div class="wd-kv"><span>${en ? 'Calls' : '调用次数'}</span><b data-countup>${fmtExact(s.requests || 0)}</b></div>
        ${budgetHtml}
      </div>`;
    host.hidden = false;
    animateNumericElements(host);
    host.querySelectorAll('[data-view-goto]').forEach((a) => a.onclick = (e) => {
      e.preventDefault(); switchView(a.getAttribute('data-view-goto'));
    });
    weeklyDigestCache.html = host.innerHTML;
    weeklyDigestCache.at = Date.now();
  }

  function renderBudget(b, rate) {
    const host = $('#budgetChart');
    if (!b || b.metric === 'none' || !b.limit) {
      host.innerHTML = isEn()
        ? `<div class="empty" style="padding:22px 10px">No budget set<br>
           <span style="font-size:11px">Set a token or cost limit in Settings,<br>and usage progress appears here</span></div>`
        : `<div class="empty" style="padding:22px 10px">尚未设置预算<br>
           <span style="font-size:11px">到「设置」里填一个 token 或成本上限，<br>这里会显示使用进度</span></div>`;
      $('#budgetSub').textContent = isEn() ? 'Configure in Settings' : '可在设置中设定';
      $('#budgetMeta').innerHTML = '';
      return;
    }
    const isCost = b.metric === 'cost';
    const showed = Math.min(1, b.ratio);
    const size = 176, r = 66, sw = 16, cx = size / 2, cy = size / 2;
    const C = 2 * Math.PI * r;
    const col = b.over ? ROLE.danger() : (b.ratio >= b.warn_ratio ? ROLE.warn() : ROLE.ok());
    const svgEl = svg('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
    svgEl.appendChild(svg('circle', { cx, cy, r, fill: 'none', stroke: 'currentColor',
      'stroke-opacity': .08, 'stroke-width': sw }));
    const arc = svg('circle', {
      cx, cy, r, fill: 'none', stroke: col, 'stroke-width': sw, 'stroke-linecap': 'round',
      'stroke-dasharray': `${showed * C} ${C}`, transform: `rotate(-90 ${cx} ${cy})`,
    });
    arc.style.animation = 'none';
    svgEl.appendChild(arc);
    const t1 = svg('text', { x: cx, y: cy - 2, 'text-anchor': 'middle', 'font-size': 24,
      'font-weight': 650, fill: 'currentColor', 'font-family': 'ui-monospace,monospace' });
    t1.textContent = (b.ratio * 100).toFixed(1) + '%';
    t1.setAttribute('data-countup', '');
    const t2 = svg('text', { x: cx, y: cy + 18, 'text-anchor': 'middle', 'font-size': 10.5,
      fill: 'currentColor', 'fill-opacity': .5 });
    t2.textContent = isCost ? (isEn() ? 'Cost budget' : '成本预算')
                            : (isEn() ? 'Token budget' : 'Token 预算');
    svgEl.append(t1, t2);
    host.innerHTML = '';
    host.appendChild(svgEl);
    $('#budgetSub').textContent = b.over
      ? (isEn() ? 'Over budget' : '已超出预算')
      : (b.ratio >= b.warn_ratio
          ? (isEn() ? 'Approaching limit' : '接近上限')
          : (isEn() ? 'Within budget' : '在预算内'));
    $('#budgetMeta').innerHTML = `<p class="mut">${esc(b.start || '')} — ${esc(b.end || '')}</p>` +
      [b.tokens, b.cost].filter((m) => m && m.limit).map((m) => {
        const cost = m.metric === 'cost', format = cost ? money : fmtExact;
        return `<div class="rowline"><span>${cost ? window.TLMoney?.currency() || 'USD' : 'Token'}</span><b>${format(m.used)} / ${format(m.limit)} · ${(m.ratio * 100).toFixed(1)}%</b></div><div class="rowline mut"><span>${isEn() ? 'Projected at period end' : '预计账期结束'}</span><span>${format(m.forecast)}</span></div>`;
      }).join('');
  }

  function checkBudget(b) {
    if (b && b.tokens && b.cost) {
      [b.tokens, b.cost].forEach((metric) => checkBudget({...metric, start:b.start, end:b.end}));
      return;
    }
    if (!b || b.metric === 'none' || !b.limit) return;
    if (!b.over && b.ratio < (b.warn_ratio || 0.8)) return;
    const key = ['tl.budget', b.start, b.metric, b.limit, b.over ? 'over' : 'warn'].join(':');
    if (localStorage.getItem(key)) return;
    localStorage.setItem(key, '1');
    const isCost = b.metric === 'cost';
    const used = isCost ? money(b.used) : b.used.toLocaleString(isEn() ? 'en-US' : 'zh-CN') + ' tokens';
    const lim = isCost ? money(b.limit) : b.limit.toLocaleString(isEn() ? 'en-US' : 'zh-CN');
    if (b.over) toast(`已超出本期预算：${used} / ${lim}`, 'err', 6500);
    else toast(`本期预算已用 ${(b.ratio * 100).toFixed(0)}%（${used} / ${lim}）`, 'warn', 5200);
  }

  /* ======================================================================
     会话详情抽屉
     ====================================================================== */
  async function openSession(session, tool, project, scope = null) {
    if (!session) return;
    const mask = $('#drawerMask'), dr = $('#drawer');
    S.drawerOpen = true;
    mask.classList.add('on'); dr.classList.add('on');
    $('#drawerTitle').textContent = '会话详情';
    $('#drawerSub').textContent = session;
    $('#drawerBody').innerHTML = '<div class="spin" style="margin-top:40px"></div>';
    try {
      const q = new URLSearchParams({ session, limit: '300', range: scope?.range || S.range });
      q.set('cost_basis', scope?.cost_basis || currentContext().cost_basis);
      if (tool) q.set('tool', tool);
      if (project != null) q.set('project', project);
      if (scope) {
        for (const key of ['model','from','to','q','project_paths']) if (scope[key]) q.set(key, scope[key]);
      } else if (S.view === 'records') {
        for (const [key, value] of Object.entries({from:S.recFrom,to:S.recTo,model:S.recModel,q:S.query})) {
          if (value) q.set(key, value);
        }
      }
      const d = await api('/api/session?' + q.toString());
      d.cost_basis = q.get('cost_basis');
      renderSessionDrawer(d);
    } catch (e) {
      $('#drawerBody').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function closeDrawer() {
    S.drawerOpen = false;
    $('#drawerMask').classList.remove('on');
    $('#drawer').classList.remove('on');
    syncNumberMotions();
  }

  function renderSessionDrawer(d) {
    const meta = (S.data && S.data.tool_meta || {})[d.tool] || {};
    const rate = (S.data && S.data.config.usd_to_cny) || 7.2;
    const body = $('#drawerBody');
    $('#drawerTitle').textContent = d.project || '会话详情';
    $('#drawerSub').innerHTML = `${esc(toolName(meta, d.tool))} · <span data-countup>${isEn() ? `${d.records} records` : `${d.records} 条记录`}</span>`;
    const maxDay = Math.max(1, ...(d.daily || []).map((x) => x.total));
    const maxTurn = Math.max(1, ...(d.turns || []).map((x) => x.total));
    body.innerHTML = `
      <div class="kpis" style="grid-template-columns:repeat(2,1fr);gap:9px">
        ${dkpi('Token 合计', fmt(d.total), fmtExact(d.total))}
        ${dkpi('调用次数', fmtExact(d.requests), '')}
        ${dkpi('输入 / 输出', fmt(d.input) + ' / ' + fmt(d.output), '')}
        ${dkpi(({total_cost:isEn()?'Saved cost':'采集时保存金额',historical_cost:isEn()?'Prices at call date':'调用日价格估算',repriced_cost:isEn()?'Current prices':'现价估算'})[d.cost_basis] || (isEn()?'Known cost':'已计价费用'), money(d.cost, rate), window.TLMoney?.coverage(d) || '')}
      </div>
      <h4 class="dsec">时间范围</h4>
      <div class="prose" style="font-size:12px">${dtime(d.first_ts)} → ${dtime(d.last_ts)}
        <span data-countup style="color:var(--muted)">（${d.daily.length} 个活跃日）</span></div>

      <h4 class="dsec">${isEn() ? 'Daily usage' : '每日用量'}</h4>
      <div class="dbar">${(d.daily || []).map((x) => `
        <div class="dbar-col" title="${x.day}${isEn() ? ': ' : '：'}${fmtExact(x.total)} tokens / ${x.requests}${isEn() ? ' calls' : ' 次'}">
          <i style="height:${Math.max(3, x.total / maxDay * 100)}%"></i>
          <span>${x.day.slice(5)}</span>
        </div>`).join('')}</div>

      <h4 class="dsec">${isEn() ? 'Model breakdown' : '模型构成'}</h4>
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>${isEn() ? 'Model' : '模型'}</th><th class="num">Token</th>
        <th class="num">${isEn() ? 'Calls' : '调用'}</th><th class="num">${isEn() ? 'Cost' : '成本'}</th>
        <th style="width:96px">${isEn() ? 'Share' : '占比'}</th></tr></thead>
        <tbody>${(d.models || []).map((m) => `
          <tr><td>${esc(m.model)}</td><td class="num">${fmtExact(m.total)}</td>
          <td class="num mut">${fmtExact(m.requests)}</td><td class="num">${money(m.cost, rate)}</td>
          <td><div class="bar-mini"><i style="width:${m.total / Math.max(1, d.total) * 100}%;
            background:linear-gradient(90deg,${ROLE.total()},${ROLE.output()})"></i></div></td></tr>`).join('')}
        </tbody></table></div>

      <h4 class="dsec">${isEn() ? 'Turn-level detail' : '逐轮明细'}<span style="color:var(--muted);font-weight:400">${isEn() ? ' (oldest first)' : '（按时间升序）'}</span></h4>
      <div class="turns">${(d.turns || []).map((t) => `
        <div class="turn">
          <div class="turn-top">
            <span class="turn-time">${dtime(t.ts)}</span>
            <span class="turn-model">${esc(t.model || '—')}</span>
            <span class="turn-total">${fmtExact(t.total)}</span>
          </div>
          <div class="turn-bar"><i style="width:${Math.max(2, t.total / maxTurn * 100)}%"></i></div>
          <div class="turn-meta">${isEn() ? 'Input' : '输入'} ${fmtExact(t.input)} · ${isEn() ? 'Output' : '输出'} ${fmtExact(t.output)} ·
            ${isEn() ? 'Cache read' : '缓存读'} ${fmtExact(t.cache_read)} · ${t.cost_kind==='unknown'?(isEn()?'Unpriced':'未定价'):money(t.cost || 0)}</div>
        </div>`).join('')}</div>`;
    if(window.TLResponsive)window.TLResponsive.apply($('#drawer'));
    animateNumericElements($('#drawer'));
  }
  const dkpi = (label, value, sub) => `
    <div class="kpi" style="padding:12px 14px">
      <div class="label">${label}</div>
      <div class="value" style="font-size:19px">${value}</div>
      ${sub ? `<div class="sub" style="font-family:var(--mono)">${sub}</div>` : ''}
    </div>`;

  /* ======================================================================
     提示词优化
     ====================================================================== */
  const PL = { cfg: null, testing: false, running: false, last: null, lastByMode: { polish: null, structured: null }, draftNew: false };
  // 暴露到 window 仅用于自动化测试与排障：让外部能读配置状态、在测试里
  // 把上游指到一个"只挂起不响应"的地址来验证停止功能。正常使用不依赖它。
  window.__PL = PL;
  // API 同时会返回这份依据；前端保留一个回退，兼容旧缓存/旧服务返回的结果。
  const PL_BASIS = {
    polish: {
      title: '清晰润色的优化依据',
      summary: '保持事实与核心意图不变，只改善指令的清晰度、歧义和可执行性。',
      principles: [
        '保留事实、数字、名称、代码、路径和显式约束，不猜测缺失信息。',
        '把模糊动作改写成可执行指令，减少冗余与歧义，保持相近的信息密度。',
        '仅在原文已有层级或确有必要时使用列表，不凭空添加角色、工具或技术栈。',
      ],
      references: [
        { name: 'OpenAI Prompt engineering', url: 'https://developers.openai.com/api/docs/guides/prompt-engineering' },
        { name: 'Anthropic Prompting best practices', url: 'https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/prompt-templates-and-variables' },
      ],
    },
    structured: {
      title: '专业结构的优化依据',
      summary: '把需求整理为可直接执行的提示词，并让输入、约束、输出和验收标准彼此对应。',
      principles: [
        '明确 AI 角色与工作目标，再组织任务上下文、范围和对象。',
        '将执行步骤、边界条件、禁止事项、质量检查和失败处理分开表达。',
        '明确输出格式、字段、长度、语言和验收标准；仅对影响执行的缺失信息使用占位符。',
        '使用 Markdown 标题与列表分区；保留原始事实，不混入优化过程或解题答案。',
      ],
      references: [
        { name: 'OpenAI Prompt engineering', url: 'https://developers.openai.com/api/docs/guides/prompt-engineering' },
        { name: 'Anthropic Prompt engineering overview', url: 'https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview' },
        { name: 'Anthropic prompting best practices', url: 'https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/prompt-templates-and-variables' },
      ],
    },
  };
  PL.mode = localStorage.getItem('tl.prompt.mode') || 'polish';

  const PL_SAMPLE = '帮我写一个爬虫';
  // 明细每页条数：默认 100，可在设置里调整；保持 API 与界面的上限一致。
  // 用函数读取而不是常量，这样用户在设置页改完立刻生效、不用刷新。
  const REC_PAGE_DEFAULT = 100;
  const REC_PAGE_MIN = 50;
  const REC_PAGE_MAX = 2000;
  function recPage() {
    const c = (S.data && S.data.config) || (S.boot && S.boot.config) || {};
    const n = parseInt(c.records_page, 10);
    const configured = Number.isFinite(n) ? Math.min(REC_PAGE_MAX, Math.max(REC_PAGE_MIN, n)) : REC_PAGE_DEFAULT;
    if (S.recConfiguredSize !== configured) {
      S.recConfiguredSize = configured; S.recPageSize = null;
    }
    return S.recPageSize || configured;
  }
  // 各列表/排行最多渲染多少行（设置里的「列表最多展示」）
  function listCap(dflt = 40) {
    const c = (S.data && S.data.config) || (S.boot && S.boot.config) || {};
    const n = parseInt(c.list_cap, 10);
    if (!Number.isFinite(n)) return dflt;
    return Math.min(500, Math.max(10, n));
  }

  async function loadPromptCfg() {
    try {
      PL.cfg = await api('/api/prompt');
    } catch (e) {
      PL.cfg = null;
    }
    renderAiProfiles();
    return PL.cfg;
  }

  function renderPromptProfiles(preferId) {
    const sel = $('#plProfileSelect');
    if (!sel || !PL.cfg) return;
    const profiles = PL.cfg.profiles || [];
    const known = (id) => !!id && profiles.some((p) => p.id === id);
    // 选择的优先级：显式指定（刚保存完）> 用户主动选的「新建配置」> 当前下拉值 > 生效配置。
    // 关键是「新建配置」不能被重置回当前生效配置，否则下拉框显示的目标和实际保存的
    // 目标不一致，用户就会看到"保存把上一个配置覆盖了"。
    let wanted = preferId || '';
    if (!known(wanted)) {
      wanted = PL.draftNew ? '' : (sel.value || '');
      if (!known(wanted)) wanted = PL.cfg.profile_id || '';
    }
    sel.innerHTML = '<option value="">新建配置</option>' + profiles.map((p) =>
      `<option value="${esc(p.id)}">${esc(p.name || p.model || '未命名配置')} · ${esc(p.model || '')}</option>`).join('');
    sel.value = known(wanted) ? wanted : '';
    PL.draftNew = !sel.value;
    const active = profiles.find((p) => p.id === sel.value);
    if ($('#plProfileName')) {
      $('#plProfileName').value = sel.value
        ? ((active && (active.name || active.model)) || '')
        : ($('#plProfileName').value || '');
    }
    if ($('#plProfileDelete')) $('#plProfileDelete').disabled = !sel.value;
    if ($('#plProfileNew')) $('#plProfileNew').disabled = !PL.cfg;
    renderProfileHint();
  }

  /* 根据当前选择更新弹窗内的操作说明，让「保存」与「另存为」的区别一眼可见。 */
  function renderProfileHint() {
    const sel = $('#plProfileSelect');
    const node = $('#plProfileHint');
    const saveBtn = $('#plSaveBtn');
    const newBtn = $('#plSaveNewBtn');
    if (!sel) return;
    const id = sel.value;
    const en = S.language === 'en-US';
    const p = id && ((PL.cfg && PL.cfg.profiles) || []).find((x) => x.id === id);
    if (node) {
      node.textContent = p
        ? (en ? `Will update the saved profile “${p.name || p.model || 'unnamed'}”; other profiles are untouched.`
              : `将更新已有配置「${p.name || p.model || '未命名配置'}」，其它配置不受影响。`)
        : (en ? 'Will add a new profile; every saved profile is kept and can be switched below.'
              : '将新增一条配置；已保存的配置会全部保留，可随时在下拉框切换。');
    }
    // 「保存并使用」在新建状态下就是新建；此时「另存为新配置」没有意义。
    if (saveBtn) {
      saveBtn.innerHTML = id
        ? '<i class="ico i-check"></i>' + (en ? 'Save & use' : '保存并使用')
        : '<i class="ico i-check"></i>' + (en ? 'Create & use' : '新建并使用');
    }
    if (newBtn) newBtn.style.display = id ? '' : 'none';
  }

  function renderAiProfiles() {
    const sel = $('#iqProfileSelect');
    if (!sel || !PL.cfg) return;
    const profiles = PL.cfg.profiles || [];
    const current = IQ && IQ.profileId ? IQ.profileId : '';
    sel.innerHTML = '<option value="">手动填写配置</option>' + profiles.map((p) =>
      `<option value="${esc(p.id)}">${esc(p.name || p.model || '未命名配置')} · ${esc(p.model || '')}</option>`).join('');
    sel.value = current && profiles.some((p) => p.id === current) ? current : '';
  }

  function renderPrompt() {
    const c = PL.cfg;
    const en = S.language === 'en-US';
    if (!c) {
      $('#plBadge').className = 'badge b-error';
      $('#plBadge').textContent = en ? 'Connection unavailable' : '连接信息读取失败';
      $('#plSub').textContent = en ? 'Reopen this page later' : '请稍后重新进入此页面';
      $('#plRunBtn').disabled = true;
      $('#plRunTip').textContent = en ? 'Unable to read local AI configuration' : '暂时无法读取本机 AI 配置';
      renderOptimizeResult(PL.last);
      return;
    }

    renderPromptProfiles();
    renderAiProfiles();
    renderProfileHint();

    /* 已保存的配置回填（Key 不回显） */
    if (!$('#plModel').value) $('#plModel').value = c.model || '';
    if (!$('#plUrl').value) $('#plUrl').value = c.base_url || '';

    const badge = $('#plBadge');
    if (c.configured) {
      badge.className = 'badge b-ok';
      badge.textContent = en ? 'Configured' : '已配置';
      $('#plKey').placeholder = en ? ('Saved (' + (c.key_hint || '') + '); enter a new key to replace it') : ('已保存（' + (c.key_hint || '') + '），如需更换请重新填写');
      $('#plSub').textContent = c.model + ' · ' + protoLabel(c.protocol_effective).split('（')[0];
    } else {
      badge.className = 'badge b-missing';
      badge.textContent = en ? 'Not configured' : '未配置';
      $('#plSub').textContent = en ? 'Choose a model in Settings' : '请在设置中选择模型';
    }
    $('#plPrivacy').style.display = c.configured ? '' : 'none';
    const profileCount = (c.profiles || []).length;
    $('#plSaveTip').textContent = c.configured
      ? (en ? (profileCount + ' saved profile(s); run the check again to change one')
            : (`已保存 ${profileCount} 个配置，可随时切换；修改后需重新检测`))
      : (en ? 'Pass the connection check before saving' : '检测通过后才可保存');
    $('#plRunTip').textContent = c.configured
      ? (en ? 'The selected optimization mode will be applied' : (PL.mode === 'structured' ? '将补充角色、目标、内容范围、约束与输出要求' : '将改善措辞、语序与逻辑衔接，保持原始意图'))
      : (en ? 'Configure the model in Settings' : '请在设置中配置提示词优化模型');
    $('#plRunBtn').disabled = !c.configured;
    $('#plInput').disabled = false;
    $$('#plMode button').forEach((b) => b.classList.toggle('active', b.dataset.promptMode === PL.mode));

    updateEndpointPreview();
    PL.last = PL.lastByMode[PL.mode] || null;
    renderOptimizeResult(PL.last);
  }

  const protoLabel = (p) => ({
    openai: 'OpenAI 兼容（/chat/completions）',
    anthropic: 'Anthropic（/messages）',
    gemini: 'Google Gemini（generateContent）',
  }[p] || p);

  function plForm() {
    return {
      profile_id: $('#plProfileSelect') ? $('#plProfileSelect').value : (PL.cfg && PL.cfg.profile_id) || '',
      profile_name: $('#plProfileName') ? $('#plProfileName').value.trim() : '',
      model: $('#plModel').value.trim(),
      base_url: $('#plUrl').value.trim(),
      api_key: $('#plKey').value.trim(),
      protocol: 'auto',
    };
  }

  function openPromptConfig() {
    renderPromptProfiles();
    $('#plConfigMask').classList.add('on');
    requestAnimationFrame(() => $('#plModel').focus());
  }

  const PL_PROVIDER_PRESETS = {
    openai: { model: 'gpt-4o-mini', url: 'https://api.openai.com/v1' },
    deepseek: { model: 'deepseek-chat', url: 'https://api.deepseek.com/v1' },
    anthropic: { model: 'claude-3-5-sonnet-latest', url: 'https://api.anthropic.com/v1' },
    siliconflow: { model: 'deepseek-ai/DeepSeek-V3', url: 'https://api.siliconflow.cn/v1' },
  };
  function applyPromptProviderPreset(name) {
    const p = PL_PROVIDER_PRESETS[name];
    if (!p) return;
    $('#plModel').value = p.model;
    $('#plUrl').value = p.url;
    $('#plKey').value = '';
    $('#plSaveBtn').disabled = true;
    if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = true;
    updateEndpointPreview();
    $('#plKey').focus();
  }

  function closePromptConfig() {
    $('#plConfigMask').classList.remove('on');
  }

  function openAbout() {
    // 关于弹窗是静态 HTML，里面混着版本号等动态值，所以在这里
    // 按当前语言把可变文案写一遍，而不是靠翻译层逐节点替换。
    const en = isEn();
    const set = (sel, zh, english) => { const n = $(sel); if (n) n.textContent = en ? english : zh; };
    set('#aboutTitle', '关于', 'About');
    set('#aboutRuntimeLabel', '运行方式', 'Runtime');
    set('#aboutRuntimeValue', 'Tauri 桌面 · 本地统计', 'Tauri desktop · local analytics');
    set('#aboutScopeLabel', '数据范围', 'Data scope');
    set('#aboutProjectLabel', '项目主页', 'Project home');
    set('#aboutScopeValue', '本机日志与导入数据', 'Local logs and imported data');
    $('#aboutVersionValue').textContent = 'v'+(S.boot?.version || '1.2.3');
    set('#aboutVersionLabel', '版本', 'Version');
    set('#aboutAuthorLabel', '作者', 'Author');
    set('#aboutCopy', 'TokenLens 汇总本机和导入的 AI 用量，支持费用核对、分页明细与三项并行 AI 任务。'
      + '用量统计在本机完成；使用 AI 功能时，输入会发送到你选择的服务。',
      'TokenLens combines local and imported AI usage, with cost review, paged records and concurrent AI tools. '
      + 'Usage analytics stay local. AI features send your input to the service you choose.');
    $('#aboutMask').classList.add('on');
    $('#aboutClose').focus();
  }

  function closeAbout() {
    $('#aboutMask').classList.remove('on');
  }

  function openMethodology() {
    $('#methodMask').classList.add('on');
    $('#methodClose').focus();
  }

  function closeMethodology() {
    $('#methodMask').classList.remove('on');
  }

  const IQ_STORE = 'tl.iq.last.v2';
  // 智商检测的 Key 只放在当前浏览器会话（sessionStorage）。它不会写入
  // localStorage/后端配置，关闭应用或标签页后由浏览器自动清除。
  const IQ_RUNTIME_KEY = 'tl.iq.runtime.key.v1';
  const IQ = { tab: localStorage.getItem('tl.iq.tab') || 'q1', results: {}, html: '', htmlSource: '', profileId: '', availability: { ok: false, cached: false, checked: false }, batch: false, anyRunning: {}, abort: {} };
  /* 检测状态只显示在当前题目的活动框内，避免页面顶部出现与当前题目脱节的全局提示。 */
  const IQ_STAGES = {
    q1: ['正在连接模型…', '模型正在推理…', '正在输出公开回答…', '正在核对最终答案…'],
    q2: ['正在连接模型…', '模型正在推理…', '正在输出公开回答…', '正在核对最终答案…'],
    pelican: ['正在连接模型…', '正在分析创作要求…', '正在生成 SVG 动画…', '正在准备预览…'],
  };
  // 每个题目各自维护计时器，三题并发运行时仍能看到实时阶段和耗时。
  const IQ_LIVE = new Map();
  function ensureIqActivityTrack(n) {
    if (!$('.iq-activity-track', n)) {
      n.insertAdjacentHTML('beforeend', '<span class="iq-activity-track" aria-hidden="true"><i></i><i></i><i></i><i></i></span>');
    }
  }
  function iqActivity(mode, state, text, sec, stage = -1) {
    const id = mode === 'pelican' ? 'iqLivePelican' : 'iqLive' + mode.toUpperCase();
    const n = $('#' + id); if (!n) return;
    ensureIqActivityTrack(n);
    n.className = 'iq-activity ' + state;
    n.dataset.stage = String(stage);
    const copy = $('.iq-activity-copy', n); if (copy) copy.textContent = text;
    const elapsed = $('.iq-activity-time', n); if (elapsed) elapsed.textContent = sec == null ? '--' : sec.toFixed(1) + 's';
    if (elapsed && state === 'live') delete elapsed.dataset.countup;
    $$('.iq-activity-track i', n).forEach((dot, index) => dot.classList.toggle('on', state === 'live' && index <= stage));
    if (elapsed && sec != null && state !== 'live') {
      elapsed.dataset.countup = '';
      animateNumericElements(elapsed);
    }
  }
  function iqLiveStart(mode) {
    const stages = IQ_STAGES[mode] || IQ_STAGES.q1;
    const t0 = Date.now();
    iqLiveStop(mode);
    // 阶段由服务端真实事件推进（见 iqStreamStage）；这里的定时器只负责
    // 秒表和「还在等首包」的兜底文案，不再用固定间隔假装推进阶段。
    const tick = () => {
      const sec = (Date.now() - t0) / 1000;
      const st = IQ_STREAM[mode];
      let i = 0, note = '正在连接模型…';
      if (st && st.reasoning > 0) {
        i = st.content > 0 ? 2 : 1;
        note = st.content > 0 ? '模型正在输出公开回答' : `模型正在推理（已思考 ${st.reasoning} 字）`;
      } else if (sec > 8) {
        note = '上游响应较慢，仍在等待首个输出';
      }
      iqActivity(mode, 'live', `${stages[i]} · ${note}`, sec, i);
    };
    tick();
    IQ_LIVE.set(mode, setInterval(tick, 500));
  }
  function iqLiveStop(mode) {
    if (mode) {
      const timer = IQ_LIVE.get(mode);
      if (timer) clearInterval(timer);
      IQ_LIVE.delete(mode);
      return;
    }
    IQ_LIVE.forEach((timer) => clearInterval(timer));
    IQ_LIVE.clear();
  }
  function iqLiveDone(mode, text, ok) {
    iqLiveStop(mode);
    iqActivity(mode, ok ? 'ok' : 'bad', text, null, ok ? IQ_STAGES[mode].length : -1);
  }
  function iqPreset(model, url) {
    if(window.TLCCSwitch)window.TLCCSwitch.clear();
    $('#iqProfileSelect').value = '';
    IQ.profileId = '';
    $('#iqModel').value = model; $('#iqUrl').value = url; iqInvalidateAvailability(); $('#iqKey').focus();
  }
  function iqSetTab(tab) {
    IQ.tab = tab; localStorage.setItem('tl.iq.tab', tab);
    $$('#iqTabs button').forEach((b) => { const active = b.dataset.iqTab === tab; b.classList.toggle('active', active); b.setAttribute('aria-selected', active ? 'true' : 'false'); });
    $$('.iq-panel').forEach((p) => p.classList.toggle('active', p.dataset.iqPanel === tab));
  }
  function iqSetNavState(mode, text, kind = '') {
    const b = $(`#iqTabs button[data-iq-tab="${mode}"]`); if (!b) return;
    const n = $('.iq-tab-state', b); if (n) n.textContent = text;
    b.classList.toggle('done', !!kind);
    b.classList.toggle('failed', kind === 'bad');
  }
  function iqRenderQuestion(item) {
    const host = $(`#iqQ${item.index}`); if (!host) return;
    const wrap = $(`#iqA${item.index}`), answer = $('.iq-answer', wrap), reason = $('.iq-reason', wrap);
    if(wrap)wrap.hidden=false;
    const failed = !!item.error && !item.text;
    const good = item.correct;
    const verdict = $('.iq-verdict', host);
    verdict.textContent = failed ? '检测失败' : (good ? '该样例通过' : (item.answer == null ? '答案未识别' : '该样例未通过'));
    verdict.className = 'iq-verdict ' + (good ? 'ok' : 'bad');
    if (answer) answer.textContent = item.text || item.error || '模型未返回公开回答。';
    if (reason) reason.textContent = item.explanation || `模型答案：${item.answer == null ? '未识别' : item.answer}；标准答案：${item.expected}。`;
    iqSetNavState('q' + item.index, failed ? '检测失败' : (good ? '通过' : '未通过'), good ? 'ok' : 'bad');
  }
  function iqRenderHtml() {
    const ok = !!IQ.html;
    $('#iqPreview').hidden=!ok;
    $('#iqPreviewPlaceholder').hidden=ok;
    $('#iqHtmlVerdict').textContent = ok ? '已生成' : '未生成';
    $('#iqHtmlVerdict').className = 'iq-verdict ' + (ok ? 'ok' : '');
    $('#iqHtmlOpen').disabled = !ok;
    if (ok) $('#iqPreview').srcdoc = IQ.html;
    else $('#iqPreview').srcdoc = '';
    iqSetNavState('pelican', ok ? '已生成' : '未生成', ok ? 'ok' : '');
    const wrap = $('#iqAPelican');
    if (wrap) {
      wrap.hidden=!(IQ.htmlSource || IQ.html);
      const answer = $('.iq-answer', wrap), reason = $('.iq-reason', wrap);
      if (answer) answer.textContent = IQ.htmlSource || IQ.html || '模型未返回可渲染内容。';
      if (reason) reason.textContent = ok ? '返回内容包含可渲染的 SVG，已加载到右侧预览。' : '返回内容未通过 SVG 可渲染性检查。';
    }
  }
  function iqSave() {
    try {
      localStorage.setItem(IQ_STORE, JSON.stringify({
        config: { model: $('#iqModel').value.trim(), base_url: $('#iqUrl').value.trim(), thinking: $('#iqThinking').value, profile_id: IQ.profileId },
        results: IQ.results, html: IQ.html.slice(0, 300000), htmlSource: (IQ.htmlSource || '').slice(0, 12000), tab: IQ.tab,
      }));
    } catch (_) { /* 本地空间不足时不阻塞检测 */ }
  }
  function iqRestore() {
    try {
      const saved = JSON.parse(localStorage.getItem(IQ_STORE) || 'null');
      if (!saved) return;
      if (saved.config) { $('#iqModel').value = saved.config.model || $('#iqModel').value || 'zai-org/GLM-4.5-Air'; $('#iqUrl').value = saved.config.base_url || $('#iqUrl').value || 'https://api.siliconflow.cn/v1'; $('#iqThinking').value = saved.config.thinking || 'high'; IQ.profileId = saved.config.profile_id || ''; }
      IQ.results = saved.results || {}; IQ.html = saved.html || ''; IQ.htmlSource = saved.htmlSource || ''; IQ.tab = saved.tab || IQ.tab;
      Object.values(IQ.results).forEach(iqRenderQuestion); iqRenderHtml(); iqSetTab(IQ.tab);
      Object.keys(IQ.results).forEach((mode) => iqActivity(mode, 'ok', '已恢复上次结果', null));
      if (IQ.html) iqActivity('pelican', 'ok', '已恢复上次结果', null);
    } catch (_) { /* 忽略损坏的本地缓存 */ }
  }

  function iqSetRunEnabled(enabled) {
    const running=IQ.checking || IQ.batch || Object.values(IQ.anyRunning || {}).some(Boolean);
    $('#iqCCOpen').disabled=running;
    $('#iqManualOpen').disabled=running;
    $('#iqThinking').disabled=running;
    $('#iqAvailabilityBtn').disabled=running || !iqCredsReady();
    $('#iqProfileSelect').disabled=running;
    $$('#iqPresets .iq-preset').forEach(button=>button.disabled=running);
    // 运行中的按钮保持可用——它此刻是「停止」，禁用了就没法中断。
    $$('.iq-run').forEach((b) => {
      const running = IQ.anyRunning && IQ.anyRunning[b.dataset.iqMode];
      b.disabled = !enabled && !running;
    });
    // 批量运行中「一键测试」是「全部停止」，必须保持可点。
    const all = $('#iqRunAllBtn');
    if (all) all.disabled = IQ.batch ? false : !enabled;
  }

  /* 只要三项都填齐（或选了已保存配置）就可以直接开始检测。
     可用性检测是可选的自检，不再是必经的前置步骤——多一步就多一次等待。 */
  function iqCredsReady() {
    const connection=window.TLCCSwitch && window.TLCCSwitch.selected();
    if(connection)return !!connection.model;
    const model = ($('#iqModel').value || '').trim();
    const base_url = ($('#iqUrl').value || '').trim();
    const api_key = ($('#iqKey').value || '').trim();
    const profile_id = IQ.profileId || ($('#iqProfileSelect') && $('#iqProfileSelect').value) || '';
    return !!(model && base_url && (api_key || profile_id));
  }
  function iqAnyRunning() {
    return Object.keys(IQ.anyRunning || {}).some((k) => IQ.anyRunning[k]);
  }
  function iqSyncRunState() {
    iqSetRunEnabled(!IQ.checking && !IQ.batch && !iqAnyRunning() && iqCredsReady());
  }

  function iqUpdateConnectionSummary(){
    const connection=window.TLCCSwitch && window.TLCCSwitch.selected(),en=S.language==='en-US';
    const ready=iqCredsReady();
    $('#iqCurrentModel').textContent=ready?$('#iqModel').value.trim():(en?'Choose a model':'尚未选择模型');
    const protocols={responses:'Responses',openai:'Chat Completions',anthropic:'Anthropic Messages',gemini:'Gemini'};
    $('#iqCurrentSource').textContent=connection?`${connection.name} · ${protocols[connection.protocol] || connection.protocol}`:ready?(IQ.profileId?(en?'Saved connection':'已保存的连接'):(en?'Custom API connection':'自定义 API 连接')):(en?'Choose from CC Switch or configure another connection':'从 CC Switch 选择，或配置其他连接');
    $('#iqCCOpen').textContent=connection?(en?'Change model':'切换模型'):(en?'Choose model':'选择模型');
  }

  let iqManualSnapshot=null;
  function iqOpenManual(){
    iqManualSnapshot={connection:window.TLCCSwitch&&window.TLCCSwitch.selected(),model:$('#iqModel').value,url:$('#iqUrl').value,key:$('#iqKey').value,profile:IQ.profileId};
    try{iqManualSnapshot.runtimeKey=sessionStorage.getItem(IQ_RUNTIME_KEY);}catch(_){}
    if(window.TLCCSwitch)window.TLCCSwitch.clear();
    $('#iqManualDialog').showModal();$('#iqProfileSelect').focus();
  }
  function iqCloseManual(accept=false){
    if(accept&&!iqCredsReady())return toast(S.language==='en-US'?'Enter a model, API URL and key or choose a saved profile':'请填写模型、API 地址和密钥，或选择已保存配置','err');
    if(!accept&&iqManualSnapshot){
      const saved=iqManualSnapshot;
      IQ.profileId=saved.profile;$('#iqProfileSelect').value=saved.profile;$('#iqModel').value=saved.model;$('#iqUrl').value=saved.url;$('#iqKey').value=saved.key;
      if(saved.connection&&window.TLCCSwitch)window.TLCCSwitch.restore(saved.connection);
      try{if(Object.hasOwn(saved,'runtimeKey')){if(saved.runtimeKey!==null)sessionStorage.setItem(IQ_RUNTIME_KEY,saved.runtimeKey);else sessionStorage.removeItem(IQ_RUNTIME_KEY);}}catch(_){}
    }
    iqManualSnapshot=null;$('#iqManualDialog').close();iqInvalidateAvailability();if(accept)iqSave();
  }

  function iqInvalidateAvailability() {
    IQ.availability = { ok: false, cached: false, checked: false };
    iqSyncRunState();
    const btn = $('#iqAvailabilityBtn');
    if (btn) { btn.disabled = IQ.checking || IQ.batch || iqAnyRunning() || !iqCredsReady(); btn.classList.remove('iq-check-ok', 'iq-check-bad'); btn.innerHTML = '<i class="ico i-radar"></i>'+(S.language==='en-US'?'Check':'自检'); }
    const tip = $('#iqTip');
    if (tip) {
      tip.classList.remove('iq-check-ok', 'iq-check-bad');
      tip.textContent = iqCredsReady()
        ? (S.language==='en-US'?'Ready to test · Check is optional':'可直接开始检测 · 自检可选')
        : (S.language==='en-US'?'Choose a model to start':'选择模型后即可开始检测');
    }
    iqUpdateConnectionSummary();
    iqRefreshThinking();
  }

  let iqThinkingRequest=0;
  async function iqRefreshThinking(){
    const request=++iqThinkingRequest,connection=window.TLCCSwitch&&window.TLCCSwitch.selected();
    const profile=(PL.cfg&&PL.cfg.profiles||[]).find(row=>row.id===IQ.profileId);
    const query=new URLSearchParams({model:$('#iqModel').value,protocol:connection?connection.protocol:profile?profile.protocol_effective:'auto',base_url:$('#iqUrl').value});
    try{const result=await api('/api/ai-thinking?'+query);if(request!==iqThinkingRequest)return;const select=$('#iqThinking'),previous=select.value,labels={auto:'自动',none:'不思考',minimal:'极低',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高'};
      select.innerHTML=result.levels.map(value=>`<option value="${esc(value)}">${isEn()?value:labels[value]||value}</option>`).join('');select.value=result.levels.includes(previous)?previous:'auto';$('#iqThinkingNote').textContent=(result.note || '')+(result.control==='reasoning_effort'?' GPT 公开推理摘要需要 Responses 协议；Chat 接口未返回摘要不代表未思考。':'');
    }catch(_){if(request===iqThinkingRequest)$('#iqThinkingNote').textContent='暂时无法读取模型思考参数，请重新选择模型。';}
  }

  function renderIqAvailability(res) {
    const btn = $('#iqAvailabilityBtn');
    const tip = $('#iqTip');
    const ok = !!(res && res.ok);
    IQ.availability = { ok, cached: !!(res && res.cached), checked: true };
    iqSyncRunState();
    if (btn) {
      btn.disabled = false;
      btn.classList.toggle('iq-check-ok', ok);
      btn.classList.toggle('iq-check-bad', !ok);
      btn.innerHTML = ok ? '<i class="ico i-check"></i>自检通过' : '<i class="ico i-radar"></i>重新自检';
    }
    if (tip) {
      tip.classList.toggle('iq-check-ok', ok);
      tip.classList.toggle('iq-check-bad', !ok);
      if (ok) {
        const ms = res.timing && res.timing.ttfb_ms ? ` · 首包 ${res.timing.ttfb_ms}ms` : '';
        tip.textContent = (S.language === 'en-US' ? 'Quick check passed' : '模型能正常返回内容') + ms + (res.cached ? (S.language === 'en-US' ? ' · cached' : ' · 使用缓存') : '');
      } else tip.textContent = (res && (res.detail || res.error)) || (S.language === 'en-US' ? 'Availability check failed' : '可用性自检未通过');
    }
  }

  async function runIqAvailability(force) {
    if(IQ.checking || IQ.batch || iqAnyRunning())return;
    const connection=window.TLCCSwitch && window.TLCCSwitch.selected();
    const model = $('#iqModel').value.trim(), base_url = $('#iqUrl').value.trim(), api_key = $('#iqKey').value.trim();
    const profile_id = IQ.profileId || ($('#iqProfileSelect') && $('#iqProfileSelect').value) || '';
    if (!model || !base_url) return toast('请填写 AI Model 和 API URL', 'err');
    if (!api_key && !profile_id && !connection) return toast('请填写 API Key，或选择已保存的 AI 配置 / CC Switch 模型', 'err');
    const btn = $('#iqAvailabilityBtn');
    const tip = $('#iqTip');
    // 点击按钮重测时始终 force：绕开缓存，保证「重新测试」真的重新发请求。
    const useForce = force !== false;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin" style="width:13px;height:13px;border-width:2px;margin:0"></span>快速自检中…'; }
    IQ.checking=true;
    iqSetRunEnabled(false);
    const t0 = Date.now();
    // 上游偶尔整体排队十几秒。如实显示已等待时间，用户就知道是在等而不是卡死。
    const ticker = setInterval(() => {
      const sec = (Date.now() - t0) / 1000;
      if (tip && sec > 1.5) {
        tip.textContent = `正在等待模型响应…已等待 ${sec.toFixed(1)} 秒` +
          (sec > 6 ? '（上游排队中，通常几秒内会返回）' : '');
      }
    }, 200);
    try {
      const body=connection ? {ccswitch_id:connection.ccswitch_id,ccswitch_protocol:connection.protocol,model:connection.model,force:useForce} : {model,base_url,api_key,profile_id,force:useForce};
      const res = await api('/api/ai-availability', { method: 'POST', body });
      renderIqAvailability(res);
      const sec = ((Date.now() - t0) / 1000).toFixed(1);
      if (res.ok) toast(`AI 快速检测通过（用时 ${sec} 秒）`, 'ok', 2200);
      else toast(res.detail || res.error || 'AI 可用性检测未通过', 'err', 4200);
    } catch (e) {
      const b = e.body || {};
      renderIqAvailability({ ok: false, detail: b.detail || b.error || e.message });
      toast(b.detail || b.error || e.message || 'AI 可用性检测失败', 'err', 4200);
    } finally {
      clearInterval(ticker);
      IQ.checking=false;iqSyncRunState();
    }
  }
  async function runAiTest(mode, fromBatch = false) {
    if(IQ.checking)return;
    if (IQ.batch && !fromBatch) return;
    if (IQ.anyRunning && IQ.anyRunning[mode]) return;
    const connection=window.TLCCSwitch && window.TLCCSwitch.selected();
    const model = $('#iqModel').value.trim(), base_url = $('#iqUrl').value.trim(), api_key = $('#iqKey').value.trim();
    const profile_id = IQ.profileId || ($('#iqProfileSelect') && $('#iqProfileSelect').value) || '';
    if (!model || !base_url || (!api_key && !profile_id && !connection)) return toast('请填写 AI Model、API URL 和 API Key，或选择已保存配置 / CC Switch 模型', 'err');
    const btn = $(`.iq-run[data-iq-mode="${mode}"]`); if (!btn) return;
    const idleHtml = btn.innerHTML;
    IQ.anyRunning = IQ.anyRunning || {};
    IQ.anyRunning[mode] = true;
    iqSetRunEnabled(false);
    $$('.iq-run').forEach((b) => { if (b !== btn) b.disabled = true; });
    // 运行中的按钮同时是「停止」：再点一下直接中断（AbortController 断开
    // SSE，服务端检测到连接关闭后把 cancel 传给上游，HTTP 请求随之终止）。
    IQ.abort = IQ.abort || {};
    IQ.abort[mode] = (typeof AbortController === 'function') ? new AbortController() : null;
    btn.classList.add('stopping');
    btn.innerHTML = '<i class="ico i-close"></i>停止';
    const label = mode === 'pelican' ? 'SVG 创作' : `逻辑题 ${mode.slice(-1)}`;
    // 实时思考面板：模型一开口就能看到内容，不必对着转圈等几分钟。
    iqLiveStart(mode);
    iqOpenStream(mode);
    if (mode === 'q1' || mode === 'q2') { const verdict = $('.iq-verdict', $(`#iqQ${mode.slice(-1)}`)); verdict.textContent = '检测中'; verdict.className = 'iq-verdict'; }
    const t0 = Date.now();
    let final = null;
    let cancelled = false;
    try {
      await apiStream('/api/ai-test-stream',
        connection ? {ccswitch_id:connection.ccswitch_id,ccswitch_protocol:connection.protocol,model:connection.model,thinking:$('#iqThinking').value,mode}
          : { model, base_url, api_key, profile_id, thinking: $('#iqThinking').value, mode },
        (ev) => {
          if (ev.type === 'stage') iqStreamStage(mode, ev.stage);
          else if (ev.type === 'reasoning') iqStreamAppend(mode, 'reasoning', ev.text, ev.chars);
          else if (ev.type === 'content') iqStreamAppend(mode, 'content', ev.text, ev.chars);
          else if (ev.type === 'result') final = ev;
        }, IQ.abort[mode] && IQ.abort[mode].signal);
      if (!final) throw new Error('连接中断，没有收到最终结果');
      if (!final.ok) {
        // 失败时不要把已经收到的思考过程丢掉：那是用户判断"到底发生了什么"的唯一线索。
        const st = IQ_STREAM[mode];
        if (st && st.reasoning) {
          iqStreamFail(mode, final.error || '模型没有返回公开回答',
            `模型已经思考了 ${st.reasoning} 字，但没有产出公开回答。通常是上游在思考阶段就结束了这次生成，重试一次通常就好了。`);
        }
        throw new Error(final.error || '检测失败');
      }
      const sec = ((Date.now() - t0) / 1000).toFixed(1);
      if (mode === 'q1' || mode === 'q2') { IQ.results[mode] = final.question; iqRenderQuestion(final.question); }
      else { IQ.html = final.html || ''; IQ.htmlSource = final.source_text || ''; iqRenderHtml(); if (!final.html_ok) throw new Error(final.html_error || '模型没有返回可渲染的 SVG HTML'); }
      iqSave();
      if(final.reasoning && IQ_STREAM[mode] && !IQ_STREAM[mode].reasoning)iqStreamAppend(mode,'reasoning',final.reasoning,final.reasoning.length);
      iqStreamDone(mode);
      const applied=final.thinking_applied||{},parameters=applied.parameters||[];
      $('#iqThinkingNote').textContent=(parameters.length?`本次已发送 ${parameters.join('、')}（${applied.requested||$('#iqThinking').value}）`:'本次使用服务商默认思考配置')+(final.reasoning_chars?' · 已返回公开推理摘要':' · 服务商未返回公开推理摘要')+(applied.parameters&&applied.parameters.includes('reasoning_effort')?'，可切换 Responses 请求 GPT 摘要':'');
      const failed = mode !== 'pelican' && final.question.error && !final.question.text;
      const ok = mode === 'pelican' || final.question.correct;
      const word = mode === 'pelican' ? '已完成' : (failed ? '检测失败' : (final.question.correct ? '该样例通过' : '该样例未通过'));
      const think = final.reasoning_chars ? ` · 思考 ${final.reasoning_chars} 字` : '';
      iqLiveDone(mode, `${label} · ${word} · 用时 ${sec}s${think}`, ok);
      if (ok) toast(`${label}${word}，用时 ${sec} 秒`, 'ok');
      else if (failed) toast(`${label} 检测失败：${final.question.error || '模型没有返回公开回答'}`, 'err', 5200);
      else toast(`${label}：${word}（用时 ${sec} 秒）`, 'err', 5200);
    } catch (e) {
      cancelled = e.name === 'AbortError'
        || /cancel/i.test((e.body && (e.body.error || e.body.code)) || '')
        || /已取消|cancelled/i.test(e.message || '');
      iqStreamDone(mode, cancelled ? 'cancel' : true);
      iqLiveDone(mode, `${label} · ${cancelled ? '已停止' : '检测失败'}`, false);
      // 已收到的思考过程保留在面板里——它记录了停止前模型在做什么。
      toast(cancelled
        ? (isEn() ? `${label} stopped. Text received so far is kept.` : `${label} 已停止，已收到的内容保留在面板中`)
        : (e.message || '检测失败'), cancelled ? 'ok' : 'err', 4200);
    } finally {
      IQ.anyRunning[mode] = false;
      IQ.abort[mode] = null;
      btn.classList.remove('stopping');
      btn.innerHTML = idleHtml;
      iqSyncRunState();
    }
  }

  /* ---- 实时思考面板：模型一边想，用户一边看 ----
     推理型模型（GLM-4.5-Air 实测）会在正文前先产出 1.5~6 万字的思维链，
     正文要等到 200 秒之后才出现。把思维链实时显示出来，等待就不再是黑箱。

     性能上有三个坑，都按下面这样处理掉了（否则会明显卡顿）：
       1) 上游一秒能推几十个增量。**逐条写 DOM 会因为强制重排而抖动**，
          所以先攒进缓冲，每个动画帧只刷一次。
       2) 思维链能到 6 万字。全量留在 DOM 里，每次追加都会让整块文本重新折行。
          所以只保留末尾 RENDER_TAIL_CHARS 字（计数器仍显示真实总量）。
       3) pre.scrollTop = scrollHeight 每次都强制同步布局。改成只在
          「用户本来就在底部」时跟随，并且一帧最多滚一次。 */
  const IQ_STREAM = {};
  const RENDER_TAIL_CHARS = 20000;   // DOM 里最多保留多少字（仅影响显示）
  const STREAM_FLUSH_MS = 60;        // 帧间隔兜底（后台标签页 rAF 不跑）

  function iqStreamHost(mode) {
    return mode === 'pelican' ? $('#iqStreamPelican') : $(`#iqStream${mode.toUpperCase()}`);
  }

  function iqOpenStream(mode) {
    const host = iqStreamHost(mode); if (!host) return;
    host.hidden = false;
    host.classList.add('live');
    host.innerHTML = `
      <div class="iq-stream-head">
        <span class="iq-stream-title"><i class="iq-stream-pulse" aria-hidden="true"></i>推理摘要与公开回答</span>
        <span class="iq-stream-stats" data-stats>等待模型响应…</span>
      </div>
      <div class="iq-stream-body" data-body><span class="iq-stream-idle">模型还没有开始输出。</span></div>`;
    // 一次性取好节点引用，之后不再 querySelector（每帧都在查 DOM 也会拖慢）。
    IQ_STREAM[mode] = {
      reasoning: 0, content: 0, phase: 'connect',
      host, body: $('[data-body]', host), stats: $('[data-stats]', host),
      nodes: {}, pend: { reasoning: [], content: [] },
      raf: 0, timer: 0, lastFlush: 0, dirty: false,
    };
  }

  function iqStreamStage(mode, stage) {
    const st = IQ_STREAM[mode]; if (!st) return;
    st.phase = stage;
    if (st.stats && !st.reasoning && !st.content) {
      st.stats.textContent = stage === 'thinking' ? '模型正在推理…'
        : (stage === 'answering' ? '正在输出公开回答…'
          : (stage === 'judging' ? '正在核对答案…' : '正在连接模型…'));
    }
  }

  // 保证某一类内容（思考/回答）的容器存在，并缓存引用。
  function iqStreamBlock(st, kind) {
    let node = st.nodes[kind];
    if (node) return node;
    if (!st.body) return null;
    const idle = $('.iq-stream-idle', st.body);
    if (idle) idle.remove();
    st.body.insertAdjacentHTML('beforeend',
      `<div class="iq-stream-block ${kind}"><span class="iq-stream-label">${
        kind === 'reasoning' ? '公开推理摘要' : '公开回答'}</span><pre class="iq-stream-pre" data-kind="${kind}"></pre></div>`);
    const pre = $('pre', st.body.lastElementChild);
    if (!pre) return null;
    // 用一个 Text 节点承载全部文本，追加走 appendData（不新建节点）。
    const textNode = document.createTextNode('');
    pre.appendChild(textNode);
    node = st.nodes[kind] = { wrap: st.body.lastElementChild, pre, text: textNode,
                              len: 0, omitted: 0 };
    // 用户手动往上翻时不要把他拽回底部；贴底时才自动跟随。
    node.pinned = true;
    pre.addEventListener('scroll', () => {
      node.pinned = (pre.scrollHeight - pre.scrollTop - pre.clientHeight) < 48;
      st.host.classList.toggle('unpinned', !node.pinned);
    }, { passive: true });
    return node;
  }

  function iqStreamAppend(mode, kind, text, chars) {
    const st = IQ_STREAM[mode]; if (!st || !text) return;
    st.pend[kind].push(text);
    st[kind] = chars || (st[kind] + text.length);
    st.dirty = true;
    iqStreamSchedule(mode);
  }

  function iqStreamSchedule(mode) {
    const st = IQ_STREAM[mode]; if (!st) return;
    if (st.raf || st.timer) return;
    const run = () => {
      if (st.raf) cancelAnimationFrame(st.raf);
      if (st.timer) clearTimeout(st.timer);
      st.raf = 0; st.timer = 0; iqStreamFlush(mode);
    };
    st.raf = requestAnimationFrame(run);
    // 后台标签页 rAF 不触发，用定时器兜底，避免恢复时一次性涌入。
    st.timer = setTimeout(run, STREAM_FLUSH_MS * 4);
  }

  function iqStreamFlush(mode) {
    const st = IQ_STREAM[mode]; if (!st || !st.dirty) return;
    st.dirty = false;
    for (const kind of ['reasoning', 'content']) {
      const buf = st.pend[kind];
      if (!buf.length) continue;
      const node = iqStreamBlock(st, kind);
      const chunk = buf.join('');
      buf.length = 0;
      if (!node) continue;
      node.text.appendData(chunk);
      node.len += chunk.length;
      // 只保留末尾若干字：整块文本越短，折行重排越便宜。
      if (node.len > RENDER_TAIL_CHARS) {
        const drop = node.len - RENDER_TAIL_CHARS;
        node.text.deleteData(0, drop);
        node.len -= drop;
        node.omitted += drop;
        node.wrap.classList.add('trimmed');
        node.wrap.dataset.omitted = String(node.omitted);
      }
    }
    if (st.stats) {
      st.stats.textContent = `思考 ${st.reasoning} 字` + (st.content ? ` · 回答 ${st.content} 字` : '');
    }
    // 一帧只滚一次，且仅在用户贴底时跟随。
    const r = st.nodes.reasoning;
    if (r && r.pinned) r.pre.scrollTop = r.pre.scrollHeight;
  }

  /* 收尾：停止呼吸动效，把状态栏定稿。failed 为 'cancel' 时是用户主动
     停止——状态写「已中断」但不标红；失败时保留已经收到的思考过程，
     并在面板里说明原因——用户最需要知道的就是"它想到哪一步断的"。 */
  function iqStreamDone(mode, failed) {
    const host = iqStreamHost(mode); if (!host) return;
    host.classList.remove('live');
    host.classList.remove('unpinned');
    if (failed === 'cancel') {
      host.classList.add('cancelled');
    } else if (failed) {
      host.classList.add('failed');
    }
    iqStreamFlush(mode);
    const st = IQ_STREAM[mode];
    if (st) {
      const tail = st.content ? ` · 回答 ${st.content} 字` : '';
      if (st.stats) {
        st.stats.textContent = (failed === 'cancel' ? '已停止 · '
          : (failed ? '已中断 · ' : '已完成 · ')) +
          (st.reasoning?`推理摘要 ${st.reasoning} 字`:'服务商未返回推理摘要') + tail;
      }
      cancelAnimationFrame(st.raf); clearTimeout(st.timer);
      st.raf = 0; st.timer = 0;
    }
  }
  function iqStreamFail(mode, reason, hint) {
    const host = iqStreamHost(mode); if (!host) return;
    host.classList.remove('live');
    host.classList.add('failed');
    const st = IQ_STREAM[mode];
    const body = (st && st.body) || $('[data-body]', host);
    if (body) {
      const stale = $('.iq-stream-note', body);
      if (stale) stale.remove();
      body.insertAdjacentHTML('beforeend',
        `<div class="iq-stream-note"><b>${esc(reason || '检测失败')}</b>${hint ? ' ' + esc(hint) : ''}</div>`);
    }
    if (st && st.stats) st.stats.textContent = `已中断 · 思考 ${st.reasoning} 字`;
  }

  /* 一键测试三题：并发执行，让三道题同时推进、同时可见实时思考过程。
     上游按请求计费但并发上限足够，实测三题并发互不拖慢。
     批量运行中按钮变为「全部停止」：一次中断所有仍在跑的请求。 */
  async function runIqAll() {
    if (IQ.batch) {
      // 停止全部仍在运行的题目
      Object.values(IQ.abort || {}).forEach((a) => { if (a) a.abort(); });
      return;
    }
    if (!iqCredsReady()) return toast('请填写 AI Model、API URL 和 API Key，或选择已保存配置', 'err');
    const button = $('#iqRunAllBtn');
    if (!button || button.disabled) return;
    IQ.batch = true;
    button.disabled = false;              // 保持可点：现在它是「全部停止」
    button.classList.add('stopping');
    button.innerHTML = '<i class="ico i-close"></i>全部停止';
    try {
      await Promise.all(['q1', 'q2', 'pelican'].map((mode) => runAiTest(mode, true)));
      toast('三题测试已完成', 'ok', 3000);
    } finally {
      IQ.batch = false;
      button.classList.remove('stopping');
      button.innerHTML = '<i class="ico i-radar"></i>检测全部';
      iqSyncRunState();
    }
  }

  function updateEndpointPreview() {
    const f = plForm();
    let proto = 'openai';
    const u = f.base_url.toLowerCase();
    if (u.includes('anthropic') || u.endsWith('/v1/messages')) proto = 'anthropic';
    else if (u.includes('generativelanguage.googleapis.com') || u.includes(':generatecontent')) proto = 'gemini';
    $('#plProto').textContent = protoLabel(proto);
    const base = f.base_url.replace(/\/+$/, '');
    let ep = '—';
    if (base) {
      if (proto === 'openai') {
        ep = base.endsWith('/chat/completions') ? base
          : (base.endsWith('/v1') || base.includes('/v1') || base.includes('/api')
            ? base + '/chat/completions' : base + '/v1/chat/completions');
      } else if (proto === 'anthropic') {
        ep = base.endsWith('/messages') ? base
          : (base.endsWith('/v1') ? base + '/messages' : base + '/v1/messages');
      } else {
        ep = base.includes(':generateContent') ? base
          : base + '/models/' + (f.model || '{model}') + ':generateContent';
      }
    }
    $('#plEndpoint').textContent = ep;
  }

  function setTestButtons(busy) {
    $('#plTestBtn').disabled = busy;
    $('#plTestBtn').innerHTML = busy
      ? '<span class="spin" style="width:13px;height:13px;border-width:2px;margin:0"></span>检测中…'
      : '<i class="ico i-radar"></i>可用性检测';
  }

  async function runCheck() {
    if (PL.testing) return;
    const f = plForm();
    if (!f.model) return toast('请先填写模型名', 'err');
    if (!f.base_url) return toast('请先填写 API 地址', 'err');
    if (!f.api_key && !(PL.cfg && PL.cfg.configured)) return toast('请先填写 API Key', 'err');

    PL.testing = true;
    setTestButtons(true);
    $('#plSaveBtn').disabled = true;
    $('#plResult').innerHTML = `
      <div class="pl-loading"><span class="spin" style="margin:0;width:16px;height:16px;border-width:2px"></span>
        <span id="plCheckWait">正在快速确认模型能否返回内容…</span><span class="pl-bar"></span></div>`;
    const t0 = Date.now();
    const ticker = setInterval(() => {
      const n = $('#plCheckWait'); if (!n) return;
      const sec = (Date.now() - t0) / 1000;
      if (sec > 1.5) {
        n.textContent = `正在等待模型响应…已等待 ${sec.toFixed(1)} 秒` +
          (sec > 6 ? '（上游排队中，通常几秒内会返回）' : '');
      }
    }, 200);
    try {
      const res = await api('/api/prompt/check', { method: 'POST', body: f });
      renderCheckResult(res);
      $('#plSaveBtn').disabled = !res.can_save;
      if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = !res.can_save;
      if (res.can_save) toast('检测通过，可以保存了', 'ok', 2600);
    } catch (e) {
      const b = e.body;
      if (b && b.code) {
        renderCheckResult(b);
        $('#plSaveBtn').disabled = true;
        if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = true;
      } else {
        $('#plResult').innerHTML =
          `<div class="pl-check err"><div class="pl-check-head">
             <span class="pl-check-icon">!</span><span class="pl-check-title">检测请求失败</span></div>
           <div class="pl-check-detail">${esc(e.message)}</div></div>`;
        $('#plSaveBtn').disabled = true;
        if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = true;
      }
    } finally {
      clearInterval(ticker);
      PL.testing = false;
      setTestButtons(false);
    }
  }

  function renderCheckResult(r) {
    const lvl = r.level === 'ok' ? 'ok' : (r.level === 'slow' ? 'slow' : 'err');
    const glyph = lvl === 'ok' ? '✓' : (lvl === 'slow' ? '!' : '×');
    const t = r.timing || {};
    const ms = t.ttfb_ms || t.total_ms || 0;
    const en = isEn();
    // 检测结论与修复建议由后端生成，同时给中英两份；按当前语言取。
    const pick = (k) => (en && r[k + '_en'] ? r[k + '_en'] : r[k]);
    const met = [];
    met.push(`<div class="pl-metric${ms > 0 ? ' good' : ''}">
      <span>${r.probe_mode === 'models' ? (en ? 'Response time' : '响应耗时') : (en ? 'First response' : '首个响应')}</span><b data-countup>${ms ? ms + ' ms' : '—'}</b></div>`);
    met.push(`<div class="pl-metric${t.total_ms > 0 ? ' good' : ''}">
      <span>${en ? 'Total time' : '总耗时'}</span><b data-countup>${t.total_ms ? t.total_ms + ' ms' : '—'}</b></div>`);
    met.push(`<div class="pl-metric"><span>${en ? 'HTTP status' : 'HTTP 状态'}</span><b>${r.http_status || '—'}</b></div>`);
    met.push(`<div class="pl-metric"><span>${en ? 'Protocol' : '协议'}</span><b style="font-size:11px">${
      protoLabel(r.protocol).split('（')[0]}</b></div>`);
    if (r.model_echo) met.push(`<div class="pl-metric" style="min-width:auto">
      <span>${en ? 'Returned model' : '返回模型'}</span><b style="font-size:11px">${esc(r.model_echo)}</b></div>`);
    if (r.usage && (r.usage.input || r.usage.output)) {
      met.push(`<div class="pl-metric" style="min-width:auto"><span>${en ? 'Usage' : '本次用量'}</span>
        <b data-countup style="font-size:11px">${r.usage.input} / ${r.usage.output}</b></div>`);
    }
    // 推理型模型先输出思维链属于正常行为，这里说明判定依据，避免误判为失败。
    const thinkNote = (r.ok && r.reply === '(推理输出)')
      ? `<div class="pl-help" style="margin-top:8px">${en
          ? 'This is a reasoning model. It returned content normally, so it is considered available.'
          : '该模型为推理型模型：已确认服务正常返回内容，判定为可用。'}</div>` : '';
    // 走的是更快的 GET /models 通道。面向最终用户，这里只陈述结论，
    // 不展开探测方式与实现细节。
    const modeNote = (r.ok && r.probe_mode === 'models')
      ? `<div class="pl-help" style="margin-top:8px">${en
          ? `Connection verified: the endpoint is reachable and the API Key is valid${
              r.model_listed === true ? ', and the model name appears in the service list' : ''}.`
          : `连接验证通过：服务地址可访问、API Key 有效${
              r.model_listed === true ? '、模型名称在服务列表中' : ''}。`}</div>` : '';

    let attempts = '';
    if ((r.attempts || []).length > 1) {
      attempts = `<div class="pl-help" style="margin-top:8px">${en ? 'Request adaptation log: ' : '请求参数自适应记录：'}` +
        r.attempts.map((a) => `${esc(a.variant)} → ${a.status || a.error || (en ? 'failed' : '失败')}`).join(en ? '; ' : '；') +
        '</div>';
    }
    const diagnostic = [
      `TokenLens AI connection diagnostic`,
      `model: ${r.model_echo || $('#plModel')?.value || ''}`,
      `endpoint: ${r.endpoint || ''}`,
      `protocol: ${r.protocol || ''}`,
      `code: ${r.code || ''}`,
      `http_status: ${r.http_status || ''}`,
      `response_ms: ${ms || 0}`,
      `total_ms: ${t.total_ms || 0}`,
      r.detail ? `detail: ${pick('detail')}` : '',
      pick('hint') ? `hint: ${pick('hint')}` : '',
      (r.attempts || []).length ? `attempts: ${(r.attempts || []).map((a) => `${a.variant}=${a.status || a.error || 'failed'}`).join('; ')}` : '',
    ].filter(Boolean).join('\n');
    $('#plResult').innerHTML = `
      <div class="pl-check ${lvl}">
        <div class="pl-check-head">
          <span class="pl-check-icon">${glyph}</span>
          <span class="pl-check-title">${esc(pick('title') || (en ? 'Check complete' : '检测完成'))}${r.ok ? (en ? ' (ready to save)' : '（可以保存）') : ''}</span>
          <span class="pl-check-code">${esc(r.code || '')}</span>
        </div>
        ${r.ok && r.reply && r.reply !== '(推理输出)' ? `<div class="pl-check-detail">${en ? 'Model reply: ' : '模型回复：'}${esc(r.reply)}</div>` : ''}
        ${!r.ok && r.detail ? `<div class="pl-check-detail">${esc(pick('detail'))}</div>` : ''}
        <div class="pl-metrics">${met.join('')}</div>
        ${thinkNote}
        ${modeNote}
        ${pick('hint') ? `<div class="pl-check-hint"><b>${en ? 'Recommendation: ' : '处理建议：'}</b>${esc(pick('hint'))}</div>` : ''}
        ${attempts}
        <div class="pl-check-tools"><button type="button" class="btn btn-ghost btn-xs" id="plCopyDiag"><i class="ico i-copy"></i>${en ? 'Copy diagnostic' : '复制诊断信息'}</button></div>
      </div>`;
    animateNumericElements($('#plResult'));
    const copy = $('#plCopyDiag');
    if (copy) copy.onclick = () => copyText(diagnostic, en ? 'Diagnostic copied' : '诊断信息已复制');
  }

  const RUN_BTN_IDLE = '<i class="ico i-spark"></i>开始优化';
  // 运行中直接显示「停止」：用户不需要猜"再点一下会发生什么"。
  // 小转圈仍在转，说明请求确实还在跑。
  const RUN_BTN_BUSY = '<span class="spin" style="width:13px;height:13px;border-width:2px;margin:0"></span>停止';
  const RUN_BTN_STOP = RUN_BTN_BUSY;

  async function runOptimize() {
    // 运行中再点一次 = 停止。中断走 AbortController：fetch 断开后，
    // 服务端 sse_run 检测到连接关闭会把 cancel 信号传给上游 worker，
    // 上游 HTTP 请求随之终止，不再继续烧 token。
    if (PL.running && PL.abort) {
      PL.abort.abort();
      return;
    }
    if (PL.running) return;
    const prompt = normalizePromptWhitespace($('#plInput').value);
    if (!prompt) return toast('请先输入需要优化的提示词', 'err');
    if ($('#plInput').value !== prompt) {
      $('#plInput').value = prompt;
      $('#plCount').textContent = String(prompt.length);
    }
    if (!PL.cfg || !PL.cfg.configured) return toast('请先完成 AI 接入配置', 'err');

    PL.running = true;
    PL.abort = (typeof AbortController === 'function') ? new AbortController() : null;
    const btn = $('#plRunBtn');
    btn.disabled = false;                 // 保持可点：现在它是「停止」
    btn.classList.add('stopping');
    btn.innerHTML = RUN_BTN_BUSY;
    renderOptimizeStream(prompt);
    const t0 = Date.now();
    let final = null;
    let cancelled = false;
    try {
      await apiStream('/api/prompt/optimize-stream',
        { prompt, task: 'general', mode: PL.mode },
        (ev) => {
          if (ev.type === 'content') plStreamAppend(ev.text || '', ev.chars);
          else if (ev.type === 'reasoning') plStreamNoteThinking(ev.chars, ev.text);
          else if (ev.type === 'stage') plStreamStage(ev.stage);
          else if (ev.type === 'result') final = ev;
        }, PL.abort && PL.abort.signal);
      plStreamFlush();
      // Partial output remains visible, but only an explicit successful result is complete.
      if (!final || !final.ok) {
        throw Object.assign(new Error((final && final.error) || '响应中断，已保留部分内容，请重试'), { body: final || {} });
      }
      PL.last = final;
      PL.lastByMode[PL.mode] = final;
      renderOptimizeResult(final);
      if (!final.cached) {
        savePromptHistory(prompt, final, PL.mode);
        renderPromptHistory();
      }
      toast(final.cached ? '已使用最近一次相同提示词的缓存结果'
        : ((PL.mode === 'structured' ? '专业结构' : '清晰润色') + '完成，用时 '
           + ((final.elapsed_ms || (Date.now() - t0)) / 1000).toFixed(1) + ' 秒'), 'ok');
    } catch (e) {
      // 用户点停止：AbortError 或上游回传 cancelled。已收到的部分结果仍然保留。
      cancelled = e.name === 'AbortError'
        || /cancel/i.test((e.body && (e.body.error || e.body.code)) || '')
        || /已取消|cancelled/i.test(e.message || '');
      const b = e.body || {};
      const partial = promptStreamText().trim();
      $('#plOutput').innerHTML = `
        <div class="pl-check ${cancelled ? 'warn' : 'err'}"><div class="pl-check-head">
          <span class="pl-check-icon">${cancelled ? '■' : '×'}</span>
          <span class="pl-check-title">${cancelled
            ? (isEn() ? 'Optimization stopped' : '已停止优化')
            : esc(b.title || '优化失败')}</span>
          <span class="pl-check-code">${esc(b.code || '')}</span></div>
          <div class="pl-check-detail">${cancelled
            ? (isEn()
                ? 'You stopped this run. Tokens already spent are billed by your provider; the text received so far is kept below.'
                : '已按要求中断本次优化。已产生的用量由服务商照常计费，下方保留了停止前收到的内容。')
            : esc(b.error || e.message)}</div>
          ${!cancelled && b.hint ? `<div class="pl-check-hint"><b>${isEn() ? 'Recommendation: ' : '处理建议：'}</b>${esc(b.hint)}</div>` : ''}
        </div>${partial ? `<div class="pl-out"><div class="pl-final">
          <div class="pl-final-head"><h3>${isEn() ? 'Partial result received' : '已经收到的部分结果'}</h3></div>
          <pre class="pl-pre">${esc(partial)}</pre></div></div>` : ''}`;
    } finally {
      plStreamStop();
      PL.running = false;
      PL.abort = null;
      btn.classList.remove('stopping');
      btn.disabled = false;
      btn.innerHTML = RUN_BTN_IDLE;
    }
  }

  /* ---- 优化过程的流式渲染：先给骨架，正文增量直接追加到同一个 <pre> ---- */
  function renderOptimizeStream(prompt) {
    PL.streamText = '';
    PL.streamTextParts = [];
    PL.streamTextLength = 0;
    PL.streamChars = 0;
    PL.streamThink = 0;
    PL.streamPend = [];
    PL.streamThinkPend = [];
    PL.streamNode = null;
    PL.thinkNode = null;
    PL.streamPinned = true;
    PL.thinkPinned = true;
    PL.streamRaf = 0;
    // 模型真在思考时状态栏会提示，用户知道它在动就够了。
    $('#plOutput').innerHTML = `
      <div class="pl-pane-head"><div><h3>正在优化</h3>
        <p class="pl-stream-stage" id="plStreamStage"><span class="pl-dots"><i></i><i></i><i></i></span>正在连接模型…</p></div>
        <span class="pl-stream-meter" id="plStreamMeter">0 字</span></div>
      <div class="pl-think-bar" id="plThinkBar" hidden>
        <span class="pl-dots"><i></i><i></i><i></i></span>
        <span id="plThinkText">模型正在思考…</span>
      </div>
      <pre class="pl-think-body" id="plThinkBody" hidden></pre>
      <pre class="pl-pre pl-stream-pre is-waiting" id="plStreamPre"><span class="pl-stream-hint">等待模型开始输出…</span></pre>`;
    const streamPre = $('#plStreamPre');
    if (streamPre) streamPre.addEventListener('scroll', () => {
      PL.streamPinned = streamPre.scrollHeight - streamPre.scrollTop - streamPre.clientHeight < 48;
    }, { passive: true });
    const thinkPre = $('#plThinkBody');
    if (thinkPre) thinkPre.addEventListener('scroll', () => {
      PL.thinkPinned = thinkPre.scrollHeight - thinkPre.scrollTop - thinkPre.clientHeight < 48;
    }, { passive: true });
  }
  function plStreamStage(stage) {
    const n = $('#plStreamStage'); if (!n) return;
    const words = {
      connecting: '正在连接模型…',
      thinking: '模型正在思考…',
      streaming: '模型正在输出…',
      polishing: '正在整理与清洗结果…',
    };
    n.innerHTML = '<span class="pl-dots"><i></i><i></i><i></i></span>' + (words[stage] || '处理中…');
    n.dataset.stage = stage || '';
    const pre = $('#plStreamPre');
    if (pre) pre.classList.toggle('is-waiting', stage === 'connecting' || stage === 'thinking');
    const bar = $('#plThinkBar');
    if (bar) bar.hidden = stage !== 'thinking';
    const txt = $('#plThinkText');
    if (txt && stage === 'thinking' && PL.streamThink > 0) {
      txt.textContent = `模型正在思考（${PL.streamThink} 字）…`;
    }
  }
  /* 增量按帧批量写入：逐条 append 会在长文本上反复触发重排，看着就是卡顿。 */
  function plStreamAppend(text, chars) {
    if (!text) return;
    // 分片收集，避免长流每次追加都复制完整字符串（O(n²)）。
    PL.streamTextParts.push(text);
    PL.streamTextLength += text.length;
    PL.streamPend.push(text);
    PL.streamChars = chars || PL.streamTextLength;
    plStreamSchedule();
  }
  function promptStreamText() {
    if (!PL.streamTextParts || !PL.streamTextParts.length) return PL.streamText || '';
    PL.streamText = PL.streamTextParts.join('');
    return PL.streamText;
  }
  function plStreamNoteThinking(chars, text) {
    // 默认只显示"正在思考（N 字）"；设置页开启后才把思考正文渲染出来。
    PL.streamThink = chars || 0;
    const bar = $('#plThinkBar'), txt = $('#plThinkText');
    if (bar && !bar.hidden && txt) {
      txt.textContent = `模型正在思考（${PL.streamThink} 字）…`;
    }
    if (!text) return;
    const box = $('#plThinkBody');
    if (!box) return;
    box.hidden = false;
    PL.streamThinkPend = PL.streamThinkPend || [];
    PL.streamThinkPend.push(text);
    plStreamSchedule();
  }
  function plStreamSchedule() {
    if (PL.streamRaf || PL.streamTimer) return;
    const run = () => {
      if (PL.streamRaf) cancelAnimationFrame(PL.streamRaf);
      if (PL.streamTimer) clearTimeout(PL.streamTimer);
      PL.streamRaf = 0; PL.streamTimer = 0; plStreamFlush();
    };
    PL.streamRaf = requestAnimationFrame(run);
    PL.streamTimer = setTimeout(run, 200);   // 后台标签页 rAF 不跑
  }
  function plStreamFlush() {
    const pre = $('#plStreamPre');
    if (pre && PL.streamPend.length) {
      const hint = $('.pl-stream-hint', pre);
      if (hint) hint.remove();
      pre.classList.remove('is-waiting');
      let node = PL.streamNode;
      if (!node || !node.isConnected) {
        node = document.createElement('span');
        node.setAttribute('data-live', '1');
        const textNode = document.createTextNode('');
        node.appendChild(textNode);
        pre.appendChild(node);
        PL.streamNode = textNode;
      }
      PL.streamNode.appendData(PL.streamPend.join(''));
      PL.streamPend.length = 0;
      if (PL.streamNode.length > RENDER_TAIL_CHARS) {
        PL.streamNode.deleteData(0, PL.streamNode.length - RENDER_TAIL_CHARS);
        pre.title = isEn() ? 'Showing the latest streamed text; the complete result appears when finished.' : '流式预览仅保留最近内容，完成后显示完整结果。';
      }
      if (PL.streamPinned !== false) pre.scrollTop = pre.scrollHeight;
    }
    const tbody = $('#plThinkBody');
    if (tbody && PL.streamThinkPend && PL.streamThinkPend.length) {
      if (!PL.thinkNode || !PL.thinkNode.isConnected) {
        PL.thinkNode = document.createTextNode('');
        tbody.appendChild(PL.thinkNode);
      }
      PL.thinkNode.appendData(PL.streamThinkPend.join(''));
      PL.streamThinkPend.length = 0;
      if (PL.thinkNode.length > RENDER_TAIL_CHARS) PL.thinkNode.deleteData(0, PL.thinkNode.length - RENDER_TAIL_CHARS);
      if (PL.thinkPinned !== false) tbody.scrollTop = tbody.scrollHeight;
    }
    const m = $('#plStreamMeter');
    if (m) m.textContent = (PL.streamChars || 0) + ' 字';
  }
  function plStreamStop() {
    if (PL.streamRaf) cancelAnimationFrame(PL.streamRaf);
    if (PL.streamTimer) clearTimeout(PL.streamTimer);
    PL.streamRaf = 0; PL.streamTimer = 0;
  }

  function normalizePromptWhitespace(text) {
    let value = String(text || '').replace(/\r\n?/g, '\n');
    let fenced = false;
    value = value.split('\n').map((line) => {
      if (/^\s*```/.test(line)) { fenced = !fenced; return line.trimEnd(); }
      return fenced ? line.trimEnd() : line.replace(/[ \t]+/g, ' ').trim();
    }).join('\n');
    value = value.replace(/\n{3,}/g, '\n\n');
    value = value.replace(/[\u3400-\u9fff] +(?=[\u3400-\u9fff])/g, '');
    value = value.replace(/[\u3400-\u9fff] +(?=[，。！？：；、（）【】《》])/g, '');
    value = value.replace(/([，。！？：；、（）【】《》]) +/g, '$1');
    return value.trim();
  }

  function cleanOptimizedPrompt(text) {
    if (!text) return '';
    // 只清思维链标签。原来这里会剥掉所有 XML 样式的标签，但正文里出现
    // <div>/<svg>/<template> 是完全合法的（用户可能就是让模型写 HTML），
    // 一删就把提示词毁了，所以通用标签剥离已移除。
    text = text.replace(/<\s*(?:think|thinking|analysis|reasoning)\b[^>]*>[\s\S]*?<\s*\/\s*(?:think|thinking|analysis|reasoning)\s*>/gi, '');
    text = text.replace(/<\s*\/?\s*(?:think|thinking|analysis|reasoning|thing)\b[^>]*>/gi, '');
    // 去掉包住整段答案的代码围栏
    text = text.replace(/^```[a-zA-Z]*\s*/, '').replace(/\s*```$/, '');
    // 清理多余的空行（保留段落结构，便于阅读）
    text = text.replace(/\n{3,}/g, '\n\n');
    return text.trim();
  }

  function renderOptimizeResult(r) {
    const host = $('#plOutput');
    const en = S.language === 'en-US';
    if (!r) {
      host.innerHTML = `<div class="pl-pane-head"><div><h3>${en ? 'Current result' : '当前优化结果'}</h3><p>${en ? 'The result follows the selected optimization mode.' : '结果将显示在当前选择的优化方式下。'}</p></div></div>` +
        `<div class="pl-empty">${en ? 'Enter an original prompt and click “Optimize”.' : '输入原始提示词后，点击“开始优化”。'}</div>`;
      return;
    }
    const p = r.parsed || {};
    const u = r.usage || {};
    const rawText = p.optimized || r.raw || '';
    const cleanedText = cleanOptimizedPrompt(rawText);
    host.innerHTML = `
      <div class="pl-pane-head"><div><h3>${en ? 'Current result' : '当前优化结果'}</h3>
        <p><span class="pl-result-mode">${r.mode === 'structured' ? (en ? 'Professional structure' : '专业结构') : (en ? 'Clarity editing' : '清晰润色')}</span> · ${esc(r.model || '')} · <span data-countup>${(r.elapsed_ms / 1000).toFixed(2)}s · ${u.output || 0} tokens</span></p></div>
        <div class="pl-result-actions"><button class="btn btn-ghost" id="plBasisBtn"><i class="ico i-info"></i>${en ? 'Why this result' : '优化依据'}</button><button class="btn btn-primary" id="plCopyBtn"><i class="ico i-copy"></i>${en ? 'Copy' : '复制'}</button></div></div>
      <pre class="pl-pre" id="plFinalText">${esc(cleanedText)}</pre>`;
    animateNumericElements(host);
    const cb = $('#plCopyBtn');
    if (cb) cb.onclick = () => copyText($('#plFinalText').textContent, '优化后的提示词已复制');
    const basisBtn = $('#plBasisBtn');
    if (basisBtn) basisBtn.onclick = () => openPromptBasis(r);
  }

  function openPromptBasis(result) {
    const basis = (result && result.basis) || PL_BASIS[(result && result.mode) === 'structured' ? 'structured' : 'polish'];
    const en = S.language === 'en-US';
    const title = en
      ? ((result && result.mode) === 'structured' ? 'Professional structure · optimization basis' : 'Clarity editing · optimization basis')
      : (basis.title || '优化依据');
    const summary = en
      ? ((result && result.mode) === 'structured'
        ? 'The result maps role, task context, constraints, output format and acceptance criteria into an executable prompt.'
        : 'The result preserves intent and facts while reducing ambiguity and improving executable wording.')
      : (basis.summary || '依据当前模式改善提示词的清晰度与可执行性。');
    $('#plBasisTitle').textContent = title;
    $('#plBasisBody').innerHTML = `<p class="pl-basis-summary">${esc(summary)}</p>
      <section class="pl-basis-section"><h4>${en ? 'Applied principles' : '本次应用的原则'}</h4><ul class="pl-basis-list">${(basis.principles || []).map((x) => `<li>${esc(en ? x : x)}</li>`).join('')}</ul></section>
      <section class="pl-basis-section"><h4>${en ? 'Professional structure references' : '专业结构参考（OpenAI / Anthropic）'}</h4><div class="pl-basis-refs">${(basis.references || []).map((ref) => `<a href="${esc(ref.url)}" target="_blank" rel="noopener noreferrer">${esc(ref.name)} ↗</a>`).join('')}</div></section>`;
    $('#plBasisMask').classList.add('on');
    $('#plBasisClose').focus();
  }

  function closePromptBasis() { $('#plBasisMask').classList.remove('on'); }

  /* ---- 本地历史（只存提示词与结果，不含任何密钥） ---- */
  const PL_HIST = 'tl.pl.hist';
  function readPromptHistory() {
    try {
      const all = JSON.parse(localStorage.getItem(PL_HIST) || '[]');
      return Array.isArray(all) ? all : [];
    } catch (_) { return []; }
  }
  function savePromptHistory(input, res, mode) {
    try {
      const all = readPromptHistory();
      all.unshift({ t: Date.now(), input: input.slice(0, 4000),
                    optimized: (res.parsed && res.parsed.optimized || '').slice(0, 20000),
                    model: res.model || '', mode: mode === 'structured' ? 'structured' : 'polish',
                    elapsed_ms: res.elapsed_ms || 0, basis: res.basis || PL_BASIS[mode === 'structured' ? 'structured' : 'polish'] });
      localStorage.setItem(PL_HIST, JSON.stringify(all.slice(0, 20)));
    } catch (e) { /* 忽略配额问题 */ }
  }
  function openPromptHistory() { renderPromptHistory(); $('#plHistoryMask').classList.add('on'); $('#plHistoryClose').focus(); }
  function closePromptHistory() { $('#plHistoryMask').classList.remove('on'); }
  function renderPromptHistoryDetail(item, index, itemEl) {
    if (!item || !itemEl) return;
    const en = S.language === 'en-US';
    const mode = item.mode === 'structured' ? (en ? 'Professional structure' : '专业结构') : (en ? 'Clarity editing' : '清晰润色');
    const stamp = item.t ? new Date(item.t).toLocaleString(S.language === 'en-US' ? 'en-US' : 'zh-CN', { hour12:false }) : '—';
    const input = item.input || (en ? '(No input)' : '（无输入）');
    const optimized = item.optimized || (en ? '(No optimized result)' : '（无优化结果）');
    const localRecord = en ? 'Local record' : '本地记录';
    const existing = $('.pl-history-detail', itemEl);
    if (existing) { existing.remove(); return; }
    const detail = document.createElement('section');
    detail.className = 'pl-history-detail';
    detail.setAttribute('aria-live', 'polite');
    detail.innerHTML = `<div class="pl-history-detail-head"><div><div class="pl-history-detail-title"><span class="pl-history-mode ${item.mode === 'structured' ? 'structured' : 'polish'}">${mode}</span><strong>${en ? 'History' : '历史记录'} #${index + 1}</strong></div><p class="pl-history-detail-meta">${esc(stamp)} · ${esc(item.model || localRecord)}</p></div><div class="pl-history-detail-actions"><button class="btn btn-ghost pl-history-detail-copy" type="button" data-history-copy="${index}"><i class="ico i-copy"></i>${en ? 'Copy result' : '复制优化结果'}</button><button class="btn btn-ghost pl-history-detail-collapse" type="button">↑ ${en ? 'Collapse' : '收起'}</button></div></div><div class="pl-history-detail-block"><span class="pl-history-detail-label">${en ? 'Original prompt' : '原始提示词'}</span><pre>${esc(input)}</pre></div><div class="pl-history-detail-block optimized"><span class="pl-history-detail-label">${en ? 'Optimized result' : '优化结果'}</span><pre>${esc(optimized)}</pre></div>`;
    itemEl.appendChild(detail);
    const copy = $('.pl-history-detail-copy', detail);
    if (copy) copy.onclick = () => copyText(optimized, en ? 'History result copied' : '历史优化结果已复制');
    const collapse = $('.pl-history-detail-collapse', detail);
    if (collapse) collapse.onclick = () => { detail.remove(); itemEl.classList.remove('is-selected'); };
  }

  function renderPromptHistory() {
    const host = $('#plHistoryList'); if (!host) return;
    const all = readPromptHistory();
    if (!all.length) { host.innerHTML = '<div class="pl-empty">暂时没有历史优化记录。</div>'; return; }
    const en = S.language === 'en-US';
    host.innerHTML = all.map((item, index) => {
      const mode = item.mode === 'structured' ? (en ? 'Professional structure' : '专业结构') : (en ? 'Clarity editing' : '清晰润色');
      const stamp = item.t ? new Date(item.t).toLocaleString(S.language === 'en-US' ? 'en-US' : 'zh-CN', {hour12:false}) : '—';
      return `<article class="pl-history-item" data-history-index="${index}"><div class="pl-history-item-head"><span class="pl-history-mode ${item.mode === 'structured' ? 'structured' : 'polish'}">${mode}</span><time>${esc(stamp)}</time><button class="btn btn-ghost pl-history-view" type="button" aria-expanded="false">${en ? 'View' : '查看'}</button></div><p class="pl-history-input">${esc(item.input || (en ? '(No input)' : '（无输入）'))}</p><p class="pl-history-preview">${esc((item.optimized || '').slice(0, 180))}</p></article>`;
    }).join('');
      $$('.pl-history-view', host).forEach((btn) => btn.onclick = (e) => {
      const itemEl = e.currentTarget.closest('[data-history-index]');
      const index = itemEl ? +itemEl.dataset.historyIndex : -1;
      const item = all[index]; if (!item) return;
      const isOpen = !!$('.pl-history-detail', itemEl);
      $$('.pl-history-item', host).forEach((node) => {
        if (node !== itemEl) { node.classList.remove('is-selected'); const open = $('.pl-history-detail', node); if (open) open.remove(); }
      });
      itemEl.classList.toggle('is-selected', !isOpen);
      e.currentTarget.setAttribute('aria-expanded', String(!isOpen));
      renderPromptHistoryDetail(item, index, itemEl);
    });
  }

  async function savePromptProfile(newProfile = false) {
    const f = plForm();
    const targetId = newProfile ? '' : (f.profile_id || '');
    try {
      const res = await api('/api/prompt/save', { method: 'POST', body: { ...f, profile_id: targetId, new_profile: newProfile } });
      if (!res.ok) return toast(res.error || '保存失败', 'err', 5200);
      PL.cfg = res.config;
      PL.draftNew = false;
      invalidateViews();
      $('#plKey').value = '';
      renderPrompt();
      renderPromptProfiles(res.profile_id || PL.cfg.profile_id);
      const pname = (PL.cfg.profile_name || '配置');
      toast((newProfile ? `已另存为新配置「${pname}」，原有配置全部保留`
                        : (targetId ? `已更新配置「${pname}」` : `已新建配置「${pname}」`))
            + `（共 ${(PL.cfg.profiles || []).length} 个配置）`
            + (res.config.encrypted ? '，API Key 已加密存储' : ''), 'ok', 3600);
      closePromptConfig();
    } catch (e) {
      toast(e.message || '保存失败', 'err', 5200);
    }
  }

  async function copyText(text, okMsg) {
    try {
      await navigator.clipboard.writeText(text);
      toast(okMsg || '已复制', 'ok');
      return true;
    } catch (e) {
      const ta = el('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      ta.remove();
      toast(ok ? (okMsg || '已复制') : '复制失败，请手动选中复制', ok ? 'ok' : 'err');
      return ok;
    }
  }


  function doSearch(v) {
    saveCurrentNavigation();
    S.query = (v || '').trim();
    $('#searchBox').classList.toggle('has-value', !!S.query);
    S.recCache = {};
    S.recOffset = 0;delete S.rendered.records;
    if (S.query && S.view !== 'records') switchView('records');
    else if (S.view === 'records') renderRecords();
    renderNavigationContext();
  }

  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme-pref')
      || (document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');
    const i = THEME_CYCLE.indexOf(cur);
    applyTheme(THEME_CYCLE[(i < 0 ? 0 : i + 1) % THEME_CYCLE.length]);
  }

  function exportQuery(fmt) {
    const q = new URLSearchParams({fmt, range: S.range});
    if (S.view === 'records') {
      for (const [name, value] of Object.entries({tool:S.recTool, project:S.recProject,
        model:S.recModel, project_paths:S.recProjectPaths, from:S.recFrom, to:S.recTo, order:S.recOrder,cost_basis:currentContext().cost_basis})) {
        if (value) q.set(name, value);
      }
    }
    if (S.query) q.set('q', S.query);
    return q;
  }

  async function copySummary() {
    if (S.view === 'records' || S.query) {
      const q = exportQuery('md');
      q.set('lang', S.language || 'zh-CN');
      const response = await fetch('/api/export?' + q.toString(), {headers: {'X-TokenLens': '1'}});
      if (!response.ok) throw new Error(isEn() ? 'Export failed' : '导出失败');
      return copyText(await response.text(), isEn() ? 'Filtered summary copied' : '筛选汇总已复制');
    }
    const D = S.data; if (!D) return;
    const s = D.summary;
    const en = isEn();
    const loc = en ? 'en-US' : 'zh-CN';
    const n = (v) => (+v || 0).toLocaleString(loc);
    // 导出的文本要跟着界面语言走：英文用户拿到一份中英混排的汇总没有意义。
    const lines = en ? [
      `TokenLens · ${rangeLabel(D)}`,
      `Total tokens: ${n(s.total)}`,
      `Input ${n(s.input)} / Output ${n(s.output)} / Cache read ${n(s.cache_read)}`,
      `${n(s.requests)} calls · ${s.sessions} sessions · ${s.cache_hit_rate}% cache hit`,
      `Recorded cost ${money(s.cost, D.config.usd_to_cny)} · saved charges and estimates`,
      s.unknown_records==null?'Cost completeness is shown in Cost management':window.TLMoney?.coverage(s),
      '',
      'Breakdown by source:',
      ...D.tools.map((t) => `  ${t.name}: ${n(t.total)} tokens (${t.share}%)`),
    ] : [
      `tokenlens · ${D.range_label}`,
      `总 Token：${n(s.total)}`,
      `输入 ${n(s.input)} / 输出 ${n(s.output)} / 缓存读 ${n(s.cache_read)}`,
      `调用 ${n(s.requests)} 次 · ${s.sessions} 个会话 · 缓存命中 ${s.cache_hit_rate}%`,
      `记录费用 ${money(s.cost, D.config.usd_to_cny)} · 实付 + 已记录估算`,
      s.unknown_records==null?'费用完整性见成本管理':window.TLMoney?.coverage(s),
      '',
      '来源明细：',
      ...D.tools.map((t) => `  ${t.name}：${n(t.total)} tokens（${t.share}%）`),
    ];
    copyText(lines.join('\n'), en ? 'Summary copied to clipboard' : '汇总已复制到剪贴板');
  }

  /* 卡片悬停高光已改为纯 CSS（见 app.css 的 .card:hover::before）。
     原先的「指针跟随聚光」要在 document 上监听 pointermove、每次 closest() 再写 CSS 变量，
     滚动/移动鼠标时会不断触发样式失效，收益远小于开销，故移除。 */

  /* 按钮涟漪 */
  function initRipple() {
    document.addEventListener('pointerdown', (e) => {
      if(document.documentElement.classList.contains('perf-mode') || !motionEnabled())return;
      const b = e.target && e.target.closest ? e.target.closest('.btn,.segmented button,.menu button') : null;
      if (!b) return;
      const box = b.getBoundingClientRect();
      b.style.setProperty('--rx', (e.clientX - box.left) + 'px');
      b.style.setProperty('--ry', (e.clientY - box.top) + 'px');
      b.classList.remove('rippling');
      void b.offsetWidth;
      b.classList.add('rippling');
      setTimeout(() => b.classList.remove('rippling'), 580);
    }, { passive: true });
  }

  function initContextMenuGuard() {
    // Keep the browser context menu available for copying paths and editing text.
  }

  /* 分段控件滑动指示器 */
  function initSegPill(sel) {
    const bar = $(sel); if (!bar) return;
    if(sel==='#rangeSeg')return;
    if (!$('.seg-pill', bar)) bar.appendChild(el('span', 'seg-pill'));
    const move = (animate = true) => {
      const act = $('button.active', bar);
      const pill = $('.seg-pill', bar);
      if (!act || !pill) return;
      if (!animate) bar.classList.add('no-anim');
      pill.style.width = act.offsetWidth + 'px';
      pill.style.transform = `translateX(${act.offsetLeft - 3}px)`;
      if (!animate) requestAnimationFrame(() => requestAnimationFrame(() => bar.classList.remove('no-anim')));
    };
    move(false);
    $$('button', bar).forEach((b) => b.addEventListener('click', () => setTimeout(() => move(true), 0)));
    bar._sync = move;
    new ResizeObserver(() => move(false)).observe(bar);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => move(false));
  }

  /* 骨架屏 */
  function showSkeleton() {
    $('#kpis').innerHTML = `<div class="skeleton sk-kpis"></div>`;
  }

  /* 键盘快捷键 */
  function initKeys() {
    const isTyping = (t) => t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    document.addEventListener('keydown', (e) => {
      if(document.querySelector('.iq-connection-dialog[open]'))return;
      if (e.key === 'Escape') {
        if (S.drawerOpen) return closeDrawer();
        if ($('#aboutMask').classList.contains('on')) return closeAbout();
        if ($('#methodMask').classList.contains('on')) return closeMethodology();
        if ($('#plBasisMask').classList.contains('on')) return closePromptBasis();
        if ($('#plHistoryMask').classList.contains('on')) return closePromptHistory();
        if ($('#plConfigMask').classList.contains('on')) return closePromptConfig();
        $('#helpMask').classList.remove('on');
        $('#exportMenu').classList.remove('on');
        $('#exportBtn').classList.remove('open');
        if (document.activeElement && isTyping(document.activeElement)) document.activeElement.blur();
        return;
      }
      if (isTyping(e.target)) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === '/' ) { e.preventDefault(); $('#searchInput').focus(); return; }
      if (e.key === '?') { $('#helpMask').classList.add('on'); return; }
      if (e.key >= '1' && e.key <= '9') {
        const v = VIEW_KEYS[parseInt(e.key, 10) - 1];
        if (v) switchView(v);
        return;
      }
      if (e.key === '0') {
        const v = VIEW_KEYS[9];
        if (v) switchView(v);
        return;
      }
      const k = e.key.toLowerCase();
      if (k === 'r') { e.shiftKey ? startScan(true) : startScan(false); }
      else if (k === 't') toggleTheme();
      else if (k === 'e') $('#exportBtn').click();
      else if (k === 's') startScan(false);
      else if (k === 'g') switchView('overview');
    });
  }

  /* 自动增量扫描 */
  let autoWatchGeneration = 0, observedScanFinish = 0;
  function scheduleAutoScan(minutes) {
    // The service owns scan scheduling. A visible page only watches the small
    // status response so automatic scans can refresh this dashboard too.
    const generation = ++autoWatchGeneration;
    clearTimeout(S.autoTimer); S.autoTimer = null;
    if (!(Number(minutes) > 0) || uiHidden()) return;
    const watch = async () => {
      if (generation !== autoWatchGeneration || uiHidden()) return;
      try {
        if (!S.scanAbort) {
          const status = await api('/api/scan/status');
          if (generation !== autoWatchGeneration || uiHidden()) return;
          if (status.running) pollScan();
          else if (status.finished > observedScanFinish) {
            applyScanState({...status, fresh:false}, true);
            clearDashboardCache(); await load(true, true);
          }
        }
      } catch (_) { /* The next visible check retries a temporarily unavailable service. */ }
      if (generation === autoWatchGeneration && !uiHidden()) S.autoTimer = setTimeout(watch, 15000);
    };
    S.autoTimer = setTimeout(watch, 15000);
  }
  document.addEventListener('visibilitychange', () => {
    scheduleAutoScan(((S.data || {}).config || (S.boot || {}).config || {}).auto_scan_minutes);
  }, {passive:true});


  const TITLES = {
    overview: ['总览', '全部编程工具与 AI 助手的 token 消耗'],
    insights: ['洞察', '自动提炼的关键结论与周期对比'],
    tools: ['按工具', '每个工具各自的用量与趋势'],
    models: ['按模型', '模型维度的用量与成本'],
    projects: ['按项目', '工程目录维度的用量分布'],
    sessions: ['按会话', '单次会话的消耗排行（点击行可下钻）'],
    sources: ['数据源', '本机各工具的数据落盘位置与采集状态'],
    manage: ['成本管理', '项目预算、数据质量、价格版本与账单核对'],
    assistant: ['AI 助手', '基于实际使用数据的费用与用量分析'],
    records: ['明细流水', '逐条调用记录，可搜索与导出'],
    prompt: ['提示词优化', '清晰润色与专业结构，两种方式按需选择'],
    'ai-test': ['AI 智商检测', '固定推理题与 SVG 创作题的模型能力检测'],
    settings: ['设置', '界面偏好、扫描自动化、预算与自定义数据源'],
  };
  const VIEW_KEYS = Object.keys(TITLES);

  const NAV_FEATURES = {insights:'nav_insights',tools:'nav_tools',models:'nav_models',
    projects:'nav_projects',sessions:'nav_sessions',sources:'nav_sources',
    manage:'nav_manage',assistant:'nav_assistant',prompt:'pl_enabled','ai-test':'iq_enabled'};
  function featureEnabled(key) {
    const c = (S.data && S.data.config) || (S.boot && S.boot.config) || {};
    return cfgValue(c, key, true) !== false;
  }

  function applyFeatureToggles() {
    for (const [view, key] of Object.entries(NAV_FEATURES)) {
      const nav = $(`.nav a[data-view="${view}"]`);
      if (nav) nav.hidden = !featureEnabled(key);
    }
    $$('[data-nav-feature]').forEach(input => input.checked = featureEnabled(input.dataset.navFeature));
    if (NAV_FEATURES[S.view] && !featureEnabled(NAV_FEATURES[S.view])) switchView('overview', {replace:true});
  }

  // History entries contain only small UI state, never tables or database rows.
  // Cached DOM is reused on return; only changed filters schedule a new query.
  let historySequence = 0, restoringHistory = false, pendingScroll = null;
  const navigationEntries = new Map(), viewPositions = new Map();
  function captureNavigation() {
    return {view:S.view,range:S.range,metric:S.metric,scroll:$('#scroll').scrollTop,
      recTool:S.recTool,recModel:S.recModel,recProject:S.recProject,recProjectPaths:S.recProjectPaths,
      query:S.query,recFrom:S.recFrom,recTo:S.recTo,recOrder:S.recOrder,recOffset:S.recOffset,
      recPageSize:S.recPageSize,ledgerCur:S.ledgerCur,analysisContext:S.analysisContext,
      returnId:S.returnId,pages:Array.from(tablePages, ([key,value])=>[key,{page:value.page,size:value.size,scope:value.scope}]),
      assistant:window.TLAssistant?.getContext?.(),manage:window.TLManage?.getNavigationState?.()};
  }
  function storeHistoryEntry(value, id) {
    navigationEntries.set(id,value);
    if (navigationEntries.size > 60) navigationEntries.delete(navigationEntries.keys().next().value);
  }
  function saveCurrentNavigation() {
    if (restoringHistory || !uiReady) return;
    const value=captureNavigation(), id=history.state?.tlId || ++historySequence;
    viewPositions.set(S.view,value.scroll); storeHistoryEntry(value,id);
    history.replaceState({tlId:id,tl:value},'', '#' + S.view);
    return id;
  }
  function writeNavigation(replace = false) {
    if (restoringHistory) return;
    const value=captureNavigation(), id=replace && history.state?.tlId ? history.state.tlId : ++historySequence;
    storeHistoryEntry(value,id);
    history[replace ? 'replaceState':'pushState']({tlId:id,tl:value},'', '#' + S.view);
  }
  function currentContext() {
    const scoped = S.view==='records' ? {tool:S.recTool,model:S.recModel,project:S.recProject,
      project_paths:S.recProjectPaths,query:S.query,from:S.recFrom,to:S.recTo} : S.view==='assistant'
      ? (window.TLAssistant?.getContext?.() || S.analysisContext || {}) : S.view==='manage'
      ? {cost_basis:window.TLManage?.getNavigationState?.().basis || 'total_cost'} : {};
    return {...scoped,query:scoped.query || scoped.q || '',range:S.range,cost_basis:scoped.cost_basis || (S.view==='records'?S.analysisContext?.cost_basis:null) || 'total_cost'};
  }
  function renderNavigationContext() {
    let host=$('#navigationContext');
    if (!host) {host=el('div','navigation-context');host.id='navigationContext';host.dataset.noTranslate='';$('#scroll').before(host);}
    const analytics=['overview','insights','tools','models','projects','sessions','records','manage','assistant'];
    const show=analytics.includes(S.view) || !!S.returnId;host.hidden=!show;if(!show)return;
    const context=currentContext(), D=S.data;
    const labels={today:isEn()?'Today':'今天','7d':isEn()?'Last 7 days':'最近 7 天','30d':isEn()?'Last 30 days':'最近 30 天',
      '90d':isEn()?'Last 90 days':'最近 90 天','365d':isEn()?'Last year':'最近一年',all:isEn()?'All time':'全部时间'};
    const chips=[D?.range===S.range ? rangeLabel(D) : labels[S.range] || S.range];
    for (const [key,label] of [['tool',isEn()?'Tool':'工具'],['model',isEn()?'Model':'模型'],['project',isEn()?'Project':'项目'],['query',isEn()?'Search':'搜索']]) {
      if(context[key])chips.push(`${label}：${context[key]}`);
    }
    if(context.project_paths) {try {chips.push((isEn()?'Projects: ':'项目：')+JSON.parse(context.project_paths).join(' · '));}catch(_) {}}
    if(context.from || context.to)chips.push(`${context.from || '…'} ~ ${context.to || '…'}`);
    const currency=window.TLMoney?.currency?.() || (cfgValue(D?.config || {},'display_currency',isEn()?'usd':'cny')==='usd'?'USD':'CNY');
    const basis=({total_cost:isEn()?'Saved cost':'采集时保存金额',historical_cost:isEn()?'Prices at call date':'调用日价格估算',repriced_cost:isEn()?'Current prices':'现价估算'})[context.cost_basis];
    if(analytics.includes(S.view))chips.push(`${currency} · ${basis || ''}`);
    const parent=S.returnId && navigationEntries.get(S.returnId);
    host.innerHTML=(parent?`<button type="button" class="btn btn-ghost btn-xs context-return" id="contextReturn">← ${isEn()?'Return to ':'返回'}${esc((LANG[S.language]?.title[parent.view] || TITLES[parent.view] || [''])[0])}</button>`:'')+
      `<div class="context-chips">${chips.filter(Boolean).map(chip=>`<span class="context-chip" title="${esc(chip)}">${esc(chip)}</span>`).join('')}</div>`+
      (S.view==='records' && featureEnabled('nav_assistant')?`<button type="button" class="btn btn-ghost btn-xs" id="contextAnalyze">${isEn()?'Analyze these records':'分析这些流水'}</button>`:'');
    if($('#contextReturn'))$('#contextReturn').onclick=()=>restoreNavigation(parent,true);
    if($('#contextAnalyze'))$('#contextAnalyze').onclick=()=>navigateWithContext('assistant',currentContext());
  }
  function restoreScroll() {
    if (!pendingScroll || pendingScroll.view!==S.view) return;
    const value=pendingScroll;pendingScroll=null;
    requestAnimationFrame(()=>{if(S.view===value.view)$('#scroll').scrollTop=value.scroll;});
  }
  function restoreNavigation(value, replace = false) {
    if(!value)return;restoringHistory=true;
    const rangeChanged=S.range!==value.range;
    const recordsChanged=['recTool','recModel','recProject','recProjectPaths','query','recFrom','recTo','recOrder','recOffset','recPageSize'].some(key=>S[key]!==value[key]);
    const pageChanged=(value.pages || []).some(([key,next])=>tablePages.get(key)?.page!==next.page || tablePages.get(key)?.size!==next.size);
    for(const key of ['range','metric','recTool','recModel','recProject','recProjectPaths','query','recFrom','recTo','recOrder','recOffset','recPageSize','ledgerCur','analysisContext','returnId']) {
      if(key in value)S[key]=value[key];
    }
    for(const [key,next] of value.pages || []) {const state=tablePages.get(key) || {request:0};
      if(state.page!==next.page || state.size!==next.size || state.scope!==next.scope)state.cacheKey=null;
      Object.assign(state,next);tablePages.set(key,state);}
    if(recordsChanged)delete S.rendered.records;
    if(pageChanged)for(const key of ['models','projects','sessions'])delete S.rendered[key];
    localStorage.setItem('tl.range',S.range);syncRangeButtons();syncRecordControls();
    window.TLAssistant?.setContext?.(value.assistant || value.analysisContext || {range:S.range});
    window.TLManage?.restoreNavigationState?.(value.manage);
    pendingScroll={view:value.view,scroll:value.scroll || 0};
    switchView(value.view,{history:false,scroll:value.scroll || 0});restoringHistory=false;
    if(replace)writeNavigation(true);
    if(rangeChanged)load();else if(S.rendered[S.view]===S.epoch)restoreScroll();
  }
  function syncRecordControls() {
    $('#searchInput').value=S.query;$('#searchBox').classList.toggle('has-value',!!S.query);
    $('#recTool').value=S.recTool;$('#recOrder').value=S.recOrder;applyRecDays(S.recFrom,S.recTo,false);
  }
  function navigateWithContext(view, filter = {}) {
    if(NAV_FEATURES[view] && !featureEnabled(NAV_FEATURES[view])) {
      toast(isEn()?'Enable this page in Settings → Feature switches.':'请在设置 → 功能开关中启用此页面。','info');return;
    }
    const origin=saveCurrentNavigation(), next={...currentContext(),...filter,range:filter.range || S.range};
    const rangeChanged=next.range!==S.range;
    S.range=next.range;localStorage.setItem('tl.range',S.range);S.returnId=origin || null;
    S.analysisContext=next;syncRangeButtons();
    if(view==='records') {
      S.recProject=next.project || '';S.recModel=next.model || '';S.recTool=next.tool || '';
      S.recProjectPaths=Array.isArray(next.project_paths)?JSON.stringify(next.project_paths):next.project_paths || '';
      if(S.recProjectPaths)S.recProject='';S.query=next.query || next.q || '';S.recFrom=next.date || next.from || '';S.recTo=next.date || next.to || '';
      S.recOffset=0;delete S.rendered.records;syncRecordControls();
    }
    if(view==='assistant'){window.TLAssistant?.setContext?.({...next,q:next.q || next.query || ''});window.TLAssistant?.showTab?.('chat');}
    if(view==='manage')window.TLManage?.setContext?.(next);
    switchView(view,{originSaved:true});if(rangeChanged)load();
  }
  window.TLNavigation={context:currentContext,navigate:navigateWithContext,refreshContext:renderNavigationContext,checkpoint:saveCurrentNavigation};

  function switchView(v, options = {}) {
    if (!TITLES[v]) v = 'overview';
    if(NAV_FEATURES[v] && !featureEnabled(NAV_FEATURES[v]))v='overview';
    const changed=v!==S.view;
    const previousContext=currentContext();
    if(uiReady && options.history!==false && !options.originSaved)saveCurrentNavigation();
    if(changed && options.history!==false && !options.originSaved)S.returnId=null;
    const scroll=options.scroll ?? (changed ? viewPositions.get(v) || 0 : $('#scroll').scrollTop);
    $('#scroll').scrollTop = 0;
    S.view = v; localStorage.setItem('tl.view', v);
    if (v !== 'records' && S.recordsAbort) { S.recordsAbort.abort(); S.recordsAbort = null; S.recRequest++; }
    document.documentElement.classList.remove('nav-open');
    if ($('#navToggle')) $('#navToggle').setAttribute('aria-expanded', 'false');
    if(options.history!==false)writeNavigation(options.replace || !changed || !history.state?.tlId);
    $$('.nav a').forEach((a) => { a.classList.toggle('active', a.dataset.view === v); a.setAttribute('aria-current', a.dataset.view === v ? 'page' : 'false'); });
    const cur = $('.view.active'), next = $(`.view[data-view="${v}"]`);
    $$('.view').forEach((s) => s.classList.remove('active'));
    if (next) next.classList.add('active');
    pruneNumberMotions();
    if (next && S.rendered[v] === S.epoch) requestAnimationFrame(()=>{
      if(S.view===v)animateNumericElements(next,true);
    });
    // Cached views are shown immediately; only the requested cold view renders.
    const titlePack = (LANG[S.language] && LANG[S.language].title[v]) || (LANG['zh-CN'].title[v]) || TITLES[v] || ['', ''];
    const [t, sub] = titlePack;
    const kicker = $('#viewKicker');
    if (kicker) kicker.textContent = v === 'ai-test' ? 'MODEL BENCH' : v === 'assistant' ? 'AI WORKSPACE' : '';
    $('#viewTitle').textContent = t;
    $('#viewSub').textContent = sub;
    document.documentElement.classList.toggle('ai-test-view', v === 'ai-test');
    // 明细页同样需要右上角的时间导航（今天 / 7 天 / 30 天…）：
    // 它和总览共用同一套 rangeSeg，切换时走同一条缓存与动画路径。
    // 只有纯工具页（提示词 / 检测 / 设置 / 数据源）才隐藏它。
    const noRange = new Set(['prompt', 'ai-test', 'settings', 'sources']);
    const rangeSeg = $('#rangeSeg');
    if (rangeSeg) rangeSeg.classList.toggle('context-hidden', noRange.has(v));
    // 切到明细时把 rangeSeg 的选中态同步为当前范围（可能带着自定义日期）
    if (v === 'records' && rangeSeg) syncRangeButtons();
    // 明细也走同一套缓存（renderRecords 成功后会登记）
    if (v === 'prompt') loadPromptCfg().then(renderPrompt);
    if (v === 'settings' && window.TLAISettings) window.TLAISettings.render(S.language);
    if (v === 'assistant' && window.TLAssistant) {
      const scoped=options.originSaved?currentContext():previousContext;
      window.TLAssistant.setContext?.({...scoped,q:scoped.q || scoped.query || '',range:S.range});
      if (!S.data || S.rendered[v] === S.epoch) window.TLAssistant.render(S.language);
    }
    if (v === 'ai-test' && window.TLCCSwitch) window.TLCCSwitch.refreshOnce();
    if (!S.data) {
      if (v === 'records') renderRecords();
      else if (v === 'settings') renderSettings();
      else if (v === 'sources') window.TLLSources?.render(next);
    }
    if (S.data && S.rendered[v] !== S.epoch && v !== 'prompt') scheduleViewRender(v);
    pendingScroll={view:v,scroll};renderNavigationContext();
    if(S.rendered[v]===S.epoch || v==='prompt' || v==='assistant')restoreScroll();
  }

  let viewRenderFrame = 0;
  function scheduleViewRender(view) {
    if (viewRenderFrame) cancelAnimationFrame(viewRenderFrame);
    viewRenderFrame = requestAnimationFrame(() => {
      viewRenderFrame = 0;
      if (view !== S.view || !S.data) return;
      renderView(view);
      if (view !== 'records') S.rendered[view] = S.epoch;
      S.rangeMotion = false;
      if (!['records','models','projects','sessions','manage'].includes(view)) restoreScroll();
    });
  }

  /**
   * 分帧挂载：把一批节点分多次插入，每帧只占用 budget 毫秒。
   * 用于卡片数量多的视图（数据源图鉴 30 张），避免冷渲染时单帧塞太多导致掉帧。
   */
  function chunkMount(host, nodes, budget, onDone) {
    host.innerHTML = '';
    const ticket = (host._mountTicket || 0) + 1;
    host._mountTicket = ticket;
    let i = 0;
    const total = nodes.length;
    if (!total) return;
    const step = () => {
      if (!host.isConnected || host._mountTicket !== ticket) return;
      const t0 = performance.now();
      const frag = document.createDocumentFragment();
      // 至少插一个，避免极端情况下卡住
      frag.appendChild(nodes[i++]);
      while (i < total && performance.now() - t0 < (budget || 6)) {
        frag.appendChild(nodes[i++]);
      }
      host.appendChild(frag);
      if (i < total) requestAnimationFrame(step);
      else if (onDone) onDone();
    };
    step();
  }

  /** 数据或配置变了：所有已渲染的视图都要作废。 */
  function invalidateViews() {
    S.epoch++;
    S.rendered = {};
    S.recCache = {};
    S.recRequest++;
    if (S.recordsAbort) { S.recordsAbort.abort(); S.recordsAbort = null; }
  }

  function renderView(v, chartsOnly) {
    if (!S.data) return;
    const started = performance.now();
    if (v === 'overview') { chartsOnly ? renderCharts() : renderOverview(); renderWeeklyDigest(); }
    else if (v === 'insights') renderInsights();
    else if (v === 'tools') renderTools();
    else if (v === 'models') {
      renderModelTable();
      requestAnimationFrame(() => {if(S.view==='models')renderLedger();});
    }
    else if (v === 'projects') renderProjectTable();
    else if (v === 'sessions') renderSessionTable();
    else if (v === 'sources') renderSources();
    else if (v === 'settings') renderSettings();
    else if (v === 'records') renderRecords();
    else if (v === 'manage' && window.TLManage) Promise.resolve(window.TLManage.render(S.range, isEn())).then(()=>{if(S.view===v)restoreScroll();});
    else if (v === 'assistant' && window.TLAssistant) window.TLAssistant.render(S.language);
    const activeView = document.querySelector('.view.active');
    // Async tables initialize labels and counters after their rows arrive.
    // Rewalking cached tables here forces layout before replacement.
    if (activeView && !['models','projects','sessions','records','manage','assistant'].includes(v)) {
      if (window.TLResponsive) window.TLResponsive.apply(activeView);
      animateNumericElements(activeView);
    }
    // Dynamic translations are applied only to changed subtrees by i18n's
    // observer. Do not walk every hidden page after rendering one view.
    if (window.__TL_PERF__) window.__TL_PERF__.recordRender(v, performance.now() - started);
  }

  async function api(path, opt = {}) {
    const o = { method: opt.method || 'GET', headers: { 'X-TokenLens': '1' } };
    if (opt.body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opt.body); }
    if (opt.signal) o.signal = opt.signal;
    const r = await fetch(path, o);
    if (!r.ok) {
      let body = null;
      try { body = await r.json(); } catch (_) { /* 非 JSON 响应 */ }
      const msg = (body && (body.error || body.title || body.message)) || ('HTTP ' + r.status);
      const err = new Error(msg);
      err.status = r.status;
      err.body = body;
      throw err;
    }
    return r.json();
  }

  /* 流式接口：服务端把模型的思维链/正文增量以 SSE 推回来，边收边渲染。
     流中途的失败不抛异常——先让已经收到的内容留在界面上，再由调用方
     决定怎么提示，避免「等到最后才报错、前面全白看」。 */
  async function apiStream(path, body, onEvent, signal) {
    let taskId = null;
    const cancelTask = () => { if (taskId) api('/api/tasks/cancel', {method:'POST', body:{task_id:taskId}}).catch(() => {}); };
    if (signal) signal.addEventListener('abort', cancelTask, {once:true});
    let reader, complete = false;
    try {
      const r = await fetch(path, {
        method: 'POST',
        headers: { 'X-TokenLens': '1', 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal,
      });
      if (!r.ok || !r.body) {
        let b = null;
        try { b = await r.json(); } catch (_) { /* 非 JSON */ }
        const err = new Error((b && (b.error || b.title || b.message)) || ('HTTP ' + r.status));
        err.body = b;
        throw err;
      }
      reader = r.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) { complete = true; break; }
        buf += decoder.decode(value, { stream: true });
        if (buf.length > 2 * 1024 * 1024) throw new Error(isEn() ? 'AI response frame is too large.' : 'AI 响应过大，请缩小分析范围。');
        let cut;
        while ((cut = buf.search(/\r?\n\r?\n/)) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + (buf[cut] === '\r' ? 4 : 2));
          for (const line of frame.split(/\r?\n/)) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            let obj;
            try { obj = JSON.parse(payload); } catch (_) { continue; }
            if (obj.type === 'task') { taskId = obj.task_id; if (signal?.aborted) cancelTask(); }
            if (onEvent) onEvent(obj);
          }
        }
      }
    } finally {
      signal?.removeEventListener('abort', cancelTask);
      if (reader) {
        if (!complete) { cancelTask(); await reader.cancel().catch(() => {}); }
        reader.releaseLock();
      }
    }
  }

  function applyDashboard(data) {
    S.data = data;
    invalidateViews();
    applyFeatureToggles();
    // 这里的 render 会紧随数据替换执行；主题变化不再额外排一帧重绘整页。
    applyTheme(S.forceTheme || S.themePreference || S.data.config.theme || 'dark', false, false);
    configureMoney();
    if(S.view==='assistant')window.TLAssistant?.setContext?.({...currentContext(),range:S.range});
    renderNavigationContext();
    scheduleViewRender(S.view);
    populateToolFilter();
    checkBudget(S.data.budget);
    if (!S.scanRunning) {
      const span = data.span || {};
      $('#scanText').textContent = `${span.n ? fmtExact(span.n) + (isEn() ? ' records' : ' 条记录') : (isEn() ? 'Ready' : '就绪')}${data.last_scan ? ' · ' + dd(data.last_scan) : ''}`;
    }
    // Renderers register every metric after each view update. Viewport observers
    // and the shared queue bound simultaneous work across dense tables.
  }

  /* 切换时间范围时的整体过渡：
     给滚动区加一个短暂的 data-motion 标记，让 KPI / 图表 / 排行一起淡入，
     数字计数由共享队列按可见区域独立触发。
     标记必须在下一帧就撤掉——留着会让后续所有重绘都重播动画。 */
  function playRangeTransition() {
    const scroll = $('#scroll');
    if (!scroll || !motionEnabled()) return;
    scroll.classList.remove('range-switch');
    // 强制重排一次，保证重复切换时动画能重新触发
    void scroll.offsetWidth;
    scroll.classList.add('range-switch');
    clearTimeout(playRangeTransition._t);
    playRangeTransition._t = setTimeout(() => scroll.classList.remove('range-switch'), 620);
  }

  async function load(silent, force = false) {
    const range = S.range;
    const cached = force ? null : S.dashboardCache.get(range);
    if (cached) {
      applyDashboard(cached);
      return;
    }
    if (S.dashboardAbort) S.dashboardAbort.abort();
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    S.dashboardAbort = controller;
    const request = ++S.dashboardRequest;
    setDashboardBusy(true);
    try {
      const data = await api('/api/dashboard?range=' + encodeURIComponent(range),
        controller ? { signal: controller.signal } : {});
      // 只允许最后一次点击的请求更新界面，快速连点范围时旧响应直接丢弃。
      if (request !== S.dashboardRequest || range !== S.range) return;
      cacheDashboard(range, data);
      applyDashboard(data);
    } catch (e) {
      if (e.name !== 'AbortError' && !silent && request === S.dashboardRequest) {
        toast('数据加载失败：' + e.message, 'err');
      }
    } finally {
      if (request === S.dashboardRequest) {
        S.dashboardAbort = null;
        setDashboardBusy(false);
      }
    }
  }

  function populateToolFilter() {
    const sel = $('#recTool');
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部工具</option>';
    ((S.data && S.data.tools) || []).forEach((t) => {
      const o = el('option', '', esc(t.name));
      o.value = t.tool;
      sel.appendChild(o);
    });
    sel.value = S.recTool || cur;
  }

  /* ---------------- 主题 ---------------- */
  // 主题刻意保持"少而精"：6 套 + 跟随系统。深色/浅色分开只是为了给"跟随系统"
  // 挑一个合适的兜底，同时决定 color-scheme 与图表重绘。
  const THEMES_DARK = ['dark', 'ocean', 'neon'];
  const THEMES_LIGHT = ['light', 'mint', 'solar', 'ink'];
  const THEMES = THEMES_DARK.concat(THEMES_LIGHT);
  // 侧栏「切换亮色」按钮在这几套之间轮换（跟着系统走时也能一键换掉）
  const THEME_CYCLE = ['dark', 'light', 'ocean', 'ink', 'neon', 'mint', 'solar'];
  // 主题名的双语文案：设置页下拉、切换提示与侧栏按钮共用一份，避免三处不一致。
  const THEME_NAMES = {
    dark: { 'zh-CN': '深空', 'en-US': 'Deep space' },
    ocean: { 'zh-CN': '海蓝', 'en-US': 'Ocean' },
    neon: { 'zh-CN': '霓虹', 'en-US': 'Neon' },
    light: { 'zh-CN': '亮色', 'en-US': 'Light' },
    mint: { 'zh-CN': '薄荷', 'en-US': 'Mint' },
    solar: { 'zh-CN': '日光', 'en-US': 'Daylight' },
    ink: { 'zh-CN': '水墨', 'en-US': 'Ink wash' },
    auto: { 'zh-CN': '跟随系统', 'en-US': 'System' },
  };
  function resolveTheme(t) {
    if (t === 'auto') {
      try {
        return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      } catch (e) { return 'dark'; }
    }
    return THEMES.includes(t) ? t : 'dark';
  }

  function applyTheme(theme, persist = true, rerender = true) {
    const requested = (THEMES.includes(theme) || theme === 'auto') ? theme : 'dark';
    const eff = resolveTheme(requested);
    const changed = document.documentElement.getAttribute('data-theme') !== eff;
    S.themePreference = requested;
    document.documentElement.setAttribute('data-theme', eff);
    document.documentElement.setAttribute('data-theme-pref', requested);
    // 让浏览器把原生控件（下拉弹层、日期选择器、滚动条）也切成对应的明暗。
    // 这不只是美观问题：深色主题下如果原生弹层按浅色绘制，选项文字会看不清。
    const isLight = THEMES_LIGHT.includes(eff);
    document.documentElement.style.colorScheme = isLight ? 'light' : 'dark';
    // 按钮上写清楚"点一下会变成什么"，比永远显示「跟随系统」有用。
    const t = $('#themeLabel');
    if (t) {
      const i = THEME_CYCLE.indexOf(requested);
      const next = THEME_CYCLE[(i < 0 ? 0 : i + 1) % THEME_CYCLE.length];
      const name = (THEME_NAMES[next] || {})[S.language] || next;
      t.textContent = (S.language === 'en-US' ? 'Switch to ' : '切换为') + name;
    }
    if (persist) persistPreference('theme', requested, applyTheme);
    // 只有配色真的变了才需要重绘图表；否则每次 load() 都会白白重建一批 SVG
    if (changed && rerender && uiReady) {
      S.rendered = {};
      if (S.data) scheduleViewRender(S.view);
    }
  }

  // 强调色 / 圆角：用 CSS 变量覆盖，不必为每种组合再写一套主题。
  const ACCENTS = {
    indigo: ['#7c8cf8', '#a855f7', '#22d3ee'],
    violet: ['#a78bfa', '#e879f9', '#818cf8'],
    cyan: ['#22d3ee', '#38bdf8', '#2dd4bf'],
    emerald: ['#34d399', '#10b981', '#22d3ee'],
    amber: ['#fbbf24', '#fb923c', '#f472b6'],
    rose: ['#fb7185', '#f472b6', '#a78bfa'],
  };
  const RADII = { sharp: ['8px', '5px'], soft: ['14px', '10px'], round: ['22px', '15px'] };
  function applyAccent(key, persist = true) {
    const k = ACCENTS[key] ? key : 'theme';
    const root = document.documentElement;
    root.dataset.accent = k;
    if (k === 'theme') {
      ['--accent', '--accent-2', '--accent-3'].forEach((v) => root.style.removeProperty(v));
    } else {
      const [a, b, c] = ACCENTS[k];
      root.style.setProperty('--accent', a);
      root.style.setProperty('--accent-2', b);
      root.style.setProperty('--accent-3', c);
    }
    if (persist) persistPreference('accent', k, applyAccent);
  }
  function applyRadius(key, persist = true) {
    const k = RADII[key] ? key : 'theme';
    const root = document.documentElement;
    if (k === 'theme') {
      root.style.removeProperty('--radius');
      root.style.removeProperty('--radius-sm');
    } else {
      const [r, rs] = RADII[k];
      root.style.setProperty('--radius', r);
      root.style.setProperty('--radius-sm', rs);
    }
    if (persist) persistPreference('radius', k, applyRadius);
  }
  // 纯前端偏好（不动后端）：极光背景、数字动画、FPS 表
  function applyBoolPref(name, on) {
    document.documentElement.classList.toggle('no-' + name, !on);
    try { localStorage.setItem('tl.pref.' + name, on ? '1' : '0'); } catch (_) { /* 隐私模式 */ }
    if (name === 'fps' && fpsMeterSync) fpsMeterSync();
    if (name === 'motion' && window.TLMotion) window.TLMotion.sync();
  }
  function readBoolPref(name, dflt) {
    try {
      const v = localStorage.getItem('tl.pref.' + name);
      return v === null ? dflt : v === '1';
    } catch (_) { return dflt; }
  }

  function initFpsPreference() {
    // Older releases persisted the old false default on every launch. Migrate
    // once so the live meter is visible; later explicit preferences still work.
    let liveFps = true;
    try {
      if (localStorage.getItem('tl.fps.live.v1')) liveFps = readBoolPref('fps', true);
      else localStorage.setItem('tl.fps.live.v1', '1');
    } catch (_) { /* default visible when storage is unavailable */ }
    applyBoolPref('fps', liveFps);
  }

  const FONT_PRESETS = {
    system: '"Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    humanist: '"Trebuchet MS", "PingFang SC", "Microsoft YaHei", sans-serif',
    serif: 'Georgia, "Songti SC", "SimSun", serif',
    mono: '"Cascadia Code", "SFMono-Regular", Consolas, monospace',
    classic: '"Noto Sans CJK SC", "Source Han Sans SC", "SimSun", sans-serif',
    rounded: '"Nunito", "Quicksand", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
  };
  function applyFont(font, persist = true) {
    const key = FONT_PRESETS[font] ? font : 'system';
    document.documentElement.dataset.font = key;
    document.documentElement.style.setProperty('--font', FONT_PRESETS[key]);
    if (persist) persistPreference('font', key, applyFont);
  }
  function applyDensity(density, persist = true) {
    const key = ['compact', 'comfortable', 'spacious'].includes(density) ? density : 'compact';
    document.documentElement.dataset.density = key;
    if (persist) persistPreference('density', key, applyDensity);
  }

  const LANG = {
    'zh-CN': {
      nav: {assistant:S.language==='en-US'?'AI Assistant':'AI 助手',overview:'总览',insights:'洞察',tools:'按工具',models:'按模型',projects:'按项目',sessions:'按会话',sources:'数据源',records:'明细流水',prompt:'提示词优化','ai-test':'AI 智商检测',settings:'设置'},
      title: {assistant:TITLES.assistant,overview:['总览','全部编程工具与 AI 助手的 token 消耗'],insights:['洞察','自动提炼的关键结论与周期对比'],tools:['按工具','每个工具各自的用量与趋势'],models:['按模型','模型维度的用量与成本'],projects:['按项目','工程目录维度的用量分布'],sessions:['按会话','单次会话的消耗排行'],sources:['数据源','本机各工具的数据落盘位置与采集状态'],records:['明细流水','逐条调用记录，可搜索与导出'],prompt:['提示词优化','清晰润色与专业结构'], 'ai-test':['AI 智商检测','逻辑题与 SVG 创作能力检测'],settings:['设置','界面偏好、扫描自动化、预算与自定义数据源']},
    },
    'en-US': {
      nav: {assistant:S.language==='en-US'?'AI Assistant':'AI 助手',overview:'Overview',insights:'Insights',tools:'By Tool',models:'By Model',projects:'By Project',sessions:'Sessions',sources:'Sources',records:'Records',prompt:'Prompt Lab','ai-test':'AI Benchmark',settings:'Settings'},
      title: {assistant:TITLES.assistant,overview:['Overview','Token usage across local AI tools'],insights:['Insights','Automated findings and period comparison'],tools:['By Tool','Usage and trends by tool'],models:['By Model','Usage and cost by model'],projects:['By Project','Usage by project directory'],sessions:['Sessions','Highest-consumption sessions'],sources:['Sources','Local data locations and collection status'],records:['Records','Individual calls, search and export'],prompt:['Prompt Lab','Clarity editing and professional structure'], 'ai-test':['AI Benchmark','Logic and SVG generation tests'],settings:['Settings','Interface, scanning, budget and data sources']},
    },
  };
  function applyLanguage(language, persist = true) {
    LANG['zh-CN'].nav.assistant='AI 助手';LANG['en-US'].nav.assistant='AI Assistant';
    LANG['zh-CN'].title.assistant=TITLES.assistant;
    LANG['en-US'].title.assistant=['AI Assistant','Analysis grounded in recorded usage data'];
    LANG['zh-CN'].nav.manage = '成本管理';
    LANG['en-US'].nav.manage = 'Cost management';
    LANG['zh-CN'].title.manage = ['成本管理', '项目预算、数据质量、价格版本与账单核对'];
    LANG['en-US'].title.manage = ['Cost management', 'Project budgets, data quality, price versions and reconciliation'];
    const key = language === 'en-US' ? 'en-US' : 'zh-CN';
    S.language = key;
    configureMoney();
    // 语言与主题都以**后端配置**为准（config.json），localStorage 只做会话内
    // 的即时记录。之前 localStorage 的旧值会在下一次启动时抢先于 config.json
    // 生效——用户在设置里明明选了简体中文，界面却还是英文，就是这个原因。
    localStorage.setItem('tl.language', key);
    document.documentElement.lang = key;
    document.documentElement.dataset.language = key;
    const dict = LANG[key];
    const en = key === 'en-US';
    const setText = (selector, zh, english) => { const n = $(selector); if (n) n.textContent = en ? english : zh; };
    const setButton = (selector, zh, english) => {
      const n = $(selector); if (!n) return;
      const value = en ? english : zh;
      const nodes = Array.from(n.childNodes).filter((x) => x.nodeType === 3);
      if (nodes.length) nodes[nodes.length - 1].nodeValue = value;
      else n.appendChild(document.createTextNode(value));
    };
    $$('.nav a').forEach((a) => { const s = $('span', a); if (s && dict.nav[a.dataset.view]) s.textContent = dict.nav[a.dataset.view]; });
    const title = dict.title[S.view] || dict.title.overview;
    $('#viewTitle').textContent = title[0]; $('#viewSub').textContent = title[1];
    $('#languageSelect').value = key;
    setText('#scanText', '就绪', 'Ready');
    setButton('#rescanBtn', '重新扫描', 'Rescan');
    setButton('#cancelScanBtn', '取消扫描', 'Cancel scan');
    setButton('#aboutBtn', '关于', 'About');
    setButton('#diagnosticsBtn', '导出诊断', 'Diagnostics');
    setText('#backupTitle', '备份与恢复', 'Backup and restore');
    setText('#backupNote', '备份用量、项目、价格版本、对账和提示词历史。密钥不进入备份；恢复保留本机设置。', 'Back up usage, projects, price versions, reconciliations and prompt history. Keys are excluded; local settings are retained on restore.');
    setText('#backupFileLabel', '选择备份 ZIP（最多32MiB）', 'Backup ZIP (max 32MiB)');
    setButton('#backupExportBtn', '下载本地备份', 'Download backup');
    setButton('#backupRestoreBtn', '恢复已预览备份', 'Restore previewed backup');
    
    setButton('#plConfigBtn', '配置 AI', 'Configure AI'); setButton('#plHistoryBtn', '历史记录', 'History');
    setButton('#plSaveNewBtn', '另存为新配置', 'Save as new'); setButton('#plProfileDelete', '删除', 'Delete');
    setButton('#plTestBtn', '测试连接', 'Test connection');
    setButton('#iqAvailabilityBtn', '自检', 'Check');
    setButton('#iqRunAllBtn', '检测全部', 'Test all');
    setButton('#iqManualOpen','其他连接','Other connection');
    setText('#iqManualTitle','其他连接','Other connection');
    setButton('#iqManualCancel','取消','Cancel');setButton('#iqManualApply','使用此连接','Use connection');
    setText('#plHistoryTitle', '历史提示词', 'Prompt history');
    setText('#plHistoryMeta', '选择记录后，可在此弹窗查看完整内容；当前输入不会被修改', 'Select a record to view it here; current input stays unchanged');
    setButton('#plHistoryClear', '清空历史', 'Clear history');
    setButton('#plRunBtn', '开始优化', 'Optimize'); setText('#plSampleBtn', '填入示例', 'Use example');
    setText('#plRunTip', PL.cfg && PL.cfg.configured ? (en ? 'The selected optimization mode will be applied' : '将按当前选择的优化方式处理') : (en ? 'Configure AI before optimizing' : '请先配置 AI 后开始优化'));
    setText('.pl-hero h3', '提示词优化工作区', 'Prompt optimization workspace');
    setText('.pl-hero>div>p:last-child', '根据需求选择清晰润色或专业结构，输出可直接使用的提示词。', 'Choose clarity editing or professional structure for a ready-to-use prompt.');
    $$('#plMode button').forEach((b) => {
      const structured = b.dataset.promptMode === 'structured';
      const spans = $$('span', b); if (spans[1]) { const parts = $$('b,small', spans[1]); if (parts[0]) parts[0].textContent = structured ? (en ? 'Professional structure' : '专业结构') : (en ? 'Clarity editing' : '清晰润色'); if (parts[1]) parts[1].textContent = structured ? (en ? 'Add role, goals, scope, constraints and output requirements' : '补充角色、目标、内容范围、约束与输出要求') : (en ? 'Improve wording and logic while preserving intent' : '在不改变原意的前提下，改善措辞、语序与逻辑衔接'); }
    });
    setText('.pl-composer .card-head h3', '原始提示词', 'Original prompt'); setText('.pl-composer .card-head p', '粘贴需要改写的内容。', 'Paste the prompt to optimize.');
    setText('#plSampleBtn', '填入示例', 'Use example');
    const plInput = $('#plInput'); if (plInput) plInput.placeholder = en ? 'Example: write a web crawler' : '例如：帮我写一个爬虫';
    const iqModel = $('#iqModel'); if (iqModel) iqModel.placeholder = en ? 'e.g. gpt-4o-mini' : '例如 gpt-4o-mini';
    const iqUrl = $('#iqUrl'); if (iqUrl) iqUrl.placeholder = en ? 'https://api.openai.com/v1' : 'https://api.openai.com/v1';
    const iqKey = $('#iqKey'); if (iqKey) iqKey.placeholder = en ? 'Used for this test only' : '仅本次检测使用';
    setText('.iq-config-head h3', '连接模型', 'Connect model'); setText('.iq-config-head p', '可直接选择本机 CC Switch 模型。测试会请求对应服务，不使用工具或网页搜索；结果仅代表本次样例。', 'Choose a local CC Switch model directly. Tests call the selected service without tools or web search; results describe this sample only.');
    if(window.TLCCSwitch)window.TLCCSwitch.language(key);
    if(window.TLAssistant)window.TLAssistant.language(key);
    if(window.TLAISettings)window.TLAISettings.language(key);
    setText('#iqTip',iqCredsReady()?'可直接开始检测 · 自检可选':'选择模型后即可开始检测',iqCredsReady()?'Ready to test · Check is optional':'Choose a model to start');
    iqUpdateConnectionSummary();
    const iqNav = [['q1','逻辑题一','Logic test 1','标准答案 21','Expected answer 21','未检测','Not tested'],['q2','逻辑题二','Logic test 2','标准答案 20','Expected answer 20','未检测','Not tested'],['pelican','SVG 创作','SVG creation','生成并渲染动画','Generate and render animation','未生成','Not generated']];
    iqNav.forEach(([mode,zh,eng,zhSmall,engSmall,zhState,engState]) => { const b=$(`#iqTabs button[data-iq-tab="${mode}"]`); if(!b)return; const spans=$$('span',b); if(spans[1]){const bits=$$('b,small',spans[1]);if(bits[0])bits[0].textContent=en?eng:zh;if(bits[1])bits[1].textContent=en?engSmall:zhSmall;} const state=$('.iq-tab-state',b); if(state && !b.classList.contains('done'))state.textContent=en?engState:zhState; });
    setText('#iqQ1 .iq-qhead h3','极端抽取条件下的充分性判断','Sufficiency under extreme sampling'); setText('#iqQ2 .iq-qhead h3','条件变化下的答案稳定性判断','Answer stability under changed conditions');
    setButton('.iq-run[data-iq-mode="q1"]','开始检测','Run test'); setButton('.iq-run[data-iq-mode="q2"]','开始检测','Run test'); setButton('.iq-run[data-iq-mode="pelican"]','开始生成','Generate'); setButton('#iqHtmlOpen','复制 HTML','Copy HTML');
    ['#iqA1','#iqA2','#iqAPelican'].forEach((id) => { setText(`${id} .iq-answer-label`, '模型公开回答', 'Model response'); setText(`${id} .iq-reason-label`, id === '#iqAPelican' ? '结果说明' : '判定说明', id === '#iqAPelican' ? 'Result' : 'Decision'); });
    setButton('#methodBtn', '统计口径', 'Methodology');
    // 用稳定的 id 选择器，不再依赖 :nth-child——新增卡片会让序号错位，
    // 把「提示词优化」的标题翻译成「自定义日志目录」。
    const cardHeads = [
      ['#cardAppearance', '外观', 'Appearance', '主题、字体与信息密度', 'Theme, font and density'],
      ['#cardScan', '扫描与性能', 'Scanning and performance', '默认采用增量索引，优先保持界面响应', 'Incremental indexing keeps the interface responsive'],
      ['#cardBudget', '预算提醒', 'Budget alerts', '0 表示不设置上限', '0 means no limit'],
      ['#cardFeatures', '功能开关', 'Feature toggles', '立即保存，仅隐藏导航入口；采集、统计和已保存的数据不受影响', 'Saved immediately. Hidden entries do not change collection, totals or saved data.'],
      ['#cardPrompt', '提示词优化', 'Prompt Lab', '可选：改写时是否让模型先思考', 'Optional: let the model reason before rewriting'],
      ['#cardRoots', '自定义日志目录', 'Custom log directories', '添加 OpenAI / Anthropic 兼容 usage 日志', 'Add OpenAI / Anthropic-compatible usage logs'],
    ];
    cardHeads.forEach(([sel, zh, eng, zhSub, enSub]) => {
      setText(sel + ' .card-head h3', zh, eng);
      setText(sel + ' .card-head p', zhSub, enSub);
    });
    const fieldLabels = [
      ['#themeSelect','界面主题','Theme'],['#fontSelect','界面字体','Font'],['#densitySelect','界面密度','Density'],['#rateInput','美元兑人民币汇率','USD/CNY rate'],['#languageSelect','界面语言','Language'],
      ['#displayCurrencySelect','金额显示币种','Display currency'],
      ['#accentSelect','主题强调色','Accent color'],['#radiusSelect','圆角','Corner radius'],
      ['#autoScanInput','自动扫描（分钟）','Auto scan (minutes)'],['#workerInput','解析线程','Parser workers'],['#maxMbInput','单文件上限（MB）','File limit (MB)'],
      ['#recPageInput','明细每页条数','Rows per page'],['#listCapInput','列表最多展示','Max list rows'],
      ['#budgetTokensInput','Token 预算（本周期）','Token budget (period)'],['#budgetCostInput','成本预算（USD）','Cost budget (USD)'],
      ['#warnRatioInput','预警阈值（百分比）','Alert threshold (%)'],['#defaultRangeSelect','默认时间范围','Default time range'],
      ['#plMaxTokensInput','生成上限（tokens）','Output limit (tokens)'],
      ['#rootInput','自定义日志目录','Custom log directory'],
    ];
    fieldLabels.forEach(([id, zh, english]) => { const input = $(id); const label = input && input.closest('.field') && $('span', input.closest('.field')); if (label) label.textContent = en ? english : zh; });
    const optionLabels = {
      '#themeSelect': Object.fromEntries(Object.entries(THEME_NAMES)
        .map(([k, v]) => [k, [v['zh-CN'], v['en-US']]])),
      '#fontSelect': {system:['系统无衬线','System sans'],humanist:['人文无衬线','Humanist sans'],serif:['中文衬线','Chinese serif'],mono:['等宽字体','Monospace'],classic:['经典宋黑','Classic Chinese'],rounded:['圆润无衬线','Rounded sans']},
      '#densitySelect': {compact:['紧凑','Compact'],comfortable:['舒适','Comfortable'],spacious:['宽松','Spacious']},
      '#accentSelect': {theme:['跟随主题','Follow theme'],indigo:['靛蓝','Indigo'],violet:['紫罗兰','Violet'],cyan:['青碧','Cyan'],emerald:['翡翠','Emerald'],amber:['琥珀','Amber'],rose:['玫瑰','Rose']},
      '#radiusSelect': {theme:['跟随主题','Follow theme'],sharp:['硬朗','Sharp'],soft:['柔和','Soft'],round:['圆润','Round']},
      '#defaultRangeSelect': {today:['今天','Today'],'7d':['7 天','7 days'],'30d':['30 天','30 days'],'90d':['90 天','90 days'],'365d':['一年','1 year'],all:['全部','All time']},
      '#languageSelect': {'zh-CN':['简体中文','Simplified Chinese'],'en-US':['English','English']},
      '#displayCurrencySelect': {auto:['跟随语言','Follow language'],cny:['人民币 CNY','Chinese yuan CNY'],usd:['美元 USD','US dollar USD']},
    };
    Object.entries(optionLabels).forEach(([selector, labels]) => { const select = $(selector); if (!select) return; Array.from(select.options).forEach((o) => { if (labels[o.value]) o.textContent = labels[o.value][en ? 1 : 0]; }); });
    // optgroup 的分组标题同样要跟着走
    const themesSel = $('#themeSelect');
    if (themesSel) Array.from(themesSel.querySelectorAll('optgroup')).forEach((g, i) => {
      g.label = i === 0 ? (en ? 'Dark' : '深色') : (en ? 'Light' : '浅色');
    });
    const themeBtn = $('#themeBtn'); if (themeBtn) Array.from(themeBtn.childNodes).filter((n) => n.nodeType === 3).forEach((n) => n.remove());
    // 侧栏主题按钮上的文案由 applyTheme 统一计算（"切换为 X"），
    // 换语言时重算一次即可，不要在这里写死。
    applyTheme(S.themePreference || 'dark', false, false);
    const switches = [
      ['#motionToggle','数字滚动动画','Animated counters','切换时间范围时数值平滑过渡','Smoothly animate values when the time range changes'],
      ['#fpsToggle','显示帧率表','Show FPS meter','右上角的实时 FPS 指示','Live frame-rate indicator in the top-right corner'],
      ['#scanOnStartToggle','启动时自动扫描','Scan on startup','打开应用后在后台执行一次增量扫描','Run one incremental scan in the background when the app starts'],
      ['#quickToggle','Codex 极速模式','Codex fast mode','只读汇总表，跳过逐轮明细','Read summary tables and skip turn-level details'],
      ['#bigLibToggle','大库模式','Large-library mode','记录量很大时自动限制明细扫描规模，优先保证界面流畅','Limit detail queries on very large libraries to keep the interface responsive'],
      ['#plThinkToggle','展示模型思考过程','Show model reasoning','关闭后结果区只显示改写结果，界面更清爽（推荐关闭）','When off, only the rewritten prompt is shown (recommended)'],
      ['#aiWarmupToggle','预热模型连接','Warm up model connection','可能增加一次付费请求；默认关闭，用量计入自身 AI 消费','May add a billed request. Off by default; counted in App AI usage.'],
      ['#aiHedgeToggle','慢响应时补发请求','Retry slow responses concurrently','可能重复计费；默认关闭，每次补发均单独记录','May incur duplicate charges. Off by default; each attempt is recorded.'],
      ['#plEnabledToggle','AI 提示词优化','AI Prompt Lab','关闭后左侧导航不再显示「提示词优化」','Hides the Prompt Lab entry in the sidebar'],
      ['#iqEnabledToggle','AI 智商检测','AI Benchmark','关闭后左侧导航不再显示「AI 智商检测」','Hides the AI Benchmark entry in the sidebar'],
    ];
    switches.forEach(([id,zh,english,zhSmall,enSmall]) => { const input=$(id); const row=input && input.closest('.switch-row'); if(row){ const b=$('b',row), s=$('small',row); if(b)b.textContent=en?english:zh; if(s)s.textContent=en?enSmall:zhSmall; }});
    setText('.settings-actions>span','危险操作不会删除原始日志。','Destructive actions do not delete original logs.');
    setText('label:has(#budgetPeriodSelect)>span', '预算账期', 'Budget period');
    $$('#budgetPeriodSelect option').forEach(o => { o.textContent = o.value === 'month' ? (en ? 'Calendar month' : '自然月') : (en ? 'Calendar week (Monday)' : '自然周（周一开始）'); });
    setText('.settings-intro h3', '运行与界面偏好', 'Runtime and interface preferences');
    setText('.settings-intro>div>p:last-child', '更改后自动保存；扫描参数在下一次任务中生效。', 'Changes save automatically; scan parameters apply to the next run.');
    setButton('#forceScanBtn', '强制全量重扫', 'Full rescan'); setButton('#resetBtn', '清空索引', 'Clear index'); 
    // 上面这些是"手写的双语"（选择器明确、文案固定）。剩下的界面文案统一交给
    // i18n.js 在渲染后处理，两者互补：这里的覆盖左侧导航与固定按钮，那里的
    // 覆盖所有随数据变化的内容（表格表头、KPI 标签、空状态、提示语）。
    if (window.TL_I18N) window.TL_I18N.setLang(key);
    // 必须让已渲染的视图失效并重画一次。原因：数字的写法（13.3亿 vs 1.33B）、
    // 表头、以及后端下发的 *_en 文案，都是在 render 时按当前语言定下来的；
    // 只靠 i18n 的文本替换改不了"已经拼进字符串里的量词与单位"。
    // 不重画的话，切换语言后总览的大数字仍然是「13.3亿」。
    invalidateViews();
    if (S.data && uiReady) scheduleViewRender(S.view);
    if (persist) persistPreference('language', key, applyLanguage);
    renderNavigationContext();renderRecordPresets();
  }

  function watchSystemTheme() {
    try {
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      const h = () => {
        if ((document.documentElement.getAttribute('data-theme-pref') || 'dark') === 'auto') {
          applyTheme('auto', false);
        }
      };
      mq.addEventListener ? mq.addEventListener('change', h) : mq.addListener(h);
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 扫描进度 ----------------
     改推模式：以前是 pollScan 递归 setTimeout 拉 /api/scan/status（正常
     620ms、出错 1500ms），三万八千个文件扫 15.7 秒，等于把一条连续曲线采样
     成二十几个点，进度条看着是跳的。现在服务端每 200ms 推一帧，只有一条
     长连接；写入方式与 prompt / ai-test 那两条流一致（chunked + data: 帧）。 */

  /* 把一帧扫描状态画到界面上。final 表示这是终态帧（done/error/…）。
     只有终态才收尾：扫描线程可能还没把 running 置位，抢先收尾会把界面刷成
     「就绪」；另外服务端会对「接上流时上一轮就已经扫完」的终态帧标
     fresh=false —— 那种情况不该再弹一次「扫描完成」、也不该白刷一遍仪表盘。 */
  let lastScanPaint = 0;
  function applyScanState(s, final) {
    const state = $('#scanState'), txt = $('#scanText');
    const wrap = $('#progressWrap'), bar = $('#progressBar');
    const cancel = $('#cancelScanBtn');
    S.scanState = s || {};
    if (s.finished) observedScanFinish = Math.max(observedScanFinish, s.finished);
    S.scanRunning = !!s.running;
    const now = performance.now();
    if (s.running && !final && now - lastScanPaint < 200) return false;
    lastScanPaint = now;
    if (S.view === 'overview') renderOverviewStatus();
    if (S.view === 'sources') renderSourceScanSummary();
    if (s.running) {
      state.classList.add('busy');
      const scope = s.target ? ` · ${s.target}` : '';
      txt.textContent = `扫描中 ${s.percent}%${scope} · ${s.current || ''}`.slice(0, 72);
      wrap.classList.add('on');
      if (cancel) cancel.hidden = false;
      bar.style.width = s.percent + '%';
      return false;
    }
    state.classList.remove('busy');
    wrap.classList.remove('on');
    if (cancel) cancel.hidden = true;
    if (!final) return false;
    const fresh = s.fresh !== false;      // 缺省按新事件处理（兼容老服务端）
    if (s.phase === 'done') {
      const em = s.errors && s.errors.length ? `（${s.errors.length} 条提示）` : '';
      txt.textContent = `${s.total} 个文件 · 新解析 ${s.parsed_files} · ${Math.round(s.elapsed)}s ${em}`;
      if (fresh) toast(`扫描完成：解析 ${s.parsed_files} 个文件，共 ${fmtExact(s.records)} 条新记录`, 'ok');
      return fresh;
    }
    if (s.phase === 'error') {
      txt.textContent = '扫描出错';
      if (fresh) toast('扫描出错：' + ((s.errors || [])[0] || '未知'), 'err', 6000);
      return fresh;
    }
    if (s.phase === 'cancelled') { txt.textContent = '扫描已取消'; return fresh; }
    txt.textContent = '就绪';
    return false;                          // idle：没有收尾动作
  }

  function stopScanStream() {
    if (S.scanAbort) { try { S.scanAbort.abort(); } catch (_) { /* ignore */ } S.scanAbort = null; }
  }

  async function pollScan() {
    stopScanStream();
    const ac = new AbortController();
    S.scanAbort = ac;
    let finished = false, doneState = null;
    try {
      const r = await fetch('/api/scan/stream', { signal: ac.signal, headers: { 'X-TokenLens': '1' } });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      const reader = r.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buf.search(/\r?\n\r?\n/)) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + (buf[cut] === '\r' ? 4 : 2));
          for (const line of frame.split(/\r?\n/)) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            let obj;
            try { obj = JSON.parse(payload); } catch (_) { continue; }
            const isFinal = obj.type === 'done' || obj.type === 'result';
            if (applyScanState(obj, isFinal) && isFinal) { finished = true; doneState = obj; }
          }
        }
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;      // 我们自己关的流
      // 流断了（服务重启、或对端是没有这条接口的老版本）：先退回一次普通
      // 状态查询。扫完了就直接收尾，没扫完才安排重连——避免一直空转重试。
      try {
        const s = await api('/api/scan/status');
        if (applyScanState(s, !s.running)) {
          if (s.phase === 'done') { clearDashboardCache(); await load(true, true); }
          return;
        }
      } catch (_) { /* 服务也没应答，下面继续退避重试 */ }
      S.timer = setTimeout(pollScan, 1500);
      return;
    } finally {
      if (S.scanAbort === ac) S.scanAbort = null;
    }
    if (finished && doneState && doneState.phase === 'done') {
      clearDashboardCache();
      await load(true, true);
    } else if (finished) {
      // 取消 / 出错也要把「就绪」状态跟服务端对齐
      api('/api/scan/status').then((s) => applyScanState(s, true)).catch(() => {});
    }
  }

  async function startScan(force, sourceId) {
    const st = $('#scanState');
    st.classList.add('busy');
    S.scanRunning = true;
    $('#scanText').textContent = sourceId ? `正在启动 ${sourceId} 扫描…` : '正在启动扫描…';
    stopScanStream();
    try {
      await api('/api/scan', { method: 'POST', body: { force: !!force, source_id: sourceId || '' } });
      clearTimeout(S.timer);
      pollScan();
    } catch (err) {
      S.scanRunning = false; st.classList.remove('busy');
      $('#scanText').textContent = isEn() ? 'Scan failed — retry' : '扫描启动失败，可重试';
      toast(err.message || String(err), 'err');
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    // 应用窗口关闭时主动擦除运行期 Key；sessionStorage 也会在标签页结束时清理。
    window.addEventListener('beforeunload', () => {
      try { sessionStorage.removeItem(IQ_RUNTIME_KEY); } catch (_) { /* ignore */ }
    }, { once: true });
    $$('.nav a').forEach((a, i) => {
      a.href = '#' + a.dataset.view;
      a.dataset.kbd = String(i + 1);
      a.onclick = (e) => { e.preventDefault(); switchView(a.dataset.view); };
    });
    $('#navToggle').onclick = () => {
      const open = document.documentElement.classList.toggle('nav-open');
      $('#navToggle').setAttribute('aria-expanded', String(open));
    };
    $('#navScrim').onclick = () => { document.documentElement.classList.remove('nav-open'); $('#navToggle').setAttribute('aria-expanded','false'); };
    $('#diagnosticsBtn').onclick = async () => { try { if(window.TLNative) {const saved=await window.TLNative.download('/api/diagnostics');if(saved)toast(isEn()?'Diagnostics saved':'诊断已保存','ok');} else window.location.href='/api/diagnostics'; } catch(error) {toast(error.message,'err');} };
    let backupToken = '';
    $('#backupExportBtn').onclick = async () => {
      const button = $('#backupExportBtn'); button.disabled = true;
      try {
        if (window.TLNative) {
          const saved=await window.TLNative.download('/api/backup/export',{method:'POST',body:JSON.stringify({prompt_history:readPromptHistory()})});
          if(saved)toast(isEn()?'Backup saved':'备份已保存','ok');
          return;
        }
        const response = await fetch('/api/backup/export', {method:'POST',
          headers:{'X-TokenLens':'1','Content-Type':'application/json'},
          body:JSON.stringify({prompt_history:readPromptHistory()})});
        if (!response.ok) { const error = await response.json(); throw new Error(error.error || 'Backup failed'); }
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement('a'); link.href = url;
        link.download = 'tokenlens-backup-' + new Date().toISOString().slice(0,10) + '.zip';
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 2000);
        toast(isEn() ? 'Backup downloaded' : '备份已下载', 'ok');
      } catch (error) { toast(error.message, 'err'); }
      finally { button.disabled = false; }
    };
    $('#backupFile').onchange = async (event) => {
      backupToken = ''; $('#backupRestoreBtn').disabled = true;
      const status = $('#backupPreview'), file = event.target.files[0];
      status.textContent = '';
      if (!file) return;
      if (file.size > 32*1024*1024) return toast(isEn() ? 'Backup exceeds 32MiB' : '备份超过32MiB', 'err');
      status.textContent = isEn() ? 'Validating backup…' : '正在验证备份…';
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = ''; for (let offset=0;offset<bytes.length;offset+=32768) {
          binary += String.fromCharCode(...bytes.subarray(offset,offset+32768));
        }
        const result = await api('/api/backup/preview', {method:'POST',body:{archive:btoa(binary)}});
        backupToken = result.token;
        const s = result.summary;
        status.textContent = isEn()
          ? `Validated: ${s.records} records, ${s.managed_projects} projects, ${s.price_versions} price versions, ${s.reconciliations} reconciliations and ${s.prompt_history} prompts. ${s.first_day || '—'} ~ ${s.last_day || '—'}.`
          : `验证通过：${s.records} 条记录、${s.managed_projects} 个项目、${s.price_versions} 个价格版本、${s.reconciliations} 份对账、${s.prompt_history} 条提示词。${s.first_day || '—'} ~ ${s.last_day || '—'}。`;
        $('#backupRestoreBtn').disabled = false;
      } catch (error) { status.textContent = error.message; toast(error.message, 'err'); }
    };
    $('#backupRestoreBtn').onclick = async () => {
      if (!backupToken) return;
      if (!(await confirmDialog(isEn()
        ? 'Replace the local usage and management data with this backup? A recovery backup of the current data will be saved first. Local settings and keys are retained.'
        : '用这份备份替换本机用量和管理数据吗？恢复前会自动保存当前数据的备份，本机设置和密钥保留。', {danger:true}))) return;
      const button = $('#backupRestoreBtn'); button.disabled = true;
      try {
        const result = await api('/api/backup/restore', {method:'POST',body:{token:backupToken}});
        if (Array.isArray(result.prompt_history)) localStorage.setItem(PL_HIST, JSON.stringify(result.prompt_history));
        backupToken = ''; $('#backupFile').value = '';
        $('#backupPreview').textContent = (isEn() ? 'Restored. Recovery backup: ' : '恢复完成。恢复前备份：') + result.recovery_backup;
        clearDashboardCache(); invalidateViews(); await load(true,true);
        toast(isEn() ? 'Backup restored' : '备份已恢复', 'ok');
      } catch (error) { button.disabled = false; toast(error.message, 'err'); }
    };

    $$('#rangeSeg button').forEach((b) => b.onclick = () => {
      const r = b.dataset.range;
      if (r === 'custom') { openRangePop(); return; }
      if (r === S.range) return;
      saveCurrentNavigation();
      closeRangePop();
      $$('#rangeSeg button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      S.range = r; localStorage.setItem('tl.range', S.range);
      // 主时间范围变了，明细页的日期精修不再成立，清掉避免"两边打架"
      S.recFrom = ''; S.recTo = '';
      applyRecDays('', '', false);
      S.recOffset = 0;
      S.rangeMotion = true;
      pendingScroll=null;
      if(S.view==='assistant')window.TLAssistant?.setContext?.({...currentContext(),range:S.range,from:'',to:''});
      renderNavigationContext();writeNavigation();
      load();
    });

    /* ---- 自定义时间段 ---- */
    function openRangePop() {
      const pop = $('#rangePop'); if (!pop) return;
      const D = S.data;
      const st = $('#rangeStart'), en = $('#rangeEnd');
      // 已有区间就回填，否则默认最近 7 天
      if (D && D.range_start) st.value = D.range_start;
      if (D && D.range_end) en.value = D.range_end;
      if (!st.value || !en.value) {
        const t = new Date(), from = new Date(Date.now() - 6 * 864e5);
        const iso = (d) => d.toISOString().slice(0, 10);
        st.value = iso(from); en.value = iso(t);
      }
      pop.hidden = false;
      updateRangeHint();
      st.focus();
    }
    function closeRangePop() { const p = $('#rangePop'); if (p) p.hidden = true; }
    function updateRangeHint() {
      const st = $('#rangeStart').value, en = $('#rangeEnd').value, hint = $('#rangeHint');
      if (!hint) return;
      if (!st || !en) { hint.textContent = '请选择开始与结束日期'; hint.classList.add('bad'); return; }
      const a = new Date(st + 'T00:00:00'), b = new Date(en + 'T00:00:00');
      if (isNaN(a) || isNaN(b)) { hint.textContent = '日期格式不正确'; hint.classList.add('bad'); return; }
      const days = Math.round(Math.abs(b - a) / 864e5) + 1;
      hint.classList.remove('bad');
      hint.textContent = `${st} 起，共 ${days} 天` + (days === 1 ? '（将按小时展示当天分布）' : '');
    }
    function applyCustomRange() {
      const st = $('#rangeStart').value, en = $('#rangeEnd').value;
      if (!st || !en) return toast('请选择开始与结束日期', 'err');
      // 起止写反了就自动纠正，不报错打断用户
      const a = st <= en ? st : en, b = st <= en ? en : st;
      const key = a + '~' + b;
      saveCurrentNavigation();
      S.range = key; localStorage.setItem('tl.range', key);
      S.recFrom = ''; S.recTo = '';
      applyRecDays('', '', false);
      S.recOffset = 0;
      S.rangeMotion = true;
      syncRangeButtons();
      closeRangePop();
      pendingScroll=null;
      if(S.view==='assistant')window.TLAssistant?.setContext?.({...currentContext(),range:S.range,from:'',to:''});
      renderNavigationContext();writeNavigation();
      load();
    }
    if ($('#rangeApply')) $('#rangeApply').onclick = applyCustomRange;
    if ($('#rangePopClose')) $('#rangePopClose').onclick = closeRangePop;
    ['#rangeStart', '#rangeEnd'].forEach((s) => {
      const n = $(s); if (n) n.addEventListener('change', updateRangeHint);
    });
    $$('#rangeQuick button').forEach((b) => b.onclick = () => {
      const t = new Date();
      const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
      let a, z;
      switch (b.dataset.quick) {
        case 'this-month': a = new Date(t.getFullYear(), t.getMonth(), 1); z = t; break;
        case 'last-month':
          a = new Date(t.getFullYear(), t.getMonth() - 1, 1);
          z = new Date(t.getFullYear(), t.getMonth(), 0); break;
        case 'last-7': a = new Date(Date.now() - 6 * 864e5); z = t; break;
        case 'last-14': a = new Date(Date.now() - 13 * 864e5); z = t; break;
        case 'last-30': a = new Date(Date.now() - 29 * 864e5); z = t; break;
        case 'this-year': a = new Date(t.getFullYear(), 0, 1); z = t; break;
        default: return;
      }
      $('#rangeStart').value = iso(a); $('#rangeEnd').value = iso(z);
      updateRangeHint();
    });
    // 点空白处收起弹层（点在弹层或"自定义"按钮上不收起）
    document.addEventListener('click', (e) => {
      const pop = $('#rangePop');
      if (!pop || pop.hidden) return;
      if (pop.contains(e.target) || (e.target.closest && e.target.closest('#rangeCustomBtn'))) return;
      closeRangePop();
    });

    /* ---- 今日分时段：切换粒度 ---- */
    // 60/30 分钟的桶后端都会算好（30 分钟是 60 的细分），所以这里只切显示，
    // 不重新请求——省一次往返，切换是瞬时的。
    $$('#intradaySeg button').forEach((b) => b.onclick = () => {
      $$('#intradaySeg button').forEach((x) => x.classList.toggle('active', x === b));
      S.intradayBucket = b.dataset.bucket === '30' ? 30 : 60;
      if (S.data) renderIntraday(S.data);
    });

    $$('#trendSeg button').forEach((b) => b.onclick = () => {
      $$('#trendSeg button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      S.metric = b.dataset.metric;
      if (S.data) renderTrend($('#trendChart'), S.data.daily, S.metric, S.data.config.usd_to_cny);
    });

    $('#themeBtn').onclick = toggleTheme;
    $('#themeSelect').onchange = (e) => {
      applyTheme(e.target.value);
      toast('主题已切换为「' + e.target.selectedOptions[0].textContent + '」', 'ok', 1800);
    };
    $('#fontSelect').onchange = (e) => applyFont(e.target.value);
    $('#densitySelect').onchange = (e) => {
      applyDensity(e.target.value);
      const label = e.target.selectedOptions[0] ? e.target.selectedOptions[0].textContent : '';
      toast('界面密度已切换为「' + label + '」', 'ok', 1600);
    };
    if ($('#accentSelect')) $('#accentSelect').onchange = (e) => {
      applyAccent(e.target.value);
      toast('强调色已更新', 'ok', 1600);
    };
    if ($('#radiusSelect')) $('#radiusSelect').onchange = (e) => {
      applyRadius(e.target.value);
      toast('圆角已更新', 'ok', 1600);
    };
    // 纯前端偏好：改完立刻生效，不必点保存
    if ($('#motionToggle')) $('#motionToggle').onchange = (e) => applyBoolPref('motion', e.target.checked);
    if ($('#fpsToggle')) $('#fpsToggle').onchange = (e) => applyBoolPref('fps', e.target.checked);
    $$('[data-nav-feature]').forEach(input=>input.onchange=()=>{
      const key=input.dataset.navFeature,value=input.checked;
      const apply=(next)=>{rememberCfg(key,next);applyFeatureToggles();
        // Only currently visible row actions need updating when the assistant entry changes.
        if(key==='nav_assistant'){delete S.rendered.models;delete S.rendered.projects;renderNavigationContext();}}
      apply(value);persistPreference(key,value,apply);
    });
    $('#languageSelect').onchange = (e) => {
      const key = e.target.value === 'en-US' ? 'en-US' : 'zh-CN';
      localStorage.setItem('tl.language', key);
      applyLanguage(key);
      toast(key === 'en-US' ? 'Language changed to English' : '界面语言已切换为简体中文', 'ok');
    };
    if($('#displayCurrencySelect'))$('#displayCurrencySelect').onchange=event=>applyDisplayCurrency(event.target.value);
    $('#rescanBtn').onclick = () => startScan(false);
    const srcGrid = $('#srcGrid');
    if (srcGrid) srcGrid.addEventListener('click', async (e) => {
      const scanBtn = e.target.closest('[data-source-scan]');
      if (scanBtn && !scanBtn.disabled && !S.scanRunning) {
        startScan(false, scanBtn.getAttribute('data-source-scan'));
        return;
      }
      const revealBtn = e.target.closest('[data-source-reveal]');
      if (revealBtn) {
        // 服务端只打开自家适配器探测到的目录，路径不经过页面。
        try { await api('/api/sources/reveal', { method: 'POST', body: { id: revealBtn.getAttribute('data-source-reveal') } }); }
        catch (error) { toast(error.message || String(error), 'err'); }
        return;
      }
      const diagBtn = e.target.closest('[data-source-diag]');
      if (diagBtn) {
        if (window.TLNative) {
          try { await window.TLNative.download('/api/diagnostics'); toast(isEn() ? 'Diagnostics saved' : '诊断已保存', 'ok'); }
          catch (error) { toast(error.message || String(error), 'err'); }
        } else {
          try {
            const res = await fetch('/api/diagnostics');
            await navigator.clipboard.writeText(await res.text());
            toast(isEn() ? 'Diagnostics copied' : '诊断已复制', 'ok');
          } catch (error) { toast(String(error), 'err'); }
        }
      }
    });
    $('#cancelScanBtn').onclick = async () => {
      const btn = $('#cancelScanBtn'); if (btn) btn.disabled = true;
      try { await api('/api/scan/cancel', { method: 'POST', body: {} }); }
      finally { if (btn) btn.disabled = false; pollScan(); }
    };
    $('#forceScanBtn').onclick = () => startScan(true);
    $('#recTool').onchange = (e) => { saveCurrentNavigation();S.recTool = e.target.value; S.recOffset = 0; S.recCache = {}; S.recRequest++;renderNavigationContext();renderRecords(); };
    $('#recOrder').onchange = (e) => { saveCurrentNavigation();S.recOrder = e.target.value; S.recOffset = 0; S.recCache = {}; S.recRequest++;renderRecords(); };
    renderRecordPresets();
    $('#recSavePreset').onclick = saveRecordPreset;
    $('#recPresetSelect').onchange = (e) => {$('#recEditPreset').disabled=e.target.value==='';if (e.target.value !== '') applyRecordPreset(e.target.value); };
    $('#recEditPreset').onclick=editRecordPreset;
    $('#recColumnsBtn').onclick = (e) => { e.stopPropagation(); toggleRecordColumns(); };
    $$('#recColumnsPop input[data-rec-col]').forEach((input) => input.onchange = () => {
      S.recColumns[input.dataset.recCol] = input.checked;
      try { localStorage.setItem('tl.rec.columns', JSON.stringify(S.recColumns)); } catch (_) { /* storage unavailable */ }
      if (S.view === 'records') renderRecords();
    });
    document.addEventListener('click', (e) => {
      const pop = $('#recColumnsPop');
      if (pop && !pop.hidden && !pop.contains(e.target) && !e.target.closest('#recColumnsBtn')) pop.hidden = true;
    });
    // 明细页的「按日期精修」：主导航在右上角（rangeSeg），这里只收窄具体日期。
    if ($('#recFrom')) $('#recFrom').onchange = (e) => applyRecDays(e.target.value, S.recTo);
    if ($('#recTo')) $('#recTo').onchange = (e) => applyRecDays(S.recFrom, e.target.value);
    if ($('#recDayClear')) $('#recDayClear').onclick = () => applyRecDays('', '');
    if ($('#ledgerSeg')) {
      $('#ledgerSeg').onclick = (e) => {
        const b = e.target.closest('button[data-cur]');
        if (!b) return;
        applyDisplayCurrency(b.dataset.cur === 'cny' ? 'cny' : 'usd');
      };
    }

    /* 搜索 */
    let st = 0;
    $('#searchInput').addEventListener('input', (e) => {
      clearTimeout(st);
      const v = e.target.value;
      st = setTimeout(() => doSearch(v), 180);
    });

    /* 导出下拉 */
    const exBtn = $('#exportBtn'), exMenu = $('#exportMenu');
    exBtn.onclick = (e) => {
      e.stopPropagation();
      const on = exMenu.classList.toggle('on');
      exBtn.classList.toggle('open', on);
    };
    $$('#exportMenu button').forEach((b) => b.onclick = async () => {
      exMenu.classList.remove('on'); exBtn.classList.remove('open');
      const fmt = b.dataset.fmt;
      if (fmt === 'clip') return copySummary();
      const q = exportQuery(fmt);
      if (fmt === 'md') q.set('lang', S.language || 'zh-CN');
      if(window.TLNative) {
        try { const saved=await window.TLNative.download('/api/export-stream?'+q.toString());if(saved)toast(isEn()?'Export saved':'导出已保存','ok'); }
        catch(error){toast(error.message,'err');}
        return;
      }
      window.location.href = '/api/export-stream?' + q.toString();
      toast(fmt === 'csv' ? '正在导出 CSV…'
        : fmt === 'md' ? '正在导出 Markdown 报告…' : '正在导出 JSON…', 'info');
      setTimeout(() => switchView(S.view), 700);
    });
    document.addEventListener('click', () => {
      exMenu.classList.remove('on'); exBtn.classList.remove('open');
    });

    /* 抽屉与帮助 */
    $('#drawerClose').onclick = closeDrawer;
    $('#drawerMask').onclick = closeDrawer;
    $('#helpClose').onclick = () => $('#helpMask').classList.remove('on');
    $('#helpMask').onclick = (e) => { if (e.target.id === 'helpMask') $('#helpMask').classList.remove('on'); };
    $('#aboutBtn').onclick = openAbout;
    $('#aboutClose').onclick = closeAbout;
    $('#aboutMask').onclick = (e) => { if (e.target.id === 'aboutMask') closeAbout(); };
    $('#methodBtn').onclick = openMethodology;
    $('#methodClose').onclick = closeMethodology;
    $('#methodMask').onclick = (e) => { if (e.target.id === 'methodMask') closeMethodology(); };
    $$('#iqTabs button').forEach((b) => b.onclick = () => iqSetTab(b.dataset.iqTab));
    // 检测按钮 = 启动/停止一体：运行中再点，走中断而不是再次启动。
    $$('.iq-run').forEach((b) => b.onclick = () => {
      const mode = b.dataset.iqMode;
      const running = IQ.anyRunning && IQ.anyRunning[mode];
      if (running && IQ.abort && IQ.abort[mode]) {
        IQ.abort[mode].abort();
        return;
      }
      runAiTest(mode);
    });
    // 题干是验收基准，不能像普通提示词一样做空白归一化；仅清理 SVG 提示词的模板空白。
    $$('.iq-pelican-prompt').forEach((n) => { n.textContent = normalizePromptWhitespace(n.textContent); });
    $$('#iqPresets .iq-preset').forEach((b) => b.onclick = () => iqPreset(b.dataset.model, b.dataset.url));
    $('#iqAvailabilityBtn').onclick = runIqAvailability;
    $('#iqRunAllBtn').onclick = runIqAll;
    $('#iqManualOpen').onclick=iqOpenManual;
    $('#iqManualClose').onclick=()=>iqCloseManual();$('#iqManualCancel').onclick=()=>iqCloseManual();
    $('#iqManualForm').onsubmit=event=>{event.preventDefault();iqCloseManual(true);};
    $('#iqManualDialog').addEventListener('cancel',event=>{event.preventDefault();iqCloseManual();});
    $('#iqProfileSelect').onchange = (e) => {
      if(window.TLCCSwitch)window.TLCCSwitch.clear();
      const id = e.target.value;
      const p = (PL.cfg && PL.cfg.profiles || []).find((x) => x.id === id);
      IQ.profileId = id;
      if (p) {
        $('#iqModel').value = p.model || '';
        $('#iqUrl').value = p.base_url || '';
        $('#iqKey').value = '';
      } else {
        try { $('#iqKey').value = sessionStorage.getItem(IQ_RUNTIME_KEY) || ''; } catch (_) { /* sessionStorage 不可用时保持空 */ }
      }
      iqInvalidateAvailability();
    };
    $('#iqHtmlOpen').onclick = () => { if (IQ.html) copyText(IQ.html, 'HTML 已复制'); };
    iqRestore();
    try { $('#iqKey').value = sessionStorage.getItem(IQ_RUNTIME_KEY) || ''; } catch (_) { /* 隐私模式可能禁用 sessionStorage */ }
    iqInvalidateAvailability();
    if(window.TLCCSwitch)window.TLCCSwitch.init({api,onChange:(connection)=>{
      const wasSelected=!!IQ.ccSwitchId;IQ.ccSwitchId=connection?connection.ccswitch_id:'';
      $('#iqModel').readOnly=!!connection && !connection.manual_model;$('#iqUrl').readOnly=!!connection;$('#iqKey').disabled=!!connection;
      $('#iqKey').placeholder=connection?'使用 CC Switch 中的密钥（不回传到页面）':'仅本次检测使用';
      if(connection){
        IQ.profileId='';$('#iqProfileSelect').value='';$('#iqModel').value=connection.model;$('#iqUrl').value=connection.base_url;$('#iqKey').value='';
      }else if(wasSelected){$('#iqModel').value='';$('#iqUrl').value='';$('#iqKey').value='';}
      iqInvalidateAvailability();if(connection)iqSave();
    }});
    $$('#plMode button').forEach((b) => b.onclick = () => {
      PL.mode = b.dataset.promptMode === 'structured' ? 'structured' : 'polish';
      localStorage.setItem('tl.prompt.mode', PL.mode);
      $$('#plMode button').forEach((x) => x.classList.toggle('active', x === b));
      PL.last = PL.lastByMode[PL.mode] || null;
      renderOptimizeResult(PL.last);
      $('#plRunTip').textContent = PL.cfg && PL.cfg.configured
        ? (PL.mode === 'structured' ? '将补充角色、目标、内容范围、约束与输出要求' : '将改善措辞、语序与逻辑衔接，保持原始意图')
        : '请先完成 AI 接入配置';
    });
    $$('.pl-provider').forEach((b) => {
      b.onclick = () => applyPromptProviderPreset(b.dataset.provider);
    });
    $('#plProfileSelect').onchange = async (e) => {
      const id = e.target.value;
      if (!id) {
        // 「新建配置」：清空表单准备录入新的一条，已保存的配置原样保留。
        PL.draftNew = true;
        $('#plModel').value = ''; $('#plUrl').value = ''; $('#plKey').value = '';
        $('#plProfileName').value = '';
        if ($('#plProfileDelete')) $('#plProfileDelete').disabled = true;
        $('#plSaveBtn').disabled = true;
        if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = true;
        $('#plResult').innerHTML = '';
        renderProfileHint();
        updateEndpointPreview();
        return;
      }
      try {
        const res = await api('/api/prompt/select', { method: 'POST', body: { profile_id: id } });
        PL.cfg = res.config;
        PL.draftNew = false;
        $('#plModel').value = PL.cfg.model || ''; $('#plUrl').value = PL.cfg.base_url || ''; $('#plKey').value = '';
        $('#plSaveBtn').disabled = true;
        if ($('#plSaveNewBtn')) $('#plSaveNewBtn').disabled = true;
        $('#plResult').innerHTML = '';
        renderPromptProfiles(); renderPrompt(); renderProfileHint();
      } catch (e2) { toast(e2.message || '切换配置失败', 'err'); }
    };
    $('#plProfileNew').onclick = () => {
      const sel = $('#plProfileSelect');
      if (!sel) return;
      sel.value = '';
      sel.dispatchEvent(new Event('change'));
      requestAnimationFrame(() => $('#plProfileName')?.focus());
    };
    $('#plProfileDelete').onclick = async () => {
      const id = $('#plProfileSelect').value;
      if (!id || !(await confirmDialog('确定删除当前 AI 配置吗？', { danger: true }))) return;
      try {
        const res = await api('/api/prompt/delete', { method: 'POST', body: { profile_id: id } });
        PL.cfg = res.config;
        $('#plModel').value = PL.cfg.model || ''; $('#plUrl').value = PL.cfg.base_url || ''; $('#plKey').value = '';
        renderPromptProfiles(); renderPrompt(); toast('AI 配置已删除', 'ok');
      } catch (e2) { toast(e2.message || '删除配置失败', 'err'); }
    };
    $('#plHistoryBtn').onclick = openPromptHistory;
    $('#plHistoryClose').onclick = closePromptHistory;
    $('#plHistoryMask').onclick = (e) => { if (e.target.id === 'plHistoryMask') closePromptHistory(); };
    $('#plBasisClose').onclick = closePromptBasis;
    $('#plBasisMask').onclick = (e) => { if (e.target.id === 'plBasisMask') closePromptBasis(); };
    $('#plHistoryClear').onclick = async () => {
      if (!readPromptHistory().length) return;
      if (!(await confirmDialog('确定清空历史提示词吗？', { danger: true }))) return;
      localStorage.removeItem(PL_HIST); renderPromptHistory(); toast('历史提示词已清空', 'ok');
    };
    $('#plConfigClose').onclick = closePromptConfig;
    $('#plConfigMask').onclick = (e) => { if (e.target.id === 'plConfigMask') closePromptConfig(); };

    /* API Key 输入框：初始 readonly 挡掉浏览器/Edge 的"保存密码"弹窗（它会跳过 readonly 字段），
       聚焦时才解除，用户照常输入。 */
    $$('#plKey, #iqKey').forEach((n) => {
      n.addEventListener('focus', () => { n.readOnly = false; });
      n.addEventListener('blur', () => { if (!n.value) n.readOnly = true; });
    });
    // AI 智商检测 Key 仅保存在当前会话，方便连续做三题；不写入持久化配置。
    $('#iqKey').addEventListener('input', () => {
      try {
        const value = $('#iqKey').value;
        if (value) sessionStorage.setItem(IQ_RUNTIME_KEY, value);
        else sessionStorage.removeItem(IQ_RUNTIME_KEY);
      } catch (_) { /* 浏览器隐私模式可能禁用 sessionStorage */ }
      iqInvalidateAvailability();
    });
    // 只要三个字段齐了就能开始检测，不必先跑一次可用性自检。
    ['#iqModel', '#iqUrl'].forEach((id) => $(id).addEventListener('input', iqInvalidateAvailability));

    /* 设置 */
    $('#addRootBtn').onclick = async () => {
      const v = $('#rootInput').value.trim();
      if (!v) return toast('请输入目录路径', 'err');
      const c = (S.data && S.data.config) || {};
      const roots = (cfgValue(c, 'api_log_roots', []) || []).concat([v]);
      await api('/api/config', { method: 'POST', body: { api_log_roots: roots } });
      rememberCfg('api_log_roots', roots);
      $('#rootInput').value = '';
      toast('已添加，正在重新扫描该目录', 'ok');
      clearDashboardCache();
      await load(true, true);
      startScan(true);
    };
    $('#resetBtn').onclick = async () => {
      if (!(await confirmDialog('确定清空本地索引吗？原始日志不会被删除，重新扫描即可恢复。', { danger: true }))) return;
      await api('/api/reset', { method: 'POST', body: {} });
      toast('索引已清空', 'ok');
      startScan(true);
    };
    // 程序退出由桌面窗口 / 控制台负责；界面只保留可逆的刷新、主题与关于入口。

    /* 提示词优化 */
    $('#plConfigForm').onsubmit = (e) => { e.preventDefault(); runCheck(); };
    $('#plTestBtn').onclick = runCheck;
    $('#plSaveBtn').onclick = async () => {
      await savePromptProfile(false);
    };
    $('#plSaveNewBtn').onclick = async () => { await savePromptProfile(true); };
    $('#plClearBtn').onclick = async () => {
      if (!(await confirmDialog('确定清除已保存的 AI 接入配置吗？\n（API Key 会一并删除，不影响 token 用量数据）', { danger: true }))) return;
      await api('/api/prompt/clear', { method: 'POST', body: {} });
      PL.cfg = await loadPromptCfg();
      invalidateViews();
      $('#plModel').value = ''; $('#plUrl').value = ''; $('#plKey').value = '';
      $('#plSaveBtn').disabled = true;
      $('#plResult').innerHTML = '';
      renderPrompt();
      toast('已清除接入配置', 'ok');
    };
    ['#plModel', '#plUrl', '#plKey'].forEach((s) => $(s).addEventListener('input', () => {
      updateEndpointPreview();
      $('#plSaveBtn').disabled = true;
    }));
    $('#plInput').addEventListener('input', (e) => {
      $('#plCount').textContent = e.target.value.length;
    });
    $('#plSampleBtn').onclick = () => {
      $('#plInput').value = PL_SAMPLE;
      $('#plCount').textContent = PL_SAMPLE.length;
      $('#plInput').focus();
    };
    $('#plRunBtn').onclick = runOptimize;

    let rt;
    addEventListener('resize', () => {
      clearTimeout(rt);
      rt = setTimeout(() => {
        // Only width-dependent charts need rebuilding. Resizing a records or
        // management view must not issue another API request.
        if (S.data && S.view === 'overview') renderView('overview', true);
        else delete S.rendered.overview;
        if (bar1 && bar1._sync) bar1._sync(false);
      }, 180);
    });
    addEventListener('popstate',event=>{
      if(event.state?.tl)restoreNavigation(event.state.tl);
    });
    document.addEventListener('tl-desktop-navigate',event=>{if(TITLES[event.detail])switchView(event.detail);});
    addEventListener('hashchange', () => {
      const v = location.hash.slice(1);
      if (v && TITLES[v] && v !== S.view) switchView(v,{replace:true});
    });
    let scrollRaf = 0;
    $('#scroll').addEventListener('scroll', () => {
      if (scrollRaf) return;
      scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; hideTip(); });
    }, { passive: true });
    const bar1 = $('#rangeSeg');
    const bar2 = $('#trendSeg');
    initSegPill('#rangeSeg');
    initSegPill('#trendSeg');
    initRipple();
    initContextMenuGuard();
    initKeys();
  }

  /* ---------------- 启动 ---------------- */
  // 自定义区间（形如 2026-01-01~2026-03-31）没有对应的预设按钮，
  // 所以要把它的标签画到「自定义」那颗按钮上，否则用户看不出当前选的是什么。
  function syncRangeButtons() {
    const isCustom = typeof S.range === 'string' && S.range.indexOf('~') > 0;
    $$('#rangeSeg button').forEach((b) => {
      if (b.dataset.range === 'custom') {
        b.classList.toggle('active', isCustom);
        b.innerHTML = isCustom
          ? `<i class="ico i-clock"></i>${S.range.split('~')[0].slice(5)}~${S.range.split('~')[1].slice(5)}`
          : '<i class="ico i-clock"></i>自定义';
        return;
      }
      b.classList.toggle('active', !isCustom && b.dataset.range === S.range);
    });
  }

  async function main() {
    if(window.TLLSources)window.TLLSources.init({api,toast,isEn,refresh:async()=>{
      clearDashboardCache();invalidateViews();await load(true,true);
    }});
    if(window.TLAISettings)window.TLAISettings.init({api,toast,onChange:async()=>{
      await loadPromptCfg();renderPrompt();if(window.TLAssistant)await window.TLAssistant.refresh();
    }});
    if(window.TLAssistant)window.TLAssistant.init({api,apiStream,toast,language:S.language,contextChanged:filter=>{
      const rangeChanged=filter.range && filter.range!==S.range;
      S.analysisContext=filter;if(rangeChanged){saveCurrentNavigation();S.range=filter.range;localStorage.setItem('tl.range',S.range);syncRangeButtons();load();}
      renderNavigationContext();
    },pricesChanged:async()=>{clearDashboardCache();invalidateViews();await load(true,true);},navigate:(target,filter)=>{
      if(target==='session')openSession(filter.session,filter.tool,filter.project,filter);
      else navigateWithContext(target,filter);
    },drill:filter=>{
      if(filter.session){openSession(filter.session,filter.tool,filter.project,filter);return;}
      navigateWithContext('records',{...filter,query:filter.q || filter.query || '',project_paths:filter.project_paths || '',from:filter.from || '',to:filter.to || ''});
    }});
    if (window.TLManage) window.TLManage.init({api, toast,pricesChanged:async()=>{clearDashboardCache();invalidateViews();await load(true,true);},
      contextChanged:()=>{renderNavigationContext();saveCurrentNavigation();},
      priceLookup:filter=>{navigateWithContext('assistant',typeof filter==='string'?{model:filter}:filter);window.TLAssistant?.openPrices?.(typeof filter==='string'?{model:filter}:filter);},
      drill:filter=>navigateWithContext('records',{...filter,query:'',from:filter.date || '',to:filter.date || ''})});
    document.addEventListener('tl-service-state',event=>{
      const value=event.detail||{};let node=$('#localServiceStatus');
      if(!node){node=el('span','service-status');node.id='localServiceStatus';node.setAttribute('role','status');$('.tb-actions').prepend(node);}
      node.hidden=value.state==='ready';node.textContent=value.message||'';node.classList.toggle('error',value.state==='unavailable');
      if(value.state==='ready'&&value.generation>1){clearDashboardCache();invalidateViews();load(true,true);}
    });
    document.addEventListener('tl-native-error', event => toast(String(event.detail?.message || event.detail || '操作失败，请重试'), 'err'));
    window.addEventListener('unhandledrejection', (event) => {
      event.preventDefault(); toast((event.reason && event.reason.message) || '操作失败，请重试', 'err');
    });
    initConfirmDialog();
    $('#methodBody').innerHTML = methodologyHTML();
    document.documentElement.classList.add('perf-mode');
    bind();
    bindAutoSettings();
    initFpsMeter();
    const qs = new URLSearchParams(location.search);
    // Apply preferences and make independent pages usable before aggregating a large index.
    const bootP = api('/api/bootstrap').then((b) => { S.boot = b; })
      .catch(() => { S.boot = { config: {} }; });
    await bootP;
    if (!localStorage.getItem('tl.range')) S.range = (S.boot.config || {}).default_range || '30d';
    const theme = qs.get('theme') || (S.boot.config || {}).theme || 'dark';
    S.forceTheme = qs.get('theme') === 'light' ? 'light' : (qs.get('theme') === 'dark' ? 'dark' : null);
    applyTheme(theme, false, false);
    applyFont((S.boot.config || {}).font || 'system', false);
    applyDensity((S.boot.config || {}).density || 'compact', false);
    applyAccent((S.boot.config || {}).accent || 'theme', false);
    applyRadius((S.boot.config || {}).radius || 'theme', false);
    applyLanguage((S.boot.config || {}).language || S.language || 'zh-CN', false);
    // 纯前端偏好（localStorage）：在首帧前应用，避免闪一下再变
    applyBoolPref('motion', readBoolPref('motion', true));
    initFpsPreference();
    watchSystemTheme();
    syncRangeButtons();
    const hashView = location.hash.slice(1);
    if (hashView && TITLES[hashView]) S.view = hashView;
    // 首屏就要按配置隐藏入口（hash 直达被关掉的视图时 switchView 会兜底回总览）
    applyFeatureToggles();
    uiReady = true;
    switchView(S.view,{replace:true});
    if (!S.data) showSkeleton();
    // The shared loader owns cancellation and stale-response checks. Navigation
    // and settings stay available while statistics are calculated in the background.
    load();

    loadPromptCfg().then(() => { if (S.view === 'prompt') renderPrompt(); });
    // 首帧布局稳定后只重描图表（避免重播 KPI 计数动画）
    // 首帧布局稳定后按容器真实宽度重描一次图表即可；字体就绪后再补一次。
    // 原来连着跑三次（140/460/fonts.ready）会造成三次全量 SVG 重建。
    // System fonts and SVG viewBoxes do not need a second startup render.
    // Resize handling above redraws width-dependent charts when necessary.
    scheduleAutoScan((S.boot.config || {}).auto_scan_minutes);
    if ((!S.boot || !S.boot.span || !S.boot.span.n) && (S.boot.config || {}).scan_on_start !== false) {
      // 首次运行：自动扫描
      startScan(true);
    } else {
      const s = await api('/api/scan/status');
      if (s.running) pollScan();
      else if (!S.data) $('#scanText').textContent =
        `${S.boot.span?.n ? fmtExact(S.boot.span.n) + (isEn() ? ' records' : ' 条记录') : (isEn() ? 'Ready' : '就绪')}${S.boot.last_scan ? ' · ' + dd(S.boot.last_scan) : ''}`;
    }
  }

  document.addEventListener('DOMContentLoaded', main);
})();

