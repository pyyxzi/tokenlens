/* Native IPC adapter; the webview never receives a local service credential. */
(() => {
  'use strict';
  const native = window.__TAURI__;
  if (!native?.core?.invoke || !native?.core?.Channel) return;
  const {invoke, Channel} = native.core;
  const originalFetch = window.fetch.bind(window);
  const MAX_BUFFERED_BYTES = 1024 * 1024;
  let hidden = false;
  window.TLDesktop = {get hidden() { return hidden; }};
  let serviceState = {state:'connecting', message:'正在连接本地服务…', generation:0};
  window.TLService = {get state() { return {...serviceState}; }};

  function setServiceState(value) {
    const state = value?.state;
    if (!['connecting','recovering','ready','unavailable'].includes(state)) return;
    serviceState = {state, message:String(value.message || ''),
      generation:Number.isSafeInteger(value.generation) ? value.generation : serviceState.generation};
    document.dispatchEvent(new CustomEvent('tl-service-state', {detail:{...serviceState}}));
  }

  function cancelled() { return new DOMException('Request cancelled', 'AbortError'); }
  function externalUrl(value) {
    if (value.length > 4096 || /[\s\u0000-\u001f\u007f-\u009f\\]/u.test(value) || !/^https?:\/\//i.test(value)) return null;
    const authority = value.split('://')[1].split(/[/?#]/)[0];
    if (!authority || authority.includes('@')) return null;
    try {
      const url = new URL(value);
      return ['http:','https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }
  function bytes(encoded) {
    const decoded = atob(encoded);
    const data = new Uint8Array(decoded.length);
    for (let index = 0; index < decoded.length; index++) data[index] = decoded.charCodeAt(index);
    return data;
  }

  window.fetch = async (input, options = {}) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.href : '';
    if (!path.startsWith('/api/')) return originalFetch(input, options);
    const signal = options.signal;
    if (signal?.aborted) throw cancelled();
    const id = crypto.randomUUID();
    let controller, finished = false, buffered = 0, endPending = false;
    const queue = [];
    const channel = new Channel();
    const release = () => signal?.removeEventListener('abort', abort);
    function fail(error) {
      if (finished) return;
      finished = true; release(); queue.length = 0;
      controller.error(error);
      invoke('service_cancel', {id}).catch(() => {});
    }
    function drain() {
      if (finished) return;
      while (queue.length && controller.desiredSize > 0) {
        const chunk = queue.shift(); buffered -= chunk.data.byteLength;
        controller.enqueue(chunk.data);
        // Consume acknowledgements bound Rust's queued IPC data too.
        invoke('service_ack', {id, seq:chunk.seq}).catch(() => {
          fail(new Error('本地响应确认失败，请重新加载当前页面'));
        });
      }
      if (endPending && !queue.length) {
        finished = true; release(); controller.close();
      }
    }
    function abort() { fail(cancelled()); }
    const body = new ReadableStream({
      start(value) { controller = value; },
      pull() { drain(); },
      cancel() {
        if (finished) return;
        finished = true; release(); queue.length = 0;
        invoke('service_cancel', {id}).catch(() => {});
      }
    }, {highWaterMark:64 * 1024, size(value) {return value.byteLength;}});
    channel.onmessage = message => {
      if (finished) return;
      try {
        if (message.kind === 'chunk') {
          const data = bytes(message.data);
          buffered += data.byteLength;
          if (buffered > MAX_BUFFERED_BYTES) throw new Error('本地响应过大，请缩小查询范围');
          queue.push({data,seq:message.seq}); drain();
        } else if (message.kind === 'end') {
          endPending = true; drain();
        } else if (message.kind === 'error') {
          fail(new Error(message.message || '本地响应中断，请稍后重试'));
        }
      } catch (error) { fail(error); }
    };
    signal?.addEventListener('abort', abort, {once:true});
    try {
      const response = await invoke('service_request', {
        id, path, method:(options.method || 'GET').toUpperCase(),
        body:options.body ?? null, onChunk:channel
      });
      if (signal?.aborted) { invoke('service_cancel', {id}).catch(() => {}); throw cancelled(); }
      if (!path.startsWith('/api/desktop/') && path !== '/api/pick-folder' && path !== '/api/shutdown'
          && serviceState.state !== 'ready') {
        setServiceState({state:'ready', message:'本地服务已连接'});
      }
      return new Response(body, {status:response.status, headers:response.headers});
    } catch (error) {
      fail(error instanceof Error ? error : new Error(String(error)));
      // An unread body must not keep an abort/error rejection alive.
      body.cancel().catch(() => {});
      throw signal?.aborted ? cancelled() : error instanceof Error ? error : new Error(String(error));
    }
  };

  window.TLNative = {
    download(path, options = {}) {
      return invoke('service_download', {
        path, method:(options.method || 'GET').toUpperCase(), body:options.body ?? null
      });
    },
    save(name, contents) { return invoke('save_document', {name, contents}); }
  };
  function externalClick(event) {
    if (event.defaultPrevented || (event.type === 'click' ? event.button !== 0 : event.button !== 1)) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download')) return;
    const value = link.getAttribute('href') || '';
    if (!value || value.startsWith('#') || value.startsWith('/')) return;
    event.preventDefault();
    const url = externalUrl(value);
    // Only an actual user activation opens a system browser. Generated model
    // text, programmatic clicks and navigation remain confined to the app.
    if (!event.isTrusted || !url) return;
    invoke('open_external', {url}).catch(error => {
      document.dispatchEvent(new CustomEvent('tl-native-error', {detail:{message:String(error)}}));
    });
  }
  document.addEventListener?.('click', externalClick, true);
  document.addEventListener?.('auxclick', externalClick, true);
  native.event.listen('service-state', event => setServiceState(event.payload)).catch(() => {});
  native.event.listen('native-state', event => {
    const state = event.payload || {};
    if (hidden !== !state.visible) {
      hidden = !state.visible;
      document.documentElement.classList.toggle('desktop-hidden', hidden);
      document.dispatchEvent(new Event('visibilitychange'));
    }
    if (['overview','settings','records'].includes(state.view)) {
      document.dispatchEvent(new CustomEvent('tl-desktop-navigate', {detail:state.view}));
    }
  }).catch(() => {});
})();
