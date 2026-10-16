# tiled-generative-upscale

一个用于 Codex 的分块生成式高清放大 Skill。根据图片内容、放大倍数和实际生图容量计算重叠切片，统一细节提示词，逐片生成与校验，再直接拼合到目标尺寸。

倍率默认指宽和高分别放大：10 倍对应 100 倍像素总数。具体切片数量由输入图片、原生生成尺寸、重叠和内容范围决定，不保证固定的生图尺寸或生成质量。

## 六步流程

1. **检查原图和目标**：识别构图、色调、文字、标志、人物、建筑、重复纹理与透明区域。
2. **计算并检查切片**：按倍率与原生容量计算数量，用内容标注调整边界，查看切片图和关键区域。
3. **制定提示词、顺序和校验规则**：同材质、同物体复用规则；先生成代表性试片及相邻片。
4. **逐片生成和验收**：保留原始输出及回执，检查原生尺寸、比例、细节、内容和边界；失败时记录并重试。
5. **直接拼合并检查接缝**：按目标坐标落位和羽化，保留透明度，检查真实覆盖及全部 100% 接缝预览。
6. **验收后交付**：输出完整 PNG、预览、处理记录和原始切片/生成图/提示词归档。

完整执行规则见 [SKILL.md](SKILL.md)，实现见 [scripts](scripts)。Python 辅助脚本负责规划、记录和拼合；实际生图与视觉检查由 Codex 调用对应工具完成。

## 必需依赖

- Codex 中可调用的内置 `image_gen__imagegen` 编辑工具，以及当前环境的 `imagegen` Skill。
- 图片查看工具（例如 `view_image`）与本地文件、命令执行能力。
- Python 3.10+、Pillow 10+（含 ImageCms）、NumPy 1.26+。

**无需 API key。** 仅安装 Python 包不能提供生图能力。OpenCV 不属于当前脚本的依赖；PyYAML 仅用于 Skill 官方结构校验。环境检查与依赖缺失时的处理见 [dependencies.md](references/dependencies.md)。

## 安装

安装到 `$CODEX_HOME/skills`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills`。下面的命令发现目标目录已存在时会停止，不覆盖已有 Skill：

```bash
(
  tiled_skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/tiled-generative-upscale"
  if [ -e "$tiled_skill_dir" ]; then
    printf '%s\n' "目标目录已存在，请先检查已有安装；本次未修改。" >&2
    exit 1
  fi
  git clone https://github.com/alexYG-arch/tiled-generative-upscale.git "$tiled_skill_dir"
)
```

在选定的 Python 环境中安装依赖；已有满足条件的依赖时可跳过：

```bash
tiled_skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/tiled-generative-upscale"
python -m pip install -r "$tiled_skill_dir/requirements.txt"
```

让 Codex 重新加载可用 Skill 后，在会话中提供原图和倍率，例如：

```text
使用 $tiled-generative-upscale，将这张图片的宽和高各放大 10 倍。
保持原构图和色调，按内容切片，逐片生成细节、检查接缝后合并交付。
```

## 验证与适用边界

目前已通过 **18 项本地流程测试**和 **9 类拼合自检**，包括非整数倍率、原生预算、覆盖缺口、透明度、试片门禁、重试记录、接缝遗漏和旧结果拒绝。可在仓库目录运行：

```bash
python scripts/test_pipeline.py
python scripts/compose.py --self-test
```

这些测试使用合成图，不调用 ImageGen。**尚未执行本 Skill 的完整真实生图回归**，本地测试不能证明实际生成细节或接缝质量。

生成细节属于合理重建，不能视为真实缺失信息的恢复。文字、脸、建筑等内容仍需逐片与最终视觉检查；尺寸达标不代表内容正确。切片边界调整是辅助计算，也需要查看原图与切片图。

所有计划、登记、审核、拼合及交付状态操作必须串行；脚本没有多进程状态锁，不要让多个代理同时写同一个运行目录。原始输出与失败尝试应保留，已有交付文件不会静默覆写。具体规则见 [校验与恢复](references/validation-and-recovery.md) 和 [执行经验](references/execution-lessons.md)。
