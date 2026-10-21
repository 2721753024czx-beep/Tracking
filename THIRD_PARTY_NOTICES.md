# Third-party notices

Tracking's original code is licensed under AGPL-3.0-or-later. The full license is
in [LICENSE](LICENSE). This choice keeps the public application compatible with
its current Ultralytics dependency; it does not relicense upstream code, models
or datasets.

## Vendored source

`3.0/app/tracking3/vendor/osnet.py` comes from Kaiyang Zhou's
[deep-person-reid](https://github.com/KaiyangZhou/deep-person-reid),
`torchreid/models/osnet.py`, copyright (c) 2018 Kaiyang Zhou. Its MIT license is
preserved verbatim in
[OSNET-LICENSE.txt](3.0/app/tracking3/vendor/OSNET-LICENSE.txt).
The local adapter is `tracking3/reid.py`. OSNet architecture code is not a
dataset license or a grant of rights to publish recordings of people.

## Installed dependencies

Dependencies are installed separately using `requirements.txt`; their source
and binaries are not copied into this repository. Each dependency retains its
own license, including its transitive dependencies:

| Component | Role | Upstream license/source |
|---|---|---|
| Ultralytics | YOLO detection and ByteTrack integration | [AGPL-3.0](https://github.com/ultralytics/ultralytics/blob/main/LICENSE) |
| PyTorch | OSNet inference | [BSD-style license](https://github.com/pytorch/pytorch/blob/main/LICENSE) |
| NumPy | Array calculations | [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| Pillow | Image handling | [HPND](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| PyAV | Media decoding | [BSD-3-Clause](https://github.com/PyAV-Org/PyAV/blob/main/LICENSE.txt) |
| ONNX Runtime | Optional ONNX encoder | [MIT](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |
| lap | Assignment dependency | [BSD-2-Clause](https://github.com/gatagat/lap/blob/master/LICENSE) |

FFmpeg/ffprobe are external tools, installed separately. FFmpeg's applicable
license depends on its build configuration; see its
[official legal information](https://ffmpeg.org/legal.html). No FFmpeg binary is
distributed here.

## Models and data

No pretrained model binary, original recording, person crop or runtime database
is distributed in this repository. Model download URLs, fixed revisions and
hashes are provided by `tools/prepare_models.py`; upstream model and training
data conditions remain applicable. Data publication rules are described in
[DATASETS.md](3.0/文档/开源/DATASETS.md).
