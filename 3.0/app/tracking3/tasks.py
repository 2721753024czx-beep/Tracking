"""Versioned task lifecycle independent from shared video indexing."""
import time


def require_active(task):
    if task['status'] != 'active':
        raise ValueError('任务已暂停或结束，请先继续任务或重新打开后修改')


def change_status(store,data):
    target=data.get('status')
    if target not in ('active','paused','ended'):
        raise ValueError('无效任务状态')
    reason=str(data.get('reason','')).strip()
    if len(reason)>2000:
        raise ValueError('状态说明不超过2000字')
    with store.lock,store.db:
        task=store.get('task',data['task_id'])
        if data.get('expected_task_version')!=task['version']:
            raise ValueError('任务版本已变化，请刷新后修改状态')
        if task['status']==target:
            return task
        if task['status']=='ended' and target!='active':
            raise ValueError('已结束任务须先重新打开')
        before=dict(task)
        task.update(status=target,reason=reason,version=task['version']+1,status_at=time.time())
        store.write('task',task)
        store.audit(task['id'],'status',dict(before=before,after=task))
        return task
