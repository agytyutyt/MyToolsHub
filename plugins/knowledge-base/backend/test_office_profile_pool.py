# -*- coding: utf-8 -*-
r"""P2-21 回归自测：vendor profile 提供者接缝 + office_render 持久 profile 池。

对应 vendor/README.md 补丁清单第 6、7 条（上游整目录同步时的验收依据）。
全程**不依赖 LibreOffice**：
  - find_soffice 只查文件存在 → 用空文件假 soffice.exe；
  - 转换执行器用注入的 runner / monkeypatch 的 _run_soffice（写假输出文件），
    只验证 profile 路径选择、release(ok) 语义、异常兜底与池机制本身。

数据根隔离：导入 office_render 之前把 JZTOOLS_DATA_ROOT 指向临时目录
（口径同 test_routes_preview.py；本套件一切落盘只允许发生在该临时目录内）。
运行：python test_office_profile_pool.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(BACKEND, os.pardir, os.pardir, os.pardir))
sys.path.insert(0, REPO)
sys.path.insert(0, BACKEND)

_TMP_ROOT = tempfile.mkdtemp(prefix="kb_pool_test_")
os.environ["JZTOOLS_DATA_ROOT"] = _TMP_ROOT

import office_render  # noqa: E402  （导入即接线 profile 池；vendor/ 由它注入 sys.path）
import jztools_data  # noqa: E402
from xhr.core import XhrError  # noqa: E402
from xhr.normalize import libreoffice as xl_lo  # noqa: E402
import dhr.normalize as dhr_norm  # noqa: E402

# 假 soffice（find_soffice 只判定 isfile）
_SOFFICE = os.path.join(_TMP_ROOT, "soffice.exe")
open(_SOFFICE, "w").close()

_PROFILE_ARG_PREFIX = "-env:UserInstallation=file:///"

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond)))
    line = "[%s] %s" % ("PASS" if cond else "FAIL", name)
    if not cond and detail:
        line += "  | " + detail
    print(line)
    return bool(cond)


def _profile_from_args(args):
    for a in args:
        if a.startswith(_PROFILE_ARG_PREFIX):
            return a[len(_PROFILE_ARG_PREFIX):]
    return None


def _make_runner(fail=None, content=b"fake-xlsx"):
    """假执行器：正常时写出 <outdir>/<stem>.xlsx；fail='raise' 抛 TIMEOUT；
    fail='no_output' 静默返回（覆盖「退出码 0 但无产出」）。"""
    calls = []

    def runner(cmd, args, timeout_ms):
        calls.append(list(args))
        if fail == "raise":
            raise XhrError("TIMEOUT", "fake timeout")
        if fail == "no_output":
            return
        outdir = args[args.index("--outdir") + 1]
        stem = os.path.splitext(os.path.basename(args[-1]))[0]
        with open(os.path.join(outdir, stem + ".xlsx"), "wb") as f:
            f.write(content)

    return runner, calls


def _out_dir(tag):
    d = os.path.join(_TMP_ROOT, "out-" + tag)
    os.makedirs(d, exist_ok=True)
    return d


# ===================== 1) xhr 接缝：默认路径 = 上游行为 =====================

def test_seam_default():
    ok = True
    xl_lo.set_profile_provider(None)          # 抵消 office_render 导入时的接线
    in_path = os.path.join(_TMP_ROOT, "数据.xls")

    runner, calls = _make_runner()
    out = xl_lo.convert_to_xlsx(in_path, _out_dir("d1"), soffice_path=_SOFFICE, runner=runner)
    prof = _profile_from_args(calls[0])
    ok &= check("默认：临时 profile 带 xhr-lo- 前缀（上游行为）",
                prof is not None and os.path.basename(prof).startswith("xhr-lo-"), repr(prof))
    ok &= check("默认：转换成功且输出存在", os.path.isfile(out), repr(out))
    ok &= check("默认：转换后临时 profile 已删除", not os.path.exists(prof), repr(prof))

    # 失败（无产出）→ CORRUPTED 且 profile 仍清理
    runner2, calls2 = _make_runner(fail="no_output")
    raised = None
    try:
        xl_lo.convert_to_xlsx(in_path, _out_dir("d2"), soffice_path=_SOFFICE, runner=runner2)
    except XhrError as e:
        raised = e
    prof2 = _profile_from_args(calls2[0])
    ok &= check("默认：无产出抛 CORRUPTED（铁律 2）",
                raised is not None and getattr(raised, "code", "") == "CORRUPTED", repr(raised))
    ok &= check("默认：失败后临时 profile 仍清理", not os.path.exists(prof2), repr(prof2))
    return ok


# ===================== 2) xhr 接缝：租用持久 profile =====================

def test_seam_leased():
    ok = True
    lease_dir = os.path.join(_TMP_ROOT, "persist-prof")
    os.makedirs(lease_dir, exist_ok=True)
    marker = os.path.join(lease_dir, "marker.txt")
    open(marker, "w").close()
    released = []
    xl_lo.set_profile_provider(
        lambda: (lease_dir, lambda okflag: released.append(bool(okflag))))
    in_path = os.path.join(_TMP_ROOT, "数据.xls")

    runner, calls = _make_runner()
    out = xl_lo.convert_to_xlsx(in_path, _out_dir("l1"), soffice_path=_SOFFICE, runner=runner)
    prof = _profile_from_args(calls[0])
    ok &= check("租用：-env:UserInstallation 指向租用槽位",
                prof == Path(lease_dir).as_posix(), repr(prof))
    ok &= check("租用：成功后槽位目录保留（不删除）",
                os.path.isfile(marker) and os.path.isfile(out))
    ok &= check("租用：成功 → release(True)", released == [True], repr(released))

    # 无产出 → CORRUPTED + release(False)
    runner2, _c = _make_runner(fail="no_output")
    raised = None
    try:
        xl_lo.convert_to_xlsx(in_path, _out_dir("l2"), soffice_path=_SOFFICE, runner=runner2)
    except XhrError as e:
        raised = e
    ok &= check("租用：无产出抛 CORRUPTED", raised is not None, repr(raised))
    ok &= check("租用：失败 → release(False)",
                released == [True, False], repr(released))

    # 执行器抛 TIMEOUT → release(False) 且异常类型保留
    runner3, _c = _make_runner(fail="raise")
    raised3 = None
    try:
        xl_lo.convert_to_xlsx(in_path, _out_dir("l3"), soffice_path=_SOFFICE, runner=runner3)
    except XhrError as e:
        raised3 = e
    ok &= check("租用：TIMEOUT 异常原样上抛",
                raised3 is not None and raised3.code == "TIMEOUT", repr(raised3))
    ok &= check("租用：TIMEOUT → release(False)",
                released == [True, False, False], repr(released))

    # release 自身抛异常 → 不得掩盖转换结果
    def bad_release(_okflag):
        raise RuntimeError("release boom")
    xl_lo.set_profile_provider(lambda: (lease_dir, bad_release))
    runner4, _c = _make_runner()
    out4 = xl_lo.convert_to_xlsx(in_path, _out_dir("l4"), soffice_path=_SOFFICE, runner=runner4)
    ok &= check("租用：release 抛错被兜住，转换结果不受影响", os.path.isfile(out4))
    return ok


# ===================== 3) xhr 接缝：取不到 / 池异常 → 降级临时 profile =====================

def test_seam_degrade():
    ok = True
    in_path = os.path.join(_TMP_ROOT, "数据.xls")
    cases = [
        ("提供者抛异常", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("提供者返回 None", lambda: None),
        ("提供者返回畸形元组（空路径）", lambda: ("", lambda _f: None)),
        ("提供者返回畸形元组（release 不可调用）", lambda: (os.path.join(_TMP_ROOT, "x"), None)),
    ]
    for i, (label, prov) in enumerate(cases):
        xl_lo.set_profile_provider(prov)
        runner, calls = _make_runner()
        try:
            out = xl_lo.convert_to_xlsx(in_path, _out_dir("g%d" % i), soffice_path=_SOFFICE, runner=runner)
            raised = None
        except Exception as e:  # noqa: BLE001
            out, raised = None, e
        prof = _profile_from_args(calls[0])
        ok &= check("降级[%s]：转换照常成功" % label, raised is None and out and os.path.isfile(out),
                    repr(raised))
        ok &= check("降级[%s]：回退临时 profile 且用完即删" % label,
                    prof is not None and os.path.basename(prof).startswith("xhr-lo-")
                    and not os.path.exists(prof), repr(prof))
    return ok


# ===================== 4) dhr 接缝（.doc → .docx） =====================

def test_dhr_seam():
    ok = True
    calls = []

    def fake_run_soffice(soffice, args, timeout_ms):
        calls.append(list(args))
        outdir = args[args.index("--outdir") + 1]
        with open(os.path.join(outdir, "input.docx"), "wb") as f:
            f.write(b"fake-docx")

    real_run = dhr_norm._run_soffice
    dhr_norm._run_soffice = fake_run_soffice
    try:
        # a) 默认路径
        dhr_norm.set_profile_provider(None)
        data = dhr_norm.normalize_doc_to_docx(b"fake-doc", soffice_path=_SOFFICE)
        prof = _profile_from_args(calls[-1])
        ok &= check("dhr 默认：临时 profile 带 dhr-lo- 前缀且已清理",
                    prof is not None and os.path.basename(prof).startswith("dhr-lo-")
                    and not os.path.exists(prof), repr(prof))
        ok &= check("dhr 默认：返回产物字节", data == b"fake-docx", repr(data))

        # b) 租用路径
        lease_dir = os.path.join(_TMP_ROOT, "persist-prof-doc")
        os.makedirs(lease_dir, exist_ok=True)
        released = []
        dhr_norm.set_profile_provider(
            lambda: (lease_dir, lambda okflag: released.append(bool(okflag))))
        data2 = dhr_norm.normalize_doc_to_docx(b"fake-doc", soffice_path=_SOFFICE)
        prof2 = _profile_from_args(calls[-1])
        ok &= check("dhr 租用：profile 指向槽位且成功后保留",
                    prof2 == Path(lease_dir).as_posix() and os.path.isdir(lease_dir), repr(prof2))
        ok &= check("dhr 租用：成功 → release(True)（含产物）",
                    released == [True] and data2 == b"fake-docx", repr(released))

        # c) 转换失败 → release(False)
        def fake_fail(soffice, args, timeout_ms):
            raise XhrError("TIMEOUT", "fake doc timeout")
        dhr_norm._run_soffice = fake_fail
        raised = None
        try:
            dhr_norm.normalize_doc_to_docx(b"fake-doc", soffice_path=_SOFFICE)
        except XhrError as e:
            raised = e
        ok &= check("dhr 租用：失败抛 TIMEOUT 且 release(False)",
                    raised is not None and released == [True, False], repr((raised, released)))
    finally:
        dhr_norm._run_soffice = real_run
        dhr_norm.set_profile_provider(None)
    return ok


# ===================== 5) 池机制：LIFO 租出 / 满额降级 / 失败擦除 / 归还 =====================

_LO_PROFILES = os.path.join(_TMP_ROOT, "plugins", "knowledge-base", "data", "lo_profiles")


def test_pool_mechanics():
    ok = True
    pool = office_render._LoProfilePool("t-slot", 2)
    l1 = pool.provider()
    l2 = pool.provider()
    l3 = pool.provider()
    ok &= check("池：两槽位可租出", l1 is not None and l2 is not None, repr((l1, l2)))
    ok &= check("池：满额第三请求 → None（降级信号）", l3 is None, repr(l3))
    if not (l1 and l2):
        return False
    ok &= check("池：槽位路径互异", l1[0] != l2[0], repr((l1[0], l2[0])))
    ok &= check("池：槽位位于数据根 lo_profiles 下",
                l1[0].startswith(_LO_PROFILES) and l2[0].startswith(_LO_PROFILES),
                repr((l1[0], l2[0])))
    ok &= check("池：租出时槽位目录已创建", os.path.isdir(l1[0]) and os.path.isdir(l2[0]))

    # release(True) → 槽位保留；release(False) → 槽位擦除
    marker1 = os.path.join(l1[0], "marker.txt")
    open(marker1, "w").close()
    l1[1](True)
    ok &= check("池：release(True) 槽位保留", os.path.isfile(marker1))
    marker2 = os.path.join(l2[0], "marker.txt")
    open(marker2, "w").close()
    l2[1](False)
    ok &= check("池：release(False) 槽位擦除", not os.path.exists(l2[0]))

    # 归还后可再租；LIFO：优先取回最近释放的槽位
    l4 = pool.provider()
    ok &= check("池：失败擦除的槽位被重建为干净目录（最近归还者先租出）",
                l4 is not None and l4[0] == l2[0] and os.path.isdir(l4[0])
                and not os.path.isfile(os.path.join(l4[0], "marker.txt")), repr(l4))
    l5 = pool.provider()
    ok &= check("池：随后取回先前成功归还的槽位（marker 保留）",
                l5 is not None and l5[0] == l1[0] and os.path.isfile(marker1), repr(l5))
    # LIFO 串行复用：刚归还的槽位应被下一次租用原样取回（热启动的直接依据）
    if l4:
        l4[1](True)
    l6 = pool.provider()
    ok &= check("池：串行复用最近释放的热槽位（LIFO 语义）",
                l6 is not None and l4 is not None and l6[0] == l4[0],
                repr((l6 and l6[0], l4 and l4[0])))
    if l6:
        l6[1](True)
    if l5:
        l5[1](True)
    return ok


# ===================== 6) 池机制：TTL 清扫 / 越界名清理 / 他物不伤 =====================

def test_pool_sweep():
    ok = True
    os.makedirs(_LO_PROFILES, exist_ok=True)
    old = time.time() - (office_render._LO_PROFILE_TTL_SECONDS + 86400)

    stale = os.path.join(_LO_PROFILES, "t2-slot-0")     # in-range 但 40 天未用
    os.makedirs(stale, exist_ok=True)
    f = os.path.join(stale, "f.txt")
    open(f, "w").close()
    os.utime(f, (old, old))
    os.utime(stale, (old, old))

    out_range = os.path.join(_LO_PROFILES, "t2-slot-9")  # 越界编号
    malformed = os.path.join(_LO_PROFILES, "t2-slot-abc")  # 畸形名
    os.makedirs(out_range, exist_ok=True)
    os.makedirs(malformed, exist_ok=True)

    fresh = os.path.join(_LO_PROFILES, "t2-slot-1")      # in-range 且新鲜 → 保留
    os.makedirs(fresh, exist_ok=True)

    other = os.path.join(_LO_PROFILES, "other-x")        # 非本池前缀 → 不伤
    os.makedirs(other, exist_ok=True)

    pool = office_render._LoProfilePool("t2-slot", 2)
    ok &= check("清扫触发：池就绪", pool._ensure_ready())
    ok &= check("清扫：过期槽位被删（TTL）", not os.path.exists(stale))
    ok &= check("清扫：越界槽位名被删", not os.path.exists(out_range))
    ok &= check("清扫：畸形槽位名被删", not os.path.exists(malformed))
    ok &= check("清扫：新鲜槽位保留", os.path.isdir(fresh))
    ok &= check("清扫：非本池目录不受影响", os.path.isdir(other))

    lease = pool.provider()
    ok &= check("清扫后仍可正常租用", lease is not None)
    if lease:
        lease[1](True)
    return ok


# ===================== 7) 池异常 → provider 返回 None（不抛） =====================

def test_pool_failure_degrades():
    ok = True
    pool = office_render._LoProfilePool("t3-slot", 2)
    blocker = os.path.join(_TMP_ROOT, "blocked-parent")
    open(blocker, "w").close()  # 父路径是文件 → makedirs 必失败
    pool._base_dir = lambda: os.path.join(blocker, "lo_profiles")
    raised = None
    try:
        lease = pool.provider()
    except Exception as e:  # noqa: BLE001
        lease, raised = None, e
    ok &= check("池：makedirs 失败 → provider 返回 None 且不抛",
                lease is None and raised is None, repr((lease, raised)))
    return ok


# ===================== 8) 池并发：2 槽 3 路 → 恰好 2 租 1 降级 =====================

def test_pool_concurrency():
    ok = True
    pool = office_render._LoProfilePool("t4-slot", 2)
    barrier = threading.Barrier(3)
    got = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        lease = pool.provider()
        with lock:
            got.append(lease)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    # 三个 worker 自同步（Barrier(3)），主线程不参与——否则 party 数不匹配会死锁
    for t in threads:
        t.join()

    leases = [g for g in got if g is not None]
    nones = [g for g in got if g is None]
    ok &= check("并发：3 路 → 恰好 2 个租约", len(leases) == 2, repr([g[0] if g else None for g in got]))
    ok &= check("并发：3 路 → 恰好 1 个降级 None", len(nones) == 1, repr(len(nones)))
    ok &= check("并发：租约槽位互异", len({l[0] for l in leases}) == 2)
    for l in leases:
        l[1](True)
    return ok


# ===================== 9) 接线与隔离护栏 =====================

def test_wiring():
    ok = True
    office_render._wire_profile_pools()     # 前面的用例改写过提供者，恢复真实接线
    ok &= check("接线：xhr 接缝 → _POOL_XLS.provider",
                xl_lo._profile_provider == office_render._POOL_XLS.provider)
    ok &= check("接线：dhr 接缝 → _POOL_DOC.provider",
                dhr_norm._profile_provider == office_render._POOL_DOC.provider)

    lease = office_render._POOL_XLS.provider()
    ok &= check("接线：池可租出且落在隔离数据根",
                lease is not None and lease[0].startswith(_TMP_ROOT), repr(lease))
    if lease:
        lease[1](True)

    ok &= check("隔离护栏：数据根 = 临时目录（本套件不碰真实数据根）",
                jztools_data.get_data_root() == _TMP_ROOT,
                repr(jztools_data.get_data_root()))
    return ok


if __name__ == "__main__":
    tests = [
        test_seam_default,
        test_seam_leased,
        test_seam_degrade,
        test_dhr_seam,
        test_pool_mechanics,
        test_pool_sweep,
        test_pool_failure_degrades,
        test_pool_concurrency,
        test_wiring,
    ]
    group_results = [t() for t in tests]
    n_ok = sum(1 for _n, o in _RESULTS if o)
    n_all = len(_RESULTS)
    print("\n%s（%d/%d 项断言，%d/%d 组）" % (
        "全部通过" if all(group_results) else "存在失败", n_ok, n_all,
        sum(group_results), len(group_results)))
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    sys.exit(0 if all(group_results) else 1)
