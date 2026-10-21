"""Render only actual recorded detector samples, keeping their media timestamps.

The output is a sampled inspection video, not a dense per-frame tracking result.
Local track IDs are not claimed to be cross-camera identities.
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import sys

import av
from PIL import ImageDraw

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.store import Store, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    store = Store.open_readonly(args.data)
    summaries = []
    try:
        samples = store.list('index_frame')
        for media in store.list('media'):
            rows = sorted([r for r in samples if r['media_id'] == media['id']], key=lambda r:r['frame_index'])
            by_index = {row['frame_index']: row for row in rows}
            path = args.output / (media['camera_id'] + '-采样跟踪.mp4')
            if path.exists():
                raise ValueError('输出已存在，不覆盖')
            source = store.asset(media['path'])
            if fingerprint(source) != media['fingerprint']:
                raise ValueError('源文件已改变')
            encoded, timestamps = 0, []
            with av.open(str(path), 'w', options={'movflags': '+faststart'}) as out:
                stream = out.add_stream('libx264', rate=30)
                stream.width, stream.height, stream.pix_fmt = 1280, 720, 'yuv420p'
                stream.options = {'preset': 'veryfast', 'crf': '23'}
                stream.time_base = Fraction(1, 1000)
                stream.codec_context.time_base = Fraction(1, 1000)
                with av.open(str(source)) as container:
                    for index, frame in enumerate(container.decode(video=0)):
                        if index not in by_index:
                            continue
                        row = by_index[index]
                        if frame.pts != row['raw_pts']:
                            raise ValueError('保存的采样PTS与实际源帧不符')
                        image = frame.to_image().resize((1280, 720))
                        draw = ImageDraw.Draw(image)
                        for detection in row['detections']:
                            x1, y1, x2, y2 = detection['box']
                            box = (x1*1280/row['width'], y1*720/row['height'],
                                   x2*1280/row['width'], y2*720/row['height'])
                            tracked = detection['detection_kind'] == 'tracked'
                            color = '#45efad' if tracked else '#ffd05a'
                            draw.rectangle(box, outline=color, width=2)
                            label = (media['camera_id'] + ' T' + str(detection['local_track'])) if tracked else 'untracked detection'
                            draw.text((box[0]+2, max(25, box[1]-13)), label, fill=color,
                                      stroke_width=1, stroke_fill='black')
                        draw.rectangle((0, 0, 1280, 23), fill='black')
                        draw.text((8, 5), f"{media['camera_id']} | media={row['media_s']:.3f}s | ACTUAL SAMPLES ONLY | local IDs, not global identity", fill='white')
                        video = av.VideoFrame.from_image(image)
                        video.pts = round(row['media_s']*1000)
                        video.time_base = Fraction(1, 1000)
                        for packet in stream.encode(video):
                            out.mux(packet)
                        encoded += 1
                        timestamps.append(row['media_s'])
                for packet in stream.encode():
                    out.mux(packet)
            if fingerprint(source) != media['fingerprint'] or encoded != len(rows):
                raise ValueError('源指纹或预览采样数不一致')
            summaries.append(dict(camera_id=media['camera_id'], path=str(path.resolve()),
                samples=encoded, source_sha256=media['fingerprint'], media_times_s=timestamps,
                dense_tracking=False, cross_camera_identity_certified=False))
            print(media['camera_id'] + ': ' + str(encoded) + ' actual samples', flush=True)
    finally:
        store.db.close()
    (args.output/'预览映射.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
