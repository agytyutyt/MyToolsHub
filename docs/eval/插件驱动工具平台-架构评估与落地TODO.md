# 插件驱动工具平台 · 架构评估与落地 TODO

> 评估对象：`docs/eval/插件驱动工具平台总体架构说明.md`（下称「总体架构说明」，为**目标态/理想态**架构）
> 对照实现：仓库 `D:\JZToolsHub` 当前主干 `main`（截至 2026-10-15）
> 评估结论口径：`已实现` / `部分实现` / `未实现`，均附代码定位（`file:line` + 关键类/函数）
> 本文档位于 `docs/eval/`，**不在随包 `copy_dirs`**（`static`、`docs/guide`、`docs/design`）内，落档不触发 S11 / 主包切版。

---

## 0. 阅读须知：本文的定位与一句话结论

「总体架构说明」是一份**面向容器化/多租户/云原生**的目标态蓝图（环境池 + 指纹复用 + 子进程隔离 + 按需启动 + 重依赖服务化 + 控制面/数据面分离 + K8s 演进）。而本仓库的真实交付场景是**公安内网、离线、老 Windows、8GB 单机**（见用户画像与 `docs/design/服务端插件运行架构-设计文档.md` 等「拍板」记录）。

因此二者存在**方向性差异**，且**多数差异是刻意的工程取舍，不是遗漏**。本文的任务不是「判定代码不达标」，而是：

1. 如实还原两套模型；
2. 逐项对照，区分**「刻意取舍」**（如共享运行时注入、venv 非沙箱、无 K8s）与**「真实缺口」**（如 Manifest 无 Schema、无请求级 `request_id` 贯穿、加载期无完整性校验、无并发/内存闸门）；
3. 只把**真实缺口**转成可执行 TODO，并明确**哪些目标态条目在本场景应判「不做」**。

**一句话结论**：进程级隔离、生命周期治理、离线依赖闭包、回滚这几块**已达工程可用且部分优于文档**；而「契约先行（Manifest Schema + 版本协商 + 签名）」「上下文/可观测性贯穿（request_id/结构化日志）」「资源闸门（并发上限/内存监控/按需启动）」三块是**真实且值得补的缺口**；「环境池指纹复用 + LRU + 重依赖服务化 + 容器/K8s」在本场景**建议维持不做或降级为轻量版**。

---

## 1. 架构还原（文档描述的目标态）

### 1.1 九条硬约束（总体架构说明 §一）

| # | 约束 | 关键含义 |
|---|------|----------|
| 1 | 最小内核稳定 | 壳层/IPC/权限/配置/日志/更新/插件注册/环境管理不可被当普通插件卸载 |
| 2 | 除内核外一切皆插件 | 主界面/后台/插件管理/市场皆插件，但核心插件随壳启动、代码受控 |
| 3 | 每插件独立进程或服务 | 桌面端每插件一子进程；服务端每插件一服务/容器 |
| 4 | 依赖隔离优先 | **禁止**多版本依赖挂载到共享 Python 运行时；按指纹复用只读 venv |
| 5 | supervisor 职责收敛 | 只做启停/健康/重启/日志/状态机，**不做**上下文/鉴权/配置中心 |
| 6 | 控制面/数据面分离 | 控制面管注册/调度/配置/鉴权/审计；数据面跑插件实例 |
| 7 | 8GB 可运行 | 按需启动 + 空闲回收 + 并发上限 + 重依赖服务化 |
| 8 | 插件契约先行 | Manifest 声明元数据/依赖/权限/入口/API 版本/资源/健康检查 |
| 9 | 平滑演进 | 环境池→容器；指纹→镜像标签；子进程→Pod |

### 1.2 分层与核心抽象

- **桌面端四层**：壳层（窗口/WebView/托盘/文件/更新） → 核心层（进程管理器、环境池管理器、消息总线/IPC、上下文服务、鉴权与权限、配置中心、插件注册中心、日志与指标） → 能力层（一切业务/管理插件） → 运行时（环境池 + 子进程）。
- **服务端三层**：控制面（注册/版本/路由/配置/权限/灰度/回滚/日志聚合） → 数据面（插件后端实例运行） → 运行时（同桌面端，生产优先容器）。
- **核心抽象**：Manifest（契约）、依赖指纹（`hash(py_version+platform+abi+sorted(lock)+sys_libs)`）、环境池（`runtime/envs/<指纹>` + `refs/<指纹>.json` 引用计数 + LRU + 只读）、受限 token（内核鉴权后注入，插件不持主凭证）、上下文对象（`request_id/user_id/tenant_id/token/config_ref/trace_id`，大对象走共享内存/文件）。
- **扩展点**：插件后端 `register(app)`；前端包（静态资源）；配置 schema；健康检查探针；权限声明；`runtime.type=venv|container`。
- **通信**：桌面端 JSON-RPC over stdio / 本地 Socket；服务端 HTTP/gRPC + MQ；上下文传引用不传大对象。

### 1.3 文档描述的「插件加载与调用链路」（目标态）

```
安装 → 校验签名/哈希 → 按指纹创建/复用 venv → 注册(Manifest+版本协商) →
按需启动子进程(对应环境解释器, 清 PYTHONPATH) → 健康检查 → 就绪 →
运行(JSON-RPC over stdio, 上下文透传 request_id/trace_id) →
空闲回收 / 崩溃重启退避 / 熔断 → 下架 → 停止 → 卸载(refs-1, LRU)
```

### 1.4 实际实现的调用链路（还原自代码，供对照）

**服务端（浏览器直连主应用）**：
```
浏览器 → app.py 主 Flask
  ├─(默认, 进程内插件) plugin_loader._load_plugin_routes → jz_plugin_host.load_backend_module
  │     → importlib 载入 plugins/<id>/backend/routes.py → register(app) 挂到同一 Flask
  └─(E 架构, worker 插件) app.py 反代 /api/<id>/* → supervisor/mainapp_bridge._proxy_to_worker
        → HTTP 127.0.0.1:5101..5199 → jz_worker.run_worker 独立 Flask 进程 → register(app)
```
- 定位：`app.py:549-558`（反代）、`supervisor/mainapp_bridge.py:119`（`_proxy_to_worker`）、`jz_host/plugin_loader.py:318-364`（进程内加载）、`jz_worker.py:102-191`（worker 装配）、`supervisor/supervisor_core.py:267`（`start_unit` 拉起子进程）。

**桌面客户端（WebView2 壳）**：
```
pywebview/WebView2 shell_window → http://127.0.0.1:<随机端口>
  → client/capability_app.py 能力层 Flask(waitress)
     ├─(hybrid) 进程内 Flask 子应用, werkzeug test_client 派发 (capability_app.py:690-711,960)
     └─(worker) client/plugin_executor.py 子进程: runtime python + plugin_host.py
           → stdio-json-v1 单行请求/响应 → mini-WSGI → register(app)
  → 需要服务器数据时经 client/jz_relay.py 白名单中继(自动附会话镜像 Cookie)
```
- 定位：`client/client_main.py:162-180`（能力层起服务）、`client/shell_window.py:135-290`（WebView2 壳 + 托盘）、`client/plugin_executor.py:193-258`（子进程 spawn + stdio 往返）、`client/plugin_host.py`（worker 侧 mini-WSGI）、`client/jz_relay.py:48-106`（中继白名单）。

**关键观察**：文档设想的「统一子进程隔离 + JSON-RPC + 环境池」在实现里落成了**三套并存的执行形态**——① 服务端进程内 `importlib`、② 服务端 worker 独立 HTTP 进程、③ 客户端 stdio 子进程 + hybrid 进程内。三者契约都是 `register(app)`（这点统一且优秀），但**隔离强度、通信协议、依赖注入方式各不相同**。

---

## 2. 实现对照（逐项核对）

### 2.1 桌面端 · 壳层与核心层

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| 壳层：窗口/WebView/托盘/文件/更新器 | 部分实现 | `client/shell_window.py:135-290`；托盘 `shell_window.py:238-281`；单实例 `client/single_instance.py:7` | WebView2 + pystray 托盘 + 关闭进托盘 + 几何持久化齐全；**无自更新器**，仅 `/local-api/restart` 重启动交接（`client_main.py:76-106`） |
| 核心层拆分为独立模块（非全塞 supervisor） | 部分实现 | 能力层集中在 `client/capability_app.py`（117KB 单文件） | 职责已按文件分（executor/relay/registry/config/secure_store），但**能力层主文件巨型化**，核心层「八模块」未清晰拆分 |
| 消息总线/IPC = JSON-RPC over stdio / Unix Socket | 部分实现 | `client/plugin_executor.py:228-258`；`client/plugin_host.py:6-9,51-67` | 实为**自定义 stdio-json-v1**（单行 `{request_id,method,path,headers,body_b64}`→单行响应），HTTP 形态而非 JSON-RPC；无 Unix Socket |
| 禁止插件直接操作主 GUI 对象 | 已实现 | 子进程/test_client 隔离 | 插件不触 GUI，隔离成立 |
| 上下文服务（request_id/user_id/tenant_id/token/config_ref/trace_id，大对象走共享内存） | 未实现（等价物部分实现） | `X-JZ-User` 头注入 `capability_app.py:195-211,953-955`；`JZTOOLS_DATA_ROOT` env 注入 `plugin_executor.py:209-213` | **无统一上下文对象**；`request_id` 每次生成但**不贯穿日志/追踪**；无 `tenant_id/trace_id/config_ref`；大对象靠 8MB 上限 + 「写临时文件传路径」约定（`plugin_executor.py:22,252-257`），非共享内存 |
| 鉴权与权限：内核统一鉴权 + 注入**受限 token**，插件不持主凭证 | 部分实现（且与文档相反） | 壳 run-token `capability_app.py:778-806`；`X-JZ-Shell` 静态键 `client_config.py:114-134`；会话镜像 DPAPI `client/session_mirror.py:16-79` + `client/secure_store.py:20-41`；中继 `client/jz_relay.py:48-106` | 无「受限 token」；中继**自动附带用户会话 Cookie** → **插件实际持有用户完整会话**，非降权 token（安全缺口，见 §3） |
| 配置中心：插件配置 schema + 实例配置 + 热更新 | 部分实现 | 客户端 `config.json` 每请求热读 `client_config.py:59-134`；服务端 `config/tools.json`、`jz_host/llm_routes.py:35-93` | 有**热读**、无**配置 schema/实例配置服务** |
| 插件注册中心：Manifest/版本/API 兼容范围/前后端包 | 部分实现 | 客户端 `client/local_registry.py:7-31`；服务端 `jz_host/plugin_loader.py:86-149,255-315` | 有 Manifest 扫描 + 版本；**无正式 Schema**、**无 API 兼容范围字段**（仅整数 `api_version` 比较，见 2.5） |
| 日志与指标：结构化 + plugin_id/instance_id/request_id | 部分实现 | `client/plugin_executor.py:260-269`（audit）；`client_main.py:26-33`（client.log）；per-plugin stderr `plugin_executor.py:223-226` | **纯文本、非结构化**；audit 有 user/plugin/method/path/耗时/结果，但**无 instance_id/request_id/trace_id**；无指标 |

### 2.2 服务端 · 控制面/数据面

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| 控制面/数据面分离 | 部分实现 | 反代 `app.py:549-558`；控制 API `supervisor/control_api.py:82-108`（status/start/stop/restart/clear-circuit/apply/rollback，127.0.0.1:5100，`X-Supervisor-Token`） | 仅 **worker（E 架构）插件**做到进程外数据面；**进程内插件（默认）与主应用共享 Flask，无面分离** |
| 插件注册/版本/路由 | 已实现 | `jz_host/plugin_loader.py:86-116`（`config/tools.json` 注册表，RLock）+ `:119-149`（Manifest 扫描）+ `:255-315`（聚合）；路由反代 `mainapp_bridge.py:119` | 注册表 + 目录扫描 + 反代路由齐全 |
| 上线/下架/调度 | 部分实现 | 商店发布态 `plugins/store/backend/routes.py:77-87`；admin 发布/下架/撤回 `plugins/admin/backend/store_admin.py` | 有发布态门控；**无调度器**（worker 端口静态分配 5101-5199，`supervisor_core.py:115`） |
| 灰度/canary | 未实现 | grep 无命中（「灰度」命中均为图像灰度） | 无灰度发布 |
| 回滚 | 已实现（优于文档） | `supervisor/apply_chain.py:191-330`（备份 zip→停→替换→健康探测→**失败自动回滚**，`rollback_unit:334`）；admin `/api/admin/plugins/rollback` | 部署级自动回滚真实可用 |
| 配置/鉴权/审计（控制面职责） | 部分实现 | 鉴权 Cookie-session `plugins/admin/backend/routes.py:90,718-730,1151,1161-1181`；权限点 `app.py:183-206`；shell gate `jz_host/shell_gate.py:35-253`；审计 `jz_host/access_log.py` | 见 2.4/2.6 |
| 数据面：插件独立 DB schema / 版本化迁移 | 部分实现 | 数据经 `jztools_data.py`；迁移 `test_data_migrate.py`/`docs/design/数据迁移-设计文档.md` | 有数据层与迁移，**非「每插件独立 DB/schema」强隔离** |

### 2.3 运行时 · 依赖管理与环境池（**差异最大处**）

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| 三层模型：基础只读 + 环境池按指纹复用 + 子进程隔离 | 未实现（改为两层共享优先） | 默认 `deps_mode:shared` 注入 `runtime/pylibs` 到**同一解释器** `sys.path[0]` `jz_plugin_host.py:55-56,62-92`；引导 `jz_host/dep_bootstrap.py:33` | **实际是「共享运行时优先 + 可选 per-plugin venv」**，与文档三层模型不同 |
| **禁止**多版本依赖挂载共享运行时 | **被违反（且为默认 sanctioned 机制）** | `jz_plugin_host.py:27-96`（`setup_dep_components`）；运行中重扫注入 `jz_deps.py:711-715,732,743,791` | 组件包装进 `runtime/pylibs` 直接注入共享 `sys.path`，并支持**同进程运行中热注入** |
| 依赖指纹 `hash(py+platform+abi+lock+libs)` | 未实现 | 最接近：venv marker `{plugin,wheels,base}` `supervisor/venv_mgr.py:58,90` | marker 仅 wheel 文件名列表，**无 platform/abi/lock 语义**，不是指纹 |
| venv 按指纹**复用/共享** | 部分实现 | `venv_mgr.py:52-93`（`ensure_venv`，marker 相等则 `reused:True`） | venv 按 **plugin_id** 建（`<data_root>/venvs/<id>/`），**相同依赖集不跨插件共享**；用 `--system-site-packages`（基线泄漏，`:81`） |
| 引用计数 refs±1 | 未实现 | 仅静态归属表 `jz_deps.py:364`（`component_file_owners`，卸载安全用） | 非运行期计数 |
| LRU 回收 / eviction | 未实现 | venv 仅在卸载/重建时删 `venv_mgr.py:96`（`discard_venv`） | 无 LRU |
| 只读保护 site-packages | 未实现 | 组件 writable 拷入 `jz_deps.py:262,269`（`_copy_component_payload`/`copyfile`） | 无只读标记 |
| 硬链接缓存 / uv | 未实现 | 纯 `pip --no-index` `venv_mgr.py:85`；`shutil.copyfile` `jz_deps.py:269` | 无 uv、无硬链接 |
| 按需启动 + 空闲回收（服务端） | 未实现（客户端部分实现） | 服务端 `supervisor_core.py:456`（`start_all` 急切全启）；**客户端** `plugin_executor.py:136-191`（`idle_ttl` 收割线程，常驻 worker 空闲干净收回） | 服务端无常驻回收；客户端已实现 idle 回收（F6） |
| 重依赖服务化（torch/CUDA 共享服务） | 未实现 | 无共享重依赖服务 | 重库进共享 `sys.path` 或 per-plugin venv |
| 延迟导入 lazy_import 设施 | 部分实现（附带） | 临时性 call-time import `jz_deps.py:274`、`dep_bootstrap.py:64` | 仅为规避循环导入，**非框架级 lazy-import 设施**；Manifest 无 `lazyImports` |
| 低内存模式 / `max_concurrent_plugins` | 未实现 | grep 无命中 | 无并发上限、无低内存降级 |
| 内存监控（psutil 每插件 RSS） | 未实现 | supervisor 无 psutil；健康仅 HTTP 存活探针 `supervisor_core.py:333`；`t13-thread-discipline.py:150` 明确规避 psutil | 无内存监控，OOM 靠熔断被动兜底 |
| 离线依赖闭包（文档未强调，实现亮点） | 已实现（优于文档） | `venv_mgr.py:85`（`pip --no-index --find-links`）；组件包 sha256 校验 `plugins/admin/backend/plugin_admin.py:262-389` | **离线闭包安装 + 机器校验**契合内网交付，是文档未覆盖的实现优势 |

### 2.4 插件契约 · Manifest 与通信

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| Manifest 必备 15 项（id/name/version/apiVersion/entry/runtime/requirements.lock/fingerprint/权限/前后端包/配置 schema/健康检查/资源/重启策略/签名/lazyImports/memoryHint） | 部分实现（覆盖率低） | 服务端 `plugins/*/manifest.json`（id/name/version/entry/form/core/requires/features，见 `plugins/admin/manifest.json`）；客户端 `client-plugins/*/manifest.json`（+`runtime:local`/`kind:hybrid`/`entry.mount`/`resident`/`idle_ttl`/`timeout_ms`） | **缺**：`apiVersion`（仅 `requires.api_version` 整数）、`requirements.lock`、`fingerprint`、配置 schema、健康检查声明、资源需求、重启策略、签名、`lazyImports`、`memoryHint`。注：客户端 `runtime:"local"` 是**部署目标标记**，非文档 `runtime.type=venv\|container` |
| Manifest JSON Schema | 未实现 | 仓库无 schema 文件；校验仅「是 dict」+「id==目录名」`plugin_loader.py:142-144` | **无正式 Schema、无字段级校验** |
| 核心 API 版本协商，不兼容拒绝加载 | 部分实现 | `jz_deps.py:43`（`PLUGIN_API=1` 唯一真源）+ `:598-621`（`_version_gate_rows`）+ `:663-697`（`evaluate`）；被 gate 的插件不加载 `plugin_loader.py:378-420` | **整数比较**（`min_app_version`/`api_version` 超上限即 blocked），非「兼容范围协商」；但**拒绝加载**语义已具备 |
| 通信协议：JSON-RPC / gRPC+MQ；上下文传引用 | 部分实现 | 客户端 stdio-json-v1（见 2.1）；服务端 worker HTTP 反代；中继 HTTP | 无 JSON-RPC/gRPC/MQ；大对象靠「临时文件传路径」约定 |

### 2.5 生命周期与治理

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| 进程管理器：启停 | 已实现 | `supervisor_core.py:267`（`start_unit`）/`:308`（`stop_unit`，terminate→wait8s→kill）；Windows Job Object 收孤 `:140,299` | 真 OS 子进程，孤儿回收完善 |
| 健康检查 | 已实现 | `supervisor_core.py:342`（`_monitor_loop`，10s 轮询）+ `:327,333`（HTTP `/_worker/health`）+ `:42`（8s 启动宽限） | 存活探针 |
| 崩溃重启 | 已实现 | `supervisor_core.py:400-407` | 死进程且非手停→重拉 |
| 指数退避 | 部分实现 | `supervisor_core.py:402`（`backoff=min(2*max(streak,1),30)`）+ `:43,376`（60s 稳定窗清零） | **线性退避**（2/4/6…封顶 30s），非指数 |
| 熔断 | 已实现 | `supervisor_core.py:392-399`（双计数器 `_fail`/`_crash` 任一达阈值 3 熔断）+ `:439`（`clear_circuit`） | 熔断后不自动重试，靠控制 API 恢复 |
| 告警 | 未实现 | grep 无 alert/notify/webhook；仅日志 `:396` + 反代 503 `mainapp_bridge.py:141` | 无主动告警 |
| 生命周期状态机 | 部分实现 | 无显式状态枚举；隐式标志位 `enabled`/`_procs`/`_circuit`/`_applying`/`_stop_requested`/`installed_version`（`status():419-437`）；部署链 `apply_chain.apply_package:190` | 流程完整但**无形式化状态机**；**下架/卸载不在 supervisor**（在 admin 侧） |
| supervisor 职责收敛（不做鉴权/配置/上下文） | 部分实现（越界但可辩护） | 越界：部署编排 + 备份回滚（`apply_chain.py` 整体）、venv 建环境（`venv_mgr.py`）、端口注册表（`supervisor-units.json`）、自身控制 API token（`control_api.py:24,54`） | **未做**上下文存储/平台鉴权/配置中心（符合红线）；但承担了**包管理 + 环境provision**，超出「纯进程管理」 |
| 资源限制：cgroup/K8s resources | 未实现 | 无 | 单机 Windows 场景，见 §3 取舍 |

### 2.6 安全与可观测性

| 文档要求 | 状态 | 实现定位 | 说明 |
|---------|------|----------|------|
| 签名校验（cosign 等） | 未实现 | 无密码学签名 | 仅 sha256 |
| 哈希校验 | 部分实现 | 分发链 sha256：发布 `store_admin.py:315-409`、安装 `plugin_admin.py:262-389`、目录 `store/backend/routes.py:102` | **仅分发/安装期**校验；`plugin_loader` 启动加载**零完整性校验**（`plugin_loader.py:318-364`） |
| SBOM / 漏洞扫描 | 未实现 | 无 | — |
| 权限最小化 | 部分实现 | 权限点 `app.py:183-206`；shell gate 默认拒 `shell_gate.py:35-253`；中继白名单 `relay_whitelist.json` | 有权限点/白名单；但插件持用户会话（见 2.1 鉴权行） |
| 不可信插件强隔离（容器/gVisor/WASM） | 未实现（本场景可判不做） | 无 | 内网可信插件，见 §3 |
| 非 root / 只读根 / cap-drop / seccomp / NetworkPolicy | 未实现（Linux 专属，本场景不适用） | 无 | 目标机为 Windows |
| 子进程加固：独立用户/工作目录隔离/环境清理/文件白名单/IPC 鉴权与超时 | 部分实现 | 工作目录/环境注入 `plugin_executor.py:209-213`；超时即杀 `:238-245`；8MB 体上限 `:252-257`；`CREATE_NO_WINDOW` `:217-219` | 有超时/体上限/env 注入；**无独立低权用户、无文件白名单、stdio 无鉴权** |
| 结构化日志 + 指标 + 追踪 + 审计（OTel/Prometheus） | 部分实现 | 审计 `jz_host/access_log.py:89-119,302-332`（异步队列 + 30 天轮转，tab 分隔文本 ip/user/method/op/path/status/cost_ms/ua）；操作标签 `jz_api.set_operation:191-212`；商店 `stats.jsonl` `store/backend/routes.py:155+` | **非 JSON 结构化**；无 plugin_id/instance_id/request_id/trace_id 字段；**无指标端点/Prometheus/OTel** |

---

## 3. 差距分析

### 3.1 方向性差异（**刻意取舍，建议维持**，非缺陷）

| 文档目标态 | 实现现状 | 为何是合理取舍 |
|-----------|---------|----------------|
| 环境池 + 指纹复用 + LRU + 只读 + 硬链接 | 共享 `pylibs` 注入 + 可选 per-plugin venv（marker 复用） | 内网离线单机、插件数量有限、依赖以 cv2/numpy/openpyxl 等**组件包**为主；指纹池的复杂度（refs/LRU/只读/硬链接）收益低、维护成本高。venv 已明确「**非沙箱，只隔离版本**」（`venv_mgr.py:5-8`） |
| 重依赖服务化（torch/CUDA 共享服务） | 无 | 目标机 8GB、无 GPU 重栈需求；引入共享服务进程反增运维面 |
| 容器 / K8s / Operator / cgroup | 无（子进程 + NSSM 服务） | **离线老 Windows 单机**无容器运行时；K8s 演进路径在本场景**判不做** |
| gVisor/Kata/WASM 强隔离 | 无 | 插件为**受控可信**内部分发（sha256 + 商店发布态），非不可信第三方 |
| seccomp/AppArmor/NetworkPolicy/非 root | 无 | Linux 内核特性，Windows 目标机不适用 |

> 结论：文档 §五.3、§八（容器/内核隔离部分）、§九.3-9.4（K8s/强隔离演进）对本项目**属于「记录在案、明确不做」**，不应转成 TODO。建议在文档或本评估中正式登记该取舍，避免后续审计反复质疑。

### 3.2 真实缺口（**值得补**，转 §5 TODO）

| # | 缺口 | 风险 | 严重度 |
|---|------|------|--------|
| G1 | **Manifest 无 JSON Schema、无字段级校验**（仅「是 dict + id==目录名」`plugin_loader.py:142-144`） | 契约漂移；坏 Manifest 静默加载或运行期才炸；插件作者无权威约束 | 高 |
| G2 | **加载期零完整性校验**（sha256 只在分发/安装期，`plugin_loader` 启动直接 `importlib` 载盘上代码） | 安装后盘上文件被篡改无从发现；信任链在「安装→运行」间断裂 | 高 |
| G3 | **插件持用户完整会话 Cookie**（中继自动附镜像 Cookie `jz_relay.py:48-106`），非受限 token | 单插件被攻破即可冒用用户全部权限；违反文档「插件不持主凭证」 | 高 |
| G4 | **无请求级上下文贯穿**：`request_id` 生成但不入日志/追踪；无 `trace_id`；日志非结构化 | 跨进程（壳→能力层→worker→主应用）问题**无法串联定位**；排障靠猜 | 中高 |
| G5 | **无资源闸门**：服务端 `start_all` 急切全启（`supervisor_core.py:456`）、无 `max_concurrent_plugins`、无内存监控、无低内存降级 | 8GB 目标机若多 worker 插件同启，**内存打满→级联崩溃**（正是文档 §五要防的） | 中高 |
| G6 | **退避为线性非指数**（`supervisor_core.py:402`）、**无告警**（仅日志 + 503） | 崩溃风暴时重试偏密；熔断/下线**运维无感知**，需用户报障 | 中 |
| G7 | **API 版本仅整数比较**，无兼容范围声明（`jz_deps.py:598-621`） | 主机与插件小版本错峰升级时，**兼容判定过粗**（要么全拒要么全放） | 中 |
| G8 | **无显式生命周期状态机**（隐式标志位分散 `supervisor_core.py:419-437`）、下架/卸载不在 supervisor | 状态推导易错；`_applying`/`_circuit`/`enabled` 组合态无单一真源 | 中 |
| G9 | **配置无 schema/实例配置服务**（裸 JSON 热读） | 配置错误运行期才暴露；无法做校验/默认值/迁移 | 低中 |
| G10 | **能力层主文件巨型化**（`capability_app.py` 117KB） | 可维护性/可测性差；核心层「八模块」边界糊在单文件 | 低中 |

### 3.3 潜在风险提示（横切）

- **信任头方案**：客户端向 worker 注入 `X-JZ-User` JSON 头（`jz_api.py:75-95`），代码已注明「**trust-the-header，非防篡改**」（`shell_gate.py:12-14`）。在纯回环 + 单用户桌面场景可接受，但若 worker 端口意外暴露则身份可伪造 → 与 G3 一并纳入鉴权收敛评估。
- **共享运行时热注入**：`rescan_components`（`jz_deps.py:743,791`）允许**同进程运行中**让新装组件可 import。便利（免重启）但意味着**依赖状态是运行时可变的**，与文档「不共享运行时可变模块状态」原则冲突；多版本 cv2/numpy 若被不同插件期望会互相踩踏（当前靠组件包单版本回避）。
- **`--system-site-packages` 基线泄漏**：venv 叠加主包 site-packages（`venv_mgr.py:81`），venv 内可见基线包 → 「特殊版本覆盖」成立，但**隔离不彻底**，与文档「不共享」边界相悖（frozen 主包形态本就不支持 venv，已注明）。

---

## 4. 改进方向（按优先级）

### 高优先级（契约与安全，成本低收益高）
1. **契约先行落地**：定义并强制执行 **Manifest JSON Schema**（G1），把「拒绝加载」从整数比较升级为**字段级校验 + 兼容范围**（G7）。
2. **信任链补全**：加载期做 **sha256 完整性校验**（G2），把安装期已算好的哈希延伸到启动/apply。
3. **鉴权降权**：评估把中继「附用户会话 Cookie」改为**受限 token / 命名空间降权**（G3），至少对非 `own()` 命名空间收窄。

### 中优先级（可观测性与资源治理）
4. **请求上下文贯穿**：统一 `request_id`（+ 可选 `trace_id`）跨 壳→能力层→worker→主应用 传递并**落结构化日志**（G4）。
5. **资源闸门**：服务端引入 `max_concurrent_plugins` + **按需启动/空闲回收**（复用客户端 idle-reap 思路）+ 轻量内存水位监控（G5）。
6. **生命周期治理**：线性退避→**指数退避 + 抖动**，熔断/下线**告警钩子**（G6）；显式**状态机**收敛标志位（G8）。

### 低优先级（结构与工程化）
7. **配置 schema 化**：为插件配置提供 schema + 默认值 + 校验（G9）。
8. **能力层拆分**：把 `capability_app.py` 按核心层八模块边界拆分（G10）。
9. **可观测性增强**：结构化日志 JSON 化，预留指标端点（是否上 Prometheus/OTel 视运维需要，**本场景可暂缓**）。

### 明确不做（登记取舍，勿转 TODO）
- 容器化 / K8s / Operator / cgroup / gVisor/Kata/WASM / seccomp / NetworkPolicy / 重依赖共享服务 / uv 硬链接环境池 —— 理由见 §3.1。

---

## 5. 细化 TODO（可执行任务项）

> 执行纪律（依项目 [plan-driven] 口径）：以下 TODO **应并入 `docs/plan/` 批次卡驱动**，不得为「变绿」删测试/放宽断言/绕门禁；一次只动本卡文件；含主包级改动（`app.py`/`static/`/核心插件源码/随包文档）在**批次收尾切一版**；随包文档改动须**先于组装**。遇「无法整段照做」→ **停该处并写失败报告**，勿自作主张换写法。

### T1 · Manifest JSON Schema 与字段级校验（对应 G1，优先级：高）
- **任务描述**：为服务端 `plugins/*/manifest.json` 与客户端 `client-plugins/*/manifest.json` 定义统一 JSON Schema（覆盖 id/name/version/entry/form/core/requires/features，及客户端 runtime/kind/entry.mount/resident/idle_ttl/timeout_ms），在加载入口做字段级校验，非法 Manifest **拒绝加载并记录原因**（沿用现有 `_plugin_load_errors` 隔离机制）。
- **涉及模块/文件**：新增 `docs/design/插件Manifest-Schema.md` + `config/`（或 `tools/`）下 schema 资源；改 `jz_host/plugin_loader.py:119-149`（`load_manifests` 校验）、`client/local_registry.py:7-31`；参考 `插件设计规范.md`。
- **预期产出**：权威 Schema 文档 + 校验函数 + 单测（合法/缺字段/类型错/id≠目录名 各用例）。
- **依赖**：无（可先行）。为 T2/T7 提供字段基座。
- **验收标准**：① Schema 落档并纳入 `docs/design/`；② 故意放一个缺 `version` 的 Manifest，加载被拒且错误可读；③ 现有全部合法插件仍正常加载（全量自检 + smoke 双绿）；④ 校验**不放宽**既有「id==目录名」断言。

### T2 · 加载期 sha256 完整性校验（对应 G2，优先级：高）
- **任务描述**：把分发/安装期已计算的 sha256（`SHA256SUMS` / catalog `sha256`）延伸为**运行期信任锚**：apply/启动加载插件后端前，对关键文件（`backend/**`、`manifest.json`）做哈希核对，不匹配则拒绝加载 + 告警日志。
- **涉及模块/文件**：`supervisor/apply_chain.py:190`（apply 链，已有 `_read_pkg_meta:95`/`validate_venv_node:53`）、`jz_host/plugin_loader.py:318-364`（`_load_plugin_routes`）、`plugins/admin/backend/plugin_admin.py:262-389`（复用其 sha256 逻辑）。
- **预期产出**：加载期完整性校验 + 篡改检测日志 + 单测（篡改一字节→拒载）。
- **依赖**：T1（哈希清单可写入 Manifest 或旁挂 `SHA256SUMS`）。
- **验收标准**：① 篡改已安装插件的 `routes.py` 一字节，重启/apply 后**拒绝加载**并留痕；② 正常插件加载不受影响、无性能回退（哈希仅在 apply/首载算，非每请求）；③ 不破坏现有回滚链（`apply_chain.py:290`）。

### T3 · 插件鉴权降权评估与收窄（对应 G3，优先级：高）
- **任务描述**：评估中继「自动附用户会话 Cookie」（`jz_relay.py:48-106`）改为**受限 token / 命名空间降权**的可行性；短期至少：对非 `own()` 自命名空间的白名单前缀（`/api/tools`、`/api/org/tree`）做**只读/最小权限**收窄，并明确 worker `X-JZ-User` 头仅回环可信。
- **涉及模块/文件**：`client/jz_relay.py`、`client/relay_whitelist.json`、`client/session_mirror.py`、`jz_host/shell_gate.py:12-14,35-253`、`jz_api.py:75-95`。
- **预期产出**：鉴权降权设计小节（含「做/不做」裁定）+ 若做则最小实现 + 单测。
- **依赖**：无强依赖；建议 T1 后（权限声明可入 Manifest）。**含裁定项**：若判「本场景维持现状」，须登记理由（单用户回环可信）而非留空。
- **验收标准**：① 给出明确裁定与理由；② 若收窄：插件越权访问非白名单/写操作被拒（单测覆盖）；③ 现有插件正常功能不回归。

### T4 · 请求级 request_id/trace_id 贯穿 + 结构化日志（对应 G4，优先级：中高）
- **任务描述**：定义贯穿链路的 `request_id`（可选 `trace_id`）：壳→能力层→worker（stdio 协议已带 `request_id`，`plugin_host.py:7-8`）→主应用反代→worker HTTP，**全链透传并落日志**；审计/访问日志增加 `plugin_id`/`request_id` 字段（先文本增列，JSON 化列为可选）。
- **涉及模块/文件**：`client/plugin_executor.py:228-258`、`client/plugin_host.py`、`supervisor/mainapp_bridge.py:119`（反代注入/透传头）、`jz_host/access_log.py:302-332`（`_log_response` 增列）、`jz_worker.py:102-191`。
- **预期产出**：上下文透传实现 + 日志字段扩展 + 单测（一次请求在各层日志可同 `request_id` 串起）。
- **依赖**：无；与 T6 状态机日志协同更佳。
- **验收标准**：① 触发一次跨进程插件调用，能在 壳/能力层/worker/主应用日志按同一 `request_id` 检索到；② 日志新增字段不破坏既有轮转（`access_log.py:89-119`）与 30 天策略；③ 无每请求性能显著回退。

### T5 · 服务端资源闸门：并发上限 + 按需启动/空闲回收（对应 G5，优先级：中高）
- **任务描述**：为服务端 worker 引入 `max_concurrent_plugins`（超限排队/拒绝并给明确提示）；把客户端已验证的 **idle 回收**（`plugin_executor.py:136-191`）思路移植到 supervisor：worker 空闲超阈值干净收回、下次请求重启；`start_all` 由「急切全启」改为「按 enabled + 按需」策略（保留核心插件随起）。
- **涉及模块/文件**：`supervisor/supervisor_core.py:456`（`start_all`）、`:267/:308`（start/stop）、`supervisor/control_api.py`、配置项落 `supervisor-units.json` / `config/`；参考 `client/plugin_executor.py` idle-reap。
- **预期产出**：并发闸门 + 空闲回收 + 配置项 + 单测（超限拒绝、空闲回收后按需重启）。
- **依赖**：T8（状态机）有助于「回收/重启」态清晰；可独立先行最简版。**注意**：核心插件（admin/store）须**豁免回收**（随壳启动，文档 §一.2）。
- **验收标准**：① 配置 `max_concurrent_plugins=N`，第 N+1 个 worker 启动被拒且提示可读；② 声明 `idle_ttl` 的 worker 空闲后被收回、再次请求静默重启（非崩溃路径）；③ 核心插件不受回收影响；④ 8GB 目标机口径下多插件场景内存不再无界增长（人工/台架验证）。

### T6 · 生命周期治理：指数退避+抖动、告警钩子、显式状态机（对应 G6/G8，优先级：中）
- **任务描述**：① 退避 `min(2*streak,30)`（`supervisor_core.py:402`）改为**指数 + 抖动**（如 `min(base*2^streak + jitter, cap)`）；② 熔断/下线增加**告警钩子**（最低成本：写告警日志 + 主应用可见状态位，供前端/托盘提示；是否接外部通知由运维定）；③ 把分散标志位（`enabled`/`_circuit`/`_applying`/`_stop_requested`/running）收敛为**显式状态枚举 + 单一真源**（`status():419-437` 派生自该枚举）。
- **涉及模块/文件**：`supervisor/supervisor_core.py:342-442`（`_monitor_loop`/`status`/`clear_circuit`）、`supervisor/control_api.py:82-108`、`supervisor/mainapp_bridge.py:141`（503 提示）。
- **预期产出**：退避/告警/状态机实现 + 单测（退避序列、熔断开/关、状态迁移）。
- **依赖**：无；与 T5 共享状态语义。
- **验收标准**：① 连续崩溃时重试间隔按指数增长且封顶、含抖动；② 熔断/下线产生可检索告警记录且前端可感知；③ `status()` 各态与状态机一致，无隐式组合态歧义；④ 既有 `clear-circuit`/`restart` 恢复路径不回退。

### T7 · API 兼容范围协商（对应 G7，优先级：中）
- **任务描述**：把 `requires.api_version` 单整数比较（`jz_deps.py:598-621`）升级为**兼容范围声明**（如 `api_version: {min,max}` 或语义化范围），`evaluate()` 按范围判定 ok/degraded/blocked；`PLUGIN_API=1`（`jz_deps.py:43`）仍为唯一真源，打包/出包写入链不变。
- **涉及模块/文件**：`jz_deps.py:43,452-453,598-621,663-697`；`build-deploy.ps1` / `build-plugin-package.ps1`（写 `api_version` 处，保持兼容）；Manifest（T1 schema 增字段）。
- **预期产出**：范围协商实现 + 向后兼容（旧整数写法仍可解析）+ 单测。
- **依赖**：T1（Schema 承载新字段）。
- **验收标准**：① 声明范围的插件在主机 `PLUGIN_API` 落于范围内→加载，落外→拒绝且原因可读；② **旧插件（整数 api_version）行为不变**（向后兼容）；③ 打包写入 `api_version` 链路不破。

### T8 · 显式生命周期状态机（可并入 T6）（对应 G8，优先级：中）
- **任务描述**：若 T6 未含，单列：定义插件单元状态枚举（`installed→validated→env_ready→registered→starting→health→ready→running→backoff→circuit_open→delisted→stopped→uninstalled`），迁移由单一函数驱动，`apply_chain.apply_package`（`:190`）各步显式置态；**下架/卸载纳入状态机**（当前不在 supervisor）。
- **涉及模块/文件**：`supervisor/supervisor_core.py`、`supervisor/apply_chain.py:190-330`、admin 卸载/下架入口（`plugins/admin/backend/plugin_admin.py`）。
- **预期产出**：状态机实现 + 迁移表 + 单测（含失败→回滚态）。
- **依赖**：T6（退避/熔断态）。
- **验收标准**：① 任一时刻单元状态唯一可查；② apply 失败自动回滚后状态回到 `ready`(旧版) 或 `installed`，无悬挂态；③ 卸载路径状态收敛。

### T9 · 插件配置 Schema 化（对应 G9，优先级：低中）
- **任务描述**：为插件配置提供 schema + 默认值 + 校验（服务端 `config/tools.json`/插件 `config.json` via `jztools_data`、LLM 设置 `jz_host/llm_routes.py:35-93`、客户端 `config.json` `client_config.py:59-134`），保留现有**热读**能力。
- **涉及模块/文件**：`jztools_data.py`、`jz_host/llm_routes.py`、`client/client_config.py`、`config/`。
- **预期产出**：配置 schema + 校验/默认值 + 单测（非法配置被拒/回退默认）。
- **依赖**：T1（Schema 机制可复用）。
- **验收标准**：① 非法配置项加载时给出可读错误或回退默认，不静默错乱；② 热读/免重启行为不回退；③ 现有配置无缝迁移。

### T10 · 能力层主文件拆分（对应 G10，优先级：低）
- **任务描述**：把 `client/capability_app.py`（117KB）按核心层边界拆为 路由/派发、鉴权与会话镜像、配置、注册、日志、中继 等模块，行为**逐字节保持**（纯重构）。
- **涉及模块/文件**：`client/capability_app.py` 及其调用方（`client/client_main.py`、`client/plugin_executor.py`、`client/jz_relay.py`）。
- **预期产出**：拆分后模块 + 全量自检/smoke 双绿 + 客户端功能不回归。
- **依赖**：建议在 T3/T4/T5 稳定后做（避免与功能改动冲突）。**纯重构，不夹带功能变更**。
- **验收标准**：① 客户端全部现有功能（登录/托盘/插件调用/中继/账号切换）人工回归通过；② 无行为变化（diff 仅结构性移动）；③ 单文件规模显著下降、边界清晰。

### TODO 依赖关系总览

```
T1(Schema) ─┬─> T2(完整性)      T1 ─> T7(版本范围)
            ├─> T3(鉴权降权)    T1 ─> T9(配置schema)
            └─> T9
T4(上下文/日志) 独立
T5(资源闸门) ── 协同 ── T6/T8(状态机/退避/告警)
T10(拆分) 建议末位（纯重构，避冲突）
不做项：容器/K8s/gVisor/seccomp/重依赖服务化/uv环境池（§3.1 登记取舍）
```

---

## 6. 附录 · 证据索引（关键定位速查）

| 主题 | 定位 |
|------|------|
| 进程内加载插件后端 | `jz_plugin_host.py:139-164`（`load_backend_module`）、`jz_host/plugin_loader.py:318-364` |
| 共享依赖注入 | `jz_plugin_host.py:27-96`（`setup_dep_components`）、`jz_host/dep_bootstrap.py:33`、`jz_deps.py:711-791`（热重扫） |
| 依赖判定/版本门控 | `jz_deps.py:43`（`PLUGIN_API`）、`:598-621`（`_version_gate_rows`）、`:663-697`（`evaluate`） |
| per-plugin venv | `supervisor/venv_mgr.py:52-98`（`ensure_venv`/`discard_venv`，`--system-site-packages` + `pip --no-index`） |
| 进程管理/健康/退避/熔断 | `supervisor/supervisor_core.py:267/308/342-442`（`start_unit`/`stop_unit`/`_monitor_loop`/`clear_circuit`），退避 `:402`，熔断 `:392-399` |
| 部署/回滚链 | `supervisor/apply_chain.py:190-330`（`apply_package`/`rollback_unit`） |
| 控制 API | `supervisor/control_api.py:82-108`（127.0.0.1:5100，`X-Supervisor-Token`） |
| 反代/桥接 | `app.py:549-558`、`supervisor/mainapp_bridge.py:119-148` |
| worker 引导 | `jz_worker.py:102-191`（`run_worker`，独立 Flask + `/_worker/health`） |
| 客户端壳 | `client/client_main.py:162-180`、`client/shell_window.py:135-290`、`client/single_instance.py:7` |
| 客户端子进程执行 | `client/plugin_executor.py:44-258`（`call`/`_spawn`/`_roundtrip`/idle-reap `:136-191`） |
| 客户端 worker 侧 | `client/plugin_host.py:6-67`（stdio-json-v1 mini-WSGI） |
| 鉴权/会话/中继 | `client/session_mirror.py:16-79`、`client/secure_store.py:20-41`、`client/jz_relay.py:48-106`、`jz_host/shell_gate.py:35-253`、`plugins/admin/backend/routes.py:718-730,1151-1181` |
| 注册表 | `jz_host/plugin_loader.py:86-149,255-315`、`client/local_registry.py:7-31` |
| 日志/审计 | `jz_host/access_log.py:89-119,302-332`、`client/plugin_executor.py:260-269` |
| 分发期 sha256 | `plugins/admin/backend/store_admin.py:315-409`、`plugins/admin/backend/plugin_admin.py:262-389`、`plugins/store/backend/routes.py:102` |
| Manifest 样例 | 服务端 `plugins/admin/manifest.json`、`plugins/info-transfer/manifest.json`；客户端 `client-plugins/local-file-filter/manifest.json` |

> 说明：以上定位采自 2026-10-15 主干快照，行号可能随后续提交漂移；引用时以符号名（类/函数）为准复核。本评估仅落档，未改动任何产品代码。
