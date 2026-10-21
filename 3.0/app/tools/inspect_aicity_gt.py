"""Inspect fixed-version 2024 AICity GT; never infer visible time from a gap."""
import argparse
from array import array
from collections import Counter
import hashlib
import itertools
import json
import math
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--fps',required=True,type=float,help='来源文档的帧率；不是视频实测值')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('输出已存在，保留旧检查记录')
    if not math.isfinite(args.fps) or args.fps<=0:
        raise ValueError('fps须为有限正数')
    digest = hashlib.sha256()
    with args.input.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            digest.update(block)
    if digest.hexdigest()!=args.expected_sha256:
        raise ValueError('输入SHA与固定版本官方指纹不一致')
    tracks, frame_sets, rows, invalid_boxes = {}, {}, 0, 0
    with args.input.open() as f:
        for number,line in enumerate(f,1):
            fields=line.split()
            if len(fields)!=9:
                raise ValueError(f'第{number}行不是9字段GT')
            camera,person,frame = map(int,fields[:3])
            geometry=list(map(float,fields[3:]))
            if min(camera,person,frame)<0 or not all(map(math.isfinite,geometry)):
                raise ValueError(f'第{number}行ID或几何数据无效')
            x,y,w,h=geometry[:4]
            rows += 1
            invalid_boxes += int(w<=0 or h<=0)
            item=tracks.setdefault((camera,person),{'frames':array('I'),'positive_bbox_rows':0,
                'max_bbox_area_px':0,'largest_bbox_frame':None,'largest_bbox':None})
            item['frames'].append(frame)
            frame_sets.setdefault(camera,set()).add(frame)
            item['positive_bbox_rows'] += int(w>0 and h>0)
            if w*h>item['max_bbox_area_px'] and w>0 and h>0:
                item.update(max_bbox_area_px=w*h,largest_bbox_frame=frame,largest_bbox=[x,y,x+w,y+h])
    summaries=[]
    for (camera,person),item in sorted(tracks.items()):
        frames=sorted(item.pop('frames'))
        if len(frames)!=len(set(frames)):
            raise ValueError(f'camera={camera} person={person}有重复帧标注')
        intervals=[]
        start=previous=frames[0]
        for at in frames[1:]:
            if at!=previous+1:
                intervals.append([start,previous])
                start=at
            previous=at
        intervals.append([start,previous])
        summaries.append(dict(camera_id=camera,object_id=person,annotation_rows=len(frames),
            first_frame=frames[0],last_frame=frames[-1],annotation_runs=intervals,**item))
    cameras=[]
    for camera,frames in sorted(frame_sets.items()):
        ordered=sorted(frames)
        gaps=Counter(b-a for a,b in zip(ordered,ordered[1:]))
        cameras.append(dict(camera_id=camera,annotated_frames=len(ordered),first_frame=ordered[0],
            last_frame=ordered[-1],frame_gap_histogram=dict(gaps),
            object_ids=sorted(p for c,p in tracks if c==camera)))
    pairs=[]
    for a,b in itertools.combinations(sorted(frame_sets),2):
        shared=sorted({p for c,p in tracks if c==a}&{p for c,p in tracks if c==b})
        pairs.append(dict(cameras=[a,b],shared_object_ids=shared,
            common_annotated_frames=len(frame_sets[a]&frame_sets[b])))
    result=dict(format='tracking3-aicity-gt-inspection-v1',sha256=digest.hexdigest(),
        rows=rows,positive_area_validation_failures=invalid_boxes,cameras=cameras,tracks=summaries,
        pairs=pairs,documented_fps=args.fps,video_fps_validated=False,
        annotation_gaps_are_not_exit_evidence=True,visible_intervals_not_certified=True,
        known_corrupt_camera_from_data_card=649)
    content=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    with args.output.open('x') as f:
        f.write(content)
    print(json.dumps({'rows':rows,'cameras':len(cameras),'tracks':len(summaries),
        'invalid_boxes':invalid_boxes,'output':str(args.output)},ensure_ascii=False))


if __name__=='__main__':
    main()
