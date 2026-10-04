# -*- coding: utf-8 -*-
"""`jz_relay` 门面（批次 3 T17）：类 C 插件访问服务器数据的唯一通道。

口径（2026-10-03 拍板，方案 §6.3）：
- 白名单初版 = 当前 HTML 版插件/框架实际暴露的**框架级只读 API**（机械提取，见
  relay_whitelist.json）；不加新面、不删现用面、不引入新权限模型；
- 自动附用户会话（镜像 Cookie），权限与浏览器路径一致、无越权；
- 白名单外路径 / 远程未配置 → RelayError；
- 能力层装配时注册为 sys.modules["jz_relay"]——插件后端 `import jz_relay` 即用
  （与主包提供 jz_api/jz_deps 可 import 的模式一致）。
"""


class RelayError(PermissionError):
    """白名单外路径 / 远程未配置 / 会话不可用。"""


class JzRelay:
    def __init__(self, upstream, address_getter, mirror, whitelist, log=None):
        self._upstream = upstream
        self._addr = address_getter
        self._mirror = mirror
        self._whitelist = whitelist or {}
        self._log = log or (lambda m: None)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def request(self, method, path, **kw):
        if not path.startswith("/"):
            path = "/" + path
        if not self._allowed(method, path):
            self._log("jz_relay 拒绝（白名单外）：%s %s" % (method, path))
            raise RelayError("路径不在 jz_relay 白名单：%s %s" % (method, path))
        addr = self._addr()
        if not addr:
            raise RelayError("远程服务器地址未配置")
        headers = {}
        cookie = self._mirror.header()
        if cookie:
            headers["Cookie"] = cookie
        r = self._upstream.request(method, addr + path, headers=headers,
                                   timeout=(5, 120), **kw)
        if r.status_code == 401:
            self._mirror.clear()
        return r

    def _allowed(self, method, path):
        for rule in self._whitelist.get("allow", []):
            if method in rule.get("methods", []) and path.startswith(rule.get("prefix", "")):
                return True
        return False
