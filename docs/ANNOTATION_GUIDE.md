# 标注复核与扩充指引（M1 · 历史指南，当前 13 类）

> **2026-10-25 现状**：当前类别定义以 `configs/classes.json` 为准，共 **13 类**
> （比早期 M1 多出 `dragon`、`jungle_buff`，`hook_aim`/`skill_effect` 顺延到 11/12）。
> `scripts/vision/annotate.py` 与 `scripts/train/prepare_dataset.py` 都已读取这份配置。
>
> **数据缺失**：本文写作时的 `data/screenshots/zhongkui`、`data/yolo_dataset/zhongkui`
> 与 `models/yolo/yolov8n.pt` 已不在当前工作区；下文的 1379 框等数字是历史快照，不能当作现状。
> 重做标注前需先恢复截图与标签，并按下面的 13 类 id 检查旧标签（旧 9/10 需要重映射到 11/12）。

## 当前类别（`configs/classes.json`）

| id | 类名 | 说明 |
|---:|---|---|
| 0 | enemy_hero | 敌方英雄 |
| 1 | ally_hero | 我方/友方英雄 |
| 2 | enemy_minion | 敌方小兵 |
| 3 | ally_minion | 我方小兵 |
| 4 | enemy_turret | 敌方防御塔 |
| 5 | ally_turret | 我方防御塔 |
| 6 | enemy_crystal | 敌方水晶 |
| 7 | ally_crystal | 我方水晶 |
| 8 | neutral_monster | 中立野怪 |
| 9 | dragon | 暴君/主宰等龙类 |
| 10 | jungle_buff | 红/蓝 Buff |
| 11 | hook_aim | 钟馗钩子瞄准指示 |
| 12 | skill_effect | 技能特效 |

> 早期 M1 的框数（enemy_hero 454、enemy_minion 622、enemy_turret 253、hook_aim 50，
> 共 1379 框）对应旧 11 类 id，仅作历史参考。

## 工具用法

```bat
venv\Scripts\python.exe scripts\vision\annotate.py data\screenshots\zhongkui
```

按键：`c` 循环当前类别｜鼠标拖框｜`l` 加载已有标签｜`s` 保存｜`n/p` 翻页（自动加载+自动保存增删）｜`d` 删除最后一个框｜`q` 退出。

标注完成后用 `prepare_dataset.py` 生成 `data.yaml`（`nc` 与 `names` 直接来自 `configs/classes.json`）。

## 复核优先级（数据恢复后）

1. **旧标签重映射**：旧 9（hook_aim）→ 11，旧 10（skill_effect）→ 12；再补 9/10（dragon/jungle_buff）。
2. **阵营歧义**：旧 `enemy_minion` / `enemy_turret` 中可能混有友方单位；按血条颜色（绿=友/红=敌）与兵线位置复核。
3. **补标** `ally_hero`、`ally_minion`、`ally_turret`、`dragon`、`jungle_buff`、`skill_effect`。
4. 半自动预标注只能覆盖旧 4 类；新类仍需手工框或另训检测器。

## 重训流程（数据恢复后）

```bat
:: 1) 同步 screenshots -> yolo_dataset（类别数来自 configs/classes.json）
venv\Scripts\python.exe scripts\train\prepare_dataset.py

:: 2) 训练（需要先准备一个 YOLO 权重；当前 models/yolo/yolov8n.pt 不存在）
venv\Scripts\yolo.exe train model=<你的权重>.pt data=data/yolo_dataset/zhongkui/data.yaml epochs=100 imgsz=640 batch=8 device=0
```

注意：`ultralytics` 当前版本没有 `python -m ultralytics.train` 入口，使用 `yolo.exe` 或 Python API。
