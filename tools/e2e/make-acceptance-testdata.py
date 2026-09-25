#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""生成「验收测试数据」：各插件的测试文件 + 预生成二维码/视频产物 + 验收检查表。

为什么单独做一份：验收（见 docs\guide\干净机器部署验收手册.md）需要**贴近业务**的输入文件，
临时找文件既不可复现、也测不全（旧格式 .doc/.xls、缺依赖时的降级、纠错/多张码等）。

用法（开发机 / 仓库内执行；目标机不需要 Python）：
    python tools\e2e\make-acceptance-testdata.py                    # 只生成文件类夹具
    python tools\e2e\make-acceptance-testdata.py --with-codes \
        --base http://127.0.0.1:5000 --user admin --password admin123
                                                                    # 另用运行中的实例预生成二维码/视频
    python tools\e2e\make-acceptance-testdata.py --verify --base http://127.0.0.1:5000
                                                                    # 对预生成产物做往返校验（推荐）

产物：`testdata\`（**随仓库提交**，约 540 KB（2026-09-24 实测）：拿到仓库就有经过校验的数据，不必跑生成器；
      发布时另打包为 deploy\JZToolsHub-验收测试数据-v<日期>.zip）
    testdata\README.md              ← 逐插件：文件 → 操作 → 通过口径（验收时照这个走）
    testdata\<插件id>\...           ← 测试文件

依赖：openpyxl / python-docx / xlwt（写 .xls）；LibreOffice（docx→doc/pdf 转换，可选——
缺它时跳过旧格式与 PDF 并提示）；cv2+numpy 仅预生成视频时用到（由运行中的实例做，不在本脚本内）。
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # tools/e2e → 仓库根
OUT = os.path.join(ROOT, "testdata")


# ============================== 基础写文件 ==============================

def ensure(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return path


def write_text(rel, text):
    p = ensure(os.path.join(OUT, rel))
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


def write_csv(rel, headers, rows):
    p = ensure(os.path.join(OUT, rel))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    return p


def write_xlsx(rel, headers, rows, sheet="Sheet1", widths=None, text_cols=()):
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    p = ensure(os.path.join(OUT, rel))
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    # 身份证号/登录名一类的"标识"列按文本存：按数字存 18 位会丢精度（admin 导入会检测并提示）
    for idx in text_cols:
        for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
            for c in row:
                c.number_format = "@"
    for i, h in enumerate(headers, start=1):
        w = (widths or {}).get(h) or max(10, min(40, len(str(h)) * 2 + 6))
        ws.column_dimensions[get_column_letter(i)].width = w
    wb.save(p)
    return p


def write_xls(rel, headers, rows, sheet="Sheet1"):
    """旧格式 .xls（xlwt）。用于验证 xlrd 依赖组件与"xls→xlsx"输出路径。"""
    import xlwt
    p = ensure(os.path.join(OUT, rel))
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet(sheet)
    for j, h in enumerate(headers):
        ws.write(0, j, str(h))
    for i, r in enumerate(rows, start=1):
        for j, v in enumerate(r):
            ws.write(i, j, "" if v is None else str(v))
    wb.save(p)
    return p


def write_docx(rel, title, paragraphs, table=None, subtitle=""):
    from docx import Document
    p = ensure(os.path.join(OUT, rel))
    doc = Document()
    doc.add_heading(title, level=0)
    if subtitle:
        doc.add_paragraph(subtitle)
    for t in paragraphs:
        if isinstance(t, tuple) and t and t[0] == "h":
            doc.add_heading(t[1], level=2)
        else:
            doc.add_paragraph(t)
    if table:
        headers, rows = table
        tb = doc.add_table(rows=1, cols=len(headers))
        tb.style = "Table Grid"
        for j, h in enumerate(headers):
            tb.rows[0].cells[j].text = str(h)
        for r in rows:
            cells = tb.add_row().cells
            for j, v in enumerate(r):
                cells[j].text = "" if v is None else str(v)
    doc.save(p)
    return p


# ============================== LibreOffice 转换（旧格式 / PDF） ==============================

def find_soffice():
    for c in (r"D:\LibreOffice\program\soffice.exe",
              r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if os.path.isfile(c):
            return c
    return shutil.which("soffice") or shutil.which("soffice.exe")


_SOFFICE_WARMED = [False]


def _soffice_run(soffice, profile, args, timeout=180):
    cmd = [soffice, "--headless", "--norestore",
           "-env:UserInstallation=file:///" + profile.replace("\\", "/")] + args
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return None


def convert_with_soffice(soffice, src, target, out_dir):
    """docx → doc / pdf。成功返回产物路径，失败返回 None（调用方决定是否跳过）。

    两个实测坑：① 首次调用时 LibreOffice 还在初始化用户 profile，转换会直接失败 →
    先预热一次（--terminate_after_init）；② 偶发失败 → 重试一次。
    ★ target 用**不带过滤器**的写法（`doc` / `pdf`）：带 `doc:"MS Word 97"` 反而报
      "SfxBaseModel::impl_store … Error Area:Io Class:Parameter Code:26"（实测）。
    """
    os.makedirs(out_dir, exist_ok=True)
    profile = os.path.join(ROOT, "build", "_soffice-profile")
    if not _SOFFICE_WARMED[0]:
        _soffice_run(soffice, profile, ["--terminate_after_init"], timeout=120)
        _SOFFICE_WARMED[0] = True
    base = os.path.splitext(os.path.basename(src))[0]
    cand = os.path.join(out_dir, base + "." + target.split(":")[0])
    for attempt in (1, 2):
        if os.path.isfile(cand):
            try:
                os.remove(cand)
            except OSError:
                pass
        _soffice_run(soffice, profile, ["--convert-to", target, "--outdir", out_dir, src])
        if os.path.isfile(cand):
            return cand
        time.sleep(2.0)
    return None


# ============================== 业务数据（贴近场景） ==============================

ROSTER_HEADERS = ["序号", "姓名", "身份证号码", "手机号", "所属单位", "职务", "备注"]
ROSTER_ROWS = [
    [1, "张伟", "110105198703124531", "13800001001", "一大队", "大队长", "主持全面工作"],
    [2, "李静", "110105199008221246", "13800001002", "一大队", "副大队长", "分管案件办理"],
    [3, "王强", "110105198511093318", "13800001003", "一大队", "民警", "办案一组"],
    [4, "刘洋", "110105199205174470", "13800001004", "一大队", "民警", "办案二组"],
    [5, "陈晨", "110105199401026612", "13800001005", "一大队", "辅警", ""],
    [6, "赵敏", "110105198809307723", "13800001006", "二大队", "大队长", "主持全面工作"],
    [7, "孙磊", "110105198612158815", "13800001007", "二大队", "民警", "技术侦查"],
    [8, "周芳", "110105199103249926", "13800001008", "二大队", "民警", "情报研判"],
    [9, "吴昊", "110105199307060037", "13800001009", "二大队", "辅警", ""],
    [10, "郑爽", "110105199512111148", "13800001010", "三大队", "大队长", "主持全面工作"],
    [11, "冯磊", "110105198705282259", "13800001011", "三大队", "副大队长", "分管勤务"],
    [12, "许静怡", "110105199609133360", "13800001012", "三大队", "民警", "内勤"],
    [13, "何鹏", "110105199012194471", "13800001013", "三大队", "民警", "外勤"],
    [14, "吕娜", "110105199203265582", "13800001014", "三大队", "辅警", ""],
    [15, "施磊", "110105198901116693", "13800001015", "综合科", "科长", "综合协调"],
    [16, "钱伟", "110105198404027704", "13800001016", "综合科", "民警", "文秘档案"],
    [17, "孔晓", "110105199310198815", "13800001017", "综合科", "民警", "装备保障"],
    [18, "曹阳", "110105198802239926", "13800001018", "法制科", "科长", "案件审核"],
    [19, "严莉", "110105199111300037", "13800001019", "法制科", "民警", "执法监督"],
    [20, "华健", "110105198706051148", "13800001020", "法制科", "民警", "行政复议"],
]
ROSTER_NOTE = "本表为**测试数据**：身份证号与手机号均为虚构，仅用于验证过滤/脱敏/导入导出。"

# ★ 表头必须对齐 trajectory-convert 的**默认字段名**（开始时间 / 经度 / 纬度），
#   否则转换任务会以"未能在表头中找到字段"失败（列名不同可在插件 config.json 里改）。
TRACK_HEADERS = ["编号", "开始时间", "经度", "纬度", "速度(km/h)", "方向(度)"]
TRACK_ROWS = []
_t0 = time.mktime(time.strptime("2026-09-18 08:00:00", "%Y-%m-%d %H:%M:%S"))
_lng, _lat = 116.3975, 39.9087
for i in range(1, 61):
    _lng += 0.0007 + (0.0002 if i % 7 == 0 else 0)
    _lat += 0.0004 - (0.0001 if i % 5 == 0 else 0)
    TRACK_ROWS.append([
        "T%04d" % i,
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t0 + i * 60)),
        round(_lng, 6), round(_lat, 6),
        round(18 + (i % 9) * 3.5, 1),
        round((i * 17) % 360, 1),
    ])
TRACK_NOTE = ("轨迹表：60 个点、每分钟一个点，沿道路向东偏北行进；用于转换/出码/绘制轨迹图。"
              "列名按 trajectory-convert 默认字段：`开始时间` / `经度` / `纬度`；若你的环境改过字段名，请在插件配置里对齐或改表头。")

BRIEF_TEXT = """收网情况简报

2026年9月16日晚，在市局统一指挥下，三大队协助经侦支队对“1·7”专案开展集中收网行动。
行动中抓获涉案犯罪嫌疑人胡某、李某等 2 人，现场查获烟叶 4.6 吨、烟丝 2.1 吨、私烟 600 件
（合计 17700 支），涉案案值约 25 万元。目前案件正在进一步侦办中。

主办大队：三大队
行动时间：2026年9月16日
抓获人数：2
缴获物品：烟叶4.6吨、烟丝2.1吨、私烟600件、私烟17700支
"""

PROFILE_TEXT = """人物档案（测试）

一、基本情况
张伟，男，1987年生，一大队大队长，主持全面工作，与副大队长李静共同负责“1·7”专案。
李静，女，1990年生，一大队副大队长，分管案件办理，直接对接法制科科长曹阳。
王强，男，1985年生，一大队民警，办案一组，日常向李静汇报；与二大队民警孙磊系同批入警。
孙磊，男，1986年生，二大队民警，负责技术侦查，与情报研判岗周芳协作。

二、关系摘要
张伟 → 领导 → 李静；李静 → 领导 → 王强；王强 ↔ 同批 → 孙磊；孙磊 ↔ 协作 → 周芳。
"""

DOC_NOTICE_PARAS = [
    ("h", "一、总体要求"),
    "各部门要按照统一部署，压实责任、细化措施，确保各项任务落地见效。",
    ("h", "二、时间安排"),
    "自即日起至本月底为集中推进阶段，下月初开展检查验收。",
    ("h", "三、工作要求"),
    "1. 明确专人负责，建立台账；",
    "2. 每周五前报送进展；",
    "3. 遇重大问题及时报告。",
]
DOC_NOTICE_TABLE = (["责任部门", "负责人", "完成时限"],
                    [["一大队", "张伟", "本月底"], ["二大队", "赵敏", "本月底"], ["综合科", "施磊", "下月初"]])

REPORT_HEADERS = ["月份", "受理数", "办结数", "办结率", "备注"]
REPORT_ROWS = [
    ["2026-06", 128, 121, "94.5%", ""],
    ["2026-07", 143, 139, "97.2%", "专项行动月"],
    ["2026-08", 151, 147, "97.4%", ""],
    ["2026-09", 96, 88, "91.7%", "统计截至 9 月 20 日"],
]


# ============================== 预生成二维码 / 视频（驱动运行中的实例） ==============================

class App(object):
    """极简客户端：登录 + multipart 上传 + 轮询 + 下载（只用标准库）。"""

    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.cookie = None
        self.login(user, password)

    def _req(self, path, data=None, headers=None, method=None):
        url = self.base + path
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                body = r.read()
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                return r.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as e:
            # 带上 URL 与响应体：否则只看到 "HTTP Error 404" 无法定位是哪个接口
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError("HTTP %s：%s %s ｜ %s" % (e.code, req.get_method(), url, detail))

    def login(self, user, password):
        body = json.dumps({"username": user, "password": password}).encode("utf-8")
        _, raw = self._req("/api/login", body, {"Content-Type": "application/json"})
        js = json.loads(raw.decode("utf-8"))
        if not js.get("ok"):
            raise RuntimeError("登录失败：%s" % js)

    def json_post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        _, raw = self._req(path, body, {"Content-Type": "application/json"})
        return json.loads(raw.decode("utf-8"))

    def json_get(self, path):
        _, raw = self._req(path)
        return json.loads(raw.decode("utf-8"))

    def multipart(self, path, fields, files):
        boundary = "----jzaccept%s" % int(time.time() * 1000)
        buf = []
        for k, v in fields.items():
            buf.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                        % (boundary, k, v)).encode("utf-8"))
        for k, (fname, content) in files.items():
            buf.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                        "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, fname)).encode("utf-8"))
            buf.append(content)
            buf.append(b"\r\n")
        buf.append(("--%s--\r\n" % boundary).encode("utf-8"))
        data = b"".join(buf)
        _, raw = self._req(path, data, {"Content-Type": "multipart/form-data; boundary=" + boundary})
        return json.loads(raw.decode("utf-8"))

    def download(self, path, dest):
        _, raw = self._req(path)
        p = ensure(dest)
        with open(p, "wb") as f:
            f.write(raw)
        return p, len(raw)


def wait_task(app, status_path, key="status", timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        js = app.json_get(status_path)
        task = js.get("task") if isinstance(js.get("task"), dict) else js
        st = task.get(key) or task.get("status")
        if st in ("done", "failed", "error"):
            if st != "done":
                raise RuntimeError("任务失败（%s）：%s" % (status_path, task.get("detail") or task))
            return task
        time.sleep(1.0)
    raise RuntimeError("任务超时：%s（stage=%s）" % (status_path, task.get("stage")))


def gen_codes(app, info):
    """用运行中的实例预生成：静态码（原件传输，字节精确）+ 二维码视频 + 轨迹视频。"""
    # ① info-transfer：静态码（单张或多张 ZIP）
    src = os.path.join(OUT, "info-transfer", "待传输-说明.txt")
    with open(src, "rb") as f:
        content = f.read()
    js = app.multipart("/api/info-transfer/encode",
                       {"mode": "static", "raw": "1", "qr_version": "15"},
                       {"file": ("待传输-说明.txt", content)})
    if not js.get("ok"):
        raise RuntimeError("静态码封装失败：%s" % js)
    tid = js["task_id"]
    task = wait_task(app, "/api/info-transfer/task/%s" % tid)
    out = task.get("output_type")
    dest = os.path.join(OUT, "info-transfer", "预生成-说明-静态码" + (".zip" if task.get("image_count", 1) > 1 else ".png"))
    p, n = app.download("/api/info-transfer/download/%s" % tid, dest)
    info.append("预生成静态码：%s（%d 字节，output_type=%s，%s 张）" % (os.path.basename(p), n, out, task.get("image_count")))

    # ② info-transfer：视频码（同一文件，视频模式 + 当场编码 mp4）
    js = app.multipart("/api/info-transfer/encode",
                       {"mode": "video", "raw": "1", "qr_version": "15", "video_file": "1"},
                       {"file": ("待传输-说明.txt", content)})
    if not js.get("ok"):
        raise RuntimeError("视频码封装失败：%s" % js)
    tid = js["task_id"]
    task = wait_task(app, "/api/info-transfer/task/%s" % tid, timeout=600)
    if task.get("video_status") != "done":
        app.json_post("/api/info-transfer/encode/%s/video" % tid, {})
        for _ in range(300):
            task = wait_task(app, "/api/info-transfer/task/%s" % tid)
            if task.get("video_status") == "done":
                break
    p, n = app.download("/api/info-transfer/download/%s" % tid, os.path.join(OUT, "qr-video-decode", "预生成-说明-视频码.mp4"))
    info.append("预生成二维码视频：%s（%d 字节）" % (os.path.basename(p), n))

    # ③ trajectory-convert：轨迹表 → 视频码（解码端与 info-transfer 同一协议）
    src = os.path.join(OUT, "trajectory-convert", "轨迹表.xlsx")
    with open(src, "rb") as f:
        content = f.read()
    js = app.multipart("/api/trajectory-convert/convert",
                       {"mode": "video", "interval_minutes": "1", "qr_version": "15"},
                       {"file": ("轨迹表.xlsx", content)})
    if not js.get("ok"):
        raise RuntimeError("轨迹转换失败：%s" % js)
    tid = js["task_id"]
    wait_task(app, "/api/trajectory-convert/status/%s" % tid, timeout=900)
    p, n = app.download("/api/trajectory-convert/download/%s" % tid,
                        os.path.join(OUT, "trajectory-convert", "预生成-轨迹视频.mp4"))
    info.append("预生成轨迹视频：%s（%d 字节）" % (os.path.basename(p), n))


# ============================== 往返校验（--verify） ==============================

def _decode_sync(app, path, dtype):
    """静态码解码是**同步**返回信封（无 task_id）；视频走任务。返回 (task_like, payload)。"""
    with open(path, "rb") as f:
        content = f.read()
    js = app.multipart("/api/info-transfer/decode", {"type": dtype},
                       {"file": (os.path.basename(path), content)})
    if not js.get("ok"):
        raise RuntimeError("解码提交失败：%s" % js)
    if js.get("payload"):
        return js, js["payload"]
    tid = js.get("task_id")
    if not tid:
        raise RuntimeError("解码响应既无 payload 也无 task_id：%s" % js)
    task = wait_task(app, "/api/info-transfer/decode/%s" % tid, timeout=900)
    pj = task.get("payload_json") or ""
    if not pj:
        raise RuntimeError("视频解码完成但没有 payload_json：%s" % task)
    return task, json.loads(pj)


def _export(app, payload, dest):
    """把信封导出为文件（走插件自己的 /export，与页面"导出文件"同一路径）。"""
    _, raw = app._req("/api/info-transfer/export",
                      json.dumps({"payload": payload, "export": "auto"}).encode("utf-8"),
                      {"Content-Type": "application/json"})
    p = ensure(dest)
    with open(p, "wb") as f:
        f.write(raw)
    return p


def verify(app):
    """往返校验预生成产物；返回 (通过数, 失败列表)。"""
    import hashlib
    passed, failed = 0, []
    src = os.path.join(OUT, "info-transfer", "待传输-说明.txt")
    src_bytes = open(src, "rb").read()
    src_sha = hashlib.sha256(src_bytes).hexdigest()
    cases = [
        ("info-transfer/预生成-说明-静态码.png", "image", "静态码 PNG → 说明文本", src_sha),
        ("qr-video-decode/预生成-说明-视频码.mp4", "video", "二维码视频 → 说明文本", src_sha),
    ]
    for rel, dtype, label, want in cases:
        p = os.path.join(OUT, rel)
        if not os.path.isfile(p):
            failed.append("%s：文件不存在（未用 --with-codes 生成？）" % label)
            continue
        try:
            task, payload = _decode_sync(app, p, dtype)
            out = _export(app, payload, os.path.join(ROOT, "build", "_verify_out"))
            got = hashlib.sha256(open(out, "rb").read()).hexdigest()
            if got == want:
                passed += 1
                print("  [PASS] %s（原名 %s，字节一致）" % (label, payload.get("name")))
            else:
                failed.append("%s：还原内容与原件不一致" % label)
                print("  [FAIL] %s：字节不一致" % label)
        except Exception as e:
            failed.append("%s：%s" % (label, e))
            print("  [FAIL] %s：%s" % (label, e))
    # 轨迹视频用的是 trajectory-convert 自己的 QR-transfer 信封（jzt 与 info-transfer 不同），
    # 且**解码在 qr-video-decode 页面由浏览器完成**（服务端只接收已解码的分块）——
    # 故这里只能自动化到「是合法 mp4 且帧数正常」，实际解码列为人工验收项。
    p = os.path.join(OUT, "trajectory-convert", "预生成-轨迹视频.mp4")
    if os.path.isfile(p):
        try:
            import cv2
            cap = cv2.VideoCapture(p)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()
            if frames > 0 and w > 0:
                passed += 1
                print("  [PASS] 轨迹视频为合法 mp4（%d 帧 / %.0f fps / %dx%d）"
                      "——解码在 qr-video-decode 页面进行，属人工验收项" % (frames, fps, w, h))
            else:
                failed.append("轨迹视频：无法读取帧（文件可能损坏）")
                print("  [FAIL] 轨迹视频：无法读取帧")
        except Exception as e:
            failed.append("轨迹视频：%s" % e)
            print("  [FAIL] 轨迹视频：%s" % e)
    return passed, failed


# ============================== 验收检查表 ==============================

README_TMPL = """# JZToolsHub 验收测试数据

> 用途：配合 `docs\\guide\\干净机器部署验收手册.md` 做功能验收。**所有数据均为虚构测试数据**
> （身份证号/手机号/单位人员均非真实信息）。
> 生成脚本：`tools\\e2e\\make-acceptance-testdata.py`（可重新生成）。本目录**随仓库提交**（约 540 KB，2026-09-24 实测），
> 因此拿到仓库就有经过校验的验收数据；发布介质另见 `deploy\\JZToolsHub-验收测试数据-v<日期>.zip`。

## 怎么用

1. 按验收手册装好主体 + 插件集（+ 需要的依赖组件包）。
2. 按下表逐插件走一遍：上传/粘贴本目录里的文件 → 对照「通过口径」。
3. 缺依赖组件的场景**故意不装**（如不装 openpyxl）再走一遍，应看到"功能降级 + 明确提示"，而不是报错堆栈。

## 逐插件清单

| 插件 | 测试文件 | 操作 | 通过口径 |
| --- | --- | --- | --- |
| **admin**（管理后台） | `admin/批量导入-单位.xlsx`、`批量导入-部门.xlsx`、`批量导入-人员.xlsx` | 后台 → 单位/部门/人员 → 批量导入（先导单位，再导部门，最后导人员） | 导入成功条数与文件行数一致；重复导入按所选方式处理 |
| admin | `admin/批量导入-人员-含错误行.xlsx` | 同上 | **逐行报错**（登录名含空格、部门不存在、密码过短），不整批失败；错误行位置可定位 |
| admin | 任意列表页 → 批量导出 | 导出 xlsx 并打开 | 列与模板一致；**未装 openpyxl 组件**时提示"仅剩 CSV"，不是报错 |
| **file-filter** | `file-filter/花名册.xlsx` | 上传 → 选保留字段（姓名、所属单位）→ 硬过滤 → 下载 | 输出只含这两列；行数不变；文件名带"过滤"标识 |
| file-filter | `file-filter/花名册.xls` | 同上 | 走 xlrd 路径；输出为 .xlsx（**未装 xlrd 组件**时提示该格式不可用） |
| file-filter | `file-filter/花名册.csv` | 同上 | CSV 读写正常（UTF-8-SIG / GBK 自适应） |
| file-filter | `花名册.xlsx` + 文本后处理（正则 `\\d{4}(\\d{4})\\d{4}` → `****`） | 勾选正则脱敏 | 手机号中段被替换；表头也参与替换 |
| file-filter | 配置 API Key 后，用"大模型辅助"模式 | 表头写「联系手机」「所属单位」，保留字段写「手机号」「单位」 | 能语义匹配（缺 requests 组件时应提示"大模型辅助过滤不可用"） |
| **info-transfer** | `info-transfer/待传输-说明.txt` | 静态码模式 + **原件传输** → 出码 → 下载 → 再用"信息解析"上传该码 | 还原出的文件与原件**字节一致**、文件名一致 |
| info-transfer | `info-transfer/待传输-花名册.xlsx` | 静态码 + 精简传输 | 自动拆成多张码 → 下载 ZIP → 解析后表格内容与原件一致（不保留格式） |
| info-transfer | `qr-video-decode/预生成-说明-视频码.mp4` | 「信息解析」上传该视频 | 还原出 `待传输-说明.txt`（**需 zfec / cv2 / numpy / zxingcpp 组件**） |
| info-transfer | 同上文件 | 视频码模式现场生成 → 下载 mp4 → 播放 | 播放流畅、码面清晰；缺 cv2/numpy 时提示"视频码流模式不可用"（静态码不受影响） |
| **qr-video-decode** | `qr-video-decode/预生成-说明-视频码.mp4`、`info-transfer/预生成-说明-静态码.zip` | 上传 → 重组 | 视频与静态码都能还原出 `待传输-说明.txt`；缺 zfec 时提示不可用 |
| **trajectory-convert** | `trajectory-convert/轨迹表.xlsx` | 转换为视频码 → 下载 → 用「信息解析」解析 | 还原的表格内容与原件一致（60 行） |
| trajectory-convert | `trajectory-convert/预生成-轨迹视频.mp4` | 直接解析该视频 | 同上（免去现场出码；需 cv2/numpy/zxingcpp/zfec） |
| trajectory-convert | `trajectory-convert/轨迹表.xls` | 同上（静态码模式） | 走 xlrd 路径；缺 xlrd 时提示 |
| **trajectory-sketch** | `trajectory-sketch/轨迹表.xlsx` | 上传 → 选经纬度/编号列 → 生成轨迹图 | 出图且轨迹形态与数据一致（向东偏北）；缺 openpyxl 时提示 |
| **knowledge-base** | `knowledge-base/通知.docx`、`报表.xlsx`、`手册.pdf` | 上传 → 在线阅读 | 内容可读（标题/段落/表格）；缺 docx/pypdf 组件时对应格式提示不可用 |
| knowledge-base | `knowledge-base/旧版通知.doc`、`旧版报表.xls` | 上传 → 预览 | **需装 LibreOffice 离线组件**；未装时给出"安装离线组件"提示而非报错 |
| **character-graph** | `character-graph/人物档案.docx`、`.pdf`、`.txt` | 上传 → 抽取人物与关系 | 抽出 4 个人名与关系；缺 docx/pypdf 时提示对应格式不可用 |
| character-graph | 配置 API Key 后重跑 | 用大模型辅助抽取 | 关系更完整；缺 requests 时提示"大模型辅助抽取不可用" |
| **case-report** | `case-report/收网情况简报.txt` | 粘贴文本 → 解析 → 生成报表 → 导出 | 要素正确（案件名 1·7 专案 / 时间 / 主办大队 三大队 / 抓获人数 2 / 缴获物品 4 项）；导出 xlsx（缺 openpyxl 时仅 CSV） |
| case-report | `case-report/战果台账.xlsx` | 导入台账 | 数据入库、列表可见 |
| **shared-docs** | `shared-docs/文档正文.txt` | 新建文档 → 粘贴正文 → 保存 | 保存成功、列表可见、详情可读 |
| shared-docs | 上述文档 → 导入导出 | 导出 docx/xlsx | 可下载；缺 docx/openpyxl 时提示不可用 |
| **notice-board** | `notice-board/公告正文.txt` | 新建公告 → 保存 | 首页/公告列表可见；停用插件后不再展示 |
| **base64** | `base64/待编码文本.txt` | 粘贴文本 → 编码 → 再解码 | 解码结果与原文逐字一致（含中文与符号） |
| **json-formatter** | `json-formatter/待格式化.json` | 粘贴 → 格式化 / 压缩 / 校验 | 格式化后缩进正确；压缩后无多余空白；校验通过 |
| json-formatter | `json-formatter/待格式化-错误.json` | 粘贴 → 校验 | **报错并指出位置**（缺右括号），不是静默通过 |
| **md5-generator** | `md5-generator/待校验文本.txt` + `预期值.md` | 粘贴 → 计算 MD5 / HMAC-MD5（密钥 `jz-accept-key`） | 结果与 `预期值.md` 一致（大小写不敏感） |
| **map-marker** | `map-marker/坐标列表.csv` | 粘贴坐标 → 标点 / 生成移动轨迹 | 地图上出现 10 个点、轨迹连线正确（需高德 Key；无 Key 时提示配置） |
| **color-picker** | `color-picker/说明.md` | 取色 → 读 HEX/RGB/HSL → 复制 | 三个值互相一致；复制内容正确（交互式，无需文件） |

## 预生成产物（`--with-codes` 生成）

| 文件 | 说明 |
| --- | --- |
| `info-transfer/预生成-说明-静态码.zip`（或 `.png`） | `待传输-说明.txt` 的原件传输静态码（单张装不下则为多张 ZIP） |
| `qr-video-decode/预生成-说明-视频码.mp4` | 同一文件的视频码（每码多帧，约 5 秒/码） |
| `trajectory-convert/预生成-轨迹视频.mp4` | 轨迹表的视频码（trajectory-convert 自己的信封协议） |

预生成产物的意义：**不必先跑通出码**就能验收解码链路（也能在未装 cv2 的机器上验静态码解码）。

## 自校验（生成后已跑过，可随时重跑）

```powershell
python tools\\e2e\\make-acceptance-testdata.py --verify --base http://127.0.0.1:5000
```

| 校验项 | 口径 | 结果 |
| --- | --- | --- |
| 静态码 PNG → 说明文本 | 解码 + 导出，与原文件 **sha256 一致** | ✅ 通过 |
| 二维码视频 → 说明文本 | 同上（视频解码链路） | ✅ 通过 |
| 轨迹视频 | 是合法 mp4 且帧数正常 | ✅ 通过（**实际解码在 qr-video-decode 页面由浏览器完成，属人工验收项**） |

> 轨迹视频为什么不能自动校验：它用的是 trajectory-convert 自己的 QR-transfer 信封（`jzt` 与
> info-transfer 不同），且 qr-video-decode 的 `/reassemble` 只接收**浏览器已解码的分块**。
> 人工验收时在该页面上传这个 mp4，应能重组出轨迹 JSON。

## 注意

- 身份证号列在 Excel 里按**文本**存储（按数字存 18 位会丢精度，admin 导入会检测并提示）。
- 表格里的数据是虚构的，但**格式与真实业务一致**（含 18 位号码、11 位手机号），便于验证脱敏/过滤规则。
- `轨迹表.xlsx` 的列名按 trajectory-convert **默认字段**（`开始时间` / `经度` / `纬度`）：列名不一致时转换会以
  "未能在表头中找到字段"失败——这不是缺陷，改表头或在插件 `config.json` 里对齐字段名即可。
- 视频码（mp4）需要 cv2 的编码器：优先 H.264（avc1），缺 OpenH264 时自动退到 mp4v；
  两者都不可用时插件会给出明确提示（不会静默产出坏文件）。
- 文件名含中文：用于同时验证"中文名不乱码"（包内条目名、上传下载、页面显示）。
"""


def write_readme(info):
    text = README_TMPL
    if info:
        text += "\n## 本次生成记录\n\n" + "".join("- %s\n" % x for x in info)
    return write_text("README.md", text)


# ============================== 主流程 ==============================

def build_fixtures():
    info = []
    # admin
    write_xlsx("admin/批量导入-单位.xlsx", ["单位名称", "描述"],
               [["管理单位", "市局管理部门"], ["一大队", "案件办理"], ["二大队", "技术侦查"],
                ["三大队", "勤务与专项行动"], ["综合科", "综合协调"], ["法制科", "案件审核"]],
               sheet="单位", widths={"单位名称": 22, "描述": 30})
    write_xlsx("admin/批量导入-部门.xlsx", ["所属单位", "部门名称", "描述"],
               [["一大队", "综合科", "内勤与文秘"], ["一大队", "办案一组", "案件办理"],
                ["一大队", "办案二组", "案件办理"], ["二大队", "技术科", "技术支撑"],
                ["二大队", "情报组", "情报研判"], ["三大队", "勤务组", "勤务调度"],
                ["法制科", "审核组", "案件审核"]],
               sheet="部门", widths={"所属单位": 16, "部门名称": 16, "描述": 26})
    user_headers = ["登录名", "姓名", "密码", "所属单位", "所属部门", "角色",
                    "身份证号码", "大模型BaseURL", "大模型APIKey", "模型名称", "权限点"]
    write_xlsx("admin/批量导入-人员.xlsx", user_headers,
               [["zhangwei", "张伟", "jz@123456", "一大队", "办案一组", "管理员",
                 "110105198703124531", "", "", "", "case-report、file-filter、knowledge-base"],
                ["lijing", "李静", "jz@123456", "一大队", "办案一组", "办案员",
                 "110105199008221246", "", "", "", "file-filter、knowledge-base"],
                ["wanglei", "王强", "jz@123456", "一大队", "办案二组", "办案员",
                 "110105198511093318", "", "", "", "trajectory-convert、trajectory-sketch"],
                ["zhaomin", "赵敏", "jz@123456", "二大队", "技术科", "办案员",
                 "110105198809307723", "", "", "", "info-transfer、qr-video-decode"],
                ["shilei", "施磊", "jz@123456", "综合科", "综合科", "办案员",
                 "110105198901116693", "", "", "", ""]],
               sheet="人员", widths={"权限点": 40, "身份证号码": 22, "大模型BaseURL": 28},
               text_cols=(1, 7))
    write_xlsx("admin/批量导入-人员-含错误行.xlsx", user_headers,
               [["chenchen", "陈晨", "jz@123456", "一大队", "办案一组", "办案员",
                 "110105199401026612", "", "", "", "file-filter"],
                ["chen chen", "陈晨2", "jz@123456", "一大队", "办案一组", "办案员",
                 "", "", "", "", "file-filter"],
                ["zhoufang", "周芳", "123", "二大队", "情报组", "办案员",
                 "", "", "", "", "file-filter"],
                ["sunlei", "孙磊", "jz@123456", "不存在大队", "技术科", "办案员",
                 "", "", "", "", "file-filter"]],
               sheet="人员", widths={"权限点": 40, "身份证号码": 22}, text_cols=(1, 7))
    info.append("admin：单位 6 行 / 部门 7 行 / 人员 5 行 + 含 3 类错误行的对照文件")

    # file-filter
    write_xlsx("file-filter/花名册.xlsx", ROSTER_HEADERS, ROSTER_ROWS, sheet="花名册",
               widths={"身份证号码": 22, "备注": 24}, text_cols=(3,))
    write_xls("file-filter/花名册.xls", ROSTER_HEADERS, ROSTER_ROWS, sheet="花名册")
    write_csv("file-filter/花名册.csv", ROSTER_HEADERS, ROSTER_ROWS)
    write_text("file-filter/说明.md", "# file-filter 测试数据\n\n%s\n\n- `花名册.xlsx` / `.xls` / `.csv`：同一份 20 行数据（含身份证号、手机号，用于过滤与脱敏）\n- 建议保留字段：`姓名`、`所属单位`；正则脱敏：`(\\d{3})\\d{4}(\\d{4})` → `$1****$2`\n" % ROSTER_NOTE)
    info.append("file-filter：花名册 20 行 × 3 种格式（xlsx/xls/csv）")

    # info-transfer
    write_text("info-transfer/待传输-说明.txt",
               "JZToolsHub 验收测试文件（info-transfer）\n\n"
               "用途：验证「封装 → 出码 → 解析」的完整链路。\n"
               "原件传输模式应能字节级还原本文件（含文件名）。\n"
               "生成时间：2026-09-21\n" + ("说明行：本条用于把文件撑到需要多张码的体量。\n" * 12))
    write_xlsx("info-transfer/待传输-花名册.xlsx", ROSTER_HEADERS, ROSTER_ROWS, sheet="花名册", text_cols=(3,))
    info.append("info-transfer：说明文本（多张码体量）+ 花名册表格")

    # trajectory-convert / trajectory-sketch
    write_xlsx("trajectory-convert/轨迹表.xlsx", TRACK_HEADERS, TRACK_ROWS, sheet="轨迹")
    write_xls("trajectory-convert/轨迹表.xls", TRACK_HEADERS, TRACK_ROWS, sheet="轨迹")
    write_xlsx("trajectory-sketch/轨迹表.xlsx", TRACK_HEADERS, TRACK_ROWS, sheet="轨迹")
    write_text("trajectory-sketch/说明.md", "# trajectory-sketch 测试数据\n\n%s\n\n- 经度列选 `经度`、纬度列选 `纬度`、编号列选 `编号`\n" % TRACK_NOTE)
    info.append("trajectory-convert/sketch：轨迹表 60 点（xlsx + xls）")

    # knowledge-base
    write_docx("knowledge-base/通知.docx", "关于开展专项行动的通知", DOC_NOTICE_PARAS,
               table=DOC_NOTICE_TABLE, subtitle="（本文件为测试数据）")
    write_xlsx("knowledge-base/报表.xlsx", REPORT_HEADERS, REPORT_ROWS, sheet="月报")
    write_docx("knowledge-base/_手册源.docx", "JZToolsHub 使用手册（节选）",
               [("h", "一、登录"), "打开 http://localhost:5000 ，使用管理员分配的账号登录。",
                ("h", "二、工具使用"), "首页选择工具卡片进入；上传文件后按提示操作。",
                ("h", "三、常见问题"), "若提示缺少依赖组件，请安装对应的依赖组件包后刷新页面。"],
               subtitle="（本文件为测试数据，用于生成 PDF）")
    info.append("knowledge-base：通知 docx（含表格）+ 报表 xlsx + 手册 PDF（源 docx 已保留）")

    # character-graph
    write_docx("character-graph/人物档案.docx", "人物档案（测试）",
               [("h", "一、基本情况"), PROFILE_TEXT.split("一、基本情况")[-1].split("二、关系摘要")[0].strip(),
                ("h", "二、关系摘要"), PROFILE_TEXT.split("二、关系摘要")[-1].strip()],
               subtitle="（本文件为测试数据，人名均为虚构）")
    write_docx("character-graph/_人物档案-源.docx", "人物档案（PDF 源）",
               [PROFILE_TEXT], subtitle="（用于生成 PDF）")
    write_text("character-graph/人物档案.txt", PROFILE_TEXT)
    info.append("character-graph：人物档案 docx / pdf（源已保留）/ txt")

    # case-report
    write_text("case-report/收网情况简报.txt", BRIEF_TEXT)
    write_xlsx("case-report/战果台账.xlsx",
               ["案件名", "时间", "主办大队", "抓获人数", "缴获物品", "涉案价值"],
               [["1·7专案", "2026-09-16", "三大队", 2, "烟叶4.6吨、烟丝2.1吨、私烟600件", "约25万元"],
                ["8·16系列盗窃案", "2026-08-20", "一大队", 5, "手机6部、现金3万元", "约8万元"]],
               sheet="战果台账", widths={"缴获物品": 30, "涉案价值": 14})
    info.append("case-report：收网简报（要素齐全）+ 战果台账 xlsx")

    # 纯前端工具插件（无后端）：数据用于粘贴，md5 给出可对照的预期值
    TOOL_TEXT = ("JZToolsHub 验收测试文本\n"
                 "包含中文、英文、数字 1234567890 与符号 !@#$%^&*()_+-=[]{}|;:,.\n"
                 "第二行：用于 base64 编码/解码往返、MD5 计算与校验。\n")
    write_text("base64/待编码文本.txt", TOOL_TEXT)
    write_text("md5-generator/待校验文本.txt", TOOL_TEXT)
    import hashlib as _hl, hmac as _hmac
    md5_hex = _hl.md5(TOOL_TEXT.encode("utf-8")).hexdigest()
    hmac_hex = _hmac.new(b"jz-accept-key", TOOL_TEXT.encode("utf-8"), _hl.md5).hexdigest()
    write_text("md5-generator/预期值.md",
               "# md5-generator 预期值\n\n文件：`待校验文本.txt`（UTF-8，含结尾换行）\n\n"
               "| 算法 | 参数 | 预期结果 |\n| --- | --- | --- |\n"
               "| MD5 | — | `%s` |\n"
               "| HMAC-MD5 | 密钥 `jz-accept-key` | `%s` |\n\n"
               "> 页面上算出的值与上表一致才算通过（大小写不敏感）。\n" % (md5_hex, hmac_hex))
    write_text("json-formatter/待格式化.json",
               '{"name":"JZToolsHub","version":"2.3.13","tags":["工具","离线"],'
               '"nested":{"level":1,"items":[{"id":1,"ok":true},{"id":2,"ok":false}]},'
               '"中文键":"中文值","count":42}')
    write_text("json-formatter/待格式化-错误.json",
               '{"name":"缺少右括号","items":[1,2,3}')
    write_text("map-marker/坐标列表.csv",
               "编号,经度,纬度,时间\n" + "".join(
                   "P%02d,%.6f,%.6f,2026-09-18 08:%02d:00\n" % (i, 116.3975 + i * 0.0007,
                                                                 39.9087 + i * 0.0004, i)
                   for i in range(1, 11)))
    write_text("color-picker/说明.md",
               "# color-picker 测试数据\n\n本插件为交互式取色器，无需输入文件：\n\n"
               "1. 在取色区选一个颜色，读 HEX / RGB / HSL 三个值；\n"
               "2. 把 HEX 值粘回输入框，确认三个值互相一致；\n"
               "3. 点「一键复制」，粘贴到记事本核对。\n")
    info.append("纯前端工具：base64/md5 文本、json 正误样例、坐标列表、取色器说明（md5 附预期值）")

    # shared-docs / notice-board
    write_text("shared-docs/文档正文.txt",
               "共享文档测试正文\n\n一、背景\n本文件用于验证共享文档的创建、保存、列表与详情展示。\n\n"
               "二、要求\n1. 保存后列表可见；\n2. 详情内容与本文一致；\n3. 导出功能可用（docx/xlsx）。\n")
    write_text("notice-board/公告正文.txt",
               "公告（测试）\n\n各部门：\n定于本周五 15:00 在三楼会议室召开专项行动推进会，请准时参加。\n\n综合科\n2026年9月21日\n")
    info.append("shared-docs / notice-board：正文文本各一份")
    return info


def build_convertibles():
    """用 LibreOffice 生成旧格式 .doc 与 PDF；缺 LibreOffice 时跳过并提示。"""
    info, skipped = [], []
    soffice = find_soffice()
    if not soffice:
        return info, ["未找到 LibreOffice：跳过 .doc 与 PDF 生成（可用离线组件包或本机 LibreOffice 重跑）"]
    tmp = os.path.join(ROOT, "build", "_accept-conv")
    jobs = [
        ("knowledge-base/通知.docx", "doc", "knowledge-base/旧版通知.doc"),
        ("knowledge-base/_手册源.docx", "pdf", "knowledge-base/手册.pdf"),
        ("character-graph/_人物档案-源.docx", "pdf", "character-graph/人物档案.pdf"),
    ]
    for src_rel, target, dst_rel in jobs:
        src = os.path.join(OUT, src_rel)
        got = convert_with_soffice(soffice, src, target, tmp)
        if not got:
            skipped.append("%s → %s 转换失败" % (src_rel, target))
            continue
        dst = ensure(os.path.join(OUT, dst_rel))
        shutil.copyfile(got, dst)
        info.append("%s（%d KB）" % (dst_rel, os.path.getsize(dst) // 1024))
    # 旧版 Excel 用 xlwt 直接写（不依赖 LibreOffice）
    write_xls("knowledge-base/旧版报表.xls", REPORT_HEADERS, REPORT_ROWS, sheet="月报")
    info.append("knowledge-base/旧版报表.xls（xlwt 直接生成）")
    return info, skipped


def main():
    ap = argparse.ArgumentParser(description="生成验收测试数据")
    ap.add_argument("--with-codes", action="store_true", help="用运行中的实例预生成二维码/视频产物")
    ap.add_argument("--base", default="http://127.0.0.1:5000", help="实例地址（默认本机 5000）")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--keep", action="store_true", help="保留已有 testdata（默认先清空）")
    ap.add_argument("--verify", action="store_true",
                    help="对预生成产物做往返校验（解码 → 导出 → 与原件比对）")
    args = ap.parse_args()

    # 只做校验时不动已有产物（否则会把要校验的预生成文件清掉）
    if not args.keep and os.path.isdir(OUT) and not (args.verify and not args.with_codes):
        shutil.rmtree(OUT)
    os.makedirs(OUT, exist_ok=True)

    info = build_fixtures()
    conv_info, skipped = build_convertibles()
    info += conv_info

    if args.with_codes or args.verify:
        app = App(args.base, args.user, args.password)
    if args.with_codes:
        gen_codes(app, info)
    if args.verify:
        print("往返校验：")
        ok, bad = verify(app)
        print("  ==== 汇总：%d 项通过 / %d 项失败 ====" % (ok, len(bad)))
        for b in bad:
            print("  ! %s" % b)

    write_readme(info)
    print("已生成验收测试数据：%s" % OUT)
    for x in info:
        print("  + %s" % x)
    for x in skipped:
        print("  ! %s" % x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
