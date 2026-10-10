#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""C4 同源副本台账漂移检查（2026-10-09，只读）。

真源：`docs/design/同源副本台账.md` §1（校验口径）与 §2（逐条台账 + 基准值）。
逐条覆盖 §2 的全部副本组，按该条声明的口径计算现文件 md5 并与本脚本的基准表比对：
任一 [DRIFT] 即非零退出；全部 [OK] 即退出 0。**本脚本不写任何文件。**

用法：
    python tools\e2e\ledger-drift-check.py               # 检查（默认全量）
    python tools\e2e\ledger-drift-check.py --sync-check  # 校验脚本基准表与台账 §2 是否同步

口径（台账 §1）：
    full  = 原始字节 md5（含行尾）
    norm  = `\r\n` 归一为 `\n` 后的 md5（跨目录同源判定）
    tail3 = 剥掉前 2 行（C 类工件的「同源副本登记」头）再按 norm 算 md5
    —— tail3 只用于带登记头的工件；对无登记头的真源剥前 2 行会切掉真实代码行（§1 警告）。

覆盖与口径按台账各条声明，不做「一刀切比 md5」：
    §2.1 过滤实现（有意分叉）：不比两块真源的 md5；比的是两个 C 类工件与其服务器源的
         逐字节同源关系（客户端 tail3 == 服务器 norm）。四份的 full 仍逐一比对——那是
         台账登记的现况值，变化即提示回写（有意演进属合法，按 §5 更新基准即可）。
    §2.2 bg_image（同步修）：四份归一后应全等（真源 norm / 工件 tail3），另逐份比对 full。
    §2.3 jsqr.min.js（同步换版）：两份 full 与 norm 均相等。
    §2.4 QR 编解码四实现（契约台架）：整体 md5 无意义，断言改为「四文件在库 + 已声明分叉
         （info-transfer 的 PAYLOAD_SAFETY=0.9）仍在」；协议字节布局由
         `tools/e2e/qr-protocol-consistency.py` 台架钉住。
    §2.5 llm_client.py 样板（各自演进）：逐份 full 比对（无跨份相等义务）。
    §2.6 任务/会话样板（成本记录）：不逐份比对，只核对台账登记的 `_viewer()` 计数。

--sync-check 的存在原因：基准值有两处——台账 §2（截断 12 字符，给人看）与本脚本
BASELINE（全量 32 字符，给断言用）。两处失真等于没有台账，故本模式解析台账 §2 表格行做
粗校验（双向）：
    ① 脚本每条基准的 12 字符前缀必须出现在台账对应小节；脚本覆盖的路径也必须出现；
    ② 台账 §2 表格里出现的每条 md5 前缀必须在本脚本基准表里；
    ③ 台账各小节行首 `路径` 行必须被脚本覆盖（新增副本未登记进脚本即报错）。
不改台账为断言源的理由：台账只存 12 字符前缀（约 48 bit，挡不住尾部字节改动），且给人看的
Markdown 表格会被排版编辑（换行 / 加粗）打散。改基准的顺序：先改台账 §2、再改本脚本，跑
`--sync-check` 验证两处一致。
"""
import argparse
import glob
import hashlib
import os
import re
import sys

# 输出流按 UTF-8 走：正文/帮助里有「↔」这类非 GBK 字符，Windows 控制台默认代码页
# 直接 print 会抛 UnicodeEncodeError（--sync-check / --help 命中）——被出包脚本
# （$ErrorActionPreference=Stop）接走时会把一次本该通过的检查误判成红灯。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # 非标准流（被重定向/捕获）时静默跳过
        pass

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER = os.path.join(REPO, "docs", "design", "同源副本台账.md")

# ---------------------------------------------------------------------------
# 基准表（全量 md5；与台账 §2 同步，跑 --sync-check 验证）
# md5  : 单文件 {path, metric(full|norm), base}
# group: 同步组 [(path, metric), ...]，全部应等于 base（并在内部两两相等）
# line: 同源关系 (src, src_metric) → (dst, dst_metric)，两值应等于 base
# structural: 无 md5 的声明项（存在性 / 常量 / 台架指向）
# count: 计数基线（glob + 文本模式）
# ---------------------------------------------------------------------------
ENTRIES = [
    # §2.1 过滤实现：两组真源为有意分叉（台账 §2.1 漂移处置：不比 md5）；C 类工件与其源同源。
    {"sec": "2.1", "kind": "md5", "path": "plugins/file-filter/backend/core.py",
     "metric": "full", "base": "85428560fd387c5627a5d7f03ccea59b"},
    {"sec": "2.1", "kind": "md5", "path": "plugins/trajectory-sketch/backend/filter_local.py",
     "metric": "full", "base": "a8158d734642cfb0034c542b46909621"},
    {"sec": "2.1", "kind": "md5", "path": "client-plugins/local-file-filter/backend/core.py",
     "metric": "full", "base": "62e642476975a8b082c1a30d70dd8de4"},
    {"sec": "2.1", "kind": "md5", "path": "client-plugins/local-trajectory-sketch/backend/filter_local.py",
     "metric": "full", "base": "1d0955f7a3dbb7b4376225f82c021eec"},
    {"sec": "2.1", "kind": "line", "src": "plugins/file-filter/backend/core.py", "src_metric": "norm",
     "dst": "client-plugins/local-file-filter/backend/core.py", "dst_metric": "tail3",
     "base": "85428560fd387c5627a5d7f03ccea59b"},
    {"sec": "2.1", "kind": "line", "src": "plugins/trajectory-sketch/backend/filter_local.py", "src_metric": "norm",
     "dst": "client-plugins/local-trajectory-sketch/backend/filter_local.py", "dst_metric": "tail3",
     "base": "a8158d734642cfb0034c542b46909621"},

    # §2.2 bg_image ×4（同步修）：真源用 norm、带登记头的工件用 tail3，四份归一后全等。
    {"sec": "2.2", "kind": "md5", "path": "plugins/file-filter/backend/bg_image.py",
     "metric": "full", "base": "57aac66b175892ddfc62b56dfec1a412"},
    {"sec": "2.2", "kind": "md5", "path": "plugins/trajectory-sketch/backend/bg_image.py",
     "metric": "full", "base": "57aac66b175892ddfc62b56dfec1a412"},
    {"sec": "2.2", "kind": "md5", "path": "client-plugins/local-file-filter/backend/bg_image.py",
     "metric": "full", "base": "2b13265c0c815abd8cbd1cbd6ad389ae"},
    {"sec": "2.2", "kind": "md5", "path": "client-plugins/local-trajectory-sketch/backend/bg_image.py",
     "metric": "full", "base": "1512a8f016ef8084a9e32660ba7a6388"},
    {"sec": "2.2", "kind": "group", "base": "57aac66b175892ddfc62b56dfec1a412",
     "files": [("plugins/file-filter/backend/bg_image.py", "norm"),
               ("plugins/trajectory-sketch/backend/bg_image.py", "norm"),
               ("client-plugins/local-file-filter/backend/bg_image.py", "tail3"),
               ("client-plugins/local-trajectory-sketch/backend/bg_image.py", "tail3")]},

    # §2.3 jsqr.min.js ×2（vendor，同步换版）：full 与 norm 都应逐字节相同。
    {"sec": "2.3", "kind": "group", "base": "7c3c379dd1277bce95a35014e6f39b65",
     "files": [("plugins/map-marker/frontend/jsqr.min.js", "full"),
               ("plugins/qr-video-decode/frontend/jsqr.min.js", "full")]},
    {"sec": "2.3", "kind": "group", "base": "3a5fd770239e74ae543b36f78d456b2c",
     "files": [("plugins/map-marker/frontend/jsqr.min.js", "norm"),
               ("plugins/qr-video-decode/frontend/jsqr.min.js", "norm")]},

    # §2.4 QR 编解码四实现：整体 md5 永远不同，改断「在库 + 已声明分叉仍成立 + 台架指向」。
    {"sec": "2.4", "kind": "structural",
     "paths": ["plugins/trajectory-convert/backend/routes.py",
               "plugins/info-transfer/backend/routes.py",
               "plugins/map-marker/frontend/generate_qr_stream.py",
               "plugins/qr-video-decode/backend/routes.py"],
     "marker_path": "plugins/info-transfer/backend/routes.py",
     "marker_re": r"^PAYLOAD_SAFETY\s*=\s*0\.9\b",
     "harness": "tools/e2e/qr-protocol-consistency.py"},

    # §2.5 llm_client.py 样板 ×6（各自演进）：无跨份相等义务，逐份 full 与登记值比对。
    {"sec": "2.5", "kind": "md5", "path": "plugins/file-filter/backend/llm_client.py",
     "metric": "full", "base": "7128463c4687e665e3442cd293815d9d"},
    {"sec": "2.5", "kind": "md5", "path": "plugins/trajectory-sketch/backend/llm_client.py",
     "metric": "full", "base": "3e1f7fee41399c41f32e6df7d2abdf01"},
    {"sec": "2.5", "kind": "md5", "path": "plugins/character-graph/backend/llm_client.py",
     "metric": "full", "base": "8f818c8dec3eaf831a2f5f172d10b3e4"},
    {"sec": "2.5", "kind": "md5", "path": "plugins/case-report/backend/llm_client.py",
     "metric": "full", "base": "e346ead9d0d5c32d299d5024bf468859"},
    {"sec": "2.5", "kind": "md5", "path": "client-plugins/local-trajectory-sketch/backend/llm_client.py",
     "metric": "full", "base": "b433047736dac1568fa83efa1eb371f0"},
    {"sec": "2.5", "kind": "md5", "path": "client-plugins/local-file-filter/backend/llm_client.py",
     "metric": "full", "base": "5f7d491749d579df7410ccfdd77a189c"},

    # §2.6 任务/会话样板：不逐份比对，核台账登记的 `def _viewer` 命中数。
    {"sec": "2.6", "kind": "count", "base": 11,
     "globs": ["plugins/*/backend/routes.py", "client-plugins/*/backend/routes.py"],
     "pattern": r"def _viewer"},
]


def digest(path, mode):
    """按口径算 md5：full（原始字节）/ norm（CRLF→LF）/ tail3（剥前 2 行再 norm）。"""
    with open(path, "rb") as f:
        data = f.read()
    if mode == "full":
        return hashlib.md5(data).hexdigest()
    if mode == "norm":
        return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()
    if mode == "tail3":  # tail -n +3 <file> | sed 's/\r$//' | md5sum（台账 §1）
        tail = b"\n".join(data.split(b"\n")[2:])
        return hashlib.md5(tail.replace(b"\r\n", b"\n")).hexdigest()
    raise ValueError("未知口径：%s" % mode)


def entry_paths(e):
    if e["kind"] in ("md5",):
        return [e["path"]]
    if e["kind"] == "group":
        return [p for p, _m in e["files"]]
    if e["kind"] == "line":
        return [e["src"], e["dst"]]
    if e["kind"] == "structural":
        return list(e["paths"])
    return []


def entry_tokens(e):
    """本条目在台账里应出现的 md5 前缀（12 字符）。"""
    if e["kind"] in ("md5", "group", "line"):
        return [e["base"][:12]]
    return []


def label(e):
    if e["kind"] == "md5":
        return "%s  %s" % (e["path"], e["metric"])
    if e["kind"] == "group":
        return "%s×%d 归一后应全等" % (e["base"][:12] + "…", len(e["files"]))
    if e["kind"] == "line":
        return "%s（%s）== %s（%s）" % (e["dst"], e["dst_metric"], e["src"], e["src_metric"])
    if e["kind"] == "structural":
        return "四实现在库 + PAYLOAD_SAFETY=0.9"
    return "_viewer 计数"


def short(h):
    return h[:12] + "…"


def check_entry(e):
    """返回 (ok, 行文本列表)。"""
    root = REPO
    if e["kind"] == "md5":
        p = os.path.join(root, e["path"].replace("/", os.sep))
        if not os.path.exists(p):
            return False, ["[DRIFT] §%s %s  %s  现值 文件不存在 vs 基准 %s"
                           % (e["sec"], e["path"], e["metric"], short(e["base"]))]
        cur = digest(p, e["metric"])
        if cur == e["base"]:
            return True, ["[OK]    §%s %s  %s  现值 %s vs 基准 %s"
                          % (e["sec"], e["path"], e["metric"], short(cur), short(e["base"]))]
        return False, ["[DRIFT] §%s %s  %s  现值 %s vs 基准 %s"
                       % (e["sec"], e["path"], e["metric"], cur, e["base"]),
                       "        现值完整 md5=%s；基准完整 md5=%s" % (cur, e["base"])]

    if e["kind"] == "line":
        sp = os.path.join(root, e["src"].replace("/", os.sep))
        dp = os.path.join(root, e["dst"].replace("/", os.sep))
        if not os.path.exists(sp):
            return False, ["[DRIFT] §%s %s（%s）== %s（%s）  现值 源文件不存在 vs 基准 %s"
                           % (e["sec"], e["dst"], e["dst_metric"], e["src"], e["src_metric"], short(e["base"]))]
        if not os.path.exists(dp):
            return False, ["[DRIFT] §%s %s（%s）== %s（%s）  现值 副本文件不存在 vs 基准 %s"
                           % (e["sec"], e["dst"], e["dst_metric"], e["src"], e["src_metric"], short(e["base"]))]
        cur = digest(dp, e["dst_metric"])
        ref = digest(sp, e["src_metric"])
        tag = "§%s %s（%s）== %s（%s）" % (e["sec"], e["dst"], e["dst_metric"], e["src"], e["src_metric"])
        if cur == e["base"] and ref == e["base"]:
            return True, ["[OK]    %s  现值 %s vs 基准 %s" % (tag, short(cur), short(e["base"]))]
        lines = ["[DRIFT] %s  现值 %s（源 %s） vs 基准 %s" % (tag, cur, ref, e["base"])]
        lines.append("        客户端 tail3=%s / 服务器 norm=%s；基准 full=%s" % (cur, ref, e["base"]))
        return False, lines

    if e["kind"] == "group":
        ok = True
        lines = []
        vals = []
        for p, m in e["files"]:
            ap = os.path.join(root, p.replace("/", os.sep))
            if not os.path.exists(ap):
                ok = False
                lines.append("[DRIFT] §%s %s  %s  现值 文件不存在 vs 基准 %s"
                             % (e["sec"], p, m, short(e["base"])))
                continue
            cur = digest(ap, m)
            vals.append(cur)
            if cur == e["base"]:
                lines.append("[OK]    §%s %s  %s  现值 %s vs 基准 %s"
                             % (e["sec"], p, m, short(cur), short(e["base"])))
            else:
                ok = False
                lines.append("[DRIFT] §%s %s  %s  现值 %s vs 基准 %s"
                             % (e["sec"], p, m, cur, e["base"]))
        if len(set(vals)) > 1:
            ok = False
            lines.append("        [DRIFT] §%s 同步组内部不一致：%s"
                         % (e["sec"], " / ".join(short(v) for v in vals)))
        return ok, lines

    if e["kind"] == "structural":
        ok = True
        lines = []
        missing = [p for p in e["paths"] if not os.path.exists(os.path.join(root, p.replace("/", os.sep)))]
        if missing:
            ok = False
            lines.append("[DRIFT] §%s QR 编解码四实现  现值 缺 %d 份：%s vs 基准 四份在库"
                         % (e["sec"], len(missing), ", ".join(missing)))
        else:
            lines.append("[OK]    §%s QR 编解码四实现  现值 4/4 在库 vs 基准 4/4 在库" % e["sec"])
        mp = os.path.join(root, e["marker_path"].replace("/", os.sep))
        if os.path.exists(mp):
            txt = open(mp, "r", encoding="utf-8", errors="replace").read()
            if re.search(e["marker_re"], txt, re.M):
                lines.append("[OK]    §%s info-transfer 已声明分叉  PAYLOAD_SAFETY=0.9  现值 在 vs 基准 在"
                             % e["sec"])
            else:
                ok = False
                lines.append("[DRIFT] §%s info-transfer 已声明分叉  PAYLOAD_SAFETY=0.9  现值 未命中 vs 基准 在"
                             % e["sec"])
        else:
            ok = False
            lines.append("[DRIFT] §%s info-transfer 已声明分叉  现值 文件不存在 vs 基准 在" % e["sec"])
        lines.append("[--]    §%s 协议面字节布局由 %s 台架钉住（本脚本不比整文件 md5）"
                     % (e["sec"], e["harness"]))
        return ok, lines

    if e["kind"] == "count":
        hits = []
        for g in e["globs"]:
            hits += glob.glob(os.path.join(root, g.replace("/", os.sep)))
        n = 0
        for h in sorted(hits):
            txt = open(h, "r", encoding="utf-8", errors="replace").read()
            n += len(re.findall(e["pattern"], txt))
        if n == e["base"]:
            return True, ["[OK]    §%s _viewer 样板计数  现值 %d vs 基准 %d" % (e["sec"], n, e["base"])]
        return False, ["[DRIFT] §%s _viewer 样板计数  现值 %d vs 基准 %d（副本增减请回写台账 §2.6）"
                       % (e["sec"], n, e["base"])]

    raise ValueError("未知条目类型：%s" % e)


def run_checks():
    print("同源副本台账漂移检查（真源：docs/design/同源副本台账.md；%d 项）" % len(ENTRIES))
    print("仓库根：%s" % REPO)
    print()
    drift = 0
    for e in ENTRIES:
        ok, lines = check_entry(e)
        for ln in lines:
            print("  " + ln)
        if not ok:
            drift += 1
    print()
    if drift:
        print("结果：%d 项 [DRIFT]（共 %d 项）—— 先判是否为真漂移；有意演进按台账 §5 更新 §2 基准，"
              "再跑 --sync-check 对齐两处。" % (drift, len(ENTRIES)))
        return 1
    print("结果：全部 %d 项 [OK]。" % len(ENTRIES))
    return 0


SEC_HEAD = re.compile(r"^### (2\.\d)")
MD5_TOKEN = re.compile(r"([0-9a-f]{12})…")
ROW_PATH = re.compile(r"^\|\s*`([^`]+)`")


def read_ledger_sections():
    with open(LEDGER, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    secs, cur = {}, None
    for line in text.splitlines():
        m = SEC_HEAD.match(line)
        if m:
            cur = m.group(1)
            secs[cur] = []
        elif line.startswith("## "):
            cur = None
        elif cur is not None:
            secs[cur].append(line)
    return {k: "\n".join(v) for k, v in secs.items()}


def run_sync_check():
    try:
        shown = os.path.relpath(LEDGER, REPO)
    except ValueError:  # 台账在别的盘符（如自测指向临时副本）时 relpath 不可算
        shown = LEDGER
    print("基准表 ↔ 台账 §2 同步校验（%s）" % shown)
    if not os.path.exists(LEDGER):
        print("  [ERROR] 台账不存在：%s" % LEDGER)
        return 1
    secs = read_ledger_sections()
    problems = []

    for e in ENTRIES:
        sec = e["sec"]
        if sec not in secs:
            problems.append("台账缺 §%s 小节（结构变了？本脚本的粗校验失效，请人工核对）" % sec)
            continue
        txt = secs[sec]
        for tok in entry_tokens(e):
            if tok not in txt:
                problems.append("§%s：脚本基准 %s… 在台账该小节中找不到 → 两处不同步" % (sec, tok))
        for p in entry_paths(e):
            if p not in txt:
                problems.append("§%s：脚本覆盖的路径 %s 在台账该小节中找不到" % (sec, p))
        if e["kind"] == "structural":
            if "PAYLOAD_SAFETY" not in txt:
                problems.append("§%s：台账未记载已声明分叉 PAYLOAD_SAFETY" % sec)
        if e["kind"] == "count":
            if ("**%d**" % e["base"]) not in txt and ("%d 份" % e["base"]) not in txt:
                problems.append("§%s：脚本计数基线 %d 在台账该小节中找不到" % (sec, e["base"]))

    script_tokens = {t for e in ENTRIES for t in entry_tokens(e)}
    for sec in sorted(secs):
        for tok in MD5_TOKEN.findall(secs[sec]):
            if tok not in script_tokens:
                problems.append("§%s：台账基准 %s… 不在本脚本基准表中 → 脚本未跟随（新增/更新基准请同步）"
                                % (sec, tok))

    covered = {p for e in ENTRIES for p in entry_paths(e)}
    for sec in sorted(secs):
        for p in ROW_PATH.findall(secs[sec]):
            if p not in covered:
                problems.append("§%s：台账表格行 `%s` 未被本脚本覆盖 → 新增副本请同步进基准表" % (sec, p))

    if problems:
        print()
        for p in problems:
            print("  [ERROR] " + p)
        print()
        print("同步校验失败：%d 处对不上。顺序：先改台账 §2，再改本脚本基准表。" % len(problems))
        return 1
    print()
    print("同步校验通过：%d 项基准 / %d 个路径 与台账 §2 各小节双向对齐。"
          % (sum(len(entry_tokens(e)) for e in ENTRIES), len(covered)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="同源副本台账漂移检查（只读）")
    ap.add_argument("--sync-check", action="store_true",
                    help="只做「脚本基准表 ↔ 台账 §2」同步校验，不跑漂移比对")
    args = ap.parse_args()
    if args.sync_check:
        return run_sync_check()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
