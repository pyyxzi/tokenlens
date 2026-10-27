/* Input shares use normalized token fields, never output or request counts. */
(() => {
  'use strict';
  const finite = value => (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '')) && Number.isFinite(Number(value));
  const nonnegative = value => finite(value) && Number(value) >= 0;
  function inputTotal(row = {}) {
    if (finite(row.input_total)) return nonnegative(row.input_total) ? Number(row.input_total) : null;
    const fields = ['input', 'cache_read', 'cache_write'];
    if (!fields.every(key => nonnegative(row[key]))) return null;
    const total = fields.reduce((sum, key) => sum + Number(row[key]), 0);
    return Number.isFinite(total) ? total : null;
  }
  function cacheRatio(row = {}) {
    if ('cache_hit_ratio' in row) return finite(row.cache_hit_ratio) ? Math.max(0, Math.min(1, Number(row.cache_hit_ratio))) : null;
    const total = inputTotal(row);
    if (!(total > 0) || !nonnegative(row.cache_read)) return null;
    const ratio = Number(row.cache_read) / total;
    return Number.isFinite(ratio) ? Math.min(1, ratio) : null;
  }
  function cacheLabel(row, english = false) {
    const ratio = cacheRatio(row);
    return ratio === null ? (english ? 'N/A' : '—') : (ratio * 100).toFixed(1) + '%';
  }
  function cacheTitle(row = {}, english = false) {
    const ratio = cacheRatio(row);
    if (ratio === null) return english ? 'No input token data in this scope.' : '当前范围没有可计算的输入 Token 数据。';
    return english
      ? 'Cache-read tokens ÷ all input tokens (uncached + cache read + cache write). Based on collected fields; not a request hit rate or a cost-saving rate.'
      : '缓存读取 Token ÷ 全部输入 Token（未缓存输入 + 缓存读 + 缓存写）。基于已采集字段，不代表请求命中率或省费比例。';
  }
  function averageTokens(row = {}) {
    if ('avg_tokens_per_request' in row) return nonnegative(row.avg_tokens_per_request) ? Number(row.avg_tokens_per_request) : null;
    if (!finite(row.requests) || !(Number(row.requests) > 0) || !nonnegative(row.total)) return null;
    const average = Number(row.total) / Number(row.requests);
    return Number.isFinite(average) ? average : null;
  }
  window.TLUsage = {inputTotal, cacheRatio, cacheLabel, cacheTitle, averageTokens};
})();
