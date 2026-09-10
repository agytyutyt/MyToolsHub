# 文件过滤器（file-filter）

对表格文件进行**脱敏过滤**与**合规性检查**：按字段名过滤列（脱敏），并按管理员配置的文本/正则规则对内容做后处理（合规清洗），输出过滤后文件供下载。

## 功能

- **两种过滤模式**
  - **硬过滤**：只保留与保留字段名单精确匹配的字段（规整后比较：去 BOM/空白、全角转半角、忽略大小写），其余列删除。
  - **大模型过滤**：提取表格全部字段（标题），交由大模型判断与名单字段的语义关联（如「时间」↔「开始时间」，字段名不必完全一致），匹配保留、其余删除。需管理员配置 OpenAI 兼容 API。
- **后处理（文本脱敏，两种模式后都执行）**：按管理员配置的规则对表头与单元格做文本替换，支持正则表达式。如：内容「开始时间」应用规则「开始」→ 空 后变为「时间」。
- **上传限制**：xlsx / xls / csv，≤ 20MB（SEC-3 白名单校验）。
- **输出**：csv 输入输出 csv（UTF-8 BOM，Excel 直接打开不乱码）；xlsx 输出 xlsx；**xls 为只读格式，统一转 xlsx 输出**。

## 页面

- 左侧输入区：虚线方框上传控件（点击/拖拽）+ 过滤模式单选 + 保留字段勾选（可临时停用名单中的字段）。
- 右侧输出区：过滤统计（保留/删除字段对照、替换次数）+ 过滤后文件下载。

## 管理配置（仅管理员/超管）

页面顶部「⚙️ 管理配置」面板：

1. **保留字段名单**：硬过滤的精确匹配基准、大模型过滤的语义匹配基准。
2. **后处理规则**：`查找内容 / 替换为（留空=删除）/ 正则开关 / 启用开关`，多条按顺序执行；非法正则保存时自动跳过。
3. **大模型配置**：API 地址 / API Key / 模型名称（OpenAI 兼容 /chat/completions），支持连通测试。

配置保存在数据根目录 `plugins/file-filter/config.json`（含 API Key，已 gitignore）。

## 接口（前缀 `/api/file-filter`）

| 接口 | 方法 | 说明 | 权限 |
| --- | --- | --- | --- |
| `/status` | GET | 依赖自检（openpyxl/xlrd/requests）+ LLM 是否已配置 | 登录 |
| `/config` | GET | 读配置（api_key 掩码） | 登录 |
| `/config` | POST | 保存配置 | 管理员 |
| `/config/test` | POST | 大模型连通测试 | 管理员 |
| `/filter` | POST | multipart `file` + `mode`(hard/llm) + `columns`（可选 JSON 数组），异步返回 `task_id` | 登录 |
| `/result/<task_id>` | GET | 轮询：`status / kept / removed / replace_count / rows / download`（任务归属校验：创建者或超管） | 创建者/超管 |
| `/download/<task_id>` | GET | 下载过滤后文件 | 创建者/超管 |
| `/apply` | POST | **程序化调用接口（供其他插件）**：JSON `{rows, mode, columns?, post_rules?}` → `{rows, kept, removed, replace_count}`；同步、不落盘 | 登录 |

### 供其他插件调用（规范 B-7 合规方式）

插件间禁止 import 彼此后端模块；复用本插件能力请走 HTTP 接口：

```python
# 其他插件后端（requests，需携带用户会话 Cookie）或插件前端 fetch
resp = requests.post(
    base_url + "/api/file-filter/apply",
    json={
        "rows": [["姓名", "开始时间", "备注"], ["张三", "2026-01-01 10:00", "x"]],
        "mode": "hard",            # 或 "llm"
        "columns": ["时间"],       # 缺省用管理员配置的保留字段名单
        # "post_rules": [{"pattern": "开始", "replacement": "", "is_regex": False}]  # 缺省用管理员配置
    },
    cookies=request.cookies,       # 透传当前请求会话
)
data = resp.json()  # {"rows": [[表头],[数据]...], "kept": [...], "removed": [...], "replace_count": N}
```

## 依赖

`backend/requirements.txt`：openpyxl（xlsx 读写）、xlrd（xls 读取）、requests（大模型调用）。
缺依赖时 `/status` 上报，相关功能优雅降级提示，不拖垮主进程（B-4）。

## 依赖检查

```bash
pip install -r plugins/file-filter/backend/requirements.txt
```

设计文档：`docs/过滤器插件-设计文档.md`
