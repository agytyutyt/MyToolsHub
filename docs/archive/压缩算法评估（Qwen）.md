# 信息传输插件压缩算法评估

> **⚠️ 已归档（2026-09-17）** —— 归档原因：本文与《压缩算法评估（DS）》是**同题独立复核**，
> 两者在"已压缩容器直压无效""k-of-m 早停判据"两点上结论一致；DS 版补充了 LZMA2 选型、
> 协议结构浪费、格式预处理与完整性校验，**已全部合入现行总纲**
> `docs/eval/信息传输格式开销评估与优化方案.md`。
> **本文的独特留存价值**：附录 B 的**开源项目许可证核实表**（GitHub API 核实，2026-09）
> 与 §2 的候选方案调研矩阵，是 DS 版未覆盖的部分。
> 本文件**内容不再维护**，仅作复核证据留存。
> 文中路径引用已按 2026-09-17 的 `docs/` 分层结构更新。


> 评估日期：2026-09-15 ｜ 对象插件：`plugins/info-transfer`（信息传输）
> 目标：在"两个物理隔离网络（B 网生成二维码 → A 网手机扫码）"的固定场景下，
> 为新增 PDF / JPG / PNG / ZIP 等格式并进一步压缩现有格式的二维码数量，
> 设计一套**压缩率优先、无需通用性**的压缩方案。
> 本报告全部压缩比均为本机实测（数据与方法见附录），开源项目信息以 GitHub 为准。

## TL;DR

1. **最大的浪费不在压缩算法，而在协议层**：现行"原件传输"管线存在**两次 base64**
   （信封内一次、QR-transfer 帧内一次）+ **FEC 冗余 1.67×**，文件每 1 字节实际要发
   ≈ **2.98 字节**二维码数据。仅把两处 base64 改为二进制承载、不动压缩算法，
   二维码量即可**直降约 53%**。
2. **对新格式（pdf/jpg/png/zip）与 Office 容器，"解包/重组 → 强熵编码"两段式**
   是唯一有效的通用路线：直接对已压缩文件套 zstd/lzma 收益 ≈ 1.0×（实测无效），
   而先解包内部流（docx/xlsx 解 zip、pdf 去流壳、jpg 用 packJPG、png 转无损 WebP）
   再重压，可做到 **1.2×～4.0×**（相对原文件；UI 截图类转 WebP 可达 5.9×）。
3. **终级熵编码推荐 LZMA（LZMA2，dict 8 MiB）**：中文文本、表格 JSON、Office 解包流全面
   优于 zlib/zstd/brotli（仅 PPMd 在表格类更好，但已无维护的 Java 解码器，淘汰）；
   **XZ for Java（0BSD、纯 Java、零 NDK）** 满足手机端解码，8 MiB 字典实测不损
   压缩率，把手机侧解码内存压到 ~10 MB 量级。
4. **组合收益**（协议瘦身 + 格式预处理 + LZMA）：各格式二维码张数预计下降
   **65%～90%**（v40 二维码、535KB 照片从 545 张 → 192 张；6.8MB 图文 PDF 从
   6925 张 → 1927 张）。
5. 落地路线四步走：**P0 协议瘦身（零算法风险）→ P1 换 LZMA → P2 格式预处理扩白名单
   → P3 结构层（Excel 列式化、zstd 字典）**；全部改动以信封 `jzt=2` + 帧协议版本位
   向后兼容，旧码旧 APP 不受影响。

---

## 1. 现状分析

### 1.1 现行管线（routes.py）

```
原件传输：文件字节 ─base64→ JSON 信封{fmt=file} ─┬→ 静态：按页拆 JSON，QR 字节模式渲染
                                                 └→ 视频：分块→再base64+帧头→zfec(FEC 0.4)→QR 文本模式→H.264
精简传输：docx/xlsx 提取文本/数组 → JSON 序列化 → zlib-9（划算才启用 zip=1）→ base64 → 同上
```

### 1.2 开销分解（原件传输，实测口径）

| 环节 | 系数 | 说明 |
| --- | --- | --- |
| 信封 base64 | ×4/3 | 文件字节→`data` 字段 base64，JSON 转义基本不触发 |
| 帧再 base64 | ×4/3 | QR-transfer 帧把 chunk 再编成 ASCII；QR 字节模式每 ASCII 字符占 8 bit，只携带 6 bit 信息 |
| zfec FEC | ×5/3（≈1.667） | `FEC_RATIO=0.4`，为对抗 H.264 有损压缩与丢码 |
| 帧头/分块对齐 | ≈×1.03 | 每帧 12 字符头 + chunk 对齐损耗 |
| **合计** | **≈ ×2.98** | **1 MB 文件 ≈ 3 MB 二维码数据** |

实测例：535 KB 照片走现行原件传输 = **545 张** v40-L 二维码；6.8 MB 图文 PDF =
**6925 张**。这是"二维码量失控"的直接原因——base64² 与 FEC 的 2 倍结构性开销，
远在压缩算法可达的收益（1.1×～2×）之上。

### 1.3 已做对的压缩决策（保留）

- 精简传输对文本/数组已用 zlib-9 + `zip=1` 标记，重复表格类可达 5～10×；
- 二维码一律 `ERROR_CORRECT_L`（纠错冗余最低 ≈5.5%），视频相机模式帧头协议兼容旧端。

### 1.4 缺口

- `fmt=file` 因 base64 高熵**完全放弃压缩**，jpg/png/pdf/zip 等已压缩容器即使压缩
  也几乎无收益（实测见 §4.2），必须走"格式感知预处理"；
- 静态/视频两种输出都背着双重 base64，属于协议设计问题而非算法问题。

---

## 2. 候选方案调研（以 GitHub 为准）

### 2.1 通用熵编码（编码端 Python，解码端 Android/Web/PC）

| 方案 | 仓库（许可） | 实测/公开收益（相对 zlib-9） | 编码端 | 解码端可行性 | 评估 |
| --- | --- | --- | --- | --- | --- |
| **LZMA / XZ** | xz、[tukaani-project/xz-java](https://github.com/tukaani-project/xz-java)（0BSD） | 中文文本 +13%～+15%；表格 JSON +38%；Office 解包流最佳 | **stdlib `lzma`，零依赖** | **XZ for Java：纯 Java、0BSD、无 NDK**；注意解码内存≈字典大小，定制 `dict=8MiB` 后压缩率无损（§4.5），手机侧内存 ~10 MB 可控 | ★★★★★ 首选 |
| zstd | [facebook/zstd](https://github.com/facebook/zstd)（BSD+GPLv2 双许可）、[luben/zstd-jni](https://github.com/luben/zstd-jni)（BSD-2） | 文本与 lzma 相当（−1%～−2%）；解码最快 | `pip zstandard`（BSD-3） | zstd-jni **Maven 官方 .aar，Android 5.0+**（示例工程 ZstdAndroidExample） | ★★★★ 备选（解码速度敏感时） |
| brotli | [google/brotli](https://github.com/google/brotli)（MIT） | 短文本/中文散文略优（+3%～+6% vs lzma），表格类略逊 | `pip brotli` | 纯 Java 解码器 `org.brotli:dec` 0.1.2（2017 后停更，格式 RFC 7932 冻结无兼容风险）；且 brotli 流**无内建校验**，信封需自加 CRC | ★★★ 无决定性优势 |
| zopfli（gzip/deflate 极致） | [google/zopfli](https://github.com/google/zopfli)（Apache-2.0） | 只比 zlib 好 3%～8%，远逊 lzma | `pip zopfli` | **解码端零改动**（现有 Android `java.util.zip.Inflater` 直接兼容）；但编码 ~100× zlib 慢（定位"归档级"） | ★★ 仅作"不动协议的免费午餐"选项 |
| PPMd（ppmd-7z） | [miurahr/pyppmd](https://github.com/miurahr/pyppmd)（LGPL-2.1+） | 表格 JSON 实测最强（比 lzma 再 +16%），中文散文 +17% | `pip pyppmd` | **无维护的 Java 实现**（仅 Google Code 遗骸仓库）；XZ for Java 不支持 PPMd——移动端断链 | ★★ 淘汰（解码端一票否决） |
| paq8 / cmix / zpaq 系 | [byronknoll/cmix](https://github.com/byronknoll/cmix)（GPL-3.0）、[zpaq](https://github.com/zpaq/zpaq) 等 | 文本可再省 20%+ | 命令行 | **手机端不可行**：cmix 官方建议 32 GB 内存起、paq8 不在 GitHub；无 Java/Android 解码器 | ✗ 排除 |

### 2.2 格式感知预处理（对已压缩容器"解壳→更强模型重压"）

| 目标格式 | 方案 | 仓库（许可） | 官方/实测数字 | 解码端 | 评估 |
| --- | --- | --- | --- | --- | --- |
| docx / xlsx / zip | **解 zip 成员→串联→LZMA**（纯 Python 可实现） | 无第三方依赖（zipfile+lzma） | 实测 docx **4.0×**、xlsx **1.95×**、zip **1.37～1.63×**（相对原文件） | XZ for Java + 重打包 zip 即可还原**可正常打开的原文件**；若需哈希级字节一致，用 [microsoft/preflate-rs](https://github.com/microsoft/preflate-rs)（Apache-2.0）做 deflate 无损去壳（正差 <1% 明文体积） | ★★★★★ 核心手段 |
| jpg | **packJPG**（JPEG 熵编码无损重压缩） | [packjpg/packJPG](https://github.com/packjpg/packJPG)（LGPL-3.0）、解码器 [packjpg/unpackJPG](https://github.com/packjpg/unpackJPG)（**BSD-2**） | 官方典型 −20%；实测 2560×1707 照片 **−16.1%**（max 模式） | unpackJPG 为 C 小库，NDK 编译，许可干净；支持 progressive/CMYK | ★★★★ 推荐 |
| jpg | lepton（brotli 压熵系数） | [dropbox/lepton](https://github.com/dropbox/lepton)（Apache-2.0，**官方已归档**）→ 继任 [microsoft/lepton_jpeg_rust](https://github.com/microsoft/lepton_jpeg_rust)（Apache-2.0，**有 PyPI 包** `lepton_jpeg_python`） | README 官方：**平均 −22%**，字节还原 | 服务端 Python 直装可用；Android 需 cargo-ndk 自编译 liblepton | ★★★ 收益最高一档，集成成本高于 packJPG |
| jpg | brunsli（JXL 的 JPEG 后端） | [google/brunsli](https://github.com/google/brunsli)（BSD-3） | 官方 −22% | 维护信号不明 | ★★ 不引入（功能被 packJPG/JXL 两侧覆盖） |
| png（照片类） | 转**无损 WebP** | libwebp（BSD）；Pillow 自带 | 官方口径"无损 WebP 比 PNG 小 **26%**"；实测 4.1 MB 照片 PNG → **1.57×** | **Android 系统原生支持 WebP 解码（含无损）**；无需任何集成 | ★★★★★ 推荐（若允许"转格式传输"） |
| png（截图/UI 类） | 转无损 WebP（+LZMA 兜底） | 同上 | 实测 40 KB 截图 → **5.87×**（WebP 6.8 KB） | 同上 | ★★★★★ |
| png（需字节还原） | zopflipng / 重定过滤器 | [google/zopfli](https://github.com/google/zopfli) 附 zopflipng | 典型仅 −1%～−5%（无官方百分比） | PNG 解码器系统自带 | ★★ 收益有限 |
| pdf（文字型） | 对象/内容流去壳→LZMA | [pikepdf](https://github.com/pikepdf/pikepdf)（MPL-2.0）/ qpdf（`--recompress-flate`、可挂 zopfli） | 实测文字型 **1.37×**（该 reportlab 样本流本就未压；Word 导出类预估再省 5～20%） | **还原端只需 LZMA 解压 + 按清单重建流**，无需 PDF 解析器 | ★★★★ |
| pdf（图文型） | 去壳 + **内嵌 DCT 图走 packJPG** | pikepdf + packJPG | 实测去壳+重压 **1.49×**（未做图分治）；图分治理论可再 −10%～−16%（图占比高的文档） | 同上 | ★★★ 进阶项 |
| 全能预压缩器 | precomp（解壳矩阵工具） | [schnaader/precomp-cpp](https://github.com/schnaader/precomp-cpp)（Apache-2.0，已停更） | README：silesia.zip 用 7z-9 只能压到 99.7%，precomp 到 **69.7%**（印证"解壳+重压"路线的量级） | C++ 命令行，**无 Java/Android 解码端** | ✗ 不引入，思路已被解包重压覆盖（bs16/bs21 等同类亦无移动生态） |
| jpg/png（极限） | **JPEG XL** 无损转码 | [libjxl/libjxl](https://github.com/libjxl/libjxl)（BSD-3） | 官方可查证：JPEG 无损重压 **≈−20%**（与 packJPG 同档）；"对 PNG −30%+"仅为社区口径，**官方数字缺失** | Android 官方明确"无开箱支持"（issue 259900694）；可嵌 [awxkee/jxl-coder](https://github.com/awxkee/jxl-coder)（Apache-2.0，Android 编解码库） | ★★★ 二期观察（一套 codec 通吃 jpg+png，替代 packJPG+WebP 两套路） |

### 2.3 结构化数据层（精简传输专用）

| 手段 | 实测收益 | 说明 |
| --- | --- | --- |
| Excel 二维数组**列式序列化**（按列分组再压） | lzma 再 **−25%**、brotli 再 **−30%** | 同列类型/取值重复，LZ 距离大幅缩短；还原时转置回行 |
| zstd **预训练字典**（内置于两端 APP/服务端） | 单文档 **−16%** | 字典≈110 KB 出厂内置、不随码传输；对"批量小文档"最划算 |
| docx 正文提取（现行）+ LZMA | 相对 zlib-9 **−11%～−85%**（视重复度） | 独立散文 −11%，模板化/重复度高的长文 −40% 起（见 §4.1 注¹） |

### 2.4 二维码协议侧

- **QR 容量事实（ISO/IEC 18004，v40-L）**：Numeric 7089 / Alphanumeric 4296 /
  **Byte 2953** 字节。base64 字符集含小写字母进不了 Alphanumeric 模式，只能按字节
  逐字符存进 Byte 模式——**每 8 bit 槽只携带 6 bit 信息**。现行"信封 base64 + 帧再
  base64"的链条合计浪费 ≈ 1.78×。改为 zfec 输出直接以 `byte[]` 喂 QR Byte 模式
  （帧头也还原为 2+2 字节二进制，替代 12 字符 base64 头），等效扩容 **33%～78%**，
  手机解码端零成本。同类开源多码传输项目（如 ganlvtech/qrcode-file-transfer、
  codingmiao/qrtransfer）同样以二进制 payload 直存为主流做法。
- **FEC 分级**：静态码图（PNG 无损、逐张确认）不需要 zfec，现行静态路径本就没有；
  视频路径 `FEC_RATIO=0.4` 可按介质分级——相机轮播模式（帧重复 5 次、逐帧 PNG
  兜底）可降到 0.2（1.25×），再省 25% 码量。风险靠"接收端漏码率统计"控制。
- 附带核实：base32k 之类"更省字符"的编码只在**字符数受限**的通道（tweet/剪贴板）
  有意义；QR 的比特预算固定，其 Kanji 模式容量 ≈ Byte 模式，**对本场景无净收益**，
  不必引入。

---

## 3. 推荐设计：JZ-Codec 四段式压缩算法

> 定位：只服务"信息传输"插件的固定场景，不追求通用兼容，压缩率优先。

```
原文件 ──[S1 格式路由]── 预处理载荷 + 还原清单(manifest)
       ──[S2 结构层]──── 文本/数组列式化、二进制小信封
       ──[S3 熵编码]──── LZMA-9e（单一算法决策，全载荷统一）
       ──[S4 传输层]──── 二进制帧（去双base64）+ 分级 FEC → QR v40 字节模式
```

### S1 格式路由（决定"怎么解壳"）

| 格式 | 预处理 | 还原方式（解码端） |
| --- | --- | --- |
| docx/xlsx/pptx/zip | 按名排序解出全部成员，`[名字表+长度+字节流]` 串联 | LZMA 解压→按清单重打包 zip：**成员文件与原件一致**，zip 容器字节因重打包而变（需哈希级一致时改用 preflate 去壳路线） |
| pdf | 对象流/内容流 inflate 去壳，DCT 图像流可选 packJPG（二期） | 按 manifest 回填对象重建 |
| jpg | packJPG（max 模式） | unpackJPG（NDK C 小库） |
| png | 照片类→无损 WebP（声明转格式）；需字节还原时仅 LZMA | Android 原生解码 / PNG 重编码 |
| txt/md/csv | 直接进 S3（现行提取路径不变） | UTF-8 解码 |
| word/excel 精简载荷 | 二维数组**列式序列化**；文本 UTF-8 直通 | 转置还原 |
| 未识别/损坏 | 直通（回退现行 fmt=file 行为） | — |

预处理失败一律安全回退直通，**正确性优先于压缩率**。

### S2–S3 载荷与熵编码

- 载荷统一为紧凑**二进制信封** `jzt=2`：`{magic, fmt, ext, alg, prep, name-utf8,
  manifest, payload}`，字符串不再过 JSON+base64；
- 熵编码**只选一种：LZMA**，参数固化为自定义滤镜
  `LZMA2(dict=8MiB, preset=9e 等效档, pb=0)`——XZ for Java 的解码内存≈字典大小，
  8 MiB 字典在中端机上 ~10 MB 峰值内存即可解码，而实测压缩率与默认 64 MiB 字典
  持平甚至更优（§4.5）；XZ 容器自带 CRC32，无需额外校验层。
  理由：除中文短散文外全面第一梯队；解码端唯一"纯 Java、零 native、许可干净
  （0BSD）"的强压缩器；避免双算法维护。表格类若二期实测 PPMd 收益稳定，
  再以 `alg` 字段扩展——但 **PPMd 无维护的 Java 解码器**，扩展大概率转向
  zstd-jni 或接受 PPMd 仅限网页端解码的不对称方案。

### S4 传输层

- 帧：`[1B 协议版本][2B 帧序][2B 总帧数][payload]` 全二进制，QR 字节模式直存；
- FEC：视频常规 0.4 保持；新增"相机轮播 0.2 / 静态 0"分级；
- 旧解码端遇到非 ASCII 首帧自然失败 → 编码端由用户显式选"高效模式"，
  默认仍走旧协议，待 APP 升级覆盖后切换默认。

### 解码端硬性要求核对

| 端 | 需要新增 | 成本 |
| --- | --- | --- |
| B 网后端（Python） | 无（lzma 标准库；packJPG 可选 NDK 编译或用 lepton 二进制） | ≈0 |
| A 网 APP（Android） | XZ for Java（0BSD，aar≈150 KB）；packJPG→unpackJPG（NDK，可选）；WebP 原生已有 | 小 |
| Web 解析端 | 走后端 `/decode`，Python 侧同上 | ≈0 |

---

## 4. 实测数据

### 4.1 精简传输载荷压缩比（越大越好；zlib-9 = 现行）

| 样本 | 原始 | zlib9 现行 | zopfli | lzma-9e | zstd-22 | brotli-11 | PPMd |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 中文长文档 HANDOFF.md（89.6 KB） | — | 2.27× | 2.39× | 2.56× | 2.49× | 2.65× | **3.01×** |
| 设计文档.md（37.2 KB） | — | 2.26× | 2.34× | 2.42× | 2.35× | 2.57× | **2.78×** |
| docx 提取正文（228 KB）¹ | — | 2.54× | 2.67× | **15.59×** | 15.28× | 16.79× | 12.42× |
| Excel 数组 JSON（347.7 KB） | — | 6.78× | 7.99× | 9.39× | 8.47× | 8.87× | **12.20×** |
| Excel 数组 JSON + 列式化² | — | — | — | **12.51×** | — | 10.82×(q9) | — |

> ¹ 样本正文为重复拼接的设计文档段落，长距重复放大了 LZ 系收益，**该列偏乐观**。
> 实测口径：独立成篇的中文散文（HANDOFF.md）lzma 对 zlib 仅省 11%；表格/模板化
> 内容省 25%～40%；高重复文档可达 −85%。真实收益随文档重复度而定。
> ² 列式化对照实验用另一份序列化的同源数组（行式 335.7 KB → lzma 37.1 KB；
> 列式 329.7 KB → lzma **27.8 KB**，即 **再省 25%**；brotli q9 再省 30%），
> 故该行与上表不同基准，只用于量化"列式化"本身的增量。

### 4.2 原件传输：直接重压（对已压缩容器无效 → 必须解壳）

| 文件 | 原始 | zlib9 | lzma-9e | zstd-22 | brotli-11 | PPMd |
| --- | --- | --- | --- | --- | --- | --- |
| docx 154 KB | | 1.02× | 1.03× | 1.03× | **1.04×** | 1.00× |
| xlsx 149 KB | | 1.08× | **1.12×** | 1.09× | 1.09× | 1.10× |
| zip 253 KB | | 1.00× | 1.00× | **1.01×** | 1.00× | 0.98× |
| jpg 535 KB | | 1.00× | 1.00× | **1.01×** | 1.00× | 0.99× |
| png 照片 4.1 MB | | 1.00× | 1.00× | 1.00× | **1.00×** | — |
| png 截图 40 KB | | 1.54× | 1.57× | 1.55× | 1.54× | **1.58×** |
| pdf 文字 43 KB | | 1.36× | 1.37× | 1.37× | **1.39×** | 1.33× |
| pdf 图文 6.8 MB | | 1.17× | 1.46× | 1.46× | **1.47×** | — |

### 4.3 解壳 → 重压（相对**原文件**的最终体积，核心表）

| 文件 | 路径 | 最终大小 | 相对原文件 | 说明 |
| --- | --- | --- | --- | --- |
| docx 154 KB | 解成员→lzma-9e | **38.6 KB** | **4.00×** | 解包 1.35 MB，zip 内 deflate 换 LZMA 一换多得 |
| xlsx 149 KB | 解成员→lzma-9e | **76.3 KB** | **1.95×** | 台账含随机数，上限受熵约束 |
| zip 253 KB | 解成员→ppmd | **154.9 KB** | 1.63× | lzma 为 1.37× |
| pdf 图文 6.8 MB | 去流壳→lzma-9e | 4.58 MB | 1.49× | 直接压 1.46×；图片为 DCT 流，需 packJPG 分治再进一步 |
| pdf 文字 43 KB | 直接→lzma-9e | 31.7 KB | 1.37× | |
| jpg 535 KB | **packJPG max** | **449.1 KB** | **1.19×（−16.1%）** | 实测；lepton 官方平均 −22% |
| png 照片 4.1 MB | 无损 WebP q100 m6 | **2.62 MB** | **1.57×** | 再叠 lzma 无收益（1.00×） |
| png 截图 40 KB | 无损 WebP | **6.8 KB** | **5.87×** | UI 平色截图是转格式最大赢家 |

### 4.4 端到端二维码张数（v40-L 视频模式；现行 2.98× 开销 vs 建议方案）

| 样本 | 现行张数 | 建议（FEC 0.4） | 建议（FEC 0.2） | **降幅** |
| --- | --- | --- | --- | --- |
| docx 原件 154 KB | 159 | 24 | 18 | **89%** |
| xlsx 原件 149 KB | 152 | 44 | 33 | **78%** |
| zip 原件 253 KB | 259 | 105 | 79 | **69%** |
| jpg 照片 535 KB | 545 | 255 | 192 | **65%** |
| png 照片 4.1 MB | 4174 | 1482 | 1112 | **73%** |
| png 截图 40 KB | 42 | 5 | 4 | **90%** |
| pdf 文字 43 KB | 45 | 19 | 14 | **69%** |
| pdf 图文 6.8 MB | 6925 | 2569 | 1927 | **72%** |

（精简传输在 §4.1 基础上再乘协议瘦身收益，1MB 级中文文档 ≈ 350→220 张 → 约 30 张内。）

### 4.5 LZMA 字典大小 vs 手机端内存（选型关键参数）

XZ for Java 解码内存 ≈ 码流声明的字典大小，故不能盲用 64 MiB 默认档。实测缩小字典
对压缩率的影响：

| 样本 | dict 64 MiB（9e 默认） | dict 16 MiB | **dict 8 MiB（p6+pb0）** |
| --- | --- | --- | --- |
| docx 原件 154 KB | 149,712 | 149,708 | **149,556** |
| xlsx 原件 149 KB | 133,068 | 132,720 | **132,556** |
| pdf 图文 6.8 MB | 4,672,492 | 4,673,316 | **4,671,452** |
| 中文长文档 89.6 KB | 35,016 | 34,952 | **34,864** |

**8 MiB 字典（pb=0 调优）无损甚至略优**——本场景单载荷 ≤20 MB，超大字典无增益
反而推高手机端内存。推荐参数直接固化进协议（两端一致即可）。另测 zstd-19 与 -22
几乎无差（4,668,690 vs 4,668,742），备选档位无选择焦虑。

---

## 5. 方案对比结论与选型

| 决策点 | 选择 | 淘汰项与理由 |
| --- | --- | --- |
| 传输承载 | 二进制帧 + QR 字节模式 | 双 base64（结构性 +98% 开销）；zopfli 仅 +3%～8% 不值一提；base32k 对 QR 无净收益 |
| 熵编码 | **LZMA（LZMA2，dict 8 MiB，pb0）** | zstd：解码最快但压缩率持平、Android 走 native；brotli：无决定性优势、dec 停更且流无校验；PPMd：无维护 Java 解码器；paq/cmix：手机端不可能 |
| Office/ZIP | 解成员重压 | 直接套压缩（1.0× 无效） |
| JPG | packJPG（二期可换 JXL 统吃） | lepton（维护停滞、集成重）、WebP 有损转码（改变原件语义） |
| PNG | 无损 WebP（照片/截图） | zopflipng（仅 −5% 左右）、JXL（Android 需自带 NDK 库，二期） |
| PDF | pikepdf 去壳 + LZMA（二期图片分治） | precomp/bs 全家桶（CLI/GPL/无移动解码） |
| Excel 载荷 | 列式序列化 | 行式 JSON（现状，浪费 25%+） |
| FEC | 分级（静态 0 / 轮播 0.2 / 视频 0.4） | 一刀切 0.4 |

## 6. 风险与限制

1. **协议互操作**：QR-transfer 帧二进制化影响与「轨迹转换 / QR 视频流解码」插件、
   旧版 APP 互通——必须整段走 `jzt=2` 新协议并保留旧模式输出，禁止同一协议半改。
2. **packJPG/lepton 原件字节还原**依赖其自身可逆性（官方承诺字节一致），
   验收标准必须含"pjg→jpg 与源文件 SHA-256 相等"。注意 dropbox/lepton 已因安全问题
   官方归档，若走该路线必须采用继任者 microsoft/lepton_jpeg_rust（有 PyPI 绑定）。
3. **png→webp 属转格式传输**：还原产物扩展名变化（信封 ext 字段声明原格式，
   APP 端可选回转 PNG，系统解码器即可），需在前端明示"高压缩模式"。
4. **PDF 去壳重压对"图占比高"的扫描件收益有限**（实测 1.5×封顶），二期图片分治
   才能进一步；若业务允许"高压缩"档走有损（jpg 重压缩 quality 下限），收益另计
   （超出本次无损范围，不推荐）。
5. **手机端解码资源**：XZ for Java 解码内存≈字典大小——已通过固化 dict=8 MiB
   参数控制在 ~10 MB（§4.5），数十 MB 载荷解码 1～3 s 量级 [需真机复测]；编码端
   LZMA / brotli-11 对 7 MB 级文件 2～20 s，不影响 B 网侧。zstd-jni 作为备选
   可显著降低解码耗时，但引入 native .so。
6. 极端语料（加密 zip、含大字体子集 PDF、CMYK JPEG）预处理会跳过或回退，
   白名单路由需要逐项回归测试。

## 7. 落地路线图

| 阶段 | 内容 | 预期 | 工作量 |
| --- | --- | --- | --- |
| **P0 协议瘦身** | 二进制帧 + 信封二进制（jzt=2）、FEC 分级；**不动压缩** | 各格式 **−50%～−55%** 码量 | 后端 ~2 天、APP ~3 天 |
| **P1 熵编码升级** | 精简载荷 zlib→LZMA；APP 集成 XZ for Java | 文本再 −10%～−25%，表格载荷再 −40%（含列式化） | ~2 天 |
| **P2 格式预处理** | docx/xlsx/zip 解壳重压；png→WebP；jpg→packJPG；pdf→pikepdf；扩白名单 | 新格式全支持，原件传输再 −20%～−75% | ~4 天 + APP NDK 项 |
| **P3 进阶** | zstd 字典（批量小文档）、JXL 统一图档路线、PDF 图片分治、PPMd 试验 | 再 −10%～−20% | 按收益排期 |

> P0 + P1 合计约一周即可让**现有四种格式的二维码量再砍一半以上**，
> 且不改任何格式语义；P2 完成 pdf/jpg/png/zip 接入。

---

## 附录 A：测试环境与语料

- 环境：Windows，Python 3.14.7；zstandard 0.25、brotli 1.2、pyppmd 1.3.1
  （order=7, 64 MB）、zopfli 0.4.3（15 迭代）、pikepdf 10.13、Pillow（libwebp）、
  packJPG 2.5k（max 模式，官方 GitHub Release 二进制）。
- 语料：真实中文工程文档 17 份（.md）；python-docx/openpyxl 生成的 docx（2400 段
  正文+表格）与 xlsx（3000 行台账）；picsum 真实照片 2560×1707（jpg 535 KB 渐进、
  无损转 PNG 4.1 MB）；arXiv 论文 PDF 6.8 MB（图文）；reportlab 文字 PDF 43 KB；
  合成 UI 截图 PNG 40 KB；中文文档 zip 包 253 KB。
- 复现：`python .bench_corpus/bench.py`（结果 `.bench_corpus/bench_results.jsonl`），
  packJPG：`packJPGx64.exe photo_cat.jpg`（输出 `photo_cat.pjg`）。

## 附录 B：主要开源项目索引（许可证均经 GitHub API 核实，2026-09）

| 项目 | 用途 | 许可 |
| --- | --- | --- |
| facebook/zstd、python-zstandard、luben/zstd-jni | 熵编码备选 / Android 官方 aar | BSD+GPLv2 双 / BSD-3 / BSD-2 |
| tukaani-project/xz-java | Android 端 LZMA 解码（**选型主因**，纯 Java，仅 LZMA2 不支持 PPMd） | 0BSD |
| google/brotli（java/、Maven org.brotli:dec 0.1.2） | 熵编码 / 纯 Java 解码（停更，流无校验） | MIT |
| google/zopfli | deflate 极限（零改动兼容路线，编码 ~100× 慢） | Apache-2.0 |
| miurahr/pyppmd | PPMd 编码参考（Android 解码断链，不采用） | LGPL-2.1+ |
| packjpg/packJPG → packjpg/unpackJPG | JPEG 无损重压缩 / 轻量 C 解码器（NDK） | LGPL-3.0 / **BSD-2** |
| dropbox/lepton（已归档）→ microsoft/lepton_jpeg_rust | JPEG 无损重压缩备选（PyPI: lepton_jpeg_python） | Apache-2.0 |
| google/brunsli | JPEG 无损重压缩（−22%，Google 出品，停更迹象） | BSD-3 |
| libjxl/libjxl、awxkee/jxl-coder | 图像统一路线（二期观察；Android 无系统级支持，需嵌库） | BSD-3 / Apache-2.0 |
| pikepdf、qpdf | PDF 去壳/重组（qpdf 可挂 zopfli） | MPL-2.0 / Apache-2.0 |
| microsoft/preflate-rs | zip/deflate 无损去壳（需哈希级还原原件时） | Apache-2.0 |
| schnaader/precomp-cpp | 解壳重压思路印证（silesia −30%，无移动解码，不引入） | Apache-2.0 |
| byronknoll/cmix、zpaq | 极端压缩参考（32 GB 内存级，明确不采用） | GPL-3.0 / 公有 |
| zfec（现行） | 视频流前向纠错 | LGPL-2.1 |
