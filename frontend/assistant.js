/* Internal assistant tabs share conversations, reports and task state. */
(() => {
  'use strict';
  let api,apiStream,toast,drill,navigate,pricesChanged,english=false,state=null,connection=null;
  let ranges='7d',localSnapshot=null,diagnosis=null,report=null,chat=[],draft='',priceResult=null;
  let reportPeriod='weekly',reportStart='',reportEnd='',pricePolicy='auto',priceUrl='',priceProvider='';
  let analysisContext={range:'7d',tool:'',model:'',project:'',cost_basis:'total_cost'},priceModel='';
  let contextChanged;
  let tab='chat';const tabScroll={chat:0,reports:0,prices:0};let chatScroll=0,followChat=true,forceChatFollow=false,priceSelections={},priceApplied={},priceSaving=false,priceSaveMessage='',deletingReport='';
  const tasks=Object.fromEntries(['chat','reports','prices'].map(lane=>[lane,{busy:false,kind:null,controller:null,ticket:0,status:{text:'',error:false},ready:false}]));
  let snapshotRequest=0,loadRequest=0,paintTimer=0,messageId=0;
  const laneFor=kind=>['weekly','monthly','report'].includes(kind)?'reports':['prices-fill','prices-restore'].includes(kind)?'prices':'chat';
  const $=selector=>document.querySelector(selector),t=(zh,en)=>english?en:zh;
  const visible=lane=>tab===lane&&$('.view.active')?.dataset.view==='assistant';
  const esc=value=>String(value==null?'':value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const btn=(action,label,cls='btn btn-ghost',extra='')=>`<button type="button" class="${cls}" data-aa-action="${action}" ${extra}>${label}</button>`;
  const rangeSelect=()=>{const options=[['today','今天','Today'],['7d','最近 7 天','Last 7 days'],['30d','最近 30 天','Last 30 days'],['90d','最近 90 天','Last 90 days'],['365d','最近一年','Last year'],['all','全部时间','All time']];if(!options.some(([v])=>v===ranges))options.push([ranges,ranges,ranges]);return `<select class="aa-range" id="aaRange" aria-label="${t('分析时间范围','Analysis range')}" >${options.map(([value,zh,en])=>`<option value="${esc(value)}" ${ranges===value?'selected':''}>${esc(t(zh,en))}</option>`).join('')}</select>`;};
  const context=()=>{const task=tasks[tab];return {t,btn:(action,label,cls,extra='')=>btn(action,label,cls,extra+' data-aa-lane="'+tab+'"'),english,state,connection,diagnosis,localSnapshot,report,reportPeriod,reportStart,reportEnd,pricePolicy,priceUrl,priceProvider,priceModel,analysisContext,priceSelections,priceApplied,priceSaving,priceSaveMessage,chat,draft,priceResult,busy:task.busy,activeTask:task.kind,status:task.status,rangeSelect};};
  function taskTabs(){
    const labels={chat:t('AI 分析','AI analysis'),reports:t('AI 报告','AI reports'),prices:t('价格更新','Price updates')};
    $('#aaTabs').querySelectorAll('[data-aa-tab]').forEach(button=>{const lane=button.dataset.aaTab,task=tasks[lane];button.innerHTML=esc(labels[lane])+(task.busy?`<span class="aa-task-badge running" role="img" aria-label="${t('运行中','Running')}"></span>`:task.ready?`<span class="aa-task-badge ${task.status.error?'error':'ready'}" role="img" aria-label="${t(task.status.error?'任务失败':'任务完成',task.status.error?'Failed':'Completed')}"></span>`:'');button.title=task.busy?task.status.text:task.ready?task.status.error?task.status.text:t('任务已完成，点击查看','Completed, select to view'):'';});
  }
  function heading(){
    $('#aaModelLabel').textContent=connection?.model||t('请在设置中选择模型','Choose a model in Settings');$('#aaConnect').classList.toggle('ready',!!connection);$('#aaConnect').title=t('在设置中修改分析模型','Change the model in Settings');$('#aaRoot .aa-connection small').textContent=t('分析模型','Model');
    $('#aaRoot .aa-hero-copy p').textContent=tab==='reports'?t('生成周报、月报或所选日期的使用报告。','Generate weekly, monthly or custom usage reports.'):tab==='prices'?t('查询最新公开价格，确认后保存并更新估算。','Find public prices, review and apply them to estimates.'):t('用记录回答问题，让建议有据可查。','Answers and recommendations grounded in your records.');
    taskTabs();$('#aaTabs').setAttribute('aria-label',t('AI 助手功能','AI assistant tools'));$('#aaRoot').dataset.tab=tab;
    $('#aaTabs').querySelectorAll('[data-aa-tab]').forEach(button=>{const selected=button.dataset.aaTab===tab;button.classList.toggle('active',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});$('#aaBody').setAttribute('aria-labelledby',{chat:'aaTabChat',reports:'aaTabReports',prices:'aaTabPrices'}[tab]);
    $('#aaRoot .aa-privacy').textContent=tab==='prices'?t('公开参考价会注明来源；第三方参考价与实际账单可能不同。','Public reference rates include their sources and may differ from your bill.'):t('聚合统计和当前对话会发送到所选 AI 服务，API Key 保存在本机。','Aggregated statistics and the conversation are sent to your selected AI service. API keys stay on this computer.');
  }
  async function load(){
    const request=++loadRequest,next=await api('/api/assistant/state');if(request!==loadRequest)return state;state=next;const selected=state.selection;
    connection=selected?.configured?{connection_id:selected.connection_id,model:selected.model,protocol:selected.protocol,thinking:selected.thinking||'auto'}:null;
    if(priceResult?.confirmed&&state.active_price_versions){
      for(const row of priceResult.rows)row.priceInactive=!!row.priceVersion&&state.active_price_versions[row.model.trim().toLowerCase()]!==row.priceVersion;
      priceSaveMessage=priceResult.confirmationMessage||priceSaveMessage;
      if(priceResult.rows.some(row=>row.priceInactive))priceSaveMessage+=t(' 当前定价已更改，之前的应用结果已失效；可重新应用或查询最新价格。',' Current pricing changed. The previous application is no longer active; reapply or check latest prices.');
    }
    heading();return state;
  }
  function setStatus(text,error=false,lane=tab){
    const task=tasks[lane];task.status={text,error};taskTabs();if(tab!==lane)return;
    const node=$('#aaGlobalStatus');if(node){node.textContent=lane==='chat'&&!error?'':text;node.classList.toggle('error',error);}
    const thinking=$('#aaConversation .aa-thinking>span:last-child');if(thinking)thinking.textContent=text||t('正在分析本机数据…','Analyzing your local data…');
  }
  async function refreshSnapshot(){
    const request=++snapshotRequest,expectedContext=JSON.stringify(analysisContext),params=new URLSearchParams({range:ranges});
    for(const [key,value] of Object.entries(analysisContext))if(value && key!=='range')params.set(key,value);
    try{const result=await api('/api/assistant/snapshot?'+params);if(request!==snapshotRequest||expectedContext!==JSON.stringify(analysisContext))return;localSnapshot=result.snapshot;draw();}
    catch(error){if(request===snapshotRequest)setStatus(error.message,true,'chat');}
  }
  function resizeInput(input){if(!input)return;input.style.height='auto';input.style.height=Math.min(144,Math.max(42,input.scrollHeight))+'px';}
  function draw(){
    const oldInput=$('#aaChatInput'),focused=document.activeElement===oldInput,selection=oldInput?[oldInput.selectionStart,oldInput.selectionEnd]:null;
    if(oldInput)draft=oldInput.value;
    const oldLog=$('#aaConversation');if(oldLog){chatScroll=oldLog.scrollTop;followChat=oldLog.scrollHeight-oldLog.clientHeight-chatScroll<70;}if(forceChatFollow){followChat=true;forceChatFollow=false;}
    const view=$('.view.active')?.dataset.view;if(view==='assistant')tasks[tab].ready=false;heading();if(view!=='assistant')return;
    const body=$('#aaBody'),scroll=body.scrollTop;body.innerHTML=tab==='reports'?window.TLAssistantPanels.reportWorkspace(context()):tab==='prices'?window.TLAssistantPanels.priceWorkspace(context()):window.TLAssistantPresenter.workspace(context());
    const range=$('#aaRange');if(range)range.onchange=event=>{setContext({...analysisContext,range:event.target.value,from:'',to:''});contextChanged?.({...analysisContext});};
    const input=$('#aaChatInput');if(input){resizeInput(input);input.oninput=()=>{draft=input.value;resizeInput(input);};input.onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!tasks.chat.busy)run('chat');}};$('#aaChatForm').onsubmit=event=>{event.preventDefault();run('chat');};}
    if(tab==='reports'){$('#aaReportStart').oninput=event=>reportStart=event.target.value;$('#aaReportEnd').oninput=event=>reportEnd=event.target.value;$('#aaReportHistory').onchange=()=>{deletingReport='';$('#aaDeleteReportConfirm').hidden=true;};$('#aaReportForm').onsubmit=event=>{event.preventDefault();if(tasks.reports.busy)return;if(!reportStart||!reportEnd||reportStart>reportEnd){$('#aaReportDialogError').textContent=t('请选择有效的开始和结束日期。','Choose valid start and end dates.');return;}run('report');};}
    if(tab==='prices'){$('#aaPriceModel').oninput=event=>priceModel=event.target.value.trim();$('#aaPricePolicy').onchange=event=>{pricePolicy=event.target.value;draw();};$('#aaPriceUrl').oninput=event=>priceUrl=event.target.value;$('#aaPriceProvider').oninput=event=>priceProvider=event.target.value;}
    const log=$('#aaConversation');if(log)log.scrollTop=followChat?log.scrollHeight:chatScroll;
    if(focused&&input){input.focus({preventScroll:true});if(selection)input.setSelectionRange(...selection);}body.scrollTop=scroll;
  }
  function showTab(value){
    if(value==='tools')value='reports';if(!['chat','reports','prices'].includes(value)||value===tab)return;
    const input=$('#aaChatInput');if(input)draft=input.value;const visible=$('.view.active')?.dataset.view==='assistant',scroll=$('#aaBody');if(visible)tabScroll[tab]=scroll.scrollTop;
    tab=value;tasks[tab].ready=false;try{localStorage.setItem('tl.assistant.tab',tab);}catch(_){}draw();if(visible)scroll.scrollTop=tabScroll[tab];
  }
  function updateStream(item){
    if(tab!=='chat'||$('.view.active')?.dataset.view!=='assistant'||paintTimer)return;
    paintTimer=setTimeout(()=>{paintTimer=0;if(!item?.pending||!visible('chat'))return;const node=$('[data-message-body="'+item.id+'"]');if(!node||!item.content)return;
      const log=$('#aaConversation'),follow=log&&log.scrollHeight-log.clientHeight-log.scrollTop<70;
      const template=document.createElement('template');template.innerHTML=window.TLAssistantPresenter.rich(item.content,item.snapshot,context());
      const incoming=template.content.firstElementChild,current=node.firstElementChild;
      // Keep completed blocks in place so streaming does not reset table scrolling,
      // selection, the input, or the rest of the conversation.
      if(current?.classList.contains('aa-rich')){let common=0;while(common<current.children.length&&common<incoming.children.length&&current.children[common].outerHTML===incoming.children[common].outerHTML)common++;while(current.children.length>common)current.lastElementChild.remove();for(const child of [...incoming.children].slice(common))current.append(child);}
      else node.replaceChildren(incoming);
      if(follow)log.scrollTop=log.scrollHeight;
    },item.content.length>16000?180:80);
  }
  async function downloadReport(){
    if(!report)return;const content=window.TLAssistantPanels.reportMarkdown(report,context());if(window.TLNative){try{await window.TLNative.save('TokenLens-'+report.kind+'-'+report.id.slice(0,8)+'.md',content);}catch(error){toast(error.message,'err');}return;}const url=URL.createObjectURL(new Blob([content],{type:'text/markdown;charset=utf-8'})),link=document.createElement('a');link.href=url;link.download='TokenLens-'+report.kind+'-'+report.id.slice(0,8)+'.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function openReport(){if(!report||!visible('reports'))return;$('#aaReportPreviewTitle').textContent=window.TLAssistantPanels.title(report,context());$('#aaReportPreviewBody').innerHTML=window.TLAssistantPanels.reportPreview(report,context());$('#aaReportPreviewBody').scrollTop=0;$('#aaReportExport').textContent=t('导出 Markdown','Export Markdown');if(!$('#aaReportPreview').open)$('#aaReportPreview').showModal();}
  function closeReport(){$('#aaReportPreview').close();}
  function priceReady(){const count=(priceResult?.rows||[]).filter(row=>priceSelections[row.candidate_id]&&row.adoptable).length;$('#aaPriceConfirm').disabled=priceSaving||tasks.prices.busy||!count;$('#aaPriceCancel').disabled=priceSaving;$('#aaPriceClose').disabled=priceSaving;$('#aaPriceConfirm').textContent=priceSaving?t('正在保存并更新…','Applying…'):priceResult?.confirmationBody?t('重新应用已确认价格','Reapply confirmed prices'):t('保存所选价格并更新','Save selected prices and apply');}
  function drawPrice(){
    const body=$('#aaPricePreviewBody'),scroll=body.scrollTop;body.innerHTML=window.TLAssistantPanels.priceReview(priceResult,context());body.querySelectorAll('[data-price-select]').forEach(input=>input.onchange=()=>{priceSelections[input.dataset.priceSelect]=input.checked;drawPrice();});body.querySelectorAll('[data-price-apply]').forEach(input=>input.onchange=()=>{priceApplied[input.dataset.priceApply]=input.checked;priceReady();});body.scrollTop=scroll;priceReady();
  }
  async function openPrice(){if(!priceResult||!visible('prices'))return;const expected=priceResult;if(priceResult.confirmed){try{await load();}catch(error){setStatus(error.message,true,'prices');return;}}if(!visible('prices')||priceResult!==expected)return;$('#aaPricePreviewTitle').textContent=t('确认模型新价格','Review new model prices');$('#aaPriceCancel').textContent=priceResult.confirmed?t('关闭','Close'):t('暂不保存','Cancel');$('#aaPriceConfirmStatus').textContent=priceResult.confirmed?priceSaveMessage:t('选择需要保存的模型，查询不会自动保存。','Select models to save. Queries do not save prices.');$('#aaPriceConfirmStatus').classList.toggle('error',!!priceResult.rows.some(row=>row.applyError||row.priceInactive));drawPrice();if(!$('#aaPricePreview').open)$('#aaPricePreview').showModal();}
  function closePrice(){if(priceSaving)return;$('#aaPricePreview').close();}
  async function confirmPrices(){
    if(priceSaving||tasks.prices.busy)return;const selected=(priceResult?.rows||[]).filter(row=>priceSelections[row.candidate_id]&&row.adoptable);if(!selected.length)return;
    priceSaving=true;drawPrice();$('#aaPriceConfirmStatus').textContent=t('正在保存价格并更新可计价流水…','Saving prices and updating eligible records…');$('#aaPriceConfirmStatus').classList.remove('error');
    try{const body=priceResult.confirmationBody||{preview_id:priceResult.preview_id,candidate_ids:selected.map(row=>row.candidate_id),apply_estimate_ids:selected.filter(row=>row.estimate_eligible&&priceApplied[row.candidate_id]).map(row=>row.candidate_id),effective_from:priceResult.effective_from};const result=await api('/api/assistant/prices/confirm',{method:'POST',body});if(!result.ok)throw new Error(result.error||t('保存失败','Save failed'));
      priceResult.confirmationBody=body;priceResult.confirmed=true;const applied=new Map((result.applied||[]).map(row=>[row.candidate_id,row])),errors=new Map((result.errors||[]).map(row=>[row.candidate_id,row.error]));for(const row of priceResult.rows){row.updated=body.candidate_ids.includes(row.candidate_id);row.applied=applied.has(row.candidate_id);row.applyError=errors.get(row.candidate_id)||'';row.priceVersion=applied.get(row.candidate_id)?.price_version_id;}priceSaveMessage=t(`已保存 ${result.saved||0} 个模型价格，应用 ${applied.size} 个估算价格，补齐 ${result.repriced_records||0} 条未计价流水。`,`Saved ${result.saved||0} model prices; applied ${applied.size} estimate prices and priced ${result.repriced_records||0} records.`);if(result.remaining_unknown_records)priceSaveMessage+=t(` ${result.remaining_unknown_records} 条记录仍缺少所用费项价格或有效用量。`,` ${result.remaining_unknown_records} records still lack required rates or valid usage.`);if(result.errors?.length)priceSaveMessage+=' '+t('部分模型应用失败，可重新应用重试；原因见对应模型。','Some models failed to apply. Reapply to retry; see each model for details.');priceResult.confirmationMessage=priceSaveMessage;$('#aaPriceConfirmStatus').textContent=priceSaveMessage;$('#aaPriceConfirmStatus').classList.toggle('error',!!result.errors?.length);$('#aaPriceCancel').textContent=t('关闭','Close');diagnosis=null;await load();await refreshSnapshot();await pricesChanged?.();draw();
    }catch(error){$('#aaPriceConfirmStatus').textContent=error.message;$('#aaPriceConfirmStatus').classList.add('error');}
    finally{priceSaving=false;drawPrice();}
  }
  async function action(button){
    const value=button.dataset.aaAction;
    if(value==='stop'){tasks[button.dataset.aaLane||tab]?.controller?.abort();return;}
    if(value==='dismiss-price'){priceResult=null;draw();return;}
    if(value==='dismiss-report'){report=null;draw();return;}
    if(value==='load-report'){
      const id=$('#aaReportHistory').value;if(!id)return;
      button.disabled=true;try{report=(await api('/api/assistant/report?id='+encodeURIComponent(id))).report;if(visible('reports')){draw();openReport();}else{tasks.reports.ready=true;taskTabs();}}catch(error){setStatus(error.message,true,'reports');if(!visible('reports'))tasks.reports.ready=true;taskTabs();button.disabled=false;}return;
    }
    if(value==='delete-report'){if(tasks.reports.busy)return;const selected=$('#aaReportHistory').selectedOptions[0];if(!selected?.value)return;deletingReport=selected.value;$('#aaDeleteReportLabel').textContent=t('删除这份报告：','Delete this report: ')+selected.textContent;$('#aaDeleteReportConfirm').hidden=false;return;}
    if(value==='cancel-delete-report'){deletingReport='';$('#aaDeleteReportConfirm').hidden=true;return;}
    if(value==='confirm-delete-report'){if(tasks.reports.busy||!deletingReport)return;button.disabled=true;try{const id=deletingReport,result=await api('/api/assistant/report/delete',{method:'POST',body:{id}});if(!result.ok)throw new Error(result.error||t('删除失败','Delete failed'));if(report?.id===id){closeReport();report=null;}state.reports=result.reports;deletingReport='';if(tab==='reports')draw();toast?.(t('报告已删除','Report deleted'));}catch(error){setStatus(error.message,true,'reports');button.disabled=false;}return;}
    if(value==='export-report'){downloadReport();return;}
    if(value==='open-report-preview'){openReport();return;}
    if(value==='open-price-preview'){openPrice();return;}
    if(value==='restore-prices'){
      const task=tasks.prices;if(task.busy||priceSaving)return;task.busy=true;task.kind='prices-restore';task.controller=new AbortController();setStatus(t('正在按已保存价格补齐流水…','Applying saved rates…'),false,'prices');draw();
      try{const result=await api('/api/assistant/prices/restore',{method:'POST',body:{},signal:task.controller.signal});if(!result.ok)throw new Error(result.error||t('应用失败','Apply failed'));priceSaveMessage=result.message;diagnosis=null;await load();await refreshSnapshot();await pricesChanged?.();setStatus(result.message,false,'prices');}
      catch(error){setStatus(error.name==='AbortError'?t('已停止','Stopped'):error.message,error.name!=='AbortError','prices');}
      finally{task.busy=false;task.kind=null;task.controller=null;task.ready=!visible('prices');taskTabs();if(visible('prices'))draw();}return;
    }
    if(value==='drill'){try{closeReport();drill(JSON.parse(button.dataset.filter||'{}'));}catch(error){setStatus(error.message,true);}return;}
    if(value==='clear-chat'){if(tasks.chat.busy)return;chat=[];draft='';tasks.chat.status={text:'',error:false};draw();$('#aaChatInput')?.focus();return;}
    if(value==='suggest'){draft=button.dataset.question;$('#aaChatInput').value=draft;resizeInput($('#aaChatInput'));$('#aaChatInput').focus();return;}
    if(value==='signal-action'){const filter=JSON.parse(button.dataset.filter||'{}'),target=button.dataset.target;
      closeReport();if(['pricing','prices'].includes(target))openPrices(filter.model);else if(['session','sources'].includes(target)&&navigate)navigate(target,filter);else drill(filter);return;
    }
    if(value==='copy-message'){
      const item=chat.find(row=>row.id===button.dataset.message);if(!item)return;
      const content=item.document&&item.document.origin!=='markdown'&&item.document.origin!=='legacy'?[item.document.summary,...(item.document.sections||[]).map(row=>'## '+(row.heading||'')+'\n'+(row.body||row.text||'')),...(item.document.findings||[]).map(row=>'## '+(row.title||'')+'\n'+[row.fact,row.interpretation,row.next_step].filter(Boolean).join('\n'))].filter(Boolean).join('\n\n'):item.content;
      try{await navigator.clipboard.writeText(content||'');toast?.(t('回答已复制','Answer copied'));}catch(error){toast?.(t('无法访问剪贴板，请选择文字复制。','Clipboard unavailable. Select the text to copy.'),'err');}return;
    }
    if(value==='retry-chat'){
      if(tasks.chat.busy)return;const index=chat.findIndex(row=>row.id===button.dataset.message);if(index<0)return;
      if(chat[index]?.task==='diagnose'){chat.splice(index);run('diagnose');return;}
      const question=chat[index-1]?.role==='user'?chat[index-1].content:'';if(question){chat.splice(index-1);draft=question;if($('#aaChatInput'))$('#aaChatInput').value=question;run('chat');}return;
    }
    if(value==='auto-prices'){run('prices-fill');return;}
    run(value);
  }
  async function run(kind){
    const lane=laneFor(kind),task=tasks[lane];if(task.busy||(lane==='prices'&&priceSaving))return;
    if(!connection?.model){setStatus(t('请先在设置中配置 AI 助手模型。','Configure the assistant model in Settings first.'),true,lane);if(navigate)navigate('settings',{});return;}
    const body={action:kind,range:ranges,context:{...analysisContext}};let item=null;
    if(kind==='report')body.range=reportStart+'~'+reportEnd;
    if(kind==='chat'){
      body.prompt=($('#aaChatInput')?.value??draft).trim();if(!body.prompt)return;
      body.messages=chat.filter(row=>!row.pending&&!row.error).slice(-20).map(row=>({role:row.role,content:row.content.slice(0,12000)}));
      chat.push({id:String(++messageId),role:'user',content:body.prompt});item={id:String(++messageId),role:'assistant',content:'',pending:true,model:connection.model};chat.push(item);draft='';if($('#aaChatInput'))$('#aaChatInput').value='';forceChatFollow=true;
    }
    if(kind==='prices-fill'){
      if(!priceModel&&!(state?.models||[]).length)return setStatus(t('暂无已使用模型，可先扫描使用记录。','No used models yet. Scan usage records first.'),true,lane);
      if(priceModel)body.models=[priceModel];body.preview_only=true;body.source_policy=pricePolicy;
      if(pricePolicy==='provider'){if(!priceUrl.trim()||!priceProvider.trim())return setStatus(t('请填写服务商名称与公开价格地址。','Enter a provider name and public pricing URL.'),true,lane);body.scope='provider';body.source_url=priceUrl.trim();body.provider=priceProvider.trim();}
    }
    if(kind==='diagnose'){snapshotRequest++;item={id:String(++messageId),role:'assistant',content:'',pending:true,task:'diagnose',model:connection.model};chat.push(item);forceChatFollow=true;}
    // Bound retained history while preserving every running answer's object identity.
    if(chat.length>40)chat.splice(0,chat.length-40);
    task.busy=true;task.kind=kind;task.ready=false;const controller=new AbortController();task.controller=controller;const ticket=++task.ticket,taskContext=JSON.stringify(analysisContext);setStatus(t(kind==='prices-fill'?'正在查询最新价格…':'正在读取统计数据…',kind==='prices-fill'?'Checking latest prices…':'Reading your usage statistics…'),false,lane);draw();let final=null,streamText='';
    try{
      await apiStream('/api/assistant/stream',body,event=>{
        if(ticket!==task.ticket)return;
        if(event.type==='stage')setStatus(event.text||event.stage,false,lane);
        if(event.type==='content'&&item){streamText=(streamText+(event.text||'')).slice(0,100000);item.content=streamText;updateStream(item);}
        if(event.type==='evidence'&&item){item.snapshot=event.snapshot;if(taskContext===JSON.stringify(analysisContext)){localSnapshot=event.snapshot;if(tab==='chat'){const scope=$('.aa-scope-bar');if(scope){const template=document.createElement('template');template.innerHTML=window.TLAssistantPresenter.diagnosis(context());scope.replaceWith(template.content);const range=$('#aaRange');if(range)range.onchange=event=>{setContext({...analysisContext,range:event.target.value,from:'',to:''});contextChanged?.({...analysisContext});};}}}updateStream(item);}
        if(event.type==='result')final=event;
      },controller.signal);
      if(!final)throw new Error(t('连接结束但未收到结果，请重试。','The connection ended without a result. Please retry.'));
      if(!final.ok)throw new Error(final.error||t('AI 请求失败。','AI request failed.'));
      if(item&&!final.text&&!streamText&&!final.document)throw new Error(t('模型未返回回答，请重试或更换模型。','The model returned no answer. Retry or select another model.'));
      if(item){Object.assign(item,{content:String(final.text||streamText).slice(0,100000),snapshot:final.snapshot||item.snapshot,document:final.document,pending:false});if(kind==='diagnose'&&taskContext===JSON.stringify(analysisContext)){diagnosis=final;localSnapshot=final.snapshot;}}
      if(kind==='prices-fill'){priceResult=final;priceSelections={};priceApplied={};priceSaveMessage='';for(const row of final.rows||[]){if(row.candidate_id){priceSelections[row.candidate_id]=!!row.adoptable;priceApplied[row.candidate_id]=!!row.estimate_eligible;}}if(visible('prices'))openPrice();}
      if(lane==='reports'){if(!final.report)throw new Error(t('未收到报告内容，请重试。','No report content was received. Please retry.'));report=final.report;if(visible('reports'))openReport();}
      setStatus(lane==='chat'?'':t('已完成','Completed'),false,lane);try{await load();}catch(error){setStatus(t('任务已完成，列表暂时未刷新。','Task completed. The list could not be refreshed yet.'),false,lane);}
    }catch(error){
      const stopped=error.name==='AbortError',message=stopped?t('已停止，可继续提问或重新生成。','Stopped. Ask another question or regenerate.'):error.message;
      if(item){item.pending=false;item.error=message;}setStatus(message,!stopped,lane);
    }finally{
      if(lane==='chat'){clearTimeout(paintTimer);paintTimer=0;}task.busy=false;task.kind=null;task.controller=null;task.ready=!visible(lane);taskTabs();if(visible(lane))draw();if($('#aaPricePreview').open)priceReady();
    }
  }
  function setContext(value={}){
    const next={range:String(value.range||ranges),tool:String(value.tool||''),model:String(value.model||''),project:String(value.project||''),project_paths:String(value.project_paths||''),q:String(value.q||value.query||''),from:String(value.from||''),to:String(value.to||''),cost_basis:['total_cost','historical_cost','repriced_cost'].includes(value.cost_basis)?value.cost_basis:'total_cost'};
    if(JSON.stringify(next)===JSON.stringify(analysisContext))return;
    // Already-running answers retain their captured evidence; the next task
    // uses this context, and stale snapshot responses cannot replace it.
    analysisContext=next;ranges=next.range;diagnosis=null;localSnapshot=null;snapshotRequest++;if(!tasks.chat.busy)tasks.chat.status={text:'',error:false};
    if(state){draw();refreshSnapshot();}
  }
  function openPrices(value=''){const model=typeof value==='object'?value?.model:value;priceModel=model==='未知模型'?'':String(model||'');showTab('prices');if(state)draw();}
  window.TLAssistant={
    init(deps){$('#aaBody').addEventListener('click',event=>{const button=event.target.closest('[data-aa-action]');if(button)action(button);});contextChanged=deps.contextChanged;api=deps.api;apiStream=deps.apiStream;toast=deps.toast;drill=deps.drill;navigate=deps.navigate;pricesChanged=deps.pricesChanged;english=deps.language==='en-US';try{const saved=localStorage.getItem('tl.assistant.tab');if(['reports','prices'].includes(saved))tab=saved;else if(saved==='tools')tab='reports';}catch(_){}$('#aaConnect').onclick=()=>navigate?.('settings',{});const tabs=[...$('#aaTabs').querySelectorAll('[data-aa-tab]')];for(const button of tabs){button.onclick=()=>showTab(button.dataset.aaTab);button.onkeydown=event=>{const index=tabs.indexOf(button),next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:-1;if(next<0)return;event.preventDefault();showTab(tabs[next].dataset.aaTab);tabs[next].focus({preventScroll:true});};}$('#aaReportPreviewBody').addEventListener('click',event=>{const button=event.target.closest('[data-aa-action]');if(button)action(button);});$('#aaReportClose').onclick=closeReport;$('#aaReportExport').onclick=downloadReport;$('#aaPriceClose').onclick=closePrice;$('#aaPriceCancel').onclick=closePrice;$('#aaPriceConfirm').onclick=confirmPrices;$('#aaPricePreview').oncancel=event=>{if(priceSaving)event.preventDefault();};},
    async refresh(){await load();draw();},
    async render(language){english=language==='en-US';window.TLMoney?.configure({language});try{await load();}catch(error){$('#aaBody').innerHTML=`<p class="aa-message-error">${esc(error.message)}</p>`;return;}draw();if(!localSnapshot)refreshSnapshot();},
    language(value){english=value==='en-US';if(state)draw();},
    selected(){return connection?{...connection}:null;},
    showTab,setContext,getContext(){return {...analysisContext};},openPrices
  };
})();
