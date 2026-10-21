"""Separate confirmed evidence, current pending candidates and search hints."""
from types import SimpleNamespace
from .engine import search
from .timing import latest, fields
from .integrity import check
from .coverage import build as coverage
from .topology import scope


def latest_evidence(store,task,observations,integrity):
    events=[(store.get('media',o['media_id']),o) for o in observations]
    result=latest(events)
    invalid=sorted({m['id'] for m,o in events if not integrity[m['id']]['valid']})
    if invalid:
        return dict(status='evidence_unavailable',observation_id=None,invalid_media=invalid)
    if task.get('time_review_required'):
        return dict(status='needs_review',observation_id=None)
    if result['observation_id']:
        m,o=next((m,o) for m,o in events if o['id']==result['observation_id'])
        result['event']={k:v for k,v in o.items() if k!='feature'}
        result['event'].update(location=m['location'],**fields(m,o))
    return result


def build(store,task_id):
    with store.lock:
        task=store.get('task',task_id)
        integrity={m['id']:check(store,m) for m in store.list('media')}
        confirmed=[store.get('observation',d['observation_id']) for d in store.decisions(task_id) if d['status']=='confirmed']
        last=latest_evidence(store,task,confirmed,integrity)
        query_error=None
        try:
            candidates=search(store,SimpleNamespace(version=task['encoder']),task_id,len(store.list('observation')))
            pending=[o for o in candidates if o['status']=='pending']
            pending_last=latest_evidence(store,task,pending,integrity)
            context=scope(store,task)
            hints=dict(direction=context.get('direction', 'after'), anchor_id=context['anchor_id'],neighbors=context['neighbors'],
                remaining_cameras=context['remaining_cameras'],outside_task_cameras=context['outside_task_cameras'],
                unavailable_neighbors=context['unavailable_neighbors'],selected_cameras=context['selected_cameras'])
            covering=coverage(store,task)
        except ValueError as exc:
            query_error=str(exc);candidates=[];pending=[]
            pending_last=dict(status='query_unavailable',observation_id=None)
            hints=None;covering=None
        return dict(task_id=task_id,task_version=task['version'],task_status=task['status'],
            confirmed_scope='全部历史已确认记录，可能超出当前查询条件',
            last_confirmed=last,latest_pending=pending_last,pending_scope='当前查询条件与查找方向内的候选',
            counts=dict(confirmed_history=len(confirmed),pending_current=len(pending),
                rejected_current=sum(o['status']=='rejected' for o in candidates),
                invalid_source_candidates=sum(not o['source_integrity']['valid'] for o in candidates)),
            search_hints=hints,coverage=covering,query_error=query_error,
            explanation='最新候选只按可信时间判断，分数不代表时间先后或身份概率；查找提示来自当前起点的人工邻接配置，不代表真实去向或当前所在。')
