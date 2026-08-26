# HANDOFF.md — 战果录入插件交接文档

> 写给一个没有上下文的会话：请先完整读完本文，再动手。
> 最后更新：2026-08-26（分支 `PluginDesign`：首页卡片内容两级读取 + 公告板动态声明 / 编辑功能，
> 提交 `04ca21f` / `e8428ca` / `69bfd06` 已合入本分支，本轮改动与文档更新待提交）

---

## 1. 这个项目是什么

`D:\JZToolsHub` 是一个 **Flask + 配置驱动的插件化工具箱**（"一切皆插件"）：

- 主应用 `app.py` 启动时扫描 `plugins/<插件id>/`，注册插件后端路由到同一 Flask 应用。
- 插件前端放在 `plugins/<id>/frontend/`，通过 `/plugin/<id>/<path>` 静态访问。
- 工具清单由 `config/tools.json` 声明（name/description/category/order）。
- `README.md` 有完整架构说明与 HTTP API 表，改完文档记得同步它。

## 2. 本会话任务

开发 **case-report（战果录入）插件**：输入一段公安「收网情况报告」，抽取五要素
（案件名 / 时间 / 主办大队 / 抓获人数 / 缴获物品），以键值对 JSON 本地存档，
并把「缴获物品」逐项拆分、归类、跨记录汇总，类别支持用户修正并持久化学习。

## 3. 已完成（全部验证通过）

- **解析**：`backend/parser.py`
  - 规则解析五要素（正则）；时间归一化（仅月/日补当年、昨天/前天/今天回推）。
  - 缴获物品逐项拆分 `{category, name, quantity, unit}`：混合单位、中文数字、
    「一批/若干」笼统量、数量在前（50克海洛因）、括号内明细、前缀/连接词剥离。
  - `aggregate_items()` 跨记录汇总：重量→克、货币→元自动换算；类似物品
    （电脑/笔记本/台式机/平板）归同一类。
  - 类别修正已针对用户反馈落地：`冻结资金`（剥离 超/约/近/达/逾 等修饰词）、
    `电话卡`（含手机卡/SIM卡，规则须在「手机」之前）、`平板`→电脑。
- **大模型解析**：`backend/llm_client.py`（OpenAI 兼容接口，默认 DeepSeek）
  - prompt 已精简，并新增 `物品分类: [{名称, 类别}]` 逐项类别输出。
- **类别学习（持久化）**：`backend/category_kb.py`
  - 本地库 `data/item_categories.json`（gitignore），`物品名→类别`。
  - 判定优先级：**用户已学习类别 > 大模型判定 > 本地规则**。
  - `_resolve_item_categories()` 实现该优先级；`_run_parse` 组装时应用。
- **后端路由**：`backend/routes.py`，前缀 `/api/case-report`
  - config GET/POST、parse（后台线程池+task_id轮询）、result、items 拆分预览、
    categories GET/POST/DELETE、aggregate 汇总、records 增删查/列表/下载。
  - `POST /records` 接受 `items?`（用户编辑后的明细），保存时同步学习类别。
  - **已修复脏数据 bug**：`item_categories.json` 会被 `list_records()` 当成记录读入
    → 前端出现"记录 undefined"且删除报 404。修复：新增 `_is_record()`（须含
    `id` + `fields`），`load_record` 对非记录返回 None，列表/读取/删除/下载统一校验。
- **前端**：`frontend/`
  - 报告输入→解析→五要素可编辑表单→「缴获物品明细编辑器」（类别/名称/数量/单位
    可改、可增删行，类别失焦即学习入库）→保存入库。
  - 战果汇总卡片、台账列表（复制/导出/删除）、大模型配置面板。
  - 资源版本号 `?v=4`（改前端必须递增，见"坑"）。
- **文档**：README 补充 case-report 接口表与类别学习说明；`.gitignore` 排除
  `config.json`（密钥）与 `data/`。
- **git**：全部改动已合入 `main` 并提交为 `c891d0e`，**未 push**（用户明确要求）。

### 追加（2026-08-21）：案件名实体对齐 / 同案合并 / 台账改案件名 / 案件筛选侧边栏

- **案件名规整**：`parser.normalize_case_name()` 去掉引号/书名号/空白
  （`"2.11"开设赌场案` ≡ `2.11开设赌场案`）。保存记录与改案件名时都会先规整再落盘。
- **入库同案检测**（前端 `doSave` + 后端 `POST /records` 的 `merge_mode`）：
  - 默认 `auto`：规整后案件名已存在则**不落盘**，返回 `duplicate:true + matches`，
    前端弹窗提示「并入该案件 / 作为新案件保存 / 取消」；
  - `merge`：并入既有案件（`merge_case` 为目标案件名）；`new`：跳过提示直接新增。
- **战果汇总按案件去重**：`parser.aggregate_items(record_items, case_keys?)` 新增可选
  `case_keys`，`/aggregate` 传入规整案件名，「涉及 N 起」= 去重后案件数（同一案件多条记录只计 1 起）；
  不传 `case_keys` 时保持原「按记录计」行为（兼容测试/内部调用）。
- **台账改案件名**：`GET /cases`（既有案件去重列表）+ `PUT /records/<rid>/case` `{case_name}`；
  前端台账卡片新增「修改案件名」按钮，弹窗可选既有案件（自动填入）或输入新案件名，改后汇总即时重算。
- **前端**：新增通用对话框（`openDialog/closeDialog`，点遮罩/Esc 关闭），
  资源版本号已递增为 `?v=5`。
- **案件筛选侧边栏**（追加）：原「案件名」下拉改为战果汇总/台账左侧的**侧边栏**，
  `GET /cases` 按**拼音排序**返回（零依赖，用 GB2312/GBK 一级汉字编码序近似拼音序）；
  点击案件后 `GET /records?case=`、`GET /aggregate?case=` 联动只展示该案件；
  保存/删除/改名后侧边栏自动重建并保持选中（选中案件消失自动回落「全部案件」）。
  下拉选项带 `normalized`（规整案名）供前端比对选中。资源版本号已到 `?v=7`。
- **后端验证**：用 Flask test client 在**临时目录**隔离 data 跑过全流程
  （auto 重复→不落盘、merge 并入、new 新立、PUT 改名、改后 涉及N起 由 2→1、
  引号写法重复检测、空名 400），全部通过；**未触碰真实 data/**。
  案件筛选/拼音排序亦单独验证（3 个拼音序案件 + 数字开头案件排前、
  `?case=` 过滤台账与汇总、引号写法过滤命中）。

### 追加（2026-08-26）：首页卡片两级读取 + 公告板动态声明 / 编辑功能

在分支 `PluginDesign` 上完成，已提交 `04ca21f` / `e8428ca` / `69bfd06`，本轮改动（编辑功能 + 文档更新）待提交。

- **首页插件卡片内容两级读取（app.py）**：
  - 方式一：插件后端提供可选钩子 `home_card()`，`register_plugin_backends()` 启动时登记，
    首页 `/api/tools` 请求时于请求上下文内实时调用（`_resolve_home_card()`），可按当前登录
    用户权限动态声明 name/description/icon/accent/features；
  - 方式二：未提供钩子时回退读取 `config/tools.json`（name/description）+ `manifest.json`
    （icon/accent/features）；
  - 钩子异常不阻断加载，声明字段可部分提供（缺省字段自动回退）。
- **公告板卡片动态声明**（`plugins/notice-board/backend/routes.py::home_card()`）：
  - 按当前用户可见范围取最新一条可读公告，以「时间 + 标题」首行 + 换行 + 内容 声明卡片；
  - 下线原前端 `frontend/js/home-card.js` 客户端覆写（已删除），首页卡片统一由后端声明；
  - `.tool-desc` 增加 `white-space: pre-line` 渲染换行。
- **公告板公告修改功能**（管理员角色/超管）：
  - 新增 `PUT /api/notice-board/announcements/<aid>`：改标题/内容/可见范围，保留创建归属与
    `created_at`，更新 `updated_at`；不可读 404、非管理员 403；
  - 列表条目新增 `editable` 字段，卡片「编辑」按钮复用发布对话框（标题「编辑公告」/按钮「保存」）；
  - 提取 `_parse_ann_body()` 共享发布/修改校验。
- **验证**：Flask test client 全流程通过（发布→列表 editable→PUT 修改→非管理员 403/404、
  created_at 保留、updated_at 更新）；`node --check` 通过。
- **git**：分支 `PluginDesign`，未 push。

## 4. 当前状态 / 卡点

- **git**：`main` 已与 `origin/main` 同步（`c891d0e` + 本轮「同案合并/改案件名/案件筛选侧边栏」改动均已提交并 push，用户已授权）。
- **前端新功能未在真实浏览器走查**：同案合并弹窗 / 改案件名弹窗（含下拉选择既有案件、
  点遮罩与 Esc 关闭）、案件筛选**左侧侧边栏**（拼音排序、高亮选中，联动台账与汇总）——
  本轮靠 node --check 语法校验 + 后端 test client 回归，未在浏览器里点过。
- **没有硬性阻塞**，但有下面几个未充分验证/待办项，别当成已解决：
  1. prompt 精简后，`物品分类` 字段的真实模型输出格式未在真实网络环境下严格验证
     （本地测试用的是规则模式或受保存的 DeepSeek key 影响的 LLM 路径）。
  2. 前端「类别编辑器 / datalist 联想 / 即时学习」未在真实浏览器里完整走查过
     （本轮验证基本靠后端 test client + 静态检查）。
  3. `data/` 目录当前有 `1bbe8e0ecac7.json`（可能是用户新存的一条台账）和
     `item_categories.json`（类别库）。**这些是用户运行时数据，属 gitignore，
     不要删除、不要提交**（曾因此误删用户数据，见"坑"第 1 条）。

## 5. 下一步计划（建议顺序）

1. 启动主应用（`python app.py`）在浏览器真实走查：解析→改类别→保存→再次解析
   验证学习类别生效；重点看 `物品分类` 大模型输出是否符合预期。
2. 用真实报告验证大模型路径的类别判定与 prompt 效果；若 `物品分类` 输出不稳，
   调 `backend/prompt.json` 与 `llm_client.py` 的 `DEFAULT_SYSTEM_PROMPT`（两处同步改）。
3. 视需要补：类别编辑器支持直接从既有类别下拉选择（现在用 datalist 联想，体验可优化）；
   汇总展示时对「冻结资金」等金额类按万元显示（当前 500万→5,000,000元 较绕）。
4. ~~用户确认后可 push 到 origin/main~~（已完成：用户已授权，本日提交并 push）。
5. 若后续新增测试，建议放 `plugins/case-report/backend/tests/`（现无测试文件），
   用 Flask test client 进程内回归。

## 6. 踩过的坑 —— 绝对不要踩

1. **绝不要清理 `backend/data/` 目录**。曾两次在测试脚本里 `os.remove` data 下的
   `.json`，把用户真实台账记录删掉了（无法找回，git 也不含插件 data）。测试要清理时，
   只清理你自己刚创建的文件，或用独立临时目录；动手前先 `git status`/列目录确认。
2. **`config.json` 里有真实 DeepSeek API Key（明文）**。它被 `.gitignore` 排除，
   任何 `git add` 前先 `git check-ignore plugins/case-report/backend/config.json`
   确认；不要把 key 打印/写进文档/提交。`prompt.json` 不是密钥，可以提交。
3. **前端资源改了必须递增 `index.html` 里的版本号**（`style.css?v=N`、`app.js?v=N`），
   否则框架对 `/plugin/` 静态资源缓存一整天的策略会让用户看不到改动。现在版本已到 v4。
4. **`normalize_fields()` 只保留五要素，会丢弃 `物品分类`**。取 LLM 的 `物品分类`
   必须在 `normalize_fields(llm_fields)` **之前**完成（routes.py `_run_parse` 里已修）。
5. **`cr_parse` 中 `api_key = body.get("api_key") or 已保存key`**：请求体传空字符串
   并不会禁用 LLM（`or` 回退到已保存配置）。测试想强制走本地规则，请直接调用
   `routes._build_items(fields, {})` / `_resolve_item_categories` 等内部函数，
   不要依赖传空 key。
6. **`item_categories.json` 与记录文件同目录**，任何按目录扫描 `.json` 的代码都必须
   用 `_is_record()`（含 `id`+`fields`）过滤，否则会把类别库当成"记录"（本会话踩过，
   表现为"记录 undefined" + 删除 404）。以后新增按文件扫描的逻辑同样适用。
7. **单位换算易漏乘系数**：聚合重量/金额时 `sum` 必须乘以各单位的换算系数
   （500克 + 1千克 = 1500克，不是 801）。
8. **数量修饰词会粘进物品名**：`冻结资金超500万` 若不 rstrip「超/约/近/达/逾/高」，
   名称会变成「冻结资金超」。`_parse_single_item` 两个分支都要 rstrip。
9. **中文输出乱码是 PowerShell 管道的显示问题**，数据本身是 UTF-8 正常值。
   断言要写在 Python 代码里（`assert ... , dict`），别靠肉眼读控制台。
10. **bash/read 工具本会话曾间歇性不稳定**（返回空/ChildProcess.kill），write/edit
    更可靠。若工具抽风，重试或改用更小的读取块；同一批并发工具调用不要依赖彼此结果。
11. **LLM 路径的解析在 HTTP 轮询下要数秒**（连接/超时兜底），前端轮询上限 90s，
    属正常；不要在测试轮询里只等 5 秒就断言，否则偶发误报。

## 7. 关键信息速查

- 提交：`c891d0e`（战果录入基础插件）+ 本日功能追加提交；分支现状 `main` 与 `origin/main` 同步。
- 接口前缀：`/api/case-report`（config/parse/result/items/categories/aggregate/**cases**/records，
  records 支持 `GET/POST/DELETE`、`GET <rid>`、`PUT <rid>/case`、`GET <rid>/download`）。
- 类别库文件：`plugins/case-report/backend/data/item_categories.json`（gitignore）。
- 大模型配置：`plugins/case-report/backend/config.json`（gitignore，含真实 key）。
- prompt 两处需同步：`backend/prompt.json` 与 `llm_client.py` 的 `DEFAULT_SYSTEM_PROMPT`。
- 启动主应用：`python app.py`（Windows PowerShell 环境）。
- 快速自测（不进浏览器）：
  ```python
  sys.path.insert(0, r"D:\JZToolsHub")
  import app as m; m.app.config["TESTING"] = True
  m.register_plugin_backends(m.app); c = m.app.test_client()
  # POST /api/case-report/parse {text} → 轮询 result/<task_id>；GET /api/case-report/aggregate
  ```
- **notice-board 插件**：`/api/notice-board/announcements`（GET/POST/PUT/DELETE），
  `home_card()` 钩子在 `plugins/notice-board/backend/routes.py`，首页卡片动态声明按用户权限
  取最新可读公告（时间+标题+换行+内容）；前端资源版本号已到 `v=4`。
