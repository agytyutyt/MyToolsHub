# -*- coding: utf-8 -*-
"""生成轨迹速写插件自带的 SVG 图标集（Material 风格 · 24×24 线性图标）。

为什么自带 SVG
--------------
插件页面**不使用 emoji 字符**作为图标：Windows 7（无彩色 emoji 字体）会把 emoji
渲染成方框。这里为每个语义绘制一个线性 SVG，页面通过 fetch 注入内联 SVG
（因此可用 currentColor 跟随主题色），彻底摆脱系统字体依赖。

用法::

    python frontend/icons/_generate.py

输出：与 ``ICONS`` 同目录的 ``<name>.svg``（16×16 viewBox 一致、stroke=currentColor）。
新增图标：在 ``ICONS`` 里加一条，重跑本脚本即可。
"""
from __future__ import annotations

import io
import os

# 名称 → (title 中文名, SVG 内部标记)
ICONS = {
    "upload":   ("上传轨迹表", '<path d="M12 15.5V3.8"/><path d="M7.6 8.2 12 3.8l4.4 4.4"/>'
                             '<path d="M4.2 15v3.4A1.6 1.6 0 0 0 5.8 20h12.4a1.6 1.6 0 0 0 1.6-1.6V15"/>'),
    "filter":   ("字段过滤", '<path d="M3.8 5h16.4l-6.6 7.4V19l-3.4-1.9v-4.7z"/>'),
    "route":    ("轨迹出行", '<path d="M5 18.6 9.6 12.4l4.2 3.9L19 5.4"/>'
                             '<circle cx="5" cy="18.6" r="1.8"/><circle cx="19" cy="5.4" r="1.8"/>'),
    "clock":    ("停留时长", '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.2V12l3.4 2.1"/>'),
    "warning":  ("位置变动", '<path d="M12 4.4 3.6 19.4h16.8z"/><path d="M12 10v4.2"/>'
                             '<circle cx="12" cy="16.9" r="0.9" fill="currentColor" stroke="none"/>'),
    "gap":      ("无定位上报", '<path d="M3.4 12h4"/><path d="M10 12h4" stroke-dasharray="1.8 2.4"/>'
                             '<path d="M16.6 12h4"/>'),
    "report":   ("速写报告", '<path d="M6.4 3.6h6.8l5.4 5.4v11.4h-12.2z"/><path d="M13.2 3.6V9h5.4"/>'
                             '<path d="M9.2 13h5.6M9.2 16.4h3.6"/>'),
    "summary":  ("速写摘要", '<path d="M3.8 17.6a8.4 8.4 0 1 1 16.4 0"/><path d="M12 17.6 16 12.4"/>'
                             '<circle cx="12" cy="17.6" r="1.3"/>'),
    "quality":  ("数据质量", '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.1"/>'
                             '<path d="M12 2.6v2.8M12 18.6v2.8M2.6 12h2.8M18.6 12h2.8"/>'),
    "download": ("下载报告", '<path d="M12 3.8v12"/><path d="M7.6 11.4 12 15.8l4.4-4.4"/>'
                             '<path d="M4.2 15.4v3A1.6 1.6 0 0 0 5.8 20h12.4a1.6 1.6 0 0 0 1.6-1.6v-3"/>'),
    "copy":     ("复制全文", '<rect x="9" y="9" width="11" height="11" rx="2"/>'
                             '<path d="M15 6.4A1.6 1.6 0 0 0 13.4 4.8H6.4A1.6 1.6 0 0 0 4.8 6.4v7'
                             'A1.6 1.6 0 0 0 6.4 15"/>'),
    "settings": ("配置", '<path d="M4 7.6h9.4M18.6 7.6H20M4 16.4h3.4M12.6 16.4H20"/>'
                         '<circle cx="16" cy="7.6" r="2.2"/><circle cx="8" cy="16.4" r="2.2"/>'),
    "check":    ("通过", '<path d="M5 12.6 9.8 17.4 19 7"/>'),
    "reset":    ("重置", '<path d="M19.6 12a7.6 7.6 0 1 1-2.3-5.4"/><path d="M19.6 3.6V9h-5.4"/>'),
    "user":     ("号码", '<circle cx="12" cy="8.4" r="3.4"/>'
                         '<path d="M5.2 20c0-3.5 3-5.4 6.8-5.4s6.8 1.9 6.8 5.4"/>'),
}

TEMPLATE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" '
            'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
            'stroke-linejoin="round" role="img" aria-label="{title}">\n'
            '  <title>{title}</title>\n  {body}\n</svg>\n')


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    for name, (title, body) in ICONS.items():
        path = os.path.join(here, name + ".svg")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(TEMPLATE.format(title=title, body=body))
    print("已生成 %d 个 SVG 图标 → %s" % (len(ICONS), here))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
