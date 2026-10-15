# -*- coding: utf-8 -*-
r"""本地插件包出包工具（批次 8 T-O2-6 + 批次 9 T-O9-1/F4 增强）：桌面端类 B/C 插件 → 商店上架包。

两种输入形态：

1. 直接模式（现状）：源目录即本地插件（manifest.runtime=local、id local- 前缀）——
   类 C 客户端工件（client-plugins/local-<id>/）用这个；
2. 派生模式（--derive-local，Q1 拍板的构建期变换）：源目录为**服务器插件**（plugins/<id>），
   工具派生 staging：manifest 变体（id 加 local- 前缀 / runtime=local / kind=local /
   entry.backend+resident+timeout_ms 按 backend 有无）+ frontend 构建期 API 基址改写——
   B 类单一真源由此保证（服务器版与本地版共用同一份源码）。

公共能力：
    --api-rewrite            构建期 API 基址改写（派生模式隐含开启）：字面量 api/<base> →
                             api/local/local-<base>/api/<base>（幂等，边界断言防误伤同前缀 id）
    L-13 静态检查            禁用模式（动态拼接 / 模板插值基址 / 数组 join / 硬编码来源 /
                             跨插件端点）命中即红；源码引用 api/<base> 但改写计数为 0 即红
                             （规范 §16.10 执行点）；自身全局面 /plugin/<自身id>/api/<端点>
                             按自身命名空间放行（与服务器链 build-plugin-package.ps1 C-13 同口径）
    -FrontendFrom <dir>      frontend 改从指定目录取（C 类客户端工件复用 plugins/<id>/frontend）
    超时缺省（D-2 护栏）      派生模式有 backend 且未显式传 -TimeoutMs 时，按设计缺省写入
                             timeout_ms=600000 并打醒目警告（显式传参仍可覆盖）；
                             漏写会让能力层回落 60000，长任务 60s 即超时
    派生门禁（S-5）           派生模式源插件目录缺 README.md → 硬失败（插件规范必备件）

产出（两种模式一致）：
    deploy\插件包\本地插件\JZClient-本地插件-<id>-v<版本>.zip     （zip 根即插件目录，T18 口径）
    同名 .sha256 旁挂 + 登记片段（id/version/file/sha256/size/requires）

用法：
    python tools/client/build-local-plugin-package.py local-report-suite
    python tools/client/build-local-plugin-package.py plugins/base64 --derive-local
    python tools/client/build-local-plugin-package.py plugins/info-transfer --derive-local -TimeoutMs 600000
    python tools/client/build-local-plugin-package.py client-plugins/local-file-filter -FrontendFrom plugins/file-filter/frontend
退出码：0 成功；1 校验失败。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_FILE = os.path.join(ROOT, "tools", "plugin-payload-rules.json")
# 缺省输出目录与「deploy 四类布局」（2026-10-07 起）一致：deploy\插件包\本地插件\
# （口径：docs/guide/打包部署手册.md「deploy 四类布局」；此前为 deploy\本地插件包 旧布局）。
DEFAULT_OUT = os.path.join(ROOT, "deploy", "插件包", "本地插件")
TEXT_EXT = {".js", ".html", ".css", ".json", ".svg"}

# 派生模式（有 backend 的 worker 本地包）timeout_ms 缺省值：600000 ms。
# 设计真源：docs/design/存量插件三类适配-设计文档.md:118「timeout_ms: 600000 // 视频/编解码长任务放宽」。
# 能力层缺省回落 60000 ms（client/capability_app.py:362 `m.get("timeout_ms") or 60000`）——
# 重出/派生时未显式传 -TimeoutMs 且不写本缺省，长视频/大文件任务的超时上限会静默从 10 分钟跌回
# 60 秒（2026-10-09 D-2 回归即此根因）。显式 -TimeoutMs 仍可覆盖。
DEFAULT_TIMEOUT_MS = 600000

# L-13 白名单框架面（规范 §16.10 第 3 款：/api/<token> 中 token 允许集）
FRAMEWORK_FACES = {"tools", "org", "login", "logout", "session"}

# admin legacy 端点已改名（2026-10-09），白名单已撤：跨插件判定只认自身 api/<id>、
# 自身全局面 /plugin/<自身id>/api/<端点> 与框架面 FRAMEWORK_FACES，不再有按插件 id 的 legacy 放行。

# 外部 URL 语境豁免（与 build-plugin-package.ps1 $l13ExtRe 同口径）：同字面量内含 host/port 形态外部
# 地址（如 http://127.0.0.1:11434/api/chat）时，其中的 /api/<token> 是外部服务地址描述（实例：admin
# 前端 <option> 文案「Ollama 本地（/api/chat）」，其所在模板字面量另含 api.deepseek.com 提示行），
# 不按跨插件调用记罪。依据：规范 §16.10 第 3 款判的是插件间调用（插件端点一律同源 /api/<id>/…）；
# 代码级硬编码来源仍由禁用模式 5（https?://…/api/）独立判红——本豁免只免「跨插件」重复记罪。
# 字面量口径：反引号模板字面量整段（跨行，按相邻反引号配对；文件反引号数为奇数时尾部失配，已知边界）；
# 不在模板字面量内时按本行。行号口径与错误报告一致（均在块注释剥离后的文本上计行）。
EXT_ADDR_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://(?:localhost|(?:\d{1,3}\.){3}\d{1,3}"
    r"|(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{2,})(?::\d{1,5})?")
TEMPLATE_LIT_RE = re.compile(r"`[^`]*`", re.S)


def load_rules():
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            r = json.load(f)
        return set(r.get("exclude_dirs") or []), set(r.get("exclude_files") or []), \
            list(r.get("exclude_globs") or [])
    except Exception:
        return set(), set(), []


def excluded(rel, rules):
    dirs, files, globs = rules
    parts = rel.split("/")
    for p in parts[:-1]:
        if p in dirs:
            return True
    name = parts[-1]
    if name in files:
        return True
    return any(fnmatch.fnmatch(name.lower(), g.lower()) for g in globs)


def die(msg):
    print("  [失败] %s" % msg)
    sys.exit(1)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_frontend_files(frontend_dir):
    for root, _dirs, files in os.walk(frontend_dir):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, frontend_dir).replace(os.sep, "/")
            if excluded("frontend/" + rel, load_rules()):
                continue
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                yield full


def l13_check(frontend_dir, base_id):
    """规范 §16.10 L-13 静态检查。返回 (errors, api_ref_count)。

    与服务器链 tools/build-plugin-package.ps1 C-13 同口径（含 2026-10-09 95c219d 增补的
    「自身全局面 /plugin/<自身id>/api/<端点> 放行」）；禁用模式与跨插件判定不变。
    """
    errors, refs = [], 0
    # 禁用模式：动态拼接 / 模板插值基址 / 数组 join / 硬编码来源
    banned = [
        (r"""['"]/api/['"]\s*\+""", "动态拼接基址（\"/api/\" + …）"),
        (r"""\+\s*['"]/api/['"]""", "动态拼接基址（… + \"/api/\"）"),
        (r"/api/\$\{", "模板插值基址（/api/${…}）"),
        (r"""\[\s*['"]/api""", "数组 join 组装路径"),
        (r"""https?://[^'\"\s`)\\]*/api/""", "硬编码来源（完整 URL）"),
        (r"""location\.origin[^;\n]*['"]/api""", "location.origin 拼接 API 路径"),
    ]
    own = re.compile(r"/api/%s(?![A-Za-z0-9_-])" % re.escape(base_id))
    other = re.compile(r"/api/([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])")
    # 自身全局面：核心插件的 API 直接挂在 /plugin/<自身id>/api/<端点>（store 形态，
    # 桌面端插件商店设计文档 §4.2）。该形态的 token 是 /api/ 后的端点名、自身 id 在其前——
    # 按自身命名空间放行；其他插件 id 仍判跨插件（与 build-plugin-package.ps1 的 $l13OwnGlobal 同款回看比对）。
    own_global = "/plugin/%s" % base_id
    for full in iter_frontend_files(frontend_dir):
        rel = os.path.relpath(full, frontend_dir).replace(os.sep, "/")
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            errors.append("%s: 不可读（%s）" % (rel, exc))
            continue
        # 注释剥离：L-13 管的是代码字面量——文档注释里提历史路径（如解耦迁移说明）不算违规
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        # 外部 URL 语境行集：模板字面量整段含外部地址 → 该段全部行号豁免跨插件判定（见 EXT_ADDR_RE 注释）
        ext_lines = set()
        for lit in TEMPLATE_LIT_RE.finditer(text):
            if EXT_ADDR_RE.search(lit.group(0)):
                start = text.count("\n", 0, lit.start()) + 1
                ext_lines.update(range(start, start + lit.group(0).count("\n") + 1))
        lines = text.splitlines()
        for li, ln in enumerate(lines, 1):
            if ln.lstrip().startswith(("//", "*", "<!--")):
                continue                       # 整行注释不参与判定（块注释已剥）
            for pat, label in banned:
                if re.search(pat, ln):
                    errors.append("%s:%d L-13 禁用模式：%s" % (rel, li, label))
        refs += len(own.findall(text))
        for li, ln in enumerate(lines, 1):
            if ln.lstrip().startswith(("//", "*", "<!--")):
                continue
            for m in other.finditer(ln):
                # 自身全局面 /plugin/<自身id>/api/<端点>：id 在 /api/ 之前，按自身命名空间放行
                if m.start() >= len(own_global) \
                        and ln[m.start() - len(own_global):m.start()] == own_global:
                    continue
                token = m.group(1)
                if token != base_id and token not in FRAMEWORK_FACES \
                        and token != "local/%s" % base_id:
                    # 外部 URL 语境：字面量（模板整段/本行）含 host/port 外部地址 → 地址描述，非跨插件调用
                    if li in ext_lines or EXT_ADDR_RE.search(ln):
                        continue
                    errors.append("%s:%d L-13 跨插件端点：/api/%s（仅允许自身 api/%s 与白名单框架面）"
                                  % (rel, li, token, base_id))
    return errors, refs


def rewrite_api_base(frontend_dir, base_id):
    """构建期基址改写（幂等）：api/<base> → api/local/local-<base>/api/<base>。返回改写次数。"""
    pat = re.compile(r"(api/local/local-%s/)?api/%s(?![A-Za-z0-9_-])"
                     % (re.escape(base_id), re.escape(base_id)))
    count = 0
    for full in iter_frontend_files(frontend_dir):
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        def sub(m):
            nonlocal count
            if m.group(1):          # 已是改写后形态（幂等）
                return m.group(0)
            count += 1
            return "api/local/local-%s/api/%s" % (base_id, base_id)
        new = pat.sub(sub, text)
        if new != text:
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(new)
    return count


def copy_tree(src_dir, dst_dir, rules):
    for root, _dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        dst_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(dst_root, exist_ok=True)
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(root, name), src_dir).replace(os.sep, "/")
            if excluded(rel, rules):
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(dst_root, name))


def build_staging(args, base_id, rules):
    """组装 staging 目录，返回 (staging_path, manifest, 说明列表)。"""
    staging = tempfile.mkdtemp(prefix="bld-local-%s-" % base_id)
    notes = []
    src = os.path.abspath(args.source)
    src_frontend = os.path.abspath(args.frontend_from) if args.frontend_from \
        else os.path.join(src, "frontend")
    if not os.path.isdir(src_frontend):
        die("缺少 frontend 目录：%s" % src_frontend)
    if args.derive_local:
        with open(os.path.join(src, "manifest.json"), encoding="utf-8-sig") as f:
            sm = json.load(f)
        if sm.get("id") != base_id:
            die("派生模式：源 manifest.id 与目录名不一致（%r vs %s）" % (sm.get("id"), base_id))
        has_backend = os.path.isfile(os.path.join(src, "backend", "routes.py"))
        if has_backend and not os.path.isfile(os.path.join(src, "backend", "__init__.py")):
            die("派生模式：backend/ 缺 __init__.py（load_backend_module 硬性要求，无法作为包装载）")
        se = sm.get("entry")
        frontend_entry = se if isinstance(se, str) else ((se or {}).get("frontend") or "index.html")
        entry = {"frontend": frontend_entry}
        if has_backend:
            # pkg_install 六连口径：entry.backend 取**相对 backend/ 的文件名**（末段）
            entry["backend"] = "routes.py"
        manifest = {k: v for k, v in sm.items() if k not in ("id", "runtime", "kind", "entry", "form")}
        manifest["id"] = "local-" + base_id
        manifest["runtime"] = "local"
        manifest["kind"] = "local"          # 规范 §16.1：类 B 一律 kind=local
        manifest["entry"] = entry
        if has_backend:
            manifest["resident"] = True     # 内存任务表+轮询型插件必须常驻（§2.4）
            if args.timeout_ms:
                manifest["timeout_ms"] = args.timeout_ms
            else:
                # D-2 护栏：未显式传参也必须写入设计缺省值——否则 manifest 无 timeout_ms，
                # 能力层回落 60000，长任务 60 s 即超时（回归无提示、只在运行时暴露）。
                manifest["timeout_ms"] = DEFAULT_TIMEOUT_MS
                print("  " + "!" * 72)
                print("  !!! [警告] 未显式传 -TimeoutMs：按设计缺省写入 timeout_ms=%d（毫秒）"
                      % DEFAULT_TIMEOUT_MS)
                print("  !!! [警告] 真源 docs/design/存量插件三类适配-设计文档.md:118；"
                      "缺省时能力层只给 60000（client/capability_app.py:362）")
                print("  !!! [警告] 需要其他上限请显式覆盖，例如 -TimeoutMs 600000")
                print("  " + "!" * 72)
                notes.append("!! timeout_ms 未显式指定 → 按设计缺省 %d 写入（-TimeoutMs 可覆盖）"
                             % DEFAULT_TIMEOUT_MS)
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8", newline="") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        copy_tree(src_frontend, os.path.join(staging, "frontend"), rules)
        if has_backend:
            copy_tree(os.path.join(src, "backend"), os.path.join(staging, "backend"), rules)
        # 插件 README.md 随派生包（上架机器校验按规范 S-5 要求包根 README；此前派生只拷
        # frontend/backend，导致全部派生本地包长期缺件告警）。main() 已保证派生模式源有 README。
        shutil.copy2(os.path.join(src, "README.md"), os.path.join(staging, "README.md"))
        notes.append("派生自服务器插件 %s（backend=%s，resident=%s；README.md 已随包）"
                     % (base_id, "有→worker" if has_backend else "无→纯前端",
                        "true" if has_backend else "n/a"))
    else:
        # 直接模式：整目录拷贝（排除运行态）；-FrontendFrom 时替换 frontend
        copy_tree(src, staging, rules)
        if args.frontend_from:
            fe_dst = os.path.join(staging, "frontend")
            if os.path.isdir(fe_dst):
                shutil.rmtree(fe_dst)
            copy_tree(src_frontend, fe_dst, rules)
            notes.append("frontend 取自 %s" % os.path.abspath(args.frontend_from))
        with open(os.path.join(staging, "manifest.json"), encoding="utf-8-sig") as f:
            manifest = json.load(f)
        _check_frontend_from_sync(args, manifest, notes)
    return staging, manifest, notes


_NIGHTLY_RE = re.compile(r"[ _-]?nightly\b", re.IGNORECASE)
# 版本号 = 数字核（至少两段）+ 可选通道后缀（release / nightly），见《打包部署手册》§2
_VER_RE = re.compile(r"^\d+(\.\d+)+(?:[ _-](?:release|nightly))?$")


def _ver_tuple(v):
    """版本号 → 可比较元组 `(主, 次, 修订, 通道序)`；通道口径见 `tools/_shared.psm1`
    （同数字核则 nightly < release，裸号与 `_release` 属正式通道）。

    非数字段按 0 处理（容忍 1.2.3b 等形态，仅用于门槛比较）。
    """
    s = str(v or "").strip()
    parts = []
    for x in s.split("."):
        digits = ""
        for ch in x:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3] + [0 if _NIGHTLY_RE.search(s) else 1])


def _check_frontend_from_sync(args, manifest, notes):
    """L-12 扩展（P3-1 D7 口径的门禁化）：-FrontendFrom 指向服务器插件 frontend 时，
    本地工件 manifest 版本不得落后于该服务器插件的 manifest 版本——服务器侧
    frontend/backend 任一变化必须同批重生成工件 + 同步递增（否则本地插件用户长期拿旧前端，
    实锤案例 = local-qr-video-decode 停 1.2.1 而服务器已 1.2.2）。工件超前=合法独立演进，仅告警。"""
    ff = os.path.abspath(args.frontend_from) if args.frontend_from else ""
    m = re.search(r"[\\/]+plugins[\\/]+([A-Za-z0-9_-]+)[\\/]+frontend[\\/]*$", ff.replace("/", "\\"))
    if not m:
        return
    srv_id = m.group(1)
    srv_manifest = os.path.join(os.path.dirname(os.path.dirname(ff)), srv_id, "manifest.json")
    if not os.path.isfile(srv_manifest):
        die("-FrontendFrom 指向服务器插件 %s 但缺 %s（L-12 同步校验无从比对）" % (srv_id, srv_manifest))
    with open(srv_manifest, encoding="utf-8-sig") as f:
        sm = json.load(f)
    srv_v = str(sm.get("version") or "")
    local_v = str(manifest.get("version") or "")
    if not srv_v or not local_v:
        die("L-12 同步校验：服务器/工件 manifest 缺 version（%s vs %s）" % (srv_v, local_v))
    if _ver_tuple(local_v) < _ver_tuple(srv_v):
        die("本地工件版本落后于服务器插件（L-12 同步门槛，P3-1 D7 口径）：\n"
            "      服务器 %s = v%s > 本地工件 = v%s\n"
            "      服务器侧 frontend/backend 变化未同步本地工件。\n"
            "      修法：① 跑对应 adapt-* 补丁器重生成 client-plugins/local-%s/（C 类）或\n"
            "            改用 --derive-local 直接派生（B 类）；② 同步递增工件 manifest 的 version。\n"
            "      口径真源：docs/design/同源副本台账.md §2.7"
            % (srv_id, srv_v, local_v, srv_id))
    if _ver_tuple(local_v) > _ver_tuple(srv_v):
        notes.append("!! 本地工件 v%s 超前于服务器插件 %s v%s（合法的独立演进；若属同批改动请核对是否漏递增服务器侧）"
                     % (local_v, srv_id, srv_v))


def main():
    ap = argparse.ArgumentParser(description="本地插件包出包（JZClient-本地插件-*.zip）")
    ap.add_argument("source", help="源目录：本地插件目录（local- 前缀）或服务器插件目录（--derive-local）")
    ap.add_argument("--derive-local", dest="derive_local", action="store_true",
                    help="派生模式：源为服务器插件目录，自动生成 manifest 变体（B 类单一真源）")
    ap.add_argument("--api-rewrite", dest="api_rewrite", action="store_true",
                    help="构建期 API 基址改写（派生模式隐含开启）")
    ap.add_argument("-FrontendFrom", dest="frontend_from", default="",
                    help="frontend 改从该目录取（C 类客户端工件复用服务器插件 frontend）")
    ap.add_argument("-TimeoutMs", dest="timeout_ms", type=int, default=0,
                    help="派生模式 worker 的 timeout_ms（有 backend 时写入 manifest；"
                         "未显式传参按设计缺省 %d 写入并警告）" % DEFAULT_TIMEOUT_MS)
    ap.add_argument("-OutDir", dest="out_dir", default=DEFAULT_OUT)
    ap.add_argument("-Version", dest="version", default="", help="缺省取 manifest.version")
    args = ap.parse_args()

    src = os.path.abspath(args.source)
    base_id = os.path.basename(src.rstrip("\\/"))
    if args.derive_local:
        if not re.match(r"^[A-Za-z0-9_-]+$", base_id):
            die("目录名不是合法插件 id：%r" % base_id)
        if not os.path.isfile(os.path.join(src, "manifest.json")):
            die("派生模式：源目录缺少 manifest.json（须为服务器插件目录）")
        if not os.path.isfile(os.path.join(src, "README.md")):
            die("缺少插件 README.md（规范 S-5 必须）")
        pid = "local-" + base_id
    else:
        if not re.match(r"^local-[A-Za-z0-9_-]+$", base_id):
            die("目录名须为 local- 前缀的插件 id（或改用 --derive-local）：%r" % base_id)
        if not os.path.isfile(os.path.join(src, "manifest.json")):
            die("缺少 manifest.json：%s" % src)
        pid = base_id
        base_id = pid[len("local-"):]
    if args.frontend_from and not os.path.isdir(os.path.abspath(args.frontend_from)):
        die("-FrontendFrom 目录不存在：%s" % args.frontend_from)

    # L-13 静态检查：对进入包内的 frontend 源（改写前）执行——禁用模式命中即红
    check_dir = os.path.abspath(args.frontend_from) if args.frontend_from \
        else os.path.join(src, "frontend")
    errors, api_refs = l13_check(check_dir, base_id)
    if errors:
        print("  L-13 静态检查：%d 处违规" % len(errors))
        for e in errors[:20]:
            print("    [红] %s" % e)
        die("L-13 前端 API 路径纪律检查未通过（规范 §16.10）——修复源码后重出包")

    rules = load_rules()
    staging, manifest, notes = build_staging(args, base_id, rules)
    try:
        rewrite_count = 0
        if args.derive_local or args.api_rewrite:
            rewrite_count = rewrite_api_base(os.path.join(staging, "frontend"), base_id)
            if api_refs and rewrite_count == 0:
                die("源码含 %d 处 api/%s 引用但改写计数为 0（L-13 执行点，规范 §16.10）"
                    % (api_refs, base_id))
        elif api_refs:
            # 2026-10-08 加闸：本地/客户端工件漏传 --api-rewrite 时，前端会继续打中继
            # （=服务器后端），本地化静默失效——此前仅靠计数打印，人工易漏。
            die("前端含 %d 处 api/%s 引用但未启用改写：加 --api-rewrite 重出"
                "（C 类客户端工件必开；规范 §16.10）" % (api_refs, base_id))
        print("  L-13 静态检查：通过（api/%s 引用 %d 处，改写 %d 处）" % (base_id, api_refs, rewrite_count))

        kind = manifest.get("kind", "local")
        if kind not in ("local", "hybrid"):
            die("kind 仅支持 local / hybrid：%r" % kind)
        if manifest.get("runtime") != "local":
            die("manifest.runtime 必须 = local（实为 %r）" % manifest.get("runtime"))
        if manifest.get("id") != pid:
            die("id 不一致：%r ≠ %r" % (manifest.get("id"), pid))
        entry = (manifest.get("entry") or {})
        frontend = entry.get("frontend") or "index.html"
        if not os.path.isfile(os.path.join(staging, "frontend", *frontend.split("/"))):
            die("缺少 frontend/%s" % frontend)
        if kind == "hybrid":
            backend = entry.get("backend")
            mount = (entry.get("mount") or "").rstrip("/")
            if not backend or not os.path.isfile(
                    os.path.join(staging, "backend", backend.split("/")[-1])):
                die("hybrid 插件缺少 backend 入口文件（entry.backend 末段须为 backend/ 下文件名）")
            if not mount.startswith("/api/local/" + pid):
                die("hybrid 的 mount 必须以 /api/local/%s 开头（实为 %r）" % (pid, mount))

        version = args.version or str(manifest.get("version") or "")
        if not _VER_RE.match(version):
            die("版本号不合法：%r（须为 数字核 X.Y.Z，可带 release / nightly 通道后缀；"
                "取 manifest.version 或 -Version 指定）" % version)

        out_dir = os.path.abspath(args.out_dir)
        os.makedirs(out_dir, exist_ok=True)
        zip_name = "JZClient-本地插件-%s-v%s.zip" % (pid, version)
        zip_path = os.path.join(out_dir, zip_name)
        excluded_list, copied = [], 0
        # 可重现构建（V2 验收抓出：两次派生 sha256 须一致）——时间戳统一、顺序确定、
        # 压缩级别固定：zip 内不携带构建时刻/文件 mtime（权限位也归固定值）。
        FIXED_DT = (1980, 1, 1, 0, 0, 0)
        members = []
        for root, _dirs, files in os.walk(staging):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, staging).replace(os.sep, "/")
                if excluded(rel, rules):
                    excluded_list.append(rel)
                    continue
                members.append((rel, full))
        members.sort(key=lambda x: x[0])
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel, full in members:
                zi = zipfile.ZipInfo(rel, date_time=FIXED_DT)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                with open(full, "rb") as f:
                    zf.writestr(zi, f.read())
                copied += 1
        if copied == 0:
            die("没有可打包的文件（排除规则全部命中？）")
        digest = sha256_file(zip_path)
        size = os.path.getsize(zip_path)
        with open(zip_path + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (digest, zip_name))

        print("==> 已产出本地插件包：%s" % zip_path)
        for n in notes:
            print("    %s" % n)
        print("    体积 %.1f KB；sha256 %s" % (size / 1024, digest[:16] + "…"))
        if excluded_list:
            print("    已剔除运行态 %d 项（%s…）" % (len(excluded_list), excluded_list[0]))
        print("    登记片段（粘入上架表单校验用 / 共享盘 index.json）：")
        print("    " + json.dumps({
            "id": pid, "version": version, "file": zip_name,
            "sha256": digest, "size": size,
            "runtime": "local", "requires": manifest.get("requires") or {},
        }, ensure_ascii=False))
        print("    下一步：管理后台「商店管理」上架（runtime=local），审核通过后客户端商店可见。")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
