# 轨迹转换（trajectory-convert）

Excel 轨迹表 → QR-transfer 二维码视频流 / 静态二维码图片。

## 功能

- **上传 Excel**：支持 .xlsx / .xls，需含「时间、经度、纬度」三列（列名由后端 `backend/config.json` 自定义，默认「开始时间、经度、纬度」）；
- **时间抽样**：从最早时刻起按固定间隔取最近点，过高速（>120km/h）点位自动过滤；
- **二维码视频流**：经 zfec 前向纠错 → qrcode 渲染 → opencv 写 H.264 MP4，与 QR-transfer 协议兼容；
- **静态二维码**：单张 PNG 或数据溢出时自动拆分为多张图片打包 ZIP，支持勾选部分下载；
- **后台任务**：异步转换 + 进度轮询（`POST /convert` → `GET /status/<task_id>` → `GET /download/<task_id>`）。

## 依赖

openpyxl、xlrd、qrcode、zfec、opencv-python、numpy（见 `backend/requirements.txt`）。

## 配置

字段名映射在 `backend/config.json` 中定义（插件目录内无此文件时使用默认值）。任务缓存文件位于 `.task_cache/`（gitignored），30 分钟 TTL 自动清理。