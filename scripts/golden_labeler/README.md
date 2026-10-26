# golden 标注台（本地、离线、单文件前端）

把 `hokdata/golden` 的人工核验从「手改 JSON」变成「拖框 + 按 Enter」。
不改任何原始数据：只读 `images/`、`images_test/`、`prefill/`、`prefill_test/`、`candidates.jsonl`，
只追加写 `labels_work.jsonl`。

## 1. 现状（开工前已核实）

| 项 | 实测 |
|---|---|
| `candidates.jsonl` | 71 条，全部 `split=test` |
| `verified` | 71 条全为 `false` |
| `prefill_source` | 71 条全为 `yolo_units` |
| `ground_truth` | 71 条全为 `null` |
| 磁盘图片 | `images/` 333 张；`images_test/` 71 张（与 `images/` 同名） |
| 不在 candidates 里的图 | 262 张 |
| MD5 重复 | 7 组 / 58 张（同一画面重复抽帧） |

**硬约束（重要）**：333 张按 `splits.json` 归属后，test 只有 85 张，唯一 md5 只有 **71 张**——
正好等于现有 candidates。也就是说：**不重新从 test 局抽帧，标注台里能新增的 test 帧是 0 张**，
到不了 200。要扩到 200–500，必须先补 test 局视频/截图再跑抽帧脚本。
工具已经把这件事写进 `--report` 的 `[约束]` 行，避免标注者白干。

test 局只有 4 个：`20261015_114142_519832`(58)、`20261015_225822_253681`(10)、
`20261017_165546_503488`(10)、`20261020_224657_708566`(7)。

## 2. 启动

```bat
cd D:\work\aiKings\WZRYagnt
venv\Scripts\python.exe tools\golden_labeler\server.py ^
  --root D:/work/aiKings/hokdata/golden --port 8765 --open
```

Git Bash：

```bash
./venv/Scripts/python.exe scripts/golden_labeler/server.py \
  --root D:/work/aiKings/hokdata/golden --port 8765 --open
```

浏览器打开 <http://127.0.0.1:8765/>。Ctrl+C 停止。
只依赖 Python 标准库（3.9+），离线可用，无 CDN、无 pip 安装。

参数：`--root`（默认 `D:/work/aiKings/hokdata/golden`）、`--port`（8765）、`--host`（127.0.0.1）、
`--open`、`--labeler`（默认取 `%USERNAME%`）、`--splits`（默认向上查找 `hokdata/splits.json`）。

## 3. 快捷键

| 键 | 作用 |
|---|---|
| `1`–`9` | 选类别（前 9 类；其余 2 类点顶部 chip） |
| 拖拽空白 | 以当前类别画新框 |
| 拖动框内 | 整体移动 |
| 抓框角/边 | 缩放（8 个方向） |
| 双击框 | 删除该框 |
| `Backspace` / `Delete` | 删除选中框（未选中则删最后一个） |
| `Enter` | 保存并跳到下一张未标注帧 |
| `U` | 撤销当前帧最近一条记录（append 一条 undo 事件） |
| `S` | 跳过（记 `op=skip`，不计入标注） |
| `D` | 标记重复/坏图（记 `op=flag`，不计入标注） |
| `←` / `→` 或 `P` / `N` | 上一帧 / 下一帧 |
| `Esc` | 取消选中 |

右上「只看 test」默认勾选；取消后能看到全部 333 张，但 train/val 帧会红字告警且**保存会被服务端 409 拒绝**。

## 4. 预填框

需求里写的是 `prefill/*.json`，**实际磁盘上是 YOLO `.txt`**（`cls cx cy w h`，归一化，空格分隔）。
服务端两种都支持：先找 `prefill/<stem>.txt`，再 `prefill_test/`，最后 `.json`；读到后按图片真实尺寸
换算成像素坐标下发。实测 333 张全部有预填（`prefill/` 333 个文件），类别只出现 0/1/8
（`enemy_hero` 110、`ally_hero` 413、`neutral_monster` 56）。

标注者打开一帧即可看到预填框，拖拽微调后按 `Enter`，不需要从零画。

## 5. 保存格式（append-only）

`hokdata/golden/labels_work.jsonl`，一行一个事件，**永不修改旧行**：

```json
{"op":"save","id":"images/20261015_114142_519832__frame_000000.png","image":"images/20261015_114142_519832__frame_000000.png",
 "session":"20261015_114142_519832","split":"test","source_image_md5":"<md5>","width":1280,"height":720,
 "boxes":[{"cls":1,"x1":634.55,"y1":359.35,"x2":735.05,"y2":484.25}],
 "verified":true,"note":"","ts":"2026-10-24T12:24:32+08:00","labeler":"你的用户名"}
```

- `op` 取 `save` / `skip` / `flag`；撤销写 `{"op":"undo","id":"...","ts":"..."}`。
- 回放规则：`undo` 撤销该 id **最近一条未撤销**的事件；同一 id 多次 save 以最后一次为准。
- `source_image_md5` 是保存时图片的 md5，用来防止「标注后图片被换掉」。
- 导入的 `ground_truth` 写成 YOLO 多行字符串（`cls cx cy w h`，6 位小数），与 `summary.json`
  的 `annotation_protocol` 一致。

## 6. 导入校验 `scripts/apply_golden_labels.py`

```bash
# 默认就是 dry-run，只校验 + 打印 diff
./venv/Scripts/python.exe scripts/apply_golden_labels.py

# 覆盖率报告（没有 labels_work.jsonl 也能跑，看现有 candidates）
./venv/Scripts/python.exe scripts/apply_golden_labels.py --report

# 真正写入（先备份成 candidates.jsonl.bak-<ts>）
./venv/Scripts/python.exe scripts/apply_golden_labels.py --apply

# 端到端自测（全部在临时目录，不碰真实数据）
./venv/Scripts/python.exe scripts/apply_golden_labels.py --self-test
```

5 条校验（任一失败退出码 2，**整批拒绝**，不会写半份）：

1. **schema**：`boxes` 非空数组；`cls` 是 `0..10` 的整数；四个坐标是有限数值；`x2>x1`、`y2>y1`；
   坐标在图片范围内；`id` 与 `image` 文件名一致。
2. **verified**：必须严格为 `true`。
3. **MD5 唯一**：与本批次其他条目、与 `candidates.jsonl` 已有条目都不能同 md5（同一文件的更新允许）。
4. **test-only**：用 `session` 查 `splits.json`（优先 `tencent_rt_base` 基础局 → `tencent_rt` → `wzry`），
   必须为 `test`。
5. **图片存在且可核对**：文件存在、能读尺寸、磁盘 md5 与事件里的 `source_image_md5` 一致。

合并规则：

- 原有 71 条**全部保留、顺序不变**；命中同一文件名的只改 `verified=true`、`ground_truth`、
  `prefill_source="human"` 三个字段（严格按需求，不动其它字段）。
- 新文件追加为完整条目（`scenario=null`、`state_*`/`wall_t` 为 `null`，明确表示未知，不编造）。
- 写盘用临时文件 + `os.replace` 原子替换；写前 `shutil.copy2` 备份。

`--report` 输出：候选帧总数、已验证数、split 分布、类别分布、距 200 帧差额，
以及磁盘 test 唯一图池（判断还能不能扩）。

## 7. 自测（2026-10-24 实测）

启动命令（未加 `--open`，避免自动弹窗）：

```bash
./venv/Scripts/python.exe scripts/golden_labeler/server.py \
  --root D:/work/aiKings/hokdata/golden --port 8765
```

三个只读端点（`curl` / `urllib` 实跑，随后已停止进程）：

| 端点 | 状态码 | Content-Type | 字节 |
|---|---|---|---|
| `GET /` | 200 | `text/html; charset=utf-8` | 24,943 |
| `GET /api/manifest` | 200 | `application/json; charset=utf-8` | 252,159 |
| `GET /img/20261015_002532_682861__frame_000000.png` | 200 | `image/png` | 1,284,679 |

manifest 实测：`total=333, test=85, train=219, val=29, saved=0, dup_groups=7, dup_images=58,
candidates=71`；首条预填 1 框、`1280x720`、`prefill/...txt` 解析正确。

写接口在**临时目录**（`temp/gl_e2e`，已清理）验证，未触碰真实 `labels_work.jsonl`：

| 请求 | 结果 |
|---|---|
| `POST /api/save`（test 帧，1 框） | 200，`saved_boxes=1`，manifest `status=saved` |
| `POST /api/save`（同帧第二次） | 200，追加新事件，仍只算 1 条标注 |
| `POST /api/save`（train 帧） | 409 `split_not_test` |
| `POST /api/save`（`cls=99`） | 400 `schema` |
| `POST /api/undo` | 200，manifest 回到 `unlabeled`，`labels_work.jsonl` 变成 2 行（append-only 生效） |

`apply_golden_labels.py --self-test`：9 项全过（正常保存、规则 3 同 md5 拒绝、规则 4 train 拒绝、
规则 2 verified=false 拒绝、规则 1 越界拒绝、undo 后不计入、规则 5 md5 篡改拒绝、`--apply` 写入并备份、
ground_truth 为 5 列 YOLO 行）。

现网 dry-run（用 `temp/` 下的 1 条**测试样例**指向真实图片，未写入，样例已删）：

```
[1/4] 标注事件  temp\gl_sample_labels.jsonl
      1 条事件（坏行 0），回放后 1 帧有记录
[2/4] 校验      通过 1 条，失败 0 条，跳过/标记 0 条
[3/4] 合并计划  更新 1 条 / 新增 0 条（candidates 原有 71 条，合并后 71 条）
      ~ 20261015_114142_519832__frame_000000.png  (verified=true, prefill_source=human)
[4/4] dry-run：未写入。确认无误后加 --apply。
```

混入 1 条 train 帧时：

```
      [FAIL] images/20261015_002532_682861__frame_000000.png
             - 规则4 test-only：session 20261015_002532_682861 在 wzry 中是 train
[4/4] dry-run：未写入。确认无误后加 --apply。
校验失败 1 条，已拒绝导入（退出码 2）。
```

`--report` 现网输出：

```
===== golden 覆盖率报告（当前 candidates，标注文件尚未生成） =====
  候选帧总数      71
  已验证 verified 0  (0.0%)
  split 分布      test 71
  类别分布        无
  距 200 帧目标   还差 200
  磁盘图片         333 张；test 唯一图 71 张，非 test 唯一图 211 张
  [约束] 现有 test 图片池只有 71 张唯一图，想到 200 帧必须重新从 test 局抽帧（同图不能重复计入）
```

## 8. 常见问题

**Q：启动报端口占用 / 打不开页面？**
换端口 `--port 8766`；或先确认 `http://127.0.0.1:<port>/` 能打开（只监听 127.0.0.1，不对外网开放）。

**Q：按 Enter 没反应 / 提示 split=train？**
golden 只收 test。看右下角 `split` 是否红色；按 `S` 跳过，或勾掉「只看 test」只浏览不下笔。

**Q：为什么顶部一直告警 MD5 重复？**
同一画面被抽成多帧（7 组 / 58 张）。确认是同一画面就按 `D` 标记；不同画面说明抽帧去重有问题，应报给数据侧。

**Q：标注到一半浏览器关了，会丢吗？**
不会。每次 `Enter` 已 append 落盘，刷新页面后状态从 `labels_work.jsonl` 回放。

**Q：误按撤销，能恢复吗？**
`undo` 是事件，不是删文件。撤销后重新标一遍即可；旧事件仍在文件里，可人工审计。

**Q：`--apply` 会不会重排/丢掉原来的 71 条？**
不会。原行按原顺序保留，只改 3 个字段；写前有 `candidates.jsonl.bak-<ts>` 备份。

**Q：一个人标 200 帧要多久？**
按每帧 20–30 秒（含预填微调、核对重复）：200 帧约 **66–100 分钟**；若还要复核已有 71 帧，
再加 24–36 分钟，合计约 **1.5–2.3 小时**。真正卡住进度的不是手速，而是 test 图不够（见第 1 节）。
