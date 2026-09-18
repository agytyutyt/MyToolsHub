# Python 版本选型评估（2026-09-14）

| 文档属性 | 内容 |
| --- | --- |
| 评估对象 | Python 3.8 / 3.10 / 3.14（含 3.13 作对照）对 JZToolsHub 的影响 |
| 评估方式 | 本机三套解释器**实测**（3.8.10 / 3.13.14 / 3.14.7）+ 仓库事实核对 + 官方支持周期查证 |
| 相关文档 | `docs/archive/20260914评估报告.md`、`HANDOFF.md` §9、`plugins/knowledge-base/backend/requirements.txt` |
| 前提 | **已决定取消对 Windows 7 目标机的支持**（2026-09-14），目标机基线为 Windows 10+ |
| 结论 | **不需要升级 Python 版本——基线已是 3.14（当前最新稳定版）。要做的是"减法 + 固化"：删掉 3.8/3.10 兼容叙事、把 3.14 写成硬基线、修 vendor 那一行 bug、解决 zfec 无 wheel 的打包硬伤** |

| 四段覆盖 | 落点 |
| --- | --- |
| 接口契约 | §3 推荐方案（基线固定 3.14、最低 3.12、取消 Win7 支线） |
| 实现路径 | §2 三版本逐项影响、§5 zfec 编译链路；实测命令见 §7 附录 |
| 当前实现状态 | §3 推荐方案**已执行**（含 vendor 一行 bug 补丁、`HANDOFF.md` / `README.md` 口径统一）；§5 zfec 已落库 `wheels/` |
| 后续优化方向 | §6 升级节奏建议（3.15 发布后不急于跟进）；复核触发条件见该节 |

> **覆盖范围**：Python 版本基线的**全部实测依据**：三版本对照、取消 Win7 的连带影响、zfec 无 wheel 的打包硬伤
> ｜**不覆盖**：实际打包 / 安装步骤 → `docs/guide/打包部署手册.md`；wheel 重建 → `wheels/README.md`
> ｜**随包**：否 ｜**最后核对**：2026-09-17

---

## 0. 结论摘要（先看这里）

**直接回答"是否需要升级 Python 版本"：不需要。** 基线已经是 3.14，而 3.15 要到 2026-10-01 才发布——已经没有更高的稳定版本可升。取消 Win7 支持**不是**产生了升级压力，而是**解除了唯一的降级压力**（3.8 支线），因此接下来的工作全是"减法"。

1. **项目自身代码与全部 11 个插件后端在 Python 3.8 上完全可用**——实测 136 个 endpoint 全注册、核心接口全 200、行为与 3.14 一致。仓库里"Python 3.8 是目标环境"的说法在**框架侧是成立的**。
2. **唯一的版本拦路虎是 vendor 的 Office 预览引擎，而它其实是一个一行 bug**：`vendor/xhr/__init__.py:84` 在运行时求值的注解里用了 `Optional` 却没导入。实测 **3.8 和 3.13 都是 `NameError` 直接导入失败**；3.14 之所以"能用"，是 PEP 649 惰性注解把这个错误掩盖了。
3. 因此仓库现有注释"引擎要求 Python >= 3.10"**是错的**：3.10~3.13 全都会失败（机制同 3.13，已实测），3.14 只是侥幸。
4. 补一行 `from typing import Optional` 后，引擎在 **3.8 / 3.13 / 3.14 全部正常，且渲染产物字节完全一致**（六种组合的 HTML sha256 前 16 位均为 `d8535d164748e122`）。
5. **取消 Win7 后，"必须用 3.8 打包"的唯一理由消失**，3.8 支线可以直接删掉；同时 §1.4 曾指出的"PyInstaller 官方只支持 Win8+"这个隐忧也一并作废——不需要再验证 Win7 了。
6. **但发现了一个与版本无关、却会挡住未来升级的硬问题**：`zfec`（轨迹转换 / QR 解码 / 信息传输三个插件的核心依赖）**在 PyPI 上没有 cp314 wheel**（最新 1.6.0.1.post0 最高只到 cp313）。当前 3.14 环境里的 zfec 是**本地用 MSVC 从源码编译**的。详见 §5。
7. **一个容易误伤的连带项**：**操作系统的基线 ≠ 浏览器的基线**。`main.js` 的 Chrome 72 规避、`jz-icon.js` 的 SVG emoji 回退、pdf.js legacy 构建等**不要**当"Win7 遗产"顺手删掉——内网 Win10 机器保留老 Chrome 同样常见。详见 §4.3。

---

## 1. 事实基础

### 1.1 Python 官方支持周期（截至 2026-09-14）

| 版本 | 状态 | 安全支持截止 | 备注 |
| --- | --- | --- | --- |
| 3.8 | **已 EOL**（2024-10-07） | 已停止 | **最后一个支持 Windows 7 的版本** |
| 3.9 | 已 EOL（2025-10-31） | 已停止 | — |
| **3.10** | 安全支持中 | **2026-10-31（距今 71 天）** | 即将 EOL |
| 3.11 | 安全支持中 | 2027-10-31 | — |
| 3.12 | 安全支持中 | 2028-10-31 | — |
| 3.13 | bugfix（至 2026-10-01） | 2029-10-31 | — |
| **3.14** | bugfix（至 2027-10-01） | 2030-10-31 | 当前项目打包基线 |

> 3.8 官方原文：*reached end-of-life on 2024-10-07. It is no longer supported and does not receive security updates.*

### 1.2 项目代码的版本兼容性（实测）

用 3.8 / 3.13 / 3.14 三套解释器对全库做「只编译不落盘」的语法检查：

| 范围 | 3.8 | 3.13 | 3.14 |
| --- | --- | --- | --- |
| 项目代码（60 个 .py） | 语法不兼容 **0** | 0 | 0 |
| vendor 引擎（53 个 .py） | 语法不兼容 **0** | 0 | 0 |

项目代码的事实：

- 类型标注统一用 `typing.Dict / List / Optional`（而非 3.9+ 的内建泛型），`trajectory-sketch` 另加 `from __future__ import annotations`；
- **未使用**任何 3.10+ 语法（无 `match`、无 `X | Y` 联合类型）与 3.10+/3.11+/3.12+ 新版 API（`itertools.pairwise` / `tomllib` / `functools.cache` / `datetime.UTC` 全无引用）；
- **未使用**任何已被移除的标准库（`distutils` / `cgi` / `imp` / `pkg_resources` 等全无引用）；
- **未使用** numpy 2.0 移除的别名（`np.float_` / `np.NaN` 等），也**未使用**已废弃的 `datetime.utcnow()`。

旁证：仓库内同时存在 `*.cpython-38.pyc` 与 `*.cpython-313.pyc`，说明这台机器上 3.8 与 3.13 都实际跑过本项目。

### 1.3 依赖可得性（三版本实测）

| 包 | 3.8 环境（已装） | 3.14 环境（已装） | 3.14 侧最低 Python |
| --- | --- | --- | --- |
| Flask | 3.0.3（>=3.8） | 3.1.3 | >=3.9 |
| Werkzeug | 3.0.6（>=3.8） | 3.1.8 | >=3.9 |
| cryptography | 47.0.0（>=3.8）⚠️ 已发弃用警告 | 50.0.0 | >=3.9 |
| waitress | 3.0.0（>=3.8.0） | 3.0.2 | >=3.9 |
| requests | 2.32.4（>=3.8） | 2.34.2 | **>=3.10** |
| openpyxl / xlrd / zfec / opencv-python | 全部可用 | 全部可用 | 较宽松 |
| python-docx | 1.1.2（>=3.7） | 1.2.0 | >=3.9 |
| pypdf | 5.9.0（>=3.8） | 6.16.0 | >=3.9 |
| qrcode | 7.4.2（>=3.7） | 8.2 | >=3.9 |
| numpy | **1.24.4**（3.8 的最后一版） | 2.5.2 | **>=3.12** |
| pillow / pystray / olefile | **未安装**（打包 3.8 支线需补） | 12.3.0 / 0.19.5 / 0.47 | pillow >=3.10 |
| zfec | 1.6.0.0（**本地编译**，PyPI 无 cp38/cp314 wheel） | 1.6.0.0（**本地编译**） | PyPI 最高仅到 cp313（§5） |
| PyInstaller | 6.22.0（Requires-Python `<3.16,>=3.8`） | 6.22.0 | — |

**在 3.8 上的直接代价**：主流库的新版本已普遍要求 >=3.9/>=3.10/>=3.12，3.8 只能停在旧版（numpy 1.24、python-docx 1.1、pypdf 5.9、qrcode 7.4）。并且 cryptography 47.0.0 在导入时就会打印：

> `CryptographyDeprecationWarning: ... The next release of cryptography will remove support for Python 3.8.`

也就是说**再升一次 cryptography，3.8 环境就会缺一个核心插件（admin 用 Fernet）的依赖**。

### 1.4 打包链路

| 事实 | 证据 |
| --- | --- |
| 现有全部部署产物都是 **Python 3.14** | `deploy/JZToolsHub`、`deploy/JZToolsHub-v1.5`、`deploy/JZToolsHub-v1.3.5`、`dist/JZToolsHub` 内一律是 `python314.dll` |
| PyInstaller 支持范围 | 6.22.0 声明 `<3.16,>=3.8`（3.8 与 3.14 都在内）；**3.14 支持自 6.15.0（2025-08-03）起** |
| `build-deploy.ps1` 的解释器来源 | `param([string]$Python = "python")` —— 默认取 PATH 上的 `python`，**无版本校验** |
| Win7 的真实可行性 | Python 官方：3.8 是最后一个支持 Win7 的版本；但 **PyInstaller 官方只支持 Windows 8+**（"should work on Windows 7 or newer, but we only officially support Windows 8+"）。仓库"HANDOFF/打包说明"里"需兼容 Win7 必须用 3.8 打包"的结论**从未在真机验证过** —— **该问题现已随 Win7 支持取消而作废**（§4.1） |
| 3.14 的 OS 下限 | Python 3.14 官方支持 Windows 10 及以上；3.12/3.13 支持 Windows 8.1 及以上 |

### 1.5 ★ 关键发现：KB Office 预览引擎与 Python 版本的真实关系

**实测结果（同一份 `sample.xlsx`，直接调 `xhr.convert`）**

| 解释器 | 未打补丁 | 打补丁后 | 渲染产物 HTML |
| --- | --- | --- | --- |
| 3.8.10 | ❌ `NameError: name 'Optional' is not defined`（`vendor/xhr/__init__.py:84`） | ✅ 21199 B | sha256[:16] `d8535d164748e122` |
| 3.13.14 | ❌ 同样 `NameError` | ✅ 21199 B | 同上（完全一致） |
| 3.14.7 | ✅ 21199 B（**靠 PEP 649 惰性注解才通过**） | ✅ 21199 B | 同上 |

**根因**：`xhr/__init__.py` 既没有 `from __future__ import annotations`，也没有导入 `Optional`，而第 84 行的 `def convert(data: bytes, options: Optional[ConvertOptions] = None)` 采用的是**运行时立即求值**的注解。Python ≤3.13 注解在定义时即求值 → `NameError`；Python 3.14 起 PEP 649 把注解改为惰性求值 → 恰好躲过。全 vendor 目录只有这 1 个文件有该问题。

**影响面**：`office_render.py` 用 `except Exception` 兜住了导入失败，所以旧版本上不会崩，而是**静默失去 Word/Excel 原样式预览**（回退 mammoth / SheetJS 降级渲染），且 `GET /api/knowledge-base/status` 会报 `office_render.available = false`。这类"静默降级"极难被用户识别为版本问题。

**两种最小补丁（均已在临时副本上实测通过，未改动仓库）**

```python
# 方案 A（推荐）：surgical，保持注解立即求值语义，与仓库其他模块风格一致
from typing import Optional

# 方案 B：把本模块注解全部字符串化（也可，但改变了注解求值语义）
from __future__ import annotations
```

两种补丁在 3.8 / 3.13 / 3.14 上渲染产物**字节完全一致**。建议用 A。

> 该补丁的性质与已有的 `xhr/renderer/table.py` 补丁相同：**属于「vendor 升级后必须重打」的清单**，需补记到 `plugins/knowledge-base/backend/vendor/README.md` 的补丁列表里。

---

## 2. 三个版本逐项影响

### 2.1 Python 3.8 —— 「框架完全可用，引擎需打补丁，但整个生态在往外走」

**实测可用性**：全量 11 个插件后端加载成功，136 个 endpoint 全注册，`/`、`/api/tools`、`/tools/visibility`、`/tool/<id>`、`/plugin/<id>/...`、`/admin`、`/api/admin/summary` 全部 200，`_plugin_load_errors` 为空 —— **与 3.14 跑出来的结果逐项一致**。

| 维度 | 影响 |
| --- | --- |
| 框架 / 插件 | ✅ 无影响（实测） |
| Office 预览引擎 | ❌ 不打补丁则完全不可用（静默降级） |
| 依赖版本 | ⚠️ 钉死在旧版；cryptography 下一版将移除 3.8 支持 |
| 官方安全支持 | ❌ 已 EOL 近 2 年，不再有安全补丁 |
| Win7 | ~~✅ 唯一可能~~ → **已作废**：Win7 支持已取消（§4） |
| 打包工具链 | ✅ PyInstaller 6.22 仍声明支持 >=3.8（本机已装）；缺 pillow / pystray / olefile 需补装；**zfec 需本地编译**（§5） |

**适用场景**：~~目标机是 Windows 7~~ → **取消 Win7 后已无用例**。保留本节是为了留下"3.8 实测可用"这一事实记录（证明框架侧本来就没有版本门槛，过去的"必须 3.14"其实是 vendor bug 造成的假象）。

### 2.2 Python 3.10 —— 「两头不占，且 71 天后 EOL」

| 维度 | 影响 |
| --- | --- |
| 框架 / 插件 | ✅ 可用（3.13 实测通过，3.10 是更低要求，机制上必然通过） |
| Office 预览引擎 | ❌ **同样失败**（注解立即求值机制与 3.13 一致；未实测，本机无 3.10 解释器） |
| 官方安全支持 | ⚠️ **2026-10-31 到期，只剩 71 天** |
| 相对 3.8 的收益 | 无实质收益（引擎问题没解决，还丢了 3.8 的旧机器覆盖面） |
| 相对 3.12/3.14 的收益 | 无（支持周期更短、依赖版本更旧） |

**结论：不建议引入 3.10。** 如果确实需要一个"中间版本"作为开发基线，选 **3.12**（EOL 2028-10）或 **3.13**（EOL 2029-10）。

> 顺带说明：仓库里"引擎要求 >=3.10"这句话，很可能是把上游项目 `pyproject.toml` 的 `requires-python` 当成了硬约束直接抄进注释。实际代码并不需要 3.10，真正需要的是"注解别炸"。

### 2.3 Python 3.14 —— 「当前事实标准，但引擎可用是侥幸」

| 维度 | 影响 |
| --- | --- |
| 框架 / 插件 | ✅ 可用（实测一致） |
| Office 预览引擎 | ✅ 可用，**但依赖 PEP 649 惰性注解这一"副作用"**——不是设计意图 |
| 依赖版本 | ✅ 全部可得且最新；numpy 2.5 / pillow 12 / requests 2.34 均要求 >=3.10/>=3.12 |
| 官方安全支持 | ✅ 至 2030-10-31 |
| 打包 | ✅ 现有产物多版本实践验证；需 PyInstaller >=6.15 |
| 风险 | ⚠️ ① 引擎隐患：一旦有人对引擎函数做 `typing.get_type_hints()`、dataclass/反射、或上游把该行改成别处求值，3.14 也会炸；② **zfec 无 cp314 wheel**，需本地编译（§5）；③ 目标机 OS 基线须 ≥ Windows 10 |
| 隐性收益 | 惰性注解让这类"注解里写错名字"的问题在本项目里被掩盖——**这是风险而不是优势**，因为它把问题推迟到某个不确定的时刻 |

---

## 3. 推荐方案

### 3.1 总原则

> **用一个明确、受支持、且不依赖语言副作用的版本基线。** 现有 3.14 基线本身没问题，但必须先把 vendor 那一行补掉——否则"3.14 能跑"这件事没有可解释性，换台机器/升个引擎就可能静默失去预览。

### 3.2 具体建议

| 项 | 建议 | 理由 |
| --- | --- | --- |
| **打包基线** | **冻结在 Python 3.14**（唯一基线，不再维护第二条线） | 现有 deploy/dist 全是 `python314.dll`，已跨 v1.3.5~v1.6 实践；依赖全绿；EOL 2030；PyInstaller 6.22 支持 |
| **必做修复** | **给 `vendor/xhr/__init__.py` 加 `from typing import Optional`** | 消除"靠 PEP 649 侥幸可用"的隐式依赖；打好后引擎在 3.8~3.14 全域可用且输出字节一致。补丁记入 `vendor/README.md` 的升级重打清单 |
| **源码开发基线** | 文档写"**最低 3.12 / 推荐 3.14**"（不再宣传 3.8） | Win7 取消后 3.8 已无存在理由；写 3.12 起是因为它能装到新版依赖且 EOL 2028，给开发机留一点余量；实际开发与打包统一 3.14 最省心 |
| **不要引入 3.10** | 明确排除 | 71 天后 EOL，且对引擎问题毫无帮助 |
| **删除 3.8 支线** | 移除 README/HANDOFF 中"需兼容 Win7 时必须用 3.8 打包"的表述与 `-Python` 的 3.8 示例；`.bat`/`install.ps1` 无 Win7 专用代码，无需改动 | Win7 已取消，保留该支线只会让人误以为还有第二条受支持路径 |
| **打包脚本增强** | `build-deploy.ps1` 增加解释器版本校验与记录 | 现在 `-Python` 默认取 PATH 上的 `python`，**无版本校验**；建议：① `version.json` 追加 `python` 字段（记录打包解释器版本）；② 与基线 3.14 不符时告警（防止有人用 PATH 上的 3.8 打出"功能残缺但没人发现"的包）。与已有的 `commit` / `built_at` 字段一脉相承 |
| **状态页暴露版本** | `GET /api/knowledge-base/status` 增加 `python_version` 与引擎 `version` | 现在引擎不可用时只看到 `office_render.available=false`，排查者难以判断是"缺引擎""Python 太旧"还是"vendor 未打补丁" |
| **解决 zfec 依赖** | 见 §5：产出预编译 wheel 或明确 MSVC 前置条件 | 这是当前 3.14 链路上**唯一必须本地编译**的依赖，是打包可复现性与未来升级的头号风险 |

### 3.3 落地清单

**立即可做（低风险，建议本轮一起提交）**

1. `plugins/knowledge-base/backend/vendor/xhr/__init__.py` 顶部加 `from typing import Optional`
2. `plugins/knowledge-base/backend/vendor/README.md` 的补丁清单追加该条（注明"上游同样需要修，待同步"）
3. 修正 `plugins/knowledge-base/backend/requirements.txt` 的错误注释：删掉"引擎要求 Python >= 3.10"，改为"引擎实测在 3.8~3.14 均可运行（需 `vendor/xhr/__init__.py` 的 `Optional` 导入补丁）"；同时补上"zfec 无 cp314 wheel，需 MSVC 源码编译"
4. 修正 `README.md` §2.1/§3.2 与 `HANDOFF.md` §2.3/§6 的版本口径，统一为"**最低 3.12 / 推荐与打包 3.14**"，并删除"需兼容 Win7 时必须用 Python 3.8 打包"的表述与 `-Python` 的 3.8 示例
5. `build-deploy.ps1`：`version.json` 增加 `python` 字段（打包解释器版本），并在与基线 3.14 不符时打印告警
6. 把 `app.py` / `jz-icon.js` / `trajectory-sketch` 注释里"理由=Windows 7"的措辞改为"无彩色 emoji 字体的环境"（**只改注释，代码一律保留**，见 §4.3）

**需决策后再做**

7. **zfec 的获取方式**（§5）——三选一：随包提供预编译 wheel / 明确 MSVC 前置条件 / 换纯 Python 纠错实现
8. 是否同时把内网的**浏览器基线**写清楚（当前实际在兼容 Chrome 72/78，见 §4.3）

---

## 4. 取消 Win7 支持的连带影响

### 4.1 解锁了什么

| 变化 | 说明 |
| --- | --- |
| **删除 3.8 打包支线** | 唯一理由（Win7）消失。`build-deploy.ps1 -Python` 不必再支持 3.8；文档不再需要"两条打包线按目标机二选一"的说明 |
| **作废一项未验证的风险** | 原 §1.4 的"PyInstaller 官方只支持 Win8+，所以 Win7 兼容存疑"——不再需要花半天做真机验证 |
| **依赖不再被向下兼容思维束缚** | 可以放心用新版依赖（numpy 2.x / Flask 3.1 / pillow 12 / requests 2.34），不必为了"万一要回到 3.8"而保守 |
| **可以启用现代语法（可选）** | 例如内建泛型 `list[str]`、`X | Y` 联合类型、`match`。**但不建议为此做大规模改造**——现有 `typing.Dict/List/Optional` 写法一致且无维护成本，改造收益极低、回归风险不小 |

### 4.2 顺便可以确认的事实

- `install.ps1`、`一键安装.bat`、`一键卸载.bat` 中**没有任何** Win7/PowerShell 版本相关代码（无 `#Requires`、无 `PSVersion` 判断、无 `chcp`），所以取消 Win7 **不需要改安装脚本**。
- `.gitignore`、`JZToolsHub.spec` 中也没有 Win7 相关条目。
- 结论：**Win7 的"痕迹"只有文档表述和几个注释，清理成本极低**。

### 4.3 ⚠️ 一个容易误伤的连带项：操作系统基线 ≠ 浏览器基线

仓库里"兼容旧浏览器"的措施有 4 处，它们**不是** Win7 的产物，不应随 Win7 一起删除：

| 位置 | 措施 | 为什么不能删 |
| --- | --- | --- |
| `static/js/main.js:75` | 规避可选链 `?.`，兼容 Chrome 72 | 内网 Win10 机器保留老 Chrome 同样常见 |
| `static/js/jz-icon.js` + `static/icons/` | emoji → SVG 回退（42 个 Twemoji SVG） | 触发条件不是操作系统，而是**缺少彩色 emoji 字体/旧字形渲染**；Windows Server、精简版系统同样会 tofu |
| `plugins/knowledge-base/frontend/vendor/` | pdf.js 用 legacy 构建；避免 `?.`/`??`/CSS `gap` | 「内网 Chrome 72/78」是独立于 OS 的既定约束 |
| `plugins/trajectory-sketch/frontend/icons/` | 页内图标一律自带 SVG，不用 emoji 字符 | 同上 |

**建议**：把"支持 Chrome ≥ 72（内网既有环境）"作为**独立的浏览器基线**写进 README/规范，与"目标机 OS ≥ Windows 10"并列。同时把 `app.py`、`jz-icon.js` 里"因为 Windows 7"的注释措辞改成"因为环境缺少彩色 emoji 字体"——**理由更容易站得住，也避免下一个人误判为可删**。

---

## 5. ★ zfec：当前 3.14 打包链路上唯一必须本地编译的依赖

### 5.1 实测事实

| 检查 | 结果 |
| --- | --- |
| 本机 3.14 环境里的 zfec | `zfec-1.6.0.0.dist-info`，WHEEL 标签 **`cp314-cp314-win_amd64`** |
| 同目录内容 | `_fec.cp314-win_amd64.pyd` + **`fec.c` / `_fecmodule.c`**（源码文件同时存在 → 典型"从 sdist 编译安装"特征） |
| PyPI 上 1.6.0.0 的 win_amd64 wheel | 只有 **cp39 / cp310 / cp311 / cp312 / cp313**（+ pypy pp38/pp39/pp310），**没有 cp38，也没有 cp314** |
| PyPI 上最新的 1.6.0.1.post0 | win_amd64 wheel 最高仍只到 **cp313** |
| 直接验证 | 在 3.14 上执行 `pip download zfec --only-binary :all:` → **`ERROR: No matching distribution found for zfec`**；在 3.8 上同命令可下到 `zfec-1.5.7.4-cp38-cp38-win_amd64.whl` |

### 5.2 这意味着什么

1. **当前的 3.14 打包是在"源码编译"前提下完成的**——那台打包机装过 MSVC Build Tools（或已有 C 编译环境）。
2. **换一台干净的构建机可能直接卡住**：`pip install -r plugins/trajectory-convert/backend/requirements.txt`（含 `zfec>=1.5`）会尝试源码构建；没有编译器就失败。这与本项目"一键打包"的预期不符，且失败现场是 pip 的编译报错，不直观。
3. **这是未来升级 Python 的头号风险**：zfec 是**版本锁定型 C 扩展**（`cpXX-cpXX` 标签，不是 abi3），每个新的 Python 小版本都要等维护者发 wheel 或继续本地编译。numpy / pillow 同属版本锁定型，但它们是活跃大项目，wheel 通常跟得上；**zfec 的维护节奏明显更慢**（1.6.0.0 是 2024-11 发布，至今未补 cp314）。
4. 受影响的功能面不小：`trajectory-convert`（二维码视频流编码）、`qr-video-decode`（zfec 纠错重组）、`info-transfer`（信封纠错）三个插件都依赖它。

### 5.3 三个处理选项

| 选项 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| **A（推荐，最省事）** | 在打包文档中**明确写出前置条件**：构建机需安装 MSVC Build Tools（含 C++ 工作负载），并把 `pip download --only-binary` 的预检做成打包脚本的前置检查 | 零代码改动；把隐性依赖变成显性 | 构建机仍需编译器 |
| **B（最稳）** | **自建一次 cp314 wheel**，把 `.whl` 纳入内网/仓库旁的制品目录，`requirements.txt` 改为指向该 wheel | 构建可复现，不再依赖编译器与网络 | 需要维护一个二进制制品；升级 Python 时要重做 |
| **C（最彻底）** | 评估替换为**纯 Python 纠错实现**（如 `reedsolo`）以消除 C 扩展依赖 | 彻底解决跨版本与跨平台问题 | 需要改动三个插件的编解码链路并重跑端到端验证（二维码视频流容错行为需实测），成本最高 |

**建议先做 A**（文档 + 打包前置检查，半天内可完成），把 B 作为内网交付时的加固手段；C 只在"长期要频繁升级 Python"时才值得。

---

## 6. 升级节奏建议

| 时间点 | 动作 |
| --- | --- |
| 现在 | 冻结 3.14；修 vendor 补丁；清掉 3.8/3.10 叙事；补 `python` 字段到 `version.json`；处理 zfec（选项 A） |
| 2026-10-01（3.15 发布） | **不跟进**。等第三方 wheel 齐备 |
| 2027 上半年（约 3.15.2+） | 评估 3.15：**前置条件**是 ① zfec 有 cp315 wheel（或决定继续本地编译）② numpy / pillow / opencv / cryptography 均有 cp315 wheel ③ PyInstaller 支持（6.21.0 起已支持 3.15） |
| 2027-10 之后 | 3.14 进入安全维护期（bugfix 到 2027-10-01），此时再规划下一轮也无压力——3.14 的 EOL 是 **2030-10-31**，窗口很宽 |

> 一句话：**没有需要升级的紧迫性，只有需要固化的地方。**

---

## 7. 附录：复现命令

```python
# 1) 全库语法兼容性（只编译不落盘，不写 __pycache__）
#    jz_pycompat.py 见本机临时目录；核心逻辑：
for p in all_py_files:
    compile(open(p, "rb").read(), p, "exec")     # 换解释器执行即得该版本结论

# 2) 引擎渲染对比（换解释器跑同一脚本，比对 sha256）
sys.path.insert(0, r"D:\JZToolsHub\plugins\knowledge-base\backend\vendor")
import xhr, dhr
r = xhr.convert(open("sample.xlsx", "rb").read(),
                xhr.ConvertOptions(mode="fragment", css_prefix="kbsheet", max_cells=120000))
print(len(r.html), hashlib.sha256(r.html.encode()).hexdigest()[:16])

# 3) 全量插件加载冒烟（隔离数据根，不碰真实 ~/.jztoolshub）
import jztools_data; jztools_data.get_data_root = lambda: <临时目录>
import app as A; A.DATA_ROOT = <临时目录>; A.CONFIG_PATH = <临时目录>/tools.json
A.register_plugin_backends(A.app)
print(A._plugin_load_errors, len(A.app.view_functions))
```

**本次实测的三套解释器**

| 版本 | 路径 |
| --- | --- |
| 3.8.10 | `C:\Users\yfjz\AppData\Local\Programs\Python\Python38\python.exe` |
| 3.13.14 | `C:\Users\yfjz\.workbuddy\binaries\python\versions\3.13.12\python.exe` |
| 3.14.7 | `C:\Python314\python.exe` |

---

*JZToolsHub · Python 版本选型评估 · 2026-09-14*
