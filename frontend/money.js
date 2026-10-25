/* Display currency is shared; persisted amounts and model rates remain USD. */
(() => {
  'use strict';
  let language = 'zh-CN', rate = 7.2, selected = '';
  const formats = new Map();
  function configure(options = {}) {
    if (options.language) language = options.language;
    if (Number.isFinite(Number(options.rate)) && Number(options.rate) > 0) rate = Number(options.rate);
    if ('currency' in options) selected = /^(usd|cny)$/i.test(options.currency) ? options.currency.toUpperCase() : '';
  }
  const currency = () => selected || (language === 'en-US' ? 'USD' : 'CNY');
  function format(usd, options = {}) {
    if (usd == null || !Number.isFinite(Number(usd))) return '—';
    const unit = options.currency?.toUpperCase() || currency(), value = Number(usd) * (unit === 'CNY' ? rate : 1);
    const precision = options.precision ?? 6, key = `${language}:${precision}`;
    if (!formats.has(key)) formats.set(key, new Intl.NumberFormat(language, {minimumFractionDigits:2, maximumFractionDigits:precision}));
    return (unit === 'CNY' ? '¥' : '$') + formats.get(key).format(value);
  }
  function label(kind, english = language === 'en-US') {
    if (kind == null) return currency();
    return ({actual:english?'Actual':'实付', estimated:english?'Estimated':'估算', free:english?'Free':'免费', unknown:english?'Unpriced':'未定价'})[kind] || '';
  }
  function coverage(value = {}, english = language === 'en-US') {
    const count = Number(value.unknown_records || 0);
    return count ? (english ? `${count.toLocaleString('en-US')} unpriced records excluded` : `未覆盖 ${count.toLocaleString('zh-CN')} 条未定价记录`) : (english ? 'All records priced' : '全部记录已计价');
  }
  window.TLMoney = {configure, format, currency, label, coverage};
})();
