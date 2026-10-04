# -*- coding: utf-8 -*-
"""会话镜像（批次 3 T14）：中继观察到的远程会话 Cookie，`jz_relay` 门面专用。

口径（方案 §6.3）：
- 任一响应带 Set-Cookie 即更新（登录与 admin 滑动续期都会重发）；
- 401 → 清除（会话失效统一信号）；
- 持久化 = DPAPI 加密落盘（仅会话 Cookie 值，凭据不落盘）——类 C 升级触发
  能力层秒级重启后免重登（GD4 轮拍板，方案 §6.7）。
"""
import os
import traceback


class SessionMirror:
    def __init__(self, store_path, log=None):
        self.path = store_path
        self._log = log or (lambda m: None)
        self.cookie = None
        self._load()

    def _load(self):
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
            from secure_store import unprotect
            self.cookie = unprotect(blob).decode("utf-8") or None
            self._log("会话镜像已从磁盘恢复（%s）" % (self.cookie.split("=")[0] + "=…" if self.cookie else "空"))
        except FileNotFoundError:
            pass
        except Exception:
            self._log("会话镜像恢复失败（忽略）:\n" + traceback.format_exc())

    def update_from_set_cookie(self, set_cookie):
        if not set_cookie:
            return
        pair = set_cookie.split(";", 1)[0].strip()
        name, _, value = pair.partition("=")
        if not name or not value.strip():
            self.clear()          # 删除型 Set-Cookie（登出）→ 镜像清空
            return
        self.cookie = pair
        self._log("会话镜像更新：%s=…" % name)
        self._persist()

    def clear(self):
        if self.cookie is None:
            return
        self.cookie = None
        self._log("会话镜像已清除")
        self._persist()

    def header(self):
        """供 Cookie 头合并的镜像值（name=value）；无会话返回 None。"""
        return self.cookie

    def _persist(self):
        try:
            from secure_store import protect
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            blob = protect((self.cookie or "").encode("utf-8"))
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except Exception:
            self._log("会话镜像持久化失败:\n" + traceback.format_exc())
