# -*- coding: utf-8 -*-
r"""B-heavy 懒加载改造补丁（批次 10，Q6 拍板）：info-transfer + trajectory-convert。

把模块级 `try: import cv2/numpy` 改为 None 哨兵 + _load_cv2/_load_numpy 惰性装载；
视频/QR 路径函数入口处装载；请求闸门改走装载器；/status 旗标改由 jz_deps 判定
数据源取（不触发 import）。同源文件服务器/客户端两形态共用——服务器副作用仅是
首次视频任务才装载，行为兼容。
"""
import io
import re
import py_compile

SENTINEL_CV2 = '''CV2_AVAILABLE = None   # 懒加载三态（Q6）：None=未探测——模块级不再触发 import cv2
cv2 = None


def _load_cv2():
    """首次进入视频/二维码路径时装载 cv2；成功写全局，失败仅置不可用。

    判据用 `cv2 is None or CV2_AVAILABLE is None` 而非只看旗标：jz_deps 刷新器
    可能单写旗标为 True（组件装好后），此时仍需真实 import 填充模块对象。
    """
    global cv2, CV2_AVAILABLE
    if cv2 is None or CV2_AVAILABLE is None:
        try:
            import cv2 as _cv2
            cv2 = _cv2
            CV2_AVAILABLE = True
        except Exception:  # pragma: no cover
            cv2 = None
            CV2_AVAILABLE = False
    return CV2_AVAILABLE
'''
SENTINEL_NP = '''NUMPY_AVAILABLE = None   # 懒加载三态（Q6）：None=未探测——模块级不再触发 import numpy
np = None


def _load_numpy():
    """同 _load_cv2：numpy 惰性装载。"""
    global np, NUMPY_AVAILABLE
    if np is None or NUMPY_AVAILABLE is None:
        try:
            import numpy as _np
            np = _np
            NUMPY_AVAILABLE = True
        except Exception:  # pragma: no cover
            np = None
            NUMPY_AVAILABLE = False
    return NUMPY_AVAILABLE
'''

DEP_HELPER = '''

def _dep_available(import_name, loaded_flag):
    """懒加载配套旗标：已装载=实际可用；未装载时读 jz_deps 判定（不触发 import）。"""
    if loaded_flag is not None:
        return bool(loaded_flag)
    try:
        verdict = jz_deps.evaluate(PLUGIN_ID, "", {"requires": {"python_packages": [
            {"name": import_name, "version": "", "required": False}]}}, app_version=None)
        for d in verdict.get("detail") or []:
            if d.get("name") == import_name:
                return d.get("state") == "ok"
    except Exception:
        pass
    return False
'''


def patch(path, funcs, gate_fixes, status_fix=None, dep_helper=False):
    s = io.open(path, encoding="utf-8").read()
    if "CV2_AVAILABLE = None" in s and "def _load_cv2" in s:
        print(path, "已打补丁，跳过")
        return
    # ① 哨兵替换（两段 try-import 块）
    m = re.search(r"try:\n    import cv2\n    CV2_AVAILABLE = True\nexcept Exception:[^\n]*\n    cv2 = None\n    CV2_AVAILABLE = False\n", s)
    assert m, path + " cv2 块"
    s = s[:m.start()] + SENTINEL_CV2 + s[m.end():]
    m = re.search(r"try:\n    import numpy as np\n    NUMPY_AVAILABLE = True\nexcept Exception:[^\n]*\n    np = None\n    NUMPY_AVAILABLE = False\n", s)
    assert m, path + " numpy 块"
    s = s[:m.start()] + SENTINEL_NP + s[m.end():]
    # ② 函数入口装载（支持多行签名：def 头起，插到签名闭合行之后）
    for fn in funcs:
        pat = re.compile(r"(?m)^def %s\(" % fn)
        m = pat.search(s)
        assert m, path + " 函数 " + fn
        close = re.compile(r"\):[ \t]*$", re.M).search(s, m.end())
        assert close, path + " 函数签名闭合未找到 " + fn
        s = s[:close.end()] + "\n    _load_cv2()\n    _load_numpy()" + s[close.end():]
    # ③ 闸门改装载器
    for old, new in gate_fixes:
        assert old in s, path + " 闸门锚点"
        s = s.replace(old, new, 1)
    # ④ /status 旗标 + helper
    if status_fix:
        old, new = status_fix
        assert old in s, path + " status 锚点"
        s = s.replace(old, new, 1)
    if dep_helper:
        anchor = "\ndef register(app)"
        assert anchor in s, path + " register 锚点"
        s = s.replace(anchor, DEP_HELPER + anchor, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)
    py_compile.compile(path, doraise=True)
    print(path, "patched + compile ok")


patch(
    "plugins/info-transfer/backend/routes.py",
    funcs=["_open_video_writer", "encode_to_video", "build_video_from_frames",
           "_cv2_can_decode", "_zxing_can_decode", "_render_verified_qr",
           "decode_image_file", "decode_video_frames"],
    gate_fixes=[(
        'if mode == "video" and not (ZFEC_AVAILABLE and CV2_AVAILABLE and NUMPY_AVAILABLE):',
        'if mode == "video" and not (ZFEC_AVAILABLE and _load_cv2() and _load_numpy()):')],
)

patch(
    "plugins/trajectory-convert/backend/routes.py",
    funcs=["encode_to_video", "_open_video_writer"],
    gate_fixes=[(
        '''    if not (QRCODE_AVAILABLE and ZFEC_AVAILABLE and CV2_AVAILABLE and NUMPY_AVAILABLE):
        missing = [
            name for name, ok in (
                ("qrcode", QRCODE_AVAILABLE), ("zfec", ZFEC_AVAILABLE),
                ("opencv-python(cv2)", CV2_AVAILABLE), ("numpy", NUMPY_AVAILABLE),
            ) if not ok
        ]''',
        '''    if not (QRCODE_AVAILABLE and ZFEC_AVAILABLE and _load_cv2() and _load_numpy()):
        missing = [
            name for name, ok in (
                ("qrcode", QRCODE_AVAILABLE), ("zfec", ZFEC_AVAILABLE),
                ("opencv-python(cv2)", CV2_AVAILABLE is True), ("numpy", NUMPY_AVAILABLE is True),
            ) if not ok
        ]''')],
    status_fix=('''            "cv2": CV2_AVAILABLE,
            "numpy": NUMPY_AVAILABLE,''',
                '''            "cv2": _dep_available("cv2", CV2_AVAILABLE),
            "numpy": _dep_available("numpy", NUMPY_AVAILABLE),'''),
    dep_helper=True,
)
