---
name: tiled-generative-upscale
description: 根据图片内容、目标放大倍数和生图原生容量，计算带重叠切片，统一提示词并逐片生成、校验、拼合交付。用于图片高清化、数倍放大、10倍放大、分块重绘、保持原构图和色调的高分辨率重建，尤其适合用户要求真实逐片补细节而非整图插值放大时。
---

# 分块生成式高清放大

把原图划为可独立生成到目标像素密度的重叠切片，逐片补充合理细节，再直接拼到最终尺寸。**宽高各放大 s 倍，目标像素总数是 s² 倍。** 不把低分辨率拼合图再次插值放大作为完成结果。生成细节是重建，不是真实缺失信息的可靠恢复。

## 依赖与边界

首次运行读取当前环境的 **imagegen Skill**。必需：内置 `image_gen__imagegen`、图片查看工具、本地文件/命令工具、Python 3.10+、Pillow 10+（含 ImageCms）、NumPy 1.26+。无需 API key。详细能力检查、可选依赖和缺失时的行为见 [dependencies.md](references/dependencies.md)。

工具名称随环境可映射；先发现工具并核对当前文档，不猜接口。默认使用内置 ImageGen。仅在用户明确选择其他服务时按该服务规则另行适配；本脚本回执格式针对内置工具，不能冒用工具名称。缺少生图或图片查看能力时，完成可做的分析与规划，明确尚未生成或尚未视觉验收。

以下命令中的 `python` 是本次确认可用的解释器，`SKILL` 是本 Skill 目录，`RUN` 是新的独立过程目录。变量仅为示意，实际命令使用正确路径并加 shell 引号。不要写死作者机器路径，不修改原图。

## 1. 看原图，确认尺寸与目标

1. 查看原图，读取实际尺寸、EXIF 方向、色彩配置、透明度。倍率默认指宽高各放大；若用户明确给出面积倍率或目标尺寸，先换算并记录。仅缺原图/倍率且无法推断时询问。
2. 判断摄影、插画、海报等类型；识别文字/标志、脸、建筑直线、细线、重复纹理、同一物体、留白和透明区域。不可辨文字不猜写。
3. 记录 `analysis.json`：原图内容、构图色调、区域坐标、共享材质提示词、共享物体规则。坐标按 EXIF 方向归一后的原图计算。格式见 [planning-and-prompts.md](references/planning-and-prompts.md)。
4. 从当前生成工具说明和实际返回图确定保守的单片原生尺寸预算。缺少可靠实测时可暂以 1024×1024 作为**待试生成验证的预算**，不能声称工具必然提供该尺寸；先生成试样，按实际可用尺寸重新规划。不要让用户决定这些技术参数。

## 2. 自动计算切片，再检查内容范围

```bash
python "$SKILL/scripts/tile_pipeline.py" plan --image "$IMAGE" --scale 10 --native 1024x1024 --analysis "$ANALYSIS" --run "$RUN"
```

脚本按倍率、原生预算、3% 余量和默认15%重叠计算切片数量，把语义标注和图像边缘用于边界微调，输出 `plan.json`、`tile_map.jpg`、原图副本、切片、每片提示词、试样和后续运行顺序。具体公式和可调参数见规划参考。

必须查看切片图与关键裁切：边界尽量经过平坦背景，文字、脸、标志和细长结构尽量保留在同一裁切内；超出单片容量的物体按对象统一审核跨块连接。算法只利用已提供的语义标注，不能代替看图。如果范围不合理，修订分析和参数，使用新 RUN 重新规划。不得仅修改可视化网格。

审阅共享提示词、目标像素、试样顺序和全部警告后记录 `plan-review.json`，再运行：

```bash
python "$SKILL/scripts/tile_pipeline.py" approve-plan "$RUN" --review-json "$PLAN_REVIEW"
```

此处是代理对实际图像的检查记录，无须额外要求用户批准。已有用户约束和当前环境权限继续适用。

## 3. 固定提示词和执行顺序

- 同材质逐字复用 `materials`；同物体复用 `objects`。锁定构图、色调、轮廓、数量、文字和边界交点，只增加原轮廓内合理的高频细节。
- 每片编辑时第一参考图为当前裁切；整图可作为第二张上下文参考，并在 prompt 明确不能输出整图。先查看尚未看过的本地参考图；用本地路径引用，遵守当前 imagegen Skill 的调用规则。保持原有透明度。
- 按 `plan.sequence` 执行：先高风险区域及相邻片，再覆盖各种材质；试样全部通过后沿相邻切片顺序完成其余块。先对比试样接缝和材质再启动长批次。
- 默认串行。**所有计划、登记、审核、拼合和交付操作必须串行**；脚本没有多进程状态锁。不要让多个代理同时写同一个 RUN。

## 4. 逐片生成，逐片校验

真正调用内置 ImageGen 编辑每个裁切。保留工具返回的原始文件和实际 prompt；不要先用 resize 或锐化伪造原生结果。把工具的真实调用时间、输出路径、引用和可得的调用标识保存成 receipt，格式见参考文档。

```bash
python "$SKILL/scripts/tile_pipeline.py" register "$RUN" --tile "$TILE" --image "$GEN_OUTPUT" --receipt "$RECEIPT"
python "$SKILL/scripts/tile_pipeline.py" review "$RUN" --tile "$TILE" --review-json "$TILE_REVIEW"
python "$SKILL/scripts/tile_pipeline.py" status "$RUN"
```

`register` 复制原始文件，测量实际尺寸并检查比例，不代表视觉通过。实际有效区域必须两边均达到目标；画幅偏差超过0.5%拒绝，禁止非等比拉伸。只在确认输出额外边缘不属于原图目标范围时使用 `--crop`，同时用 `--placement-note` 留下证据；不能裁掉原图边缘以凑比例。

逐片查看原图裁切、生成图及与已通过邻块的重叠区域。检查新增细节、物体几何、色调、文字、边界和透明度，通过后才记录 `qa_passed`。有问题先保留失败记录，修订该片或共享规则后重试；修订过的 prompt 用 `--prompt` 登记。

工具未返回图像，用 `failure RUN --tile ID --receipt FILE` 保存失败。默认每片最多3次，用户指定上限优先；达到上限先分析原因、报告未完成项并改规划，不得静默提高上限。新 RUN 不应成为绕过同一失败上限的方式。调用结果未知时先查是否已有输出，不盲目重复请求。恢复任务读取 `status`，复用已通过的原始输出；规划脚本拒绝重置已有 RUN。

完整通过条件、失败恢复与原图保护的限制见 [validation-and-recovery.md](references/validation-and-recovery.md)。

## 5. 按坐标拼合，检查全部接缝

```bash
python "$SKILL/scripts/compose.py" "$RUN"
```

只接受全部 `qa_passed` 的计划块。脚本直接建立目标尺寸画布，仅执行已审核裁切、必要的缩小归一、坐标放置和重叠羽化；保持 alpha，实际检查全画布覆盖，缺片/缺口立即失败。不会自动做光流、几何变形、背景替换或整图放大。

输出 `composite.png`、`preview.jpg`、机械报告与 `seam_reviews/index.json`。先查看整图，再按索引逐段以100%查看**全部**接缝，并检查文字、标志、脸、建筑及前后细节对比。记录每段实际观察；不得批量填 true 替代查看。发现断线、重影、色阶或虚构文字，返回对应块修复，归档旧导出并重新拼合、重审受影响结果。

## 6. 验收后交付

```bash
python "$SKILL/scripts/tile_pipeline.py" finish "$RUN" --review-json "$FINAL_REVIEW" --out "$DELIVERY"
```

`finish` 要求逐片状态、完整接缝记录、整图检查与实际成图尺寸同时满足条件。交付独立目录包含完整 PNG、预览、计划/拼合/验收报告、切片及 prompts/receipts 的 ZIP 和中文说明；`delivery_complete.json` 存在才表示打包完成。

回答用户时给出原图→成图尺寸、倍率、实际切片数、成图/记录链接和已知限制。展示预览；不要把缩小预览当成完整尺寸结果。若用户另行要求保留原始字形等区域，须单独明确区域、处理方式、记录和验收；本辅助脚本只处理全区域生成切片，不自动覆盖原图，也不把保护区称为新生细节。

## 维护与验证

```bash
python "$SKILL/scripts/compose.py" --self-test
python "$SKILL/scripts/test_pipeline.py"
```

这些是合成图本地检查，不调用 ImageGen，不能证明真实生成质量。修改 Skill 后还应运行当前 `skill-creator` 提供的结构校验。设计来源和历史实现中需要避免的问题见 [execution-lessons.md](references/execution-lessons.md)。
