# -*- coding: utf-8 -*-
"""插件商店（核心插件，O2）——读面。

定位（docs/design/桌面端插件商店-设计文档.md §3.2）：
- 商店 = 主包第二核心插件（id=store，随包分发、可单独升级、不可卸载）；
- 本插件是**读面**：只读 `数据根\\store\\` 的 catalog.json 与 packages\\，
  唯一可写的是自己的下载统计 stats.jsonl（单一写方纪律：catalog/packages=admin 写）；
- 与 admin 商店管理面（写面）之间唯一的交互面是数据契约，零跨插件 import/调用
  （插件间独立铁律 / C-11）。

接口前缀：/plugin/store/api（会话鉴权同主包；store 工具条目 grant_all，全登录用户可用）。
"""
