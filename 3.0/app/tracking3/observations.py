"""Task-owned immutable manual revisions; original indexes remain reproducible."""
import time
from PIL import Image
from .store import uid, fingerprint
from .media import finite, read_frame, crop
from .connections import invalidate, for_task


def change(store, encoder, data):
    with store.lock:
        task = store.get('task', data['task_id'])
        from .tasks import require_active
        require_active(task)
        if data.get('expected_task_version') != task['version']:
            raise ValueError('任务版本已变化，请刷新后纠正')
        if task['encoder'] != encoder.version:
            raise ValueError('须使用任务原编码器纠正片段')
        old = store.get('observation', data['observation_id']) if data.get('observation_id') else None
        if old and (store.observation_owner(old) not in (None,task['id']) or old['id'] in task.get('superseded_observations',[])):
            raise ValueError('片段不属于此任务或已有更新版本')
        media = store.get('media', old['media_id'] if old else data['media_id'])
        if media['status'] == 'processing':
            raise ValueError('请暂停素材处理后纠正片段')
        if fingerprint(store.asset(media['path'])) != media['fingerprint']:
            raise ValueError('源文件已改变，请重新导入后核对')
        if old and data.get('media_id',media['id']) != media['id']:
            raise ValueError('纠正不能更换源录像，另建观察')
        evidence = str(data.get('evidence','')).strip()
        if not evidence or len(evidence)>2000:
            raise ValueError('须填写核对依据（不超过2000字）')
        start,end,at = [finite(data[k],k,0) for k in ('start','end','representative')]
        if not 0 <= start <= at <= end < media['duration']:
            raise ValueError('代表帧须在片段内，片段须位于源录像时长内')
        box = [finite(v,'目标框',0) for v in data['box']]
        if len(box)!=4 or not (box[0]<box[2]<=media['width'] and box[1]<box[3]<=media['height']):
            raise ValueError('目标框须完整位于源画面内')
        rgb,actual = read_frame(store.asset(media['path']),at)
        if not start<=actual<=end:
            raise ValueError('实际解码帧超出片段范围，请按帧时间调整')
        patch=crop(rgb,box);feature=encoder.extract(patch);ident=uid()
        relative=f'crops/manual-{ident}.jpg'
        observation=dict(id=ident,media_id=media['id'],camera_id=media['camera_id'],
            start=start,end=end,representative=actual,box=box,crop=relative,feature=feature,
            encoder=encoder.version,source='manual',sampled=False,confidence=None,local_track=None,
            owner_task=task['id'],replaces=old['id'] if old else None,
            revision=(old.get('revision',0)+1) if old else 1,evidence=evidence,at=time.time())
        Image.fromarray(patch).save(store.root/relative)
        try:
            with store.db:
                changed=[]
                if old:
                    roots=[c['id'] for c in for_task(store,task['id']) if old['id'] in (c['from_observation'],c['to_observation'])]
                    changed=invalidate(store,task['id'],roots,'观察片段已纠正，请使用新片段重新核对连接')
                    key=f"{task['id']}:{old['id']}"
                    previous=next((d for d in store.decisions(task['id']) if d['id']==key),None)
                    if previous:
                        store.write('decision',{**previous,'status':'pending','superseded_by':ident})
                    task['superseded_observations']=task.get('superseded_observations',[])+[old['id']]
                    anchor=task.get('query_anchor',task.get('anchor'))
                    if anchor==old['id']:task['query_anchor']=None
                store.write('observation',observation)
                store.write('decision',dict(id=f"{task['id']}:{ident}",task_id=task['id'],observation_id=ident,status='pending'))
                task['version']+=1;store.write('task',task)
                store.audit(task['id'],'observation_revision',dict(before=old,after=observation,affected=changed))
            return dict(observation={k:v for k,v in observation.items() if k!='feature'},affected_connections=changed,task_version=task['version'])
        except Exception:
            (store.root/relative).unlink(missing_ok=True)
            raise
