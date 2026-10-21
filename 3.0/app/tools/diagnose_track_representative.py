"""Compare a frozen track representative with every recorded sample crop.

This is a diagnostic using an already confirmed query, not a new ranking policy.
It leaves tasks, candidates, model parameters and source media unchanged.
"""
import argparse
import json
from pathlib import Path
import sys

import av
import numpy as np
import torch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.engine import Encoder
from tracking3.media import crop
from tracking3.store import Store, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--observation', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('诊断输出已存在，不覆盖')
    store = Store.open_readonly(args.data)
    try:
        query = store.get('task', args.task)
        observation = store.get('observation', args.observation)
        media = store.get('media', observation['media_id'])
        source = store.asset(media['path'])
        if fingerprint(source) != media['fingerprint']:
            raise ValueError('源指纹不一致')
        torch.set_num_threads(4)
        encoder = Encoder(osnet=APP/'models/osnet_x0_25_msmt17.pth', device='cpu')
        if query['encoder'] != encoder.version or observation['encoder'] != encoder.version:
            raise ValueError('查询或轨迹编码器版本不一致')
        expected = {}
        for row in store.list('index_frame'):
            if row['media_id'] != media['id']:
                continue
            for detection in row['detections']:
                if detection['observation_id'] == observation['id']:
                    expected[row['frame_index']] = row, detection
        scores = []
        with av.open(str(source)) as container:
            for index, frame in enumerate(container.decode(video=0)):
                if index not in expected:
                    continue
                row, detection = expected[index]
                if frame.pts != row['raw_pts']:
                    raise ValueError('采样PTS不一致')
                patch = crop(frame.to_ndarray(format='rgb24'), detection['box'])
                vector = encoder.extract(patch)
                scores.append(dict(media_s=row['media_s'], source_frame=index,
                    detector_confidence=detection['confidence'], box=detection['box'],
                    cosine=float(np.dot(query['feature'], vector)),
                    current_representative=abs(row['media_s']-observation['representative']) < 1e-6))
        if len(scores) != len(expected) or fingerprint(source) != media['fingerprint']:
            raise ValueError('采样数或源指纹变化')
        result = dict(status='diagnostic_only_baseline_unchanged', task_id=query['id'],
            observation_id=observation['id'], source_sha256=media['fingerprint'], encoder=encoder.version,
            baseline_representative_s=observation['representative'],
            baseline_cosine=float(np.dot(query['feature'], observation['feature'])),
            all_recorded_samples=scores, dense_frames_inferred=False,
            deployed_ranking_change=False, identity_ground_truth=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'samples':len(scores), 'baseline':result['baseline_cosine'],
                          'highest_sample_cosine':max(s['cosine'] for s in scores),
                          'baseline_unchanged':True}))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
