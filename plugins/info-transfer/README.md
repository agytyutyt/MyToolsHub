# 信息传输（info-transfer）

把文字 / 文档文件封装为二维码进行"离线传输"，并在另一端解析还原。
支持两种传输模式：**精简传输**（只传数据，二维码更少）与**原件传输**（完整还原原文件）。

## 功能

### 信息封装
- 输入：粘贴文字，或上传文件（**可多选**，每个文件独立封装、逐文件进度条，
  完成后按文件名分组展示多份二维码/二维码流，均支持单独下载）；
  **格式白名单**（内置清单，经 GET /formats 下发前端）：Word(.docx/.doc) /
  Excel(.xlsx/.xlsm/.xls) / CSV(.csv) / Txt(.txt) / Markdown(.md/.markdown)；
- **精简传输（默认，raw=0）**——只传输数据，不保留格式、宏等参数：
  - `.docx`：提取正文段落与表格文本（按文档顺序），fmt=word；
  - `.txt`：提取文本，fmt=text；`.md`：提取源码，fmt=markdown；
  - `.xlsx` / `.xlsm`：提取内容构建二维数组（值取缓存计算结果，裁剪尾部空行/空列），fmt=excel；
  - `.xls`（97-2003 二进制）：xlrd 提取内容构建二维数组（数字/日期/布尔按类型还原），fmt=excel；
  - `.csv`：解析构建二维数组（整数/小数→数值、TRUE/FALSE→布尔、前导零保留文本），fmt=excel；
  - 信封声明原始文件后缀名（`ext` 字段，如 docx / xlsx / csv），还原端据此复原为对应格式；
  - `.doc`（97-2003 二进制）不可精简提取 → 自动回退原件传输并附提示；
    白名单内文件精简提取失败（如文件损坏）同样自动回退。
- **原件传输（开关开启，raw=1）**：文件不做任何解析，fmt=file，
  文件字节经 base64 直接封装——样式、宏、图片、表格等与原文件完全一致；
  注意：开启后生成的二维码数量将**大大提高**（base64 约 +33% 体积）。
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
- 流程：**先读取信封判断文档格式与原始后缀**，再还原数据；
- 导出：
  - file → **原始文件**（文件名与字节与原文档一致，mime 按扩展名推断）；
  - text / markdown → `.txt` / `.md`（精简传输按声明的原始后缀还原）；
  - word → `.docx`（重建纯文本段落，无样式/宏）；
  - excel → `.xlsx`（重建纯数据表格，无样式/公式）。

## 封装协议（信封）

```json
{"jzt": 1, "fmt": "text|markdown|word|excel|file", "name": "显示名",
 "ext": "原始后缀名（精简传输时声明，可选）",
 "zip": 1,
 "data": "<文本 / 二维数组 / 文件字节base64 / base64(zlib压缩载荷)>"}
```

- `file`：原件传输；`data` 为文件字节的 base64，
  **`name` 为完整原始文件名（含扩展名）**；
- `word` / `excel`：精简传输（v3 起重新生成；旧二维码同样兼容）：
  `data` 为逐段文本 / 二维数组，`name` 不含扩展名；
- `ext`：精简传输声明的原始文件后缀名（docx / xlsx / xlsm / txt / md / markdown），
  旧二维码与粘贴文字无此字段；
- `zip`：v1.7 起精简传输的数据压缩标记。data 经 zlib 压缩（level 9）后
  base64 承载，消除 JSON 语法与重复文本冗余（重复度高的表格/文档典型
  压缩比 5-10 倍，3000 行表格二维码量从 354 页降至 45 页）；仅当压缩后
  确实更小时才启用，`fmt=file`（base64 高熵无收益）与小数据保持原形态。
  旧二维码无此字段，两端解析均自动兼容（有 `zip` 解压、无则直读）。

静态二维码单张装不下时按页拆分：

```json
{"jzt": 1, "fmt": "...", "name": "...", "pg": {"i": 1, "n": 3}, "data": "<片段>"}   // 首页
{"jzt": 1, "pg": {"i": 2, "n": 3}, "data": "<片段>"}                               // 续页
```

## 后端接口（/api/info-transfer）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /status | 依赖自检（qrcode/zfec/cv2/numpy/openpyxl/docx） |
| GET | /formats | 可封装文件格式清单（内置 SUPPORTED_FORMATS） |
| POST | /encode | multipart：file 或 text + mode(static/video) + qr_version + raw(0/1) → task_id |
| GET | /task/<id> | 封装任务轮询（含 fmt / ext / note） |
| GET | /download/<id> | 下载产物（PNG / ZIP / MP4） |
| GET | /image/<id>/<n> | 静态二维码单张预览 |
| GET | /frame/<id>/<n> | 相机传输模式单帧码图（JS 轮播用） |
| POST | /decode | multipart：file + type(image/zip/video)，图片同步、视频异步 |
| GET | /decode/<id> | 视频解析任务轮询 |
| POST | /export | 解析数据 → 导出文件（file 直接还原原始文件；word/excel 重建 docx/xlsx） |

## 依赖

`pip install -r backend/requirements.txt`
（qrcode、zfec、opencv-python、numpy、openpyxl、python-docx、xlrd）

## 已知限制

- 上传格式白名单为内置清单（docx/doc/xlsx/xlsm/xls/csv/txt/md/markdown，
  见 routes.py 的 SUPPORTED_FORMATS），其他格式前后端均拒绝；
- 精简传输不保留样式、宏、图片、公式（值取 excel 缓存计算结果）；
  需要完整还原请开启「原件传输」；
- `.doc`（97-2003 二进制）无安全的纯 Python 文本提取方案，仅支持原件传输；
  `.xls`/`.xlsm` 精简传输还原为 `.xlsx`（APP 端重建现代工作簿），
  `.csv` 精简传输还原为 `.csv` 文本；
- 精简传输已启用 zlib 压缩（zip=1）：压缩不划算的小数据与 fmt=file
  保持未压缩形态；解析端需 v1.7+ APP 或最新网页端（均自动兼容旧码）；
- 原件传输会显著增大数据量（base64 约 +33%，二维码数量相应大增），
  静态二维码拆分上限 200 张，超过请调大二维码版本或改用视频流；
  20MB 上传上限不变；
- `.doc`（97-2003 二进制格式）与 `.xls` 无法安全精简提取，自动按原件传输；
- 静态二维码自动拆分上限 200 张，超过请调大二维码版本或改用视频流；
- 相机传输模式（默认）下视频时长为普通模式的 5 倍（帧率不变），超大文档建议调高二维码版本减少码数；
- 手机摄像头扫描屏幕视频流时：调高屏幕亮度、放大预览、避免反光，APP 端使用「视频流采集」入口；
- 任务产物缓存 30 分钟 TTL 自动清理（数据根目录 `plugins/info-transfer/.task_cache/`）。

## 移动端 APP（InfoParse）

与网页端协议互通，扫码还原后：
- 精简传输按信封声明的后缀名（ext）还原为对应格式文件：
  word → `.docx`、excel → `.xlsx`（重建纯数据工作簿）、markdown → `.md`、text → `.txt`；
  压缩信封（zip=1）自动 zlib 解压（java.util.zip.Inflater）；
- 原件传输直接还原为原本文件（字节一致）；
- 结果页以卡片展示内容，**点击内容卡片**可调用其他应用（如 WPS Office）打开阅读；
  底部保留下载（导出）、复制、分享按钮。
