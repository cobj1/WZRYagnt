# WZRYagnt/reverse/（本目录 = 逆向**分析**）

⚠️ 与工作区根目录的 `reverse/` 同名不同物：

| 目录 | 内容 |
|---|---|
| **`WZRYagnt/reverse/`（本目录）** | 71 个分析脚本（8 488 行）+ `out/` 结论产物（951 文件 / 2.6 GB） |
| `D:/work/aiKings/reverse/`（根目录） | 二进制原料：`libil2cpp.so`(227 MB)、`libGameCore.so`(86 MB)、`global-metadata.dat`(60 MB)、内存 dump、`Il2CppDumper` |

**读结论在本目录的 `out/`；要重扫才去根目录的 `reverse/hok/`。**

## 路径依赖

- 本目录脚本读原料时，应通过 `AIKINGS_ROOT` 环境变量（默认 `Path(__file__).resolve().parents[2]`）拼 `reverse-bin/`（原 `reverse/`，改名计划见根目录 `reverse/README.md`）。
- `parse_metadata.py` 已按此规则改造；其余脚本若仍写死绝对路径，请一并替换为 `__file__` 相对定位。

## 证据等级

内存偏移、字段语义的**权威记录**在 `WZRYagnt/docs/wzry-ai-reverse.md`（含证据等级与被证伪清单）。本目录只放工具与产物，结论不要写在这里。
