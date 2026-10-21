# 数据集、模型与大文件

当前公开仓库发布源码与工具，**不附带真实人物录像、裁图、截图、业务数据库或模型权重**。现有图书馆实拍包含旁人，暂不公开；不能仅凭参与实验者本人的同意就发布画面中的其他人。

## 哪些内容可以分发

| 内容 | 当前处理 |
|---|---|
| 项目源码与测试 | 按仓库许可证公开；保留第三方许可声明 |
| 小型程序生成的合成示例 | 优先提供生成脚本，在使用者本机生成，不随源码提交输出录像 |
| 公开下载的数据集 | 优先提供作者／机构官方下载入口、获取步骤与清单；逐项检查许可后再决定是否分发 |
| 自采人物录像与标注 | 具备明确拍摄与公开发布授权、相关参与者同意，并处理旁人及场所要求后，才能另行考虑可发布版本；当前真实素材不上传 |
| OSNet、YOLO 等模型权重 | 从上游准备，记录版本与校验值；权重许可及使用条件独立于本项目代码 |
| 数据库、证据包、派生裁图 | 可能携带身份、路径、设备或来源信息，默认留在本地，不因来自原始录像而自动获得发布条件 |

“网上能下载”不等于允许重新上传到本仓库。原作者要求申请、署名、研究用途限制或其他条件时，继续由使用者遵循；本项目代码许可证不会改变第三方数据的许可。匿名化后的数据也需核查可识别信息与原始授权范围。

## 可复现的分发方式

建议公开轻量的下载／生成脚本与数据清单，原始大文件保存在使用者的本地数据目录。清单至少记录：官方来源 URL、版本／日期、文件名、SHA256、许可及获取条件、摄像头匿名编号、媒体时长／帧率、时间依据、标注版本和用途限制。

清单中的路径使用相对路径，人物采用匿名编号；不写真实姓名、设备地址、账号或开发者本机绝对路径。业务确认与独立真值分开保存，未标注或无时间依据的字段保持未知，不用推测值填满。

第三方数据的官方下载脚本与已核验许可清单仍需逐个数据集准备。当前不承诺上传任何既有真实素材，也不把“计划提供下载脚本”写成已经完成的数据分发。

## 本地生成小型合成示例

已有 [generate_demo_dataset.py](../../app/tools/generate_demo_dataset.py) 可生成两路几何人物示意录像、素材清单及事件表。从仓库根目录进入应用环境后运行：

```sh
cd 3.0/app
.venv/bin/python tools/generate_demo_dataset.py --output ../../local-data/synthetic-demo
```

目录应为空；脚本默认拒绝覆盖非空输出。该示例用于测试导入、时间字段、CSV 和证据导出流程，外观及身份由程序生成，**不能作为人物检测、ReID、遮挡恢复或真实部署效果基准**。生成物保留本地，不自动同步到 GitHub。

## 模型来源

OSNet x0.25 使用 MSMT17 `combineall` 行人 ReID 权重，不能用 ImageNet 初始化权重直接替代。官方来源为 [作者的 OSNet 模型仓库](https://huggingface.co/kaiyangzhou/osnet)和 [Torchreid Model Zoo](https://kaiyangzhou.github.io/deep-person-reid/MODEL_ZOO)。[prepare_models.py](../../app/tools/prepare_models.py)提供 YOLOv8n／OSNet 的本地准备与固定版本校验；权重文件不纳入 Git。安装位置和启动命令见根 [README](../../../README.md)。

随项目保留的 OSNet 架构代码采用 [上游 MIT 许可](https://github.com/KaiyangZhou/deep-person-reid/blob/master/LICENSE)，本地声明在 [OSNET-LICENSE.txt](../../app/tracking3/vendor/OSNET-LICENSE.txt)。其他模型和数据集仍按各自来源条件使用。

## GitHub 容量

普通 Git 仓库中，**单文件超过 50 MiB 会收到警告，超过 100 MiB 会被 GitHub 阻止**；浏览器上传的单文件上限为 25 MiB。不要把大量录像或数据库当作普通源码提交。规则以 [GitHub 大文件官方说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)为准。

Git LFS 的存储和下载带宽有独立配额与计费，公开仓库也不等于无限免费存储；文件新版本可能继续占用空间。使用前查看仓库所属账号的实际计划和预算，见 [Git LFS 计费说明](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)。本项目目前不默认启用 LFS，也不通过 LFS 绕过数据发布授权。
