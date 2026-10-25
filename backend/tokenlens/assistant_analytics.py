"""Read-only arithmetic and actionable evidence for the AI workspace.

Models receive a bounded projection of this snapshot. They can explain its
signals; they cannot replace arithmetic, filters, or action destinations.
"""
from __future__ import annotations

import datetime as dt
import time

from .aggregator import Aggregator, TOTAL_EXPR, range_bounds
from .export_snapshot import export_snapshot
from .finance import project_cost_basis


KINDS = ('actual', 'estimated', 'unknown', 'free')
SUMS = ('COUNT(*) records,COALESCE(SUM(requests),0) requests,'
        'COALESCE(SUM('+TOTAL_EXPR+'),0) tokens,' +
        ','.join('COALESCE(SUM('+key+'),0) '+key
                 for key in ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'cost')) +
        ",COALESCE(SUM(CASE WHEN cost_kind='actual' THEN cost ELSE 0 END),0) actual_cost" +
        ",COALESCE(SUM(CASE WHEN cost_kind='estimated' THEN cost ELSE 0 END),0) estimated_cost" +
        ",COALESCE(SUM(CASE WHEN cost_kind='unknown' THEN 1 ELSE 0 END),0) unknown_records" +
        ",COALESCE(SUM(CASE WHEN cost_kind='unknown' THEN requests ELSE 0 END),0) unknown_requests" +
        ",COALESCE(SUM(CASE WHEN cost_kind='unknown' THEN "+TOTAL_EXPR+' ELSE 0 END),0) unknown_tokens' +
        ",COALESCE(SUM(CASE WHEN cost_kind<>'unknown' THEN "+TOTAL_EXPR+' ELSE 0 END),0) known_tokens')


def metrics(row=None):
    row = row or {}
    keys = ('records', 'requests', 'tokens', 'input', 'output', 'reasoning',
            'cache_read', 'cache_write', 'cost', 'unknown_records',
            'unknown_requests', 'unknown_tokens', 'known_tokens', 'actual_cost', 'estimated_cost')
    value = {key: row.get(key) or 0 for key in keys}
    value['cost'] = round(float(value['cost']), 6)
    input_total = value['input']+value['cache_read']+value['cache_write']
    value['average_input'] = round(input_total/value['requests'], 2) if value['requests'] else 0
    value['cache_hit_rate'] = round(value['cache_read']/input_total, 6) if input_total else 0
    value['pricing_coverage'] = round(value['known_tokens']/value['tokens'], 6) if value['tokens'] else (0 if value['unknown_records'] else 1)
    value['cost_per_request'] = round(value['cost']/value['requests'], 6) if value['requests'] else 0
    value['known_requests'] = value['requests']-value['unknown_requests']
    value['known_cost_per_request'] = round(value['cost']/value['known_requests'], 6) if value['known_requests'] else None
    value['cost_complete'] = not bool(value['unknown_records'])
    return value


def change(current, previous):
    delta = round(current-previous, 6)
    state = 'unchanged' if delta == 0 else ('new' if previous == 0 else ('up' if delta > 0 else 'down'))
    return {'current': current, 'previous': previous, 'delta': delta,
            'growth': round(delta/previous, 6) if previous else None, 'state': state}


def comparison(current, previous):
    result = {key: change(current[key], previous[key]) for key in
              ('cost', 'requests', 'tokens', 'average_input', 'cost_per_request', 'pricing_coverage', 'cache_hit_rate')}
    result['cache_hit_rate']['delta_pp'] = round((current['cache_hit_rate']-previous['cache_hit_rate'])*100, 4)
    result['pricing_coverage']['delta_pp'] = round((current['pricing_coverage']-previous['pricing_coverage'])*100, 4)
    result['cost']['complete'] = current['cost_complete'] and previous['cost_complete']
    return result


def _dollars(value):
    return '$'+format(value, ',.4f')


def _num(value):
    return format(value, ',.2f').rstrip('0').rstrip('.')


def _fact(current, previous):
    delta = current['cost']-previous['cost']
    return (f"本期已记录费用 {_dollars(current['cost'])}，上一周期 {_dollars(previous['cost'])}，"
            f"变化 {'+' if delta >= 0 else '-'}{_dollars(abs(delta))}；调用 {_num(current['requests'])} 次，"
            f"平均输入 {_num(current['average_input'])} Token，缓存命中率 {current['cache_hit_rate']*100:.2f}%。")


def build_snapshot(app, rng='7d', filters=None):
    # Use the same filter implementation as records and exports, including
    # normalized model names, managed project identities and blank projects.
    basis = (filters or {}).get('cost_basis') or 'total_cost'
    if basis not in ('total_cost', 'historical_cost', 'repriced_cost'):
        raise ValueError('计价方式无效。')
    filters = {key: value for key, value in (filters or {}).items()
               if key in ('tool', 'model', 'project', 'project_paths', 'q', 'from', 'to') and value}
    start, end, _, _ = range_bounds(rng)
    with export_snapshot(app.store) as store:
        view = Aggregator(store, app.cfg).filtered(filters)
        # Explicit record dates become the analysis period itself; keeping
        # them in the common predicate would erase the comparison period.
        lo, hi = view._filters.get('from'), view._filters.get('to')
        if lo:
            start = max(start, lo) if start else lo
        if hi:
            end = min(end, hi)
        if start and start > end:
            raise ValueError('分析日期与所选时间范围没有交集。')
        filters = {key:value for key,value in filters.items() if key not in ('from','to')}
        view = Aggregator(store, app.cfg).filtered(filters)
        if not start:
            where, args = view._where('all')
            start = store.query_one('SELECT MIN(day) day FROM records '+where+' AND day<=?', [*args,end]).get('day') or end
        days = (dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days+1
        if days>3660:
            raise ValueError('AI 可视化单次最多分析十年，请缩小时间范围。')
        if dt.date.fromisoformat(start).toordinal()<=days:
            raise ValueError('此时间范围无法建立上一等长周期，请调整开始日期。')
        prior_end = dt.date.fromisoformat(start)-dt.timedelta(days=1)
        prior_start = prior_end-dt.timedelta(days=days-1)
        # Prices are projected once across current + comparison scopes. All
        # subsequent totals, groups and drill evidence share the same basis.
        clause, params = view._where(f'{prior_start}~{end}')
        project_cost_basis(store, app.cfg, basis, clause, params)
        drill_filters = {**filters, 'cost_basis':basis}
        bounds = {'start':start, 'end':end, 'previous_start':str(prior_start), 'previous_end':str(prior_end)}
        current_range, prior_range = f'{start}~{end}', f'{prior_start}~{prior_end}'
        def where(period):
            return view._where(period)
        def aggregate(period):
            clause, params = where(period)
            return metrics(store.query_one('SELECT '+SUMS+' FROM records '+clause, params))
        current, previous = aggregate(current_range), aggregate(prior_range)
        def evidence_item(title, cur, old, item_filter, kind='', name='', previous_filter=None):
            return {'id':'E'+str(len(evidence)+1), 'title':title, 'kind':kind, 'name':name,
                    'metrics':{'current':cur, 'previous':old, 'cost_delta':round(cur['cost']-old['cost'], 6),
                               'cost_growth':change(cur['cost'], old['cost'])['growth']},
                    'comparison':comparison(cur, old), 'fact':_fact(cur, old), 'filter':item_filter,
                    'previous_filter':previous_filter or {**item_filter, 'range':prior_range}}
        evidence = []
        evidence.append(evidence_item('本期与上一等长周期', current, previous,
                                     {'range':current_range, **drill_filters}))
        grouped = {}
        for field in ('project', 'model', 'tool'):
            expression = "COALESCE(NULLIF(project,''),'(未标注)')" if field == 'project' else (
                "COALESCE(NULLIF(lower(trim(model)),''),'未知模型')" if field == 'model' else "COALESCE(NULLIF(tool,''),'(未标注)')")
            def groups(period):
                clause, params = where(period)
                return {row['name']:metrics(row) for row in store.query(
                    'SELECT '+expression+' name,'+SUMS+' FROM records '+clause+
                    ' GROUP BY 1 ORDER BY cost DESC,tokens DESC,name LIMIT 80', params)}
            now, old = groups(current_range), groups(prior_range)
            grouped[field] = []
            names = sorted(set(now)|set(old), key=lambda name:(-now.get(name, {}).get('cost',0),
                -abs(now.get(name, {}).get('cost',0)-old.get(name, {}).get('cost',0)),
                -now.get(name, {}).get('tokens',0), name))
            for name in names:
                cur, before = now.get(name, metrics()), old.get(name, metrics())
                item = evidence_item(field+' · '+name, cur, before,
                                     {'range':current_range, **drill_filters, field:name}, field, name)
                item['state'] = 'disappeared' if not cur['records'] else ('new' if not before['records'] else 'existing')
                item['share'] = round(cur['cost']/current['cost'], 6) if current['cost'] else 0
                evidence.append(item); grouped[field].append(item)
        def daily_rows(period):
            clause, params = where(period)
            return {row['day']:metrics(row) for row in store.query(
                'SELECT day,'+SUMS+' FROM records '+clause+' GROUP BY day ORDER BY day', params)}
        now_days, prior_days = daily_rows(current_range), daily_rows(prior_range)
        daily = []
        for offset in range(days):
            date = (dt.date.fromisoformat(start)+dt.timedelta(days=offset)).isoformat()
            prior_date = (prior_start+dt.timedelta(days=offset)).isoformat()
            cur, old = now_days.get(date, metrics()), prior_days.get(prior_date, metrics())
            daily.append({'date':date, 'day':date, 'previous_date':prior_date, 'previous_day':prior_date, 'current':cur, 'previous':old,
                          'cost':cur['cost'], 'previous_cost':old['cost'], 'tokens':cur['tokens'],
                          'previous_tokens':old['tokens'], 'requests':cur['requests'], 'previous_requests':old['requests'],
                          'filter':{'range':date+'~'+date, **drill_filters},
                          'previous_filter':{'range':prior_date+'~'+prior_date, **drill_filters}})
        def cost_kinds(period):
            clause, params = where(period)
            rows = {row['kind']:metrics(row) for row in store.query(
                'SELECT cost_kind kind,'+SUMS+' FROM records '+clause+' GROUP BY cost_kind', params)}
            return [{'kind':kind, **rows.get(kind, metrics()), 'current':rows.get(kind, metrics())} for kind in KINDS]
        cost_breakdown = cost_kinds(current_range)
        previous_cost_breakdown = cost_kinds(prior_range)
        clause, params = where(current_range)
        sessions = []
        for row in store.query("SELECT tool,COALESCE(project,'') project,session,"+SUMS+
                               ' FROM records '+clause+" AND COALESCE(session,'')<>'' GROUP BY tool,project,session "
                               'ORDER BY cost DESC,tokens DESC,tool,project,session LIMIT 8', params):
            item = {'tool':row['tool'], 'project':row['project'], 'session':row['session'], 'metrics':metrics(row),
                    'filter':{'range':current_range, **drill_filters, 'tool':row['tool'],
                              'project':row['project'] or '(未标注)', 'session':row['session']}}
            item['share'] = round(item['metrics']['cost']/current['cost'],6) if current['cost'] else 0
            # Session drill-down must preserve the composite identity.
            prior_clause, prior_params = where(prior_range)
            prior_metrics = metrics(store.query_one('SELECT '+SUMS+' FROM records '+prior_clause+
                " AND tool=? AND COALESCE(project,'')=? AND session=?", [*prior_params, row['tool'], row['project'], row['session']]))
            ev = evidence_item('会话 · '+row['session'], item['metrics'], prior_metrics, item['filter'], 'session', row['session'])
            evidence.append(ev); item['id'] = ev['id']; sessions.append(item)
        # Bind the largest daily changes to the same evidence contract as
        # projects and models, so a report can show exact day-level figures.
        peak_days = sorted((row for row in daily if row['current']['records'] or row['previous']['records']),
                           key=lambda row:(-abs(row['cost']-row['previous_cost']), -row['cost'], row['date']))[:5]
        for row in peak_days:
            ev = evidence_item('日期 · '+row['date'], row['current'], row['previous'],
                               row['filter'], 'day', row['date'], row['previous_filter'])
            ev['previous_name'] = row['previous_date']
            evidence.append(ev); row['id'] = ev['id']
    snapshot = {'schema_version':2, 'range':current_range, 'bounds':bounds, 'current':current, 'previous':previous,
                'summary':current, 'coverage':current['pricing_coverage'], 'comparison':comparison(current,previous),
                'evidence':evidence, 'groups':grouped, 'daily':daily, 'cost_breakdown':cost_breakdown,
                'previous_cost_breakdown':previous_cost_breakdown,
                'sessions':sessions, 'filters':filters, 'cost_basis':basis, 'currency':'USD', 'captured_at':int(time.time()),
                'limitations':['金额为实付与所选计价口径的估算合计，不等同于订阅账单；未计价记录不计入金额，未知费用不代表免费。',
                               '项目、模型和工具明细分别选取本期及上期费用/用量排名前80项；总量仍覆盖所选范围的全部记录。',
                               '前后周期按相同天数对照，最近周期可能包含尚未结束的今天。',
                               '用量日志不能证明任务质量、无效重试或生产率；降价方案需要实际任务验证。']}
    snapshot['signals'] = signals(snapshot)
    return snapshot


def signals(snapshot):
    """Rank observable changes, attaching facts and precise destinations."""
    current, previous = snapshot['current'], snapshot['previous']
    summary = snapshot['evidence'][0]
    output = []
    def add(identifier, kind, title, fact, action, evidence, *, priority=50, severity='info', values=None, target=None):
        target = target or ('prices' if kind=='unpriced' else ('sources' if kind=='empty' else ('session' if kind=='session' else 'records')))
        label = '检查模型价格' if target=='prices' else ('检查采集来源' if target=='sources' else ('打开会话' if target=='session' else '查看本期明细'))
        actions = [{'label':label, 'target':target, 'filter':evidence[0]['filter']}]
        if target == 'records':
            actions.append({'label':'查看上期明细', 'target':'records', 'filter':evidence[0]['previous_filter']})
        output.append({'id':identifier, 'kind':kind, 'title':title, 'fact':fact, 'next_step':action,
                       'actions':actions,
                       'evidence_ids':[item['id'] for item in evidence], 'filter':evidence[0]['filter'],
                       'previous_filter':evidence[0]['previous_filter'], 'priority':priority, 'severity':severity,
                       'values':values or {}})
    if not current['records'] and not previous['records']:
        add('no_data', 'empty', '所选范围尚无用量记录', '本期与对照周期均没有已采集记录，无法判断消费变化。',
            '检查采集来源或扩大时间范围，再生成诊断。', [summary], priority=100)
        return output
    unknown = sorted((item for item in snapshot['groups']['model'] if item['metrics']['current']['unknown_records']),
                     key=lambda item:(-item['metrics']['current']['unknown_tokens'], -item['metrics']['current']['unknown_records']))
    if unknown:
        top = unknown[0]; value = top['metrics']['current'];missing_name = top['name']=='未知模型'
        add('unpriced', 'unpriced', '先补齐未计价用量',
            f"本期 {_num(current['unknown_records'])} 条记录费用未知，涉及 {_num(current['unknown_tokens'])} Token；"
            f"其中 {top['name']} 有 {_num(value['unknown_tokens'])} Token，占本期未知用量 "
            f"{(value['unknown_tokens']/current['unknown_tokens']*100) if current['unknown_tokens'] else 0:.2f}%。",
            ('先检查采集来源中的模型名称，补齐真实模型 ID 后再核对价格；未知费用不能视为免费。' if missing_name else
             '先核对该模型的实付来源或检查价格；未知部分未纳入费用，当前总额不能视为完整账单。'),
            [top, summary], priority=96, severity='warning', target='sources' if missing_name else 'prices', values={'unknown_tokens':current['unknown_tokens'],
                'unknown_records':current['unknown_records'], 'model':top['name'], 'model_unknown_tokens':value['unknown_tokens']})
    delta = current['cost']-previous['cost']
    if delta:
        direction = '增加' if delta > 0 else '减少'
        projects = sorted(snapshot['groups']['project'], key=lambda item:item['metrics']['cost_delta'], reverse=delta>0)
        contributors = [item for item in projects if item['metrics']['cost_delta']*delta > 0][:2]
        fact = f"已记录费用从 {_dollars(previous['cost'])} 变为 {_dollars(current['cost'])}，{direction} {_dollars(abs(delta))}。"
        if contributors:
            fact += '主要同向变化：'+ '；'.join(f"{item['name']} {'+' if item['metrics']['cost_delta']>=0 else '-'}{_dollars(abs(item['metrics']['cost_delta']))}" for item in contributors)+'。'
        add('cost_change', 'cost_change', '定位费用'+direction+'的主要项目', fact,
            '查看贡献最大的项目，并对照前后周期的调用数量和输入规模；这反映消费变化，不证明任务效率。',
            contributors+[summary] if contributors else [summary], priority=90 if delta>0 else 83,
            severity='warning' if delta>0 else 'positive', values={'cost_delta':round(delta,6),
                'cost_growth':snapshot['comparison']['cost']['growth'], 'complete':snapshot['comparison']['cost']['complete']})
    if current['requests'] and previous['requests'] and previous['cost']:
        old_unit = previous['cost']/previous['requests']
        # Exact additive attribution: holding previous unit cost, request-volume
        # effect plus remaining unit/mix effect equals the total cost delta.
        volume = (current['requests']-previous['requests'])*old_unit
        unit = delta-volume
        if abs(delta)>0.000001 and abs(unit)>0.000001:
            fact = (f"调用从 {_num(previous['requests'])} 变为 {_num(current['requests'])} 次；已记录费用/次从 "
                    f"{_dollars(previous['cost_per_request'])} 变为 {_dollars(current['cost_per_request'])}。"
                    f"按上一周期每次费用拆分，调用数量项 {'+' if volume>=0 else '-'}{_dollars(abs(volume))}，"
                    f"每次费用与模型组合项 {'+' if unit>=0 else '-'}{_dollars(abs(unit))}。")
            add('cost_driver', 'cost_driver', '区分调用量与每次费用的影响', fact,
                '对照模型组合与平均输入；该拆分为算术分解，不能据此认定价格上涨或无效重试。', [summary],
                priority=78, values={'volume_effect':round(volume,6), 'unit_mix_effect':round(unit,6),
                    'cost_delta':round(delta,6), 'complete':snapshot['comparison']['cost']['complete']})
    current_input = current['input']+current['cache_read']+current['cache_write']
    previous_input = previous['input']+previous['cache_read']+previous['cache_write']
    cache_delta = snapshot['comparison']['cache_hit_rate']['delta_pp']
    if current_input and previous_input and cache_delta <= -5:
        model_rows = [item for item in snapshot['groups']['model']
                      if item['metrics']['current']['records'] and item['metrics']['previous']['records']
                      and item['comparison']['cache_hit_rate']['delta_pp'] <= -5]
        model_rows.sort(key=lambda item:item['comparison']['cache_hit_rate']['delta_pp'])
        refs = model_rows[:1]+[summary]
        add('cache_decline', 'cache_decline', '缓存命中率明显下降',
            f"缓存读取占全部输入从 {previous['cache_hit_rate']*100:.2f}% 变为 {current['cache_hit_rate']*100:.2f}%，"
            f"下降 {abs(cache_delta):.2f} 个百分点；本期缓存读取 {_num(current['cache_read'])} Token。",
            '先核对相同模型的缓存字段与上下文是否变化；支持缓存时再检查稳定提示词前缀，不承诺节省金额。',
            refs, priority=86, severity='warning', values={'cache_delta_pp':cache_delta,
                 'current_cache_tokens':current['cache_read'], 'current_input_tokens':current_input})
    if snapshot['sessions'] and current['cost']:
        session = snapshot['sessions'][0]
        if session['share'] >= .25:
            ev = next(item for item in snapshot['evidence'] if item['id']==session['id'])
            add('costly_session', 'session', '优先复核高占比会话',
                f"会话 {session['session']} 已记录费用 {_dollars(session['metrics']['cost'])}，"
                f"占本期 {session['share']*100:.2f}%；{_num(session['metrics']['requests'])} 次调用、"
                f"{_num(session['metrics']['tokens'])} Token。",
                '打开该会话，核对实际任务与输入规模，再决定是否缩短上下文或调整后续模型。',
                [ev], priority=74, values={'cost':session['metrics']['cost'], 'share':session['share'],
                    'tool':session['tool'], 'project':session['project'], 'session':session['session']})
    if not previous['records'] and current['records']:
        add('no_baseline', 'baseline', '当前范围尚无可对照的历史',
            f"本期 {_num(current['requests'])} 次调用，已记录费用 {_dollars(current['cost'])}；上一等长周期没有记录，不能计算增长率。",
            '扩大时间范围或积累下一周期数据，当前先核对主要模型与未计价部分。', [summary], priority=68)
    if not output:
        add('stable', 'stable', '本期已记录费用与对照周期持平', _fact(current,previous),
            '检查主要模型和项目的结构变化；持平只描述已采集用量，不代表任务效果相同。', [summary], priority=60)
    return sorted(output, key=lambda item:(-item['priority'], item['id']))


def model_context(snapshot):
    """Do not send full charts, source paths or raw log bodies to a model."""
    selected = snapshot['signals'][:8]
    ids = {'E1'} | {identifier for item in selected for identifier in item['evidence_ids']}
    # Include high-cost and biggest decreasing groups, preserving distinct
    # questions while keeping a stable upper bound on model context.
    groups = {}
    for field, items in snapshot['groups'].items():
        chosen = items[:5]
        chosen += sorted(items, key=lambda item:item['metrics']['cost_delta'])[:2]
        chosen += [item for item in items if item['id'] in ids]
        unique = {item['id']:item for item in chosen}
        groups[field] = list(unique.values())[:16]
        ids.update(item['id'] for item in groups[field])
    selected_days = sorted(snapshot['daily'], key=lambda item:(-item['cost'], -item['tokens']))[:8]
    sessions = snapshot.get('sessions', [])[:8]
    ids.update(item['id'] for item in sessions)
    ids.update(item['id'] for item in snapshot['evidence'] if item.get('kind') == 'day')
    return {key:snapshot[key] for key in ('schema_version','range','bounds','current','previous','comparison','filters','limitations')} | {
        'cost_basis':snapshot.get('cost_basis','total_cost'), 'currency':'USD',
        'signals':selected, 'groups':groups,
        'evidence':[item for item in snapshot['evidence'] if item['id'] in ids][:64],
        'daily_peaks':selected_days, 'sessions':sessions,
        'cost_breakdown':snapshot['cost_breakdown'],
        'previous_cost_breakdown':snapshot['previous_cost_breakdown']}
