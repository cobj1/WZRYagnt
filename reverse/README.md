# WZRYagnt/reverse/（本目录 = 逆向**分析**）

⚠️ 与工作区根目录的 `reverse/` 同名不同物：

| 目录 | 内容 |
|---|---|
| **`WZRYagnt/reverse/`（本目录）** | 71 个分析脚本 + `out/` 结论产物（787 文件 / 2.6 GB） |
| `D:/work/aiKings/reverse/`（根目录） | 二进制原料：`hok/libil2cpp.so`(227 MB)、`hok/libGameCore.so`(86 MB)、`hok/global-metadata.dat`(60 MB)、内存 dump、`tools/Il2CppDumper` |

**读结论在本目录的 `out/`；要重扫才去根目录的 `reverse/hok/`。**

## 路径依赖

- 本目录脚本读原料时，应通过 `AIKINGS_ROOT` 环境变量（默认 `Path(__file__).resolve().parents[2]`）拼 `reverse-bin/`（原 `reverse/`，改名计划见根目录 `reverse/README.md`）。
- `parse_metadata.py` 已改为优先 `reverse-bin/`、缺失时回退 `reverse/`；改名前后都能运行。其余脚本若仍写死绝对路径，请一并替换为 `__file__` 相对定位。

## 安全读与诊断（2026-10-25）

参考 TrueVision 的只读安全结论，已落地、均不含 KPM/perf/注入：

- `livemem.py`：`is_mapped` / `read_checked` / `read_ptr_checked` / `read_spans_checked`（映射白名单、单次 4MB 预算、失败返回 None、PAC 去 tag）；`resolve` 在重复映射时优先 exec 段。为兼容定位阶段的 48MB 大块读，原 `read` 语义未改，新诊断走 checked API。
- `diag_actor_state.py`：对局内记录 actor 指针 / (cid,camp) / slot 变化、HP 连续零帧判死、位置抖动；`--selftest` 无设备自检。
- `diag_respawn_identity.py`：死亡→复活段的身份稳定性；`respawn_s`/`deaths`/HP 三路判死，对比死前与复活后的 actor/slot/camp/状态对象地址；`--auto-die` 摇杆送塔，`--selftest` 无设备自检。
- `../scripts/test_livemem_safe.py`：安全读与 exec 段优先的离线回归。
- `../scripts/test_camp_identity.py`：阵营无关的自我定位/镜像消歧/敌我划分回归（19 项）。

未移植：KPM、ESP、slotpatch、HW BP、注入、反检测、商业驱动、雾旁路（版本 offset 不兼容）。

## 证据等级

内存偏移、字段语义的**权威记录**在 `WZRYagnt/docs/wzry-ai-reverse.md`（含证据等级与被证伪清单）。本目录只放工具与产物，结论不要写在这里。
