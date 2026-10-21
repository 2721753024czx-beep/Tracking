"""Versioned venue and camera metadata used to explain locations."""
import time


def configuration(store):
    try:
        return store.get('venue', 'main')
    except ValueError:
        return dict(id='main', version=0, name='', cameras=[])


def _camera(item):
    if not isinstance(item, dict):
        raise ValueError('每个机位必须是对象')
    ident = str(item.get('id', '')).strip()
    if not ident or len(ident) > 100:
        raise ValueError('机位编号不能为空且不超过100字符')
    values = {}
    for key, limit in (('label', 200), ('floor', 100), ('area', 200),
                       ('orientation', 200), ('notes', 1000)):
        value = str(item.get(key, '')).strip()
        if len(value) > limit:
            raise ValueError(f'{key}过长')
        values[key] = value
    return dict(id=ident, **values)


def normalize(data):
    name = str(data.get('name', '')).strip()
    if len(name) > 200:
        raise ValueError('场所名称过长')
    cameras = data.get('cameras', [])
    if not isinstance(cameras, list) or len(cameras) > 500:
        raise ValueError('机位必须是最多500条的列表')
    result, seen = [], set()
    for item in cameras:
        camera = _camera(item)
        if camera['id'] in seen:
            raise ValueError('机位编号不能重复')
        seen.add(camera['id'])
        result.append(camera)
    return name, result


def camera(store, camera_id):
    ident = str(camera_id).strip()
    return next((item for item in configuration(store)['cameras'] if item['id'] == ident), None)


def update(store, data):
    name, cameras = normalize(data)
    with store.lock, store.db:
        old = configuration(store)
        if data.get('expected_version') != old['version']:
            raise ValueError('场所配置已变化，请重新载入')
        new = dict(id='main', version=old['version'] + 1, name=name,
                   cameras=cameras, at=time.time())
        store.write('venue', new)
        store.audit(None, 'venue', dict(before=old, after=new))
        for task in store.list('task'):
            task['version'] += 1
            store.write('task', task)
            store.audit(task['id'], 'venue-revised', {'venue_version': new['version']})
    return new
