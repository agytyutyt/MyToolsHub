# 信息传输插件优化 TODO 清单

| 文档属性 | 内容 |
| --- | --- |
| 日期 | 2026-09-16 |
| 依据 | `docs/信息传输格式开销评估与优化方案.md`（v1.3）+《压缩算法评估（DS）》 |
| 决策说明 | **多码同屏方案经评审不采用**（《二维码多码同屏可行性评估》相关结论未纳入本清单），全部任务按**单码口径**设计；原"静态 r×c 拼图""码位轮换"两项已移除（后者仅多码场景需要） |
| 用法 | 按批次顺序执行；每项含**任务名称 / 具体实现方案 / 预期效果**；完成后在状态列打勾并回写当日日志 |

## 总览

| 批次 | 主题 | 改动面 | 依赖 |
| --- | --- | --- | --- |
| 1 | 桌面端独立发版（零协议风险） | 仅桌面端 | 无 |
| 2 | 配置层提速（满屏码面舞台 + 默认版本上调） | 仅桌面端 | 无（可与批次 1 并行开发） |
| 3 | 协议 v2（二进制直载 + 完整性 + 早停） | 两端同步发版 | 无硬依赖（建议在 1/2 验证后） |
| 4 | 压缩器升级 + 新增提取能力 | 桌面端 + APP | 批次 3 的 flags/mode 字段 |
| 5 | 可选扩展 | 视需求 | 视需求 |

---

## 批次 1：桌面端独立发版（现解析端 ≥v1.7 天然兼容）

### T01 · 建立端到端回归基线 【状态：已完成（2026-09-16，47/47 全绿，~24min）】

**实现方案**：新增 `plugins/info-transfer/backend/test_roundtrip.py`——对 9 类格式样例（txt/md/csv/xlsx/xls/docx/doc/ppt/pdf，样例生成方式复用 `.workbuddy/tmp/it_bench/`）分别走 `POST /encode（static+video 两种 mode）→ /decode → /export` 全链路，断言导出文件 SHA-256 与源文件一致（精简模式断言提取文本一致）。静态多页 >200 页的用例断言报错信息含引导文案。
**预期效果**：9 格式 × 2 模式回归全绿；此后每个 TODO 合并前跑通本回归，作为改动是否破坏还原正确性的唯一判据。

### T02 · `fmt=file` 试压 zlib（S2 阶段一） 【状态：已完成（2026-09-16）】

**实现方案**：改 `routes.py:_pack_data`——删除 `fmt=file` 直通分支；对 base64 后的 data 做 `zlib.compress(level 9)` 试压，**压缩后更小才置 `zip=1`**，否则保持原样（沿用现有"更小才启用"判定）。解析端无需改动（`parse_envelope` 按 `zip==1` 解压与 fmt 无关，解压结果为 base64 串，`envelope_to_file` 照常 b64decode）。配套：T01 回归 + 新增高熵载荷用例；由于压缩后载荷变高熵，`_render_verified_qr` 回读自检升级为"回读内容与载荷逐字节比对"，且单页/单帧载荷 ≤ 该版本容量的 85–90%。
**预期效果**（v15 视频口径）：ppt 原件 env 650,636 → ~62,400 B（10.4×），扫码 15.9 min → **~92 s**；doc 12.0×（4.6 min → ~23 s）；xls 4.1×；pdf 2.9×（23.3 min → ~8 min）；docx/xlsx/jpg env 变化 ≤1%（试压自动跳过）；解析端对旧码（无 zip）零回归。

### T03 · 白名单加入 ppt / pdf / pptx（S1） 【状态：已完成（2026-09-16）】

**实现方案**：`routes.py` 的 `SUPPORTED_FORMATS` 增加三条 `extract=none` 条目（仅原件传输）；前端 `index.html` 的 `accept` 属性同步（/formats 接口驱动则自动生效）；上传 note 文案注明"该格式仅支持原件传输"；README 白名单一节同步。
**预期效果**：上传 ppt/pdf/pptx 返回 task_id 并正常出码（静态 >200 页时按 T04 引导）；/formats 返回含新格式；原 9 类格式行为零变化（T01 回归全绿）。

### T04 · 前端耗时预估与分级引导（S3） 【状态：已完成（2026-09-16）】

**实现方案**：把 `_chunk_data` + `_fec_encode` 的块数计算提为可复用函数，`/task/<id>` 响应附 `estimate: {codes, seconds, pages}`；前端任务卡片显示"预计 N 码 / 约 M 分钟"；预计 >60 s 时黄色提示（调高二维码版本 / 改用视频流 / 该格式可用精简传输）；静态拆页预估 >200 页时**发码前**弹窗引导改视频模式（替代现行的事后报错）。
**预期效果**：任一任务可见量化预估且与实际码数误差 ≤5%；>200 页静态请求不再走到报错分支。

### T05 · README / 文档同步（B8） 【状态：已完成（2026-09-16）】

**实现方案**：修正 `plugins/info-transfer/README.md` 三处过时描述：① ".doc 仅原件传输"→ 已有 OLE 精简提取；② 补 `zip=1` 对 `fmt=file` 生效的行为；③ 白名单格式清单更新（T03 之后）。按文档同步链回写 HANDOFF.md 与当日日志。
**预期效果**：README 与代码行为逐条一致（人工对照清单核对）；升级告警无新增。

---

## 批次 2：配置层提速（仅桌面端）

### T06 · 满屏码面舞台（P5） 【状态：已完成 2026-09-16】

**实现方案**：`frontend/index.html` 新增全屏播放层——流式预览/静态多页支持"一键全屏"（画布 ≥1080px，`object-fit: contain`）；Canvas 渲染遵守整数设备像素约定（位图 `round(视口×scale×dpr)`、CSS 尺寸由位图反推 + 一次性负边距补偿）。**二维码版本默认保留 15（业务决策 2026-09-16，不做全局上调）**；px/模块按"拍摄后像素"反算（屏幕像素 ÷ 显示器占画面比例），≥3 底线、4 留余量，不足时提示用户调高版本或靠近取景。
**预期效果**：v15–v25 码面在屏上 px/模块 ≥4（1080p 画布）；结合 APP 1080p 分析，同版本下识别可靠性提升；用户按 T04 的预估提示手动调高版本时可安全用到 v25+。

---

## 批次 3：协议 v2（两端同步发版）

### T07 · 信封 v2 基础字段：CRC32 + orig_len + mode + 算法 flags（S7 / DS §7.1） 【状态：已完成（2026-09-16，桌面端全量落地 + APP 侧同步）】

**实现方案**：定义二进制帧头 `magic "JZ2"(3B) + ver(1B) + flags(1B，含压缩算法 zlib/xz/none) + mode(1B，exact/rebuild) + 页号(3B) + 总数(3B) + 长度(4B) + crc32(4B)`，后接原始字节载荷；`build_envelope`/`_build_static_pages`/`encode_to_video` 三处接入；`parse_envelope` 对 JZ2 帧校验 CRC32 与长度，旧码仍按 `{` 开头解析（双端向后兼容）；导出结果页显示"还原方式：原样/重建"。
**预期效果**：损坏/截断载荷 100% 被拒并给出明确错误（回归：随机位翻转注入 ≥1000 次）；旧协议二维码解析零回归（T01 全绿）；HANDOFF:618 首要待办关闭。
**落地记录（2026-09-16）**：`PROTOCOL_V2=True` 默认启用；帧格式与上文一致（mode 位预留未消费，导出页"还原方式"标注随 T11 rebuild 路线落地）。桌面端实测：1200 次位翻转 + 截断注入 100% 被拒（`test_protocol_v2.py` 28/28）；T01 回归 47/47 全绿。APP 侧新增 `Jz2.kt`（帧解析 + 信封解析 + 静态多页收集，与桌面端逐字段对齐）。**遗留**：导出页"还原方式"UI 标注待 T11 一并做。**补记（2026-09-17）**：APP 侧 Kotlin 已在 Android Studio 全量编译并跑通 `Jz2Test` 70/70（原 66 例 + 批次 4 新增 4 例），编译验证遗留关闭。

### T08 · 二进制直载：去双重 base64（P3 / DS §7.1） 【状态：已完成（2026-09-16 桌面端 + APP 代码落地；2026-09-17 真机 PoC 通过）】

**实现方案**：视频路径信封本体二进制化（不再 JSON+base64），帧载荷直接原始字节 + JZ2 帧头；APP 侧 `CameraScanner.kt` / `ImageDecoder.kt` 从 `rawValue` 切到 `rawBytes`（网页端 jsQR 读 `binaryData`，`worker.js` 同步）；**PoC 先行：随机 1 KB 二进制 B 端编码 → 手机扫 → 哈希一致**，再上真实格式；静态路径（−26%）与视频路径（−45%）分开灰度。
**预期效果**：静态每码净载荷 2160 → 2940 B（−26%）、视频 990 → 1764 B/帧（−45%）；端到端 SHA-256 一致（T01 口径）；实测 ML Kit / jsQR 对二进制 byte 模式码的解码成功率 ≥ 旧字符串形态的 95%。
**落地记录（2026-09-16）**：桌面端 `_pack_data_v2`/`_qrformat_encode_v2` 原始字节承载（信封帧 + share 帧均不再 base64）；解码端引入 zxing-cpp（cv2 对二进制码 11/40 未解出、29/40 损坏，弃用；zxing-cpp 60/60 roundtrip 完美），v1 文本码仍回退 cv2。APP 侧 `CameraScanner`/`ImageDecoder` 全部切 `rawBytes`（回调改字节、内容去重按字节比较）。本插件网页端无本地解码，无需改动；**jsQR worker.js 属「QR 视频流解码」独立插件，v2 消费适配另立待办**。qrcode 库全零数据块 `glog(0)` 崩溃已用 `_safe_poly_mod` 猴补修复。**遗留**：验收红线第 2 条已关闭——**2026-09-17 真机实测通过**：2a（1KB 随机二进制静态码导出 SHA-256 与期望一致）与 2b（225 帧视频连续解码）均未发现问题，验收口径与素材见 `.workbuddy/test-materials/2a、2b`。

### T09 · k-of-m 早停接线（P4 / DS §1.2②） 【状态：已完成（2026-09-16 两端代码落地；2026-09-17 蒙特卡洛真机复测通过）】

**实现方案**：APP `QrFrame.kt:76` `assembleSystematic` 判据 `have < n` → `have >= k`（或直接接线现成的 `ZfecCompat`）；实时循环模式凑够每组 k 个 share 即解码；桌面端网页解析侧同步修改。
**预期效果**：200 KB / q=0.8 蒙特卡洛完成轮次 284 → 58（**4.9×**）；FEC 40% 冗余从"纯开销"转为真容错（丢 30% 帧仍可还原的注入测试通过）。
**落地记录（2026-09-16）**：桌面端 `reassemble_qrtransfer_v2` 按组任取 k 个 share 经 zfec 解码（缺 ≤ m-k 帧容忍）；APP 侧 `QrFrame.Collector` 重构——`isSatisfied()`（每组 ≥k）早停判据 + `assembleBytes()`（全收齐走系统位快路径，否则逐组 `ZfecCompat.decode`），`VideoParseHelper` MP4 逐帧循环满足即 break，相机实时流 k-of-m 满足即出结果。注入测试：k=14/m=24 丢 7 帧（30%）还原通过（`test_protocol_v2.py`）；APP 侧 `Jz2Test` 覆盖 k=6/m=10 丢 3 帧场景。**遗留**：已关闭——**2026-09-17 蒙特卡洛真机复测通过**（200KB/q=0.8 口径素材 `.workbuddy/test-materials/5_蒙特卡洛200KB/`，多轮完成未发现问题，k-of-m 容错与还原正确性达标）。

---

## 批次 4：压缩器升级 + 新增提取能力（依赖 T07 的 flags/mode 字段）

### T10 · 压缩器升级 zlib → LZMA2（P2 / DS §3–4） 【状态：已完成（2026-09-16，两端代码落地）】

**实现方案**：`_pack_data` 试压顺序改为 zlib 与 `lzma.compress(format=FORMAT_XZ, preset=9|PRESET_EXTREME)`（字典 ≤16 MB，`dict_size=1<<24`）取更小者，JZ2 flags 标注算法；APP 新增依赖 `org.tukaani:xz:1.10`（纯 Java 168.6 KB，0BSD），按 flags 选 `XZInputStream`/`Inflater`；依赖登记 `JZToolsHub.spec` 的 `PACKAGES` 与插件 requirements；损坏 xz 流 fail-open 回退。
**预期效果**：473 KB 中文文档 −18%（39.8%→32.7%）；长重复正文最高 −86%（docx 精简正文 45 页 → **7 码**）；PDF 图文 85.5%→68.6%（6.5 MB 少 1.1 MB ≈ 390 码）；APK 体积 +≤170 KB（+1.9%）；解压耗时 6.5 MB 载荷 ≤0.5 s（真机实测）。
**落地记录（2026-09-16）**：桌面端 `_compress_best`（zlib 9 → xz 9|EXTREME 试压取更小，fail-open 回退）接入 `_pack_data_v2`，flags `comp=2` 标注；`parse_envelope_v2` 增加 xz 解压分支。依赖登记完成（`JZToolsHub.spec` PACKAGES + `requirements.txt` 增加 pypdf 之外同步核对 xz-java 0BSD）。APP 侧 `build.gradle.kts` 增加 `org.tukaani:xz:1.10`，`Jz2.kt` 增加 `COMP_XZ` 常量与 `xzInflate`（XZInputStream），`Jz2Test` 覆盖 xz 信封还原 + 坏流拒收。测试注意：试压选优数据必须让两段相同文本相隔 >32KB（zlib 窗口外），否则小数据下 zlib 反超属正常（`test_protocol_v2.py` 44/44 全绿）。**遗留**：真机解压耗时口径未实测。

### T11 · Office/ZIP 拆包重组（S6，`mode=rebuild`） 【状态：已完成（2026-09-16 两端代码落地；2026-09-17 真机联调 + Office/WPS 人工验收均通过）】

**实现方案**：对 `fmt=file` 且扩展名 ∈ {docx,xlsx,xlsm,pptx,zip} 的新预处理：`zipfile` 解包 → 按"条目名\0条目内容"拼接连续流 → LZMA2 压缩；JZ2 mode=rebuild；APP 端按流拆分后用系统 `ZipOutputStream` 重建（条目顺序与时间戳不保证）；UI 导出页标注"重建（内容等价，非字节一致）"；字节精确诉求仍走 T02/T10 纯压缩路线。
**预期效果**：docx 原件 −75%（151 KB：71 页 → **14 码**）、xlsx −49%（69 → 26 码）、zip −27%（118 → 63 码）；重建文件经 Office/WPS 打开人工验收正常（每类 ≥3 个真实样例）；mode=rebuild 标识正确透传到导出页。
**落地记录（2026-09-16）**：流格式定为 `[条目数 2B] + 每条目 [name_len 2B][name utf8][content_len 4B][content]`（未采用 `\0` 分隔方案——二进制内容含 \0 且长度前缀已足够）；桌面端 `_zip_pack_stream`/`_zip_unpack_stream`/`_zip_rebuild`，`build_envelope(..., rebuild=True)` 仅当拆包压缩后确实更小才置 mode=1（否则回退纯压缩）；解析端 `parse_envelope_v2` 在解压后拆流重建 zip，下游 `envelope_to_file` 零改动，`env["mode"]=1` 透传；前端 `index.html` 原件模式新增"拆包重组"开关（仅 file+ZIP 容器格式显示），结果摘要显示"还原方式：重建"。APP 侧 `Jz2.rebuildZip`（ZipOutputStream 重建，时间戳固定 1980-01-01 与桌面端对齐），`Envelope.rebuilt` 标识，结果页预览追加"还原方式：重建"。测试：6 用例 roundtrip + 残留字节/长度越界拒收（`test_protocol_v2.py` T11 全绿）。**生成验收素材时实测发现并修复一个生产 bug**：`_zip_rebuild` 的 `writestr(ZipInfo)` 按 ZipInfo 自带 compress_type（默认 **STORED**）处理、不继承归档级 DEFLATED，导致桌面端 /export 落盘的重建文件不压缩、体积膨胀 10-20 倍（37KB docx → 831KB）；网络侧信封是压缩流所以既有测试全绿没拦住。已修（显式置 DEFLATED）并加"重建产物 ≈原件尺寸"回归断言（套件 45/45）；APP 侧 ZipOutputStream 默认 DEFLATED 本就正确。**遗留**：已全部关闭——① 前端开关 + APP「还原方式：重建」标注真机联调通过（2026-09-17，素材 `4_拆包重组真机联调/`）；② 重建 zip 经 Office/WPS 人工验收通过（2026-09-17，素材 `3_重建人工验收/` 12 个文件：docx/xlsx/pptx/zip 各 3，全部正常打开、内容与原件一致；机器预检亦确认重建产物与原件**条目级逐字节一致**、仅容器时间戳/压缩元数据不同）。

### T12 · pdf / ppt 精简提取（P1） 【状态：已完成（2026-09-16）】

**实现方案**：① pdf：新增 `pypdf` 依赖（登记 PACKAGES + requirements），逐页 `extract_text()` 按页拼接，提取为空（扫描版）→ 自动回退原件 + note；② ppt：olefile 读 `PowerPoint Document` 流，扫描 `TextCharsAtom`(0x0FA0, UTF-16LE) / `TextBytesAtom`(0x0FA8, ANSI) 记录头提取文本，按出现顺序拼接（复用 `_parse_doc_pieces` 的自研解析与 fail-open 姿态）；③ 信封 `fmt=text + ext=pdf/ppt`（解析端零改动）；④ fuzz 测试：随机字节注入 / 截断 / 加密文件全部不崩溃。
**预期效果**：pdf 纯内容 ~350× 缩减（718 KB → env ~2 KB，v15 十码内、**秒级**）；ppt 同理；扫描版 PDF 自动回退原件且 note 说明；fuzz 1000 轮零崩溃；T01 回归全绿。
**落地记录（2026-09-16）**：`SUPPORTED_FORMATS` 中 ppt/pptx/pdf 改为 text 提取口径（pptx 保持原件），`extract_doc_lean` 增加 `pypdf`（PdfReader.extract_text，扫描版/加密抛 LeanUnsupported 自动回退原件）与 `ppt`（olefile 扫 TextCharsAtom/TextBytesAtom，乱码防护 >30% 控制字符拒收）两分支；`requirements.txt` 增加 `pypdf>=4.0`，`JZToolsHub.spec` PACKAGES 登记（本机 venv 已装 pypdf 6.19.0）。fuzz 1000 轮（随机字节/截断/伪结构）零崩溃。样例 pdf/ppt 提取 + 信封 roundtrip 全绿（`test_protocol_v2.py` 44/44）。**遗留**：真实加密/扫描 PDF 的回退路径未用真实样本验证；T01 九格式基线中 ppt/pdf 用例行为随本项变化（精简口径），回归以文本一致断言通过。

---

## 批次 5：可选扩展（按业务需求启动）

### T13 · png / jpg 白名单 + WebP 无损（DS §4.2–4.3） 【状态：未开始】

**实现方案**：白名单加 png/jpg（原件）；png 走 WebP 无损转码（像素等价，`mode=rebuild`，Android 平台 API 解码零体积）；jpg 先直压，packJPG（NDK，−16%）留观察；**禁止无条件 PNG→JPEG**（实测截图膨胀 338.9%）；有损选项默认关闭、逐次显式授权。
**预期效果**：PNG 截图 −83%（39 KB → 3 码）、照片型 −36%；JPEG 直压不膨胀（+0.5% 以内）；有损转换必须经确认框。

### T14 · 静态多页纠错（S4） 【状态：未开始】

**实现方案**：静态多页拆分改用 QR-transfer 分帧协议（复用 zfec，按 idx 重组、缺页容忍），或最小改动版：每 k 页附加 1 张冗余页（k-1-of-k）。
**预期效果**：缺 1 页不再全部作废（注入"随机丢 10% 页"用例还原成功率 ≥95%）；200 页原件静态传输成功率从趋近 0 提升到可用。

### T15 · 观察项：packJPG / 喷泉码 / 帧头二进制化 【状态：未开始】

**实现方案**：不排期，满足触发条件再启动——jpg 无损诉求明确 → packJPG/brunsli（NDK，−16~−22%）；实时流长尾明显 → wirehair/RaptorQ（NDK `.so`，冗余 40%→0~2%）；小码场景占比高 → 帧头 12 字符二进制化（v15 下 1.6%）。
**预期效果**：各自立项时再定量化口径。

---

## 验收红线（贯穿所有批次）

1. **每次合并前跑 T01 回归**（9 格式 × 2 模式 SHA-256/文本一致）；
2. 高版本满屏大码、二进制直载上线前**真机验证**（连续 200 帧全解 ≥95%；≥2 台：旗舰 + 中低端）；
3. 有损变换必须逐次显式授权，默认关闭；
4. 新增第三方依赖必须同时登记 `JZToolsHub.spec` 的 `PACKAGES`（硬约定）与插件 requirements，并核对许可（xz-java 0BSD、pypdf BSD、packJPG LGPL 注意动态链接合规）；
5. 每批次发版按 `docs/打包部署手册.md` 流程，先 commit 再打包；两端协议变更（批次 3/4）须同步《移动端APP.md》第 3 章与插件 README。
