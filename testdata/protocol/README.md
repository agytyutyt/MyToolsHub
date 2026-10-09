# `testdata/protocol/` · 二维码传输协议测试向量

| 属性 | 内容 |
| --- | --- |
| 用途 | 跨实现一致性断言的数据基座：同一输入下的**标准编码帧**与**期望解码结果** |
| 契约真源 | `docs/design/二维码传输协议-契约文档.md`（字节布局、flags、FEC、版本与兼容口径都以它为准） |
| 消费者 | `tools/e2e/qr-protocol-consistency.py`（宿主侧台架，驱动四处实现 + Node 侧 `worker.js`） |
| 生成方式 | `python testdata/protocol/generate.py`（**向量由实现产出，不手写**；改了实现或协议就重生成） |
| 生成时点 | 2026-10-09（`generate.py` 记录的 `constants_snapshot` 即当时的实现实况） |

## 文件

| 文件 | 内容 |
| --- | --- |
| `input.txt` | 固定输入（2952 字节，UTF-8，含 ASCII / CJK / 标点 / 全角空格）。所有向量的唯一输入源 |
| `manifest.json` | 向量元数据：生成脚本、台架、被驱动实现清单、输入哈希、二维码版本（15）与纠错等级（L）、共享常量快照 |
| `v1_headers.json` | v1 帧头三段基元的期望值（`encode_index` / `encode_fecinfo` / `encode_data`），以及 **m=256 缺陷刻画**（`defect_m256`） |
| `v1_roundtrip.json` | v1 往返向量：trajectory-convert / info-transfer 各自的全帧序列 + 期望解回哈希；map-marker 的单组（`nblocks=1`）通过用例；以及**多组回归向量**（`known_defect_multigroup`，历史缺陷②，2026-10-09 已修复） |
| `v2_envelope.bin` | v2 `JZ2` 信封帧原始字节（326 B） |
| `v2_shares.bin` | v2 `JZ2` FEC share 帧序列，`长度(4B 大端) + 帧字节` 串接（5688 B） |
| `v2_frames.json` | v2 期望解析字段（comp/mode/is_fec/seg/meta_len/body_len）、信封解码内容哈希、share 计数与 k/m、crc 负样本期望、v2 重组结果哈希 |

## 产出端与来源说明

| 向量 | 由哪个实现产出 | 说明 |
| --- | --- | --- |
| v1 帧头基元 | `plugins/info-transfer/backend/routes.py`（`encode_index/fecinfo/data`），并用 trajectory-convert 与 `plugins/map-marker/frontend/generate_qr_stream.py` 的同名函数交叉比对 | 三端逐字符相同才算契约成立（`m ≤ 255`） |
| v1 往返帧 | trajectory-convert 与 info-transfer 各自 `_chunk_data → _fec_encode → _qrformat_encode` | 只调纯函数：**不生成视频、不起 Flask、不联网**，因此可重现 |
| map-marker 单组帧 | `generate_qr_stream.prepare_data / fec_encode / make_frame_payload`（复刻 `encode_to_video` 的容量与 k/m 推导） | 与解码端同构（2026-10-09 起多组亦同构，见下方回归向量） |
| v2 帧 | info-transfer `build_envelope_v2` / `_fec_encode` / `_qrformat_encode_v2` | info-transfer 与 `worker.js` 是 v2 的两处实现，APP 端（`Jz2.kt`）未纳入自动核对 |
| 解码侧 | `plugins/qr-video-decode/backend/routes.py` 的 `reassemble` / info-transfer 的 `reassemble_qrtransfer_v2` / `worker.js` 的 `__qrvdTestHooks.parseJz2Frame` | JS 段需要 `node`；缺失时台架明确 skip 而不是当作通过 |

## 「已知缺陷」与回归向量

台架里 C3–C7 分两类：**刻画现状**（characterization，红了说明现状变了，强制回来更新契约文档）与**回归向量**（绿=修复形态被保持）。

- `v1_headers.json → defect_m256`（**已修复 · 回归**）：帧头第 3 段用 `(k << 8) | m` 表示 k/m，`m` 只有 8 位，但 `MAX_FEC_M = 256` 允许 `m = 256` → 进位吃掉 k（解码端读出 `m = 0`）；三端在 `m=256` 时输出还互不相同。2026-10-09 按拍板的**修法①**把三个编码端的**参数推导**产物收到 `m ≤ 255`（帧头字节布局、`encode_fecinfo` 基元、解码端与移动端 APP 一律未动）。本条目自此保存修复后形态：`derivation_guard` 记录契约文档 §6.1 的复现输入（trajectory-convert `FEC_RATIO=0.1`、v15/L、数据恰 230 块）修复前后取值、帧头回读与逐字节往返结果，外加三端参数推导扫描；`cases` **保留修复前史实**（低层基元直接传 `m=256` 至今仍产出被破坏的字符串，故不参与断言）。台架 C6–C7 断言其不再漂移。
- `v1_roundtrip.json → known_defect_multigroup`（**已修复 · 回归**）：`generate_qr_stream.py` 曾把数据块**连续**分进各组，而 trajectory-convert / info-transfer / qr-video-decode 一律**跨步**分组；`nblocks > 1` 时解码端长度校验通过、内容错位（静默坏）。2026-10-09 改为跨步切片（契约文档 §6.2），本条目自此保存修复后形态：多组输出可被 qr-video-decode 重组、往返逐字节一致；台架 C3–C5 断言其不再漂移。

任一端被改动时，这几条断言会变红——那是**期望行为**：它强制改动者回来更新契约文档与向量，而不是让分叉再次静默存在。

## 重生成

```bash
python testdata/protocol/generate.py      # 重写本目录全部向量文件
python tools/e2e/qr-protocol-consistency.py   # 断言一致性
```
