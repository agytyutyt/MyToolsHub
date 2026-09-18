# 知识库插件 Word 预览三问题排查报告

> **状态：已修复（2026-09-18）**。修复实现见 `plugins/knowledge-base/backend/office_render.py`
> 的 `_polish_word_html()` 及其子补丁函数；回归断言见
> `plugins/knowledge-base/backend/test_routes_preview.py` 第 0b 节（6 项）。
> vendor 保持零改动（B-7 约定）。

## 1. 结论速览

| 问题 | 根因定位 | 确定性 | 状态 |
|------|----------|--------|------|
| 仿宋_GB2312 笔画"竖"过细 | 目标字体缺失 + fallback 链跳过同类衬线体直落黑体 | 高 | 已修（补链） |
| 部分文字加粗（原文未加粗） | `仿宋_GB2312` 缺失时回退 `Microsoft YaHei`，墨量约为仿宋的 2.56 倍 | 高 | 已修（补链） |
| 单元格文字"穿模" | `<tr>` 上的 `overflow:hidden` 对 table-row 无效；`<p>` 默认 margin 未重置 | 高 | 已修（CSS + 属性清理） |

涉及链路：后端 `office_render.py` → `vendor/dhr/renderer/css.py` → 前端 `reader.js`（仅做注入）。**不改动 vendor，在 `office_render.py` 做 HTML 后处理修复**。

---

## 2. 复现实验

构造两个样例文档（路径）：

- `C:\Users\yfjz\AppData\Local\Temp\kb_fs_sample.docx`：仿宋_GB2312 三号正文 + 精确行高表格。
- `C:\Users\yfjz\AppData\Local\Temp\kb_cell_sample.docx`：含长英文/数字串与精确行高表格，用于验证穿模。

渲染命令（直接调用 dhr，无需启动服务）：

```python
import sys; sys.path.insert(0, r"D:\JZToolsHub\plugins\knowledge-base\backend\vendor")
import dhr
html = dhr.convert(open(r"...\kb_fs_sample.docx", "rb").read(),
                   dhr.ConvertOptions(mode="flow", output="fragment",
                                      css_prefix="kbdoc", media_mode="base64")).html
```

---

## 3. 问题 1 & 2：字体/加粗

### 3.1 代码事实

`vendor/dhr/renderer/css.py` 第 14 行：

```python
CJK_FALLBACK = '"Microsoft YaHei","微软雅黑","PingFang SC","Hiragino Sans GB","Source Han Sans CN","Noto Sans CJK SC",sans-serif'
```

`font_family_of()` 输出示例（实测）：

```css
.kbdoc-c1{font-family:"仿宋_GB2312","Times New Roman","Microsoft YaHei","微软雅黑",...,sans-serif;...}
```

### 3.2 客户端字体探测结果

用 Canvas 像素签名法（`kb_probe2.py` + agent-browser）在本机测得：

| 字体 | alpha 和（墨量） | 是否存在 |
|------|-----------------|----------|
| 仿宋_GB2312 | 133,422 | **缺失**（等于缺失基线） |
| FangSong（仿宋） | 53,860 | 存在 |
| Microsoft YaHei | 138,131 | 存在 |
| SimSun | 71,580 | 存在 |
| Times New Roman | — | 存在 |

### 3.3 根因

1. **目标字体缺失**：多数客户端（含本开发机）没有安装 `仿宋_GB2312`。
2. **fallback 链过跳**：缺失后直接命中 `Microsoft YaHei`（黑体），墨量是 仿宋_GB2312 的约 **2.56 倍**，肉眼观感就是"加粗"。
3. **混排反差**：数字/英文走 `Times New Roman`（细），中文走 `Microsoft YaHei`（粗），同一段落粗细不均 → "部分文字加粗"；若客户端恰有 仿宋_GB2312，则其 ASCII 字形/TNR 又可能显"竖细"。

---

## 4. 问题 3：穿模

### 4.1 代码事实

`vendor/dhr/renderer/__init__.py` 第 374-380 行：

```python
def _row_html(row, ctx: _Ctx) -> str:
    style = ""
    if row.height_px:
        if row.height_rule == "exact":
            style = f' style="height:{row.height_px:.0f}px;overflow:hidden"'
```

CSS base 规则（第 446-447 行）：

```css
.kbdoc-t{border-collapse:collapse;margin:8px 0}
.kbdoc-t td,.kbdoc-t th{border:1px solid #666;padding:2px 8px;vertical-align:top;text-align:left}
```

### 4.2 实测

设置 `w:trHeight w:val="400" w:hRule="exact"`（20pt ≈ 27px）后：

| 行 | 设置高度 | 实测 tr 高度 | 内容 p 高度 | 说明 |
|----|----------|-------------|------------|------|
| row0 | 27px | **49px** | 18px | 多出 27px（≈ 默认 p margin） |
| row1 | 27px | **67px** | 37px | 行高被内容撑开，约束失效 |

**原因**：
- `overflow:hidden` 对 `display:table-row` 的 `<tr>` 不生效；浏览器把它当成 `height`（最小高度）。
- dhr base CSS 没有 `p{margin:0}`，浏览器默认 `p{margin:1em 0}` 额外撑高单元格。
- `.kbdoc-t td` 缺少 `word-break:break-all; overflow-wrap:anywhere`，极端不可断行内容可横向撑出。

---

## 5. 已实施的修复（`office_render.py` 后处理层）

对 `render_word()` 的产物调用 `_polish_word_html()`，由三个子步骤组成：

### 5.1 字体回退链增强 —— `_patch_font_fallback()`

在 `"仿宋_GB2312"` 与 `"Microsoft YaHei"` 之间插入仿宋族真实可用名：

```python
_FANGSONG_FALLBACK = ('FangSong', '仿宋', 'SimSun')
```

修复后实测链：

```
"仿宋_GB2312","Times New Roman","FangSong","仿宋","SimSun","Microsoft YaHei",...
```

覆盖 6 种常见写法（`仿宋_GB2312` / `仿宋-GB2312` / `仿宋GB2312` / `FangSong_GB2312` /
`FangSong-GB2312` / `仿宋_GB2312_CN`）与裸 `仿宋`。**幂等**：目标字体到雅黑之间已含任一
仿宋族别名即跳过（否则裸 `仿宋` 会自我匹配导致反复膨胀）。

### 5.2 表格穿模修正 —— `_patch_table_overflow()` + CSS 补丁

- 清除 `<tr>` 上对 table-row 无效的 `overflow:hidden`（`height` 与标签数保持不变）；
- 追加 CSS：

```css
.kbdoc p{margin:0}                    /* ① 归零默认 margin，修正单元格行距 */
.kbdoc-t td,.kbdoc-t th{word-break:break-all;overflow-wrap:anywhere;word-wrap:break-word}  /* ② 可断行 */
.kbdoc-t td>p:only-child{overflow:hidden}  /* ③ 裁剪语义交由内层 p 承担 */
```

补丁带标记 `/*kbdoc-patch*/`，同产物重复处理不再注入。

### 5.3 实测效果（浏览器验证）

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 实际渲染字体 | 微软雅黑（黑体） | **仿宋**（`FangSong` 命中，墨量 53860 vs 雅黑 138131） |
| 正文字重 | 视觉加粗 | `font-weight:400`，无一处加粗 |
| 精确行高表格行高 | 49 / 67 / 49 px（被 `<p>` margin 撑开） | **33 / 51 / 33 px** |

---

## 6. 复现实物

- 样例 docx：`C:\Users\yfjz\AppData\Local\Temp\kb_fs_sample.docx`、`kb_cell_sample.docx`
- 修复前产物：`kb_fs_preview.html`、`kb_cell_preview.html`
- 修复后产物：`kb_patched_kb_fs_sample.html`、`kb_patched_kb_cell_sample.html`
- 截图：`kb_cell_shot.png`（修复前）、`kb_patched_cell_shot.png`、`kb_patched_fs_shot2.png`（修复后）
- 回归断言：`plugins/knowledge-base/backend/test_routes_preview.py` 第 0b 节
