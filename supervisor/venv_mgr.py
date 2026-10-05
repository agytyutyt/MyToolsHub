# -*- coding: utf-8 -*-
"""venv 建环境（T-O2-12c 后半，需求⑩）：deps_mode=venv 的插件私有依赖环境。

口径（运行架构设计文档 §4 拍板）：
- **venv = 可选项**：要求特殊依赖版本的插件以 venv 解释器拉起，商店目录标注
  `deps_mode: venv`；venv **不是沙箱**（隔离版本、不隔离行为）；
- **`--system-site-packages` 建 venv**（实施口径，2026-10-06）：解释器基线随主包——
  worker 引导件所需的 flask/waitress 及框架基线来自主包解释器的 site-packages，
  插件 wheel 装进 venv site-packages（sys.path 中**先于**基线）→ 特殊版本覆盖
  生效、worker 引导件开箱即用；frozen 主包形态不支持 venv（无 site-packages，
  文档 §4 已注明源码态验证）；
- **离线闭包**：`pip --no-index --find-links` 只从包内 payload/wheels/ 装清单，
  机器校验（apply 前）已核 wheel tag + 在场 + sha256；
- **幂等**：venv 已存在且 wheel 清单一致（marker 比对）则复用；否则整目录重建。

venv 落位：`<数据根>\\venvs\\<插件id>\\`（随数据根；插件目录在 apply 时整目录
替换，venv 不放其中）。
"""

import json
import os
import shutil
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
VENV_TIMEOUT = 600          # venv 创建 + pip 离线安装上限


def venv_dir(data_root, plugin_id):
    return os.path.join(data_root, "venvs", plugin_id)


def venv_python(data_root, plugin_id):
    vdir = venv_dir(data_root, plugin_id)
    exe = os.path.join(vdir, "Scripts", "python.exe") if sys.platform == "win32" \
        else os.path.join(vdir, "bin", "python")
    return exe


def _marker_path(data_root, plugin_id):
    return os.path.join(venv_dir(data_root, plugin_id), ".venv-marker.json")


def _wheel_set(wheels_dir):
    try:
        return sorted(f for f in os.listdir(wheels_dir) if f.lower().endswith(".whl"))
    except OSError:
        return []


def ensure_venv(data_root, plugin_id, wheels_dir, base_python, log=None):
    """确保 venv 与 wheel 清单一致（幂等）；返回 {ok, venv_python, reused?, error?}。"""
    log = log or (lambda m: None)
    wheels = _wheel_set(wheels_dir)
    if not wheels:
        return {"ok": False, "error": "venv 包没有 wheel 载荷（payload/wheels 为空）"}
    want = {"plugin": plugin_id, "wheels": wheels, "base": base_python}
    vpy = venv_python(data_root, plugin_id)
    if os.path.isfile(vpy):
        try:
            with open(_marker_path(data_root, plugin_id), "r", encoding="utf-8") as f:
                if json.load(f) == want:
                    return {"ok": True, "venv_python": vpy, "reused": True}
        except Exception:
            pass
        log("venv 与 wheel 清单不一致 → 重建：%s" % plugin_id)
    shutil.rmtree(venv_dir(data_root, plugin_id), ignore_errors=True)
    os.makedirs(os.path.dirname(venv_dir(data_root, plugin_id)), exist_ok=True)

    def _run(args, what):
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=VENV_TIMEOUT, creationflags=flags)
        except Exception as exc:
            raise RuntimeError("%s 失败：%s" % (what, exc))
        if r.returncode != 0:
            raise RuntimeError("%s 失败（%s）：\n%s"
                               % (what, r.returncode, (r.stderr or r.stdout or "")[-500:]))
    _run([base_python, "-m", "venv", "--system-site-packages", venv_dir(data_root, plugin_id)],
         "venv 创建")
    # wheel 传**绝对路径**（裸文件名会被 pip 当作相对 CWD 的文件）；闭包不完整时
    # pip 解析依赖失败 → apply 失败——这正是「机器校验含依赖闭包完整性」的运行期兜底
    _run([vpy, "-m", "pip", "install", "--no-index", "--disable-pip-version-check"]
         + [os.path.join(wheels_dir, w) for w in wheels],
         "wheel 离线安装（闭包 %d 项）" % len(wheels))
    if not os.path.isfile(vpy):
        return {"ok": False, "error": "venv 建成后解释器缺失（%s）" % vpy}
    with open(_marker_path(data_root, plugin_id), "w", encoding="utf-8") as f:
        json.dump(want, f, ensure_ascii=False, indent=2)
    log("venv 就绪：%s（wheels %d 项，基线 %s）" % (vpy, len(wheels), base_python))
    return {"ok": True, "venv_python": vpy, "reused": False}


def discard_venv(data_root, plugin_id):
    """卸载/重建前清理（回滚不删——venv 与代码版本无关，随下次 apply 幂等重建）。"""
    shutil.rmtree(venv_dir(data_root, plugin_id), ignore_errors=True)
