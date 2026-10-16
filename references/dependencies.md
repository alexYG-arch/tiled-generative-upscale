# 依赖与执行环境

## 必需能力

| 依赖 | 用途 | 不可用时 |
| --- | --- | --- |
| 内置 `image_gen__imagegen` 图像编辑工具 | 为每个切片生成新的高分辨率细节，引用当前切片和整图上下文 | 保留已完成的分析与切片计划，报告生图能力不可用；不得改用普通插值冒充生成 |
| 当前会话的 `imagegen` Skill | 约束内置生图工具的输入、参考图、结果展示和调用方式；首次使用前阅读 | 查找当前环境的实际 Skill 路径；找不到时说明缺少此依赖 |
| 图片查看工具，例如 `view_image` | 查看原图、切片、生成结果、100% 接缝和最终图 | 不能将未查看的内容登记为视觉通过 |
| 本地文件读取、写入和执行工具 | 保存原图副本、分析、计划、提示词、工具回执、生成图和交付结果 | 继续允许范围内的规划；明确无法落盘或执行的部分 |
| Python 3.10+、Pillow 10+、NumPy 1.26+ | 切片、原生尺寸检查、带透明度的机械拼合和文件核验 | 先定位可用运行时；缺包时按当前环境权限安装，再检查 |

本 Skill 使用内置 ImageGen，不要求用户提供 API key，也不依赖自行调用的图像 API 或生图 CLI。不能因为内置工具暂时不可用而擅自替换生成服务。

Pillow 需要可用的 `ImageCms` 模块，以处理 sRGB 色彩配置。输入原图应先归一为 sRGB；拼合脚本会转换有嵌入配置的生成图，未标记的生成图按 sRGB 解释。RGBA 透明度必须保留。

## 可选依赖

- **OpenCV**：仅当另行实施并验证几何配准或特殊修复时使用。当前 `tile_pipeline.py` 与 `compose.py` 不依赖它，也不会默认运行仿射或光流修复。
- **PyYAML**：仅用于 Skill 官方结构校验工具，不是切片、生图、拼合或验收的运行依赖。

不要把上次执行所用的绝对 Python 路径、下载目录或安装目录写死。桌面环境可先用运行时发现工具定位已有 Python 和库，再选择本次命令使用的解释器。

## 启动检查

先确认上述工具实际可调用，读取 `imagegen` Skill，然后用选定的 Python 检查版本与导入：

```bash
python -c 'import sys, PIL, numpy; from PIL import Image, ImageCms; print(sys.version); print("Pillow", PIL.__version__); print("NumPy", numpy.__version__); print("sRGB", ImageCms.createProfile("sRGB"))'
python scripts/compose.py --self-test
python scripts/tile_pipeline.py --help
```

版本输出及自测通过只证明本地运行条件；不能替代真实生图、内容验收和最终接缝检查。

运行前检查目标目录可写和可用磁盘。`compose.py` 会估算 memmap、导出与接缝预览所需空间；原始生成图、历史尝试和交付归档仍需额外空间。保留原始图和原始生成结果，过程文件放在独立运行目录。
