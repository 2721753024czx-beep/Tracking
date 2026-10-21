"""Versioned camera hints and explicit, expandable search scopes."""
import time

from .timing import coordinate


def configuration(store):
    try:
        return store.get('topology', 'main')
    except ValueError:
        return dict(id='main', version=0, edges=[])


def update(store, data):
    edges = data.get('edges')
    if not isinstance(edges, list) or len(edges) > 1000:
        raise ValueError('邻接关系须为最多1000条的列表')
    normalized, pairs = [], set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError('每条邻接关系须填写来源、去向和依据')
        a, b = str(edge.get('from', '')).strip(), str(edge.get('to', '')).strip()
        evidence = str(edge.get('evidence', '')).strip()
        if not a or not b or a == b or max(len(a), len(b)) > 100 or not evidence or len(evidence) > 2000:
            raise ValueError('邻接关系需两个不同摄像头及有效依据')
        if (a, b) in pairs:
            raise ValueError('重复的有向邻接关系')
        pairs.add((a, b)); normalized.append(dict(**{'from': a, 'to': b}, evidence=evidence))
    with store.lock, store.db:
        old = configuration(store)
        if data.get('expected_version') != old['version']:
            raise ValueError('邻接配置已变化，请重新载入')
        new = dict(id='main', version=old['version']+1, edges=normalized, at=time.time())
        store.write('topology', new)
        store.audit(None, 'topology', dict(before=old, after=new))
        for task in store.list('task'):
            task['version'] += 1
            store.write('task', task)
            store.audit(task['id'], 'topology-revised', dict(topology_version=new['version']))
    return new


def scope(store, task):
    topo = configuration(store)
    media = store.list('media')
    available = sorted({m['camera_id'] for m in media})
    allowed = sorted(set(task.get('cameras') or available))
    ident = task.get('query_anchor', task.get('anchor'))
    anchor = store.get('observation', ident) if ident else None
    if anchor and store.get('decision', f"{task['id']}:{ident}")['status'] != 'confirmed':
        raise ValueError('查找起点身份已撤销，请重新确认或选择其他起点')
    camera = store.get('media', anchor['media_id'])['camera_id'] if anchor else None
    if anchor:
        anchor_media=store.get('media',anchor['media_id'])
        if anchor_media.get('path'):
            from .integrity import require
            require(store,anchor_media)
    neighbors = sorted({e['to'] for e in topo['edges'] if e['from'] == camera})
    mode = task.get('search_mode', 'all')
    fallback = mode == 'neighbors' and not neighbors
    selected = sorted(set(allowed).intersection([camera]+neighbors)) if mode=='neighbors' and not fallback else allowed
    return dict(mode=mode, direction=task.get('search_direction', 'after'), anchor_id=ident, anchor_camera=camera, topology_version=topo['version'],
        selected_cameras=selected, remaining_cameras=sorted(set(allowed)-set(selected)),
        outside_task_cameras=sorted(set(available)-set(allowed)), neighbors=neighbors,
        unavailable_neighbors=sorted(set(neighbors)-set(available)),
        fallback=fallback, anchor=anchor,
        coverage=[dict(camera_id=c, files=[dict(id=m['id'],status=m['status'],
                   processed_until=m.get('processed_until',-1),sample_fps=m.get('sample_fps'))
                   for m in media if m['camera_id']==c]) for c in selected])


def set_scope(store, data):
    with store.lock, store.db:
        task = store.get('task', data['task_id'])
        from .tasks import require_active
        require_active(task)
        if data.get('expected_task_version') != task['version']:
            raise ValueError('任务已变化，请刷新后调整范围')
        mode = data.get('mode')
        if mode not in ('all','neighbors'):
            raise ValueError('无效查找范围')
        direction = data.get('direction', task.get('search_direction', 'after'))
        if direction not in ('after', 'before', 'both'):
            raise ValueError('无效查找方向')
        ident = data.get('anchor_id') or None
        if mode=='neighbors' and not ident:
            raise ValueError('邻居查找需要已确认的起点')
        task.update(query_anchor=ident, search_mode=mode, search_direction=direction, version=task['version']+1)
        result = scope(store, task)  # Validate before committing.
        store.write('task', task)
        store.audit(task['id'],'search-scope', {k:v for k,v in result.items() if k!='anchor'})
        return task


def before_anchor(store, anchor, observation):
    if not anchor:
        return False
    a, b = store.get('media', anchor['media_id']), store.get('media', observation['media_id'])
    if a['id']==b['id']:
        return observation['end'] < anchor['start']
    da, start = coordinate(a, anchor['start']); db, end = coordinate(b, observation['end'])
    if da!=db or da[0]=='file' or a.get('uncertainty') is None or b.get('uncertainty') is None:
        return False  # Unknown clocks are retained, not silently excluded.
    return end+b['uncertainty'] < start-a['uncertainty']
