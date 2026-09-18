# 二维码视频流传输修复 todolist

> 背景：桌面端生成 v10 二维码视频流，APP 相机扫描未能解析。
> 根因：15fps 下每码仅显示 66ms，APP CameraScanner 500ms 节流使捕获率上限约 13%，低于 FEC 所需 60%。
> 本文件为任务中断后的重启依据，完成一项标记一项（[ ] → [x]）。

## 待办清单

- [x] **1. [高] 修复 APP CameraScanner 500ms 节流瓶颈** ✅ 2026-09-06
  文件：`android-app/InfoParse/app/src/main/java/com/jztools/infoparse/scan/CameraScanner.kt`
  做法：新增视频流模式（`streamMode`），该模式下取消 500ms 时间节流，改为「解码文本与上次相同才跳过」；静态码模式保留 500ms 节流。
  实际完成内容：
  - `CameraScanner.kt`：新增 `streamMode` 属性，流模式跳过时间节流、同文本去重；
  - `MainActivity.kt`：新增「视频流采集」开关按钮（`btnStream`），开启后帧文本（非 `{` 开头且帧头解析通过）路由到 `QrFrame.Collector`，JSON 信封仍走原静态路径；帧收集进度实时显示，收齐自动重组；FRAME_MISMATCH 自动重置吸收新流；
  - `activity_main.xml` / `strings.xml`：新增按钮与文案资源；
  - 顺手修复阻塞测试的既有问题：`ZfecCompatTest.kt` 笔误（`gmulPub`→`gmul`）、`Msg.progress` 省略号空格与 3.5 规范不一致、`EnvelopeTest` 序列化用例分片数据不合法；
  - 验证：`compileDebugKotlin` + `testDebugUnitTest` 全部通过（28/28，JDK 21 + Gradle 8.7）。

- [x] **2. [高] 验证 APP QrFrame.Collector 对重复帧的去重兼容性** ✅ 2026-09-06
  文件：`android-app/InfoParse/app/src/main/java/com/jztools/infoparse/protocol/QrFrame.kt`
  做法：确认 `offer()` 对同序号重复帧直接忽略（编码端将引入连续重复帧，重复帧会大量出现）；如已支持仅补充验证/注释。
  结论：**兼容**。`Collector.offer()`（QrFrame.kt:67）以 `shares[head.idx] = payload` 按序号覆盖存储（HashMap），`have` 取 `shares.size`，重复帧天然幂等，无需改动。
  新增单测：`EnvelopeTest.重复帧去重 同序号多次offer不影响计数与重组`——覆盖「每帧连续重复 5 次 + 循环播放乱序重复」场景，验证 `have` 不增长且重组结果正确。
  验证：`testDebugUnitTest` 全部通过（29/29）。

- [x] **3. [高] 编码端：每张二维码连续重复 N 帧渲染（fps 保持 15）** ✅ 2026-09-06
  文件：`plugins/info-transfer/backend/routes.py`
  做法：`encode_to_video()` 增加 `frame_repeat` 参数（默认 1；相机传输模式取 `CAMERA_FRAME_REPEAT=5`，即每码显示 333ms），循环内每码写入 repeat 帧后再进入下一码；fps 保持 15 不改，保证 `VideoParseHelper.kt` 与 qr-video-decode / trajectory-convert 协议兼容。`/encode` 接口新增可选参数 `camera_mode`（`1`/`true` 时启用）。
  实现与验证：
  - 常量 `CAMERA_FRAME_REPEAT = 5`（routes.py:116 附近）；
  - `/encode` 解析 `camera_mode` 表单参数 → `frame_repeat` 传入 `_run_encode` → `encode_to_video`；
  - 渲染循环：每码 `writer.write(img)` 重复 `frame_repeat` 次（每张二维码只渲染一次，复用同一图像）；
  - 实测：repeat=1 → 2 帧 / 0.13s；repeat=5 → 10 帧 / 0.67s、fps 恒 15、首帧可解码、15fps 均匀抽帧拿到全部 2 个唯一码（APP `VideoParseHelper` 抽帧逻辑兼容）；
  - 帧头协议（序号/总数/FEC 参数）与 `nframes` 语义（二维码个数）不变，qr-video-decode / trajectory-convert 互通不受影响。

- [x] **4. [中] 前端：生成端提供「相机传输模式」选项 + 说明文案** ✅ 2026-09-06
  文件：`plugins/info-transfer/frontend/index.html`
  做法：视频模式勾选项「相机传输模式（放慢速度，便于手机摄像头捕获）」，提交时附带参数；附提示：播放时调高屏幕亮度、预览放大。
  实现：
  - 二维码设置区新增勾选框（仅视频模式显示，`syncCameraModeRow()` 联动 `mode` 下拉与初始化）；
  - `startEncode()` 提交时附带 `camera_mode=1`；
  - 结果提示区分普通/相机模式：相机模式提示「视频循环播放、每码停留约 0.33 秒、APP 视频流采集、屏幕常亮调高亮度放大预览」。

- [x] **5. [中] 前端：视频预览循环播放、隐藏播放器控件** ✅ 2026-09-06
  文件：`plugins/info-transfer/frontend/index.html`
  做法：`autoplay loop muted playsinline`，移除 `controls`，CSS `pointer-events: none`；预览默认放大显示。
  实现：`#videoPreview` 移除 `controls` 改为 `autoplay loop muted playsinline`；CSS 增加 `pointer-events: none` 防误触暂停；结果渲染时主动 `play()`（muted 下免手势）；尺寸沿用按窗口高度自适应的正方形预览。

- [x] **6. [中] 前端：静态多张二维码单张预览提供左右切换功能** ✅ 2026-09-06
  文件：`plugins/info-transfer/frontend/index.html`
  做法：单张预览界面加上一张/下一张按钮（及页码显示），复用 `/image/<id>/<n>` 接口。
  实现：
  - 放大预览弹窗（zoomModal）新增「‹ 上一张 / 页码 / 下一张 ›」导航行，多张时显示；
  - `showZoomPage()` / `zoomStep()` 管理页码状态，标题与页码同步显示 `x/n`，边界禁用按钮；
  - 键盘 ← / → 翻页，Esc 关闭；`fitZoom` 计入导航行高度；`hidden` 属性与 flex 布局冲突已处理（`#zoomNav[hidden]{display:none}`）。

- [x] **7. [低] 评估：FEC 冗余 0.4→0.5 是否调整** ✅ 2026-09-06（结论：不调整）
  文件：`plugins/info-transfer/backend/routes.py`（`FEC_RATIO`）
  权衡：冗余提高到 0.5 → 所需捕获帧占比降至 50%，但帧数增多 25%、视频变长。若第 1、3 项完成后端到端测试稳定，则不改。
  结论：**维持 0.4**。数据级验证显示：相机传输模式（每码 333ms）+ 循环播放多轮补帧场景下，随机丢弃 20% 码仍稳定重组（FEC 上限可容忍缺失 40%）；再提冗余的收益递减、视频时长代价明显。

- [x] **8. [中] 端到端验证 + 文档** ✅ 2026-09-06（数据级验证通过；真机相机联测待用户执行）
  做法：桌面端生成相机传输模式视频 → APP 相机扫描解析成功（含大文档多帧场景）；回归验证 APP 解析视频文件路径不受影响；`plugins/info-transfer/README.md` 补充「相机传输模式」与已知限制说明。
  已完成：
  - **数据级端到端**：10KB 文档 → v10 相机传输模式编码（127 码，k=76/m=127，每码×5 帧）→ 全帧解码重组后信封逐字节一致、内容还原正确；
  - **丢码容错**：随机丢弃 20% 码（30/127，FEC 上限 51）→ 重组仍逐字节一致；
  - **抽帧兼容**：15fps 均匀抽帧（模拟 APP `VideoParseHelper`）在 repeat=5 视频上可取到全部唯一码；
  - **APP 回归**：`testDebugUnitTest` 29/29 通过，视频文件解析路径协议未变；
  - **文档**：`plugins/info-transfer/README.md` 补充相机传输模式说明与使用/已知限制；`android-app/InfoParse/README.md` 更新 CameraScanner 描述并新增 FR-11。
  待办（需真机）：手机 APP「视频流采集」对准桌面循环播放画面实测（需真机 + 摄像头，无法在此环境自动化）。

## 优先级顺序

1 → 2 → 3 → 5 → 4 → 8（6、7 可穿插进行）

## 第二轮优化（2026-09-06）

- [x] **9. [高] 网站端：修复视频预览偶发加速播放** ✅
  根因：流式播放时网络缓冲抖动 → 浏览器丢帧"追赶"，表现为忽快后恢复。
  文件：`plugins/info-transfer/frontend/index.html`
  实现：MP4 先整体取回为 blob 再播放（`playVideoBlob()`，失败退回网络直连）+ `ratechange` 事件速率防护（偏离 1x 立即拉回）+ 页面隐藏自动暂停/恢复（`visibilitychange`，浏览器节流渲染会导致画面停跳）+ `preload="auto"`；`resetResult` 释放 objectURL。

- [x] **10. [高] APP：启动即持续识别 + 自动分流三种码型** ✅
  文件：`android-app/InfoParse/.../MainActivity.kt`、`CameraScanner.kt`、`activity_main.xml`、`strings.xml`
  实现：
  - 移除「视频流采集」手动开关按钮，扫码主页启动即持续识别；
  - `CameraScanner` 统一为「同文本去重」（彻底移除 500ms 时间节流）：静态码面停留只回调一次，视频流码面高速切换逐帧处理；新增 `resetDedupe()`，`onResume` 调用（返回主页后重扫同一码可再次触发）；
  - 内容自动分流（`onScanned`）：`{` 开头 → JSON 信封（单张直出 / 多页进 PageCollector）；帧头解析通过 → QR-transfer 帧进 `QrFrame.Collector`（懒创建，收齐自动重组）；其余 → 无效码提示；
  - 无效码 Toast 限流（同类间隔 ≥1 秒），防止流式高速扫描下模糊乱码文本刷屏；
  - 「放弃」按钮同时重置帧收集器；扫到不一致任务帧自动重置吸收新流；
  - 验证：`compileDebugKotlin` + `testDebugUnitTest` 通过（29/29）。

## 第三轮：丢帧"追赶"根治（2026-09-06）

- [x] **11. [高] 网站端：彻底根治播放偶发加速** ✅
  排查结论：本地生成的 MP4（avc1/H.264、恒定 15fps、无 B 帧、重复帧逐帧相同）文件本身无变速问题；blob 本地播放后仍偶发加速，定位为**浏览器视频管线**（渲染线程卡顿 → 丢帧追赶）行为，文件侧无法根治。
  根治方案（相机传输模式绕开视频管线）：
  - 后端 `encode_to_video()` 增加 `frames_dir` 参数：渲染循环内把每张已验证可解码的二维码 PNG 同步落盘（`frame_0001.png` 起 0 填充，字典序 == 播放序，单次渲染无额外开销）；任务状态新增 `frame_count`；
  - 新增 `GET /api/info-transfer/frame/<task_id>/<index>` 帧接口（归属校验同 /image）；
  - `_clean_task_files()` 兼容目录删除（TTL 到期清理 `<task_id>_frames/`）；
  - 前端相机模式改用 **JS 定时轮播**：`setTimeout` 固定 333ms 步进（与后端 frame_repeat=5 一致），全帧预加载后本地显示——卡顿只会整体变慢、**绝不跳码**；页面隐藏暂停/恢复；普通模式（非相机传输）仍走 blob 视频播放；
  - 验证：PY/JS 语法通过；帧序列落盘 2/2 逐张可解码；TTL 目录清理正常。

- [x] **12. [中] 相机传输模式默认开启（移除选项）** ✅ 2026-09-06
  - 后端：`/encode` 移除 `camera_mode` 参数解析，`frame_repeat` 恒为 `CAMERA_FRAME_REPEAT=5`；帧序列 PNG 落盘与 `frame_count` 随视频任务始终产出；
  - 前端：移除「相机传输模式」勾选框与联动逻辑；视频结果提示统一为相机传输模式文案；`state.cameraMode` 字段清理；
  - 验证：PY/JS 语法通过、无残留引用；实测编码 2 码 → 视频 10 帧（15fps，0.67s）+ 2 张帧 PNG，符合默认重复 5 帧预期。
