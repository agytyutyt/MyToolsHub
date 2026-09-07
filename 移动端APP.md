# 「信息传输」移动端 APP 开发文档（Android · 仅信息解析）

| 文档属性 | 内容 |
| --- | --- |
| 文档版本 | v1.0（2026-09-04） |
| 对应桌面端 | JZToolsHub 插件 `info-transfer`（信息传输）v1.0 |
| APP 范围 | **只做「信息解析」**（解析信息封装功能产生的二维码图片 / 二维码视频） |
| 读者 | Android 开发者（假设为初级水平，本文给出可直接使用的代码与逐项检查清单） |
| 通信方式 | **无后端、无联网**。APP 完全离线运行，第 3 章协议即全部契约 |

> **如何阅读本文档**：
> - 第 1、2 章了解做什么；
> - **第 3 章是核心契约，必须逐字实现**，实现前请反复核对；
> - 第 4 章给出一期（MVP）的完整实现指引，代码可直接复制；
> - 第 5 章是二期可选的视频流解析；
> - 开发中遇到问题先查第 6 章异常总表与附录 D FAQ；
> - 交付前逐项勾选第 8 章验收清单。

---

## 目录

1. [项目概述](#1-项目概述)
2. [总体设计](#2-总体设计)
3. [协议规范（★核心契约）](#3-协议规范核心契约)
4. [一期实现指引（MVP）](#4-一期实现指引mvp)
5. [二期实现指引（可选）：二维码视频解析](#5-二期实现指引可选二维码视频解析)
6. [异常与边界处理总表](#6-异常与边界处理总表)
7. [测试指南](#7-测试指南)
8. [验收清单](#8-验收清单)
9. [附录](#9-附录)

---

## 1. 项目概述

### 1.1 背景

桌面端「信息传输」插件（JZToolsHub）提供两个功能：

- **信息封装**：把文字 / txt / markdown / word / excel 的**数据内容**（不含样式与宏）封装为 JSON「信封」，编码为**静态二维码**（单张或自动拆分多张）或**二维码视频流**（MP4，QR-transfer 协议 + zfec 前向纠错）；
- **信息解析**：对上述二维码 / 视频解码，**先读取信封判断文档格式**，再还原出纯数据内容并导出文件。

本 APP 是解析功能的**手机端实现**：用手机摄像头扫描（或从相册导入）桌面端生成的二维码，还原出数据内容并保存 / 分享。典型场景：电脑上把文档封装成二维码显示 / 打印，民警用手机扫码即取走数据。

### 1.2 APP 范围

**做**（信息解析，全部离线完成）：

| 编号 | 能力 | 优先级 |
| --- | --- | --- |
| FR-01 | 相机实时扫码解析（单张完整信封） | P0 一期 |
| FR-02 | 相册导入 PNG/JPG 图片解析（支持一次多选，多张为多页拆分码） | P0 一期 |
| FR-03 | 多页拆分码的收集与重组（可分多次扫描，进度提示） | P0 一期 |
| FR-04 | 解析结果展示：文档格式徽标、来源名、内容预览（文本 / 表格） | P0 一期 |
| FR-05 | 导出文件（.txt / .md / .csv）到「下载」目录 | P0 一期 |
| FR-06 | 分享内容 / 文件到微信、QQ 等 | P0 一期 |
| FR-07 | 复制全部内容到剪贴板 | P0 一期 |
| FR-08 | 多页收集任务的本地暂存（APP 退出后可继续） | P1 一期 |
| FR-09 | 二维码视频（MP4）流解析（含 zfec 纠错） | P2 二期（可选） |
| FR-10 | 解析历史记录 | P2 可选 |

**不做**（明确排除，勿实现）：

- ❌ 信息封装（生成二维码）——仅桌面端提供；
- ❌ 任何联网功能、任何服务端接口对接——协议自包含，离线即可解析；
- ❌ 解析非「信息传输」封装的普通二维码（如网址码）——统一按第 6 章文案提示「不是信息传输封装的二维码」；
- ❌ 保留 word/excel 的样式、宏、图片——协议本身只承载数据。

### 1.3 名词解释（首次接触请先读）

| 名词 | 解释 |
| --- | --- |
| **信封（envelope）** | 封装端生成的 JSON 结构，声明「这是什么格式的文档 + 数据是什么」。见 3.1 |
| **多页拆分** | 数据超过单张二维码容量时，封装端把信封 JSON 切成 N 个片段，逐张编码；每张上的 JSON 带 `pg` 页码字段 |
| **QR-transfer 帧** | 二维码视频里每一帧画面上的二维码内容，格式为「12 字符 base64 帧头 + base64 数据」（仅二期需要） |
| **zfec / 前向纠错** | 视频帧的冗余编码：只要收到 m 帧中的任意 k 帧，即可还原原始数据（仅二期需要） |
| **二维码版本（1-40）** | QR Code 规格参数，版本越大容量越大、点阵越密。APP 无需关心，扫码库自动识别 |
| **EC 级别 L** | 封装端统一使用的纠错等级（7% 冗余）。APP 无需关心 |

### 1.4 运行形态与约束

- **完全离线**：`AndroidManifest.xml` 中**不声明任何 INTERNET 权限**（这也是隐私要求：数据不出设备）；
- 最低支持 Android 10（API 29），目标 API 34；理由见附录 D FAQ-1；
- 扫码库使用 Google ML Kit（内置模型版，**无需 Google Play 服务**，国产手机可用）；
- 产物二维码特性（供扫码参数调优参考）：封装端以 `box_size=10, border=4` 渲染，二维码图像边长 = (模块数+8)×10 像素，版本 15 约 850px、版本 40 约 1850px，点阵清晰、无压缩失真（PNG 直出）。

---

## 2. 总体设计

### 2.1 页面流

```
┌────────────────┐  相机FAB   ┌───────────┐  扫码/导图   ┌────────────────┐ 集齐 n 页   ┌──────────┐
│ 主页(识别历史)  │ ────────▶ │ 识别页      │ ──────────▶ │ 内容判别/收集器  │ ─────────▶ │ 结果页    │
│ 历史卡片列表    │           │ CameraX    │             │ · 单张信封→直出  │            │ 格式徽标  │
│ +相机FAB       │           │ + ML Kit   │             │ · 带 pg→收集进度 │            │ 内容预览  │
│ 点卡片回看结果  │           │ 重置/导入   │             │ · base64→(二期) │            │ 导出/分享 │
└────────────────┘           └───────────┘             └────────────────┘            └──────────┘
      ▲                                                    │  「重新扫描」回识别页(从历史进入时隐藏)
      └──────────────── 识别成功自动写入历史 ────────────────┘
```

识别成功的结果自动保存到本地历史（`filesDir/history/`，一条结果一个 JSON 文件）；主页按时间倒序以卡片展示，支持长按删除单条、菜单清空全部。识别页提供「重置」按钮：一键清空多页收集器、视频流帧收集器与本地暂存，回到全新识别状态。

### 2.2 技术选型（按此执行，不要自行更换）

| 项 | 选择 | 版本参考 | 理由 |
| --- | --- | --- | --- |
| 语言 | Kotlin | 1.9+ | 官方首选；本文代码均为 Kotlin |
| UI | 单 Activity + 多页面（Fragment 或 Activity 均可，本文按 Activity 描述） | — | 结构最简单，适合小团队 |
| minSdk / targetSdk | 29 / 34 | — | API 29 起「保存到下载」免存储权限（附录 D FAQ-1） |
| 扫码 | **ML Kit Barcode Scanning（bundled 模型）** | `com.google.mlkit:barcode-scanning:17.3.0` | 离线、免费、识别率高、集成简单；bundled 版不依赖 GMS，国产手机可用 |
| 相机 | **CameraX** | `androidx.camera:1.3.4` | 官方推荐，与 ML Kit 配合成熟 |
| JSON | Gson | `com.google.code.gson:2.11.0` | API 简单，容忍宽松 |
| 相册 | 系统 Photo Picker / `ActivityResultContracts.GetContent` | — | 免权限 |

### 2.3 工程结构（建议）

```
app/src/main/java/com/xxx/infoparse/
├── HomeActivity.kt          # 主页（识别历史卡片列表 + 相机 FAB 入口）
├── ScanActivity.kt          # 识别页（相机 + 重置 + 图片/视频导入）
├── CollectActivity.kt       # 多页收集进度页（可与 ScanActivity 合并，现状已合并）
├── ResultActivity.kt        # 结果展示页
├── history/
│   └── HistoryStore.kt      # 识别历史持久化（filesDir/history/，一条结果一个 JSON）
├── ui/
│   ├── HistoryAdapter.kt    # 主页历史卡片适配器
│   └── FmtUi.kt             # 格式徽标着色/统计文案（主页与结果页共用）
├── protocol/
│   ├── Envelope.kt          # 信封数据模型 + 解析 + 校验
│   ├── PageCollector.kt     # 多页收集/重组器
│   └── QrFrame.kt           # (二期) 视频帧解析 + 系统位重组
├── scan/
│   ├── CameraScanner.kt     # CameraX + ML Kit 封装
│   └── ImageDecoder.kt      # 相册图片解码
├── export/
│   └── Exporter.kt          # CSV/TXT 生成、保存、分享
└── util/Csv.kt              # CSV 转义工具
```

---

## 3. 协议规范（★核心契约）

> 本章是 APP 与桌面端封装功能的全部接口。**实现必须与本章逐字一致**；拿不准时以桌面端插件源码 `plugins/info-transfer/backend/routes.py` 中的 `build_envelope / _build_static_pages / _parse_scanned_texts / reassemble_qrtransfer` 为准。

### 3.1 信封（envelope）

信封是一段 UTF-8 编码的 JSON 文本，压缩风格（无多余空格）：

```json
{"jzt":1,"fmt":"text","name":"会议通知","data":"本周五 14:00 在三楼会议室召开季度总结会，请准时参加。"}
```

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `jzt` | number | ✅ | 协议标识，**恒为 1**。解析时若 ≠ 1 → 拒绝（文案见 3.6） |
| `fmt` | string | ✅（完整信封必含） | 文档格式声明，允许 5 个值：`text`（纯文本）、`markdown`、`word`、`excel`（后两个为兼容保留，封装端不再生成）、`file`（原始文件完整传输，v2 新增） |
| `name` | string | ✅（完整信封必含） | 来源显示名。`text`/`markdown`/`word`/`excel` **不含扩展名**（如原文件 `花名册.xlsx` → `name` 为 `花名册`）；**`file` 为完整原始文件名（含扩展名）**；粘贴的文字默认 `文字信息`。展示用 |
| `data` | string 或 二维数组 | ✅ | 数据载荷，类型随 `fmt` 而定，见 3.1.1 |

三种格式的真实样例（来自封装端实测输出，可直接作为测试断言）：

```json
{"jzt":1,"fmt":"text","name":"会议通知","data":"本周五 14:00 在三楼会议室召开季度总结会，请准时参加。"}
```

```json
{"jzt":1,"fmt":"markdown","name":"值班安排","data":"# 值班安排\n\n- 周一：张三\n- 周二：李四"}
```

```json
{"jzt":1,"fmt":"excel","name":"花名册","data":[["姓名","部门"],["张三","刑侦"],["李四","网安"]]}
```

#### 3.1.1 `data` 细则

| fmt | data 类型 | 约定 |
| --- | --- | --- |
| `text` | string | 原文，可能含 `\n` 换行 |
| `markdown` | string | markdown 原文 |
| `word` | string | **（兼容保留，仅旧二维码出现）** 逐段纯文本（`\n` 分段）；表格行以「单元格A \| 单元格B」形式混在文本行中（`\|` 两侧各一个空格）。APP 按纯文本展示即可 |
| `excel` | 二维数组 `[[v,v,...],...]` | **（兼容保留，仅旧二维码出现）** 第一行通常为表头（不做强约定，原样展示）。**单元格值只可能是 4 类**：string、number（int/float）、boolean、null。多工作表时以单元素标记行 `["〖工作表：Sheet名〗"]` 分隔。⚠️ 不存在日期对象——封装端已把日期转为 `"2026-01-05 09:30:00"` 这类字符串 |
| `file` | string | **（v2 新增）** 原始文件字节的 base64（标准字母表，含 padding）。**不做任何解析，导出/分享时解码还原原始文件**；`name` 为完整文件名（含扩展名），mime 按扩展名推断。样式/宏/图片等与原文件完全一致 |

#### 3.1.2 导出规则（APP 端落盘）

| fmt | 导出扩展名 | 内容 |
| --- | --- | --- |
| `text` | `.txt` | data 原文（UTF-8） |
| `markdown` | `.md` | data 原文（UTF-8） |
| `word` | `.docx` | **（兼容旧码）** data 逐段文本重建为**可编辑的 Word 文档**（最小 OOXML，一个文本行一个段落；原始样式/宏/图片不在数据中，无法还原） |
| `excel` | `.csv` | **（兼容旧码）** 二维数组 → CSV（UTF-8 **带 BOM**，Excel 直接打开不乱码）；转义规则见 4.7.1 |
| `file` | 原扩展名 | data base64 解码后的**原始文件字节**，文件名 = `name` 原样（已含扩展名），mime 按扩展名推断（未知类型 `application/octet-stream`）。**与原文档逐字节一致** |

导出文件名 = `name` + 扩展名（`file` 除外，直接用 `name`）；`name` 中若含 `\ / : * ? " < > |` 等非法文件名字符需替换为 `_`；`name` 已带同名扩展名时不重复追加。分享文件使用对应 mime（file 按扩展名推断），接收端即可显示完整文件名与后缀。

### 3.2 二维码内容的三种形态与判别

APP 扫到一串文本后，按下面的顺序判别（**顺序不能变**）：

```
扫描得到文本 text
   │
   ├─ text 以 "{" 开头？
   │     ├─ 是 → 尝试按 JSON 解析
   │     │     ├─ 解析失败            → 错误「未识别到信息传输封装的二维码内容」
   │     │     ├─ 有 "pg" 字段        → 【形态B】多页分片 → 交给 PageCollector（3.3）
   │     │     └─ 无 "pg" 字段        → 【形态A】单张完整信封 → 校验(3.1) → 直出结果
   │     └─
   └─ 否 → 按 base64 帧头解析（二期，3.4）
         ├─ 头部合法（9 字节、第 7 字节为 0）→ 【形态C】QR-transfer 视频帧 → 收集
         └─ 不合法                          → 错误「未识别到信息传输封装的二维码内容」
```

> 判别依据：信封 JSON 以 `{` 开头；而 base64 字母表（`A-Z a-z 0-9 + / =`）不含 `{`，二者天然互斥，不会误判。

### 3.3 形态 B：多页分片协议与重组

数据超过单张容量时，封装端把**完整信封 JSON 文本**切成 N 个 UTF-8 字节片段，逐张编码：

- **第 1 页**（含格式声明）：
  ```json
  {"jzt":1,"fmt":"text","name":"长文示例","pg":{"i":1,"n":5},"data":"{\"jzt\":1,\"fmt\":\"text\",..."}
  ```
- **第 2..N 页**（只带页码与片段）：
  ```json
  {"jzt":1,"pg":{"i":2,"n":5},"data":"AAAAAAAA..."}
  ```

注意：每页 `data` 里装的是**完整信封 JSON 的一个片段**（字符串），所以第 1 页的 `data` 值里会出现 `\"` 转义引号——这是正常的，不要试图在第 1 页上单独解析出信封。

**重组规则（PageCollector 状态机）**：

1. 收到某页：读 `pg.i`（1 起）、`pg.n`。
2. 首个入集的页确定 `total = pg.n`；之后任何 `pg.n ≠ total` 的页**忽略**并提示「页数与当前任务不一致」。
3. 以 `i` 为键存片段；同 `i` 重复扫描 → **覆盖**（等同忽略）；乱序无影响。
4. 集齐 `1..n` 后：按 `i` 升序把各页 `data` 字符串**直接拼接**，得到完整信封 JSON 文本 → 按 3.1 校验解析。
5. 页面 1 缺失时无法得知 `fmt/name`，**必须**等页 1 到齐（集齐后自然包含）。
6. 进度展示：`已收集 x/n 张`；可列出缺失页码。

**多张图片一次导入**（FR-02）：相册多选时，逐张解码 → 全部片段交给同一个收集器 → 立即尝试重组；集齐则直出结果，未集齐则进入收集进度状态。图片顺序无关（协议按 `pg.i` 排序，不依赖文件名）。

### 3.4 形态 C：QR-transfer 视频帧协议（二期）

封装端把信封 JSON 字节编码进视频 MP4：每帧画面一个二维码。APP 需逐帧取图 → 扫码 → 重组。

#### 3.4.1 帧内容格式

每个二维码的内容是一串 ASCII 文本：

```
[12 字符 base64 帧头][base64 数据载荷]
```

帧头 12 个 base64 字符解码后是 **9 字节**，布局：

| 字节偏移 | 长度 | 含义 |
| --- | --- | --- |
| 0..2 | 3 | 帧序号 `idx`（0 起，大端 24 位） |
| 3..5 | 3 | 声明总帧数 `n`（大端 24 位） |
| 6 | 1 | 保留位，**恒为 0x00**（校验用） |
| 7 | 1 | 纠错参数 `k`（1..255） |
| 8 | 1 | 纠错参数 `m`（k..255） |

实测样例（某视频帧 0 内容前 16 字符 `AAAAAAAKAAYKAAAI`，k=6/m=10/n=10 的流）：

```
帧头 base64 = "AAAAAAAKAAYK"
解码 9 字节  = 00 00 00 | 00 00 0A | 00 | 06 | 0A
           → idx=0, n=10, 保留=0, k=6, m=10 ✓
```

#### 3.4.2 重组算法

设 `nblocks = n / m`（整数除法；**绝大多数数据量下 nblocks = 1**，只有单帧分块数超过 255 才会 >1）。

1. 帧号 `idx` 换算：**组号 `g = idx % nblocks`，组内序号（share 序号）`s = idx / nblocks`**；
2. 每组含 m 个 share（s = 0..m-1），其中 **s < k 的 share 就是原始数据块**（系统码，无需任何数学运算）；
3. 组 `g` 的第 `c` 个原始块（c = 0..k-1）对应**全局块号 = `c * nblocks + g`**；
4. 全部原始块按全局块号 `0 .. k*nblocks-1` **顺序拼接**（每个块等长，各帧载荷解码后长度一致；不足处以 0x00 填充过）；
5. 拼接结果前 **4 字节（大端）= 数据长度 L**；取第 `5` 字节起、长 `L` 字节 = 信封 JSON（UTF-8）→ 按 3.1 解析。

**纠错**：某组若缺原始块（s<k 的没扫全），但该组收到的 share 总数 ≥ k，可用 RS 纠错还原——见附录 C 的 Kotlin 实现与测试向量。若某组收到的 share < k 个，该组不可恢复 → 提示用户重新扫描（视频帧有约 40% 冗余，完整扫描时一定够）。

> 实现提示：完整扫描视频时**永远走纯系统位路径（步骤 2-5）即可**，附录 C 只在「用户漏扫部分帧」时才被用到。一期二期实现均可先只做系统位路径 + 「缺帧请重新扫描」提示，附录 C 作为增强。

### 3.5 错误文案对照表（APP 提示必须使用以下文案，与桌面端行为对齐）

| 场景 | APP 提示文案 |
| --- | --- |
| 内容不是信封也不是合法帧 | 未识别到「信息传输」封装的二维码内容 |
| `jzt` ≠ 1 | 信封协议版本不受支持 |
| `fmt` 不在 4 个取值内 | 未知的文档格式声明：<fmt 原值> |
| 多页未集齐 | 已收集 x/n 张，还差：第 a、b…张（列出缺失页码，最多列 5 个） |
| 新页 `pg.n` 与任务 `total` 不一致 | 该二维码页数与当前任务不一致，已忽略 |
| excel 的 `data` 不是二维数组 | Excel 数据结构异常 |
| 图片中扫不出码 | 图片中未识别到二维码 |

---

## 4. 一期实现指引（MVP）

> 本章代码可直接复制使用。包名以 `com.xxx.infoparse` 为例，自行替换。

### 4.1 工程创建与依赖

1. Android Studio（Koala 或更新）→ New Project → **Empty Views Activity**（不要选 Compose）→ 语言 Kotlin → minSdk 29。
2. `app/build.gradle.kts` 追加依赖：

```kotlin
dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")

    // 相机
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")

    // 扫码（bundled 内置模型，无需 GMS）
    implementation("com.google.mlkit:barcode-scanning:17.3.0")

    // JSON
    implementation("com.google.code.gson:gson:2.11.0")
}
```

3. `AndroidManifest.xml`：

```xml
<uses-feature android:name="android.hardware.camera.any" android:required="true" />
<uses-permission android:name="android.permission.CAMERA" />
<!-- 注意：不要声明 INTERNET 权限（离线承诺，见 1.4） -->
```

`<application>` 内注册结果页（若使用独立 Activity）。

### 4.2 协议解析层（核心代码，完整文件）

#### `protocol/Envelope.kt`

```kotlin
package com.xxx.infoparse.protocol

import com.google.gson.JsonParser

/** 文档格式常量 */
object Fmt {
    const val TEXT = "text"
    const val MARKDOWN = "markdown"
    const val WORD = "word"
    const val EXCEL = "excel"
    val ALL = setOf(TEXT, MARKDOWN, WORD, EXCEL)
    fun label(f: String) = when (f) {
        TEXT -> "纯文本"; MARKDOWN -> "Markdown"; WORD -> "Word 文档"; EXCEL -> "Excel 表格"
        else -> f
    }
}

/** 解析成功后的信封 */
data class Envelope(
    val fmt: String,
    val name: String,
    /** fmt=text/markdown/word 时为字符串；fmt=excel 时为 List<List<Any?>> */
    val data: Any,
) {
    val isExcel: Boolean get() = fmt == Fmt.EXCEL && data is List<*>
}

/** 单次扫码文本的解析结论 */
sealed class ScanResult {
    /** 形态A：单张完整信封，直接出结果 */
    data class Single(val envelope: Envelope) : ScanResult()
    /** 形态B：多页分片之一，已进入收集器 */
    data class Page(val i: Int, val n: Int, val have: Int) : ScanResult()
    /** 形态C：视频帧（二期） */
    data class Frame(val idx: Int, val n: Int, val have: Int) : ScanResult()
    /** 无法处理 */
    data class Invalid(val reason: String) : ScanResult()
}

object EnvelopeParser {

    /** 顶层入口：把扫到的文本交给对应处理器 */
    fun handle(text: String, collector: PageCollector): ScanResult {
        val t = text.trim()
        if (t.startsWith("{")) {
            return handleJson(t, collector)
        }
        // 二期：return QrFrame.handle(t, frameCollector)
        return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
    }

    /** 处理 JSON 形态（形态A/B） */
    private fun handleJson(t: String, collector: PageCollector): ScanResult {
        val obj = try {
            JsonParser.parseString(t).asJsonObject
        } catch (e: Exception) {
            return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        }
        return if (obj.has("pg")) {
            collector.offer(obj)   // 形态B
        } else {
            validateEnvelope(obj)?.let { ScanResult.Single(it) }
                ?: ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        }
    }

    /** 校验并解析完整信封（形态A 或 收集器重组完成后的 JSON） */
    fun validateEnvelope(obj: com.google.gson.JsonObject): Envelope? {
        if (obj.get("jzt")?.takeIf { it.isJsonPrimitive }?.asInt != 1) return null
        val fmt = obj.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString ?: return null
        if (fmt !in Fmt.ALL) return null
        val name = obj.get("name")?.takeIf { it.isJsonPrimitive }?.asString ?: "未命名"
        val dataEl = obj.get("data") ?: return null
        val data: Any = if (fmt == Fmt.EXCEL) {
            if (!dataEl.isJsonArray) return null
            jsonToRows(dataEl.asJsonArray) ?: return null
        } else {
            dataEl.takeIf { it.isJsonPrimitive }?.asString ?: return null
        }
        return Envelope(fmt, name, data)
    }

    /** excel 二维数组 → List<List<Any?>>；元素仅允许 string/number/boolean/null */
    fun jsonToRows(arr: com.google.gson.JsonArray): List<List<Any?>>? {
        val rows = mutableListOf<List<Any?>>()
        for (rowEl in arr) {
            if (!rowEl.isJsonArray) return null
            val row = mutableListOf<Any?>()
            for (cellEl in rowEl.asJsonArray) {
                row.add(
                    when {
                        cellEl.isJsonNull -> null
                        cellEl.isJsonPrimitive -> when {
                            cellEl.asJsonPrimitive.isBoolean -> cellEl.asBoolean
                            cellEl.asJsonPrimitive.isNumber -> cellEl.asString  // 保留原始数字文本，见下注
                            else -> cellEl.asString
                        }
                        else -> return null   // 出现对象/嵌套数组 → 结构异常
                    }
                )
            }
            rows.add(row)
        }
        return rows
    }
}
```

> **为什么 excel 数字按 `asString` 保留**：Gson 会把 `1` 解析成 `Double 1.0`，导出 CSV 时会变成 `1.0`。按原始文本保留（`asJsonPrimitive.asString`）可原样还原 `1`、`3.5`、`0.001`。展示时同样直接使用该文本，无需再格式化。

#### `protocol/PageCollector.kt`

```kotlin
package com.xxx.infoparse.protocol

import com.google.gson.JsonObject

/** 多页收集器：形态B 的状态机（3.3 节规则 1-6） */
class PageCollector {

    var total = 0; private set                 // pg.n
    private val parts = LinkedHashMap<Int, String>()   // pg.i -> data 片段
    var fmt: String? = null; private set       // 来自第 1 页
    var name: String? = null; private set

    val have: Int get() = parts.size
    val isComplete: Boolean get() = total in 1..have

    /** 缺失页码（升序，最多返回 5 个用于提示） */
    fun missingPreview(): List<Int> =
        (1..total).filter { it !in parts }.take(5)

    /** 收到一页（已确认为带 pg 的 JSON）。返回给 UI 的结论 */
    fun offer(obj: JsonObject): ScanResult {
        val pg = obj.getAsJsonObject("pg") ?: return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        val i = pg.get("i")?.takeIf { it.isJsonPrimitive }?.asInt ?: return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        val n = pg.get("n")?.takeIf { it.isJsonPrimitive }?.asInt ?: return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        val data = obj.get("data")?.takeIf { it.isJsonPrimitive }?.asString
            ?: return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
        if (i < 1 || n < 1 || i > n) return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")

        if (total == 0) {
            total = n
        } else if (n != total) {
            // 3.3 规则 2：页数不一致 → 忽略
            return ScanResult.Invalid("该二维码页数与当前任务不一致，已忽略")
        }
        if (i == 1) {
            fmt = obj.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString
            name = obj.get("name")?.takeIf { it.isJsonPrimitive }?.asString
        }
        parts[i] = data            // 重复 i → 覆盖
        if (isComplete) {
            val env = assemble()
            return env?.let { ScanResult.Single(it) }
                ?: ScanResult.Invalid("数据还原失败，请重新扫描")
        }
        return ScanResult.Page(i, total, have)
    }

    /** 集齐后重组：拼接 → 校验 → 信封 */
    fun assemble(): Envelope? {
        if (!isComplete) return null
        val merged = (1..total).joinToString("") { parts[it]!! }
        val obj = try {
            com.google.gson.JsonParser.parseString(merged).asJsonObject
        } catch (e: Exception) {
            return null
        }
        return EnvelopeParser.validateEnvelope(obj)
    }

    // ---------- 本地暂存（FR-08）：简单 JSON 序列化 ----------
    fun serialize(): String {
        val map = org.json.JSONObject()
        map.put("total", total)
        fmt?.let { map.put("fmt", it) }
        name?.let { map.put("name", it) }
        val ps = org.json.JSONObject()
        parts.forEach { (k, v) -> ps.put(k.toString(), v) }
        map.put("parts", ps)
        return map.toString()
    }

    companion object {
        fun restore(json: String): PageCollector? = try {
            val map = org.json.JSONObject(json)
            val c = PageCollector()
            val f = map.optInt("total", 0); if (f > 0) { c.total = f }
            // 反射置 private set 太绕——改为下面构造函数版本更佳，见注
            null
        } catch (e: Exception) { null }
    }
}
```

> **实现注意**：`restore` 的字段回填建议改为「构造函数注入」写法避免反射——把 `total/fmt/name/parts` 改为主构造参数并重载：`class PageCollector(total: Int = 0, fmt: String? = null, name: String? = null, parts: Map<Int, String> = emptyMap())`。文档给出的是逻辑规范，字段可见性可按团队习惯调整，但**状态机行为必须与 3.3 一致**。

### 4.3 扫码页（CameraX + ML Kit 完整代码）

#### `scan/CameraScanner.kt`

```kotlin
package com.xxx.infoparse.scan

import android.content.Context
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class CameraScanner(
    private val context: Context,
    private val onQrText: (String) -> Unit,
) {
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private var lastAt = 0L

    /** 绑定相机。view 为布局中的 PreviewView */
    fun start(lifecycleOwner: LifecycleOwner, view: PreviewView) {
        val providerFuture = ProcessCameraProvider.getInstance(context)
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(view.surfaceProvider)
            }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            analysis.setAnalyzer(executor) { proxy -> process(proxy) }
            provider.unbindAll()
            provider.bindToLifecycle(
                lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis
            )
        }, ContextCompat.getMainExecutor(context))
    }

    private fun process(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null) { proxy.close(); return }
        // 500ms 节流：同一码面会连续回调，避免重复触发
        val now = System.currentTimeMillis()
        val throttled = now - lastAt < 500
        if (throttled) { proxy.close(); return }
        lastAt = now
        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener { codes ->
                codes.firstOrNull()?.rawValue?.let { onQrText(it) }
            }
            .addOnCompleteListener { proxy.close() }
    }

    fun stop() { executor.shutdown() }
}
```

MainActivity 中使用：

```kotlin
class MainActivity : AppCompatActivity() {
    private lateinit var scanner: CameraScanner
    private val collector = PageCollector()

    private val permLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            if (ok) startCamera() else Toast.makeText(this, "需要相机权限才能扫码", Toast.LENGTH_LONG).show()
        }
    private val pickImage =
        registerForActivityResult(ActivityResultContracts.GetMultipleContents()) { uris ->
            if (uris.isNotEmpty()) decodeImages(uris)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        // ... 布局：PreviewView + 「相册导入」按钮 + 状态 TextView
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) startCamera()
        else permLauncher.launch(Manifest.permission.CAMERA)

        findViewById<Button>(R.id.btnAlbum).setOnClickListener {
            pickImage.launch("image/*")
        }
    }

    private fun startCamera() {
        scanner = CameraScanner(this, onQrText = { text -> runOnUiThread { onScanned(text) } })
        scanner.start(this, findViewById(R.id.previewView))
    }

    private fun onScanned(text: String) {
        when (val r = EnvelopeParser.handle(text, collector)) {
            is ScanResult.Single -> openResult(r.envelope)
            is ScanResult.Page ->
                statusView.text = "已收集 ${r.have}/${r.n} 张" +
                    (if (r.have < r.n) "，请继续扫描" else "")
            is ScanResult.Invalid -> Toast.makeText(this, r.reason, Toast.LENGTH_SHORT).show()
            else -> {}
        }
    }

    /** 相册批量导入：逐张解码后按顺序喂给同一收集器 */
    private fun decodeImages(uris: List<Uri>) {
        statusView.text = "正在解析图片…"
        thread {
            val texts = uris.mapNotNull { ImageDecoder.decodeUri(this, it) }
            runOnUiThread {
                if (texts.isEmpty()) {
                    statusView.text = "图片中未识别到二维码"; return@runOnUiThread
                }
                var result: ScanResult? = null
                for (t in texts) {
                    result = EnvelopeParser.handle(t, collector)
                    if (result is ScanResult.Single) break
                }
                when (val r = result) {
                    is ScanResult.Single -> openResult(r.envelope)
                    is ScanResult.Page ->
                        statusView.text = "已收集 ${r.have}/${r.n} 张，请补扫缺失页"
                    else -> statusView.text = (r as? ScanResult.Invalid)?.reason ?: ""
                }
            }
        }
    }
}
```

#### `scan/ImageDecoder.kt`（相册图片，含防 OOM 缩放）

```kotlin
package com.xxx.infoparse.scan

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage

object ImageDecoder {
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
    )

    /** 同步解码单张图片，返回二维码文本（可能为多码图返回多段，取全部） */
    fun decodeUri(context: Context, uri: Uri): List<String> {
        val bitmap = loadScaled(context, uri, 2048) ?: return emptyList()
        val image = InputImage.fromBitmap(bitmap, 0)
        val out = mutableListOf<String>()
        val task = scanner.process(image)
        try {
            val codes = com.google.android.gms.tasks.Tasks.await(task, 10, java.util.concurrent.TimeUnit.SECONDS)
            for (b in codes) {
                b.rawValue?.let { out.add(it) }
                // 如遇中文乱码，改用 b.rawBytes 与 String(bytes, Charsets.UTF_8)
            }
        } catch (_: Exception) { }
        return out
    }

    private fun loadScaled(context: Context, uri: Uri, maxDim: Int): Bitmap? = try {
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, opts) }
        var sample = 1
        while (maxOf(opts.outWidth, opts.outHeight) / sample > maxDim) sample *= 2
        val o = BitmapFactory.Options().apply { inSampleSize = sample }
        context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, o) }
    } catch (_: Exception) { null }
}
```

> **不要把长边压到 2048 以下太多**：版本 40 的二维码点阵约 177×177 模块，加上静区与放大余量，建议 `maxDim ≥ 2048`。若版本 40 大码识别率低，把 `maxDim` 提到 4096。

### 4.4 结果页（ResultActivity）

接收 `Envelope`（建议用内存单例或 Intent 传 JSON），页面元素与渲染规则：

| 元素 | 规则 |
| --- | --- |
| 格式徽标 | `Fmt.label(fmt)`：纯文本 / Markdown / Word 文档 / Excel 表格（信息卡内着色徽标） |
| 来源名 | `name` 原样展示（顶栏标题） |
| 统计行 | 信息卡内：excel「共 N 行」；其他「共 X 字符」，另固定展示「已去除样式与宏，仅保留数据信息」 |
| 内容卡 | 文本类：TextView 等宽字体（`monospace`），**最多渲染前 4000 字符**，超出追加「…（内容较长，导出文件可查看全部）」；excel：表格预览（见下），**最多渲染前 100 行**；`file`：不展示内容，显示「原始文件已完整封装，导出可还原」提示 |
| 按钮 | 底部动作导航栏（藏蓝底、纯图标、长按气泡显示名称）：「导出文件」（4.7，下载图标）、「复制全部」（ClipboardManager，excel 用 TSV 文本）、「分享文件」；「重新扫描」为导航栏上方独立按钮（回识别页；从主页历史卡片进入时隐藏，返回键即回主页） |

excel 表格预览最简单实现：`HorizontalScrollView` 包 `TableLayout`，逐行 `TableRow` 逐格 `TextView`；首行加粗作为表头。超过 100 行截断并提示。

### 4.5 导出与分享

#### 4.5.1 excel → CSV（`util/Csv.kt`）

```kotlin
object Csv {
    /** 二维数组 → CSV 文本（含 UTF-8 BOM，Excel 打开不乱码） */
    fun fromRows(rows: List<List<Any?>>): String {
        val sb = StringBuilder("\uFEFF")
        rows.forEach { row ->
            sb.append(row.joinToString(",") { escape(cell(it)) }).append("\r\n")
        }
        return sb.toString()
    }

    private fun cell(v: Any?): String = when (v) {
        null -> ""
        is Boolean -> if (v) "TRUE" else "FALSE"
        else -> v.toString()
    }

    /** 标准 CSV 转义：含 逗号/引号/换行 时加引号，内部引号翻倍 */
    fun escape(s: String): String =
        if (s.contains(',') || s.contains('"') || s.contains('\n') || s.contains('\r'))
            "\"" + s.replace("\"", "\"\"") + "\""
        else s
}
```

> 行结束符用 `\r\n`（Excel 兼容）；BOM `\uFEFF` 写在文件最前。

#### 4.5.2 保存到「下载」（API 29+ 免权限，`export/Exporter.kt`）

```kotlin
object Exporter {
    /** 返回保存后的 content Uri */
    fun saveToDownloads(context: Context, fileName: String, text: String, mime: String): Uri {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, fileName)
            put(MediaStore.Downloads.MIME_TYPE, mime)
        }
        val resolver = context.contentResolver
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)!!
        resolver.openOutputStream(uri)!!.use { it.write(text.toByteArray(Charsets.UTF_8)) }
        return uri
    }

    fun shareText(context: Context, text: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
        }
        context.startActivity(Intent.createChooser(intent, "分享内容"))
    }

    fun shareFile(context: Context, uri: Uri, mime: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = mime
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(intent, "分享文件"))
    }
}
```

导出入口逻辑（结果页按钮）：

```
fmt=text      → 文件 name.txt   (mime text/plain)     保存 + 分享
fmt=markdown  → 文件 name.md    (mime text/markdown)  保存 + 分享
fmt=word      → 文件 name.txt   (mime text/plain)     保存 + 分享
fmt=excel     → 文件 name.csv   (mime text/csv)       保存 + 分享
```

文件名清洗：`name.replace(Regex("""[\\/:*?"<>|]"""), "_")`，重名时 MediaStore 自动追加 `(1)`，无需处理。

### 4.6 多页收集暂存（FR-08）

- 每次收集器状态变化时把 `collector.serialize()` 存入 `SharedPreferences`（键 `pending_collect`）；
- APP 启动时读取：存在且未完成 → 恢复收集器并在主页显示「有待继续的收集任务：已收集 x/n」与「放弃」按钮；
- 收集完成或用户放弃后清除该键。
- ⚠️ 序列化可能包含 MB 级文本，SharedPreferences 存储上限实操约 1~2MB；超出时可改写内部存储文件（`context.filesDir/pending_collect.json`），逻辑不变。

---

## 5. 二期实现指引（可选）：二维码视频解析

> 一期交付并通过验收后再启动。协议见 3.4。

### 5.1 技术路线

| 环节 | 方案 | 说明 |
| --- | --- | --- |
| 视频选入 | `ActivityResultContracts.GetContent` `video/mp4` | 免权限 |
| 逐帧取图 | `MediaMetadataRetriever.getFrameAtTime(t, OPTION_CLOSEST)` | 简单可靠；速度约 3-10 帧/秒，可接受 |
| 步进时刻 | 封装端帧率恒为 **15fps**：第 i 帧时刻 = `i × 1_000_000 / 15` 微秒 | 以帧头声明的 `n` 为循环上限 |
| 扫码 | 同一期 `ImageDecoder`（Bitmap → ML Kit） | 复用 |
| 重组 | `QrFrame`（下方）系统位路径 | 缺帧时提示重扫或启用附录 C |

### 5.2 帧解析与系统位重组（`protocol/QrFrame.kt` 完整实现）

```kotlin
package com.xxx.infoparse.protocol

import android.util.Base64

/** 单帧解析结果 */
data class FrameHead(val idx: Int, val n: Int, val k: Int, val m: Int)

object QrFrame {

    /** 解析帧头；不合法返回 null */
    fun parseHead(text: String): Pair<FrameHead, ByteArray>? {
        if (text.length < 13) return null
        return try {
            val head = Base64.decode(text.substring(0, 12), Base64.DEFAULT)
            if (head.size != 9 || head[6].toInt() != 0) return null
            val idx = u24(head, 0); val n = u24(head, 3)
            val k = head[7].toInt() and 0xFF; val m = head[8].toInt() and 0xFF
            if (k < 1 || m < k) return null
            val payload = Base64.decode(text.substring(12), Base64.DEFAULT)
            Pair(FrameHead(idx, n, k, m), payload)
        } catch (e: Exception) { null }
    }

    private fun u24(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 16) or ((b[off + 1].toInt() and 0xFF) shl 8) or (b[off + 2].toInt() and 0xFF)

    /**
     * 视频帧收集器（系统位优先路径）。
     * 用法：每个扫码文本 offer 一次；collectToEnvelope() 在进度 100% 后调用。
     */
    class Collector {
        var n = 0; private set
        var k = 0; private set
        var m = 0; private set
        private val shares = HashMap<Int, ByteArray>()   // idx -> share 字节
        private var chunkSize = -1

        val have: Int get() = shares.size

        fun offer(text: String): ScanResult {
            val (head, payload) = parseHead(text) ?: return ScanResult.Invalid("未识别到「信息传输」封装的二维码内容")
            if (n == 0) { n = head.n; k = head.k; m = head.m }
            else if (head.n != n || head.k != k || head.m != m) return ScanResult.Invalid("该二维码与当前视频任务不一致，已忽略")
            if (chunkSize < 0) chunkSize = payload.size
            if (payload.size != chunkSize) return ScanResult.Invalid("视频帧数据异常")
            shares[head.idx] = payload
            return ScanResult.Frame(head.idx, n, have)
        }

        /**
         * 系统位重组（3.4.2 步骤 2-5）。
         * 返回信封 JSON 字节；若存在缺失原始块返回 null（缺帧）。
         */
        fun assembleSystematic(): ByteArray? {
            if (n == 0 || have < n) return null
            val nblocks = n / m
            val total = k * nblocks
            val chunks = arrayOfNulls<ByteArray>(total)
            for ((idx, payload) in shares) {
                val g = idx % nblocks
                val s = idx / nblocks
                if (s < k) chunks[s * nblocks + g] = payload
            }
            if (chunks.any { it == null }) return null   // 缺原始块（理论上有 n 帧不会发生）
            val joined = chunks.requireNoNulls().reduce { a, b -> a + b }
            if (joined.size < 4) return null
            val len = ((joined[0].toInt() and 0xFF) shl 24) or ((joined[1].toInt() and 0xFF) shl 16)
                    or ((joined[2].toInt() and 0xFF) shl 8) or (joined[3].toInt() and 0xFF)
            if (len < 0 || 4 + len > joined.size) return null
            return joined.copyOfRange(4, 4 + len)
        }
    }
}
```

> ⚠️ `chunkSize`：`_chunk_data` 会把每个分块 0x00 填充到等长再参与纠错，因此**所有 share 等长**，直接以首帧长度为准即可；拼接后由 4 字节长度头截出真实数据。

### 5.3 驱动流程（VideoActivity 要点）

```kotlin
val retriever = MediaMetadataRetriever()
retriever.setDataSource(path)
val durationUs = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)!!.toLong() * 1000
val collector = QrFrame.Collector()
val frameUs = 1_000_000L / 15L
var t = 0L
while (t < durationUs) {
    val bmp = retriever.getFrameAtTime(t, MediaMetadataRetriever.OPTION_CLOSEST) ?: break
    val texts = ImageDecoder.decodeBitmap(bmp)   // 同 4.3 的 ML Kit 调用，需补一个 decodeBitmap 重载
    for (txt in texts) {
        when (val r = collector.offer(txt)) {
            is ScanResult.Frame -> updateProgress(r.have, r.n)
            is ScanResult.Invalid -> { showToast(r.reason); return }
            else -> {}
        }
    }
    t += frameUs
}
val envBytes = collector.assembleSystematic()
if (envBytes == null) showToast("视频帧不全，请重新解析或重新传输原始视频文件")
else parseEnvelope(envBytes.toString(Charsets.UTF_8))   // 复用 4.2 校验
```

> 注意 `decodeBitmap` 与 `decodeUri` 共用 ML Kit scanner，返回 `List<String>`；照 4.3 的模式实现即可。

---

## 6. 异常与边界处理总表

| # | 场景 | 检测点 | 处理 | 提示文案 |
| --- | --- | --- | --- | --- |
| E-01 | 扫到普通网址/名片码 | 非 `{` 开头且帧头不合法 | 忽略本次 | 未识别到「信息传输」封装的二维码内容 |
| E-02 | `jzt` ≠ 1 | 信封校验 | 拒绝 | 信封协议版本不受支持 |
| E-03 | `fmt` 不认识 | 信封校验 | 拒绝 | 未知的文档格式声明：<fmt> |
| E-04 | 多页未集齐就点导出/查看 | `collector.isComplete == false` | 保持收集状态 | 已收集 x/n 张，还差：第 a、b 张… |
| E-05 | 新页页数 n 与任务不一致 | PageCollector 规则 2 | 忽略该页 | 该二维码页数与当前任务不一致，已忽略 |
| E-06 | 重复扫同一页 | 同 `pg.i` 覆盖 | 静默覆盖 | （无提示，进度不变则刷新进度） |
| E-07 | 拼接后 JSON 解析失败 | `assemble()` 返回 null | 清空收集器重来 | 数据还原失败，请重新扫描 |
| E-08 | excel data 非数组/元素含对象 | `jsonToRows` 返回 null | 拒绝 | Excel 数据结构异常 |
| E-09 | 图片里扫不出码 | ML Kit 返回空 | 逐张提示可跳过 | 图片中未识别到二维码 |
| E-10 | 相机权限拒绝 | 权限回调 | 停用扫码、保留相册入口 | 需要相机权限才能扫码 |
| E-11 | 超大文本渲染 | 预览截断（4000 字符/100 行） | 全量走导出 | （预览区尾部省略提示） |
| E-12 | name 含非法文件名字符 | 导出前清洗 | 替换为 `_` | （无提示） |
| E-13 | 视频帧不全（二期） | `assembleSystematic` 返回 null | 要求重扫 | 视频帧不全，请重新解析或重新传输原始视频文件 |

---

## 7. 测试指南

### 7.1 测试素材生成

**方式一（推荐）**：在部署了 JZToolsHub 的站点打开「信息传输 → 信息封装」，生成所需素材（单张、多张 ZIP、视频）。

**方式二**：在能运行插件环境的机器执行以下脚本（依赖插件包 `qrcode`），批量产出测试 PNG/MP4：

```python
# gen_test_materials.py —— 放到 JZToolsHub 项目根目录运行
import sys
sys.path.insert(0, r"D:\JZToolsHub")
sys.path.insert(0, r"D:\JZToolsHub\plugins\info-transfer")
import os
from backend import routes as R

OUT = r"C:\temp\qr_test_materials"
os.makedirs(OUT, exist_ok=True)

# 1) 单张中文文本
env = R.build_envelope("text", "会议通知", "本周五 14:00 在三楼会议室召开季度总结会，请准时参加。")
R.encode_static_output("text", "会议通知", env, 15, OUT, "case1_single")

# 2) 多页拆分（5 页）
big = "机密数据行-%d：" + "甲乙丙丁戊己庚辛壬癸" * 50 + "\n"
env2 = R.build_envelope("text", "长文示例", "".join(big % i for i in range(8)))
paths, n = R.encode_static_output("text", "长文示例", env2, 15, OUT, "case2_multi")
print("case2 页数:", n)

# 3) excel 表格（含中文/数字/空值）
env3 = R.build_envelope("excel", "花名册", [["姓名","部门","备注"],["张三","刑侦",1],["李四","网安",None]])
R.encode_static_output("excel", "花名册", env3, 15, OUT, "case3_excel")

# 4) markdown
env4 = R.build_envelope("markdown", "值班安排", "# 值班安排\n\n- 周一：张三\n- 周二：李四")
R.encode_static_output("markdown", "值班安排", env4, 15, OUT, "case4_md")

# 5) 二维码视频（二期用）
env5 = R.build_envelope("text", "视频示例", "视频往返验证。" * 300)
mp4 = os.path.join(OUT, "case5_video.mp4")
R.encode_to_video(env5, 15, mp4)
print("case5 视频:", mp4)
```

### 7.2 用例表

| 用例 | 步骤 | 预期 |
| --- | --- | --- |
| TC-01 | 扫 case1 单张码 | 直出结果：徽标「纯文本」、名称「会议通知」、内容完整、无提示 |
| TC-02 | 相册导入 case1 图片 | 同 TC-01 |
| TC-03 | 逐张扫 case2 全部 5 页（乱序） | 每页提示进度；集齐后自动出结果，内容与原文一致 |
| TC-04 | 相册一次多选 case2 全部图片 | 直出结果 |
| TC-05 | 只扫 case2 前 3 页 | 停留在收集状态：「已收集 3/5 张，还差：第 4、5 张」 |
| TC-06 | TC-05 后杀掉 APP 重启 | 恢复收集任务（FR-08），补扫后正常出结果 |
| TC-07 | case2 集齐前混扫其他二维码 | 提示「未识别到…」，收集进度不受影响 |
| TC-08 | case3 excel | 表格预览正确；空单元格显示为空；导出 CSV 用 Excel 打开无乱码、行列一致 |
| TC-09 | case4 markdown | 导出 `值班安排.md`，内容含换行 |
| TC-10 | 扫普通网址二维码 | 提示「未识别到…」，无崩溃 |
| TC-11 | word 导出 | 生成 `xxx.txt`（与桌面端行为一致，word 数据导出纯文本） |
| TC-12 | 复制全部 | 剪贴板内容与预览全文一致（excel 为 TSV） |
| TC-13 | (二期) case5 视频 | 解析成功，内容一致；故意截断视频 → E-13 文案 |
| TC-14 | 断网状态全流程 | 全部功能可用（APK 未声明 INTERNET 权限即天然通过） |
| TC-15 | 弱光/远距扫码 | 距离 10~30cm、对焦清晰时可识别；过近/过远给「未识别到」而非崩溃 |

### 7.3 真机扫描技巧（写给测试同学）

- 屏幕显示二维码时**调高亮度、关闭深色模式**；
- 距离以二维码完整入框且略有余量为准（约 10~30cm），手机**不要贴太近**（近距易失焦）；
- 大版本码（点阵密）识别稍慢属正常，稳住 1~2 秒；
- 拍摄视频解析时，务必用**文件传输**拿到原始 MP4（微信/QQ「发送文件」不压缩）；经朋友圈/群聊压缩过的视频可能无法解析。

---

## 8. 验收清单

**功能**
- [ ] FR-01~08 全部实现且通过 TC-01~TC-12
- [ ] （若含二期）TC-13 通过

**协议一致性**
- [ ] 信封校验逻辑与 3.1 完全一致（jzt/fmt/name/data）
- [ ] 多页状态机与 3.3 规则 1-6 完全一致（页数一致性、覆盖、乱序、缺页文案）
- [ ] 错误文案与 3.5 表逐字一致

**非功能**
- [ ] Manifest 未声明 INTERNET 权限；飞行模式全流程可用
- [ ] 无权限弹窗滥用：仅 CAMERA 一个运行时权限
- [ ] excel 千行数据展示不卡顿（预览截断生效）
- [ ] Android 10/12/14 真机各一台通过

**交付物**
- [ ] APK（release 签名）+ 源码仓库 + 本清单勾选记录
- [ ] 已知问题列表（如有）

---

## 9. 附录

### 附录 A：协议速查卡（打印贴墙版）

```
┌─ 二维码内容判别 ─────────────────────────────────────────┐
│ "{" 开头 → JSON 信封                                     │
│   有 "pg" → 多页分片 {jzt,pg:{i,n},data}（首页含 fmt/name）│
│   无 "pg" → 完整信封 {jzt:1, fmt, name, data}             │
│ 其他     → base64 帧头 12 字符（二期视频帧）               │
├─ 信封字段 ───────────────────────────────────────────────┤
│ jzt=1；fmt ∈ {text,markdown,word,excel}；name 不含扩展名   │
│ data：text/md/word → 字符串；excel → 二维数组(仅4类值)      │
├─ 多页重组 ───────────────────────────────────────────────┤
│ total=首个 n；n 不一致→忽略；同 i→覆盖；按 i 升序拼 data     │
│ 拼接结果 = 完整信封 JSON 文本                               │
├─ 导出 ───────────────────────────────────────────────────┤
│ text→.txt  markdown→.md  word→.txt  excel→.csv(BOM)       │
├─ 视频帧（二期）────────────────────────────────────────┤
│ 头12字符b64→9字节[3B idx][3B n][0x00][k][m]；载荷b64→等长块 │
│ nblocks=n/m；g=idx%nblocks；s=idx/nblocks；s<k 为原始块     │
│ 全局块号=c*nblocks+g；顺序拼接；前4B大端=长度L；[4,4+L)=JSON │
└──────────────────────────────────────────────────────────┘
```

### 附录 B：视频帧重组 worked example（真实数据）

封装端对 2200 字符文本（版本 15）编码得到：分块 6 → `k=6, m=10`，冗余后总帧 `n=10`，`nblocks = 10/10 = 1`。

| 帧 idx | 组 g | share s | 角色 |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 原始块 c=0 |
| 1 | 0 | 1 | 原始块 c=1 |
| … | 0 | … | … |
| 5 | 0 | 5 | 原始块 c=5 |
| 6 | 0 | 6 | 校验块（parity） |
| … | 0 | … | 校验块 |
| 9 | 0 | 9 | 校验块 |

帧 0 的二维码内容前 16 字符：`AAAAAAAKAAYKAAAI`；其帧头 12 字符 `AAAAAAAKAAYK` 解码为 9 字节：

```
00 00 00 | 00 00 0A | 00 | 06 | 0A
idx=0     n=10      保留  k=6  m=10
```

APP 处理：收齐 10 帧 → 取 s<6 的帧（idx 0..5）按全局块号 `c*1+0 = c` 排列拼接 → 前 4 字节大端为长度 L → 取 [4, 4+L) → UTF-8 解码 = `{"jzt":1,"fmt":"text","name":"示例","data":"XXX…"}`。

> 分块数 ≤ 255 时恒有 `nblocks = 1`（本插件单帧载荷最大约 2.9KB，需数据 > 约 740KB 才会出现多组），一期二期实现先按 `nblocks=1` 理解即可，代码仍按通式编写以保安全。

### 附录 C：zfec 兼容纠错解码（Kotlin 完整实现，仅二期缺帧时需要）

> 以下实现与桌面端 zfec 库（GF(2^8)、本原多项式 0x11d、Vandermonde 系统化矩阵）**逐位兼容**，构造算法已与 zfec 全参数比对验证。仅当某组缺原始块但 share ≥ k 时使用。

```kotlin
/** GF(2^8) 与 zfec 兼容的 RS 解码 */
object ZfecCompat {

    private const val PP = 0x11d
    private val EXP = IntArray(512)
    private val LOG = IntArray(256)

    init {
        var x = 1
        for (i in 0 until 255) {
            EXP[i] = x; LOG[x] = i
            x = x shl 1
            if (x and 0x100 != 0) x = x xor PP
        }
        for (i in 255 until 512) EXP[i] = EXP[i - 255]
    }

    private fun gmul(a: Int, b: Int): Int =
        if (a == 0 || b == 0) 0 else EXP[LOG[a] + LOG[b]]

    private fun ginv(a: Int): Int = EXP[255 - LOG[a]]

    /** zfec fec_new 的编码矩阵（前 k 行单位阵，后 m-k 行为校验行） */
    fun buildEncodeMatrix(k: Int, m: Int): Array<IntArray> {
        // n×k Vandermonde：第 r 行第 c 列 = EXP[(r*c) % 255]，第 0 行特例 [1,0,..]
        val V = Array(m) { IntArray(k) }
        V[0][0] = 1
        for (r in 1 until m) for (c in 0 until k) V[r][c] = EXP[(r * c) % 255]
        // 求逆 top k×k（GF 高斯消元）
        val aug = Array(k) { r -> V[r].toIntArray() + IntArray(k) { c -> if (c == r) 1 else 0 } }
        for (col in 0 until k) {
            var piv = col
            while (piv < k && aug[piv][col] == 0) piv++
            if (piv != col) { val t = aug[col]; aug[col] = aug[piv]; aug[piv] = t }
            val iv = ginv(aug[col][col])
            aug[col] = IntArray(2 * k) { j -> gmul(aug[col][j], iv) }
            for (r in 0 until k) if (r != col && aug[r][col] != 0) {
                val f = aug[r][col]
                aug[r] = IntArray(2 * k) { j -> aug[r][j] xor gmul(f, aug[col][j]) }
            }
        }
        val inv = Array(k) { r -> aug[r].copyOfRange(k, 2 * k) }
        // bottom = V[k:] × inv
        val out = Array(m) { r ->
            if (r < k) IntArray(k) { c -> if (c == r) 1 else 0 }
            else IntArray(k) { j ->
                var acc = 0
                for (c in 0 until k) acc = acc xor gmul(V[r][c], inv[c][j])
                acc
            }
        }
        return out
    }

    /**
     * 解码一组 share：present = share序号(0..m-1) -> 块字节（等长）。
     * 返回 k 个原始块。要求 present.size >= k。
     */
    fun decode(k: Int, m: Int, present: Map<Int, ByteArray>): Array<ByteArray> {
        require(present.size >= k) { "share 不足" }
        val enc = buildEncodeMatrix(k, m)
        val idx = present.keys.sorted().take(k)            // 任取 k 个
        val chunkSize = present.values.first().size
        // k×k 矩阵：行 = 对应 share 的编码行（<k 为单位行）
        val mat = Array(k) { r ->
            val s = idx[r]
            if (s < k) IntArray(k) { c -> if (c == s) 1 else 0 } else enc[s]
        }
        val inv = invert(mat, k)
        // originals = inv × blocks
        val out = Array(k) { ByteArray(chunkSize) }
        for (r in 0 until k) {
            val row = inv[r]
            for (c in 0 until k) {
                val coef = row[c]
                if (coef == 0) continue
                val blk = present.getValue(idx[c])
                for (b in 0 until chunkSize) out[r][b] = (out[r][b].toInt() xor gmul(coef, blk[b].toInt() and 0xFF)).toByte()
            }
        }
        return out
    }

    /** GF 高斯消元求逆 */
    private fun invert(mat: Array<IntArray>, n: Int): Array<IntArray> {
        val aug = Array(n) { r -> mat[r].toIntArray() + IntArray(n) { c -> if (c == r) 1 else 0 } }
        for (col in 0 until n) {
            var piv = col
            while (piv < n && aug[piv][col] == 0) piv++
            require(piv < n) { "矩阵奇异，share 组合不可逆" }
            if (piv != col) { val t = aug[col]; aug[col] = aug[piv]; aug[piv] = t }
            val iv = ginv(aug[col][col])
            aug[col] = IntArray(2 * n) { j -> gmul(aug[col][j], iv) }
            for (r in 0 until n) if (r != col && aug[r][col] != 0) {
                val f = aug[r][col]
                aug[r] = IntArray(2 * n) { j -> aug[r][j] xor gmul(f, aug[col][j]) }
            }
        }
        return Array(n) { r -> aug[r].copyOfRange(n, 2 * n) }
    }
}
```

**自测向量（来自真实 zfec，必须全部通过后再上线）**：

```
向量1：k=2, m=3
  原始块: ["01020304", "05060708"]（16 进制）
  全部 share: s0=01020304, s1=05060708, s2=090a0b1c
  用 ZfecCompat.decode(2, 3, mapOf(1 to s1, 2 to s2)) 应还原 ["01020304","05060708"]

向量2：k=3, m=5
  原始块: ["41414141", "42424242", "43434343"]
  全部 share: 41414141 / 42424242 / 43434343 / 55555555 / 29292929
  用 decode(3, 5, mapOf(1 to s1, 2 to s2, 3 to s3)) 应还原三个原始块
```

在 `QrFrame.Collector` 中接入：`assembleSystematic()` 返回 null（缺原始块）时，按组尝试 `ZfecCompat.decode` 补齐；某组 share < k 则整体失败（E-13 文案）。

### 附录 D：FAQ（弱开发者常见坑）

**FAQ-1 为什么 minSdk 定 29？**
API 29 起 `MediaStore.Downloads` 写「下载」目录**不需要任何存储权限**。若必须支持 Android 8/9，需追加 `WRITE_EXTERNAL_STORAGE` 运行时权限与旧路径分支，复杂度明显上升——除非需求方明确要求，不建议。

**FAQ-2 ML Kit 与国产手机**
本文用的是 `com.google.mlkit:barcode-scanning`（**bundled** 版，模型打进 APK，APK 增大约 2~4MB），运行时不需要 Google Play 服务，华为/荣耀等无 GMS 手机可正常使用。不要误用 `play-services-mlkit-barcode-scanning`（unbundled 版依赖 GMS）。

**FAQ-3 Gradle 同步失败**
国内网络建议在 `settings.gradle.kts` 配置镜像仓库（阿里云 maven mirror），或配置代理后再 Sync。

**FAQ-4 扫码回调疯狂触发**
CameraX 的 ImageAnalysis 是连续回调，必须像 4.3 那样做节流（500ms）+ `STRATEGY_KEEP_ONLY_LATEST`；否则 UI 会被刷爆。

**FAQ-5 中文乱码**
ML Kit `rawValue` 按内置规则解码，本协议二维码为 UTF-8 字节模式，正常不乱码。若个别机型乱码，改用 `barcode.rawBytes` 后 `String(bytes, Charsets.UTF_8)`。

**FAQ-6 相册大图 OOM**
必须按 4.3 `loadScaled` 先采样再解码；版本 40 大码不要压到 2048 以下。

**FAQ-7 多页收集时用户中途换了图片集**
以首个页面的 `pg.n` 为准（3.3 规则 2），不一致的页忽略并提示；无需更复杂的逻辑。

**FAQ-8 word 数据里的「A | B」是什么**
word 信封的 data 是纯文本，表格行以 ` | ` 分隔单元格混排在文本里（协议如此，桌面端同样展示）。APP 按纯文本显示即可，不要尝试还原表格。

**FAQ-9 excel 导出在 Excel 里打开乱码**
CSV 必须带 UTF-8 BOM（`\uFEFF`），且行结束用 `\r\n`，见 4.5.1。

**FAQ-10 能不能直接解析桌面端生成的多张 ZIP**
不必支持。手机端交互是「逐张扫描 / 相册多选图片」，ZIP 是桌面 Web 端的产物形态，协议重组规则（按 `pg.i`）与载体无关。

### 附录 E：与桌面端行为对照及修订记录

| 行为 | 桌面端（Web） | 本 APP |
| --- | --- | --- |
| 解码引擎 | 图片 cv2 / 视频 cv2 逐帧 | 图片与实时流 ML Kit；（二期）视频 retriever+ML Kit |
| word 导出 | `.txt` 纯文本 | v2 起封装为 `file` 完整传输，还原即原始文件；旧 word 码由逐段文本重建 `.docx` |
| excel 导出 | `.xlsx` 重建表格 | v2 起封装为 `file` 完整传输，还原即原始文件；旧 excel 码导出 `.csv`（含 BOM） |
| 多页收集 | 一次上传全部图片 | 支持分次扫描收集（更贴合手机交互） |
| 错误文案 | 见 3.5 表 | 与桌面端文案对齐 |

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-09-04 | 首版：协议规范、一期实现指引、二期视频指引、附录（速查卡/worked example/zfec 兼容解码/FAQ） |
| v1.1 | 2026-09-06 | UI 改版：新增主页（识别历史卡片 + 相机 FAB，识别结果自动入史 `history/HistoryStore`）；扫码页更名为识别页 `ScanActivity` 并新增「重置」（清空多页收集器/视频流帧收集器/暂存）；导入入口改为小字提示「从相册导入」+「导入图片」（多选）/「导入视频」；结果页 Material3 化（Toolbar/内容卡，预览截断规则不变）。协议与错误文案（3.5）无变更。 |
| v1.2 | 2026-09-07 | 主题色定为公安藏蓝（#1C2F5E/#121F40，tonal 容器日夜适配）；主页/识别页/结果页沉浸式系统栏（手势条按 insets 避让）；「重置」按钮暂时移除（引入时因初始化顺序导致识别页闪退，恢复时需保证视图绑定先于 insets 监听）；结果页卡片化（信息卡 + 内容卡），导出/复制/分享文件改为纯图标按钮。 |
| v1.3 | 2026-09-07 | 结果页去除「分享内容」；导出/复制/分享文件移入底部动作导航栏（藏蓝底纯图标，labelVisibilityMode=unlabeled，长按气泡显示名称，背景延伸至手势条区域）；「重新扫描」保留为导航栏上方独立按钮。 |
| v1.4 | 2026-09-07 | 修复 word 文档导出/分享：APP 端由逐段文本重建可编辑 `.docx`（util/DocxWriter，最小 OOXML），文件名 = name + `.docx`（防重复后缀），分享 mime 改为 docx 标准类型——不再导出 `.txt`。二维码数据仅承载逐段文本（3.1.1），原文样式/宏/图片无法还原；桌面端 Web 解析导出行为不变。 |
| v1.5 | 2026-09-07 | **协议 v2：文档完整传输**。封装端（info-transfer 插件）word/excel 及其他文档改为原始文件完整传输——新增 `fmt=file`（data=文件字节 base64，name 含扩展名），不再解析文档内容，.doc/.wps/.pdf 等同步放开；`word/excel` 为兼容保留（旧码可解析）。APP 端支持 file 格式：还原原始文件（导出/分享文件名与字节与原文档一致）、结果页不展示内容预览、历史卡片显示文件字节数；复制对 file 提示改用导出。Web 端解析 file 直接下载原文件。3.1/3.1.1/3.1.2/附录 E 已同步。 |

> 协议变更须同步修订本文档并升版本号；桌面端插件 `info-transfer` 的 `routes.py` 为协议最终裁定依据。
