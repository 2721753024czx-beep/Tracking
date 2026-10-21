"""Read actual index samples, without inferring unsampled or legacy frames."""
from fractions import Fraction
import math


def snapshot(store, media_ids):
    media = {ident:store.get('media',ident) for ident in media_ids}
    rows = [r for r in store.list('index_frame') if r['media_id'] in media]
    for row in rows:
        source=media[row['media_id']]
        if row.get('version')!=1 or row.get('source_sha256')!=source['fingerprint']:
            raise ValueError('轨迹采样与源版本不一致，不能混入评估快照')
        if type(row['frame_index']) is not int or row['frame_index']<0 or row['frame_index_base']!=0:
            raise ValueError('轨迹源帧号无效')
        reconstructed=float(row['raw_pts']*Fraction(row['time_base']))-row['origin_pts_s']
        if not math.isfinite(reconstructed) or not math.isfinite(row['media_s']) or abs(reconstructed-row['media_s'])>1e-6:
            raise ValueError('轨迹源帧时间不一致')
        sample = row.get('detector_sample')
        if sample is not None:
            from .detection import POLICY, RETAIN_CONFIDENCE
            raw, selected = sample['raw_detections'], sample['returned_detection_indices']
            count = sample['tracker_returned_count']
            if (sample['policy'] != POLICY or sample['retention_confidence'] != RETAIN_CONFIDENCE or
                    type(count) is not int or count != len(selected) or len(set(selected)) != len(selected) or
                    any(type(i) is not int or not 0 <= i < len(raw) for i in selected) or
                    len(row['detections']) != count + sample['retained_filtered_count']):
                raise ValueError('原始检测与保留线索的来源计数不一致')
            expected = [i for i, detection in enumerate(raw) if i not in selected and
                        detection['class_id'] == 0 and detection['confidence'] >= RETAIN_CONFIDENCE]
            if [d.get('raw_detection_index') for d in row['detections'][count:]] != expected:
                raise ValueError('单帧检测线索缺失或重复，不能导出该采样')
            for position, detection in enumerate(row['detections']):
                index = detection['raw_detection_index']
                if type(index) is not int or not 0 <= index < len(raw):
                    raise ValueError('保留检测的原始索引无效')
                if position < count:
                    if index != selected[position]:
                        raise ValueError('跟踪返回检测索引与采样不一致')
                elif (index in selected or detection.get('detection_kind') != 'single_frame' or
                        detection.get('retention_reason') != 'tracker_filtered' or
                        raw[index]['class_id'] != 0 or detection['confidence'] < RETAIN_CONFIDENCE or
                        detection['confidence'] != raw[index]['confidence'] or detection['box'] != raw[index]['box']):
                    raise ValueError('单帧线索与原始检测不一致')
    rows.sort(key=lambda r:(r['media_id'],r['run_no'],r['frame_index']))
    return dict(format='tracking3-index-samples-v1', frames=rows,
        sources=[dict(media_id=ident,source_sha256=m.get('fingerprint'),
                      sample_count=sum(r['media_id']==ident for r in rows),
                      status='recorded_samples_only' if any(r['media_id']==ident for r in rows)
                             else 'not_recorded_legacy_or_unindexed') for ident,m in sorted(media.items())],
        unsampled_frames_inferred=False, empty_detections_are_ground_truth_absence=False,
        trajectory_identity_certified=False)
