# -*- coding: utf-8 -*-
"""S12 静态门禁扫描器：《插件设计规范》「必须」条款中无门禁的那批（L-8/L-9/L-12/L-14/F-1/F-3/F-4）。

真源：`docs/plan/文档体系整改-TODO清单.md` S12；判据口径对齐现有门禁宿主
（`tools/build-plugin-package.ps1` 的 C-11、`tools/client/build-local-plugin-package.py` 的 L-13）。

**先警告后门禁**：默认 warning 模式一律退出 0；`--gate` 才让未被豁免的命中变成红灯。

用法：
    python tools/e2e/lint-static-rules.py --check-all
    python tools/e2e/lint-static-rules.py --plugin info-transfer
    python tools/e2e/lint-static-rules.py --check-all --json
    python tools/e2e/lint-static-rules.py --selftest        # 每条规则各造一个违规样例，验证能被命中

误报处置：在 `EXEMPTIONS` 表里按 (规则, 路径前缀, 理由) 登记，输出会显示「已豁免 N 项」并把
该条降级为 `[豁免]` 行——**豁免必须有理由**，不接受静默删除。
"""
import argparse
import ast
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# 输出流按 UTF-8 走：豁免理由里有「•」「✅」这类非 GBK 字符，Windows 控制台默认代码页
# 直接 print 会抛 UnicodeEncodeError —— 被出包脚本（$ErrorActionPreference=Stop）接走时
# 会把一次本该通过的门禁误判成红灯。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # 非标准流（被重定向/捕获）时静默跳过
        pass

SERVER_DIRS = ("plugins",)
LOCAL_DIRS = ("client-plugins",)
LOCAL_DERIVED_PREFIX = "local-"          # 派生本地包 = 服务器插件 --derive-local 产出，源在 plugins/

# 框架 API 面（FC-2/FC-3）：插件 import 它们不算跨插件——词表对齐 build-plugin-package.ps1:347
FRAMEWORK_MODULES = {"jz_api", "jz_deps", "jztools_data", "jz_llm", "jz_relay",
                     "jz_tasks", "jz_ui", "jz_plugin_host", "supervisor", "jztools_log"}
# 重原生依赖（规范 §16.11 L-14）：必须懒加载，不得模块级 import
HEAVY_MODULES = {"cv2", "numpy", "zxingcpp"}
PY_EXT = (".py",)
WEB_EXT = (".js", ".html", ".css")

RULES = {
    "L-8":  "禁用 shell=True（规范：命令执行必须走参数列表）",
    "L-9":  "本地包 backend 跨插件 import（规范 B-7 / 出包门禁 C-11 的源码侧前移）",
    "L-12": "本地包版本未递增（对齐出包门禁 C-2：必须高于同 id 已出包最新版）",
    "L-14": "重依赖未懒加载（规范 §16.11：cv2/numpy/zxingcpp 不得模块级 import）",
    "F-1":  "源码里的绝对路径（`[A-Za-z]:\\…` 或 Git-Bash `/d/…`），应走数据根/相对路径",
    "F-3":  "innerHTML 动态拼接未见转义（XSS 面，规范 F-3）",
    "F-4":  "硬编码密钥（`sk-…` 字面量 / `\"api_key\": \"…\"`），凭据必须交 jz_llm（SEC-2/L-10）",
}

# 已核实为误报或既有形态的豁免（rule, 路径, 理由）
# 路径两种形态：
#   - 目录/文件前缀（如 `plugins/admin/frontend/`）：整片命中都降级；
#   - `文件:行`（如 `plugins/store/frontend/store.js:600`）：**只豁免那一行**——
#     逐处分诊（F-3 存量清诊）用它，避免把整个文件的将来命中一起吞掉。
EXEMPTIONS = [
    ("L-14", "plugins/", "test_*.py 与台架夹具不参与出包，模块级 import 重依赖属预期（构建期剔除）"),
    ("F-1", "plugins/admin/frontend/", "占位符文案里的示例路径（`D:\\…`）是给用户看的说明文字，非代码取径"),
    ("F-1", "plugins/knowledge-base/backend/office_render.py", "模块文档串里记录上游项目来源路径（`D:\\TestWorkSpace\\…`），是留痕不是取径"),
    # ---- L2 批 1（2026-10-10 F-3 逐处分诊）：admin 前端 39 处，全部判「安全/误报」----
    # 判据基线：`plugins/admin/frontend/js/admin-common.js:5` 的 `esc()` 真替换 & < > " '，
    # 各页经 `window.AdminCommon.esc` 解构使用；启发式只认 escapeHtml/sanitize/DOMPurify/
    # textContent，不认本地 `esc(` 简写，故「同行已 esc()」成片误报；封装函数已逐一体内核对。
    ("F-3", "plugins/admin/frontend/js/admin-common.js:226", "右值＝openModal 形参 bodyHtml；14 个调用点的模板体对外链数据一律 esc()"),
    ("F-3", "plugins/admin/frontend/js/admin-common.js:389", "右值＝本文件 pickerHtml()：只有 encodeURIComponent(静态 module 名) 与 :281 esc(label)"),
    ("F-3", "plugins/admin/frontend/js/admin-common.js:454", "右值＝previewHtml(p)：label/message/ignored_columns/sheet/notes/模式名+文件名逐条 esc()（:329-349），未 esc 仅数值"),
    ("F-3", "plugins/admin/frontend/js/admin-common.js:475", "右值＝doneHtml(p)：只拼 summaryHtml(p.summary)（纯数字）与静态文案"),
    ("F-3", "plugins/admin/frontend/js/admin-dept.js:16", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-dept.js:25", "departments.map 模板体内 d.id/name/unit_name/description 全部 esc()（:26-33），users_count 为数值"),
    ("F-3", "plugins/admin/frontend/js/admin-llm.js:66", "rows 构造于 :51-64：模式名/problem/url/label/format/model 均 esc()；okClass、keyText 取本文件枚举，user_count 数值"),
    ("F-3", "plugins/admin/frontend/js/admin-llm.js:71", "formats.map 体内 esc(f.id)、esc(f.label)"),
    ("F-3", "plugins/admin/frontend/js/admin-llm.js:135", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:52", "右值＝report() 形参 html 透传；两个调用点 :113 esc(download)、:205 lines（:188-204 时间/备份目录/plugins.map 均 esc）"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:61", "右值＝sectionHtml(s)：sec.id/label/tags/plugin_id 全 esc（:36-40），files/bytes 数值"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:68", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:151", "同行已 esc(data.source)/esc(data.created_at)/esc(data.app_version)，totals 数值"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:154", "右值＝sectionHtml(s)，同 :61 证据"),
    ("F-3", "plugins/admin/frontend/js/admin-migrate.js:162", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:97", "plugins.map 体 :98-107 id/icon/name/code_version/state_version 全 esc；statusChips(:33-45) 静态枚举；depBadges(:49-66) title/name/reason+missing 均 esc；未 esc 仅 backups 计数与 fmtSize"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:237", "parts(:216-232)＋pendHtml：version/path/imports/component/name/分组名/lock_path 全 esc，其余数值"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:240", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:347", "右值＝planHtml(res)，函数体 :245-271 逐字段 esc（id/name/from_version/version/base_source/文件名数组/warnings）"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:372", "errListHtml(:274-278) 对 errors/message 逐条 esc；rejectHint(:280-296) 只返回本文件静态文案"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:423", "head(:416-421) 与 restart_message 全 esc；written/deleted/kept_unknown 数值"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:431", "同 :423（应用结果分支复用 head ＋ esc(res.restart_message)）"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:445", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:649", "右值＝scanHtml(res)，函数体 :592-638 对 path/kind/id/name/version/installed/reason/依赖组件/默认目录全部 esc；cls 取 SCAN_STATUS 枚举"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:654", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:748", "installProgressHtml(:663-726)：p.id/name/version/installed 与 picks[].id 均 esc；note（含后端 error 与文件/占用文案）在 :707 统一 esc(note)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:794", "indexHtml(:546-579，来源＝共享盘 index.json)：id/name/current/blocked_reason/changelog/index_dir 全部 esc"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:799", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:823", "lines(:819-821) 逐条 esc(展示名/id/from/to/error) ＋ :824 esc(res.restart_message)"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:830", "同 :823（失败分支复用 lines ＋ esc(restart_message)）"),
    ("F-3", "plugins/admin/frontend/js/admin-plugins.js:837", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-store.js:99", "同行已 esc(f.name)（本文件 :15 本地 esc）；fmtSize 数值，note 由静态 runtimeLabel 拼"),
    ("F-3", "plugins/admin/frontend/js/admin-unit.js:14", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-unit.js:23", "units.map 体 :24-30 id/name/description 全 esc；departments_count/users_count 数值"),
    ("F-3", "plugins/admin/frontend/js/admin-user.js:37", "同行已 esc(err.message)"),
    ("F-3", "plugins/admin/frontend/js/admin-user.js:201", "deptOptionsHtml(:108-114) 对 d.id/d.name esc，其余为静态 option"),
    ("F-3", "plugins/admin/frontend/js/admin-user.js:348", "roles.map 体 :349-361：opt.name（本文件 MODULE_OPTIONS 常量）/r.id/r.name/r.description 全 esc；usage 数值"),
    ("F-3", "plugins/admin/frontend/js/admin.js:120", "moduleCard(:32-47) 已 esc(m.name)；meta/icon 取本文件 MODULE_META 常量与 encodeURIComponent；m.id 为后端 ADMIN_MODULES 闭集（routes.py:60），count 数值"),
    ("F-3", "plugins/admin/frontend/js/admin.js:125", "同行已 esc(err.message)"),
    # ---- L2 批 2（2026-10-10）：case-report / knowledge-base / shared-docs ----
    # knowledge-base reader.js 的 Excel 渲染原为**真风险**（SheetJS 只转义单元格正文，
    # data-v / data-z / 超链接 href 属属性侧裸拼），已在生成处补 DOMPurify.sanitize；
    # 余下 :506/:533 读的是同一已清洗串，故登记为已修复形态的豁免。
    ("F-3", "plugins/case-report/frontend/app.js:87", "右值＝openDialog 形参 bodyHtml；三个调用点（:320/:1002/:1060）插值全部 esc()（本文件 :37 定义，替换 &<>\"'）"),
    ("F-3", "plugins/case-report/frontend/app.js:246", "orgUnitOptions(:222-229) 内两处 esc(u)；state.orgUnits 源自 /org-units 但已转义"),
    ("F-3", "plugins/case-report/frontend/app.js:345", "同 :246（orgUnitOptions(\"\") 静态骨架＋esc 输出）"),
    ("F-3", "plugins/case-report/frontend/app.js:608", "同行 esc(r.category)；qtyHtml(:589-606) 为 fmtNum 数字或 esc(r.unit)＋静态单位"),
    ("F-3", "plugins/case-report/frontend/app.js:851", "pair[1] 取本文件 FIELD_KEYS(:14) 静态枚举，f[pair[0]] 走 esc()"),
    ("F-3", "plugins/case-report/frontend/app.js:857", '同行 esc(f["抓获人数"])'),
    ("F-3", "plugins/case-report/frontend/app.js:864", '同行 esc(f["涉案价值"])'),
    ("F-3", "plugins/case-report/frontend/app.js:873", "同行 esc(it.category)＋itemQtyHtml(:426-432：esc(it.name)/esc(it.unit)/fmtNum)"),
    ("F-3", "plugins/case-report/frontend/app.js:879", '同行 esc(f["缴获物品"])'),
    ("F-3", "plugins/knowledge-base/frontend/app.js:208", "html＝renderTree/nodeHtml(:182-198) 累加，插值只有 esc(c.id)/esc(c.name)（本文件 :13 定义），depth、n 为数字"),
    ("F-3", "plugins/knowledge-base/frontend/app.js:333", "renderFiles(:301-332) 对 id/ext/name/summary/created_by/fmtSize/fmtTime/download_ext/original_ext 逐一 esc；DL_SVG 与按钮标签静态"),
    ("F-3", "plugins/knowledge-base/frontend/app.js:396", "右值＝openModal 形参 bodyHtml；调用点 :356/:447/:471/:492/:507 与 catOptions(:428-439) 内 esc() 全覆盖"),
    ("F-3", "plugins/knowledge-base/frontend/reader.js:442", "clean＝DOMPurify.sanitize(marked.parse(原文), USE_PROFILES html)（:438-439）"),
    ("F-3", "plugins/knowledge-base/frontend/reader.js:463", "clean＝DOMPurify.sanitize(mammoth 结果)（:460），|| 右侧为字面量"),
    ("F-3", "plugins/knowledge-base/frontend/reader.js:506", "tableHtmls 已在生成处 :500-504 过 DOMPurify.sanitize（L2 批 2 补的清洗，兜住 SheetJS 属性侧裸拼）"),
    ("F-3", "plugins/knowledge-base/frontend/reader.js:533", "同 :506（复制路径读同一已清洗串，且写进游离节点、只取 textContent）"),
    ("F-3", "plugins/knowledge-base/backend/_build_phase8.py:64", "构建期脚本内的 JS 字符串模板（page :42-89）：该行是写进 out/phase8.html 的产物文本，产物侧 clean＝DOMPurify.sanitize(body)（模板 :60）；脚本为本地目检夹具，不参与出包"),
    ("F-3", "plugins/shared-docs/frontend/app.js:419", "html＝renderGrid(:402-418) 累加：colName 由字符码生成、r/c 数字，单元格值这一唯一外部项走 esc(v)（本文件 :41 定义）"),
    # ---- L2 批 3（2026-10-10）：map-marker / notice-board / store / json-formatter /
    #      color-picker / info-transfer / md5-generator / trajectory-convert ----
    ("F-3", "plugins/map-marker/frontend/index.html:743", "右值＝JZIcon.html('▶')＋静态中文标签；JZIcon.html（static/js/jz-icon.js:148-154）只返回原 emoji 或 encodeURIComponent(常量表) 的 <img>"),
    ("F-3", "plugins/map-marker/frontend/index.html:827", "同 :743（入参 '⏸' 为本行字面量）"),
    ("F-3", "plugins/map-marker/frontend/index.html:850", "同 :743（入参 '▶' 为本行字面量）"),
    ("F-3", "plugins/map-marker/frontend/index.html:923", "preview 拼于 :920-922：points.length 数值、COLORS 十六进制常量数组(:249)、escapeHtml(p.label)(:383) 已转义、lng/lat 经 parseCoord(:556-570) parseFloat+isFinite 必为数值"),
    ("F-3", "plugins/notice-board/frontend/app.js:120", "TYPE_ICON 为本文件 :46 emoji 常量枚举，node.type 仅作键、未命中回落「•」，再经 JZIcon.html"),
    ("F-3", "plugins/notice-board/frontend/app.js:298", "同 :120；兄弟节点 :302/:306 走 textContent"),
    ("F-3", "plugins/notice-board/frontend/app.js:350", "nl2br＝先 esc 再把换行转 <br>（:21-24），item.content 已全量转义"),
    ("F-3", "plugins/notice-board/frontend/app.js:395", "同 :350（详情窗复用同一 nl2br）"),
    ("F-3", "plugins/store/frontend/store.js:271", "rows 由详情渲染函数(:236 起)产出：icon/name/version/published_at/id/frontend_file/changelog 与版本历史 v.* 逐个 esc(:29)；requiresText(:124-130) 对 d.name/d.version 各 esc；humanRuntime/fmtSize 为常量与数值"),
    ("F-3", "plugins/store/frontend/store.js:594", "html 为 :592-593 循环拼出的固定骨架串，零外部插值"),
    ("F-3", "plugins/store/frontend/store.js:600", "同行 esc(d.error)"),
    ("F-3", "plugins/store/frontend/store.js:604", "同行 esc(err)（Error 对象先 String() 再转义）"),
    ("F-3", "plugins/json-formatter/frontend/index.html:115", "全静态串＋JZIcon.html('✅')（字面量）；含用户输入的 e.message 分支走 :121 textContent＋appendChild"),
    ("F-3", "plugins/json-formatter/frontend/index.html:134", "同 :115（另一处成功态渲染，错误分支 :139 走 textContent）"),
    ("F-3", "plugins/color-picker/frontend/index.html:124", "items 为 ['HEX', hex.toUpperCase()] 等本行组合：hex 来自 <input type=color>（浏览器规范化 #rrggbb），hexToRgb(:82-85) 有正则白名单，rgbToHsl 返回整数，label 静态"),
    ("F-3", "plugins/md5-generator/frontend/index.html:359", "html 拼于 :338-356，v 只可能是 MD5.md5HexOfBytes→bytesToHex(md5.js:83-87) 的纯 [0-9a-f]；标签为字面量，输入文本/文件内容从不进 HTML"),
    ("F-3", "plugins/info-transfer/frontend/index.html:2071", "items 来自后端 /status 的 missing_deps，值域＝routes.py:3279-3285 _DEP_FEATURES 硬编码常量（包名＋中文功能名）与 :3321-3324 固定文案；manifest hint 链路（_declared）在 routes 内为死代码，不进本行渲染（未 esc 一事已记 §B 加固建议）"),
    ("F-3", "plugins/trajectory-convert/frontend/index.html:691", "同 info-transfer:2071 口径：routes.py:1081-1086 _DEP_FEATURES 字面量常量＋:1110-1126 固定中文文案，无用户/文件数据（未 esc 已记 §B 加固建议）"),
    # ---- L2 批 4（2026-10-10）：trajectory-sketch ----
    ("F-3", "plugins/trajectory-sketch/frontend/app.js:32", "右值＝ICONS[name]，注入前已过本文件 sanitizeIconSvg()（:47-66）：只认根 <svg>、剥 on*/href/xlink:href，含 script/foreignObject 或解析失败整枚弃用并回落 <img>；name 受 ICON_NAMES(:11-12) 白名单约束（C 类按保守修复处置）"),
]

# 结构性豁免（逐条给理由，出现在报告里而不是被悄悄过滤掉）
STRUCTURAL_EXEMPT = [
    ("F-1", "/vendor/", "第三方 vendor 代码不参与本仓纪律（其内部字符串非本仓取径）"),
    ("F-1", "test_", "测试夹具里的绝对路径是样例数据/被测候选路径，非运行期取径"),
]


def norm(p):
    return p.replace(os.sep, "/")


def exempt(rule, rel, line=None):
    rel = norm(rel)
    for r, target, why in EXEMPTIONS:
        if r != rule:
            continue
        if ":" in target:                       # 精确到行
            if line is not None and "%s:%d" % (rel, line) == target:
                return why
        elif rel.startswith(target):
            return why
    base = rel.split("/")[-1]
    for r, token, why in STRUCTURAL_EXEMPT:
        if r == rule and (token in rel or base.startswith(token)):
            return why
    return None


def plugin_ids():
    ids = {}
    for base in ("plugins", "client-plugins"):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            mf = os.path.join(root, name, "manifest.json")
            if os.path.isfile(mf):
                try:
                    with open(mf, encoding="utf-8-sig") as f:      # 单个坏文件不能让扫描器罢工
                        obj = json.load(f)
                    ids[str(obj.get("id") or name)] = norm("%s/%s" % (base, name))
                except Exception:
                    ids[name] = norm("%s/%s" % (base, name))
    return ids


def module_name_of(plugin_id):
    return "jztools_" + re.sub(r"\W", "_", plugin_id)


def iter_targets(which, only_plugin=None):
    """产出 (rel_path, kind)：kind ∈ backend / frontend / local-backend。"""
    for base in ("plugins", "client-plugins"):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if only_plugin and only_plugin not in (name, base + "/" + name):
                continue
            for sub, kind in (("backend", "backend"), ("frontend", "frontend")):
                d = os.path.join(root, name, sub)
                if not os.path.isdir(d):
                    continue
                k = ("local-backend" if base == "client-plugins" and kind == "backend" else kind)
                for dirpath, dirs, files in os.walk(d):
                    dirs[:] = [x for x in dirs if x != "__pycache__"]
                    for fn in sorted(files):
                        if kind == "backend" and not fn.endswith(PY_EXT):
                            continue
                        if kind == "frontend" and not fn.endswith(WEB_EXT):
                            continue
                        full = os.path.join(dirpath, fn)
                        yield norm(os.path.relpath(full, REPO)), k, name


def read_lines(rel):
    try:
        with open(os.path.join(REPO, rel), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def latest_local_package_version(pid):
    """deploy/插件包/本地插件 下同 id 已出包的最新版本号（对齐 C-2 的「与已登记最新版比较」）。"""
    out = os.path.join(REPO, "deploy", "插件包", "本地插件")
    if not os.path.isdir(out):
        return None
    pat = re.compile(r"^JZClient-本地插件-%s-v(\d+(?:\.\d+)*)(?:[^/]*?)\.zip$" % re.escape(pid))
    best = None
    for fn in os.listdir(out):
        m = pat.match(fn)
        if m:
            v = tuple(int(x) for x in m.group(1).split("."))
            best = v if best is None or v > best else best
    return ".".join(str(x) for x in best) if best else None


def semver_lt(a, b):
    pa = [int(x) for x in re.findall(r"\d+", a)] or [0]
    pb = [int(x) for x in re.findall(r"\d+", b)] or [0]
    n = max(len(pa), len(pb))
    return pa + [0] * (n - len(pa)) < pb + [0] * (n - len(pb))


# ---------------------------------------------------------------- 规则实现

def check_L8(rel, lines, ctx):
    out = []
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith("#"):
            continue
        if re.search(r"shell\s*=\s*True", ln):
            out.append((i, "shell=True（应改为参数列表，避免 shell 注入面）"))
    return out


def check_L14(rel, lines, ctx):
    out = []
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return out
    lazy = re.compile(r"=\s*[\"']lazy[\"']")
    has_sentinel = any(lazy.search(l) for l in lines)
    for node in tree.body:                     # 只看模块级（函数内的懒加载是合规形态）
        names = []
        if isinstance(node, ast.Import):
            names = [(a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n in HEAVY_MODULES and not has_sentinel and not os.path.basename(rel).startswith("test_"):
                out.append((node.lineno, "模块级 import %s（规范 §16.11：真值哨兵 + 装载函数）" % n))
    return out


def check_L9(rel, lines, ctx):
    out = []
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return out
    own = ctx.get("own_module")
    others = ctx.get("other_modules", set())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n in FRAMEWORK_MODULES or n in ("flask", "json", "os", "re"):
                continue
            if n in others and n != own:
                out.append((node.lineno, "本地包 import 其他插件后端模块 %s（插件间零依赖 / C-11）" % n))
    return out


def check_F1(rel, lines, ctx):
    out = []
    for i, ln in enumerate(lines, 1):
        if "://" in ln:                        # URL 语境（文档链接、外部服务地址）不算取径
            continue
        if re.search(r"[A-Za-z]:\\\\?[A-Za-z_.]", ln) or re.search(r"[A-Za-z]:\\\\\\\\", ln):
            out.append((i, "源码里的 Windows 绝对路径"))
        elif re.search(r"(?<![\w.])/[dD]/[\w.\-]+/", ln):
            out.append((i, "源码里的 Git-Bash 盘符路径 /d/…"))
    return out


def check_F3(rel, lines, ctx):
    out = []
    esc = re.compile(r"escapeHtml|escape_html|sanitize|DOMPurify|textContent|createTextNode")
    dyn = re.compile(r"\$\{|innerHTML\s*=\s*[^;]*\+\s*\w|innerHTML\s*=\s*\w")
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("<!--"):
            continue
        if "innerHTML" not in ln:
            continue
        if not re.search(r"\.innerHTML\s*=", ln):
            continue
        if dyn.search(ln) and not esc.search(ln):
            out.append((i, "innerHTML 赋值含动态值且同行未见转义（启发式；确认后可豁免）"))
    return out


def check_F4(rel, lines, ctx):
    out = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("#") or s.startswith("//") or s.startswith("*"):
            continue
        if re.search(r"[\"']sk-[A-Za-z0-9]{8,}", ln):
            out.append((i, "硬编码 Key 字面量（sk-…）——凭据必须交框架模块 jz_llm（SEC-2/L-10）"))
        elif re.search(r"[\"']api_key[\"']\s*:\s*[\"'][^\"']+[\"']", ln):
            out.append((i, "配置字面量里写了 api_key 的值（应仅由 jz_llm 管理）"))
    return out


def check_L12(rel, lines, ctx):
    """整包级规则：由 run() 单独汇总（不是逐行）。"""
    return []


CHECKERS = {"L-8": check_L8, "L-9": check_L9, "L-14": check_L14,
            "F-1": check_F1, "F-3": check_F3, "F-4": check_F4}


def build_findings(only_plugin=None):
    ids = plugin_ids()
    other_modules = {module_name_of(p) for p in ids}
    findings = []
    for rel, kind, dirname in iter_targets("all", only_plugin):
        lines = read_lines(rel)
        pid = rel.split("/")[1]
        ctx = {"own_module": module_name_of(pid), "other_modules": other_modules}
        for rule, fn in CHECKERS.items():
            if rule == "L-9" and kind != "local-backend":
                continue
            for ln, msg in fn(rel, lines, ctx):
                findings.append({"rule": rule, "path": rel, "line": ln, "message": msg,
                                 "exempt": exempt(rule, rel, ln)})
    # L-12：本地包版本递增（manifest.version 必须 > 已出包最新版）
    for base in ("client-plugins",):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if only_plugin and only_plugin not in (name, base + "/" + name):
                continue
            mf = os.path.join(root, name, "manifest.json")
            if not os.path.isfile(mf):
                continue
            obj = json.load(open(mf, encoding="utf-8-sig"))
            pid, ver = str(obj.get("id") or name), str(obj.get("version") or "")
            latest = latest_local_package_version(pid)
            if latest and semver_lt(ver, latest):
                findings.append({"rule": "L-12", "path": norm(os.path.relpath(mf, REPO)),
                                 "line": 0, "message": "manifest.version=%s 低于已出包最新 v%s"
                                 % (ver, latest), "exempt": None})
    # 派生本地包（plugins/<id> --derive-local）同口径
    if only_plugin is None:
        for pid, _ in sorted(ids.items()):
            if not pid.startswith(LOCAL_DERIVED_PREFIX) or "/" in pid:
                continue
            src = pid[len(LOCAL_DERIVED_PREFIX):]
            mf = os.path.join(REPO, "plugins", src, "manifest.json")
            if not os.path.isfile(mf):
                continue
            ver = str(json.load(open(mf, encoding="utf-8-sig")).get("version") or "")
            latest = latest_local_package_version(pid)
            if latest and semver_lt(ver, latest):
                findings.append({"rule": "L-12", "path": norm(os.path.relpath(mf, REPO)),
                                 "line": 0, "message": "派生本地包 %s 的源版本 %s 低于已出包 v%s"
                                 % (pid, ver, latest), "exempt": None})
    return findings


def summary(findings):
    rows = []
    for rule in sorted(RULES):
        hits = [f for f in findings if f["rule"] == rule]
        live = [f for f in hits if not f["exempt"]]
        rows.append((rule, len(live), len(hits) - len(live), len(set(f["path"] for f in hits))))
    return rows


# ---------------------------------------------------------------- 自测（造违规样例）

SELFTEST_FIXTURES = {
    "L-8": ("plugins/probe_l8/backend/probe.py", 'import subprocess\nsubprocess.run("dir", shell=True)\n'),
    "L-9": ("client-plugins/local-probe-l9/backend/probe.py", 'import jztools_info_transfer\n'),
    "L-14": ("plugins/probe_l14/backend/probe.py", 'import cv2\n'),
    "F-1": ("plugins/probe_f1/backend/probe.py", 'P = "D:\\\\JZToolsHub\\\\secret.txt"\n'),
    "F-3": ("plugins/probe_f3/frontend/app.js", 'el.innerHTML = "<b>" + userInput + "</b>";\n'),
    "F-4": ("plugins/probe_f4/backend/probe.py", 'KEY = "sk-ABCDEFGHIJKLMNOP"\n'),
}


def selftest():
    global REPO
    tmp = tempfile.mkdtemp(prefix="lint-selftest-")
    fake = os.path.join(tmp, "repo")
    for _rule, (rel, content) in SELFTEST_FIXTURES.items():
        p = os.path.join(fake, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    real = REPO
    REPO = fake
    try:
        # 直接逐条调用检测器，确保每条规则都能命中自己的样例
        bad = []
        by_rule = {}
        for rule, (rel, _c) in SELFTEST_FIXTURES.items():
            by_rule.setdefault(rule, []).append(rel)
        for rule, rels in by_rule.items():
            hits = []
            for rel in rels:
                lines = read_lines(rel)
                fn = CHECKERS[rule]
                hits += fn(rel, lines, {"own_module": "jztools_selfprobe",
                                        "other_modules": {"jztools_info_transfer"}})
            print("  %s 造样例命中：%s" % (rule, "OK（%d 处）" % len(hits) if hits else "**未命中**"))
            if not hits:
                bad.append(rule)
        # L-12：造一个「已出包最新版 > manifest 版本」的场景
        out = os.path.join(fake, "deploy", "插件包", "本地插件")
        os.makedirs(out, exist_ok=True)
        open(os.path.join(out, "JZClient-本地插件-local-selfprobe-v9.9.9.zip"), "w").close()
        mfp = os.path.join(fake, "client-plugins", "local-selfprobe")
        os.makedirs(mfp, exist_ok=True)
        with open(os.path.join(mfp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "local-selfprobe", "version": "1.0.0"}, f)
        got = latest_local_package_version("local-selfprobe")
        ok12 = bool(got) and semver_lt("1.0.0", got)
        print("  L-12 造样例命中：%s（已出包最新=%s manifest=1.0.0）" % ("OK" if ok12 else "**未命中**", got))
        if not ok12:
            bad.append("L-12")
        # 行级豁免（F-3 存量清诊用它逐处登记）：只降级登记的那一行
        probe = "plugins/probe_f3/frontend/app.js"
        EXEMPTIONS.append(("F-3", probe + ":1", "selftest 行级豁免"))
        try:
            hit_line = exempt("F-3", probe, 1) == "selftest 行级豁免"
            hit_other_line = exempt("F-3", probe, 2) is None
            hit_other_file = exempt("F-3", "plugins/probe_f3/frontend/other.js", 1) is None
        finally:
            EXEMPTIONS.pop()
        ok_line = hit_line and hit_other_line and hit_other_file
        print("  行级豁免（文件:行）：%s（登记行降级=%s 同文件他行不降级=%s 他文件不降级=%s）"
              % ("OK" if ok_line else "**不符**", hit_line, hit_other_line, hit_other_file))
        if not ok_line:
            bad.append("EXEMPT")
    finally:
        REPO = real
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    total = 8
    print("\n== 扫描器自测：%d/%d 项通过 ==" % (total - len(bad), total))
    if bad:
        print("未通过项：" + "、".join(bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="S12 静态门禁扫描器（先警告后门禁）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check-all", action="store_true", help="扫描全仓插件源码")
    g.add_argument("--plugin", help="只扫一个插件（目录名或 local-* id）")
    g.add_argument("--selftest", action="store_true", help="每条规则造一个违规样例验证能命中")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--gate", action="store_true", help="门禁模式：未被豁免的命中即红灯（默认关）")
    ap.add_argument("--list-rules", action="store_true", help="打印规则清单")
    a = ap.parse_args()

    if a.list_rules:
        for r, d in sorted(RULES.items()):
            print("%-5s %s" % (r, d))
        return 0
    if a.selftest:
        return selftest()

    findings = build_findings(a.plugin)
    live = [f for f in findings if not f["exempt"]]
    waived = [f for f in findings if f["exempt"]]

    if a.json:
        print(json.dumps({"rules": RULES, "findings": findings,
                          "counts": {r: len([f for f in findings if f["rule"] == r]) for r in RULES},
                          "exemptions": len(waived), "gate": bool(a.gate)},
                         ensure_ascii=False, indent=2))
        return 1 if (a.gate and live) else 0

    scope = "全仓" if a.check_all else "插件 %s" % a.plugin
    print("== S12 静态规则扫描（%s）==" % scope)
    for r, d in sorted(RULES.items()):
        print("  %-5s %s" % (r, d))
    print("")
    for f in sorted(findings, key=lambda x: (x["rule"], x["path"], x["line"])):
        tag = "豁免" if f["exempt"] else "告警"
        print("  [%s] %-5s %s:%s %s" % (tag, f["rule"], f["path"], f["line"], f["message"]))
        if f["exempt"]:
            print("         └ 理由：%s" % f["exempt"])
    print("\n  规则命中汇总：")
    print("  %-5s %-8s %-8s %s" % ("规则", "未豁免", "已豁免", "涉及文件"))
    for rule, l, w, files in summary(findings):
        print("  %-5s %-8d %-8d %d" % (rule, l, w, files))
    print("\n== 扫描完成：告警 %d 项 / 已豁免 %d 项（模式：%s）==" % (
        len(live), len(waived), "门禁" if a.gate else "警告（默认，不阻断）"))
    if a.gate and live:
        print("门禁模式：存在未豁免命中 → 红灯。确认误报请登记 EXEMPTIONS 并写理由。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
