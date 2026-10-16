# -*- coding: utf-8 -*-
r"""P2-21 实测脚本：LibreOffice 持久 profile 池的冷/热耗时对照（**需真实 LibreOffice**）。

用法：
    set XHR_SOFFICE=D:\LibreOffice\program\soffice.exe
    python bench_office_profile_pool.py [N=3]

两臂对照（N 由 argv 指定，默认 3）：
  - 「改动前」：提供者置 None（上游行为——每次转换新建 profile、用完即删）→ N 次全冷；
  - 「改动后」：接入 office_render 持久 profile 池（全新空槽位）→ 第 1 次冷、第 2..N 次热。
  .xls（xhr）与 .doc（dhr）各跑一轮。
并发对照（ruling 验收②）：
  - 先跑一轮并发预热（LIFO 串行场景只热一个槽位——这正是 LIFO 的目的：
    不付第二槽的冷启动；并发首轮会付一次性的「第二槽冷启动」，留痕但不参与判定）；
  - 然后两槽皆热：先串行产出基线，再 2 线程并发转换（池的 2 个槽位同时在用）；
  - 并发产物 vs 串行基线：转 HTML 逐字比对（不串扰）；不退化 = 并发单路 ≤ 串行基线
    ×1.5 且墙钟 ≈ max(A,B)（真并行）——阈值针对**病态退化**（同槽冲突/共享 profile
    锁序列化会到 ≥2×），正常 CPU 竞争波动（本机复测 ±1~3s）不判退化。
数据根：本脚本强制 JZTOOLS_DATA_ROOT = 临时目录（每次启动重建），绝不触碰真实数据根。
"""
import os
import shutil
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台兜底（tools/ 统一处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(BACKEND, os.pardir, os.pardir, os.pardir))
sys.path.insert(0, REPO)
sys.path.insert(0, BACKEND)

_TMP_ROOT = os.path.join(tempfile.gettempdir(), "kb-lo-bench-root")
shutil.rmtree(_TMP_ROOT, ignore_errors=True)
os.makedirs(_TMP_ROOT, exist_ok=True)
os.environ["JZTOOLS_DATA_ROOT"] = _TMP_ROOT

import office_render  # noqa: E402
from xhr.normalize import libreoffice as xl_lo  # noqa: E402
import dhr.normalize as dhr_norm  # noqa: E402
import xhr as _xhr  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

# 测量用超时：生产默认 120_000（2026-10-16 起；xhr/normalize 的 soffice_timeout_ms），实测环境首冷
# 可能超过 60s——测量脚本必须让冷跑跑完，否则拿不到冷/热对照数据。
_TIMEOUT_MS = 300_000

# 「不退化」阈值：并发单路耗时 / 串行基线 ≤ 1.5。病态失效模式（两路撞同一 profile
# 触发锁等待、或共享锁把两路序列化）会到 ≥2×；正常 CPU 竞争波动远低于此。
_REGRESS_RATIO = 1.5

XLS_A = os.path.join(REPO, "testdata", "knowledge-base", "旧版报表.xls")
XLS_B = os.path.join(REPO, "testdata", "file-filter", "花名册.xls")
DOC_A = os.path.join(REPO, "testdata", "knowledge-base", "旧版通知.doc")
OUT = os.path.join(_TMP_ROOT, "bench-out")
os.makedirs(OUT, exist_ok=True)

_LO_PROFILES = os.path.join(_TMP_ROOT, "plugins", "knowledge-base", "data", "lo_profiles")


def _soffice():
    p = os.environ.get("XHR_SOFFICE") or None
    if p and os.path.isfile(p):
        return p
    return xl_lo.find_soffice()


def _timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return time.perf_counter() - t0, r


def _dir_size_mb(path):
    total = 0
    for r, _d, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total / (1024.0 * 1024.0)


def _slot_report():
    for slot in ("xhr-slot-0", "xhr-slot-1", "dhr-slot-0", "dhr-slot-1"):
        p = os.path.join(_LO_PROFILES, slot)
        if os.path.isdir(p):
            print("[bench] profile 槽位在校：%s  体积 %.1f MB" % (slot, _dir_size_mb(p)))
        else:
            print("[bench] profile 槽位缺失：%s" % slot)


def _xlsx_html(path):
    with open(path, "rb") as f:
        data = f.read()
    res = _xhr.convert(data, _xhr.ConvertOptions(mode="fragment", css_prefix="kbbench"))
    return res.html


def bench_xls(soffice, arm, n):
    """xls 臂：返回 [秒, ...]（每次转换耗时）。"""
    times = []
    for i in range(n):
        out_dir = os.path.join(OUT, "xls-%s-%d" % (arm, i))
        os.makedirs(out_dir, exist_ok=True)
        dt, out_path = _timed(lambda: xl_lo.convert_to_xlsx_guarded(
            XLS_A, out_dir, soffice_path=soffice, timeout_ms=_TIMEOUT_MS))
        ok = os.path.isfile(out_path)
        times.append(dt)
        print("[bench] xls  arm=%s run=%d  %.1fs  产出=%s" % (arm, i + 1, dt, "有" if ok else "无!"))
        if not ok:
            raise SystemExit("[bench] 未产出输出文件，铁律 2 失败")
    return times


def bench_doc(soffice, arm, n):
    times = []
    with open(DOC_A, "rb") as f:
        data = f.read()
    for i in range(n):
        dt, docx_bytes = _timed(lambda: dhr_norm.normalize_doc_to_docx(
            data, soffice_path=soffice, timeout_ms=_TIMEOUT_MS))
        times.append(dt)
        print("[bench] doc  arm=%s run=%d  %.1fs  产物=%dB" % (arm, i + 1, dt, len(docx_bytes)))
    return times


def _parallel_round(soffice, tag):
    """一轮 2 线程并发转换（A=XLS_A，B=XLS_B）→ (da, db, wall, pa, pb)。"""
    res = {}
    a_dir = os.path.join(OUT, "cc-%s-a" % tag)
    b_dir = os.path.join(OUT, "cc-%s-b" % tag)
    for d in (a_dir, b_dir):
        os.makedirs(d, exist_ok=True)

    def worker(name, src, out_dir):
        dt, p = _timed(lambda: xl_lo.convert_to_xlsx_guarded(
            src, out_dir, soffice_path=soffice, timeout_ms=_TIMEOUT_MS))
        res[name] = (dt, p)

    t1 = threading.Thread(target=worker, args=("A", XLS_A, a_dir))
    t2 = threading.Thread(target=worker, args=("B", XLS_B, b_dir))
    wall0 = time.perf_counter()
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    wall = time.perf_counter() - wall0
    da, pa = res["A"]
    db, pb = res["B"]
    return da, db, wall, pa, pb


def bench_concurrency(soffice):
    """2 路并发 vs 串行基线：产物 HTML 逐字比对 + 耗时记录（验收②）。"""
    # 预热轮：填满两槽（LIFO 串行后第二槽从未被租用过的情形在此一次性补齐）
    da_w, db_w, wall_w, _pa_w, _pb_w = _parallel_round(soffice, "warm")
    print("[bench] 并发预热轮（含一次性第二槽冷启动）：A %.1fs  B %.1fs（墙钟 %.1fs）"
          % (da_w, db_w, wall_w))

    # 串行基线（两槽皆热）
    a_dir = os.path.join(OUT, "cc-a")
    b_dir = os.path.join(OUT, "cc-b")
    for d in (a_dir, b_dir):
        os.makedirs(d, exist_ok=True)
    ta, pa = _timed(lambda: xl_lo.convert_to_xlsx_guarded(XLS_A, a_dir, soffice_path=soffice, timeout_ms=_TIMEOUT_MS))
    tb, pb = _timed(lambda: xl_lo.convert_to_xlsx_guarded(XLS_B, b_dir, soffice_path=soffice, timeout_ms=_TIMEOUT_MS))
    html_a_base = _xlsx_html(pa)
    html_b_base = _xlsx_html(pb)
    print("[bench] 并发基线：A %.1fs  B %.1fs（串行）" % (ta, tb))

    # 测量轮
    da, db, wall, pa2, pb2 = _parallel_round(soffice, "par")
    html_a_par = _xlsx_html(pa2)
    html_b_par = _xlsx_html(pb2)
    same_a = html_a_par == html_a_base
    same_b = html_b_par == html_b_base
    ratio_a = da / ta if ta else 1.0
    ratio_b = db / tb if tb else 1.0
    no_regress = ratio_a <= _REGRESS_RATIO and ratio_b <= _REGRESS_RATIO
    # 真并行：墙钟应 ≈ max(A,B)；若两路被共享锁序列化，墙钟会 ≈ A+B
    overlapped = wall <= max(da, db) + 1.0
    print("[bench] 并发 2 路：A %.1fs  B %.1fs（墙钟 %.1fs）" % (da, db, wall))
    print("[bench] 不串扰：A 产物 HTML 一致 = %s，B 产物 HTML 一致 = %s" % (same_a, same_b))
    print("[bench] 不退化：并发/串行 = A %.2fx、B %.2fx（阈值 ≤%.1fx） = %s"
          % (ratio_a, ratio_b, _REGRESS_RATIO, no_regress))
    print("[bench] 真并行：墙钟(%.1fs) ≤ max(A,B)+1(%.1fs)，而非 A+B(%.1fs) = %s"
          % (wall, max(da, db) + 1.0, ta + tb, overlapped))
    ok = same_a and same_b and no_regress and overlapped and os.path.isfile(pa2) and os.path.isfile(pb2)
    return ok


def main():
    soffice = _soffice()
    if not soffice:
        print("[bench] 未找到 LibreOffice（soffice），无法实测；跳过（exit 2）")
        return 2
    print("[bench] soffice=%s  N=%d  数据根=%s" % (soffice, N, _TMP_ROOT))

    # ---- 臂 1：改动前（上游行为，每次新建即删）→ N 次全冷 ----
    xl_lo.set_profile_provider(None)
    dhr_norm.set_profile_provider(None)
    before_xls = bench_xls(soffice, "before", N)
    before_doc = bench_doc(soffice, "before", N)

    # ---- 臂 2：改动后（持久 profile 池，全新槽位）→ 1 冷 + N-1 热 ----
    if os.path.isdir(_LO_PROFILES):
        print("[bench] 警告：profile 目录残留未清干净，after 臂首跑可能不冷：%s" % _LO_PROFILES)
    print("[bench] 说明：before 臂 run1 机器最冷（soffice 二进制未入缓存）；对照重点是 "
          "after run2+（热 profile）与 before 全冷。")
    xl_lo.set_profile_provider(office_render._POOL_XLS.provider)
    dhr_norm.set_profile_provider(office_render._POOL_DOC.provider)
    after_xls = bench_xls(soffice, "after", N)
    after_doc = bench_doc(soffice, "after", N)
    _slot_report()

    # ---- 并发 2 路 ----
    cc_ok = bench_concurrency(soffice)

    print("\n[bench] ====== 汇总（秒，run1 = 首冷）======")
    for label, before, after in (("xls", before_xls, after_xls), ("doc", before_doc, after_doc)):
        print("[bench] %s before: %s" % (label, "  ".join("%.1f" % t for t in before)))
        print("[bench] %s after : %s" % (label, "  ".join("%.1f" % t for t in after)))
    print("[bench] 并发 2 路不串扰/不退化 = %s" % cc_ok)
    print("[bench] profile 目录 = %s" % _LO_PROFILES)
    return 0 if cc_ok else 1


if __name__ == "__main__":
    sys.exit(main())
