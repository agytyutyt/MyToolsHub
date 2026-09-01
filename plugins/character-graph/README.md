# 人物关系立体星图（character-graph）

全栈插件，上传文档（小说/剧本/笔录）→ 大模型抽取人物关系 → Three.js 3D 星图可视化。

## 功能

- **文档上传**：支持 .txt / .md / .docx / .pdf，后端自动提取纯文本；
- **大模型关系抽取**：调用 OpenAI 兼容接口（可配置 base_url / api_key / model），提取人物节点与关系边；
- **3D 可视化**：Three.js 渲染可旋转/缩放/平移的立体星图，节点悬浮显示详情；
- **异步任务**：文件接收后立即返回 task_id，后端线程池异步执行，前端轮询进度；
- **配置持久化**：LLM 参数（API 地址 / Key / 模型）保存至 `config.json`，Key 不回传明文。

## 依赖

- 后端：`python-docx>=0.8.11`、`pypdf>=3.0`、`requests>=2.28`（详见 `backend/requirements.txt`）；
- 前端：Three.js（ES modules，含 `three.module.js`、OrbitControls、CSS2DRenderer），已内置于 `frontend/js/lib/`。

## 注意事项

- 使用前需在页面配置页填写大模型 API 地址、Key 与模型名称，或编辑 `backend/config.json`；
- 文档文本过长（>120KB）时自动截断，超出部分可能影响关系抽取完整度；
- PDF 扫描件无法提取文字，请确保上传的 PDF 为文本型（非纯图片扫描）；
- 分析结果保留 30 分钟，超时后需重新上传。