"""Source-file availability versus sampled processing, grouped by clock domain."""
from .conditions import window, origin
from .timing import coordinate
from .topology import scope
from .integrity import check


def intervals(items):
    merged=[];overlaps=[]
    for start,end in sorted(items):
        if merged and start<=merged[-1][1]:
            if start<merged[-1][1]:overlaps.append([start,min(end,merged[-1][1])])
            merged[-1][1]=max(merged[-1][1],end)
        else:merged.append([start,end])
    return merged,overlaps


def gaps(merged,lo=None,hi=None):
    if not merged:return [[lo,hi]] if lo is not None and hi is not None and lo<hi else []
    result=[[a[1],b[0]] for a,b in zip(merged,merged[1:]) if a[1]<b[0]]
    if lo is not None and lo<merged[0][0]:result.insert(0,[lo,merged[0][0]])
    if hi is not None and merged[-1][1]<hi:result.append([merged[-1][1],hi])
    return [[max(a,lo) if lo is not None else a,min(b,hi) if hi is not None else b]
            for a,b in result if (hi is None or a<hi) and (lo is None or b>lo)]


def build(store,task):
    selected=scope(store,task)['selected_cameras'];mode,group,lo,hi=window(task)
    groups={};missing=[]
    for camera in selected:
        media=[m for m in store.list('media') if m['camera_id']==camera]
        if not media:missing.append(camera)
        for m in media:
            domain,base=coordinate(m,0)
            query_base=origin(m,mode,group)
            if mode!='any' and query_base is not None:
                domain=(mode,group or 'UTC');base=query_base
            ident=(camera,*domain)
            item=groups.setdefault(ident,dict(camera_id=camera,time_domain=list(domain),files=[],
                  intervals=[],verified_intervals=[],timing_certain=True))
            duration=m.get('duration',0);end=base+duration
            matching=mode!='any' and query_base is not None
            start=max(base,lo) if matching and lo is not None else base
            stop=min(end,hi) if matching and hi is not None else end
            # Unknown timing may still overlap the query. Keep its availability visible.
            error=m.get('uncertainty')
            if stop<start and error is not None and (end+error<(lo if lo is not None else end) or base-error>(hi if hi is not None else base)):
                continue
            if stop<start:start,stop=base,end
            item['timing_certain'] &= domain[0]!='file' and error==0
            progress=m.get('processed_until',-1)
            integrity=check(store,m)
            item['files'].append(dict(id=m['id'],name=m.get('name',m['id']),source_range=[base,end],
               status=m['status'],last_sample=progress,sample_fps=m.get('sample_fps'),
               source_integrity=integrity,
               unprocessed_tail=None if m['status']=='complete' else [max(0,progress),duration],
               time_uncertainty=error,time_version=m.get('time_version',1),
               query_time_unknown=mode!='any' and (query_base is None or error is None)))
            item['intervals'].append([start,stop])
            if integrity['valid']:item['verified_intervals'].append([start,stop])
    for item in groups.values():
        merged,overlaps=intervals(item.pop('intervals'))
        verified,_=intervals(item.pop('verified_intervals'))
        domain=item['time_domain']
        bounded=mode!='any' and domain==[mode,group or 'UTC']
        item.update(available=merged,verified_source_intervals=verified,nominal_gaps=gaps(merged,lo if bounded else None,hi if bounded else None),
                    nominal_overlaps=overlaps)
    return dict(groups=list(groups.values()),missing_cameras=missing,
        explanation='登记区间、缺口和重叠按导入时名义时间计算，不代表源文件仍可用；源核验有效区间另列。误差未知或非零时不证明真实缺录。完成仅表示按配置采样处理完成，未命中不证明没有目标。')
