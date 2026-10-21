# 本地模型准备

权重不随源码分发。所有命令在 `3.0/app` 目录执行；准备工具只依赖 Python 标准库，不加载模型或启动服务。

```sh
.venv/bin/python tools/prepare_models.py
.venv/bin/python tools/prepare_models.py --check
```

首次命令准备 `models/yolov8n.pt` 和 `models/osnet_x0_25_msmt17.pth`。工具先预检全部已有文件，再下载缺失项；下载写入临时文件，大小和 SHA256 均匹配才提交。已有文件或并发产生的目标文件不会被覆盖；不匹配时返回非零退出码，需保留原件后自行处理或换空目录。

`--check` 完全离线、不创建目录，缺失或校验失败返回非零退出码。可用 `--model yolov8n` 或 `--model osnet` 单独准备，`--models-dir /path/to/models` 改目标目录，`--timeout 120` 改网络超时。路径改变后，启动时须传对应 `--weights` 和 `--reid-osnet`。

## 固定来源与校验

| 文件 | 固定官方来源 | 字节数 |
| --- | --- | ---: |
| `yolov8n.pt` | [Ultralytics assets v8.4.0 下载](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt)；[发布页指纹](https://github.com/ultralytics/assets/releases/expanded_assets/v8.4.0) | 6,549,796 |
| `osnet_x0_25_msmt17.pth` | [作者 Hugging Face 仓库](https://huggingface.co/kaiyangzhou/osnet)，固定 revision `a5c5cc037c24235cda3b21085b93ad77c9616224` | 9,336,983 |

```text
yolov8n.pt
f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36

osnet_x0_25_msmt17.pth
cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18
```

OSNet 原始文件名是 `osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth`。完整固定下载 URL、两个文件的大小和 SHA256 均保存在 [`tools/prepare_models.py`](../tools/prepare_models.py) 中。离线机器可从这些来源在另一台机器下载，将文件按上表名称放入本机 `models/`，再运行 `--check`；无需改动程序或让推理 SDK 自动下载。

## 模型协议与归属

YOLOv8n 用于人员检测，ByteTrack 通过 `ultralytics` 依赖进行本路框关联。[Ultralytics 官方仓库](https://github.com/ultralytics/ultralytics)及其 [YOLOv8 模型页](https://huggingface.co/Ultralytics/YOLOv8)标注 AGPL-3.0；依赖和模型的使用须遵守各自许可。

OSNet 是作者发布的 x0.25、MSMT17 combineall 训练权重，不是 ImageNet 初始化权重。其 [模型卡](https://huggingface.co/kaiyangzhou/osnet)标注 MIT；架构来自 [deep-person-reid](https://github.com/KaiyangZhou/deep-person-reid) 的固定 revision `f8cd150fdf77e8d9e1ed143b7f308c2c609ded50`，版权与许可保留在 [`OSNET-LICENSE.txt`](../tracking3/vendor/OSNET-LICENSE.txt)。

[`tracking3/reid.py`](../tracking3/reid.py) 使用 RGB、双线性缩放为高256×宽128、ImageNet均值与标准差；模型输出512维 embedding，经 L2 归一化后用于余弦排序。分类器不作为本项目身份标签。权重以 `weights_only=True`、`strict=True` 加载，缺层或不匹配即失败；`pretrained=False` 避免隐式下载其他初始化权重。

默认 CPU；检测设备用 `--device`，OSNet 设备用 `--reid-device`。MPS 须由所在环境实际支持，不保证速度或算子兼容。更换权重或预处理协议会改变编码器版本，应使用独立索引和任务；候选仍须人工核对。
