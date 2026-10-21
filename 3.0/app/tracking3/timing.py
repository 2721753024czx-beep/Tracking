"""Comparable event clocks, kept separate from source-file playback coordinates."""
import json
import time

from .media import absolute_start, finite


def metadata(data):
    group = str(data.get('reference_group') or '').strip()
    evidence = str(data.get('time_evidence') or '').strip()
    if len(group) > 100 or len(evidence) > 2000:
        raise ValueError('时间组或校时依据过长')
    relative = data.get('relative_start')
    relative = None if relative is None or relative == '' else finite(relative, '相对起点')
    if bool(group) != (relative is not None):
        raise ValueError('共同时间组与相对起点须一起填写；未知时同时留空')
    if group and not evidence:
        raise ValueError('共同相对时间需要登记同步或校时依据')
    return dict(start_epoch=absolute_start(data.get('start')),
                offset=finite(data.get('offset', 0), '时间偏差'),
                uncertainty=finite(data['uncertainty'], '时间误差', 0)
                            if data.get('uncertainty') is not None else None,
                reference_group=group or None, relative_start=relative, time_evidence=evidence)


def coordinate(media, second):
    offset = media.get('offset', 0)
    if media.get('start_epoch') is not None:
        return ('absolute', 'UTC'), media['start_epoch'] + offset + second
    if media.get('reference_group') and media.get('relative_start') is not None:
        return ('relative', media['reference_group']), media['relative_start'] + offset + second
    return ('file', media['id']), second


def fields(media, observation):
    domain, start = coordinate(media, observation['start'])
    _, end = coordinate(media, observation['end'])
    return dict(time_domain=list(domain), common_start=start, common_end=end,
                time_uncertainty=media.get('uncertainty'), time_version=media.get('time_version', 1),
                time_evidence=media.get('time_evidence', ''))


def latest(events):
    """Find a provably later observed end, respecting uncertainty and ties."""
    if not events:
        return {'status': 'none', 'observation_id': None}
    values = [(m, o, coordinate(m, o['end'])) for m, o in events]
    domains = {value[2][0] for value in values}
    # Same file playback has a trustworthy local ordering even without a wall clock.
    if len(domains) != 1:
        return {'status': 'incomparable', 'observation_id': None}
    local = next(iter(domains))[0] == 'file'
    if local:
        return {'status': 'local_only', 'observation_id': None}
    if not local and any(m.get('uncertainty') is None for m, _, _ in values):
        return {'status': 'unknown_error', 'observation_id': None}
    bounds = [(o, at - (0 if local else m['uncertainty']),
                    at + (0 if local else m['uncertainty'])) for m, o, (_, at) in values]
    for index, (o, lower, _) in enumerate(bounds):
        if all(lower > other_upper for j, (_, _, other_upper) in enumerate(bounds) if j != index):
            return {'status': 'unique', 'observation_id': o['id']}
    return {'status': 'overlap_or_tie', 'observation_id': None}


def update(store, media_id, data):
    values = metadata(data)
    # Single transaction: metadata, task revision/review flags and audit all move together.
    with store.lock, store.db:
        media = store.get('media', media_id)
        if data.get('expected_time_version') is not None and data['expected_time_version'] != media.get('time_version', 1):
            raise ValueError('素材时间版本已变化，请重新载入后修订')
        before = {key: media.get(key) for key in values}
        if before == values:
            return media
        media.update(values, time_version=media.get('time_version', 1) + 1)
        store.db.execute('UPDATE documents SET body=? WHERE kind=? AND id=?',
                         (json.dumps(media, ensure_ascii=False), 'media', media_id))
        for task in store.list('task'):
            if task.get('cameras') and media['camera_id'] not in task['cameras']:
                # Monitor seed may be outside the candidate camera scope.
                if not task.get('anchor') or store.get('observation', task['anchor'])['media_id'] != media_id:
                    continue
            from .connections import for_task, invalidate
            roots = [c['id'] for c in for_task(store, task['id'])
                     if any(store.get('observation', ident)['media_id'] == media_id
                            for ident in (c['from_observation'], c['to_observation']))]
            invalidate(store, task['id'], roots, '素材时间已修订，需重新核对连接')
            task.update(version=task['version'] + 1, time_review_required=True)
            store.db.execute('UPDATE documents SET body=? WHERE kind=? AND id=?',
                             (json.dumps(task, ensure_ascii=False), 'task', task['id']))
            store.db.execute('INSERT INTO audit(at,task_id,action,body) VALUES(?,?,?,?)',
                             (time.time(), task['id'], 'media-time-updated', json.dumps(
                                 {'media_id': media_id, 'before': before, 'after': values,
                                  'time_version': media['time_version']}, ensure_ascii=False)))
    return media


def acknowledge(store, task_id, versions):
    with store.lock, store.db:
        # Client must have seen every current media revision before dismissing the notice.
        current = {m['id']: m.get('time_version', 1) for m in store.list('media')}
        if versions != current:
            raise ValueError('时间配置已变化，请刷新后重新核对')
        task = store.get('task', task_id)
        from .tasks import require_active
        require_active(task)
        task.update(time_review_required=False, version=task['version'] + 1)
        store.db.execute('UPDATE documents SET body=? WHERE kind=? AND id=?',
                         (json.dumps(task, ensure_ascii=False), 'task', task_id))
        store.db.execute('INSERT INTO audit(at,task_id,action,body) VALUES(?,?,?,?)',
                         (time.time(), task_id, 'time-reviewed', json.dumps(current)))
    return task
