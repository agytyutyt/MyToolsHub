# 信息传输（info-transfer）

把文字 / txt / markdown / 任意文档文件封装为二维码进行"离线传输"，并在另一端解析还原。

## 功能

### 信息封装
- 输入：粘贴文字，或上传 `.txt` / `.md` / `.docx` / `.xlsx` / `.pdf` / `.doc` 等任意带扩展名的文档文件；
- **v2 起文档以原始文件完整传输**（fmt=file）：文件字节经 base64 直接封装，
  不做任何解析——样式、宏、图片、表格等与原文件完全一致；
  `.txt` / `.md` 仍按纯文本提取（数据量小、两端可直接预览）；
- 输出形式（可设定二维码版本 1-40）：
  - **静态二维码**：单张装得下输出 1 张 PNG；装不下自动拆分为多张
    （首页带文档格式/文件名，续页带页码；可单张预览、下载，多张打包 ZIP）；
  - **动态二维码视频**：QR-transfer 协议（zfec 前向纠错）生成 MP4，
    与「轨迹转换」「QR 视频流解码」插件协议互通；
    默认**相机传输模式**：每张二维码连续重复 5 帧（15fps 下单码停留约 0.33 秒），
    并同步落盘逐帧 PNG（`/frame/<task_id>/<n>`），网页端以 JS 定时轮播展示
    （绕开浏览器视频管线，杜绝丢帧"追赶"），APP 扫码自动识别视频流，
    手机摄像头对准屏幕即可传输；帧率与帧头协议不变，解析端完全兼容。

### 信息解析
- 输入：封装产生的二维码图片（单张或多张）、二维码视频；
- 流程：**先读取信封判断文档格式**，再还原数据；
- 导出：
  - file → **原始文件**（文件名与字节与原文档一致，mime 按扩展名推断）；
  - text / markdown → `.txt` / `.md`；
  - word / excel（兼容旧二维码）→ `.txt`（逐段纯文本）/ `.xlsx`（重建纯数据表格）。

## 封装协议（信封）

```json
{"jzt": 1, "fmt": "text|markdown|file", "name": "显示名", "data": "<文本 或 文件字节base64>"}
```

- `file`：v2 新增，原始文件完整传输；`data` 为文件字节的 base64，
  **`name` 为完整原始文件名（含扩展名）**；
- `word` / `excel` 为兼容保留（旧二维码仍可解析，封装端不再生成）：
  `data` 为逐段文本 / 二维数组，`name` 不含扩展名。

静态二维码单张装不下时按页拆分：

```json
{"jzt": 1, "fmt": "...", "name": "...", "pg": {"i": 1, "n": 3}, "data": "<片段>"}   // 首页
{"jzt": 1, "pg": {"i": 2, "n": 3}, "data": "<片段>"}                               // 续页
```

## 后端接口（/api/info-transfer）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /status | 依赖自检（qrcode/zfec/cv2/numpy/openpyxl） |
| POST | /encode | multipart：file 或 text + mode(static/video) + qr_version → task_id |
| GET | /task/<id> | 封装任务轮询 |
| GET | /download/<id> | 下载产物（PNG / ZIP / MP4） |
| GET | /image/<id>/<n> | 静态二维码单张预览 |
| GET | /frame/<id>/<n> | 相机传输模式单帧码图（JS 轮播用） |
| POST | /decode | multipart：file + type(image/zip/video)，图片同步、视频异步 |
| GET | /decode/<id> | 视频解析任务轮询 |
| POST | /export | 解析数据 → 导出文件（file 格式直接还原原始文件） |

## 依赖

`pip install -r backend/requirements.txt`
（qrcode、zfec、opencv-python、numpy、openpyxl）

## 已知限制

- 原始文件完整传输会显著增大数据量（base64 约 +33%），静态二维码拆分上限 200 张，
  超过请调大二维码版本或改用视频流；20MB 上传上限不变；
- word / excel 为兼容保留的旧格式（v1 提取式传输），仅旧二维码会出现；
- 静态二维码自动拆分上限 200 张，超过请调大二维码版本或改用视频流；
- 相机传输模式（默认）下视频时长为普通模式的 5 倍（帧率不变），超大文档建议调高二维码版本减少码数；
- 手机摄像头扫描屏幕视频流时：调高屏幕亮度、放大预览、避免反光，APP 端使用「视频流采集」入口；
- 任务产物缓存 30 分钟 TTL 自动清理（数据根目录 `plugins/info-transfer/.task_cache/`）。
