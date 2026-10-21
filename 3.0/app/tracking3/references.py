"""Explicit multi-reference query configuration."""
import numpy as np

from .integrity import check


def values(store, task):
    return list(task.get('reference_observations') or [])


def configure(store, data):
    with store.lock, store.db:
        task = store.get('task', data['task_id'])
        from .tasks import require_active
        require_active(task)
        if data.get('expected_task_version') != task['version']:
            raise ValueError('任务已变化，请刷新后配置参考片段')
        ids = data.get('observation_ids') or []
        if not isinstance(ids, list) or len(ids) > 5 or len(set(ids)) != len(ids):
            raise ValueError('最多选择5个不重复的参考片段')
        if not ids:
            raise ValueError('至少选择一个已确认片段')
        query = np.asarray(task.get('feature'), dtype=float)
        if query.ndim != 1 or query.size == 0 or not np.isfinite(query).all():
            raise ValueError('任务缺少有效外观特征，请重新创建查询')
        vectors = []
        for ident in ids:
            observation = store.get('observation', ident)
            if not task.get('encoder') or observation.get('encoder') != task['encoder']:
                raise ValueError('参考片段与任务编码器不同或未知，请使用同一编码器的片段')
            if store.observation_owner(observation) not in (None, task['id']):
                raise ValueError('参考片段不属于当前任务')
            decision = store.get('decision', f"{task['id']}:{ident}")
            if decision['status'] != 'confirmed':
                raise ValueError('所有参考片段都必须先确认身份')
            media = store.get('media', observation['media_id'])
            if not check(store, media)['valid']:
                raise ValueError('参考片段源录像失效或未核实')
            vector = np.asarray(observation.get('feature'), dtype=float)
            if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
                raise ValueError('参考片段缺少有效外观特征')
            if vector.shape != query.shape:
                raise ValueError('参考片段与任务外观特征维度不同，请重新核实索引')
            vectors.append(vector)
        feature = np.mean(np.stack(vectors), axis=0)
        norm = np.linalg.norm(feature)
        if not np.isfinite(norm) or norm == 0:
            raise ValueError('参考片段外观特征无法合并')
        task.update(feature=(feature / norm).tolist(), reference_observations=ids,
                    reference_review_required=False, version=task['version'] + 1)
        store.write('task', task)
        store.audit(task['id'], 'references', {'observation_ids': ids})
        return task
