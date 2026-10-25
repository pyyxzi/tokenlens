/* Settings-owned model picker. Credentials remain in the local backend. */
(() => {
 'use strict';
 let api,toast,onChange,state=null,target='assistant',english=false,ticket=0,session=0,fetchController=null,models=[],saving=false,editing=false,initializing=false;
 let checkController=null,checkTicket=0,checking=false,checkedSignature='',checkExpires=0,expiryTimer=0;
 const $=s=>document.querySelector(s),t=(zh,en)=>english?en:zh;
 const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const appNames={codex:'Codex',claude:'Claude Code',gemini:'Gemini',opencode:'OpenCode','claude-desktop':'Claude Desktop'};
 const row=()=>state?.connections.find(item=>item.id===$('#aisProvider').value);
 function status(text,error=false){$('#aisStatus').textContent=text;$('#aisStatus').classList.toggle('error',error);}
 function body(){
   const result={target,connection_id:$('#aisSource').value==='custom'?'custom':$('#aisProvider').value,protocol:$('#aisProtocol').value,model:$('#aisModel').value.trim(),thinking:$('#aisThinking').value||'auto'};
   if(result.connection_id==='custom'||editing)result[result.connection_id==='custom'?'custom':'edit']={name:$('#aisName').value.trim(),base_url:$('#aisUrl').value.trim(),api_key:$('#aisKey').value};
   return result;
 }
 function signature(){const value=body();delete value.thinking;return JSON.stringify(value);}
 let thinkingTimer=0,thinkingTicket=0;
 const thinkingLabels={auto:'自动',none:'不思考',minimal:'极低',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高'};
 // The reasoning menu mirrors the IQ page: ask the backend which levels the
 // selected model and protocol actually accept instead of guessing.
 function loadThinking(){
   const select=$('#aisThinking'),note=$('#aisThinkingNote'),model=$('#aisModel').value.trim(),protocol=$('#aisProtocol').value;
   const ticket=++thinkingTicket;select.disabled=true;
   if(!model){select.innerHTML='<option value="auto">'+t('自动','Auto')+'</option>';select.disabled=saving||initializing;note.textContent=t('填写模型 ID 后读取可用思考档位。','Enter a model ID to read the available levels.');return;}
   const query=new URLSearchParams({model,protocol,base_url:row()?.base_url||''}).toString();
   api('/api/ai-thinking?'+query).then(result=>{
     if(ticket!==thinkingTicket)return;
     const levels=Array.isArray(result?.levels)&&result.levels.length?result.levels:['auto'],previous=select.value;
     select.innerHTML=levels.map(value=>`<option value="${esc(value)}">${esc(english?value:thinkingLabels[value]||value)}</option>`).join('');
     select.value=levels.includes(previous)?previous:(levels.includes('auto')?'auto':levels[0]);
     select.disabled=saving||initializing;
     note.textContent=result?.note||t('自动模式使用服务商默认值。','Auto uses the provider default.');
   }).catch(()=>{if(ticket===thinkingTicket){select.disabled=saving||initializing;note.textContent=t('读取思考参数失败，将使用服务商默认值。','Could not read reasoning parameters; the provider default is used.');}});
 }
 function scheduleThinking(){clearTimeout(thinkingTimer);thinkingTimer=setTimeout(loadThinking,180);}
 function ready(){
   $('#aisProvider').disabled=saving||initializing;
   const value=body(),fields=value.custom||value.edit,selected=row();
   const complete=value.connection_id&&value.model&&(!fields||fields.base_url&&(fields.api_key||editing&&selected?.has_key));
   $('#aisCheck').disabled=saving||checking||!complete||$('#aisProvider').disabled;
   $('#aisSave').disabled=saving||checking||!checkedSignature||checkedSignature!==signature()||Date.now()>=checkExpires;
   $('#aisFetch').disabled=saving||initializing||!!fetchController||!value.connection_id||!!fields&&!(fields.base_url&&(fields.api_key||editing&&selected?.has_key));
   $('#aisEdit').hidden=!selected?.editable||$('#aisSource').value==='custom';
   $('#aisEdit').textContent=editing?t('取消编辑','Cancel edit'):t('编辑连接','Edit connection');
   $('#aisSave').textContent=editing?t('保存并使用','Save and use'):t('使用此模型','Use this model');
   for(const id of ['aisSource','aisAppType','aisProviderSearch','aisModel','aisName','aisUrl','aisKey','aisProtocol','aisThinking','aisRefresh','aisEdit'])$('#'+id).disabled=saving||initializing;
   if(!$('#aisModel').value.trim())$('#aisThinking').disabled=true;
   $('#aisClose').disabled=saving;$('#aisCancel').disabled=saving;
 }
 function invalidateCheck(){checkTicket++;checkController?.abort();checkController=null;checking=false;checkedSignature='';checkExpires=0;clearTimeout(expiryTimer);$('#aisCheck').textContent=t('快速自检','Quick check');$('#aisCheck').classList.remove('iq-check-ok');ready();}
 function invalidate(){ticket++;fetchController?.abort();fetchController=null;models=[];$('#aisModelBrowser').hidden=true;invalidateCheck();}
 function draw(){
   if(!state)return;
   for(const feature of ['assistant','prompt']){
     const selected=state.assignments[feature];
     $('#aisSummary'+feature).textContent=selected.configured?selected.model:t('尚未配置','Not configured');
     $('#aisSource'+feature).textContent=selected.configured?`${selected.name} · ${selected.protocol}`:selected.error||t('选择服务商或自定义连接','Choose a provider or custom connection');
   }
 }
 function apps(selected=''){
   const available=[...new Set((state?.connections||[]).filter(item=>item.source==='ccswitch').map(item=>item.app_type||'other'))];
   const ordered=[...Object.keys(appNames).filter(value=>available.includes(value)),...available.filter(value=>!appNames[value])];
   $('#aisAppType').innerHTML=ordered.map(value=>`<option value="${esc(value)}">${esc(appNames[value]||t('其他','Other'))}</option>`).join('')||`<option value="">${t('没有本机配置','No local profiles')}</option>`;
   if(ordered.includes(selected))$('#aisAppType').value=selected;
 }
 function choices(id=''){
   const source=$('#aisSource').value,query=$('#aisProviderSearch').value.trim().toLowerCase();
   $('#aisAppTypeWrap').hidden=source!=='ccswitch';$('#aisProviderWrap').hidden=source==='custom';
   if(source==='custom'){$('#aisProvider').innerHTML='<option value="custom">Custom</option>';$('#aisProvider').value='custom';return;}
   let rows=(state?.connections||[]).filter(item=>item.source===source&&(source!=='ccswitch'||(item.app_type||'other')===$('#aisAppType').value)&&(!query||`${item.name} ${item.model||''}`.toLowerCase().includes(query)));
   const all=rows.length;rows=rows.slice(0,12);
   const selected=state?.connections.find(item=>item.id===id);if(selected&&!rows.some(item=>item.id===id)&&all>12&&(!query||`${selected.name} ${selected.model||''}`.toLowerCase().includes(query))&&selected.source===source&&(source!=='ccswitch'||(selected.app_type||'other')===$('#aisAppType').value)){rows.pop();rows.push(selected);}
   $('#aisProvider').innerHTML=`<option value="">${t('选择连接','Choose a connection')}</option>`+rows.map(item=>`<option value="${esc(item.id)}" ${item.configured||item.editable?'':'disabled'}>${esc(item.name)}${item.configured?'':' · '+esc(item.reason||t('需要补充连接信息','Needs configuration'))}</option>`).join('');
   if(rows.some(item=>item.id===id))$('#aisProvider').value=id;
   $('#aisProviderCount').textContent=all>12?t(`共 ${all} 个连接，显示 12 个；搜索查找更多`,`${all} connections; showing 12. Search for more.`):t(`${all} 个连接`,`${all} connections`);
 }
 function choose(){
   editing=false;invalidate();const custom=$('#aisSource').value==='custom',selected=row();
   $('#aisCustomFields').hidden=!custom;$('#aisModel').value=selected?.model||'';$('#aisProtocol').value=selected?.protocol||'responses';
   $('#aisThinking').innerHTML='<option value="auto">'+t('自动','Auto')+'</option>';$('#aisThinking').value='auto';scheduleThinking();
   $('#aisName').value='';$('#aisUrl').value='';$('#aisKey').value='';$('#aisKey').placeholder=t('仅发送至所填服务商','Sent only to the provider');ready();
   status(t('选择模型后进行快速自检，通过即可保存。','Choose a model and pass a quick check to save.'));
 }
 function labels(){
   const label=(selector,zh,en)=>{const node=$(selector);if(node)node.textContent=t(zh,en);};
   label('#aisSource option[value=profile]','TokenLens 已保存连接','Saved TokenLens connections');label('#aisSource option[value=ccswitch]','CC Switch','CC Switch');label('#aisSource option[value=custom]','新建自定义连接','New custom connection');
   $('#aisSource').parentElement.querySelector('span').textContent=t('连接来源','Connection source');$('#aisAppType').parentElement.querySelector('span').textContent=t('应用类型','App type');
   label('label[for=aisProvider]',$('#aisSource').value==='ccswitch'?'服务商':'已保存的连接',$('#aisSource').value==='ccswitch'?'Provider':'Saved connections');label('#aisRefresh','刷新','Refresh');label('#aisFetch','获取模型','Fetch models');label('#aisCancel','取消','Cancel');
   for(const [id,zh,en] of [['aisName','连接名称','Connection name'],['aisUrl','API URL','API URL'],['aisKey','API Key','API key'],['aisProtocol','请求协议','Protocol'],['aisThinking','思考强度','Reasoning'],['aisModel','模型','Model']]){const field=$('#'+id).closest('.field');if(field?.querySelector('span'))field.querySelector('span').textContent=t(zh,en);}
   $('#aisKey').placeholder=editing&&row()?.has_key?t('留空保留已保存的密钥','Leave blank to keep the saved key'):t('仅发送至所填服务商','Sent only to the provider');
   $('#aisProviderSearch').placeholder=t('搜索连接名称或模型…','Search connection name or model…');$('#aisProviderSearch').setAttribute('aria-label',t('搜索连接','Search connections'));$('#aisSearch').placeholder=t('搜索模型…','Search models…');
 }
 function edit(){
   const selected=row();if(!selected?.editable||saving)return;
   if(editing){choose();return;}
   editing=true;invalidate();$('#aisCustomFields').hidden=false;$('#aisName').value=selected.name;$('#aisUrl').value=selected.base_url||'';$('#aisKey').value='';
   $('#aisKey').placeholder=selected.has_key?t('留空保留已保存的密钥','Leave blank to keep the saved key'):t('填写 API Key','Enter API key');ready();status(t('编辑后需重新自检；保存会更新这条连接。','Retest edited fields. Saving updates this connection.'));
 }
 async function refresh(){
   invalidate();const current=$('#aisProvider').value,version=ticket,opened=session;status(t('正在读取本机配置…','Reading local profiles…'));
   const value=await api('/api/ai-settings');if(version!==ticket||opened!==session||!$('#aiSettingsDialog').open)return;
   state=value;apps($('#aisAppType').value);choices(current);draw();if($('#aisProvider').value!==current)choose();else ready();status(t('配置已刷新，请快速自检后保存。','Profiles refreshed. Run a quick check before saving.'));
 }
 function list(){
   const q=$('#aisSearch').value.trim().toLowerCase();const filtered=models.filter(item=>`${item.id} ${item.group||''}`.toLowerCase().includes(q));
   $('#aisOptions').innerHTML=filtered.slice(0,200).map(item=>`<button type="button" role="option" data-model="${esc(item.id)}">${esc(item.id)}</button>`).join('')||`<p class="iq-dialog-note">${t('没有匹配模型','No matching models')}</p>`;
   $('#aisOptions').querySelectorAll('[data-model]').forEach(button=>button.onclick=()=>{$('#aisModel').value=button.dataset.model;$('#aisModelBrowser').hidden=true;invalidateCheck();status(t('模型已选择，请快速自检。','Model selected. Run a quick check.'));});
 }
 async function fetchModels(){
   if($('#aisFetch').disabled)return;invalidate();const version=ticket;fetchController=new AbortController();ready();status(t('正在获取模型…','Fetching models…'));
   try{const result=await api('/api/ai-settings/models',{method:'POST',body:body(),signal:fetchController.signal});if(version!==ticket||!$('#aiSettingsDialog').open)return;if(!result.ok)throw new Error(result.message||result.error||t('目录不可用，可填写模型 ID。','Catalog unavailable; enter a model ID.'));models=(result.models||[]).map(item=>typeof item==='string'?{id:item}:item);$('#aisSearch').value='';$('#aisModelBrowser').hidden=false;list();status(result.message||t(`已获取 ${models.length} 个模型`,`${models.length} models fetched`));}
   catch(error){if(version===ticket&&error.name!=='AbortError')status(error.message,true);}
   finally{if(version===ticket){fetchController=null;ready();}}
 }
 function close(){if(saving)return;session++;initializing=false;clearTimeout(thinkingTimer);thinkingTicket++;invalidate();editing=false;$('#aisKey').value='';$('#aisUrl').value='';$('#aisName').value='';$('#aiSettingsDialog').close();}
 async function open(feature){
   target=feature;editing=false;initializing=true;const opened=++session;invalidate();labels();$('#aiSettingsDialog').showModal();ready();$('#aisTitle').textContent=feature==='assistant'?t('选择 AI 助手模型','Choose AI Assistant model'):t('选择提示词优化模型','Choose Prompt Lab model');
   try{const value=await api('/api/ai-settings');if(opened!==session||!$('#aiSettingsDialog').open)return;state=value;draw();const selected=state.assignments[target],connection=state.connections.find(item=>item.id===selected.connection_id);$('#aisSource').value=connection?.source||'profile';$('#aisProviderSearch').value='';apps(connection?.app_type);choices(selected.connection_id);choose();labels();$('#aisModel').value=selected.model||'';$('#aisProtocol').value=selected.protocol||'responses';await loadThinking();const level=selected.thinking||'auto',select=$('#aisThinking');if(level&&![...select.options].some(option=>option.value===level))select.insertAdjacentHTML('beforeend',`<option value="${esc(level)}">${esc(english?level:thinkingLabels[level]||level)}</option>`);select.value=level;}
   catch(error){if(opened===session)status(error.message,true);}
   finally{if(opened===session){initializing=false;ready();$('#aisSource').focus();}}
 }
 async function check(){
   if(checking||saving||$('#aisCheck').disabled)return;invalidateCheck();const version=checkTicket,expected=signature();checking=true;checkController=new AbortController();$('#aisCheck').textContent=t('自检中…','Checking…');ready();status(t('正在自检，收到有效输出即通过，最多等待 10 秒…','Checking. First valid output passes; up to 10 seconds…'));
   try{const result=await api('/api/ai-settings/check',{method:'POST',body:body(),signal:checkController.signal});if(version!==checkTicket||!$('#aiSettingsDialog').open||expected!==signature())return;if(!result.ok)throw new Error(result.detail||result.title||t('自检未通过，请检查连接。','Check failed. Review the connection.'));checkedSignature=expected;checkExpires=Date.now()+result.expires_in*1000;$('#aisCheck').classList.add('iq-check-ok');$('#aisCheck').textContent=t('自检通过 · 重测','Passed · Retest');const ms=result.timing?.total_ms;status(t('自检通过，可以保存使用。','Check passed. Ready to save.')+(Number.isFinite(ms)?` · ${ms} ms`:''));expiryTimer=setTimeout(()=>{invalidateCheck();status(t('自检结果已过期，请重新自检。','Check expired. Please retest.'));},result.expires_in*1000);}
   catch(error){if(version===checkTicket&&error.name!=='AbortError'){checkedSignature='';status(error.message,true);$('#aisCheck').textContent=t('重新自检','Retry check');}}
   finally{if(version===checkTicket){checking=false;checkController=null;ready();}}
 }
 async function save(){
   if(saving)return;if($('#aisSave').disabled)return status(t('请先通过快速自检。','Pass a quick check first.'),true);const request=body();saving=true;ready();
   try{const value=await api('/api/ai-settings/save',{method:'POST',body:request});state=value;draw();if(onChange)await onChange(value);saving=false;close();toast(t('模型设置已保存','Model settings saved'),'ok');}
   catch(error){invalidateCheck();status(error.message,true);}
   finally{saving=false;ready();}
 }
 window.TLAISettings={
   init(deps){
     api=deps.api;toast=deps.toast;onChange=deps.onChange;document.querySelectorAll('[data-ai-configure]').forEach(button=>button.onclick=()=>open(button.dataset.aiConfigure));
     $('#aisClose').onclick=close;$('#aisCancel').onclick=close;$('#aisSave').onclick=save;$('#aisCheck').onclick=check;$('#aisEdit').onclick=edit;$('#aisRefresh').onclick=()=>refresh().catch(error=>status(error.message,true));$('#aisProvider').onchange=choose;
     $('#aisSource').onchange=()=>{$('#aisProviderSearch').value='';choices();choose();labels();};$('#aisAppType').onchange=()=>{$('#aisProviderSearch').value='';choices();choose();};$('#aisProviderSearch').oninput=()=>{const id=$('#aisProvider').value;choices(id);if($('#aisProvider').value!==id)choose();};
     $('#aisProtocol').onchange=()=>{invalidate();scheduleThinking();status(t('协议已改变，请重新自检。','Protocol changed. Please retest.'));};
     $('#aisThinking').onchange=()=>{status(t('思考强度已改变，保存后生效。','Reasoning level changed; it applies after saving.'));};
     $('#aisModel').oninput=()=>{invalidate();scheduleThinking();status(t('连接已修改，请重新自检。','Connection changed. Please retest.'));};
     ['aisName','aisUrl','aisKey'].forEach(id=>$('#'+id).oninput=()=>{invalidate();status(t('连接已修改，请重新自检。','Connection changed. Please retest.'));});
     $('#aisFetch').onclick=fetchModels;$('#aisSearch').oninput=list;$('#aiSettingsDialog').addEventListener('cancel',event=>{event.preventDefault();close();});
   },
   async render(language){english=language==='en-US';try{state=await api('/api/ai-settings');draw();}catch(error){toast(error.message,'err');}},
   language(value){english=value==='en-US';draw();labels();},open,selection(feature){return state?.assignments[feature];}
 };
})();
