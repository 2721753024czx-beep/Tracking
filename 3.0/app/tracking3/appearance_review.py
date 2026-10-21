"""Task-owned, source-bound appearance suggestions; never confirm identity."""
from collections import deque
from fractions import Fraction
import hashlib
import json
import time

import av
import numpy as np

from .appearance_segments import propose
from .integrity import require as require_source
from .media import crop
from .store import uid
from .tasks import require_active
from .trace_review import _read


RULE = 'rolling-stable-5-two-coherent-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def warnings(vectors, segments, threshold):
    """Flag retained low-reference points; do not discard or identify them."""
    values = np.asarray(vectors, dtype=np.float64)
    values /= np.linalg.norm(values, axis=1)[:, None]
    result = []
    for segment in segments:
        history = deque([values[segment[0]]], maxlen=5)
        for index in segment[1:]:
            mean = np.mean(history, axis=0)
            norm = np.linalg.norm(mean)
            reference = mean / norm if norm > 1e-12 else history[-1]
            score = float(np.clip(values[index] @ reference, -1, 1))
            if score < threshold:
                result.append(dict(index=index, similarity=score,
                    kind='retained_low_reference_sample'))
            else:
                history.append(values[index])
    return result


def _validate(store, task_id, observation_id, expected_version):
    task, observation, media, samples = _read(store, task_id, observation_id)
    require_active(task)
    if type(expected_version) is not int or task['version'] != expected_version:
        raise ValueError('任务已变化，请重新打开轨迹')
    if observation_id in task.get('superseded_observations', []):
        raise ValueError('历史片段已被纠正，仅允许回看')
    if observation.get('source') == 'manual' or not samples:
        raise ValueError('没有此片段自己的自动采样轨迹')
    if media['status'] == 'processing':
        raise ValueError('请暂停素材处理后生成或核对建议')
    require_source(store, media)
    return task, observation, media, samples


def _features(store, encoder, media, samples):
    # Decode the source once; do not substitute a nearby requested second.
    wanted = {}
    for index, sample in enumerate(samples):
        wanted.setdefault(sample['frame_index'], []).append((index, sample))
    features = [None] * len(samples)
    crop_hashes = [None] * len(samples)
    origin = None
    with av.open(str(store.asset(media['path']))) as container:
        for index, frame in enumerate(container.decode(video=0)):
            if frame.pts is None:
                raise ValueError('源录像缺少帧时间')
            raw_time = float(frame.pts * frame.time_base)
            if origin is None:
                origin = raw_time
            if index not in wanted:
                continue
            rgb = frame.to_ndarray(format='rgb24')
            for position, sample in wanted.pop(index):
                if (frame.pts != sample['raw_pts'] or
                        Fraction(frame.time_base) != Fraction(sample['time_base']) or
                        abs(origin - sample['origin_pts_s']) > 1e-6 or
                        abs(raw_time - origin - sample['media_s']) > 1e-6 or
                        frame.width != sample['width'] or frame.height != sample['height']):
                    raise ValueError('源帧与实际采样记录不同')
                patch = crop(rgb, sample['detection']['box'])
                crop_hashes[position] = hashlib.sha256(patch.tobytes()).hexdigest()
                features[position] = encoder.extract(patch)
            if not wanted:
                break
    if wanted:
        raise ValueError('源录像缺少实际采样帧')
    require_source(store, media)
    return features, crop_hashes


def generate(store, encoder, data):
    threshold = data.get('threshold')
    # Validate parameters before expensive I/O. No globally selected threshold.
    if type(threshold) not in (int, float):
        raise ValueError('请显式填写数值外观阈值')
    propose([], threshold=threshold)
    with store.lock:
        task, observation, media, samples = _validate(store, data['task_id'],
            data['observation_id'], data.get('expected_task_version'))
        if task['encoder'] != encoder.version or observation['encoder'] != encoder.version:
            raise ValueError('须使用任务和轨迹原编码器生成建议')
        if len(samples) > 5000:
            raise ValueError('当前逐点建议限5000个实际样本；没有截断或生成建议')
        vectors, crop_hashes = _features(store, encoder, media, samples)
        partition = propose(vectors, threshold=threshold)
        segments = []
        for number, indices in enumerate(partition['segments']):
            representative = max(indices, key=lambda i: samples[i]['detection']['confidence'])
            segments.append(dict(number=number, indices=indices,
                start=samples[indices[0]]['media_s'], end=samples[indices[-1]]['media_s'],
                representative_index=representative))
        review = dict(id=uid(), task_id=task['id'], observation_id=observation['id'],
            created_task_version=task['version'], created_at=time.time(), revision=0,
            rule=RULE, threshold=threshold, window=5, confirmation_samples=2,
            encoder=encoder.version, source_sha256=media['fingerprint'],
            sample_sha256=digest(samples), feature_sha256=digest(vectors),
            crop_rgb_sha256=crop_hashes, samples=samples, segments=segments,
            cuts=partition['cuts'], warnings=warnings(vectors, partition['segments'], threshold),
            reviews={}, history=[], identity_certified=False,
            unsampled_frames_inferred=False, production_index_changed=False)
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            # Another process may have changed the task or trace during decoding.
            _, _, current_media, current_samples = _validate(store, task['id'],
                observation['id'], task['version'])
            if digest(current_samples) != review['sample_sha256']:
                raise ValueError('采样记录已变化，请重新生成建议')
            require_source(store, current_media)
            store.write('appearance_review', review)
            store.audit(task['id'], 'appearance_proposal', dict(review_id=review['id'],
                observation_id=observation['id'], sample_sha256=review['sample_sha256'],
                threshold=threshold, identity_certified=False))
        return review


def list_reviews(store, task_id, observation_id):
    with store.lock:
        task, _, media, samples = _read(store, task_id, observation_id)
        require_source(store, media)
        sample_sha = digest(samples)
        items = []
        for review in store.list('appearance_review'):
            if review['task_id'] == task_id and review['observation_id'] == observation_id:
                items.append(dict(review, valid=review['sample_sha256'] == sample_sha and
                    review['source_sha256'] == media['fingerprint']))
        return dict(task_id=task_id, task_version=task['version'],
            items=sorted(items, key=lambda r: r['created_at'], reverse=True))


def decide(store, data):
    status = data.get('status')
    reason = str(data.get('reason', '')).strip()
    if status not in ('dismissed', 'draft_prepared') or not reason or len(reason) > 2000:
        raise ValueError('须选择忽略或准备草稿，并填写核对说明（不超过2000字）')
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        review = store.get('appearance_review', data['review_id'])
        if review['task_id'] != data['task_id']:
            raise ValueError('建议不属于此任务')
        _, _, media, samples = _validate(store, review['task_id'], review['observation_id'],
            data.get('expected_task_version'))
        if (review['sample_sha256'] != digest(samples) or
                review['source_sha256'] != media['fingerprint']):
            raise ValueError('源或采样记录已变化，请重新生成建议')
        if (type(data.get('expected_review_revision')) is not int or
                review['revision'] != data['expected_review_revision']):
            raise ValueError('建议核对记录已变化，请重新加载')
        number = data.get('segment')
        if type(number) is not int or not 0 <= number < len(review['segments']):
            raise ValueError('无效建议片段')
        event = dict(segment=number, status=status, reason=reason, at=time.time(),
            task_version=data['expected_task_version'])
        review['reviews'][str(number)] = event
        review['history'].append(event)
        review['revision'] += 1
        store.write('appearance_review', review)
        store.audit(review['task_id'], 'appearance_review', dict(review_id=review['id'],
            revision=review['revision'], **event, identity_certified=False))
        return review
