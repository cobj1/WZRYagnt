# -*- coding: utf-8 -*-
"""camp_src 取值的单一事实来源（v15.2）。

`camp_src`（阵营判据来源）由感知层产出（`hok_yolov5.map_class` / `detector.Det`），
随 state units 流向下游。它被**两处以相反口径**消费：

  - 决策层 `wzry.decision.optimizer.enemy_hero_trusted`：只有"判据不显著时的保守兜底判敌"
    （`default`）不可信；其余（含缺失 / `None`）一律放行 —— 这是对旧行为的向后兼容。
  - 训练侧 `wzry.train.encoding`：只有被明确判据确认的来源可信，其余记 0。

两个集合互补，但改动前**没有共同定义**：任一处新增取值只会改到一边 -> 口径漂移。
同类事故已有先例（`top_status_bars` 曾因阈值口径漂移导致真机扫不到血条，见其模块注释）。

本模块**只做声明，不改变任何行为**：两处消费者各自引用同一组常量，取值含义集中在此，
新增取值时必须同时评估两个集合。无依赖（叶模块），可被 decision / train 安全导入。
"""

# 感知层可能产出的全部取值：
#   bar     = 英雄"框上方血条颜色"判定（自己绿/队友蓝/敌人红）—— 高置信
#   color   = 框内蓝/红像素占比判定（小兵/塔/水晶）—— 高置信
#   neutral = 中立单位（野怪/龙/BUFF），无阵营
#   default = 阵营判据不显著（遮挡/远景/特效）时的**保守兜底判敌** —— 幻影敌人来源
#   extra   = schema 之外的额外类（草丛等）
#   unknown = 非感知层来源 / 旧数据缺字段（如 ultralytics .pt 后端不写 camp_src）
CAMP_SRC_VALUES = ("bar", "color", "default", "neutral", "extra", "unknown")

# "判据可信"的允许集（训练特征 trust 位用；不在其中即为 0）。
TRUSTED_CAMP_SRC = ("bar", "color", "neutral")

# "幻影敌人"过滤的否决集（决策层用；不在其中一律可信）。
# ⚠️ 只有保守兜底的 default —— 扩大它等于收紧决策，属行为变更，须单独评审。
UNTRUSTED_CAMP_SRC = ("default",)
