"""Editable query conditions; evidence decisions outlive changes of search scope."""
from .media import absolute_start, finite


def values(data):
    cameras = data.get('cameras') or []
    if not isinstance(cameras, list) or len(cameras)>500 or any(not isinstance(c,str) or not c.strip() or len(c)>100 for c in cameras):
        raise ValueError('摄像头条件须为有效编号列表')
    mode = data.get('time_filter') or ('absolute' if data.get('from') or data.get('to') else 'any')
    if mode not in ('any','absolute','relative'):
        raise ValueError('无效时间查询方式')
    group = str(data.get('query_group') or '').strip()
    if len(group)>100:
        raise ValueError('时间组名称过长')
    start = absolute_start(data.get('from')) if mode=='absolute' else None
    end = absolute_start(data.get('to')) if mode=='absolute' else None
    rstart = finite(data['relative_from'],'相对查询开始') if mode=='relative' and data.get('relative_from') not in (None,'') else None
    rend = finite(data['relative_to'],'相对查询结束') if mode=='relative' and data.get('relative_to') not in (None,'') else None
    if mode=='relative' and not group:
        raise ValueError('相对时间查询须填写共同时间组')
    lo, hi = (start,end) if mode=='absolute' else (rstart,rend)
    if lo is not None and hi is not None and lo>hi:
        raise ValueError('查询开始时间不能晚于结束时间')
    return dict(cameras=sorted(set(c.strip() for c in cameras)),time_filter=mode,
                from_epoch=start,to_epoch=end,query_group=group if mode=='relative' else None,
                relative_from=rstart,relative_to=rend)


def window(task):
    mode=task.get('time_filter') or ('absolute' if task.get('from_epoch') is not None or task.get('to_epoch') is not None else 'any')
    if mode=='absolute': return mode,None,task.get('from_epoch'),task.get('to_epoch')
    if mode=='relative': return mode,task.get('query_group'),task.get('relative_from'),task.get('relative_to')
    return 'any',None,None,None


def origin(media, mode, group):
    if mode=='absolute' and media.get('start_epoch') is not None:
        return media['start_epoch']+media.get('offset',0)
    if mode=='relative' and media.get('reference_group')==group and media.get('relative_start') is not None:
        return media['relative_start']+media.get('offset',0)
    return None


def match(task, media, observation):
    mode,group,lo,hi=window(task)
    if mode=='any' or (lo is None and hi is None): return 'included'
    base=origin(media,mode,group)
    error=media.get('uncertainty')
    if base is None or error is None: return 'unknown'
    if lo is not None and base+observation['end']+error<lo: return 'outside'
    if hi is not None and base+observation['start']-error>hi: return 'outside'
    return 'included'


def update(store,data):
    new=values(data)
    with store.lock,store.db:
        task=store.get('task',data['task_id'])
        from .tasks import require_active
        require_active(task)
        if data.get('expected_task_version')!=task['version']: raise ValueError('任务版本已变化，请重新载入条件')
        before={key:task.get(key) for key in new}
        task.update(new,search_mode='all',version=task['version']+1)
        store.write('task',task)
        store.audit(task['id'],'query-conditions',dict(before=before,after=new))
    return task
