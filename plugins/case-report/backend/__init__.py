"""战果录入 —— 后端插件包。

通过 rules.py（本地规则）与 llm_client.py（大模型）从收网情况报告中
抽取五要素（案件名 / 时间 / 主办大队 / 抓获人数 / 缴获物品），
并以键值对 JSON 的形式本地化存档（backend/data/ 目录，已 gitignore）。
"""