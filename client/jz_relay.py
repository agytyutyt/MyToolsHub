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


class _OwnRelay:
    """自身插件命名空间视图（需求⑨，批次 8 T-O2-10）。

    类 C 本地部分经 ``jz_relay.own(<自身插件 id>)`` 取得本视图：只放行「自身 id 去
    local- 前缀」对应的服务器端点前缀（/api/<server_id> 及其子路径），附用户会话，
    权限=用户本人；框架级白名单面照常可用，其他插件端点仍 RelayError
    （跨插件禁调用铁律不变）。服务器工件 id 与客户端 id 的映射见设计文档 §3.5.1。
    """

    def __init__(self, relay, prefix):
        self._relay = relay
        self._prefix = prefix.rstrip("/")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def request(self, method, path, **kw):
        if not path.startswith("/"):
            path = "/" + path
        p = path.rstrip("/") if path != "/" else path
        if p != self._prefix and not p.startswith(self._prefix + "/"):
            self._relay._log("jz_relay.own 拒绝（自身命名空间外）：%s %s" % (method, path))
            raise RelayError("路径不在自身插件命名空间（%s）：%s %s"
                             % (self._prefix, method, path))
        return self._relay.request(method, path, _bypass_whitelist=True, **kw)


class JzRelay:
    def __init__(self, upstream, address_getter, mirror, whitelist, log=None):
        self._upstream = upstream
        self._addr = address_getter
        self._mirror = mirror
        self._whitelist = whitelist or {}
        self._log = log or (lambda m: None)

    def own(self, local_id):
        """类 C 专用：返回自身插件命名空间视图（需求⑨）。

        local_id 传本插件 id（local- 前缀或裸 id 均可）；白名单 own_namespace.enabled=false
        时该通道整体关闭。用法：``relay = jz_relay.own("local-report-suite")``。
        """
        cfg = self._whitelist.get("own_namespace") or {}
        if cfg.get("enabled") is False:
            raise RelayError("own_namespace 通道未启用（relay_whitelist.json）")
        base = str(local_id or "")
        if base.startswith("local-"):
            base = base[len("local-"):]
        if not base:
            raise RelayError("own() 需要本插件 id")
        return _OwnRelay(self, "/api/" + base)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def request(self, method, path, _bypass_whitelist=False, **kw):
        if not path.startswith("/"):
            path = "/" + path
        if not _bypass_whitelist and not self._allowed(method, path):
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
                                   timeout=kw.pop("timeout", (5, 120)), **kw)
        if r.status_code == 401:
            self._mirror.clear()
        return r

    def _allowed(self, method, path):
        for rule in self._whitelist.get("allow", []):
            if method in rule.get("methods", []) and path.startswith(rule.get("prefix", "")):
                return True
        return False
