# -*- coding: utf-8 -*-
"""阶段 8 集成自测：假 admin 会话 + Flask test client，
覆盖 docx/xlsx 上传 → 按需 /preview（引擎渲染 + 磁盘缓存）→ 404 语义 → 删除清理；
另测 office_render 模块级渲染（docx/xlsx/xls/doc 四类，旧格式经 soffice 转换）。"""
import io
import json
import os
import sys
import time
import types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
sys.path.insert(0, BACKEND)

# ---- 伪造 jztools_admin（routes 顶层 try-import）----
admin = types.ModuleType("jztools_admin")
admin_routes = types.ModuleType("jztools_admin.routes")
admin_routes.get_session_user = lambda: {
    "username": "tester", "name": "测试管理员", "super_admin": True,
    "role_id": "role-admin", "unit_id": "", "department_id": ""}
admin_routes.set_operation = lambda op: None
admin.routes = admin_routes
sys.modules["jztools_admin"] = admin
sys.modules["jztools_admin.routes"] = admin_routes

import importlib  # noqa: E402
pkg = types.ModuleType("kb_backend_test")
pkg.__path__ = [BACKEND]
sys.modules["kb_backend_test"] = pkg
routes = importlib.import_module("kb_backend_test.routes")
office_render = importlib.import_module("kb_backend_test.office_render")

from flask import Flask  # noqa: E402

OUT = os.path.join(BACKEND, "out")
os.makedirs(OUT, exist_ok=True)

app = Flask(__name__)
routes.register(app)
client = app.test_client()

# ---------- 0) 模块级渲染：四类格式 ----------
import openpyxl  # noqa: E402
import docx  # noqa: E402

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "数据"
ws["A1"] = "区域"; ws["B1"] = "销售额"
ws["A2"] = "华东"
c = ws["B2"]; c.value = 1234567.891; c.number_format = "#,##0.00"
buf = io.BytesIO(); wb.save(buf)
xlsx_bytes = buf.getvalue()

d = docx.Document()
d.add_heading("集成测试文档", level=1)
d.add_paragraph("第一段正文内容。")
t = d.add_table(rows=1, cols=2); t.style = "Table Grid"
t.rows[0].cells[0].text = "甲"; t.rows[0].cells[1].text = "乙"
buf = io.BytesIO(); d.save(buf)
docx_bytes = buf.getvalue()

r = office_render.render_sheet(xlsx_bytes)
assert r["kind"] == "sheet" and "1,234,567.89" in r["html"], "xlsx 渲染失败"
print("module render sheet OK | html %dB | warnings=%s" % (len(r["html"]), r["warnings"]))
r = office_render.render_word(docx_bytes)
assert r["kind"] == "word" and "集成测试文档" in r["html"] and "<style>" in r["html"]
print("module render word OK | html %dB" % len(r["html"]))

# 旧格式：用 soffice 把 xlsx→xls、docx→doc（可用才测）
xls_bytes = doc_bytes = None
soffice = office_render.soffice_available()
print("soffice available:", soffice)
if soffice:
    import subprocess, tempfile, shutil
    work = tempfile.mkdtemp(prefix="kbfmt-")
    try:
        for src_bytes, src_ext, dst_ext in ((xlsx_bytes, "xlsx", "xls"), (docx_bytes, "docx", "doc")):
            sp = os.path.join(work, "f." + src_ext)
            with open(sp, "wb") as f:
                f.write(src_bytes)
            subprocess.run([os.environ.get("XHR_SOFFICE") or office_render._configured_soffice(),
                            "--headless", "--convert-to", dst_ext, "--outdir", work, sp],
                           capture_output=True, timeout=120,
                           startupinfo=None)
            dp = os.path.join(work, "f." + dst_ext)
            if os.path.isfile(dp):
                with open(dp, "rb") as f:
                    if dst_ext == "xls":
                        xls_bytes = f.read()
                    else:
                        doc_bytes = f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if xls_bytes:
        r = office_render.render_sheet(xls_bytes)
        assert r["kind"] == "sheet" and "<table" in r["html"]
        print("module render legacy xls OK | html %dB | warnings=%s" % (len(r["html"]), r["warnings"]))
    if doc_bytes:
        r = office_render.render_word(doc_bytes)
        # 注：LibreOffice doc 归一化回环对本例的微型表格会退化为制表符段落
        # （内容保留）；真实链路 .doc 上传走 doc_convert 转换件（docx），不经过此通道，
        # 故这里只断言内容保留与 kind 正确，不断言表格结构。
        assert r["kind"] == "word" and "集成测试文档" in r["html"] and "甲" in r["html"]
        print("module render legacy doc OK | html %dB | warnings=%s" % (len(r["html"]), r["warnings"]))

# ---------- 1) status ----------
r = client.get("/api/knowledge-base/status")
data = r.get_json()
print("status:", r.status_code, "| office_preview:", data.get("office_preview"),
      "| office_render:", data.get("office_render"))
assert data.get("office_preview") is True

# ---------- 2) 上传 xlsx / docx → /preview ----------
def upload_and_preview(name, blob, expect_kind, expect_text):
    r = client.post("/api/knowledge-base/files", data={
        "file": (io.BytesIO(blob), name), "name": name.rsplit(".", 1)[0],
    }, content_type="multipart/form-data")
    up = r.get_json()
    assert r.status_code == 200, up
    assert "pdf_status" not in up and "html_status" not in up
    fid = up["id"]
    t0 = time.time()
    r = client.get(f"/api/knowledge-base/files/{fid}/preview")
    pv = r.get_json()
    assert r.status_code == 200 and pv["ok"] and pv["kind"] == expect_kind, pv.get("error")
    assert expect_text in pv["html"], "预览 HTML 缺少预期内容"
    first_ms = (time.time() - t0) * 1000
    # 第二次请求应命中缓存（cached 标记）
    r2 = client.get(f"/api/knowledge-base/files/{fid}/preview")
    pv2 = r2.get_json()
    assert r2.status_code == 200 and pv2.get("cached") is True
    cache_file = f"{fid}.preview.json"
    assert os.path.isfile(os.path.join(routes.FILES_DIR, cache_file))
    print(f"upload+preview {name}: kind={pv['kind']} first={first_ms:.0f}ms "
          f"html={len(pv['html'])}B cached=OK")
    return fid

fid_x = upload_and_preview("报表.xlsx", xlsx_bytes, "sheet", "1,234,567.89")
fid_d = upload_and_preview("文档.docx", docx_bytes, "word", "集成测试文档")

# ---------- 3) 非 Office 类 404 ----------
r = client.post("/api/knowledge-base/files", data={
    "file": (io.BytesIO("# md\n正文".encode("utf-8")), "note.md"), "name": "note",
}, content_type="multipart/form-data")
fid_m = r.get_json()["id"]
r = client.get(f"/api/knowledge-base/files/{fid_m}/preview")
assert r.status_code == 404, "非 Office 类应 404"
print("non-office preview 404 OK")

# 不存在 id
r = client.get("/api/knowledge-base/files/f-nonexist/preview")
assert r.status_code == 404
print("missing id preview 404 OK")

# ---------- 4) 旧格式直传（.xls 转换链 + 预览） ----------
if xls_bytes:
    r = client.post("/api/knowledge-base/files", data={
        "file": (io.BytesIO(xls_bytes), "老表格.xls"), "name": "老表格",
    }, content_type="multipart/form-data")
    up = r.get_json()
    assert r.status_code == 200, up
    assert up["item"]["converted"] is True and up["item"]["ext"] == "xlsx"
    fid_old = up["id"]
    r = client.get(f"/api/knowledge-base/files/{fid_old}/preview")
    pv = r.get_json()
    assert r.status_code == 200 and pv["kind"] == "sheet"
    print("legacy .xls upload -> converted -> preview OK")

# ---------- 5) 删除清理（含预览缓存） ----------
for fid in (fid_x, fid_d, fid_m):
    r = client.delete(f"/api/knowledge-base/files/{fid}")
    assert r.status_code == 200
leftover = [f for f in os.listdir(routes.FILES_DIR)
            if any(f.startswith(x) for x in (fid_x, fid_d, fid_m))]
assert not leftover, leftover
print("delete cleanup OK (no leftovers incl. preview cache)")

print("ALL PHASE-8 ROUTE CHECKS DONE")
