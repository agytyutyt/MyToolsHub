"""JZToolsHub 宿主内部模块包 —— app.py 的职责拆分（P2-4 / 报告 §4.4）。

五个子模块（dep_bootstrap / access_log / plugin_loader / llm_routes / tray）只被
app.py 装配，不是对插件的稳定接口——插件面向的稳定契约在 jz_api / jz_deps / jz_llm /
jztools_data（FC-3/FC-5，见 docs/design/主体与插件解耦-设计文档.md §5）。

路径与状态一律由 app.py 在**装配期注入**（各子模块不自行推导程序目录）：测试沙箱以
副本 app.py 加载、打包冻结运行（exe 同层目录）两种形态下行为一致。
"""
