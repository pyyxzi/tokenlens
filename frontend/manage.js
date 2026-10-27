/* Local cost management. User content is escaped and never sent to a model. */
(() => {
  'use strict';
  let api, toast, drill, pricesChanged, openPrices, data, range = '30d', english = false, tab = 'projects', request = 0;
  let preview = null, editedProject = null, basis = 'total_cost';
  let projectDialog = null;
  let contextChanged;
  // Keep table pagination independent from forms and aggregate cards.
  const pageSizes = [20, 50, 100];
  const tableStates = Object.create(null), tableViews = Object.create(null);
  let internalOffset = 0, internalLimit = 20, internalScrollPending = false;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const t = (zh, en) => english ? en : zh;
  const money = (n) => {
    const full = window.TLMoney?.format(n) ?? ('$' + Number(n || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:6}));
    const compact = window.TLMoney?.format(n, {precision:2});
    return compact && /[1-9]/.test(compact) ? compact : full;
  };
  const price = (n) => n == null ? '—' : '$' + Number(n).toLocaleString('en-US',{maximumFractionDigits:6});
  const costLabel = kind => window.TLMoney?.label(kind,english) || ({actual:t('实付','Actual'),estimated:t('估算','Estimated'),free:t('免费','Free'),unknown:t('未定价','Unpriced')})[kind];
  const num = (n) => Number(n || 0).toLocaleString(english ? 'en-US' : 'zh-CN');
  const time = (n) => n ? new Date(n * 1000).toLocaleString(english ? 'en-US' : 'zh-CN') : '—';
  const empty = (text) => `<p class="manage-empty">${esc(text)}</p>`;
  const button = (action, label, index = '', cls = 'btn btn-ghost btn-xs') => `<button type="button" class="${cls}" data-action="${action}" data-index="${esc(index)}">${label}</button>`;
  function table(heads, rows, numeric = []) {
    return `<div class="table-wrap"><table class="tbl manage-table"><thead><tr>${heads.map((h,i) => `<th scope="col"${numeric.includes(i) ? ' class="manage-num-head"' : ''}>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  }
  const input = (id, label, value = '', type = 'text', extra = '') => `<label class="field"><span>${label}</span><input class="input" id="${id}" type="${type}" value="${esc(value)}" ${extra}></label>`;
  const field = (id) => ($(id).value || '').trim();
  const today = () => { const d = new Date(); return [d.getFullYear(), String(d.getMonth()+1).padStart(2,'0'), String(d.getDate()).padStart(2,'0')].join('-'); };
  const pageButton = (key, page, label, disabled = false, current = false) =>
    `<button type="button" class="btn ${current ? 'btn-primary' : 'btn-ghost'} btn-xs" data-manage-key="${key}" data-manage-page="${page}" ${disabled ? 'disabled' : ''}${current ? ' aria-current="page"' : ''}>${label}</button>`;
  function pageNumbers(key, cur, pages) {
    const marks = new Set([1, pages, cur]);
    if (pages <= 9) { for (let i = 1; i <= pages; i++) marks.add(i); }
    else { for (let d = 1; d <= 2; d++) { marks.add(cur - d); marks.add(cur + d); } }
    const ordered = [...marks].filter((p) => p >= 1 && p <= pages).sort((a, b) => a - b);
    const out = [];
    let last = 0;
    for (const page of ordered) {
      if (page - last > 1) out.push('<span class="manage-pager-gap">…</span>');
      out.push(pageButton(key, page, String(page), page === cur, page === cur));
      last = page;
    }
    return out.join('');
  }

  function pagerHTML(key, total, page, size, shown) {
    if (!total) return '';
    const pages = Math.max(1, Math.ceil(total / size));
    const start = (page - 1) * size + 1, end = start + shown - 1;
    return `<nav class="manage-pager" aria-label="${t('表格分页','Table pagination')}"><span class="manage-pager-info">${t(`第 ${num(start)}–${num(end)} 条，共 ${num(total)} 条`, `Showing ${num(start)}–${num(end)} of ${num(total)}`)}</span><label class="manage-pager-size">${t('每页','Rows per page')}<select class="input sm" data-manage-key="${key}" data-manage-size>${pageSizes.map(n=>`<option value="${n}" ${size===n?'selected':''}>${n}</option>`).join('')}</select></label>${pages > 1 ? `${pageButton(key, page - 1, t('‹ 上一页','‹ Previous'), page <= 1)}<span class="manage-pager-pages">${pageNumbers(key, page, pages)}</span>${pageButton(key, page + 1, t('下一页 ›','Next ›'), page >= pages)}<form class="manage-pager-jump" data-manage-key="${key}" data-manage-jump><label>${t('跳至','Page')}<input class="input sm" type="number" min="1" max="${pages}" step="1" value="${page}" aria-label="${t('跳转页码','Page number')}"></label><button type="submit" class="btn btn-ghost btn-xs">${t('确定','Go')}</button></form>` : ''}<span>${t(`第 ${page} / ${pages} 页`, `Page ${page} of ${pages}`)}</span></nav>`;
  }

  function pagedContent(key) {
    const view = tableViews[key], state = tableStates[key];
    state.page = Math.min(Math.max(1, state.page), Math.max(1, Math.ceil(view.rows.length / state.size)));
    const rows = view.rows.slice((state.page - 1) * state.size, state.page * state.size);
    const html = table(view.heads, rows, view.numeric);
    return (key === 'projects' ? html.replace('tbl manage-table', 'tbl manage-table data-table manage-project-table').replace('<thead>', '<colgroup><col style="width:32%"><col style="width:16%"><col style="width:16%"><col style="width:22%"><col style="width:14%"></colgroup><thead>') : html) + pagerHTML(key, view.rows.length, state.page, state.size, rows.length);
  }
  function pagedTable(key, heads, rows, numeric = []) {
    tableStates[key] ||= {page:1, size:20};
    tableViews[key] = {heads, rows, numeric};
    return `<section class="manage-table-section" data-manage-table="${key}">${pagedContent(key)}</section>`;
  }
  function updateTable(key) {
    const container = document.querySelector(`[data-manage-table="${key}"]`);
    if (!container) return;
    const focus = container.contains(document.activeElement);
    container.innerHTML = pagedContent(key);
    bindActions(container); bindPagination(container);
    window.TLResponsive?.apply(container);
    window.TLMotion?.animate(container);
    if (focus) container.querySelector('[data-manage-size]')?.focus({preventScroll:true});
    container.scrollIntoView({block:'start', behavior:'instant'});
  }
  function bindPagination(root) {
    const go = (key, value) => {
      const state = key === 'internal' ? {page:1, size:internalLimit} : tableStates[key];
      const total = key === 'internal' ? data.internal.records : tableViews[key]?.rows.length;
      if (!state || !Number.isFinite(value)) return;
      state.page = Math.min(Math.max(1, Math.floor(value)), Math.max(1, Math.ceil(total / state.size)));
      if (key === 'internal') {internalOffset=(state.page - 1) * internalLimit; internalScrollPending=true; refresh();}
      else updateTable(key);
      contextChanged?.();
    };
    root.querySelectorAll('[data-manage-page]').forEach(b=>b.onclick=()=>go(b.dataset.manageKey, Number(b.dataset.managePage)));
    root.querySelectorAll('[data-manage-size]').forEach(s=>s.onchange=()=>{
      const size=Number(s.value), key=s.dataset.manageKey;
      if (!pageSizes.includes(size)) return;
      if (key === 'internal') {internalLimit=size; internalOffset=0; internalScrollPending=true; refresh();}
      else {tableStates[key]={page:1,size}; updateTable(key);}
      contextChanged?.();
    });
    root.querySelectorAll('[data-manage-jump]').forEach(form=>form.onsubmit=e=>{
      e.preventDefault(); if (form.reportValidity()) go(form.dataset.manageKey, Number(form.querySelector('input').value));
    });
  }

  async function act(body) {
    const result = await api('/api/manage', {method:'POST', body});
    if (result.ok === false) throw new Error(result.error || t('操作失败', 'Action failed'));
    if (['price_save','price_delete'].includes(body.action)) await pricesChanged?.();
    return result;
  }
  async function refresh() { return render(range, english); }

  function fillProject(id) {
    const project=(data.projects || []).find(row=>row.id===id);
    if(!project)return;
    editedProject=project.id;
    $('#manageProject').value=project.id;
    $('#manageProjectName').value=project.name || '';
    $('#manageProjectTags').value=(project.tags || []).join(', ');
    $('#manageProjectBudget').value=project.budget || 0;
    $('#manageProjectPaths').value=(project.paths || []).join('\n');
  }
  function closeProjectEditor(redraw=true) {
    if(!projectDialog)return;
    const dialog=projectDialog;projectDialog=null;
    if(dialog.open)dialog.close();
    dialog.remove();
    if(redraw)draw();
  }
  function openProjectEditor(index) {
    const project=(data.projects || [])[index];
    if(!project)throw new Error(t('项目已变更，请刷新后重试','Project changed. Refresh and retry.'));
    closeProjectEditor(false);
    editedProject=project.id;draw();
    const form=$('#manageProjectForm');
    const dialog=document.createElement('dialog');
    dialog.className='manage-project-dialog';
    dialog.setAttribute('aria-labelledby','manageEditorTitle');
    dialog.innerHTML=`<header><div><h3 id="manageEditorTitle">${t('编辑项目','Edit project')}</h3><p>${esc(project.name)}</p></div><button type="button" class="icon-btn" aria-label="${t('关闭','Close')}">×</button></header><div id="manageEditorStatus" role="status" aria-live="polite"></div>`;
    dialog.append(form);document.body.append(dialog);projectDialog=dialog;
    dialog.querySelector('header button').onclick=()=>closeProjectEditor();
    dialog.addEventListener('cancel',event=>{event.preventDefault();closeProjectEditor();});
    dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeProjectEditor();}});
    dialog.showModal();
    $('#manageProjectName').focus({preventScroll:true});
    $('#manageProjectName').select();
  }

  function projectContent() {
    const rows = data.projects || [];
    const project = rows.find(r => r.id === editedProject) || rows[0];
    const defaultPaths = project ? project.paths.join('\n') : '';
    return `<div class="manage-intro"><div><h3>${t('让每一笔成本归属到项目','Give every cost a project')}</h3><p>${t('使用完整路径识别项目；月预算独立于右上角查看范围。','Full paths identify projects. Monthly budgets are independent of the viewing range.')}</p></div></div>` +
      (rows.length ? pagedTable('projects',[t('项目 / 路径','Project / paths'),t('Token / 调用','Tokens / calls'),t('范围内采集金额','Saved cost in range'),t('本月 / 月预算','This month / budget'),t('操作','Actions')], rows.map((r,i) => `<tr><td><span class="data-identity" title="${esc([r.name,...r.paths].join('\n'))}"><b>${esc(r.name)}</b><small class="manage-project-paths">${r.paths.map(esc).join(' · ')}</small></span><small>${r.tags.map(esc).join(' · ')}</small></td><td class="num"><b>${num(r.tokens)}</b><small>${num(r.requests)} ${t('次调用','calls')}</small></td><td class="num">${money(r.cost)}</td><td class="num">${money(r.period_cost)}<small class="${r.budget && r.ratio > 1 ? 'manage-danger' : ''}">${r.budget ? `${t('预算','Budget')} ${money(r.budget)} · ${(r.ratio*100).toFixed(1)}%` : t('未设置预算','No budget set')}</small></td><td><div class="manage-project-actions">${button('edit-project',t('编辑','Edit'),i)}${button('project-records',t('明细','Records'),i)}</div></td></tr>`), [1, 2, 3]) : empty(t('扫描数据后，项目会自动出现在这里。','Projects appear here after scanning.'))) +
      `<form id="manageProjectForm" class="manage-form"><h4>${t('项目名称、标签与预算','Project name, tags and budget')}</h4><label class="field"><span>${t('选择项目','Project')}</span><select class="input" id="manageProject">${rows.map(r=>`<option value="${esc(r.id)}" ${project && project.id===r.id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}</select></label>${input('manageProjectName',t('显示名称','Display name'),project ? project.name : '', 'text', 'maxlength="200"')}${input('manageProjectTags',t('标签（逗号分隔）','Tags (comma separated)'),project ? project.tags.join(', ') : '')}${input('manageProjectBudget',t('月预算（USD，0为不限）','Monthly budget (USD, 0 = no limit)'),project ? project.budget : 0,'number','min="0" step="0.01"')}<label class="field manage-wide"><span>${t('关联路径（每行一个；留空保留当前路径）','Associated paths (one per line; blank keeps existing paths)')}</span><textarea class="input" id="manageProjectPaths" rows="3">${esc(defaultPaths)}</textarea></label><div class="manage-wide">${button('save-project',t('保存项目','Save project'),'','btn btn-primary')}</div></form>`;
  }

  function qualityContent() {
    const q = data.quality;
    const labels = {exact:t('逐请求','Per request'),aggregate:t('汇总','Aggregated'),heuristic:t('近似提取','Heuristic'),detection:t('仅检测','Detection only')};
    return `<div class="manage-intro"><div><h3>${t('知道数字覆盖了什么','Know what your numbers cover')}</h3><p>${t('错误来源保留最近成功快照；近似和汇总数据会明确标注。','Failed sources keep the last successful snapshot; approximate and aggregate data are labelled.')}</p></div><span class="manage-note">${t('最近扫描','Last scan')} ${time(q.last_scan)}</span></div>` +
      (q.sources.length ? pagedTable('quality-sources',[t('来源','Source'),t('精度 / 状态','Quality / status'),t('接受 / 原始 / 跳过','Accepted / raw / skipped'),t('最近成功','Last success'),t('采集说明','Collection notes')], q.sources.map(r=>`<tr><td><b>${esc(english ? r.name_en || r.name : r.name)}</b></td><td>${esc(labels[r.quality] || r.quality || '—')}<small>${esc(r.status)}</small></td><td class="num">${num(r.accepted_records)} / ${num(r.raw_records)} / ${num(r.skipped_records)}</td><td>${time(r.last_success_at)}</td><td class="manage-wrap">${esc(r.error || r.note || '—')}</td></tr>`), [2]) : empty(t('尚无采集报告。请先扫描本地数据。','No collection reports yet. Scan local data first.'))) +
      `<h4>${t('待定价模型','Models awaiting prices')}</h4>` + (q.unknown_models.length ? pagedTable('quality-models',[t('模型','Model'),'Token',t('记录','Records'),t('操作','Actions')],q.unknown_models.map((r,i)=>`<tr><td>${esc(r.model || t('未知模型','Unknown model'))}</td><td class="num">${num(r.tokens)}</td><td class="num">${num(r.records)}</td><td>${button('unknown-records',t('查看明细','View records'),i)} ${r.model&&r.model!=='未知模型'?button('fill-model-price',t('查询并预览补价','Find and review prices'),i):''}</td></tr>`), [1, 2]) : empty(t('当前范围没有未知成本记录。','No unknown-cost records in this range.')));
  }

  function pricesContent() {
    return `<div class="manage-intro"><div><h3>${t('保留历史，解释每一次调价','Keep history and explain every price change')}</h3><p>${t('单价单位为 USD / 百万 Token。新采集按调用日期应用已生效版本；已有金额、实付和免费记录保持原值。','Rates are USD per million tokens. New records use the version effective on the call date; saved, actual and free costs are retained.')}</p></div></div>` +
      `<form id="managePriceForm" class="manage-form">${input('managePriceModel',t('模型（完整名称）','Model (exact name)'),'','text','required maxlength="200"')}${input('managePriceDate',t('生效日期','Effective date'),today(),'date','required')}${input('managePriceSource',t('价格来源 / 备注','Price source / note'),'','text','maxlength="500"')}${['输入','输出','缓存读','缓存写'].map((label,i)=>input('managePrice'+i,t(label,['Input','Output','Cache read','Cache write'][i]),0,'number','min="0" step="any" required')).join('')}<div class="manage-wide">${button('save-price',t('保存价格版本','Save price version'),'','btn btn-primary')}</div></form>` +
      (data.prices.length ? pagedTable('prices',[t('模型','Model'),t('生效日期','Effective'),t('输入 / 输出 / 缓存读 / 写','Input / output / cache read / write'),t('来源','Source'),t('操作','Actions')],data.prices.map((r,i)=>`<tr><td>${esc(r.model)}</td><td>${esc(r.effective_from)}<small><code>${esc(r.id)}</code></small></td><td class="num">${r.prices.map(price).join(' / ')}</td><td>${esc(r.source)}</td><td>${button('delete-price',t('删除版本','Delete version'),i)}</td></tr>`), [2]) : empty(t('尚无自定义价格版本。未覆盖的记录使用采集时保存的金额。','No price versions yet. Uncovered records use their saved cost.'))) +
      `<details class="manage-audit"><summary>${t('价格变更记录','Price change history')}</summary>${pagedTable('price-audit',[t('时间','Time'),t('操作','Action'),t('变更后','After')], (data.price_audit || []).map(r=>`<tr><td>${time(r.created_at)}</td><td>${esc(r.action)}</td><td class="manage-wrap"><code>${esc(r.after_json)}</code></td></tr>`))}</details>`;
  }

  function previewHTML(report) {
    if (!report) return '';
    return `<div class="manage-preview"><h4>${t('对账结果','Reconciliation results')} · ${esc(report.name || '')}</h4><p>${t('账单','Billed')} ${money(report.totals.billed)} · ${t('记录','Recorded')} ${money(report.totals.recorded)} · ${t('差额','Difference')} <b>${money(report.totals.difference)}</b></p>` +
      (report.errors || []).map(e=>`<p class="manage-danger">${t('行','Line')} ${e.line}: ${esc(e.error)}</p>`).join('') +
      pagedTable('bill-preview',[t('日期 / 模型','Date / model'),t('工具 / 项目','Tool / project'),t('账单','Billed'),t('记录','Recorded'),t('差额','Difference'),t('匹配','Matched')],report.rows.map((r,i)=>`<tr><td>${esc(r.date)}<small>${esc(r.model)}</small></td><td>${esc(r.tool || '—')}<small>${esc(r.project || '—')}</small></td><td class="num">${money(r.billed)}</td><td class="num">${money(r.recorded)}</td><td class="num ${Math.abs(r.difference)>0.000001 ? 'manage-danger' : ''}">${money(r.difference)}</td><td>${button('bill-records',`<span data-countup>${num(r.matched_records)}</span> ${t('条明细','records')}`,i)}${r.unknown_records ? `<small data-countup>${num(r.unknown_records)} ${t('条未定价','unpriced')}</small>` : ''}</td></tr>`), [2,3,4]) + '</div>';
  }

  function reconciliationContent() {
    return `<div class="manage-intro"><div><h3>${t('核对账单，不改写原始记录','Reconcile bills while keeping original records')}</h3><p>${t('按日期、模型及可选工具/项目匹配。预览通过后再保存，对账结果仅在本机保留。','Match by date, model and optional tool/project. Preview before saving locally.')}</p></div></div><form id="manageBillForm" class="manage-form">${input('manageBillName',t('账单名称','Bill name'),'','text','maxlength="200"')}<label class="field"><span>${t('选择 CSV（UTF-8，最多2MB）','CSV file (UTF-8, max 2MB)')}</span><input class="input" type="file" accept=".csv,text/csv" id="manageBillFile"></label><label class="field manage-wide"><span>${t('CSV内容：date,model,cost_usd；可选tool,project','CSV: date,model,cost_usd; optional tool,project')}</span><textarea class="input manage-code" id="manageBillCSV" rows="6" placeholder="date,model,cost_usd,tool&#10;2026-10-09,model-name,1.25,tool-name"></textarea></label><div class="manage-wide manage-actions">${button('bill-preview',t('预览差异','Preview differences'),'','btn btn-primary')}${button('bill-import',t('保存对账','Save reconciliation'))}${button('bill-template',t('下载模板','Download template'))}</div></form><div id="managePreview">${previewHTML(preview)}</div><h4>${t('已保存对账','Saved reconciliations')}</h4>` +
      (data.reconciliations.length ? pagedTable('bills',[t('账单','Bill'),t('保存时间','Saved'),t('行数','Rows'),t('账单 / 记录 / 差额','Billed / recorded / difference'),t('操作','Actions')],data.reconciliations.map((r,i)=>`<tr><td>${esc(r.name)}</td><td>${time(r.created_at)}</td><td class="num">${num(r.rows)}</td><td class="num">${money(r.billed)} / ${money(r.recorded)} / ${money(r.difference)}</td><td>${button('open-bill',t('查看','Open'),i)} ${button('delete-bill',t('删除','Delete'),i)}</td></tr>`), [2, 3]) : empty(t('尚无已保存对账。','No saved reconciliations.')));
  }

  function anomaliesContent() {
    return `<div class="manage-intro"><div><h3>${t('费用增长从哪里来','Where cost growth comes from')}</h3><p>${t('按所选范围与上一等长周期比较，以金额增量排序。不同维度可能描述同一批调用，不应相加。','Compared with the preceding equal-length period, ranked by cost increase. Dimensions can overlap and should not be added.')}</p></div></div>` +
      (data.anomalies.length ? pagedTable('anomalies',[t('维度 / 对象','Dimension / item'),t('本期','Current'),t('上期','Previous'),t('增量','Increase'),t('操作','Action')],data.anomalies.map((r,i)=>`<tr><td><b>${esc(r.title)}</b><small>${esc(r.kind)}</small></td><td class="num">${money(r.current_cost)}</td><td class="num">${money(r.previous_cost)}</td><td class="num manage-danger">+${money(r.delta)}</td><td>${button('anomaly-records',t('定位调用','Inspect calls'),i)}</td></tr>`), [1, 2, 3]) : empty(t('当前没有费用增长项；“全部时间”不进行环比。','No growth items. All-time ranges do not have a preceding comparison period.')));
  }

  function internalContent() {
    const d = data.internal;
    const total = Number(d.records || 0);
    const limit = Math.max(1, Number(d.limit || internalLimit));
    const offset = Math.max(0, Number(d.offset || 0));
    const pages = Math.max(1, Math.ceil(total / limit));
    const cur = Math.min(pages, Math.floor(offset / limit) + 1);
    const rows = d.recent || [];
    return `<div class="manage-intro"><div><h3>${t('本应用 AI 消费','This app’s AI usage')}</h3><p>${t('包含优化、测试及额外请求。只保存用量和运行元信息，不保存提示词、回答或密钥。上游未返回usage时标为未知。','Includes optimization, tests and additional requests. Stores usage and runtime metadata only, never prompts, responses or keys. Missing provider usage remains unknown.')}</p></div></div><div class="manage-stats"><article><span>${t('请求记录','Request records')}</span><b>${num(d.records)}</b></article><article><span>${t('费用 / Token','Cost / tokens')}</span><b>${money(d.cost)}</b><small>${num(d.tokens)} ${t('已知 Token','known tokens')} · ${num(d.usage_unknown)} ${t('条用量未知','usage unknown')}</small><small>${t('实付','Actual')} ${money(d.actual)} · ${t('估算','Estimated')} ${money(d.estimated)} · ${num(d.unknown)} ${t('条未计价','unpriced')}</small></article><article><span>${t('失败 / 取消 / 未计价','Failed / cancelled / unpriced')}</span><b>${num(d.failures)} / ${num(d.cancelled)} / ${num(d.unknown)}</b></article></div>` +
      `<p class="manage-note">${t('上方卡片汇总所选时间范围内的全部请求；下方表格按页展示。','The cards total every request in the selected range; the table shows one page.')}</p>` +
      (rows.length ? table([t('时间','Time'),t('模型','Model'),t('用途 / 状态','Purpose / status'),'Token',t('费用','Cost'),t('耗时','Duration')],rows.map(r=>`<tr><td>${time(r.ts)}</td><td>${esc(r.model)}</td><td>${esc(({primary:t('主请求','Primary'),planner:t('查询规划','Query planning')})[r.purpose]||r.purpose||'—')}<small>${esc(r.operation || '—')} · ${esc(({completed:t('完成','Completed'),failed:t('失败','Failed'),cancelled:t('已取消','Cancelled')})[r.status]||r.status||'—')}</small></td><td class="num">${r.usage_known===false?t('用量未知','Usage unknown'):num(r.tokens)}</td><td class="num">${r.cost_kind === 'unknown' ? t('未计价','Unpriced') : money(r.cost)}<small>${r.usage_known===false?t('服务商未返回用量','Provider usage unavailable'):costLabel(r.cost_kind)}</small></td><td class="num">${r.duration_ms == null ? '—' : num(Math.round(r.duration_ms))+' ms'}</td></tr>`), [3,4,5]) : empty(t('尚无本应用的 AI 请求。使用优化或模型测试后会显示在这里。','No AI calls from this app yet. Optimization and model tests will appear here.'))) +
      pagerHTML('internal', total, cur, limit, rows.length);
  }

  function draw() {
    const root = $('#manageRoot');
    if (!root || !data) return;
    const tabs = [['projects','项目成本','Projects'],['quality','数据质量','Data quality'],['prices','价格版本','Price versions'],['bills','账单对账','Reconcile'],['anomalies','异常定位','Cost changes'],['internal','自身 AI 消费','App AI usage']];
    const s = data.summary;
    const unknownCount = basis === 'repriced_cost' ? s.repriced_unknown_records : basis === 'historical_cost' ? s.historical_unknown_records : s.unknown_records;
    root.innerHTML = `<div class="manage-stats"><article><span>${t('成本视图','Cost view')+' ('+(window.TLMoney?.currency()||'USD')+')'}</span><b>${money(s[basis])}</b><select id="manageBasis" class="input sm" aria-label="${t('计价方式','Pricing basis')}">${[['total_cost','采集时保存金额','Saved cost'],['historical_cost','按调用日价格估算','Prices at call date'],['repriced_cost','按现价估算','Current prices']].map(r=>`<option value="${r[0]}" ${basis===r[0]?'selected':''}>${t(r[1],r[2])}</option>`).join('')}</select><small>${num(unknownCount)} ${t("\u6761\u672a\u5b9a\u4ef7", "unpriced records in this view")}</small></article><article><span>${t('所选口径：实付 / 估算','Selected basis: actual / estimated')}</span><b>${money(s.actual)} / ${money(basis==='total_cost'?s.estimated:Math.max(0,s[basis]-s.actual))}</b><small>${t('实付不参与重估','Actual charges are never repriced')}</small></article><article><span>${t('采集时金额覆盖率（Token）','Saved-cost coverage (tokens)')}</span><b>${(s.pricing_coverage*100).toFixed(1)}%</b><small>${num(s.unknown_records)} ${t('条未知','unknown records')} · ${num(s.free_records)} ${t('条免费','free records')}</small></article></div><nav class="manage-tabs" aria-label="${t('成本管理功能','Cost management sections')}">${tabs.map(r=>`<button class="${tab===r[0]?'active':''}" data-tab="${r[0]}" aria-pressed="${tab===r[0]}">${t(r[1],r[2])}</button>`).join('')}</nav><div class="card manage-body"><div id="manageStatus" role="status" aria-live="polite"></div>${({projects:projectContent,quality:qualityContent,prices:pricesContent,bills:reconciliationContent,anomalies:anomaliesContent,internal:internalContent}[tab])()}</div>`;
    root.querySelectorAll('form').forEach(f=>f.onsubmit=e=>e.preventDefault());
    root.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab; draw();contextChanged?.();});
    bindPagination(root);
    $('#manageBasis').onchange=(e)=>{basis=e.target.value; draw();contextChanged?.();};
    if ($('#manageProject')) $('#manageProject').onchange=(e)=>fillProject(e.target.value);
    if ($('#manageBillFile')) $('#manageBillFile').onchange=async(e)=>{
      const file=e.target.files[0]; if (!file) return;
      if (file.size>2_000_000) return toast(t('文件超过2MB','File exceeds 2MB'),'err');
      $('#manageBillCSV').value=await file.text(); $('#manageBillName').value=file.name;
    };
    bindActions(root);
    if(window.TLResponsive)window.TLResponsive.apply(root);
    if (window.TLMotion) window.TLMotion.animate(root);
    if (internalScrollPending) {
      internalScrollPending=false;
      if (tab==='internal') root.querySelector('.manage-body .table-wrap')?.scrollIntoView({block:'start',behavior:'instant'});
    }
  }

  function download(text, name) {
    if(window.TLNative){window.TLNative.save(name,text).catch(error=>{if(toast)toast(error.message,'err');});return;}
    const href=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));
    const a=document.createElement('a'); a.href=href; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(href),1000);
  }
  function bindActions(root) {
    root.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{
      const action=b.dataset.action, index=Number(b.dataset.index);
      b.disabled=true;
      try {
        if(action==='edit-project'){openProjectEditor(index); return;}
        if(action==='save-project'){
          await act({action:'project_save',project:field('#manageProject'),name:field('#manageProjectName') || field('#manageProject'),tags:field('#manageProjectTags').split(/[,，]/).filter(Boolean),budget:Number(field('#manageProjectBudget')),paths:field('#manageProjectPaths')?field('#manageProjectPaths').split(/\r?\n/).map(p=>p.trim()).filter(Boolean):undefined});
          closeProjectEditor(false);
        } else if(action==='project-records'){drill({project:data.projects[index].id,range}); return;}
        else if(action==='unknown-records'){drill({model:data.quality.unknown_models[index].model,range}); return;}
        else if(action==='fill-model-price'){openPrices?.({model:data.quality.unknown_models[index].model,range,cost_basis:basis}); return;}
        else if(action==='save-price'){
          await act({action:'price_save',model:field('#managePriceModel'),effective_from:field('#managePriceDate'),source:field('#managePriceSource'),prices:[0,1,2,3].map(i=>Number(field('#managePrice'+i)))});
        } else if(action==='delete-price'){
          if(!window.confirm(t('删除该价格版本？原始用量不受影响。','Delete this price version? Original usage is retained.'))) return;
          await act({action:'price_delete',id:data.prices[index].id});
        } else if(action==='bill-preview'||action==='bill-import'){
          preview=await act({action:action==='bill-preview'?'reconcile_preview':'reconcile_import',csv:field('#manageBillCSV'),name:field('#manageBillName')});
          if(tableStates['bill-preview'])tableStates['bill-preview'].page=1;
          $('#managePreview').innerHTML=previewHTML(preview); bindActions($('#managePreview')); bindPagination($('#managePreview'));
          if(window.TLResponsive)window.TLResponsive.apply($('#managePreview'));
          if (window.TLMotion) window.TLMotion.animate($('#managePreview'));
          if(action==='bill-import'){toast(t('对账已保存','Reconciliation saved'),'ok'); await refresh();}
          return;
        } else if(action==='bill-template'){download('date,model,cost_usd,tool,project\n','tokenlens-bill-template.csv');return;}
        else if(action==='open-bill'){preview=await act({action:'reconcile_detail',id:data.reconciliations[index].id});if(tableStates['bill-preview'])tableStates['bill-preview'].page=1;draw();return;}
        else if(action==='delete-bill'){
          if(!window.confirm(t('删除这份对账记录？','Delete this reconciliation?'))) return;
          await act({action:'reconcile_delete',id:data.reconciliations[index].id});
        } else if(action==='bill-records'){drill({...preview.rows[index],range:'all'});return;}
        else if(action==='anomaly-records'){const r=data.anomalies[index];drill({[r.kind]:r[r.kind],range:r.range});return;}
        toast(t('已保存','Saved'),'ok'); await refresh();
      } catch(err){toast(err.message || String(err),'err'); const status=$('#manageEditorStatus') || $('#manageStatus');if(status)status.textContent=err.message || String(err);}
      finally{b.disabled=false;}
    });
  }

  async function render(selectedRange, isEnglish) {
    if (selectedRange !== range) {internalOffset = 0; Object.values(tableStates).forEach(state=>state.page=1);}
    range=selectedRange; english=!!isEnglish;window.TLMoney?.configure({language:english?'en-US':'zh-CN'});
    const id=++request, root=$('#manageRoot'); if(!root)return;
    root.setAttribute('aria-busy','true');
    if(!data)root.innerHTML=empty(t('正在读取本地成本数据…','Loading local cost data…'));
    try{const response=await api('/api/manage?range='+encodeURIComponent(range)+'&internal_offset='+encodeURIComponent(internalOffset)+'&internal_limit='+internalLimit);if(id!==request)return;data=response;internalOffset=Number(data.internal?.offset || 0);draw();}
    catch(err){if(id!==request)return;root.innerHTML=`<div class="card"><p role="alert">${esc(err.message)}</p>${button('retry',t('重试','Retry'))}</div>`;root.querySelector('button').onclick=refresh;}
    finally{if(id===request)root.removeAttribute('aria-busy');}
  }
  window.TLManage={init(deps){api=deps.api;toast=deps.toast;drill=deps.drill;pricesChanged=deps.pricesChanged;openPrices=deps.priceLookup||deps.openPrices;contextChanged=deps.contextChanged;},render,
    setContext(value={}){if(['total_cost','historical_cost','repriced_cost'].includes(value.cost_basis))basis=value.cost_basis;if(value.tab&&['projects','quality','prices','bills','anomalies','internal'].includes(value.tab))tab=value.tab;if(value.internal_offset!=null)internalOffset=Math.max(0,Number(value.internal_offset)||0);},
    getContext(){return {cost_basis:basis,tab,internal_offset:internalOffset,internal_limit:internalLimit};},
    getNavigationState(){return {tab,basis,range,internalOffset,internalLimit,tables:Object.fromEntries(Object.entries(tableStates).map(([key,value])=>[key,{...value}]))};},
    restoreNavigationState(value){if(!value)return;if(['projects','quality','prices','bills','anomalies','internal'].includes(value.tab))tab=value.tab;if(['total_cost','historical_cost','repriced_cost'].includes(value.basis))basis=value.basis;if(typeof value.range==='string')range=value.range;internalOffset=Math.max(0,Number(value.internalOffset)||0);if(pageSizes.includes(value.internalLimit))internalLimit=value.internalLimit;for(const [key,state] of Object.entries(value.tables||{})){if(pageSizes.includes(state.size))tableStates[key]={page:Math.max(1,Number(state.page)||1),size:state.size};}},
    selectTab(value){
      if (!['projects','quality','prices','bills','anomalies','internal'].includes(value)) return;
      tab=value;
      if (data && document.querySelector('.view.active[data-view="manage"]')) draw();
    }};
})();
