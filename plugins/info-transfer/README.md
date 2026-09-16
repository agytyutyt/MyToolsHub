# 信息传输（info-transfer）

把文字 / 文档文件封装为二维码进行"离线传输"，并在另一端解析还原。
支持两种传输模式：**精简传输**（只传数据，二维码更少）与**原件传输**（完整还原原文件）。

## 功能

### 信息封装
- 输入：粘贴文字，或上传文件（**可多选**，每个文件独立封装、逐文件进度条，
  完成后按文件名分组展示多份二维码/二维码流，均支持单独下载）；
  **格式白名单**（内置清单，经 GET /formats 下发前端）：Word(.docx/.doc) /
  Excel(.xlsx/.xlsm/.xls) / CSV(.csv) / Txt(.txt) / Markdown(.md/.markdown) /
  PPT 演示(.ppt/.pptx) / PDF 文档(.pdf)——后两类仅支持原件传输；
- **精简传输（默认，raw=0）**——只传输数据，不保留格式、宏等参数：
  - `.docx`：提取正文段落与表格文本（按文档顺序），fmt=word；
  - `.txt`：提取文本，fmt=text；`.md`：提取源码，fmt=markdown；
  - `.xlsx` / `.xlsm`：提取内容构建二维数组（值取缓存计算结果，裁剪尾部空行/空列），fmt=excel；
  - `.xls`（97-2003 二进制）：xlrd 提取内容构建二维数组（数字/日期/布尔按类型还原），fmt=excel；
  - `.csv`：解析构建二维数组（整数/小数→数值、TRUE/FALSE→布尔、前导零保留文本），fmt=excel；
  - 信封声明原始文件后缀名（`ext` 字段，如 docx / xlsx / csv），还原端据此复原为对应格式；
  - `.doc`（97-2003 二进制）：olefile 解析 FIB/CLX 提取正文文本，fmt=word；
  - 白名单内文件精简提取失败（如文件损坏、pdf 为扫描版文本缺失）自动回退原件传输并附提示；
  - ppt / pdf / pptx 不支持精简提取（白名单标注 extract=none），仅原件传输。
- **原件传输（开关开启，raw=1）**：文件不做任何解析，fmt=file，
  文件字节经 base64 封装——样式、宏、图片、表格等与原文件完全一致（还原字节一致）；
  base64 文本再做一轮 zlib 试压（zip=1）：OLE 类（doc/ppt/xls）与文本类源文件
  可缩 4–12×，已压缩容器（docx/xlsx/jpg）自动保持原样；
  解析端按 `zip=1` 解压与 fmt 无关，网页端与 APP（≥v1.7）天然兼容。
- 输出形式（可设定二维码版本 1-40）：
  - **静态二维码**：单张装得下输出 1 张 PNG；装不下自动拆分为多张
    （首页带文档格式/文件名，续页带页码；可单张预览、下载，多张打包 ZIP）；
  - **动态二维码视频**：QR-transfer 协议（zfec 前向纠错）生成 MP4，
    与「轨迹转换」「QR 视频流解码」插件协议互通；
    默认**相机传输模式**：每张二维码连续重复 5 帧（15fps 下单码停留约 0.33 秒），
    并同步落盘逐帧 PNG（`/frame/<task_id>/<n>`），网页端以 JS 定时轮播展示
    （绕开浏览器视频管线，杜绝丢帧"追赶"），APP 扫码自动识别视频流，
    手机摄像头对准屏幕即可传输；帧率与帧头协议不变，解析端完全兼容。
  - **全屏码面舞台**：静态与视频结果均提供「全屏展示 / 全屏播放（手机扫码）」
    一键全屏（黑底覆盖层 + 浏览器真全屏增强，Esc 退出，静态多页支持 ‹ › 循环翻页）；
    码面按 96vmin 最大化，Canvas 位图落整数设备像素（位图 round(视口×dpr)、
    CSS 尺寸由位图反推 + 一次性负边距补偿），底部实时显示码面 px/模块
    （屏幕像素 ÷ (17+4×版本)，≥4 余量充足 / ≥3 底线 / 不足提示靠近取景并调高亮度）；
    任务结果（/task）新增 `qr_version` 字段供前端计算。

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
- `ext`：精简传输声明的原始文件后缀名（docx / doc / xlsx / xlsm / xls / csv / txt / md / markdown），
  旧二维码与粘贴文字无此字段；
- `zip`：v1.7 起的数据压缩标记（v1.8 起对 `fmt=file` 原件同样生效）。data 经
  zlib 压缩（level 9）后 base64 承载，消除 JSON 语法与重复文本冗余
  （重复度高的表格/文档典型压缩比 5-10 倍；原件传输对 OLE/文本类源文件
  4-12×）；仅当压缩后确实更小时才启用，小数据与不可压缩数据保持原形态。
  旧二维码无此字段，两端解析均自动兼容（有 `zip` 解压、无则直读）；
  解压结果按 fmt 语义解释：文本/二维数组直读，`file` 再做一次 base64 解码。

静态二维码单张装不下时按页拆分：

```json
{"jzt": 1, "fmt": "...", "name": "...", "pg": {"i": 1, "n": 3}, "data": "<片段>"}   // 首页
{"jzt": 1, "pg": {"i": 2, "n": 3}, "data": "<片段>"}                               // 续页
```

### 协议 v2：JZ2 二进制帧（2026-09-16 起，默认启用）

`PROTOCOL_V2=True`（routes.py）时，新封装产物改用二进制帧承载——去双重 base64
（静态净载荷 −26%、视频 −45%）并内建 CRC32 完整性校验。帧结构
（21B 定长帧头 + meta + payload）：

```
magic "JZ2"(3B) | ver(1B) | flags(1B) | seg_i(3B) | seg_n(3B) | meta_len(2B) | body_len(4B) | crc32(4B)
flags bit0-1=压缩算法（0=none 1=zlib）| bit2=mode（0=原样 1=重建，预留）| bit3=帧类型（0=信封帧 1=FEC share 帧）
```

- **信封帧**（静态码直接承载）：`meta = fmt(1B)+orig_len(4B)+name_len(2B)+name+ext_len(1B)+ext`，
  `payload` 为数据原始字节（压缩与否由 flags 标注）。静态多页时 meta 每页重复、
  payload 为整份数据切片，收齐 seg_n 页后拼接再统一解压；
- **share 帧**（视频流）：`meta = k(1B)+m(1B)`，`payload` 为 zfec share 原始字节。
  帧序与 v1 相同（`idx = share×组数 + 组号`）；APP 端 k-of-m 早停——每组集齐
  ≥k 个 share 即可经 GF 解码重组（丢 30% 帧仍可还原），全收齐走系统位快路径；
- **完整性**：CRC32 覆盖帧头（除 CRC 字段）+ meta + payload，损坏/截断 100% 被拒
  （≥1200 次随机位翻转注入回归通过）；`orig_len` 二次校验解压后长度；
- **双协议自适应**：解码端按 `b"JZ2"` 魔数判别——v2 帧走新解析，否则按 v1 JSON
  文本解析；旧码、外部插件 v1 流（轨迹转换）零回归（T01 47/47 全绿）；
- **解码依赖**：v2 二进制码解码优先 zxing-cpp（`pip install zxing-cpp`，返回原始
  bytes 无损）；cv2 对二进制 byte 模式码不可靠，仅作 v1 文本回退。依赖已登记
  `JZToolsHub.spec` 的 PACKAGES 与插件 requirements；
- **前端无改动**：本插件网页端无本地解码（解析在后端），v2 自动生效。
  「QR 视频流解码」插件（jsQR worker）如需消费 v2 流另行适配（读 `binaryData`）。

## 后端接口（/api/info-transfer）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /status | 依赖自检（qrcode/zfec/cv2/numpy/openpyxl/docx/zxing/协议版本） |
| GET | /formats | 可封装文件格式清单（内置 SUPPORTED_FORMATS） |
| POST | /encode | multipart：file 或 text + mode(static/video) + qr_version + raw(0/1) → task_id |
| GET | /task/<id> | 封装任务轮询（含 fmt / ext / note / estimate 预估码数·耗时·页数） |
| GET | /download/<id> | 下载产物（PNG / ZIP / MP4） |
| GET | /image/<id>/<n> | 静态二维码单张预览 |
| GET | /frame/<id>/<n> | 相机传输模式单帧码图（JS 轮播用） |
| POST | /decode | multipart：file + type(image/zip/video)，图片同步、视频异步 |
| GET | /decode/<id> | 视频解析任务轮询 |
| POST | /export | 解析数据 → 导出文件（file 直接还原原始文件；word/excel 重建 docx/xlsx） |

## 依赖

`pip install -r backend/requirements.txt`
（qrcode、zfec、opencv-python、numpy、openpyxl、python-docx、xlrd、olefile、zxing-cpp）

## 已知限制

- 上传格式白名单为内置清单（docx/doc/xlsx/xlsm/xls/csv/txt/md/markdown/ppt/pptx/pdf，
  见 routes.py 的 SUPPORTED_FORMATS），其他格式前后端均拒绝；
  ppt/pptx/pdf 仅支持原件传输（extract=none）；
- 精简传输不保留样式、宏、图片、公式（值取 excel 缓存计算结果）；
  需要完整还原请开启「原件传输」；
- `.xls`/`.xlsm` 精简传输还原为 `.xlsx`（APP 端重建现代工作簿），
  `.csv` 精简传输还原为 `.csv` 文本；
- 精简传输与原件传输均启用 zlib 试压（zip=1）：压缩不划算的小数据与
  不可压缩数据保持未压缩形态；解析端需 v1.7+ APP 或最新网页端（均自动兼容旧码）；
- 单码载荷预留 10% 安全余量（PAYLOAD_SAFETY=0.9）：满容量高熵载荷在部分
  解码器下不稳定，码数相应增加约 11% 以换取识别可靠性；
- 原件传输 base64 约 +33%，但 zlib 试压对 OLE/文本类源文件可省 4–12×；
  静态二维码拆分上限 200 张，超过时任务在发码前快速失败并提示
  调大二维码版本或改用视频流（/task 的 estimate 字段提供预估页数/码数/耗时）；
  20MB 上传上限不变；
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
