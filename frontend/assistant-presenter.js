/* Compact assistant workspace. Figures come from local snapshots. */
(() => {
  'use strict';
  const esc=value=>String(value==null?'':value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const n=value=>Number.isFinite(Number(value))?Number(value):0;
  const fmt=value=>n(value).toLocaleString('en-US',{maximumFractionDigits:2});
  const rate=value=>n(value).toLocaleString('en-US',{maximumFractionDigits:6});
  const money=value=>window.TLMoney?.format(value)??('$'+n(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:6}));
  const costNote=(value,c)=>c.t('实付 ','Actual ')+money(value?.actual_cost)+' · '+c.t('估算 ','Estimated ')+money(value?.estimated_cost)+' · '+(window.TLMoney?.coverage(value,c.english)||c.t('未计价记录不计入金额','Unpriced records excluded'));
  const costBasis=(snapshot,c)=>({historical_cost:c.t('调用日价格估算','Prices at call date'),repriced_cost:c.t('按现价估算','Current prices')})[snapshot?.cost_basis]||c.t('采集时保存金额','Saved cost');
  const short=value=>n(value)>=1e9?fmt(n(value)/1e9)+'B':n(value)>=1e6?fmt(n(value)/1e6)+'M':n(value)>=1e3?fmt(n(value)/1e3)+'K':fmt(value);
  const rich=(text,snapshot,c)=>`<div class="aa-rich">${window.TLAssistantRich.render(text||'',{evidence:snapshot?.evidence||[],language:c.english?'en-US':'zh-CN'})}</div>`;
  function sourceLink(url,label){
    try{if(typeof url!=='string'||url.length>2048||!/^https?:\/\//i.test(url)||/[\s\u0000-\u001f\u007f\\]/.test(url))throw new Error();const parsed=new URL(url);if(!/^https?:$/.test(parsed.protocol)||!parsed.hostname||parsed.username||parsed.password)throw new Error();return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;}catch(_){return esc(label);}
  }
  function evidence(snapshot,refs,c){
    return (snapshot?.evidence||[]).filter(row=>!refs||refs.includes(row.id)).slice(0,6).map(row=>`<div class="aa-evidence-item"><span>${esc(row.title)}<small>${money(row.metrics?.current?.cost)} · ${fmt(row.metrics?.current?.requests)} ${c.t('次调用','calls')}</small></span>${c.btn('drill',c.t('明细','Records'),'aa-text-button',`data-filter="${esc(JSON.stringify(row.filter||{}))}"`)}</div>`).join('');
  }
  function actionButtons(actions,c){
    return (actions||[]).slice(0,2).map(action=>c.btn('signal-action',esc(action.label),'aa-text-button',`data-target="${esc(action.target||'records')}" data-filter="${esc(JSON.stringify(action.filter||{}))}"`)).join('');
  }
  function compactFact(item,c){
    const values=item.values||{};
    if(item.kind==='unpriced')return c.t(`${fmt(values.unknown_records)} 条记录尚未计价 · ${values.model||''}`,`${fmt(values.unknown_records)} unpriced records · ${values.model||''}`);
    if(item.kind==='cost_change')return c.t(`已记录费用较上期 ${n(values.cost_delta)>=0?'+':'−'}${money(Math.abs(n(values.cost_delta)))}`,`Recorded cost vs. previous period ${n(values.cost_delta)>=0?'+':'−'}${money(Math.abs(n(values.cost_delta)))}`)+(values.cost_growth==null?'':` (${n(values.cost_growth)>=0?'+':''}${fmt(values.cost_growth*100)}%)`)+(item.filter?.project?' · '+item.filter.project.split(/[\\/]/).filter(Boolean).pop():'');
    if(item.kind==='cache_decline')return c.t(`缓存命中率下降 ${fmt(Math.abs(n(values.cache_delta_pp)))} 个百分点`,`Cache hit rate declined ${fmt(Math.abs(n(values.cache_delta_pp)))} percentage points`);
    return item.fact||item.title;
  }
  function findings(items,snapshot,c){
    return (items||[]).slice(0,c.compactAnswer?3:6).map(item=>`<section class="aa-finding"><h4>${esc(item.title)}</h4>${item.fact&&!c.compactAnswer?`<p class="aa-fact">${esc(item.fact)}</p>`:''}${rich(item.interpretation||(c.compactAnswer&&!item.data_rows?.length?item.fact:''),snapshot,c)}${dataTable(item.data_rows,c)}${item.next_step&&item.next_step!==item.interpretation?`<p class="aa-next-step">${esc(item.next_step)}</p>`:''}<div class="aa-actions-row">${actionButtons(item.actions,c)}</div></section>`).join('');
  }
  function dataTable(rows,c){
    if(!rows?.length)return '';
    const value=(v,unit)=>v==null?'—':String(unit).startsWith('USD')?(n(v)<0?'−':'')+money(Math.abs(n(v))):fmt(v)+(unit==='%'?'%':'');
    return `<div class="aa-data-table ${c.compactAnswer?'aa-data-table-compact':''}"><table><thead><tr><th>${c.t('统计指标','Metric')}</th><th>${c.t('本期','Current')}</th><th>${c.t('上期','Previous')}</th><th>${c.t('变化','Change')}</th><th><span class="sr-only">${c.t('查看明细','View records')}</span></th></tr></thead><tbody>${rows.slice(0,c.compactAnswer?6:24).map(row=>`<tr><td><b>${esc(row.label||row.metric)}</b><small>${esc(row.evidence_title||row.name||'')}</small></td><td>${value(row.current,row.unit)}</td><td>${value(row.previous,row.unit)}</td><td class="${n(row.delta)>0?'aa-delta-up':n(row.delta)<0?'aa-delta-down':''}">${row.delta==null?'—':(n(row.delta)>0?'+':'')+value(row.delta,row.delta_unit==='百分点'?'':row.unit)}${row.delta_unit==='百分点'?`<small>${c.t('个百分点','percentage points')}</small>`:row.growth==null?'':`<small>${n(row.growth)>0?'+':''}${fmt(row.growth*100)}%</small>`}</td><td>${c.btn('drill',c.t('明细','Records'),'aa-text-button',`data-filter="${esc(JSON.stringify(row.filter||{}))}"`)}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function documentContent(document,text,snapshot,c){
    if(!document||document.origin==='legacy'||document.origin==='markdown')return rich(document?.legacy_text||text,snapshot,c);
    const items=document.findings||[],sections=(document.sections||[]).slice(0,c.compactAnswer?3:6),used=new Set(sections.flatMap(section=>section.signal_ids||[]));
    const seen=new Set(),unique=rows=>(rows||[]).filter(row=>{const key=row.evidence_id+'|'+row.metric;if(seen.has(key))return false;seen.add(key);return true;});
    const content=rich(document.summary||'',snapshot,c)+sections.map(section=>{const selected=items.filter(item=>(section.signal_ids||[]).includes(item.signal_id||item.id));return `<section class="aa-answer-section">${section.heading?`<h4>${esc(section.heading)}</h4>`:''}${c.compactAnswer?rich(section.body||section.text||'',snapshot,c):''}${dataTable(unique(section.data_rows),c)}${!c.compactAnswer?selected.map(item=>item.fact?`<p class="aa-fact">${esc(item.fact)}</p>`:'').join(''):''}${!c.compactAnswer?rich(section.body||section.text||'',snapshot,c):''}${selected.slice(0,c.compactAnswer?1:6).map(item=>`<div class="aa-actions-row">${actionButtons(item.actions,c)}</div>`).join('')}</section>`;}).join('');
    const remaining=c.compactAnswer&&sections.length?[]:items.filter(item=>!used.has(item.signal_id||item.id)).map(item=>({...item,data_rows:unique(item.data_rows)}));return content+findings(remaining,snapshot,c);
  }
  function answerMeta(item,c){
    const snapshot=item.snapshot;if(!snapshot)return '';
    const refs=item.document?.evidence_refs||['E1'],rows=evidence(snapshot,refs,c);
    const scope=Object.values(snapshot.filters||{}).filter(Boolean).join(' · ');
    const line=[snapshot.range,scope,costBasis(snapshot,c)+' '+money(snapshot.current?.cost),fmt(snapshot.current?.requests)+' '+c.t('次调用','calls')].filter(Boolean).join(' · ');
    return `<div class="aa-answer-meta"><span>${esc(line)}</span><small>${esc(costNote(snapshot.current,c))}</small>${rows?`<details><summary>${c.t('统计依据','Evidence')}</summary>${rows}</details>`:''}</div>`;
  }

  function overview(c){
    const snapshot=c.localSnapshot,cur=snapshot?.current,filters=c.analysisContext||{};
    const scope=Object.entries(filters).filter(([key,value])=>['tool','model','project','q'].includes(key)&&value).map(([key,value])=>`<span class="aa-scope-chip" title="${esc(value)}">${esc(key==='project'?value.split(/[\\/]/).filter(Boolean).pop():value)}</span>`).join('');
    return `<div class="aa-scope-bar"><div class="aa-scope-controls"><span class="aa-scope-label">${c.t('分析范围','Scope')}</span>${c.rangeSelect()}${scope}</div><div class="aa-scope-actions"><details class="aa-scope-details"><summary>${c.t('数据概况','Usage summary')}</summary><div class="aa-scope-popover"><div class="aa-scope-facts"><span>${c.t('已记录费用','Recorded cost')}<b>${cur?money(cur.cost):'—'}</b></span><span>${c.t('调用','Calls')}<b>${cur?fmt(cur.requests):'—'}</b></span><span>Token<b>${cur?short(cur.tokens):'—'}</b></span><span>${c.t('缓存命中','Cache hit')}<b>${cur?fmt(cur.cache_hit_rate*100)+'%':'—'}</b></span></div><p>${cur?esc(costNote(cur,c)):c.t('正在读取本机统计…','Loading local statistics…')}</p></div></details>${c.btn('diagnose',c.t('分析用量','Analyze usage'),'btn btn-ghost btn-xs',c.busy?'disabled':'')}</div></div>`;
  }
  const copyIcon='<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="9" height="10" rx="2"/><path d="M12 7V4a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h3"/></svg>';
  function message(item,index,c){
    const key=esc(item.id||String(index));
    if(item.role==='user')return `<article class="aa-message user" data-message-id="${key}"><div class="aa-user-content">${esc(item.content)}</div></article>`;
    const content=item.pending?item.content?rich(item.content,item.snapshot,c):`<div class="aa-thinking"><span class="aa-dot-pulse"></span><span>${esc(c.status?.text||c.t('正在分析本机数据…','Analyzing your local data…'))}</span></div>`:documentContent(item.document,item.content,item.snapshot,{...c,compactAnswer:true});
    return `<article class="aa-message assistant" data-message-id="${key}"><div class="aa-answer"><div class="aa-answer-label"><span class="aa-answer-mark" aria-hidden="true">✦</span>TokenLens<span>${esc(item.model||c.connection?.model||'AI')}</span></div><div class="aa-message-body" data-message-body="${key}">${content}</div>${!item.pending?answerMeta(item,c):''}${item.error?`<p class="aa-message-error">${esc(item.error)}</p>`:''}<div class="aa-message-actions" ${item.pending?'hidden':''}>${c.btn('copy-message',copyIcon+c.t('复制','Copy'),'aa-text-button',`data-message="${key}" aria-label="${c.t('复制回答','Copy answer')}"`)}${c.btn('retry-chat','↻ '+c.t('重新生成','Regenerate'),'aa-text-button',`data-message="${key}" ${c.busy?'disabled':''}`)}</div></div></article>`;
  }
  function composer(c){
    return `<div class="aa-composer-dock"><form class="aa-composer" id="aaChatForm"><textarea id="aaChatInput" rows="1" maxlength="20000" placeholder="${c.t('询问 TokenLens，例如：最近哪个项目花费最多？','Ask TokenLens, for example: Which project costs the most?')}" aria-label="${c.t('询问 AI','Ask AI')}">${esc(c.draft)}</textarea><div class="aa-composer-bottom"><span>${c.t('本机聚合统计 · Enter 发送 · Shift + Enter 换行','Local usage summaries · Enter to send · Shift + Enter for a new line')}</span>${c.busy?c.btn('stop',`<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5" y="5" width="10" height="10" rx="1"/></svg>`,'aa-send aa-send-stop',`data-aa-lane="chat" aria-label="${c.t('停止分析','Stop analysis')}" title="${c.t('停止分析','Stop analysis')}"`):`<button class="aa-send" type="submit" aria-label="${c.t('发送问题','Send question')}" title="${c.t('发送问题','Send question')}" ${!c.connection?'disabled':''}><svg aria-hidden="true" viewBox="0 0 20 20"><path d="M10 15V5m-4 4 4-4 4 4"/></svg></button>`}</div></form><p class="aa-composer-note">${c.t('回答基于所选范围的记录。聚合统计与对话会发送至所选模型。','Answers use the selected records. Usage summaries and this conversation are sent to the selected model.')}</p></div>`;
  }
  function chat(c){
    const prompts=[c.t('有哪些可以节省费用的地方？','Where can I reduce costs?'),c.t('最近费用为什么增加？','Why did my costs increase?'),c.t('哪个项目最值得优化？','Which project should I optimize first?')];
    return `<div class="aa-chat-log" id="aaConversation" tabindex="0" role="log" aria-live="off" aria-label="${c.t('对话记录','Conversation')}" aria-busy="${!!c.busy}"><div class="aa-chat-column">${c.chat.length?`<div class="aa-conversation-tools">${c.btn('clear-chat',c.t('新对话','New conversation'),'aa-text-button',c.busy?'disabled':'')}</div>${c.chat.map((item,index)=>message(item,index,c)).join('')}`:`<div class="aa-chat-empty"><div class="aa-empty-mark" aria-hidden="true">✦</div><h3>${c.t('今天想了解什么？','What would you like to know?')}</h3><p>${c.t('从你的 AI 用量中找原因，也找更好的用法。','Understand your AI usage and find better ways to work.')}</p><div class="aa-prompts">${prompts.map(question=>c.btn('suggest',esc(question)+'<span aria-hidden="true">↗</span>','aa-prompt',`data-question="${esc(question)}"`)).join('')}</div></div>`}</div></div>`;
  }
  function workspace(c){return `<section class="aa-chat-workspace">${overview(c)}${chat(c)}${composer(c)}<div class="aa-global-status ${c.status?.error?'error':''}" id="aaGlobalStatus" role="status" aria-live="polite">${c.status?.error?esc(c.status.text):''}</div></section>`;}
  function pricePreview(result,c){
    if(!result)return '';
    const rows=result.rows||[],missing=rows.filter(row=>['missing','unsupported','error','unverified'].includes(row.status)).map(row=>row.model);
    const text=c.t(`已检查 ${result.checked||rows.length} 个模型 · 更新 ${result.updated||0} 条价格记录 · ${result.applied||0} 条已用于估算 · ${result.unchanged||0} 条已是最新`,`Checked ${result.checked||rows.length} models · ${result.updated||0} price records updated · ${result.applied||0} applied to estimates · ${result.unchanged||0} current`);
    const sourceNames={public_reference:c.t('市场参考价','Market reference'),reference:c.t('市场参考价','Market reference'),official:c.t('官方公开价','Official list price'),provider:c.t('服务商公开价','Provider quote')};
    const labels={updated:c.t('已更新','Updated'),applied:c.t('已应用','Applied'),unchanged:c.t('已是最新','Current'),missing:c.t('未查到','Not found'),conditional:c.t('目录已保存','Saved to catalog'),unsupported:c.t('未查到','Not found'),error:c.t('查询失败','Failed'),unverified:c.t('来源待核验','Unverified')};
    const provenance=row=>{const date=new Date(row.fetched_at),name={openrouter:'OpenRouter',litellm:'LiteLLM',modelsdev:'models.dev'}[row.source_name]||row.source_name||c.t('查看来源','View source');return `<small>${sourceLink(row.source_url,name)} · ${esc(sourceNames[row.source_kind]||sourceNames[row.scope]||row.scope||'')} ${esc(row.source_provider||row.provider||'')}</small>${Number.isFinite(date.getTime())?`<small>${c.t('查询于 ','Checked ')}${esc(date.toLocaleString(c.english?'en-US':'zh-CN'))}</small>`:''}`;};
    return `<section class="aa-price-result"><div class="aa-price-result-head"><span class="aa-result-icon" aria-hidden="true">${result.updated?'✓':'↻'}</span><div><h4>${c.t('价格更新结果','Price update result')}</h4><p>${esc(text)}</p></div>${c.btn('dismiss-price',c.t('关闭','Close'),'aa-text-button',`aria-label="${c.t('关闭价格结果','Dismiss price result')}"`)}</div>${missing.length?`<p class="aa-price-missing">${c.t('暂未查到：','Not found: ')}${missing.map(esc).join('、')}</p>`:''}${rows.length?`<div class="aa-price-table"><table><thead><tr><th>${c.t('模型 / 来源','Model / source')}</th><th>${c.t('输入','Input')}</th><th>${c.t('输出','Output')}</th><th>${c.t('缓存读取 / 写入','Cache read / write')}</th><th>${c.t('状态','Status')}</th></tr></thead><tbody>${rows.map(row=>`<tr><td><b>${esc(row.model)}</b>${provenance(row)}<small>${esc(row.currency||'USD')} / ${row.unit&&row.unit!=='per_million_tokens'?esc(row.unit):c.t('百万 Token','million tokens')}</small></td><td>${row.rates?.input==null?'—':rate(row.rates.input)}</td><td>${row.rates?.output==null?'—':rate(row.rates.output)}</td><td>${row.rates?.cache_read==null?'—':rate(row.rates.cache_read)} / ${row.rates?.cache_write==null?'—':rate(row.rates.cache_write)}</td><td>${esc(labels[row.status]||row.status||'—')}${row.errors?.length?`<small>${row.errors.map(esc).join('；')}</small>`:''}</td></tr>`).join('')}</tbody></table></div>`:''}</section>`;
  }
  window.TLAssistantPresenter={workspace,diagnosis:overview,chat,message,findings,evidence,documentContent,dataTable,pricePreview,sourceLink,rich};
})();

