# -*- coding: utf-8 -*-
r"""本地文件能力台架（/local-api/fs/*，20261016 数据迁移桌面端适配）。

覆盖（客户端能力层，隔离数据根 + create_capability_app 真实装配）：
  A  壳自证门：无 Cookie 全端点 403、boot 错/对 token。
  B  save-dir：默认=客户端 downloads、合法设置并写盘 config.json、
     非法（相对路径/不存在）400、null 重置、跨"重启"（二次装配）仍生效。
  C  pick-dir：JZCLIENT_NO_DIALOG=1 下优雅不可用（不挂不崩）。
  D  save-file：正常落盘、文件名净化（路径穿越/空名）、重名 (n) 后缀、
     自设目录被删 400、默认目录自动补建、超限 413。
  E  reveal：目录内已存在文件放行（monkeypatch subprocess 不真开窗）、
     目录外/不存在/前缀同名的兄弟目录拒绝。

用法：
    python tools\e2e\fs-save-tests.py
退出码：0 = 全过；1 = 有 FAIL。
"""

import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


# ============================================================
# 装配（隔离数据根）
# ============================================================

WORK = tempfile.mkdtemp(prefix="jz-fs-")
CDIR = os.path.join(WORK, "clientdata")
os.environ["JZCLIENT_DATA_DIR"] = CDIR

from client_config import load_build_config, save_client_config  # noqa: E402

BCFG = load_build_config()
save_client_config(BCFG, {"remote_address": "http://127.0.0.1:1"})

import capability_app as cap  # noqa: E402


def make_app(token):
    return cap.create_capability_app(
        build_cfg=BCFG, client_cfg={"remote_address": "http://127.0.0.1:1"},
        address=None, static_dir=os.path.join(ROOT, "static"),
        local_plugins_dir=os.path.join(CDIR, "local-plugins"),
        fallback_file=os.path.join(ROOT, "client", "fallback.html"),
        data_dir=CDIR, log=lambda m: None, shell_token=token)


APP = make_app("fs-run-token-1")
ANON = APP.test_client()
C = APP.test_client()

DEFAULT_DIR = os.path.join(CDIR, "downloads")
PICKED_DIR = os.path.join(WORK, "pickme")
os.makedirs(PICKED_DIR, exist_ok=True)


def boot(client, token):
    r = client.get("/shell-boot?_shell=%s&next=/" % token)
    kv = (r.headers.get("Set-Cookie") or "").split(";")[0]
    if "=" in kv:
        k, v = kv.split("=", 1)
        client.set_cookie(k, v)
    return r


def cfg_on_disk():
    with open(os.path.join(CDIR, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# A. 壳自证门
# ============================================================

def part_a():
    print("==> A. 壳自证门（无 Cookie 一律 403）")
    for method, path, kw in [
        ("get", "/local-api/fs/save-dir", {}),
        ("post", "/local-api/fs/save-dir", {"json": {"dir": None}}),
        ("post", "/local-api/fs/pick-dir", {"json": {}}),
        ("post", "/local-api/fs/save-file", {"data": b""}),
        ("post", "/local-api/fs/reveal", {"json": {"path": "x"}}),
    ]:
        r = getattr(ANON, method)(path, **kw)
        check("A %s %s 无 Cookie 403" % (method.upper(), path), r.status_code == 403,
              "实得 %s" % r.status_code)
    r = boot(C, "wrong-token")
    check("A boot 错 token 403", r.status_code == 403)
    r = boot(C, "fs-run-token-1")
    check("A boot 对 token → 302 + 种 Cookie", r.status_code == 302, "实得 %s" % r.status_code)


# ============================================================
# B. save-dir
# ============================================================

def part_b():
    print("==> B. save-dir 读取/设置/重置/持久化")
    r = C.get("/local-api/fs/save-dir")
    d = r.get_json()
    check("B1 默认：dir=null、default=downloads",
          r.status_code == 200 and d["dir"] is None and d["default_dir"] == DEFAULT_DIR
          and d["effective_dir"] == DEFAULT_DIR, json.dumps(d, ensure_ascii=False)[:90])

    r = C.post("/local-api/fs/save-dir", json={"dir": PICKED_DIR})
    d = r.get_json()
    check("B2 设置合法目录 → 生效", r.status_code == 200 and d["effective_dir"] == PICKED_DIR)
    check("B2 config.json 实际写盘", cfg_on_disk().get("data_save_dir") == PICKED_DIR)

    for bad in ("not\\abs", "sub/dir", os.path.join(WORK, "no-such-dir")):
        r = C.post("/local-api/fs/save-dir", json={"dir": bad})
        check("B3 非法目录 400：%s" % bad, r.status_code == 400, "实得 %s" % r.status_code)
    check("B3 非法设置不落盘", cfg_on_disk().get("data_save_dir") == PICKED_DIR)

    r = C.post("/local-api/fs/save-dir", json={"dir": None})
    d = r.get_json()
    check("B4 null 重置 → 回默认", r.status_code == 200 and d["dir"] is None
          and d["effective_dir"] == DEFAULT_DIR)
    check("B4 config.json 键已清除", "data_save_dir" not in cfg_on_disk())

    APP2 = make_app("fs-run-token-1")     # 二次装配模拟客户端重启
    c2 = APP2.test_client()
    C.post("/local-api/fs/save-dir", json={"dir": PICKED_DIR})
    boot(c2, "fs-run-token-1")
    d = c2.get("/local-api/fs/save-dir").get_json()
    check("B5 重启后配置仍在", d["effective_dir"] == PICKED_DIR, json.dumps(d, ensure_ascii=False)[:90])
    C.post("/local-api/fs/save-dir", json={"dir": None})


# ============================================================
# C. pick-dir
# ============================================================

def part_c():
    print("==> C. pick-dir（台架禁用对话框）")
    os.environ["JZCLIENT_NO_DIALOG"] = "1"
    try:
        r = C.post("/local-api/fs/pick-dir", json={"seed": PICKED_DIR})
        d = r.get_json()
        check("C1 禁用下优雅不可用（503 + 说明）", r.status_code == 503 and not d.get("ok"),
              json.dumps(d, ensure_ascii=False)[:90])
    finally:
        os.environ.pop("JZCLIENT_NO_DIALOG", None)


# ============================================================
# D. save-file
# ============================================================

def post_save_file(client, filename, payload, **kw):
    # 手工拼 multipart：本机 flask/werkzeug 组合的 test_client 传 files= 有兼容问题，
    # 且手工报文对"声明超限"（environ_overrides 改 CONTENT_LENGTH）也更可控。
    b = "----jzfsboundary20261016"
    head = (
        "--%s\r\nContent-Disposition: form-data; name=\"filename\"\r\n\r\n%s\r\n"
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n" % (b, filename, b, filename)
    )
    raw = head.encode("utf-8") + payload + ("\r\n--%s--\r\n" % b).encode("ascii")
    return client.post("/local-api/fs/save-file", data=raw,
                       headers={"Content-Type": "multipart/form-data; boundary=%s" % b}, **kw)


def part_d():
    print("==> D. save-file 落盘/净化/重名/缺目录/超限")
    C.post("/local-api/fs/save-dir", json={"dir": PICKED_DIR})

    r = post_save_file(C, "迁移包.jzdata", b"JZDATA1-fixture")
    d = r.get_json()
    p1 = d.get("path", "")
    check("D1 正常落盘到自设目录", r.status_code == 200
          and os.path.dirname(p1) == PICKED_DIR and os.path.isfile(p1), p1)
    with open(p1, "rb") as f:
        check("D1 内容一致", f.read() == b"JZDATA1-fixture")
    check("D1 回显 filename/size", d.get("filename") == "迁移包.jzdata" and d.get("size") == 15)

    r = post_save_file(C, "..\\..\\evil.txt", b"x")
    d = r.get_json()
    check("D2 路径穿越净化为 basename",
          r.status_code == 200 and os.path.basename(d.get("path", "")) == "evil.txt"
          and os.path.dirname(d.get("path", "")) == PICKED_DIR, d.get("path", ""))
    r = post_save_file(C, "a/b.txt", b"x")
    check("D2 正斜杠同样取 basename", os.path.basename(r.get_json().get("path", "")) == "b.txt")

    r = post_save_file(C, "迁移包.jzdata", b"second")
    d = r.get_json()
    check("D3 重名自动 (1) 后缀且不覆盖", r.status_code == 200
          and os.path.basename(d.get("path", "")) == "迁移包 (1).jzdata")
    with open(p1, "rb") as f:
        check("D3 原文件未被覆盖", f.read() == b"JZDATA1-fixture")

    r = post_save_file(C, "...", b"x")
    check("D4 纯点文件名 400", r.status_code == 400, "实得 %s" % r.status_code)

    gone = os.path.join(WORK, "gonedir")
    os.makedirs(gone, exist_ok=True)
    C.post("/local-api/fs/save-dir", json={"dir": gone})
    shutil.rmtree(gone)
    r = post_save_file(C, "x.bin", b"x")
    check("D5 自设目录被删 → 400 提示重设", r.status_code == 400
          and "重新设置" in (r.get_json().get("error") or ""), r.get_data(as_text=True)[:80])

    shutil.rmtree(DEFAULT_DIR, ignore_errors=True)
    C.post("/local-api/fs/save-dir", json={"dir": None})
    r = post_save_file(C, "默认落盘.bin", b"ok")
    check("D6 默认目录缺席自动补建并落盘", r.status_code == 200
          and os.path.dirname(r.get_json().get("path", "")) == DEFAULT_DIR)

    r = post_save_file(C, "big.bin", b"x", environ_overrides={"CONTENT_LENGTH": str(600 * 1024 * 1024)})
    check("D7 声明超限 → 413", r.status_code == 413, "实得 %s" % r.status_code)


# ============================================================
# E. reveal
# ============================================================

class _FakePopen:
    calls = []

    @classmethod
    def reset(cls):
        cls.calls = []

    @classmethod
    def popen(cls, cmd, *a, **k):
        cls.calls.append(cmd)
        return object()


def part_e():
    print("==> E. reveal（monkeypatch subprocess，不真开资源管理器）")
    real_subprocess = sys.modules.get("subprocess")

    class _FakeSP:
        Popen = staticmethod(_FakePopen.popen)

    sys.modules["subprocess"] = _FakeSP
    try:
        target = os.path.join(PICKED_DIR, "定位我.jzdata")
        with open(target, "wb") as f:
            f.write(b"reveal-me")
        C.post("/local-api/fs/save-dir", json={"dir": PICKED_DIR})
        _FakePopen.reset()
        r = C.post("/local-api/fs/reveal", json={"path": target})
        ok_cmd = _FakePopen.calls and ("explorer /select,\"%s\"" % target) == _FakePopen.calls[0]
        check("E1 目录内已存在文件 → explorer /select 定位",
              r.status_code == 200 and r.get_json().get("ok") and ok_cmd,
              repr(_FakePopen.calls[:1]))

        outside = os.path.join(WORK, "outside.txt")
        with open(outside, "wb") as f:
            f.write(b"no")
        _FakePopen.reset()
        r = C.post("/local-api/fs/reveal", json={"path": outside})
        check("E2 目录外路径拒绝", r.status_code == 400 and not _FakePopen.calls)

        r = C.post("/local-api/fs/reveal", json={"path": os.path.join(PICKED_DIR, "不存在.jzdata")})
        check("E3 目录内不存在的文件拒绝", r.status_code == 400)

        sibling = PICKED_DIR + "2"      # 前缀同名兄弟目录：防 startswith 误放行
        os.makedirs(sibling, exist_ok=True)
        sib_file = os.path.join(sibling, "s.txt")
        with open(sib_file, "wb") as f:
            f.write(b"s")
        _FakePopen.reset()
        r = C.post("/local-api/fs/reveal", json={"path": sib_file})
        check("E4 兄弟前缀目录拒绝", r.status_code == 400 and len(_FakePopen.calls) == 0)
    finally:
        sys.modules["subprocess"] = real_subprocess


if __name__ == "__main__":
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    bad = [name for name, ok, _d in RESULTS if not ok]
    print("\n===== %d/%d 过 =====" % (len(RESULTS) - len(bad), len(RESULTS)))
    for name in bad:
        print("  FAIL: %s" % name)
    sys.exit(1 if bad else 0)
