/* Only public metadata reaches the browser. Draft choices are committed on Apply. */
(() => {
  'use strict';
  let api,onChange,providers=[],current=null,models=[],english=false,loaded=false;
  let generation=0,controller=null,loading=false,activeIndex=-1,revision=0;
  let catalogRequest=0,draftRevision=0,openSession=0;
  const $=s=>document.querySelector(s),t=(zh,en)=>english?en:zh;
  const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const appName={claude:'Claude Code',codex:'Codex',gemini:'Gemini',opencode:'OpenCode','claude-desktop':'Claude Desktop'};
  const storage='tl.iq.ccswitch.selection.v3';
  const selected=()=>current?{...current}:null;
  const provider=()=>providers.find(row=>row.id===$('#iqCCProvider').value);
  function labels(){
    $('#iqCCAppType').parentElement.querySelector('span').textContent=t('应用类型','App type');
    $('#iqCCProviderSearch').placeholder=t('搜索服务商…','Search providers…');$('#iqCCProviderSearch').setAttribute('aria-label',t('搜索服务商','Search providers'));
  }
  function commit(value){
    revision++;
    current=value?{...value}:null;
    try{if(current)localStorage.setItem(storage,JSON.stringify({ccswitch_id:current.ccswitch_id,model:current.model,protocol:current.protocol}));else localStorage.removeItem(storage);}catch(_){}
    if(onChange)onChange(selected());
  }
  function status(message,error=false){$('#iqCCStatus').textContent=message;$('#iqCCStatus').classList.toggle('error',error);}
  function ready(){const row=provider(),model=$('#iqCCModel').value.trim();$('#iqCCApply').disabled=loading || !row || !row.configured || !model || model.length>256;}
  function cancelFetch(){generation++;if(controller)controller.abort();controller=null;loading=false;$('#iqCCFetch').disabled=false;$('#iqCCFetch').classList.remove('loading');ready();}
  function popup(show){$('#iqCCPopup').hidden=!show;$('#iqCCToggle').setAttribute('aria-expanded',String(show));if(show){renderList();$('#iqCCSearch').focus();}}
  function close(){openSession++;cancelFetch();popup(false);$('#iqCCDialog').close();}
  function apply(){
    const row=provider();if(!row || !row.configured || $('#iqCCApply').disabled)return;
    commit({ccswitch_id:row.id,model:$('#iqCCModel').value.trim(),protocol:$('#iqCCProtocol').value,base_url:row.base_url,name:row.name,app_type:row.app_type,manual_model:false});close();
  }
  function populateApps(type=''){
    const groups=[...new Set(providers.map(row=>row.app_type))];
    const ordered=[...Object.keys(appName).filter(value=>groups.includes(value)),...groups.filter(value=>!appName[value])];
    $('#iqCCAppType').innerHTML=ordered.map(value=>`<option value="${esc(value)}">${esc(appName[value]||value)}</option>`).join('')||`<option value="">${t('没有本机配置','No local profiles')}</option>`;
    if(ordered.includes(type))$('#iqCCAppType').value=type;
  }
  function populate(id=''){
    const query=$('#iqCCProviderSearch').value.trim().toLowerCase(),type=$('#iqCCAppType').value;
    const matches=providers.filter(row=>row.app_type===type&&(!query||`${row.name} ${row.model||''}`.toLowerCase().includes(query)));
    const rows=matches.slice(0,12),selected=matches.find(row=>row.id===id);
    if(selected&&!rows.some(row=>row.id===id)){rows.pop();rows.push(selected);}
    $('#iqCCProvider').innerHTML=`<option value="">${t('选择服务商','Select a provider')}</option>`+rows.map(row=>`<option value="${esc(row.id)}" ${row.configured?'':'disabled'}>${esc(row.name)}${row.is_current?t(' · 当前',' · current'):''}${row.configured?'':` · ${esc(row.reason||t('未配置密钥','No API key'))}`}</option>`).join('');
    if(rows.some(row=>row.id===id&&row.configured))$('#iqCCProvider').value=id;
    $('#iqCCProviderCount').textContent=matches.length>12?t(`共 ${matches.length} 个服务商，显示 12 个；搜索查找更多`,`${matches.length} providers; showing 12. Search for more.`):t(`${matches.length} 个服务商`,`${matches.length} providers`);
  }
  function invalidateModels(){cancelFetch();models=[];$('#iqCCSearch').value='';popup(false);renderList();}
  function choose(){
    invalidateModels();const row=provider();
    $('#iqCCProtocol').value=row?row.protocol:'openai';
    $('#iqCCModel').value=row?(current&&current.ccswitch_id===row.id?current.model:row.model || ''):'';
    status(row?t('点击获取按钮，读取此服务商的模型列表。','Fetch the model list from this provider.'):t('先选择服务商，再选择请求协议。','Choose a provider and request protocol.'));ready();
  }
  function filtered(){const q=$('#iqCCSearch').value.trim().toLowerCase();return models.filter(row=>!q || `${row.id} ${row.group}`.toLowerCase().includes(q));}
  function pick(id){draftRevision++;$('#iqCCModel').value=id;popup(false);ready();$('#iqCCModel').focus();}
  function renderList(){
    const all=filtered(),rows=all.slice(0,200);activeIndex=-1;
    $('#iqCCCount').textContent=t(`${all.length} 个模型${all.length>200?' · 搜索可缩小范围':''}`,`${all.length} models${all.length>200?' · Search to narrow results':''}`);
    let group=null;
    $('#iqCCOptions').innerHTML=rows.length?rows.map(row=>{const heading=row.group!==group?`<div class="iq-model-group">${esc(row.group || t('可用模型','Available models'))}</div>`:'';group=row.group;return heading+`<button type="button" class="iq-model-option${row.id===$('#iqCCModel').value?' selected':''}" role="option" aria-selected="${row.id===$('#iqCCModel').value}" data-model="${esc(row.id)}">${esc(row.id)}</button>`;}).join(''):`<p class="iq-model-empty">${models.length?t('没有匹配的模型','No matching models'):t('获取列表后在这里选择；也可直接输入模型 ID。','Fetch models to choose here, or enter a model ID directly.')}</p>`;
    $('#iqCCOptions').querySelectorAll('[data-model]').forEach(button=>button.onclick=()=>pick(button.dataset.model));
  }
  function keyboard(event){
    if(event.key==='Escape'&&!$('#iqCCPopup').hidden){event.preventDefault();event.stopPropagation();popup(false);$('#iqCCToggle').focus();return;}
    if(event.key==='ArrowDown' || event.key==='ArrowUp'){
      event.preventDefault();if($('#iqCCPopup').hidden)popup(true);
      const options=[...$('#iqCCOptions').querySelectorAll('[data-model]')];if(!options.length)return;
      activeIndex=(activeIndex+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;
      options.forEach((button,index)=>button.classList.toggle('active',index===activeIndex));options[activeIndex].scrollIntoView({block:'nearest'});
    }else if(event.key==='Enter'&&!$('#iqCCPopup').hidden){event.preventDefault();const options=$('#iqCCOptions').querySelectorAll('[data-model]');if(options[activeIndex])pick(options[activeIndex].dataset.model);}
  }
  async function fetchModels(){
    const row=provider();if(!row || !row.configured){status(t('请先选择服务商','Select a provider first'),true);return;}
    cancelFetch();const request=++generation,protocol=$('#iqCCProtocol').value;controller=new AbortController();loading=true;
    $('#iqCCFetch').disabled=true;$('#iqCCFetch').classList.add('loading');ready();status(t('正在获取模型…','Fetching models…'));
    try{
      const result=await api('/api/ccswitch/models',{method:'POST',body:{ccswitch_id:row.id,ccswitch_protocol:protocol},signal:controller.signal});
      if(request!==generation || !$('#iqCCDialog').open)return;
      if(!result.ok)throw new Error(result.message || result.error || t('获取失败','Fetch failed'));
      models=(result.models || []).map(item=>typeof item==='string'?{id:item,group:''}:{id:item.id,group:item.group || ''}).filter(item=>item.id);renderList();
      status(result.message || (models.length?t(`已获取 ${models.length} 个模型，选择后点击“使用此模型”。`,`Fetched ${models.length} models. Choose one and apply.`):t('服务商返回了空列表，可直接输入模型 ID。','The provider returned an empty list. You can enter a model ID.')),!!result.partial);popup(true);
    }catch(error){if(request===generation&&error.name!=='AbortError'){models=[];renderList();status((error.body&&error.body.error) || error.message || t('获取失败，可直接输入模型 ID。','Fetch failed. You can enter a model ID.'),true);}}
    finally{if(request===generation){loading=false;controller=null;$('#iqCCFetch').disabled=false;$('#iqCCFetch').classList.remove('loading');ready();}}
  }
  async function refresh(){
    cancelFetch();const request=++catalogRequest,session=openSession,draft=draftRevision;
    const id=$('#iqCCProvider').value,model=$('#iqCCModel').value,protocol=$('#iqCCProtocol').value,type=$('#iqCCAppType').value;
    $('#iqCCRefresh').disabled=true;status(t('正在读取本机配置…','Reading local profiles…'));
    const valid=()=>request===catalogRequest&&session===openSession&&draft===draftRevision&&$('#iqCCDialog').open;
    try{
      const result=await api('/api/ccswitch/connections');if(!valid())return;
      providers=result.connections || [];loaded=true;populateApps(type||providers.find(row=>row.id===id)?.app_type);populate(id);choose();
      if(provider()){$('#iqCCModel').value=model;$('#iqCCProtocol').value=protocol;ready();}
      if(!providers.length)status(result.message || t('未找到配置，请先在 CC Switch 保存服务商。','No profiles found. Save a provider in CC Switch first.'));
    }catch(error){if(valid())status(error.message || t('读取失败，请重试','Read failed. Please retry.'),true);}
    finally{if(request===catalogRequest)$('#iqCCRefresh').disabled=false;}
  }
  function open(){
    openSession++;labels();$('#iqCCDialog').showModal();
    $('#iqCCProviderSearch').value='';populateApps(current?.app_type);
    populate(current?current.ccswitch_id:'');choose();if(current&&provider()){$('#iqCCProtocol').value=current.protocol;$('#iqCCModel').value=current.model;ready();}
    $('#iqCCAppType').focus();if(!loaded)refresh();
  }
  async function restoreSaved(){
    const initialRevision=revision;
    let saved;try{saved=JSON.parse(localStorage.getItem(storage) || 'null');}catch(_){return;}if(!saved || !saved.ccswitch_id || typeof saved.model!=='string')return;
    try{const result=await api('/api/ccswitch/connections');providers=result.connections || [];loaded=true;const row=providers.find(item=>item.id===saved.ccswitch_id&&item.configured);if(row&&!current&&revision===initialRevision&&!document.querySelector('.iq-connection-dialog[open]'))commit({ccswitch_id:row.id,model:saved.model.slice(0,256),protocol:['openai','responses','anthropic','gemini'].includes(saved.protocol)?saved.protocol:row.protocol,base_url:row.base_url,name:row.name,app_type:row.app_type,manual_model:false});}catch(_){}
  }
  window.TLCCSwitch={
    init(deps){
      api=deps.api;onChange=deps.onChange;
      $('#iqCCOpen').onclick=open;$('#iqCCClose').onclick=close;$('#iqCCCancel').onclick=close;$('#iqCCApply').onclick=apply;
      $('#iqCCRefresh').onclick=refresh;$('#iqCCProvider').onchange=()=>{draftRevision++;choose();};
      $('#iqCCAppType').onchange=()=>{draftRevision++;$('#iqCCProviderSearch').value='';populate();choose();};
      $('#iqCCProviderSearch').oninput=()=>{draftRevision++;const id=$('#iqCCProvider').value;populate(id);if($('#iqCCProvider').value!==id)choose();};
      $('#iqCCProtocol').onchange=()=>{draftRevision++;invalidateModels();status(t('协议已切换，请重新获取模型列表。','Protocol changed. Fetch the model list again.'));};
      $('#iqCCFetch').onclick=fetchModels;$('#iqCCToggle').onclick=()=>popup($('#iqCCPopup').hidden);
      $('#iqCCModel').oninput=()=>{draftRevision++;ready();};$('#iqCCSearch').oninput=renderList;
      $('#iqCCPicker').addEventListener('keydown',keyboard);
      $('#iqCCDialog').addEventListener('cancel',event=>{event.preventDefault();close();});
      $('#iqCCDialog').addEventListener('click',event=>{if(event.target===$('#iqCCDialog')){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left || event.clientX>rect.right || event.clientY<rect.top || event.clientY>rect.bottom)close();}});
      document.addEventListener('pointerdown',event=>{if(!$('#iqCCPicker').contains(event.target))popup(false);});
      restoreSaved();
    },
    selected,clear:()=>commit(null),restore:commit,open,refresh,fetchModels,refreshOnce(){},
    language(value){english=value==='en-US';labels();$('#iqCCDialogTitle').textContent=t('选择 CC Switch 模型','Choose a CC Switch model');$('#iqCCRefresh').textContent=t('刷新配置','Refresh profiles');$('#iqCCApply').textContent=t('使用此模型','Use this model');$('#iqCCCancel').textContent=t('取消','Cancel');renderList();}
  };
})();
