# Tracking

源码仓库：[2721753024czx-beep/Tracking](https://github.com/2721753024czx-beep/Tracking)。

一个在本机运行的多摄像头录像检索与人工核对原型。用照片或监控画面框选人物，在已处理的录像中查找候选片段，再查看原帧、采样轨迹和时间信息，保存人工判断并导出证据。

当前侧重 macOS／Apple Silicon 的 CPU 实践流程。项目仍处于实验阶段，尚无完整独立身份真值支持的总体准确率或稳定部署结论。

## 能做什么

| 功能 | 当前行为 |
|---|---|
| 素材接入 | 导入本地录像，或将 RTSP／HTTP 流录成本地文件；支持双路实时预览与录制 |
| 人物检索 | 照片与监控起点两个入口，可限定机位、方向及参考图；返回相似片段供核对 |
| 本路跟踪证据 | 默认约 3 fps 采样，YOLOv8n 检测人物，ByteTrack 根据运动与框关联本路轨迹 |
| 外观检索 | OSNet x0.25 提取归一化外观向量，以余弦相似度排序；OSNet 不参与当前 ByteTrack 的本路关联代价 |
| 目标回放 | 按已保存采样点叠框、慢放、点选人物、显示未检出与重新检出；支持短历史轨迹 |
| 人工核对与导出 | 保存确认／排除／待核对，纠正片段，导出报告、冻结评估输入和可迁移证据包 |

回放展示已有检测结果，播放时不会重新运行跟踪模型。约 3 fps 的采样回放不代表原视频每一帧都完成识别，也不是选定目标后逐帧推理的单目标跟踪器（SOT）。不同片段或镜头不会因本路 ID 相同就自动合并身份。

相似度分数不是身份概率。代表帧裁切、遮挡、相似衣着、检测遗漏和轨迹断段都会影响结果；业务人工判断也不能直接替代独立效果真值。没有共同时间依据时，跨镜头先后关系保持未知。

## 安装与启动

推荐先使用 Python 3.12 和 CPU，接近当前 macOS 开发环境。需要 `ffmpeg`／`ffprobe`；使用 Homebrew 的 Mac 可安装：

```sh
brew install python@3.12 ffmpeg
```

工具来源见 [Python 3.12](https://formulae.brew.sh/formula/python@3.12)和 [FFmpeg](https://formulae.brew.sh/formula/ffmpeg)。获取源码并安装：

```sh
git clone https://github.com/2721753024czx-beep/Tracking.git
cd Tracking
cd 3.0/app
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/prepare_models.py
.venv/bin/python tools/prepare_models.py --check
```

模型准备工具从上游下载 YOLOv8n 与 OSNet x0.25 MSMT17 `combineall` 权重到本地 `models/`，并检查固定版本的 SHA256。`--check` 仅做本地校验；首次下载需要网络。模型不随 Git 仓库提交；OSNet 来源与数据条件见 [DATASETS.md](3.0/文档/开源/DATASETS.md)。

保持当前目录，启动独立工作区：

```sh
.venv/bin/python -m tracking3.server \
  --data data --port 8765 \
  --weights models/yolov8n.pt \
  --reid-osnet models/osnet_x0_25_msmt17.pth \
  --reid-device cpu --device cpu
```

浏览器打开 <http://127.0.0.1:8765/>。导入自己有权使用的录像、处理素材，再通过照片或监控画面创建查询。双路预览与录制入口为 <http://127.0.0.1:8765/live.html>，设备账号密码仅在本机页面输入。默认只监听本机回环地址；此处未配置远程访问或托管。

双路页面中的局域网地址和主／子码流路径是示例；请展开各机位的“取流地址”，填写实际设备支持的 RTSP 地址。浏览器显示的是后端转换的预览，录制文件保存在所选工作区的 `captures/`。

保留启动终端；退出时按 `Control+C`。关闭网页不会停止服务，单独打开网址也不会启动服务。再次使用相同 `--data` 会继续原工作区；公开仓库不含开发者的既有任务或录像。

## 开发验证

从 `3.0/app/` 执行 Python 测试；浏览器逻辑测试另需 Node.js：

```sh
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_candidate_browser.cjs
```

部分测试会创建临时本地 HTTP 服务，需要允许本机监听。测试通过用于核对实现行为，不等于真实人物检索准确率通过。合成流程示例见 [DATASETS.md](3.0/文档/开源/DATASETS.md)。

GitHub Actions 运行公开文件审计、Python 语法检查、同步工具与模型准备工具的离线测试，以及浏览器逻辑测试。完整应用测试在安装上述依赖后本地执行；CI 不下载模型或运行真实人物录像。

## 源码导航

| 位置 | 内容 |
|---|---|
| [tracking3](3.0/app/tracking3/) | 本地服务、索引、检索、时间信息、人工判断与证据导出 |
| [static](3.0/app/static/) | 浏览器工作台、双路预览及目标回放界面 |
| [tools](3.0/app/tools/) | 素材处理、合成示例、校时、评估与导出 CLI |
| [tests](3.0/app/tests/) | Python 与浏览器逻辑测试 |
| [更新记录](CHANGELOG.md) | 公开版本的功能与边界变化 |
| [GitHub 同步约定](3.0/文档/开源/GITHUB_SYNC.md) | 公开范围和每次更新后的同步流程 |
| [数据集与模型分发](3.0/文档/开源/DATASETS.md) | 数据许可、合成示例、大文件与模型准备原则 |
| [首次发布验证](3.0/文档/开源/VALIDATION.md) | 实际检查结果、公开副本与验证边界 |

本仓库不是完整实验磁盘备份。自采视频、人物截图、运行数据库、会话记忆、模型二进制和历史实验目录均不随源码发布。

## 许可证

项目代码采用 **AGPL-3.0-or-later**，见 [LICENSE](LICENSE)。随附的 OSNet 架构代码保留作者的 [MIT 许可声明](3.0/app/tracking3/vendor/OSNET-LICENSE.txt)，上游为 [deep-person-reid](https://github.com/KaiyangZhou/deep-person-reid)。模型权重与数据集各自的许可和使用条件需单独遵循。
