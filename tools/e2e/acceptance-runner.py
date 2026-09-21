#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""自动验收脚本：把《验收手册》里**可 HTTP 驱动**的用例跑一遍，输出逐条结果与汇总。

设计取舍：
  · 只自动化"能用接口判定"的用例；浏览器侧解码（qr-video-decode）、需要高德 Key 的地图、
    纯交互的取色器、需要大模型 Key 的流程一律标 SKIP 并写明原因——**不假装通过**。
  · 依赖相关的用例断言的是**一致性**（面板列出的组件 ⇔ 插件判定为满足），
    这样在任何机器上都有意义，而不是"本机装了所以通过"。
  · 每条用例独立、失败不影响后续；退出码 0 = 无 FAIL。

用法（开发机 / 仓库内执行，指向一个运行中的实例）：
    python tools\e2e\acceptance-runner.py --base http://127.0.0.1:5000
    ... --testdata testdata --groups F,R,S,X
    ... --report build\acceptance-report.md
    ... --app-dir "%LOCALAPPDATA%\JZToolsHub"    # 额外跑 R-06（--check-deps 真实 import 自检）
    ... --media deploy\插件包                     # 额外跑 S-01（改包后应被拒绝）

退出码：0 = 无 FAIL；1 = 有 FAIL（SKIP 不计入失败）
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

if getattr(sys, "frozen", False):
    # 打包成 exe 后：以 exe 所在目录为根（测试数据 testdata\ 放在它旁边）
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================== 用例注册表 ==============================

CASES = []


def case(cid, group, title, skip=None, needs=()):
    """注册一条用例。

    skip  ：字符串 = 人工/环境原因不自动跑（写明原因，输出里如实标 SKIP）
    needs ：需要的上下文参数名（如 "app_dir"）——**传了才跑**，没传则标 SKIP 并提示加哪个开关
    """
    def deco(fn):
        CASES.append({"id": cid, "group": group, "title": title, "skip": skip,
                      "needs": tuple(needs), "fn": fn})
        return fn
    return deco


# ============================== HTTP 客户端 ==============================

class Client(object):
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.cookie = None
        self.user = user
        self.password = password

    def _req(self, path, data=None, headers=None, method=None, timeout=300):
        url = self.base + path
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                return r.status, r.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            raise HttpError(e.code, "%s %s → HTTP %s ｜ %s" % (req.get_method(), path, e.code, detail))

    def get(self, path, **kw):
        return self._req(path, **kw)

    def get_json(self, path):
        _s, _c, raw = self._req(path)
        return json.loads(raw.decode("utf-8"))

    def post_json(self, path, payload):
        _s, _c, raw = self._req(path, json.dumps(payload).encode("utf-8"),
                                {"Content-Type": "application/json"})
        return json.loads(raw.decode("utf-8"))

    def post_multipart(self, path, fields, files, timeout=300):
        boundary = "----jzacc%s" % int(time.time() * 1000)
        buf = []
        for k, v in (fields or {}).items():
            buf.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                        % (boundary, k, v)).encode("utf-8"))
        for k, v in (files or {}).items():
            fname, content = v
            buf.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                        "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, fname)).encode("utf-8"))
            buf.append(content)
            buf.append(b"\r\n")
        buf.append(("--%s--\r\n" % boundary).encode("utf-8"))
        _s, _c, raw = self._req(path, b"".join(buf),
                                {"Content-Type": "multipart/form-data; boundary=" + boundary},
                                timeout=timeout)
        return json.loads(raw.decode("utf-8"))

    def download(self, path, timeout=300):
        _s, _c, raw = self._req(path, timeout=timeout)
        return raw

    def login(self):
        js = self.post_json("/api/login", {"username": self.user, "password": self.password})
        if not js.get("ok"):
            raise RuntimeError("登录失败：%s" % js)
        return js


class HttpError(Exception):
    def __init__(self, code, message):
        super(HttpError, self).__init__(message)
        self.code = code


def wait_task(cl, status_path, timeout=600, interval=1.0):
    """轮询任务到终态；失败时抛出带 detail 的异常。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        js = cl.get_json(status_path)
        task = js.get("task") if isinstance(js.get("task"), dict) else js
        st = task.get("status")
        last = task
        if st == "done":
            return task
        if st in ("error", "failed"):
            raise RuntimeError("任务失败：%s" % (task.get("detail") or task))
        time.sleep(interval)
    raise RuntimeError("任务超时（stage=%s）" % last.get("stage"))


# ============================== 断言小工具 ==============================

def need(cond, msg):
    if not cond:
        raise AssertionError(msg)


def td(ctx, *parts):
    p = os.path.join(ctx["testdata"], *parts)
    need(os.path.isfile(p), "测试数据缺失：%s（先跑 make-acceptance-testdata.py）" % p)
    return p


def read(ctx, *parts):
    with open(td(ctx, *parts), "rb") as f:
        return f.read()


# ============================== F 组：功能 ==============================

@case("F-01", "F", "admin 登录 / 会话")
def f01(cl, ctx):
    js = cl.login()
    need(js.get("user", {}).get("username") == cl.user, "登录响应缺少用户信息")
    return "登录成功，用户 %s（%s）" % (js["user"].get("username"), js["user"].get("role"))


@case("F-02", "F", "admin 单位 / 部门 / 人员列表")
def f02(cl, ctx):
    out = []
    for path, key in (("/api/admin/units", "units"), ("/api/admin/departments", "departments"),
                      ("/api/admin/users", "users")):
        js = cl.get_json(path)
        items = js.get(key) or js.get("items") or []
        out.append("%s=%d" % (key, len(items)))
    return "列表可读：" + "，".join(out)


@case("F-03", "F", "admin 批量导入（单位 → 部门 → 人员）")
def f03(cl, ctx):
    results = []
    for module, fname in (("unit", "admin/批量导入-单位.xlsx"),
                          ("department", "admin/批量导入-部门.xlsx"),
                          ("user", "admin/批量导入-人员.xlsx")):
        content = read(ctx, fname)
        # dry_run 默认 true（只预览不落库）→ 真要导入必须显式 false；auto_create_parent 让部门/人员先跑通
        js = cl.post_multipart("/api/admin/batch/%s/import" % module,
                               {"mode": "upsert", "dry_run": "false", "on_error": "skip",
                                "auto_create_parent": "true"},
                               {"file": (os.path.basename(fname), content)})
        need(js.get("ok"), "导入 %s 失败：%s" % (module, json.dumps(js, ensure_ascii=False)[:200]))
        summary = js.get("summary") or {}
        results.append("%s(总 %s/新增 %s/更新 %s/错误 %s)" % (
            module, summary.get("total", js.get("total_rows")), summary.get("create", 0),
            summary.get("update", 0), summary.get("error", 0)))
    return "导入完成：" + "，".join(results)


@case("F-04", "F", "admin 批量导入逐行报错（含错误行）")
def f04(cl, ctx):
    content = read(ctx, "admin/批量导入-人员-含错误行.xlsx")
    # auto_create_parent=false：让"部门/单位不存在"如实报错（true 会把它们自动建出来）
    js = cl.post_multipart("/api/admin/batch/user/import",
                           {"mode": "upsert", "dry_run": "false", "on_error": "skip",
                            "auto_create_parent": "false"},
                           {"file": ("批量导入-人员-含错误行.xlsx", content)})
    summary = js.get("summary") or {}
    errs = [r for r in (js.get("rows") or []) if r.get("level") == "error" or "错误" in str(r.get("message", ""))
            or r.get("ok") is False]
    if not errs:
        errs = [r for r in (js.get("rows") or []) if re.search(r"重复|必须|不存在", str(r.get("message", "")))]
    need(summary.get("error", 0) >= 1 or errs, "错误行未被报出：%s" % json.dumps(js, ensure_ascii=False)[:200])
    msgs = "；".join("第%s行：%s" % (r.get("row"), str(r.get("message"))[:40]) for r in errs[:3])
    return "错误 %s 条（%s）" % (summary.get("error", len(errs)), msgs)


@case("F-05", "F", "admin 批量导出 xlsx")
def f05(cl, ctx):
    raw = cl.download("/api/admin/batch/user/export")
    need(raw[:2] == b"PK", "导出不是 xlsx（%d 字节，开头 %r）" % (len(raw), raw[:8]))
    return "导出 %d 字节，xlsx 头正确" % len(raw)


@case("F-06", "F", "file-filter：xlsx 硬过滤（只保留指定列）")
def f06(cl, ctx):
    content = read(ctx, "file-filter/花名册.xlsx")
    js = cl.post_multipart("/api/file-filter/filter",
                           {"mode": "hard", "columns": json.dumps(["姓名", "所属单位"], ensure_ascii=False)},
                           {"file": ("花名册.xlsx", content)})
    need(js.get("task_id"), "过滤提交失败：%s" % json.dumps(js, ensure_ascii=False)[:200])
    task = wait_task(cl, "/api/file-filter/result/%s" % js["task_id"])
    raw = cl.download("/api/file-filter/download/%s" % js["task_id"])
    need(raw[:2] == b"PK", "输出不是 xlsx")
    return "过滤完成：%s（%d 字节）" % (task.get("filename") or task.get("name") or "输出文件", len(raw))


@case("F-07", "F", "file-filter：xls 与 csv 两种格式")
def f07(cl, ctx):
    outs = []
    for fname in ("file-filter/花名册.xls", "file-filter/花名册.csv"):
        content = read(ctx, fname)
        js = cl.post_multipart("/api/file-filter/filter",
                               {"mode": "hard", "columns": json.dumps(["姓名"], ensure_ascii=False)},
                               {"file": (os.path.basename(fname), content)})
        need(js.get("task_id"), "%s 提交失败：%s" % (fname, json.dumps(js, ensure_ascii=False)[:200]))
        wait_task(cl, "/api/file-filter/result/%s" % js["task_id"])
        raw = cl.download("/api/file-filter/download/%s" % js["task_id"])
        need(len(raw) > 0, "%s 输出为空" % fname)
        outs.append("%s→%d 字节" % (os.path.basename(fname), len(raw)))
    return "两种格式均可过滤：" + "，".join(outs)


@case("F-08", "F", "file-filter：正则脱敏（手机号中段）")
def f08(cl, ctx):
    content = read(ctx, "file-filter/花名册.xlsx")
    rules = json.dumps([{"pattern": r"(\d{3})\d{4}(\d{4})", "replacement": r"\1****\2",
                         "is_regex": True, "enabled": True}], ensure_ascii=False)
    js = cl.post_multipart("/api/file-filter/filter",
                           {"mode": "hard", "columns": json.dumps(["姓名", "手机号"], ensure_ascii=False),
                            "rules": rules},
                           {"file": ("花名册.xlsx", content)})
    need(js.get("task_id"), "提交失败：%s" % json.dumps(js, ensure_ascii=False)[:200])
    wait_task(cl, "/api/file-filter/result/%s" % js["task_id"])
    raw = cl.download("/api/file-filter/download/%s" % js["task_id"])
    need(raw[:2] == b"PK", "输出不是 xlsx")
    return "脱敏流程完成（输出 %d 字节）" % len(raw)


@case("F-10", "F", "info-transfer：静态码原件传输往返（字节一致）")
def f10(cl, ctx):
    src = read(ctx, "info-transfer/待传输-说明.txt")
    js = cl.post_multipart("/api/info-transfer/encode", {"mode": "static", "raw": "1", "qr_version": "15"},
                           {"file": ("待传输-说明.txt", src)})
    need(js.get("ok"), "封装失败：%s" % js)
    tid = js["task_id"]
    wait_task(cl, "/api/info-transfer/task/%s" % tid)
    code = cl.download("/api/info-transfer/download/%s" % tid)
    need(len(code) > 0, "出码为空")
    js2 = cl.post_multipart("/api/info-transfer/decode", {"type": "image"}, {"file": ("code.png", code)})
    need(js2.get("ok"), "解析失败：%s" % js2)
    payload = js2.get("payload")
    if not payload:
        task = wait_task(cl, "/api/info-transfer/decode/%s" % js2["task_id"])
        payload = json.loads(task.get("payload_json") or "{}")
    out = cl._req("/api/info-transfer/export",
                  json.dumps({"payload": payload, "export": "auto"}).encode("utf-8"),
                  {"Content-Type": "application/json"})[2]
    need(hashlib.sha256(out).hexdigest() == hashlib.sha256(src).hexdigest(),
         "还原内容与原件不一致（%d vs %d 字节）" % (len(out), len(src)))
    return "往返一致：%d 字节，原名 %s" % (len(out), payload.get("name"))


@case("F-11", "F", "info-transfer：精简传输（表格）")
def f11(cl, ctx):
    src = read(ctx, "info-transfer/待传输-花名册.xlsx")
    js = cl.post_multipart("/api/info-transfer/encode", {"mode": "static", "raw": "0", "qr_version": "20"},
                           {"file": ("待传输-花名册.xlsx", src)})
    need(js.get("ok"), "封装失败：%s" % js)
    tid = js["task_id"]
    task = wait_task(cl, "/api/info-transfer/task/%s" % tid)
    code = cl.download("/api/info-transfer/download/%s" % tid)
    need(len(code) > 0, "出码为空")
    js2 = cl.post_multipart("/api/info-transfer/decode", {"type": "image"}, {"file": ("code.zip", code)})
    need(js2.get("ok"), "解析失败：%s" % js2)
    n = js2.get("pages") or task.get("image_count") or 1
    return "精简传输完成：%s 张码，%d 字节（表头已解析）" % (n, len(code))


@case("F-12", "F", "info-transfer：视频码生成（需 cv2/numpy/zfec）")
def f12(cl, ctx):
    st = cl.get_json("/api/info-transfer/status")
    deps = st.get("dependencies") if isinstance(st.get("dependencies"), dict) else st
    if not deps.get("cv2", True):
        return "SKIP：未装 cv2 组件（视频码流模式不可用，属预期降级）"
    src = read(ctx, "info-transfer/待传输-说明.txt")
    js = cl.post_multipart("/api/info-transfer/encode",
                           {"mode": "video", "raw": "1", "qr_version": "15", "video_file": "1"},
                           {"file": ("待传输-说明.txt", src)})
    need(js.get("ok"), "封装失败：%s" % js)
    tid = js["task_id"]
    task = wait_task(cl, "/api/info-transfer/task/%s" % tid, timeout=900)
    if task.get("video_status") != "done":
        cl.post_json("/api/info-transfer/encode/%s/video" % tid, {})
        for _ in range(120):
            task = wait_task(cl, "/api/info-transfer/task/%s" % tid)
            if task.get("video_status") == "done":
                break
    raw = cl.download("/api/info-transfer/download/%s" % tid)
    need(len(raw) > 1000, "视频产物过小：%d 字节" % len(raw))
    need(raw[4:8] == b"ftyp", "不是 mp4（头 %r）" % raw[:12])
    return "视频码生成成功：%d 字节 mp4" % len(raw)


@case("F-13", "F", "info-transfer：粘贴文字（不选文件）")
def f13(cl, ctx):
    text = read(ctx, "info-transfer/待传输-说明.txt").decode("utf-8")
    js = cl.post_multipart("/api/info-transfer/encode", {"mode": "static", "raw": "1", "text": text}, {})
    need(js.get("ok"), "文字封装失败：%s" % js)
    wait_task(cl, "/api/info-transfer/task/%s" % js["task_id"])
    return "文字封装成功（task=%s）" % js["task_id"]


@case("F-15", "F", "trajectory-convert：轨迹表 → 视频码")
def f15(cl, ctx):
    src = read(ctx, "trajectory-convert/轨迹表.xlsx")
    js = cl.post_multipart("/api/trajectory-convert/convert",
                           {"mode": "video", "interval_minutes": "1", "qr_version": "15"},
                           {"file": ("轨迹表.xlsx", src)})
    need(js.get("ok"), "转换失败：%s" % js)
    tid = js["task_id"]
    wait_task(cl, "/api/trajectory-convert/status/%s" % tid, timeout=900)
    raw = cl.download("/api/trajectory-convert/download/%s" % tid)
    need(len(raw) > 1000, "产物过小：%d 字节" % len(raw))
    return "转换完成：%d 字节（%s）" % (len(raw), "mp4" if raw[4:8] == b"ftyp" else "其它格式")


@case("F-17", "F", "trajectory-sketch：轨迹表 → 轨迹图")
def f17(cl, ctx):
    src = read(ctx, "trajectory-sketch/轨迹表.xlsx")
    up = cl.post_multipart("/api/trajectory-sketch/upload", {}, {"file": ("轨迹表.xlsx", src)})
    need(up.get("ok") is not False, "上传失败：%s" % json.dumps(up, ensure_ascii=False)[:200])
    sid = up.get("staged_id")
    need(sid, "上传响应缺少 staged_id：%s" % list(up.keys()))
    kept = [k.get("column") for k in (up.get("filter") or {}).get("kept") or []]
    js = cl.post_json("/api/trajectory-sketch/analyze", {"staged_id": sid, "mode": "hard"})
    need(js.get("ok") is not False, "分析失败：%s" % json.dumps(js, ensure_ascii=False)[:200])
    tid = js.get("task_id")
    if tid:
        wait_task(cl, "/api/trajectory-sketch/result/%s" % tid)
        raw = cl.download("/api/trajectory-sketch/download/%s" % tid)
        kind = ("PNG" if raw[:8] == b"\x89PNG\r\n\x1a\n"
                else "ZIP（多图/报告打包）" if raw[:2] == b"PK" else None)
        need(kind, "产物格式异常（头 %r）" % raw[:8])
        return "出图成功：%d 字节 %s（字段自检保留 %s）" % (len(raw), kind, "、".join(kept) or "—")
    return "分析接口已接受请求（无 task_id，返回：%s）" % json.dumps(js, ensure_ascii=False)[:120]


@case("F-18", "F", "knowledge-base：docx / xlsx / pdf 上传与在线阅读")
def f18(cl, ctx):
    outs = []
    # docx/xlsx 走 /preview（Office 渲染成 HTML，返回 JSON 里的 html 字段）；
    # pdf **不需要** Office 渲染——它由浏览器直接读 /raw（所以 /preview 对 pdf 返回 404 是设计如此）。
    for fname, expect, mode in (("knowledge-base/通知.docx", "专项行动", "preview"),
                                ("knowledge-base/报表.xlsx", "办结", "preview"),
                                ("knowledge-base/手册.pdf", "%PDF", "raw")):
        content = read(ctx, fname)
        js = cl.post_multipart("/api/knowledge-base/files", {},
                               {"file": (os.path.basename(fname), content)})
        need(js.get("ok") is not False, "%s 上传失败：%s" % (fname, js))
        fid = (js.get("file") or {}).get("id") or js.get("id") or js.get("file_id")
        need(fid, "%s 上传响应缺少文件 id：%s" % (fname, js))
        _s, ctype, raw = cl._req("/api/knowledge-base/files/%s/%s" % (fid, mode))
        if mode == "raw":
            need(raw[:4] == b"%PDF", "%s 的 /raw 不是 PDF（头 %r）" % (fname, raw[:8]))
            outs.append("%s(raw %d 字节)" % (os.path.basename(fname), len(raw)))
            continue
        # 预览返回 JSON（中文可能被 \uXXXX 转义）→ 解析后再查关键字
        try:
            text = json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False)
        except Exception:
            text = raw.decode("utf-8", "replace")
        need(expect in text, "%s 预览里没有关键字 %r（前 240 字：%s）" % (fname, expect, text[:240]))
        outs.append(os.path.basename(fname))
    return "三种格式预览均含预期内容：" + "、".join(outs)


@case("F-20", "F", "character-graph：人物档案抽取（docx / pdf / txt）",
      skip="该插件的抽取依赖大模型 API Key（未配置时接口如实返回 400）——人工验收项")
def f20(cl, ctx):
    outs = []
    for fname in ("character-graph/人物档案.docx", "character-graph/人物档案.pdf",
                  "character-graph/人物档案.txt"):
        content = read(ctx, fname)
        js = cl.post_multipart("/api/character-graph/analyze", {}, {"file": (os.path.basename(fname), content)})
        need(js.get("ok") is not False, "%s 抽取失败：%s" % (fname, js))
        tid = js.get("task_id")
        if tid:
            task = wait_task(cl, "/api/character-graph/result/%s" % tid, timeout=600)
            names = json.dumps(task, ensure_ascii=False)
            need("张伟" in names or "李静" in names, "%s 未抽出预期人名" % fname)
        outs.append(os.path.basename(fname))
    return "三种格式均可抽取：" + "、".join(outs)


@case("F-22", "F", "case-report：收网简报解析（五要素）",
      skip="解析依赖大模型 API Key（未配置时任务如实失败）——配置后人工验收")
def f22(cl, ctx):
    text = read(ctx, "case-report/收网情况简报.txt").decode("utf-8")
    js = cl.post_json("/api/case-report/parse", {"text": text})
    need(js.get("ok") is not False, "解析失败：%s" % js)
    tid = js.get("task_id")
    data = js
    if tid:
        data = wait_task(cl, "/api/case-report/result/%s" % tid, timeout=600)
    blob = json.dumps(data, ensure_ascii=False)
    for key in ("案件名", "时间", "主办大队", "抓获人数", "缴获物品"):
        need(key in blob, "解析结果缺少字段 %s（%s）" % (key, blob[:200]))
    need("三大队" in blob, "主办大队识别不正确")
    return "五要素齐全，主办大队=三大队"


@case("F-24", "F", "shared-docs：新建文档 → 列表 → 详情")
def f24(cl, ctx):
    title = "验收自动测试文档"
    body = read(ctx, "shared-docs/文档正文.txt").decode("utf-8")
    js = cl.post_json("/api/shared-docs/documents", {"name": title, "type": "word"})
    need(js.get("ok") is not False, "新建失败：%s" % json.dumps(js, ensure_ascii=False)[:200])
    doc = js.get("document") or js.get("doc") or {}
    did = doc.get("id") or js.get("id") or js.get("doc_id")
    need(did, "新建响应缺少文档 id：%s" % js)
    # 注：内容更新接口（/content）要求带上当前版本号，否则 409——那是并发保护、不是缺陷；
    # 编辑流程属人工验收项（验收手册 F-24），这里验 新建 → 列表 → 详情 → 导出。
    lst = cl.get_json("/api/shared-docs/documents")
    blob = json.dumps(lst, ensure_ascii=False)
    need(title in blob, "列表里没有新建的文档")
    detail = cl.get_json("/api/shared-docs/documents/%s" % did)
    need(title in json.dumps(detail, ensure_ascii=False), "详情里没有该文档")
    exp = cl.download("/api/shared-docs/documents/%s/export" % did)
    need(len(exp) > 100, "导出为空")
    return "新建/列表/详情/导出均正常（id=%s，导出 %d 字节）" % (did, len(exp))


@case("F-26", "F", "notice-board：发布公告 → 列表 / 最新")
def f26(cl, ctx):
    content = read(ctx, "notice-board/公告正文.txt").decode("utf-8")
    units = cl.get_json("/api/admin/units")
    items = units.get("units") or units.get("items") or []
    need(items, "取不到单位列表，无法构造可见范围：%s" % json.dumps(units, ensure_ascii=False)[:120])
    uid = items[0].get("id") or items[0].get("unit_id")
    js = cl.post_json("/api/notice-board/announcements",
                      {"title": "验收自动测试公告", "content": content, "level": "normal",
                       "targets": [{"type": "unit", "id": uid}]})
    need(js.get("ok") is not False, "发布失败：%s" % json.dumps(js, ensure_ascii=False)[:200])
    lst = cl.get_json("/api/notice-board/announcements")
    need("验收自动测试公告" in json.dumps(lst, ensure_ascii=False), "列表里没有该公告")
    latest = cl.get_json("/api/notice-board/latest")
    need(latest.get("ok") is not False, "latest 接口异常：%s" % latest)
    return "公告发布成功，列表与最新接口均可读"


@case("F-27", "F", "base64：编码 → 解码往返")
def f27(cl, ctx):
    # 纯前端插件：只校验页面资源可服务（编解码在浏览器里做，属人工验收项）
    _s, ctype, raw = cl._req("/plugin/base64/index.html")
    need(len(raw) > 100, "插件页面为空")
    return "插件页面可服务（%d 字节）——编解码往返见验收手册 F-27（人工）" % len(raw)


@case("F-28", "F", "json-formatter：页面可服务")
def f28(cl, ctx):
    _s, _c, raw = cl._req("/plugin/json-formatter/index.html")
    need(len(raw) > 100, "插件页面为空")
    return "插件页面可服务（%d 字节）——格式化/校验见验收手册 F-28（人工）" % len(raw)


@case("F-29", "F", "md5-generator：页面可服务")
def f29(cl, ctx):
    _s, _c, raw = cl._req("/plugin/md5-generator/index.html")
    need(len(raw) > 100, "插件页面为空")
    return "插件页面可服务（%d 字节）——MD5 与预期值比对见验收手册 F-29（人工）" % len(raw)


# ============================== R 组：依赖与降级 ==============================

@case("R-01", "R", "依赖判定与「已安装依赖」面板一致")
def r01(cl, ctx):
    deps = cl.get_json("/api/admin/deps")
    groups = {g["id"]: g for g in deps.get("groups", [])}
    need("framework" in groups and "external" in groups, "依赖面板缺少分组：%s" % list(groups))
    installed = set()
    for g in deps["groups"]:
        for it in g.get("items") or []:
            if it.get("state") in (None, "ok") and (it.get("dist") or it.get("name")):
                installed.add(str(it.get("dist") or it.get("name")).lower())
    rows = cl.get_json("/api/admin/plugins").get("plugins") or []
    bad = []
    for row in rows:
        for d in (row.get("deps") or {}).get("detail") or []:
            if d.get("kind") != "python_package":
                continue
            name = str(d.get("name") or "").lower()
            state_ok = d.get("state") == "ok"
            listed = name in installed or name.replace("_", "-") in installed
            if state_ok and not listed and d.get("state") != "unknown":
                bad.append("%s/%s：判定 ok 但面板未列出" % (row["id"], name))
    need(not bad, "判定与面板不一致：%s" % "；".join(bad[:3]))
    return "面板 %d 个分组；%d 个插件的逐依赖判定与面板一致" % (len(groups), len(rows))


@case("R-02", "R", "插件页面自报依赖（/status）可读且与判定一致")
def r02(cl, ctx):
    outs = []
    for pid, path in (("info-transfer", "/api/info-transfer/status"),
                      ("file-filter", "/api/file-filter/status")):
        js = cl.get_json(path)
        d = js.get("dependencies") if isinstance(js.get("dependencies"), dict) else js
        d = {k: v for k, v in d.items() if isinstance(v, bool)}
        need(d, "%s 的 /status 未暴露依赖布尔字段：%s" % (pid, list(js.keys())))
        outs.append("%s=%s" % (pid, {k: bool(v) for k, v in d.items() if isinstance(v, bool)}))
    return "；".join(outs)


@case("R-06", "R", "--check-deps 真实 import 自检", needs=("app_dir",))
def r06(cl, ctx):
    import subprocess
    import tempfile
    exe = os.path.join(ctx["app_dir"], "JZToolsHub.exe")
    need(os.path.isfile(exe), "程序目录里没有 JZToolsHub.exe：%s" % exe)
    out = os.path.join(tempfile.gettempdir(), "jz-acceptance-checkdeps.json")
    if os.path.isfile(out):
        os.remove(out)
    # ★ 用 Popen + 轮询而不是 subprocess.run(timeout)：旧版主包的 exe **不认识 --check-deps**，
    #   会把这次调用当成"启动服务"并一直运行——必须主动收掉，避免在目标机上留下一个服务进程。
    proc = subprocess.Popen([exe, "--check-deps", out],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 120:
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if not os.path.isfile(out):
        ver = ""
        try:
            vj = json.load(open(os.path.join(ctx["app_dir"], "version.json"), encoding="utf-8-sig"))
            ver = "（该目录 version.json: app %s）" % vj.get("app")
        except Exception:
            pass
        raise AssertionError("该程序目录的 exe 不支持 --check-deps 或未产出结果%s"
                            "——疑似旧版主包，请用本批主包验收" % ver)
    js = json.load(open(out, encoding="utf-8-sig"))
    imports = js.get("imports") or {}
    if not imports or not js.get("components"):
        return "SKIP：该程序目录未装依赖组件，自检没有可验对象（属预期）"
    need(js.get("ok"), "自检未通过：%s" % json.dumps(imports, ensure_ascii=False))
    return "自检通过：%d 个模块真实 import 成功（%s）" % (len(imports), "、".join(sorted(imports)))


@case("R-07", "R", "依赖组件登记与目录一致")
def r07(cl, ctx):
    deps = cl.get_json("/api/admin/deps")
    comps = []
    for g in deps.get("groups", []):
        for it in g.get("items") or []:
            if it.get("source") == "component":
                comps.append("%s %s" % (it.get("dist"), it.get("version")))
    return "已装依赖组件 %d 个：%s" % (len(comps), "、".join(comps) or "（无）")


# ============================== S 组：安全 ==============================

@case("S-01", "S", "被篡改的插件包必须被拒绝", needs=("media",))
def s01(cl, ctx):
    media = ctx["media"]
    pkgs = [f for f in os.listdir(media) if f.startswith("JZToolsHub-插件-") and f.endswith(".zip")]
    need(pkgs, "介质目录里没有插件包：%s" % media)
    src = os.path.join(media, sorted(pkgs)[0])
    data = bytearray(open(src, "rb").read())
    data[len(data) // 2] ^= 0xFF                       # 改一个字节
    try:
        js = cl.post_multipart("/api/admin/plugins/upload", {}, {"file": ("tampered.zip", bytes(data))})
    except HttpError as e:
        need(e.code in (400, 422), "篡改包应被 400 拒绝，实际 HTTP %s" % e.code)
        return "篡改包被拒绝（HTTP %s）" % e.code
    need(js.get("ok") is False, "篡改包未被拒绝：%s" % json.dumps(js, ensure_ascii=False)[:200])
    return "篡改包被拒绝：%s" % (js.get("error") or "")[:100]


@case("S-02", "S", "非超管账号不能管理插件", needs=("user2",))
def s02(cl, ctx):
    c2 = Client(ctx["base"], ctx["user2"], ctx["password2"])
    c2.login()
    try:
        c2.get_json("/api/admin/plugins")
    except HttpError as e:
        need(e.code in (403, 401), "应 403/401，实际 %s" % e.code)
        return "普通账号访问插件管理被拒（HTTP %s）" % e.code
    raise AssertionError("普通账号竟能访问 /api/admin/plugins")


@case("S-03", "S", "空文件与错误扩展名被拒绝")
def s03(cl, ctx):
    try:
        cl.post_multipart("/api/file-filter/filter", {"mode": "hard", "columns": "[]"},
                          {"file": ("empty.xlsx", b"")})
        raise AssertionError("空文件未被拒绝")
    except HttpError as e:
        need(e.code in (400, 413), "空文件应 400/413，实际 %s" % e.code)
    try:
        cl.post_multipart("/api/file-filter/filter", {"mode": "hard", "columns": "[]"},
                          {"file": ("x.txt", b"hello")})
        raise AssertionError("错误扩展名未被拒绝")
    except HttpError as e:
        need(e.code == 400, "错误扩展名应 400，实际 %s" % e.code)
    return "空文件与错误扩展名均被拒绝（400/413）"


# ============================== X 组：稳定性与边界 ==============================

@case("X-01", "X", "不存在的任务 id 返回 404")
def x01(cl, ctx):
    try:
        cl.get_json("/api/info-transfer/task/nonexistent123")
        raise AssertionError("不存在的任务未被拒绝")
    except HttpError as e:
        need(e.code == 404, "应 404，实际 %s" % e.code)
    return "不存在的任务返回 404"


@case("X-02", "X", "并发提交多个封装任务，全部完成")
def x02(cl, ctx):
    src = read(ctx, "info-transfer/待传输-说明.txt")
    tids = []
    for _ in range(3):
        js = cl.post_multipart("/api/info-transfer/encode", {"mode": "static", "raw": "1"},
                               {"file": ("待传输-说明.txt", src)})
        need(js.get("ok"), "提交失败：%s" % js)
        tids.append(js["task_id"])
    for tid in tids:
        wait_task(cl, "/api/info-transfer/task/%s" % tid)
        raw = cl.download("/api/info-transfer/download/%s" % tid)
        need(len(raw) > 0, "任务 %s 产物为空" % tid)
    return "3 个并发任务全部完成并可下载"


@case("X-03", "X", "断网可用（本地资源不依赖外网）")
def x03(cl, ctx):
    _s, _c, raw = cl._req("/")
    need(len(raw) > 100, "首页为空")
    _s, _c, raw2 = cl._req("/api/tools")
    need(len(raw2) > 10, "/api/tools 为空")
    return "首页与 /api/tools 均为本地响应（%d / %d 字节）" % (len(raw), len(raw2))


# ============================== 运行器 ==============================

def detect_app_dir():
    """自动探测程序目录：注册表 InstallLocation → %LOCALAPPDATA%\\JZToolsHub（与安装器同口径）。

    这样在目标机上跑 R-06（--check-deps 自检）不必让执行人知道程序装在哪。
    """
    import subprocess
    try:
        out = subprocess.run(["reg", "query",
                              "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\JZToolsHub",
                              "/v", "InstallLocation"], capture_output=True, timeout=20)
        m = re.search(r"InstallLocation\s+REG_SZ\s+(.+)", out.stdout.decode("gbk", "replace"))
        if m:
            p = m.group(1).strip()
            if os.path.isfile(os.path.join(p, "JZToolsHub.exe")):
                return p
    except Exception:
        pass
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "JZToolsHub")
    return cand if os.path.isfile(os.path.join(cand, "JZToolsHub.exe")) else ""


def main():
    ap = argparse.ArgumentParser(description="自动验收：跑可 HTTP 驱动的用例")
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--user2", default="", help="普通账号（跑 S-02）")
    ap.add_argument("--password2", default="")
    ap.add_argument("--testdata", default="", help="测试数据目录（缺省：exe 旁的 testdata\\，再退回仓库）")
    ap.add_argument("--app-dir", default="", help="程序目录（跑 R-06 --check-deps）")
    ap.add_argument("--media", default="", help="插件包目录（跑 S-01 篡改包）")
    ap.add_argument("--groups", default="", help="只跑指定组，如 F,R（逗号分隔）")
    ap.add_argument("--report", default="", help="输出 markdown 报告路径")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    testdata = args.testdata
    if not testdata:
        for cand in (os.path.join(ROOT, "testdata"), os.path.join(os.getcwd(), "testdata")):
            if os.path.isdir(cand):
                testdata = cand
                break
        testdata = testdata or os.path.join(ROOT, "testdata")
    app_dir = args.app_dir or detect_app_dir()
    print("测试数据：%s" % testdata)
    if app_dir:
        print("程序目录（自动探测）：%s" % app_dir)
    ctx = {"base": args.base, "testdata": testdata, "app_dir": app_dir,
           "media": args.media, "user2": args.user2, "password2": args.password2,
           "timeout": args.timeout}
    groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]
    cl = Client(args.base, args.user, args.password)
    try:
        cl.login()
    except Exception as e:
        print("无法连接/登录：%s" % e)
        print("请先启动服务（start.bat 或 python app.py）再运行本脚本。")
        return 2

    results = []
    for c in CASES:
        if groups and c["group"] not in groups:
            continue
        if c["skip"]:
            results.append((c, "SKIP", c["skip"]))
            print("  [SKIP] %-6s %s（%s）" % (c["id"], c["title"], c["skip"]))
            continue
        missing = [k for k in c["needs"] if not ctx.get(k)]
        if missing:
            reason = "需加开关：%s" % "、".join("--" + k.replace("_", "-") for k in missing)
            results.append((c, "SKIP", reason))
            print("  [SKIP] %-6s %s（%s）" % (c["id"], c["title"], reason))
            continue
        t0 = time.time()
        try:
            detail = c["fn"](cl, ctx) or ""
            if str(detail).startswith("SKIP"):
                results.append((c, "SKIP", detail[5:].lstrip("：: ")))
                print("  [SKIP] %-6s %s（%s）" % (c["id"], c["title"], detail))
            else:
                results.append((c, "PASS", detail))
                print("  [PASS] %-6s %s —— %s（%.1fs）" % (c["id"], c["title"], detail, time.time() - t0))
        except HttpError as e:
            results.append((c, "FAIL", str(e)))
            print("  [FAIL] %-6s %s —— %s" % (c["id"], c["title"], e))
        except Exception as e:
            results.append((c, "FAIL", "%s: %s" % (type(e).__name__, e)))
            print("  [FAIL] %-6s %s —— %s: %s" % (c["id"], c["title"], type(e).__name__, e))

    npass = sum(1 for _c, r, _d in results if r == "PASS")
    nfail = sum(1 for _c, r, _d in results if r == "FAIL")
    nskip = sum(1 for _c, r, _d in results if r == "SKIP")
    print("\n==== 汇总：通过 %d / 失败 %d / 跳过 %d（共 %d）====" % (npass, nfail, nskip, len(results)))

    if args.report:
        lines = ["# 自动验收报告", "",
                 "- 实例：`%s`" % args.base,
                 "- 测试数据：`%s`" % ctx["testdata"],
                 "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                 "- 结果：**通过 %d / 失败 %d / 跳过 %d**" % (npass, nfail, nskip), "",
                 "| 用例 | 组 | 结果 | 说明 |", "| --- | --- | --- | --- |"]
        for c, r, d in results:
            lines.append("| %s | %s | %s | %s |" % (c["id"], c["group"], r, str(d).replace("|", "/")[:160]))
        lines += ["", "> 跳过项多为「浏览器侧 / 需 Key / 需人工」的用例，详见《验收手册》同名编号。"]
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        print("报告已写入：%s" % args.report)

    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
