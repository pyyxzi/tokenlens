"""Validate model commentary and bind it to computed evidence and actions."""
from __future__ import annotations

import re


VISUALS = {'trend', 'projects', 'models', 'tools', 'cost_breakdown', 'cache', 'none'}
DATA_METRICS = {
    'cost': ('已记录费用', 'USD'),
    'requests': ('调用次数', '次'),
    'tokens': ('Token 总量', 'tokens'),
    'input': ('输入 Token', 'tokens'),
    'output': ('输出 Token', 'tokens'),
    'reasoning': ('推理 Token', 'tokens'),
    'cache_read': ('缓存读取', 'tokens'),
    'cache_write': ('缓存写入', 'tokens'),
    'average_input': ('平均输入', 'tokens/次'),
    'cache_hit_rate': ('缓存命中率', '%'),
    'cost_per_request': ('已记录单次费用', 'USD/次'),
    'pricing_coverage': ('价格覆盖率', '%'),
}
_NUMERIC_CLAIM = re.compile(
    r'[$¥€]\s*\d|\d[\d,.]*\s*(?:%|美元|人民币|USD|次|条|Token|tokens|个|百分点)|'
    r'(?<![A-Za-z0-9._/\-])\d[\d,.]*(?![A-Za-z0-9._/\-])|'
    r'[零〇一二两三四五六七八九十百千万亿点]+\s*(?:美元|人民币|元|次|条|个|Token|tokens|百分点)|'
    r'百分之[零〇一二两三四五六七八九十百千万亿点]+', re.I)


def _text(value, fallback='', limit=2400):
    if not isinstance(value, str):
        return fallback
    value = value.strip()[:limit]
    # Numbers are rendered from local metrics and signal facts. Unknown model
    # prose is never allowed to quietly become an authoritative numeric claim.
    if _NUMERIC_CLAIM.search(value):
        return fallback
    return value or fallback


def _data_rows(snapshot, refs, metrics=None):
    """Resolve selected references to local values; never accept model values."""
    evidence = {row['id']: row for row in snapshot.get('evidence', [])}
    rows = []
    for identifier in dict.fromkeys(refs or []):
        item = evidence.get(identifier)
        if not item:
            continue
        current, previous = item['metrics']['current'], item['metrics']['previous']
        for metric in dict.fromkeys(metrics or DATA_METRICS):
            if metric not in DATA_METRICS:
                continue
            label, unit = DATA_METRICS[metric]
            cur, old = current.get(metric, 0), previous.get(metric, 0)
            if unit == '%':
                cur, old = round(cur * 100, 4), round(old * 100, 4)
            delta = round(cur - old, 6)
            rows.append({'evidence_id': identifier, 'evidence_title': item.get('title', identifier),
                         'kind': item.get('kind', ''), 'name': item.get('name', ''),
                         'metric': metric, 'label': label, 'unit': unit,
                         'current': cur, 'previous': old, 'delta': delta,
                         'delta_unit': '百分点' if unit == '%' else unit,
                         'growth': round(delta / old, 6) if old else None,
                         'current_records': current.get('records', 0), 'previous_records': previous.get('records', 0),
                         'current_cost_complete': current.get('cost_complete', True),
                         'previous_cost_complete': previous.get('cost_complete', True),
                         'filter': item.get('filter', {}), 'previous_filter': item.get('previous_filter', {})})
    return rows


def _data_refs(value, evidence):
    """Validate [{evidence_id, metrics:[...]}] selections, not supplied values."""
    if value in (None, ''):
        return []
    if not isinstance(value, list) or len(value) > 24:
        raise ValueError('AI 的数据引用格式无效。')
    refs = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('AI 的数据引用格式无效。')
        identifier = item.get('evidence_id', item.get('id'))
        metrics = item.get('metrics', list(DATA_METRICS))
        if not isinstance(identifier, str) or identifier not in evidence:
            raise ValueError('AI 引用了不存在的数据证据。')
        if not isinstance(metrics, list) or not metrics or len(metrics) > len(DATA_METRICS):
            raise ValueError('AI 的数据指标格式无效。')
        if any(not isinstance(metric, str) or metric not in DATA_METRICS for metric in metrics):
            raise ValueError('AI 引用了不支持的数据指标。')
        refs.append({'evidence_id': identifier, 'metrics': list(dict.fromkeys(metrics))})
    return refs


def normalize_document(parsed, snapshot, action, *, legacy_text=''):
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError('AI 返回的结构化结果无效。')
    evidence = {row['id']:row for row in snapshot['evidence']}
    signals = {row['id']:row for row in snapshot['signals']}
    titles = {'diagnose':'使用诊断', 'chat':'用量与费用解答', 'weekly':'AI 周报', 'monthly':'AI 月报', 'report':'AI 用量报告'}
    selected = snapshot['signals'][:6 if action in ('weekly', 'monthly', 'report') else 3]
    if action == 'chat':
        # A focused question must not become the same global three-item
        # diagnosis. Let the answer cite a relevant computed signal or section;
        # figures still come from its evidence and the scoped dashboard.
        requested = []
        for row in (parsed or {}).get('findings', []) if isinstance((parsed or {}).get('findings', []), list) else []:
            if isinstance(row,dict) and isinstance(row.get('signal_id'),str):requested.append(row['signal_id'])
        for row in (parsed or {}).get('sections', []) if isinstance((parsed or {}).get('sections', []), list) else []:
            if isinstance(row,dict) and isinstance(row.get('signal_ids'),list):requested.extend(row['signal_ids'])
        selected = [signals[identifier] for identifier in dict.fromkeys(
                        value for value in requested if isinstance(value,str)) if identifier in signals][:3]
    explanations = {}
    if parsed is not None:
        if not isinstance(parsed, dict):
            raise ValueError('AI 返回的结构化结果无效。')
        rows = parsed.get('findings', [])
        if not isinstance(rows, list):
            raise ValueError('AI 诊断格式无效，请重试。')
        if len(rows) > 12:
            rows = rows[:12]
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = row.get('signal_id')
            if identifier:
                if not isinstance(identifier,str) or identifier not in signals:
                    raise ValueError('AI 未提供可核对的信号与证据引用，请重试。')
                explanations[identifier] = _text(row.get('interpretation'), 'AI 未提供可核对的解释；请以统计事实和明细为准。')
            else:
                refs = row.get('evidence_refs', row.get('evidence_ids', []))
                if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
                    raise ValueError('AI 未提供可核对的证据引用，请重试。')
                if refs:
                    matching = next((item for item in selected if any(ref in item['evidence_ids'] for ref in refs)), None)
                    if matching:
                        explanations.setdefault(matching['id'], _text(row.get('interpretation'), '请对照统计事实进一步核对实际任务。'))
        refs = parsed.get('evidence_refs', [])
        if not isinstance(refs, list) or any(not isinstance(ref,str) or ref not in evidence for ref in refs):
            raise ValueError('AI 返回了不存在的证据引用。')
    findings, actions = [], []
    for signal in selected:
        finding = {key:signal[key] for key in ('id','kind','title','fact','next_step','evidence_ids','filter','previous_filter','severity','values')}
        finding['signal_id'] = signal['id']
        finding['evidence_refs'] = signal['evidence_ids']
        finding['actions'] = signal.get('actions', [])
        finding['interpretation'] = explanations.get(signal['id'], '')
        finding['interpretation_source'] = 'ai' if signal['id'] in explanations else 'local'
        finding['data_rows'] = _data_rows(snapshot, signal['evidence_ids'][:3],
                                        ['cost', 'requests', 'average_input', 'cache_hit_rate'])
        findings.append(finding)
        actions.append({'signal_id':signal['id'], 'label':signal['next_step'],
                        'evidence_refs':signal['evidence_ids'], 'filter':signal['filter'],
                        'previous_filter':signal['previous_filter'],
                        'target':signal['actions'][0]['target'] if signal.get('actions') else 'records'})
    sections = []
    if parsed is not None:
        rows = parsed.get('sections', [])
        if not isinstance(rows, list):
            raise ValueError('AI 返回的报告章节格式无效。')
        for row in rows[:6]:
            if not isinstance(row, dict):
                continue
            refs = row.get('evidence_refs', [])
            ids = row.get('signal_ids', [])
            if (not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in evidence for ref in refs)
                    or not isinstance(ids,list) or any(not isinstance(identifier,str) or identifier not in signals for identifier in ids)):
                raise ValueError('AI 返回了不存在的章节依据。')
            # An unreferenced qualitative paragraph can be conversational advice,
            # but cannot be presented as a finding derived from user data.
            data_refs = _data_refs(row.get('data_refs'), evidence)
            refs = list(dict.fromkeys(refs + [item['evidence_id'] for item in data_refs] +
                                     [ref for identifier in ids for ref in signals[identifier]['evidence_ids']]))
            body = _text(row.get('text', row.get('body')), '请以展示的本地统计与证据明细为准。', 4800)
            visual = row.get('visual')
            if not data_refs:
                defaults = ['cache_hit_rate', 'cache_read', 'average_input'] if visual == 'cache' else ['cost', 'requests', 'tokens']
                data_refs = [{'evidence_id': ref, 'metrics': defaults} for ref in refs[:3]]
            data_rows = []
            used = set()
            for item in data_refs:
                for data in _data_rows(snapshot, [item['evidence_id']], item['metrics']):
                    identity = (data['evidence_id'], data['metric'])
                    if identity not in used:
                        data_rows.append(data); used.add(identity)
            sections.append({'heading':_text(row.get('heading'), '分析说明', 200),
                             'text':body, 'body':body,
                             'evidence_refs':list(dict.fromkeys(refs))[:12], 'signal_ids':list(dict.fromkeys(ids))[:8],
                             'visual':visual if isinstance(visual,str) and visual in VISUALS else 'none',
                             'data_refs':data_refs, 'data_rows':data_rows[:72]})
    if not sections:
        # A focused chat without sections must not acquire unrelated global
        # boilerplate. Its selected findings already carry facts and data.
        if action != 'chat':
            sections = [{'heading':'本期与对照周期',
                         'text':'本表展示已采集记录的费用和用量变化；分析时应同时核对调用次数与平均输入，避免把调用增长直接归因于单次价格变化。',
                         'evidence_refs':['E1'], 'signal_ids':[], 'visual':'none',
                         'data_refs':[{'evidence_id':'E1','metrics':['cost','requests','average_input','cache_hit_rate','pricing_coverage']}],
                         'data_rows':_data_rows(snapshot, ['E1'], ['cost','requests','average_input','cache_hit_rate','pricing_coverage'])}]
    for section in sections:
        section['body'] = section['text']
    current, previous = snapshot['current'], snapshot['previous']
    if not current['records'] and not previous['records']:
        fallback = '所选范围及对照周期均无已采集记录，暂不能进行用量与费用分析。'
    elif current['unknown_records']:
        fallback = '费用记录尚未覆盖全部用量，比较消费前应先核对未计价部分。'
    elif not previous['records']:
        fallback = '已整理本期用量；所选对照周期尚无记录，暂不能判断增长趋势。'
    else:
        direction = '增加' if current['cost'] > previous['cost'] else '减少' if current['cost'] < previous['cost'] else '持平'
        fallback = '本期已记录费用相较上一等长周期'+direction+'，可对照调用量、输入与模型组合核对变化来源。'
    summary = _text((parsed or {}).get('summary', (parsed or {}).get('text')), fallback, 1200)
    document = {'schema_version':1, 'heading':_text((parsed or {}).get('heading'), titles[action], 200),
                'summary':summary, 'sections':sections, 'findings':findings, 'actions':actions,
                'evidence_refs':list(dict.fromkeys([ref for row in findings for ref in row['evidence_ids']]+
                                                  [ref for row in sections for ref in row['evidence_refs']]+
                                                  (parsed or {}).get('evidence_refs', []))),
                'range':snapshot.get('range',''), 'bounds':snapshot.get('bounds',{}),
                'limitations':snapshot['limitations'], 'origin':'structured' if parsed is not None else 'legacy',
                'legacy_text':legacy_text[:100000] if parsed is None and legacy_text else ''}
    return document


def document_text(document):
    """Human-readable export and conversational history; UI uses the schema."""
    lines = ['# '+document['heading'], '', document['summary']]
    if document.get('range'):
        lines += ['', '统计范围：'+document['range']]
    def number(value, unit, signed=False):
        text = format(abs(value) if signed else value, ',.6f').rstrip('0').rstrip('.')
        if signed:
            text = ('+' if value > 0 else '-' if value < 0 else '') + text
        return text + (' '+unit if unit else '')
    def cell(value):
        return str(value or '').replace('|', '\\|').replace('\n', ' ')
    for row in document['findings']:
        lines += ['', '## '+row['title'], row['fact'], row['interpretation'],
                  '下一步：'+row['next_step'], '依据：'+', '.join('['+ref+']' for ref in row['evidence_ids'])]
    for row in document['sections']:
        lines += ['', '## '+row['heading'], row['text']]
        if row.get('data_rows'):
            lines += ['', '| 数据对象 | 指标 | 本期 | 上期 | 变化 |', '| --- | --- | ---: | ---: | ---: |']
            for data in row['data_rows']:
                lines.append('| '+ ' | '.join([cell(data['evidence_title'])+' ['+data['evidence_id']+']',
                    cell(data['label']), number(data['current'],data['unit']),
                    number(data['previous'],data['unit']), number(data['delta'],data.get('delta_unit',data['unit']),True)]) +' |')
    lines += ['', '## 统计边界', *('- '+line for line in document['limitations'])]
    return '\n'.join(lines)
