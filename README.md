# WZRYagnt（内测）

王者荣耀对局自动化 Agent（仅用于训练与研究）：屏幕视觉 + 内存直读 + 官方 AI 逆向。

## 文档入口

- 仓内文档索引（本目录 `docs/`）：
  - [离线对局 Runbook](docs/OFFLINE_MATCH_RUNBOOK.md)
  - [执行通道架构](docs/ARCH_CONTROL_CHANNEL.md) / [决策分层](docs/ARCH_DECISION_LAYERS.md) / [UI 查询层](docs/ARCH_UI_QUERY_LAYER.md)
  - [逆向主记录](docs/wzry-ai-reverse.md) / [官方数据面](docs/wzry-ai-data-inputs.md)
  - [奖励标准](docs/REWARD_STANDARD.md) / [规则长尾](docs/RULES_TRIAGE.md) / [标注指南](docs/ANNOTATION_GUIDE.md)
  - 最新评估：[决策离线评估](docs/决策离线评估报告-2026-10-24.md) / [离线回放评估](docs/决策离线回放评估-2026-10-25.md) / [端到端回放](docs/决策端到端离线回放-2026-10-25.md) / [延迟基准](docs/决策延迟基准-2026-10-24.md) / [桥接保真度](docs/决策桥接保真度-2026-10-24.md)
  - 历史蓝图：[PLAN.md](docs/PLAN.md)；学习日志：[LEARNING_LOG.md](docs/LEARNING_LOG.md)
- 工作区总入口：`../docs/README.md`

## 快速自检

```bat
:: 一次性安装（当前环境为 Python 3.12）
venv\Scripts\python.exe -m pip install -r requirements-dev.txt

:: 配置校验 + 全部离线回归
venv\Scripts\python.exe -X utf8 scripts\validate_configs.py
venv\Scripts\python.exe -X utf8 scripts\run_tests.py
```

`run_tests.py` 当前 **25 套全绿**：protocols / decision_output / action_dispatch / context / engage / combat / combo / safety / mem_offsets / perception_contract / replay_capture / channel_health / inference / trace_report / eval_replay / official_ai / laya / new_modules / bridge_fields / data_contract / golden_readiness / frame_features / collector_coverage / security / m2 行为回归；不含需要真机的 ESP32/采集用例。

运行真机/离线对局前先读 `docs/OFFLINE_MATCH_RUNBOOK.md`；当前只支持离线对局，不上线上真人对局。
