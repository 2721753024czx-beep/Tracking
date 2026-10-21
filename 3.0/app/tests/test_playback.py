from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np

from tracking3 import playback
from tracking3.store import Store, fingerprint


class TargetPlayback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.source = self.store.root / 'uploads/source.mp4'
        # VFR with a non-zero initial PTS: frame number, raw PTS and relative
        # media time must all agree. No model or indexer is invoked in this test.
        times = [80, 100, 136, 180, 226, 260, 280]
        with av.open(str(self.source), 'w') as out:
            stream = out.add_stream('mpeg4', rate=25)
            stream.width, stream.height, stream.pix_fmt = 96, 64, 'yuv420p'
            stream.time_base = stream.codec_context.time_base = Fraction(1, 100)
            for index, pts in enumerate(times):
                rgb = np.zeros((64, 96, 3), dtype=np.uint8)
                rgb[:] = [20 + index * 25, 70, 180 - index * 20]
                frame = av.VideoFrame.from_ndarray(rgb, format='rgb24')
                frame.pts = pts; frame.time_base = Fraction(1, 100)
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        self.media = dict(id='media-a', name='VFR source', camera_id='a',
            path='uploads/source.mp4', fingerprint=fingerprint(self.source),
            status='complete', encoder='fixture', duration=2.04, width=96, height=64)
        self.store.put('media', self.media)
        self.task = dict(id='task', name='read only', version=7, encoder='fixture',
            status='paused', cameras=['b'], anchor='manual-query')
        self.store.put('task', self.task)
        self.observation = dict(id='segment-one', media_id='media-a', camera_id='a',
            encoder='fixture', source='detected', detection_kind='tracked',
            local_track='1', start=0., end=2., representative=0., crop='crops/one.jpg')
        self.store.put('observation', self.observation)
        self.store.put('observation', dict(self.observation, id='segment-two', start=1.46, end=1.46))
        self.store.put('observation', dict(self.observation, id='manual-query', source='manual', owner_task='task'))
        self.store.put('decision', dict(id='task:segment-one', task_id='task', observation_id='segment-one', status='confirmed'))
        self.rows = []
        with av.open(str(self.source)) as video:
            frames = list(video.decode(video=0))
        origin = float(frames[0].pts * frames[0].time_base)
        for position, index in enumerate([0, 2, 4, 6]):
            frame = frames[index]
            ident = 'segment-one' if position in (0, 3) else 'segment-two'
            detections = [] if position == 1 else [dict(observation_id=ident,
                local_track='1', box=[10. + index, 8., 40. + index, 56.],
                confidence=.9, detection_kind='tracked')]
            row = dict(id='sample-' + str(index), version=1, media_id='media-a',
                camera_id='a', run_no=1, source_sha256=self.media['fingerprint'],
                frame_index=index, frame_index_base=0, raw_pts=frame.pts,
                time_base=str(frame.time_base), origin_pts_s=origin,
                media_s=float(frame.pts * frame.time_base) - origin,
                width=96, height=64, sample_fps=3, encoder='fixture', detections=detections)
            self.rows.append(row); self.store.put('index_frame', row)

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def read(self, ident='segment-one'):
        return playback.inspect(self.store, 'task', ident)

    def prepare(self, ident='segment-one'):
        return playback.prepare(self.store, 'task', ident)

    def database(self):
        return [tuple(row) for row in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')], \
               [tuple(row) for row in self.store.db.execute('SELECT * FROM audit ORDER BY id')]

    def test_get_is_readonly_missing_samples_and_segment_ids_are_preserved(self):
        before = self.database()
        result = self.read()
        self.assertFalse(result['ready']); self.assertIsNone(result['preview']['path'])
        self.assertFalse((self.store.root / 'playback-previews').exists())
        self.assertEqual(result['task']['version'], 7)
        self.assertEqual(result['observation']['status'], 'confirmed')
        self.assertEqual(result['observation']['crop'], 'crops/one.jpg')
        self.assertEqual(len(result['samples']), 4)
        self.assertEqual(result['samples'][1]['detections'], [])
        # Another segment reuses local track 1. It remains another observation.
        ids = [[d['observation_id'] for d in row['detections']] for row in result['samples']]
        self.assertEqual(ids, [['segment-one'], [], ['segment-two'], ['segment-one']])
        self.assertEqual(self.read('segment-two')['samples'], result['samples'])
        self.assertEqual(self.database(), before)

    def test_real_vfr_export_matches_source_sampling_without_overlays_or_db_changes(self):
        before = self.database(); source_sha = fingerprint(self.source)
        result = self.prepare()
        self.assertTrue(result['ready']); self.assertEqual(result, self.read())
        self.assertEqual(result['preview']['sample_count'], 4)
        preview = self.store.asset(result['preview']['path'])
        self.assertEqual(fingerprint(preview), result['preview']['sha256'])
        with av.open(str(preview)) as video:
            self.assertEqual(video.streams.video[0].codec_context.name, 'h264')
            self.assertAlmostEqual(video.duration / 1_000_000, self.media['duration'])
            actual = list(video.decode(video=0))
        with av.open(str(self.source)) as video:
            source = list(video.decode(video=0))
        self.assertEqual(len(actual), 4)
        for frame, row in zip(actual, self.rows):
            self.assertAlmostEqual(float(frame.pts * frame.time_base), row['media_s'], places=6)
            next_time = self.rows[self.rows.index(row)+1]['media_s'] if row is not self.rows[-1] else self.media['duration']
            self.assertAlmostEqual(float(frame.duration * frame.time_base), next_time - row['media_s'], places=6)
            expected_rgb = source[row['frame_index']].to_ndarray(format='rgb24').astype(float)
            rgb = frame.to_ndarray(format='rgb24').astype(float)
            self.assertLess(np.max(np.abs(rgb - expected_rgb)), 6)
        self.assertEqual(fingerprint(self.source), source_sha)
        self.assertEqual(self.database(), before)
        # Same-media cache is independent of the selected target segment.
        with patch.object(playback, '_encode', side_effect=AssertionError('cache not reused')):
            self.assertEqual(self.prepare('segment-two')['preview'], result['preview'])

    def test_actual_non_widescreen_dimensions_do_not_depend_on_media_metadata(self):
        # This source is 3:2, so a missing-size fallback to 16:9 misplaces boxes.
        for metadata in ({}, dict(width=None, height=None), dict(width=160, height=90)):
            media = {k: v for k, v in self.media.items() if k not in ('width', 'height')}
            self.store.put('media', dict(media, **metadata))
            before = self.database()
            for result in (self.read(), self.prepare(), self.read()):
                self.assertEqual((result['media']['width'], result['media']['height']), (96, 64))
            self.assertEqual(self.database(), before)

    def test_cache_binds_every_sample_and_detects_modified_preview(self):
        first = self.prepare()
        manifest = self.store.root / 'playback-previews/media-a/manifest.json'
        original_binding = json.loads(manifest.read_text())['binding']
        changed = dict(self.rows[1], detector='new detector evidence')
        self.store.put('index_frame', changed)
        self.assertFalse(self.read()['ready'])
        self.prepare()
        self.assertNotEqual(json.loads(manifest.read_text())['binding'], original_binding)
        video = self.store.asset(first['preview']['path'])
        video.write_bytes(video.read_bytes() + b'changed')
        self.assertFalse(self.read()['ready'])
        self.assertTrue(self.prepare()['ready'])

    def test_foreign_scope_encoder_manual_isolated_and_absent_records_are_rejected(self):
        for changed, message in [
                (dict(self.observation, owner_task='elsewhere'), '不属于'),
                (dict(self.observation, encoder='different'), '编码器'),
                (dict(self.observation, source='manual'), '自动跟踪'),
                (dict(self.observation, detection_kind='single_frame'), '自动跟踪'),
                (dict(self.observation, id='no-own-samples'), '自己的实际跟踪')]:
            self.store.put('observation', changed)
            with self.assertRaisesRegex(ValueError, message):
                self.read(changed['id'])
            self.store.put('observation', self.observation)
        self.store.put('task', dict(self.task, anchor=None))
        with self.assertRaisesRegex(ValueError, '可访问'):
            self.read()
        for ident in ('../outside', '', None, ['segment-one']):
            with self.assertRaises(ValueError):
                self.read(ident)

    def test_absent_index_samples_and_missing_records_are_explicitly_refused(self):
        with self.assertRaisesRegex(ValueError, '记录不存在'):
            self.read('unknown-observation')
        self.store.remove_where('index_frame', 'media_id', 'media-a')
        with self.assertRaisesRegex(ValueError, '没有保存实际采样'):
            self.read()
        with self.assertRaisesRegex(ValueError, '没有保存实际采样'):
            self.prepare()
        self.assertFalse((self.store.root / 'playback-previews').exists())

    def test_http_routes_share_structure_and_reject_bad_identifiers_without_network(self):
        from tracking3.server import make_handler
        handler = make_handler(self.store, SimpleNamespace(version='fixture'), SimpleNamespace())
        class Connection:
            def __init__(self, request):
                self.request, self.response = io.BytesIO(request), bytearray()
            def makefile(self, *args): return self.request
            def sendall(self, data): self.response.extend(data)
        def call(path, data=None):
            body = json.dumps(data).encode() if data is not None else b''
            method = 'POST' if data is not None else 'GET'
            request = (f'{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
            connection = Connection(request)
            handler(connection, ('127.0.0.1', 0), SimpleNamespace())
            headers, content = bytes(connection.response).split(b'\r\n\r\n', 1)
            return int(headers.split(b' ')[1]), json.loads(content)
        before = self.database()
        status, result = call('/api/target-playback?task=task&observation=segment-one')
        self.assertEqual(status, 200); self.assertFalse(result['ready'])
        status, prepared = call('/api/target-playback-prepare', dict(task_id='task', observation_id='segment-one'))
        self.assertEqual(status, 200); self.assertTrue(prepared['ready'])
        self.assertEqual(set(result), set(prepared))
        status, reread = call('/api/target-playback?task=task&observation=segment-one')
        self.assertEqual(reread, prepared)
        for data in (dict(task_id='task', observation_id='../outside'), [], {}):
            status, error = call('/api/target-playback-prepare', data)
            self.assertEqual(status, 400); self.assertIn('error', error)
        self.assertEqual(self.database(), before)

    def test_changed_source_invalid_trace_and_unverified_actual_position_refuse_export(self):
        original = self.source.read_bytes()
        self.prepare()
        self.source.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, '已改变'): self.read()
        with self.assertRaisesRegex(ValueError, '已改变'): self.prepare()
        self.source.write_bytes(original)
        for row, message in [
                (dict(self.rows[0], source_sha256='wrong'), '源版本'),
                (dict(self.rows[0], media_s=10), '时间不一致'),
                (dict(self.rows[0], width=1), '目标框无效'),
                (dict(self.rows[0], detections=[dict(self.rows[0]['detections'][0], box=[1, 0, 1000, 64])]), '目标框无效')]:
            self.store.put('index_frame', row)
            with self.assertRaisesRegex(ValueError, message): self.read()
            self.store.put('index_frame', self.rows[0])
        # A self-consistent timestamp alone does not certify the raw source frame.
        self.store.put('index_frame', dict(self.rows[0], frame_index=1))
        with self.assertRaisesRegex(ValueError, '源帧与保存'):
            self.prepare()

    def test_concurrent_prepare_encodes_once_and_changed_during_decode_never_publishes(self):
        original_encode = playback._encode
        with patch.object(playback, '_encode', wraps=original_encode) as encode:
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: self.prepare(), range(2)))
            self.assertEqual(encode.call_count, 1)
        self.assertEqual(results[0]['preview'], results[1]['preview'])
        # Invalidate the existing cache, then change source during the export.
        self.store.put('index_frame', dict(self.rows[1], detector='changed evidence'))
        def encode_then_change(*args):
            result = original_encode(*args)
            self.source.write_bytes(self.source.read_bytes() + b'changed')
            return result
        with patch.object(playback, '_encode', side_effect=encode_then_change):
            with self.assertRaisesRegex(ValueError, '已改变'):
                self.prepare()
        directory = self.store.root / 'playback-previews/media-a'
        self.assertEqual(list(directory.glob('.samples-*')), [])


if __name__ == '__main__':
    unittest.main()
