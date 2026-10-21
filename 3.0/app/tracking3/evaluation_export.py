"""Freeze evaluation inputs in one read-only SQLite snapshot.

Ranking uses persisted query/observation features, just as the workbench does.
No encoder inference, identity annotation or startup recovery is performed.
"""
import csv
import hashlib
import io
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from .engine import search
from .integrity import check, require
from .snapshots import crop_source
from .store import Store, fingerprint
from .index_trace import snapshot as trace_snapshot
from .topology import scope, configuration as topology_configuration
from .venue import configuration as venue_configuration


def _public(item):
    return {k:v for k,v in item.items() if k!='feature'}


def export(data, task_id, output, *, expected_task_version=None, limit=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('输出目录已存在，请使用新目录保留旧快照')
    if limit is not None and (type(limit) is not int or limit<1):
        raise ValueError('候选数量上限须为正整数；留空导出全部')
    store = Store.open_readonly(data)
    created = False
    try:
        store.db.execute('BEGIN')
        task = store.get('task',task_id)  # Establish the WAL read snapshot.
        if expected_task_version is not None and expected_task_version!=task['version']:
            raise ValueError('任务版本与预期不一致，请核对后重新导出')
        context = scope(store,task)
        context.pop('anchor',None)
        media = store.list('media')
        selected = [m for m in media if m['camera_id'] in context['selected_cameras']]
        if any(m.get('status')=='processing' for m in selected):
            raise ValueError('所选录像正在处理，请暂停并等待停止后导出')
        # Engine.search only needs .version: all vectors already exist in DB.
        # This preserves the exact persisted query encoder; no model is loaded.
        ranked = search(store,SimpleNamespace(version=task['encoder']),task_id,
                        limit=len(store.list('observation')))
        candidates = ranked if limit is None else ranked[:limit]
        by_id = {m['id']:m for m in media}
        used_ids = {c['media_id'] for c in candidates}
        if task.get('media_id'):
            used_ids.add(task['media_id'])
        for key in ('anchor','query_anchor'):
            if task.get(key):
                used_ids.add(store.get('observation',task[key])['media_id'])
        trace_ids = {m['id'] for m in selected} | used_ids
        trace = trace_snapshot(store,trace_ids)
        # Include executed zero-detection frames even if they produced no candidates.
        used_ids.update(r['media_id'] for r in trace['frames'])
        for ident in used_ids:
            require(store,by_id[ident])
        for c in candidates:
            c['source_path'] = by_id[c['media_id']]['path']
        sources = [{**_public(m),'integrity_at_export':check(store,m),
                    'selected_query_scope':m['camera_id'] in context['selected_cameras']} for m in media]
        crops = {task['crop']} | {c['crop'] for c in candidates}
        contents, crop_digests = {}, {}
        for relative in sorted(crops):
            content = crop_source(store,relative).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            contents['evidence/'+relative] = content
            crop_digests[relative] = digest
        snapshot = dict(format='tracking3-evaluation-inputs-v1', exported_at=time.time(),
            task=_public(task), encoder=task['encoder'], search_context=context,
            venue=venue_configuration(store), topology=topology_configuration(store),
            media=sources, scope_candidate_count=len(ranked),
            exported_candidate_count=len(candidates), limit_requested=limit,
            truncated=len(candidates)<len(ranked),
            identity_reviewed=False, ranking_basis='persisted query and observation features',
            index_trace_frame_count=len(trace['frames']),
            source_path_basis='data-relative media.path', crop_digests=crop_digests)
        for name,payload in [('snapshot.json',snapshot),('candidates.json',candidates),
                             ('tracking-samples.json',trace)]:
            contents[name] = (json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
        fields = ['global_person_id','camera_id','source_path','start_media_s','end_media_s',
                  'visibility','identity_evidence','annotator','annotation_version']
        gt = io.StringIO(newline='')
        writer = csv.DictWriter(gt,fieldnames=fields)
        writer.writeheader()
        for m in selected:
            writer.writerow(dict(camera_id=m['camera_id'],source_path=m['path'],
                                 visibility='uncertain',annotation_version='unreviewed-template'))
        contents['target-events-template.csv'] = gt.getvalue().encode()
        contents['README.md'] = ('''# 评估输入快照

同一只读数据库事务中的任务、候选、机位、拓扑和来源记录；不加载模型、不确认身份、不修改原数据库。
候选截图和查询图位于 evidence/，原相对路径/SHA见 snapshot.json。没有复制源视频，源来源和指纹见快照media。
将 target-events-template.csv 复制到独立工作文件后，通过连续原录像填写GT；不要直接编辑此快照。
GT完成后，用 tools/evaluate_events.py 的 --write-review-template 生成绑定GT/候选SHA的独立身份复核模板。
工作台confirmed/pending只记录业务状态，不是独立真值。未知GT与身份指标保持未知。
候选中的 source_path 与GT使用相同的数据目录内路径；默认导出当前范围全部候选，上限与截断状态见快照。
搜索范围外/未处理/缺源的资料不因此被判定无人；只有已有索引进入排序，处理覆盖另看快照。
tracking-samples.json保留实际执行检测的源帧/PTS/当时轨迹框，包括检测器零结果；不推定未采样或旧索引帧、不认证身份。
''').encode()
        # Recheck external files as well as DB consistency; file bytes are not
        # protected by SQLite. Abort rather than bind changed source evidence.
        def recheck():
            for ident in used_ids:
                require(store,by_id[ident])
            for relative,digest in crop_digests.items():
                if fingerprint(crop_source(store,relative))!=digest:
                    raise ValueError('截图在导出期间改变，请重新核对后导出')
        recheck()
        output.mkdir(parents=True,exist_ok=False)
        created = True
        for name,content in contents.items():
            target = output/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
        recheck()
        manifest = dict(format='tracking3-evaluation-artifact-v1', task_id=task_id,
                        task_version=task['version'], files={name:hashlib.sha256(content).hexdigest()
                                                           for name,content in contents.items()},
                        source_sha256={by_id[i]['path']:by_id[i]['fingerprint'] for i in sorted(used_ids)},
                        identity_reviewed=False)
        (output/'artifact.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        return dict(output=str(output),task_id=task_id,task_version=task['version'],
                    candidates=len(candidates),scope_candidates=len(ranked),truncated=snapshot['truncated'])
    except BaseException:
        if created:
            shutil.rmtree(output)
        raise
    finally:
        store.db.rollback()
        store.db.close()
