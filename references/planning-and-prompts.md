# 规划、提示词与运行记录

## 计算方式

`W,H` 是按 EXIF 方向归一后的原图宽高，`s>1` 是每一边的放大倍数。目标为 `round(W*s), round(H*s)`，所有坐标采用 Python 的 `round` 规则。区域使用左闭右开 `[left, top, right, bottom)`。

给定实测或待试样验证的原生预算 `Gw,Gh`、余量 `m`：

```text
Uw = floor(Gw * (1-m)); Uh = floor(Gh * (1-m))
Cw = min(W, floor(Uw/s)); Ch = min(H, floor(Uh/s))
Ow = max(1, ceil(Cw*overlap)); Oh = max(1, ceil(Ch*overlap))
Nx = 1 if W<=Cw else 1+ceil((W-Cw)/(Cw-Ow))
Ny = 1 if H<=Ch else 1+ceil((H-Ch)/(Ch-Oh))
数量 = Nx*Ny
x_i = round(i*(W-Cw)/(Nx-1))，Nx=1 时为0
y_j 同理
target_box = [round(s*l), round(s*t), round(s*r), round(s*b)]
```

使用全图坐标映射，避免非整数倍数逐片累加引起偏移。第一片从0开始，末片贴住原图末端；内部分界线在重叠和覆盖约束内调整。默认 `--margin 0.03 --overlap 0.15 --max-attempts 3`。比例和尺寸每片都检查，原生预算不是未来输出尺寸保证。

例如1920×1080、宽高各10倍、暂定1024×1024原生预算，默认参数得到99×99原图裁切、至少15像素重叠、23×13=299片；最终为19200×10800。这是参数计算例子，**不是这次又生成了299片**。实测输出不同应重新计算；不要固定复用历史204片。

边界调整使用内容标注与边缘强度，优先避开 `seam_avoid` 区域。它不会自行识别人脸或文字，也无法保证比单片容量更大的对象不被切开。运行后必须看 `tile_map.jpg` 和关键裁切；需要改变范围时调整分析/参数并新建 RUN。仅改图上网格无效。不要在批量生成途中手改坐标而复用旧范围输出。

## 内容分析 JSON

以下是结构示例，所有内容与坐标必须按本次图片重写：

```json
{
  "inspected": true,
  "coordinate_system": "exif_oriented_source_pixels",
  "summary": "暖色背景前的一件玻璃产品，右侧有细小品牌文字",
  "style": "保持原摄影光线、视角、轮廓、位置和景深",
  "palette": ["米白背景", "透明玻璃", "深灰文字"],
  "materials": {
    "background": "Preserve smooth warm-white tonal transitions; no invented grain or objects.",
    "glass": "Refine existing glass edge highlights and subtle reflections at consistent scale; no new reflection objects.",
    "lettering": "Preserve exact visible glyph contours and spacing; do not invent or replace characters."
  },
  "objects": {
    "bottle": "Keep the original bottle silhouette, cap geometry and reflection direction identical in every crop."
  },
  "regions": [
    {"id":"background","box":[0,0,800,600],"kind":"background","material":"background","seam_avoid":false,"detail":"平滑留白"},
    {"id":"product","box":[220,80,490,550],"kind":"product","material":"glass","object":"bottle","seam_avoid":true,"detail":"原图中的完整玻璃瓶"},
    {"id":"brand","box":[560,230,710,300],"kind":"text","material":"lettering","seam_avoid":true,"detail":"按本次查看到的原文字形保留；不可辨内容不猜写"}
  ]
}
```

`materials` 是细节一致性的主要约束；同一 ID 的提示词逐字复用。`objects` 可选，用来约束跨块同一物体。`regions` 可以相互重叠，背景区域可覆盖全图。补细节应符合材质：金属高光、叶脉、玻璃折射、建筑窗格分别定义；留白不应强行生纹理。

脚本为每片写出 `prompts/ID.txt`。内容依次是编辑目标、全图坐标/尺寸、固定风格色调、共享材质/物体规则、局部内容与四边连续约束。不要把小裁切解释成独立构图重新设计。更改共享规则后审查全部受影响块，不能只修一块留下不同版本。

## 审阅计划

查看原图、网格及关键裁切后保存文件：

```json
{
  "checks": {"image_content":true,"tile_extents":true,"critical_objects":true,"shared_prompts":true,"pilot_sequence":true},
  "warnings_acknowledged": [],
  "notes": "写本次实际检查结果；如有跨块关键对象，写明如何检查连接。"
}
```

`warnings_acknowledged` 必须包含计划中的全部警告，不能填空跳过真实警告。`approve-plan` 不等于用户批准，也不替代试样。

## 执行顺序和工具记录

`plan.pilot_ids` 先选高风险切片、其相邻片和未覆盖材质代表；`plan.sequence` 将试样排在最前，再按相邻行往返排列其他块。试样全部 `qa_passed` 后才允许登记普通块。执行前查看已有 `state.json`，跳过已通过的块；所有状态读写串行。

每次真实工具调用保存 receipt JSON：

```json
{
  "tool": "image_gen__imagegen",
  "called_at": "本次实际UTC时间",
  "output_path": "/本次工具实际返回的原始图片路径.png",
  "reference_paths": ["/本次裁切.png", "/本次整图.png"],
  "tool_call_id": "如工具提供则记录，未提供则省略",
  "note": "本次实际使用的prompt文件路径或简短说明"
}
```

不是调用过工具就能随意填回执。字段记录实际工具结果；脚本校验尺寸与状态，不独立证明某个文件的模型来源。失败而没有图时，receipt 使用 `error` 保存实际错误并调用 `failure`；这同样计入次数。将状态与工具结果一并保留。

输出文件不要预先 resize；`register` 保存原始文件、原生尺寸及当前 prompt 快照。修订 prompt 时用 `--prompt` 传真实使用的文件。画幅不匹配先重试，不自动中心裁切或拉伸。只有额外边缘不属于目标源范围且全部目标仍在时，才能使用 `--crop x0,y0,x1,y1 --placement-note '本次具体观察'`。

## 逐片视觉记录

```json
{
  "output": "generated/r000c000/attempt_01.png",
  "checks": {"composition":true,"detail":true,"geometry":true,"color":true,"text":null,"borders":true,"alpha":true},
  "not_applicable": {"text":"该裁切没有文字或标志"},
  "notes": "本次实际观察到的细节、原图对照与邻接情况；不要使用无内容的统一通过套话。"
}
```

`output` 必须对应当前尝试。字段为 `true` 表示已检查通过；不适用使用 `null` 和具体原因；任何 `false` 或缺项拒绝通过。文字很小不能当成不适用。`borders` 包括原图边缘对应关系，已有邻块时还要比较重叠内容。每片都要观察新增细节是否超越插值像素。

## 最终视觉记录

```json
{
  "checks": {"whole_image":true,"critical_objects":true,"all_seams":true,"text":true,"color":true,"alpha":true,"detail_comparison":true},
  "seams": {
    "seam_reviews/实际索引中的文件.jpg": {"passed":true,"notes":"本段实际观察，说明线条、颜色或透明边缘是否连续"}
  },
  "notes": "整图与关键细节的本次检查结论",
  "limitations": []
}
```

`seams` 必须完整对应 `seam_reviews/index.json`，不漏、不多；只有一片没有接缝时为 `{}`。小图总览不能替代100%片段。新增细节的真实性限制在交付说明统一披露；其他实际问题写进 `limitations`，不能隐瞒影响结果的问题并勾选通过。

`finish` 写出的 `delivery_complete.json` 标识打包成功；RUN 中 `final_qa.json` 仅在交付文件完成后产生。交付中断时保留原始结果，清理/归档自己不完整的交付目录后重试，不能把部分 ZIP 作为完整交付。
