/* Overview status uses the dashboard snapshot; no polling or extra requests. */
(() => {
  'use strict';
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g,
    char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

  function render(data, scan, english, actions) {
    const host = document.getElementById('overviewStatus');
    if (!host || !data) return;
    const t = (zh, en) => english ? en : zh;
    const sources = data.sources || [];
    const issues = sources.filter(source => source.status === 'error' || source.status === 'partial');
    const summary = data.summary || {};
    const span = data.span || {};
    const running = !!(scan && scan.running);
    const hasRows = Number(summary.requests || summary.total) > 0;
    const lastScan = data.last_scan ? new Date(data.last_scan * 1000).toLocaleString(english ? 'en-US' : 'zh-CN') : t('尚未采集', 'Not scanned yet');
    const total = Number(summary.total || 0);
    const ledgerTotals = (data.ledger || {}).totals || {};
    const unpriced = Number(ledgerTotals.unpriced_tokens || 0);
    // The dashboard limits its model list. Do not extrapolate its prices to
    // models omitted by that limit; the management view has full coverage.
    const complete = Number(ledgerTotals.tokens) === total;
    const coverage = total && complete ? Math.max(0, Math.min(100, (total - unpriced) / total * 100)) : null;
    const coverageNote = complete
      ? t('当前单价可覆盖的 Token 比例；实际金额以记录费用为准', 'Token coverage of the current price list; recorded cost preserves collected amounts')
      : t('模型列表有显示上限，完整成本覆盖率见成本管理', 'The model list has a display limit. See Cost management for complete coverage.');
    const metrics = [
      `<span>${t('最近采集', 'Last collection')} <b>${escape(lastScan)}</b></span>`,
      `<button type="button" data-status-action="quality">${t('来源提示', 'Source issues')} <b data-countup>${issues.length}</b></button>`,
      `<button type="button" data-status-action="prices" title="${coverageNote}">${t('单价覆盖', 'Price coverage')} <b data-countup>${coverage == null ? '—' : coverage.toFixed(1) + '%'}</b></button>`,
    ];
    let message = '', detail = '', primary = '', action = '';
    if (!hasRows) {
      if (running) {
        message = t('正在整理你的本地用量', 'Collecting your local usage');
        detail = t('首次扫描会遍历历史日志。你可以继续使用其他工具，已完成的数据会逐步显示。', 'The first scan reads historical logs. You can keep working while collected data becomes available.');
      } else if (issues.length) {
        message = t('部分数据源需要处理', 'Some sources need attention');
        detail = t('打开数据源查看错误和修复方法。采集失败时保留上一次成功的数据。', 'Open Sources for errors and repair steps. Failed collection retains the last successful data.');
        primary = t('检查数据源', 'Inspect sources'); action = 'sources';
      } else if (Number(span.n) > 0) {
        message = t('这个时间范围还没有记录', 'No records in this range');
        detail = t('历史数据已在本机，可以切换到全部时间查看。', 'Historical data is available locally. Switch to All time to see it.');
        primary = t('查看全部时间', 'Show all time'); action = 'all';
      } else {
        message = t('从第一份用量报告开始', 'Start with your first usage report');
        detail = data.last_scan
          ? t('没有找到可读取的用量日志。先使用已支持的 AI 编程工具，或在设置中添加 API 日志目录，再重新扫描。', 'No readable usage logs were found. Use a supported AI coding tool or add an API log directory in Settings, then scan again.')
          : t('自动识别本机 AI 编程日志。先扫描数据，再设置月预算和自动采集间隔。', 'Detect local AI coding logs, scan usage, then set a monthly budget and collection interval.');
        primary = data.last_scan ? t('配置日志目录', 'Configure log folders') : t('扫描本地用量', 'Scan local usage');
        action = data.last_scan ? 'settings' : 'scan';
      }
    }
    const budget = data.budget || {};
    const alerts = [budget.tokens, budget.cost].filter(metric => metric && metric.limit && metric.ratio >= metric.warn_ratio);
    const budgetText = alerts.map(metric => {
      const used = metric.metric === 'cost' ? (window.TLMoney?.format(metric.used) ?? '$' + Number(metric.used).toFixed(2)) : Number(metric.used).toLocaleString(english ? 'en-US' : 'zh-CN') + ' tokens';
      return `${metric.over ? t('已超预算', 'Over budget') : t('接近预算', 'Approaching budget')}: ${used} (${(metric.ratio * 100).toFixed(1)}%)`;
    }).join(' · ');
    host.innerHTML = `<div class="overview-health">${metrics.join('')}<button type="button" data-status-action="settings">${t('预算与自动采集', 'Budget & collection')} →</button></div>` +
      (message ? `<div class="overview-guide"><div><h3>${message}</h3><p>${detail}</p></div>${primary ? `<button type="button" class="btn btn-primary" data-status-action="${action}">${primary}</button>` : ''}</div>` : '') +
      (budgetText ? `<div class="overview-budget" role="status"><span data-countup>${escape(budgetText)}</span><button type="button" class="btn btn-ghost btn-xs" data-status-action="insights">${t('查看预算', 'View budget')} →</button></div>` : '');
    host.querySelectorAll('[data-status-action]').forEach(button => {
      button.onclick = () => actions(button.dataset.statusAction);
    });
    if (window.TLMotion) window.TLMotion.animate(host);
  }
  window.TLOverview = {render};
})();
