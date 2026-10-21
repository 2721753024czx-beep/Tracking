"""Read-only diagnostics for independently matched shared video events."""
from statistics import median

from .media import finite


def analyze(points):
    if not isinstance(points, list) or not 1 <= len(points) <= 100:
        raise ValueError('请提供1至100个共同事件，建议至少3个分布在开头、中段和结尾')
    rows, identifiers = [], set()
    for point in points:
        if not isinstance(point, dict):
            raise ValueError('每个共同事件须是对象')
        ident = str(point.get('event_id') or '').strip()
        evidence = str(point.get('evidence') or '').strip()
        if not ident or len(ident) > 100 or ident in identifiers:
            raise ValueError('共同事件编号须非空、唯一且不超过100字符')
        if not evidence or len(evidence) > 2000:
            raise ValueError('每个事件须填写不超过2000字符的共同事件依据')
        identifiers.add(ident)
        if any(point.get(key) is None or isinstance(point.get(key), bool) for key in ('reference_second', 'target_second')):
            raise ValueError('每个事件须填写两份录像的实际秒数')
        ref = finite(point.get('reference_second'), '参考录像事件秒数', 0)
        target = finite(point.get('target_second'), '待校录像事件秒数', 0)
        bounds = []
        for key in ('reference_tolerance', 'target_tolerance'):
            value = point.get(key)
            if isinstance(value, bool):
                raise ValueError('事件定位误差须为数值或未知')
            bounds.append(None if value is None or value == '' else finite(value, '事件定位误差', 0))
        bound = None if None in bounds else sum(bounds)
        rows.append(dict(event_id=ident, reference_second=ref, target_second=target,
                         reference_tolerance=bounds[0], target_tolerance=bounds[1],
                         difference=ref-target, total_annotation_bound=bound, evidence=evidence,
                         human_matching_confirmed=point.get('human_matching_confirmed') is True))
    rows.sort(key=lambda row: row['reference_second'])
    if len({r['reference_second'] for r in rows}) != len(rows) or len({r['target_second'] for r in rows}) != len(rows):
        raise ValueError('多个事件不能使用同一个定位时刻，请选择分开的共同事件')
    order_valid = all(b['target_second'] > a['target_second'] for a, b in zip(rows, rows[1:]))
    offset = median(row['difference'] for row in rows)
    for row in rows:
        row['residual_from_median'] = row['difference']-offset
    known = all(row['total_annotation_bound'] is not None for row in rows)
    interval = None
    if known:
        lower = max(row['difference']-row['total_annotation_bound'] for row in rows)
        upper = min(row['difference']+row['total_annotation_bound'] for row in rows)
        interval = [lower, upper] if lower <= upper+1e-9 else []
        if interval and lower > upper: interval = [(lower+upper)/2]*2
    if not order_valid:
        status, compatible = 'event_order_conflict', False
    elif known and interval == []:
        status, compatible = 'fixed_offset_inconsistent', False
    elif len(rows) < 3:
        status, compatible = 'insufficient_points', None
    elif not known:
        status, compatible = 'annotation_bounds_unknown', None
    else:
        status, compatible = 'consistent_at_observed_points', True
    return dict(status=status, point_count=len(rows), points=rows,
                mapping='reference_media_s ≈ target_media_s + offset_to_reference_s',
                median_offset_to_reference_s=offset,
                candidate_offset_to_reference_s=(sum(interval)/2 if compatible is True else None),
                feasible_offset_interval_s=interval,
                compatible_at_observed_points=compatible,
                max_abs_residual_s=max(abs(row['residual_from_median']) for row in rows),
                observed_offset_range_s=max(row['difference'] for row in rows)-min(row['difference'] for row in rows),
                endpoint_offset_change_s=rows[-1]['difference']-rows[0]['difference'],
                reference_observed_interval_s=[rows[0]['reference_second'], rows[-1]['reference_second']],
                target_observed_interval_s=[min(row['target_second'] for row in rows),max(row['target_second'] for row in rows)],
                uncertainty_to_register=None, time_metadata_changed=False,
                interpretation='仅诊断给定事件定位误差下的固定偏移相容性；不证明事件配对正确、未采样区间、摄像头拍摄时钟或真实同步精度')
