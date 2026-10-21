# Tracking 3.0 应用

本地跨摄像头录像检索与复核工作台：导入或录制录像，保存检测采样与本路轨迹，用 OSNet 外观特征排序候选，再人工核对身份、时间和片段关系。选定目标回放使用已有采样帧，不重新推理；空采样和不同片段 ID 保持独立。

## 安装

下面命令从仓库根目录开始，适用于 macOS/Linux 的终端。已验证的 Python 环境为 3.12；Windows 尚未验证，回放缓存当前使用 Unix 文件锁。需要系统 `ffmpeg`、`ffprobe` 在 PATH 中，FFmpeg 应包含 `libx264` 编码器。macOS 可用 `brew install ffmpeg`；其他系统按 [FFmpeg 安装说明](https://ffmpeg.org/download.html)配置。

```sh
cd 3.0/app
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
ffmpeg -version
ffprobe -version
.venv/bin/python tools/prepare_models.py
.venv/bin/python tools/prepare_models.py --check
```

`requirements.txt` 固定直接依赖版本；`requirements.lock.txt` 保存开发环境的完整快照，不保证每个平台都有对应二进制轮子。模型不随源码分发，准备工具只从固定官方来源下载两个文件到 `models/`，校验大小与 SHA256，已有文件不匹配时拒绝覆盖。来源、指纹和离线准备见 [模型说明](models/README.md)。

## 启动

在 `3.0/app` 目录执行：

```sh
.venv/bin/python -m tracking3.server --data ./data --port 8765 \
  --weights models/yolov8n.pt \
  --reid-osnet models/osnet_x0_25_msmt17.pth --reid-device cpu --device cpu
```

打开 [本地工作台](http://127.0.0.1:8765/)。录像、裁图、SQLite 状态和报告保存在所选 `--data` 目录；更换目录会进入另一个工作区。启动终端须保持运行，停止后刷新网页会连接失败。服务只监听本机地址。

macOS/Linux 也可使用 `sh run-reid.sh --port 8765`，默认工作区为 `data-reid/`。裸 `sh run.sh` 不传 `--reid-osnet` 时使用颜色特征流程基线。更换外观编码器或模型权重后须新建独立索引和任务；旧模型的任务继续使用其原编码器查看。

检测权重默认优先取 `models/yolov8n.pt`。仅在该文件缺失且历史目录中的 `Tracking-2.0/yolov8n.pt` 存在时兼容旧位置；新安装无需历史目录。`--weights` 可显式指定其他本地文件。

## 不用摄像头的流程验证

可以自行导入有权使用的视频。也可以生成双路几何合成素材，检查导入、索引、查询和报告流程：

```sh
.venv/bin/python tools/generate_demo_dataset.py --output ./examples/generated
.venv/bin/python tools/run_synthetic_workflow.py \
  --input ./examples/generated --output ./data-synthetic-demo
```

此流程使用确定性的合成目标检测和颜色特征，不依赖模型下载；它验证数据链路，不能作为真实检测、ReID 或遮挡恢复的性能结果。原始人物录像、裁图、数据库、模型、缓存和历史实验记录均不随公共源码提供。

## 检查与目录

```sh
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_candidate_browser.cjs
```

Python HTTP 测试会在本机临时端口运行受控服务，测试数据使用临时目录。Node.js 仅用于 JavaScript 检查。`test_reid.py` 在 OSNet 权重缺失时会跳过；跳过不代表模型通过验证。部分 `tools/verify_*` 工具属于可选集成实验，可能需要自行提供素材、FFmpeg 或 MediaMTX；它们不属于首次启动必需步骤。

| 目录 | 内容 |
| --- | --- |
| `tracking3/` | 后端、索引、检索、状态、时间、复核与导出 |
| `static/` | 浏览器工作台与采样目标回放 |
| `tools/` | 模型准备、合成素材、校时及评估 CLI |
| `tests/` | 临时数据测试与界面逻辑检查 |
| `models/` | 模型来源说明；权重由本地准备工具取得 |

候选分数不是身份概率；本路编号不代表跨摄像头身份。约 3fps 等采样配置只记录执行过检测的画面，未命中区间不能解释为目标必然不在场。第三方代码与模型许可见 [模型说明](models/README.md)及仓库根目录许可说明。
