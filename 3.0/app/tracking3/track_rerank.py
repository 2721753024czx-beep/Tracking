"""Compare frozen single-frame ranking to experimental multi-frame ranking."""
from collections import defaultdict
import math

import numpy as np

from .appearance_review import _features
from .engine import search
from .index_trace import snapshot
from .store import Store


POLICY = 'nonedge-time-bins-centroid-v1'
GALLERY_POLICY = 'same-frames-top2-cosine-mean-v1'
GALLERY_TOP_MATCHES = 2
EDGE_PIXELS = 1
MAX_FRAMES = 5


def _touches_boundary(sample):
    x1, y1, x2, y2 = sample['detection']['box']
    return min(x1, y1, sample['width'] - x2, sample['height'] - y2) <= EDGE_PIXELS


def _select_frames(samples):
    """Choose frames with metadata only, independent of the query feature."""
    interior = [sample for sample in samples if not _touches_boundary(sample)]
    avoid_boundary = len(interior) >= 2
    pool = sorted(interior if avoid_boundary else samples,
                  key=lambda row: (row['media_s'], row['frame_index']))
    count = min(MAX_FRAMES, len(pool))
    first, last = pool[0]['media_s'], pool[-1]['media_s']
    bins = {}
    for sample in pool:
        if last > first:
            position = (sample['media_s'] - first) / (last - first)
        else:
            # Frame order remains a meaningful fallback for equal timestamps.
            position = (sample['frame_index'] - pool[0]['frame_index']) / (
                pool[-1]['frame_index'] - pool[0]['frame_index'])
        bin_no = min(count - 1, int(position * count))
        current = bins.get(bin_no)
        if current is None or sample['detection']['confidence'] > current['detection']['confidence']:
            bins[bin_no] = sample
    reason = 'nonedge_preferred' if avoid_boundary else 'insufficient_nonedge_frames'
    return [bins[i] for i in sorted(bins)], reason


def _sample_groups(store, candidates, media_ids):
    groups = defaultdict(list)
    for row in snapshot(store, media_ids)['frames']:
        for detection in row['detections']:
            ident = detection['observation_id']
            if ident not in candidates:
                continue
            candidate = candidates[ident]
            box = detection['box']
            if (row['media_id'] != candidate['media_id'] or row['camera_id'] != candidate['camera_id']
                    or row['encoder'] != candidate['encoder']
                    or str(detection['local_track']) != str(candidate['local_track'])
                    or not candidate['start'] - 1e-6 <= row['media_s'] <= candidate['end'] + 1e-6
                    or len(box) != 4 or not all(math.isfinite(value) for value in box)
                    or box[0] >= box[2] or box[1] >= box[3]
                    or not 0 <= detection['confidence'] <= 1):
                raise ValueError('采样框与候选片段记录不一致')
            groups[ident].append({**row, 'detection': detection})
    for samples in groups.values():
        if len({row['frame_index'] for row in samples}) != len(samples):
            raise ValueError('候选片段包含重复源帧')
    return groups


def _unit_vectors(query, vectors):
    values = np.asarray(vectors, dtype=np.float64)
    if values.shape != (len(vectors), query.size) or not np.isfinite(values).all():
        raise ValueError('采样特征和查询特征不兼容')
    norms = np.linalg.norm(values, axis=1)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise ValueError('采样特征不能归一化')
    return values / norms[:, None]


def _centroid_score(query, unit_vectors):
    """Keep the persisted query fixed and combine the selected gallery vectors."""
    centroid = np.mean(unit_vectors, axis=0)
    norm = np.linalg.norm(centroid)
    if norm <= 1e-12:
        return None
    return round(float(query @ (centroid / norm)), 4)


def _gallery_score(query, unit_vectors):
    similarities = np.sort(unit_vectors @ query)
    return round(float(np.mean(similarities[-GALLERY_TOP_MATCHES:])), 4)


def compare(data, encoder, task_id, *, compare_gallery=False):
    """Run independent ranking on one read-only DB snapshot and verified media."""
    store = Store.open_readonly(data)
    try:
        store.db.execute('BEGIN')
        task = store.get('task', task_id)
        ranked = search(store, encoder, task_id, limit=len(store.list('observation')))
        media = {ident: store.get('media', ident) for ident in dict.fromkeys(c['media_id'] for c in ranked)}
        for candidate in ranked:
            if not candidate['source_integrity']['valid']:
                raise ValueError(candidate['source_integrity']['reason'])
            if media[candidate['media_id']]['status'] != 'complete':
                raise ValueError('请在候选素材处理完成后比较排名')

        query = np.asarray(task['feature'], dtype=np.float64)
        if query.ndim != 1 or not query.size or not np.isfinite(query).all() or not np.any(query):
            raise ValueError('查询特征无效')
        tracked = {c['id']: c for c in ranked if c.get('source') == 'detected'
                   and c.get('detection_kind') != 'single_frame' and c['end'] > c['start']}
        groups = _sample_groups(store, tracked, {c['media_id'] for c in tracked.values()})
        selected_by_media = defaultdict(list)
        rows = []
        for rank, candidate in enumerate(ranked, 1):
            ident = candidate['id']
            samples = groups.get(ident, [])
            result = dict(observation_id=ident, media_id=candidate['media_id'], camera_id=candidate['camera_id'],
                          start_s=candidate['start'], end_s=candidate['end'],
                          representative_s=candidate['representative'], status=candidate['status'],
                          baseline_rank=rank, baseline_score=candidate['score'], multiframe_score=candidate['score'],
                          policy='baseline', selected_frames=[])
            if compare_gallery:
                result.update(gallery_score=candidate['score'], gallery_policy='baseline')
            rows.append(result)
            if ident not in tracked:
                result['selection'] = 'manual_or_single_frame'
            elif len(samples) < 2:
                result['selection'] = 'insufficient_recorded_samples'
            else:
                selected, result['selection'] = _select_frames(samples)
                result['selected_frames'] = [dict(frame_index=row['frame_index'], media_s=row['media_s'],
                    confidence=row['detection']['confidence'], touches_boundary=_touches_boundary(row))
                    for row in selected]
                selected_by_media[candidate['media_id']].append((result, selected))

        for ident, selections in selected_by_media.items():
            flattened = [sample for _, selected in selections for sample in selected]
            vectors, _ = _features(store, encoder, media[ident], flattened)
            offset = 0
            for result, selected in selections:
                end = offset + len(selected)
                unit_vectors = _unit_vectors(query, vectors[offset:end])
                score = _centroid_score(query, unit_vectors)
                if score is None:
                    result['selection'] = 'degenerate_centroid'
                else:
                    result.update(policy=POLICY, multiframe_score=score)
                if compare_gallery and len(unit_vectors) >= GALLERY_TOP_MATCHES:
                    result.update(gallery_score=_gallery_score(query, unit_vectors),
                                  gallery_policy=GALLERY_POLICY)
                offset = end

        # Ties retain baseline order. The output itself lists the baseline order.
        for rank, result in enumerate(sorted(rows, key=lambda row: -row['multiframe_score']), 1):
            result['multiframe_rank'] = rank
        report = dict(format='tracking3-track-appearance-comparison-v1', task_id=task_id,
                      task_version=task['version'], encoder=encoder.version, policy=POLICY,
                      max_selected_frames=MAX_FRAMES, edge_pixels=EDGE_PIXELS,
                      rank_tie_break='baseline_order', score_decimals=4, candidate_count=len(rows),
                      sources=[dict(media_id=m['id'], camera_id=m['camera_id'], source_sha256=m['fingerprint'])
                               for m in media.values()], candidates=rows)
        if compare_gallery:
            for rank, result in enumerate(sorted(rows, key=lambda row: -row['gallery_score']), 1):
                result['gallery_rank'] = rank
            report.update(gallery_policy=GALLERY_POLICY, gallery_top_matches=GALLERY_TOP_MATCHES)
        return report
    finally:
        store.db.close()
