"""Explicit, auditable links between independently confirmed observations."""
import time

from .store import uid
from .timing import coordinate


def for_task(store, task_id):
    store.get('task', task_id)
    return [c for c in store.list('connection') if c['task_id'] == task_id]


def source_review(store, task_id):
    from .integrity import check
    integrity={m['id']:check(store,m) for m in store.list('media')}
    links=[{**c,'source_valid':all(integrity[store.get('observation',i)['media_id']]['valid']
              for i in (c['from_observation'],c['to_observation']))} for c in for_task(store,task_id)]
    invalid={c['id'] for c in links if not c['source_valid']}
    while True:
        expanded=invalid|{c['id'] for c in links if invalid.intersection(c['depends_on'])}
        if expanded==invalid:break
        invalid=expanded
    return [{**c,'source_valid':c['id'] not in invalid} for c in links]


def affected(store, task_id, roots):
    """Transitive dependency closure; never changes independent identity decisions."""
    found = set(roots)
    links = for_task(store, task_id)
    while True:
        new = {c['id'] for c in links if found.intersection(c.get('depends_on', []))}
        if new <= found:
            return found
        found |= new


def preview(store, task_id, observation_id=None, connection_id=None):
    with store.lock:
        task = store.get('task', task_id)
        if bool(observation_id) == bool(connection_id):
            raise ValueError('请选择一个观察或连接预览影响')
        links = for_task(store, task_id)
        if connection_id:
            link = store.get('connection', connection_id)
            if link['task_id'] != task_id:
                raise ValueError('连接不属于此任务')
            roots = [connection_id]
            impacted = affected(store, task_id, roots) - {connection_id}
        else:
            store.get('observation', observation_id)
            roots = [c['id'] for c in links if observation_id in
                     (c['from_observation'], c['to_observation'])]
            impacted = affected(store, task_id, roots)
        return dict(task_version=task['version'],
                    affected=[c for c in links if c['id'] in impacted and c['status']=='confirmed'],
                    identities_preserved=True)


def invalidate(store, task_id, roots, reason, exclude=()):
    # Caller owns the transaction and lock.
    changed = []
    impacted = affected(store, task_id, roots) - set(exclude)
    for c in for_task(store, task_id):
        if c['id'] in impacted and c['status'] == 'confirmed':
            c.update(status='needs_review', review_reason=reason, at=time.time(),
                     version=c['version'] + 1)
            store.write('connection', c)
            changed.append(c['id'])
    return changed


def assess(store, link, enforce=False):
    a = store.get('observation', link['from_observation'])
    b = store.get('observation', link['to_observation'])
    ma, mb = store.get('media', a['media_id']), store.get('media', b['media_id'])
    da, end = coordinate(ma, a['end'])
    db, start = coordinate(mb, b['start'])
    _, first = coordinate(ma, a['start'])
    _, last = coordinate(mb, b['end'])
    errors = (ma.get('uncertainty'), mb.get('uncertainty'))
    known = da == db and da[0] != 'file' and all(e is not None for e in errors)
    result = dict(comparable=known, gap=None, gap_range=None,
                  media_time_versions={ma['id']: ma.get('time_version', 1),
                                       mb['id']: mb.get('time_version', 1)})
    if known:
        error = sum(errors)
        result.update(gap=start-end, gap_range=[start-end-error, start-end+error])
        if enforce and link['relation'] == 'transition' and start-end+error < 0:
            raise ValueError('时间范围证明后一片段开始早于前一片段结束；请核对片段或使用重叠关系')
        if enforce and link['relation'] == 'overlap' and (last+error < first or start-error > end):
            raise ValueError('时间范围不支持两个观察重叠')
    return result


def validate_confirmation(store, link, task):
    if task.get('time_review_required'):
        raise ValueError('时间配置已修订，请先完成时间核对再确认连接')
    for ident in [link['from_observation'], link['to_observation']]:
        observation=store.get('observation',ident)
        media=store.get('media',observation['media_id'])
        if media.get('path'):
            from .integrity import require
            require(store,media)
        if store.get('decision', f"{task['id']}:{ident}")['status'] != 'confirmed':
            raise ValueError('连接两端必须分别确认身份')
    for ident in link['depends_on']:
        dependency = store.get('connection', ident)
        if dependency['task_id'] != task['id'] or dependency['status'] != 'confirmed':
            raise ValueError('依赖连接尚未确认或已需重新核对')
        if not next(c['source_valid'] for c in source_review(store,task['id']) if c['id']==ident):
            raise ValueError('依赖连接的源证据失效或无法核实，请先恢复或重新核对')
    return assess(store, link, enforce=True)


def change(store, data):
    with store.lock, store.db:
        task = store.get('task', data['task_id'])
        from .tasks import require_active
        require_active(task)
        if data.get('expected_task_version') is not None and data['expected_task_version'] != task['version']:
            raise ValueError('任务版本已变化，请刷新后核对')
        existing = bool(data.get('id'))
        if existing:
            link = store.get('connection', data['id'])
            if link['task_id'] != task['id']:
                raise ValueError('连接不属于此任务')
            if data.get('expected_version') != link['version']:
                raise ValueError('连接版本已变化，请刷新后核对')
            old = dict(link)
        else:
            a, b = data['from_observation'], data['to_observation']
            if a == b:
                raise ValueError('不能连接同一个观察片段')
            store.get('observation', a)
            store.get('observation', b)
            relation = data.get('relation', 'unknown')
            if relation not in ('transition', 'overlap', 'unknown'):
                raise ValueError('无效连接类型')
            dependencies = data.get('depends_on', [])
            if not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies):
                raise ValueError('依赖必须为不重复的连接ID列表')
            # Dependencies are immutable and can only refer to earlier links: no cycles.
            for ident in dependencies:
                if store.get('connection', ident)['task_id'] != task['id']:
                    raise ValueError('依赖连接不属于此任务')
            link = dict(id=uid(), task_id=task['id'], from_observation=a, to_observation=b,
                        relation=relation, depends_on=dependencies, version=0)
            old = None
        status = data.get('status', 'pending')
        if status not in ('pending', 'confirmed', 'rejected', 'revoked'):
            raise ValueError('无效连接核对状态')
        evidence = str(data.get('evidence', link.get('evidence', ''))).strip()
        if len(evidence) > 2000 or (status == 'confirmed' and not evidence):
            raise ValueError('确认连接须填写核对依据（不超过2000字）')
        assessment = validate_confirmation(store, link, task) if status == 'confirmed' else assess(store, link)
        link.update(status=status, evidence=evidence, assessment=assessment,
                    review_reason='', at=time.time(), operator='local', version=link['version']+1)
        store.write('connection', link)
        changed = invalidate(store, task['id'], [link['id']], '上游连接已修改，需重新核对依赖', exclude=[link['id']])
        task['version'] += 1
        store.write('task', task)
        store.audit(task['id'], 'connection', dict(before=old, after=link, affected=changed))
        return dict(connection=link, affected=changed, task_version=task['version'])
