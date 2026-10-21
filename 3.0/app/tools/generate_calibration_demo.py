"""Create independently readable synthetic shared flashes with fixed/drifting clocks."""
import argparse
import json
import sys
from pathlib import Path

import av
from PIL import Image, ImageDraw

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.media import probe
from tracking3.store import Store, fingerprint
from tracking3.timing import metadata


def generate(output):
    root = Path(output)
    if root.exists():
        raise ValueError('演示输出目录已存在，请选择新目录')
    root.mkdir(parents=True)
    store = Store(root/'data')
    try:
        for ident, length, origin, rate in [('reference',8,0,1),('fixed',9,1,1),('drifting',10,1,1.1)]:
            path = store.root/f'uploads/{ident}.mp4'
            with av.open(str(path),'w') as out:
                stream = out.add_stream('mpeg4',rate=10)
                stream.width, stream.height, stream.pix_fmt = 320,240,'yuv420p'
                for index in range(length*10):
                    local = index/10
                    common = (local-origin)/rate
                    event = next((i+1 for i,t in enumerate((1,3,6)) if t-1e-6 <= common < t+.2-1e-6),None)
                    image = Image.new('RGB',(320,240),(240,230,100) if event else (20,35,50))
                    draw = ImageDraw.Draw(image)
                    draw.text((12,12),'SYNTHETIC CLOCK FIXTURE',fill='white')
                    draw.text((12,40),f'{ident}: local {local:.1f}s',fill='white')
                    if event: draw.text((80,120),f'COMMON EVENT E{event}',fill='black')
                    frame = av.VideoFrame.from_image(image)
                    for packet in stream.encode(frame): out.mux(packet)
                for packet in stream.encode(): out.mux(packet)
            store.put('media',dict(id=ident,path=f'uploads/{ident}.mp4',name=path.name,camera_id=ident,
                location='人工构造共同时钟闪光',fingerprint=fingerprint(path),status='unprocessed',
                processed_until=-1,time_version=1,**metadata({}),**probe(path)))
        for name, target, seconds, known in [('constant','fixed',(2,4,7),True),
                                            ('drift','drifting',(2.1,4.3,7.6),True),
                                            ('unknown','fixed',(2,4,7),False)]:
            request = dict(reference_media_id='reference',target_media_id=target,
                reference_fingerprint=store.get('media','reference')['fingerprint'],
                target_fingerprint=store.get('media',target)['fingerprint'], points=[])
            for i,(reference,other) in enumerate(zip((1,3,6),seconds)):
                request['points'].append(dict(event_id=f'E{i+1}',reference_second=reference,
                    target_second=other,reference_tolerance=.02 if known else None,
                    target_tolerance=.02 if known else None,
                    evidence=f'生成器预设的共同闪光E{i+1}；人工合成，不代表实际摄像头同步'))
            (root/f'{name}.json').write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n')
        return root
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    print(generate(parser.parse_args().output))


if __name__=='__main__': main()
