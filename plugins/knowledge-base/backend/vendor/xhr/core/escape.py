"""HTML 转义与安全白名单 —— 对应 TS 版 ``packages/core/src/escape.ts``。

所有来自用户文档的文本都必须经过这里，这是防 XSS 的安全基线。
"""
from __future__ import annotations

import re
from typing import Optional

_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_ESC_RE = re.compile(r"[&<>\"']")


def esc(s: str) -> str:
    """基础转义：& < > " '。"""
    return _ESC_RE.sub(lambda m: _ENTITIES[m.group(0)], s)


def esc_text(s: str) -> str:
    """单元格文本转义：保留 Excel 中的空白语义。

    · 换行 → <br>
    · 连续空格只保留第一个，其余转 &nbsp;（HTML 会折叠空格）

    ⚠️ 顺序很关键：必须**先转义、再插入 <br>**。
       反过来写会把自己插入的标签也转义成 &lt;br&gt;，页面上显示字面量
       "<br>"（TS 版 escape.ts 早期版本的真实 bug）。
    """
    out = esc(re.sub(r"\r\n|\r", "\n", s))
    out = out.replace("\n", "<br>")
    out = re.sub(r" {2,}", lambda m: " " + "&nbsp;" * (m.end() - m.start() - 1), out)
    # 行首空格（含整串开头、<br> 之后的空格）也要转 &nbsp;，否则行首缩进会被吃掉
    out = re.sub(
        r"(<br>|^)( |&nbsp;)",
        lambda m: m.group(1) + ("&nbsp;" if m.group(2) == " " else m.group(2)),
        out,
    )
    return out


def esc_attr(s: str) -> str:
    """属性值转义（href / title / style 等）。"""
    return esc(s).replace("\n", " ")


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_SCHEME_RE = re.compile(r"^(https?:|mailto:|tel:|ftp:)", re.IGNORECASE)


def safe_url(raw: Optional[str]) -> Optional[str]:
    """链接协议白名单校验。不通过返回 None（调用方降级为纯文本）。

    ⚠️ 必须拒绝 javascript: / data: / vbscript: 等伪协议。
    """
    if not raw:
        return None
    t = raw.strip()
    if not t:
        return None
    if t.startswith("#"):
        return t
    if not _SAFE_SCHEME_RE.match(t):
        return None
    if _CTRL_RE.search(t):
        return None
    return t


# 图片 MIME 白名单（禁止 svg —— 内联 SVG 可携带脚本）
SAFE_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/bmp",
    "image/webp",
}


def is_safe_image_mime(mime: Optional[str]) -> bool:
    return bool(mime) and mime.lower() in SAFE_IMAGE_MIME
