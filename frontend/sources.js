/* Local source assets and file import. No remote images or file-path API. */
(() => {
  'use strict';
  const ICONS = {
    'claude-code':'claude','claude-desktop':'claude',codex:'codex','gemini-cli':'gemini-cli',
    'deepseek-harness':'deepseek',opencode:'opencode',zcode:'zhipu',kimi:'kimi',
    cline:'cline',continue:'continue.png',copilot:'githubcopilot','copilot-cli':'githubcopilot',lmstudio:'lmstudio',
    ollama:'ollama',aider:'aider.png',cursor:'cursor',trae:'trae',windsurf:'windsurf','cherry-studio':'cherry',
    chatbox:'chatbox.png',codebuddy:'codebuddy','jetbrains-ai':'jetbrains',zed:'zedindustries',
    kiro:'kiro',qoder:'qoder','qoder-work':'qoder',warp:'warp',pi:'pi',goose:'goose',
    'api-log':'import',imported:'import'
  };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function html(id, fallback='?') {
    const file = ICONS[id];
    return file ? `<span class="tl-source-icon" aria-hidden="true"><img src="./source-icons/${file.includes('.')?file:file+'.svg'}" width="28" height="28" alt="" loading="lazy" decoding="async"></span>`
      : `<span class="tl-source-icon tl-source-monogram" aria-hidden="true">${esc(fallback)}</span>`;
  }
  window.TLSourceIcons = {html};
  let deps, preview = null, busy = false, host, hostLanguage;
  const en = () => deps?.isEn?.() || false;
  const text = (zh, english) => en() ? english : zh;
  const formats = [['auto','自动识别','Auto detect'],['csv','CSV 用量表','CSV usage'],['api','OpenAI / Anthropic API','OpenAI / Anthropic API'],
    ['claude-code','Claude Code','Claude Code'],['codex','Codex CLI','Codex CLI'],['gemini-cli','Gemini CLI','Gemini CLI'],
    ['copilot-cli','GitHub Copilot CLI','GitHub Copilot CLI'],['pi','Pi Coding Agent','Pi Coding Agent']];
  function status(message, error=false) {
    const node = host?.querySelector('.src-import-status');
    if (node) {node.textContent=message; node.classList.toggle('is-error',error);}
  }
  function setBusy(value) {
    busy=value;
    host?.querySelectorAll('button,input,select').forEach(node=>node.disabled=value);
    host?.setAttribute('aria-busy',String(value));
  }
  async function discard() {
    if (preview?.token) await deps.api('/api/sources/import/discard',{method:'POST',body:{token:preview.token}}).catch(()=>{});
    preview=null;
    const node=host?.querySelector('.src-import-preview');if(node){node.innerHTML='';node.hidden=true;}
  }
  async function encode(file) {
    // FileReader performs native base64 encoding without a byte-by-byte JS loop.
    return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error(text('文件读取失败','Unable to read file')));reader.readAsDataURL(file);});
  }
  function drawPreview(value) {
    const target=host.querySelector('.src-import-preview');
    target.hidden=false;
    const format=val=>Number(val||0).toLocaleString(en()?'en-US':'zh-CN');
    target.innerHTML=`<div class="src-import-preview-head"><strong>${esc(value.filename)}</strong><span>${esc(value.format.toUpperCase())}</span></div>
      <div class="src-import-metrics"><span>${text('记录','Records')} <b>${format(value.records)}</b></span><span>Tokens <b>${format(value.tokens)}</b></span><span>${text('记录费用','Recorded cost')} <b>$${Number(value.cost_usd||0).toFixed(4)}</b></span></div>
      <div class="src-import-sample"><table><thead><tr><th>${text('模型','Model')}</th><th>${text('输入','Input')}</th><th>${text('输出','Output')}</th><th>${text('缓存','Cache')}</th><th>${text('费用','Cost')}</th></tr></thead><tbody>${value.sample.map(row=>`<tr><td>${esc(row.model)}</td><td>${format(row.input)}</td><td>${format(row.output)}</td><td>${format(Number(row.cache_read||0)+Number(row.cache_write||0))}</td><td>${row.cost_kind==='unknown'?'—':'$'+Number(row.cost||0).toFixed(4)}</td></tr>`).join('')}</tbody></table></div>
      <p class="src-import-note">${text('仅展示前 5 条。确认后合并到统计，重复记录自动跳过；原始提示词、回复和密钥不会保存。','First 5 rows. Confirm to merge into usage totals; duplicate records are skipped. Prompts, responses and credentials are discarded.')}</p>
      <div class="src-import-actions"><button type="button" class="btn btn-primary" data-import-commit>${text('确认导入','Confirm import')}</button><button type="button" class="btn btn-ghost" data-import-cancel>${text('取消','Cancel')}</button></div>`;
  }
  async function selectFile(file) {
    if (!file || busy) return;
    if (file.size<1 || file.size>32*1024*1024) {status(text('请选择 32 MB 以内的用量文件','Choose a usage file up to 32 MB'),true);return;}
    setBusy(true);status(text('正在验证用量文件…','Validating usage file…'));
    try {
      await discard();
      const format=host.querySelector('[data-import-format]').value;
      preview=await deps.api('/api/sources/import/preview',{method:'POST',body:{filename:file.name,format,data:await encode(file)}});
      drawPreview(preview);status(text('验证完成，请确认预览后导入','Validated. Review the preview before importing.'));
    } catch(error) {status(error.message||String(error),true);}
    finally {setBusy(false);const input=host.querySelector('[data-import-file]');if(input)input.value='';}
  }
  function render(target) {
    target=target||document.querySelector('#v-sources');
    if(!target || !deps)return;
    if (host?.isConnected && (hostLanguage === en() || busy)) return;
    const selectedFormat = host?.querySelector('[data-import-format]')?.value || 'auto';
    if (host?.isConnected) host.remove();
    hostLanguage = en();
    host=document.createElement('section');host.className='src-import';host.setAttribute('aria-label',text('导入数据源','Import source'));
    host.innerHTML=`<div class="src-import-top"><div><h3>${text('导入用量数据','Import usage data')}</h3><p>${text('合并账单导出、API 日志或其他电脑的会话用量。仅在本机处理。','Merge billing exports, API logs or sessions from another device. Files stay local.')}</p></div>
      <div class="src-import-controls"><label><span class="sr-only">${text('文件格式','File format')}</span><select data-import-format>${formats.map(([value,zh,english])=>`<option value="${value}">${esc(en()?english:zh)}</option>`).join('')}</select></label><button type="button" class="btn btn-primary" data-import-choose><i class="ico i-upload"></i>${text('选择文件','Choose file')}</button><input data-import-file type="file" accept=".csv,.json,.jsonl,.ndjson" hidden></div></div>
      <details class="src-import-help"><summary>${text('支持哪些文件？','Supported files')}</summary><p>${text('支持 CSV、JSON、JSONL；OpenAI / Anthropic 调用日志；Claude Code、Codex、Gemini CLI、GitHub Copilot CLI 和 Pi 会话。单文件最多 32 MB / 10 万条。每条须有时间、模型和 token 用量。CSV 可使用 timestamp,model,input,output,cache_read,cache_write,cost_usd,request_id。','CSV, JSON, JSONL; OpenAI / Anthropic logs; Claude Code, Codex, Gemini CLI, GitHub Copilot CLI and Pi sessions. Maximum 32 MB / 100,000 records. Each record needs timestamp, model and token usage. CSV columns: timestamp,model,input,output,cache_read,cache_write,cost_usd,request_id.')}</p><p>${text('云端订阅或 premium request 单位不能直接当成美元。没有实报费用的记录会标为估算或未知。','Subscription and premium-request units are not USD. Records without reported cost remain estimated or unknown.')}</p></details>
      <p class="src-import-status" role="status" aria-live="polite"></p><div class="src-import-preview" hidden></div>`;
    const anchor=target.querySelector('#srcGrid');
    if(anchor)anchor.before(host);else target.prepend(host);
    host.querySelector('[data-import-format]').value = selectedFormat;
    if(preview) drawPreview(preview);
    host.addEventListener('change',event=>{if(event.target.matches('[data-import-file]'))selectFile(event.target.files?.[0]);});
    host.addEventListener('click',async event=>{
      if(event.target.closest('[data-import-choose]'))host.querySelector('[data-import-file]').click();
      if(event.target.closest('[data-import-cancel]')){await discard();status('');}
      if(event.target.closest('[data-import-commit]') && preview && !busy){
        setBusy(true);status(text('正在合并用量…','Merging usage…'));
        try{const result=await deps.api('/api/sources/import/commit',{method:'POST',body:{token:preview.token}});preview=null;host.querySelector('.src-import-preview').hidden=true;
          const message=result.imported?text(`已导入 ${result.imported.toLocaleString()} 条记录`,`Imported ${result.imported.toLocaleString()} records`):text('这些记录已经导入，已跳过重复数据','These records are already imported; duplicates skipped');status(message);deps.toast?.(message,'ok');await deps.refresh?.();
        }catch(error){status(error.message||String(error),true);}finally{setBusy(false);}
      }
    });
  }
  window.TLLSources={init(options){deps=options;},render,selectFile};
})();
