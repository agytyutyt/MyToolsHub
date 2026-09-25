# JZToolsHub 验收测试数据

> 用途：配合 `docs\guide\干净机器部署验收手册.md` 做功能验收。**所有数据均为虚构测试数据**
> （身份证号/手机号/单位人员均非真实信息）。
> 生成脚本：`tools\e2e\make-acceptance-testdata.py`（可重新生成）。本目录**随仓库提交**（约 540 KB / 548589 字节，2026-09-24 实测），
> 因此拿到仓库就有经过校验的验收数据；发布介质另见 `deploy\JZToolsHub-验收测试数据-v<日期>.zip`。

## 怎么用

1. 按验收手册装好主体 + 插件集（+ 需要的依赖组件包）。
2. 按下表逐插件走一遍：上传/粘贴本目录里的文件 → 对照「通过口径」。
3. 缺依赖组件的场景**故意不装**（如不装 openpyxl）再走一遍，应看到"功能降级 + 明确提示"，而不是报错堆栈。

## 逐插件清单

| 插件 | 测试文件 | 操作 | 通过口径 |
| --- | --- | --- | --- |
| **admin**（管理后台） | `admin/批量导入-单位.xlsx`、`批量导入-部门.xlsx`、`批量导入-人员.xlsx` | 后台 → 单位/部门/人员 → 批量导入（先导单位，再导部门，最后导人员） | 导入成功条数与文件行数一致；重复导入按所选方式处理 |
| admin | `admin/批量导入-人员-含错误行.xlsx` | 同上 | **逐行报错**（登录名含空格、部门不存在、密码过短），不整批失败；错误行位置可定位 |
| admin | 任意列表页 → 批量导出 | 导出 xlsx 并打开 | 列与模板一致；**未装 openpyxl 组件**时提示"仅剩 CSV"，不是报错 |
| **file-filter** | `file-filter/花名册.xlsx` | 上传 → 选保留字段（姓名、所属单位）→ 硬过滤 → 下载 | 输出只含这两列；行数不变；文件名带"过滤"标识 |
| file-filter | `file-filter/花名册.xls` | 同上 | 走 xlrd 路径；输出为 .xlsx（**未装 xlrd 组件**时提示该格式不可用） |
| file-filter | `file-filter/花名册.csv` | 同上 | CSV 读写正常（UTF-8-SIG / GBK 自适应） |
| file-filter | `花名册.xlsx` + 文本后处理（正则 `\d{4}(\d{4})\d{4}` → `****`） | 勾选正则脱敏 | 手机号中段被替换；表头也参与替换 |
| file-filter | 配置 API Key 后，用"大模型辅助"模式 | 表头写「联系手机」「所属单位」，保留字段写「手机号」「单位」 | 能语义匹配（缺 requests 组件时应提示"大模型辅助过滤不可用"） |
| **info-transfer** | `info-transfer/待传输-说明.txt` | 静态码模式 + **原件传输** → 出码 → 下载 → 再用"信息解析"上传该码 | 还原出的文件与原件**字节一致**、文件名一致 |
| info-transfer | `info-transfer/待传输-花名册.xlsx` | 静态码 + 精简传输 | 自动拆成多张码 → 下载 ZIP → 解析后表格内容与原件一致（不保留格式） |
| info-transfer | `qr-video-decode/预生成-说明-视频码.mp4` | 「信息解析」上传该视频 | 还原出 `待传输-说明.txt`（**需 zfec / cv2 / numpy / zxingcpp 组件**） |
| info-transfer | 同上文件 | 视频码模式现场生成 → 下载 mp4 → 播放 | 播放流畅、码面清晰；缺 cv2/numpy 时提示"视频码流模式不可用"（静态码不受影响） |
| **qr-video-decode** | `qr-video-decode/预生成-说明-视频码.mp4`、`info-transfer/预生成-说明-静态码.zip` | 上传 → 重组 | 视频与静态码都能还原出 `待传输-说明.txt`；缺 zfec 时提示不可用 |
| **trajectory-convert** | `trajectory-convert/轨迹表.xlsx` | 转换为视频码 → 下载 → 用「信息解析」解析 | 还原的表格内容与原件一致（60 行） |
| trajectory-convert | `trajectory-convert/预生成-轨迹视频.mp4` | 直接解析该视频 | 同上（免去现场出码；需 cv2/numpy/zxingcpp/zfec） |
| trajectory-convert | `trajectory-convert/轨迹表.xls` | 同上（静态码模式） | 走 xlrd 路径；缺 xlrd 时提示 |
| **trajectory-sketch** | `trajectory-sketch/轨迹表.xlsx` | 上传 → 选经纬度/编号列 → 生成轨迹图 | 出图且轨迹形态与数据一致（向东偏北）；缺 openpyxl 时提示 |
| **knowledge-base** | `knowledge-base/通知.docx`、`报表.xlsx`、`手册.pdf` | 上传 → 在线阅读 | 内容可读（标题/段落/表格）；缺 docx/pypdf 组件时对应格式提示不可用 |
| knowledge-base | `knowledge-base/旧版通知.doc`、`旧版报表.xls` | 上传 → 预览 | **需装 LibreOffice 离线组件**；未装时给出"安装离线组件"提示而非报错 |
| **character-graph** | `character-graph/人物档案.docx`、`.pdf`、`.txt` | 上传 → 抽取人物与关系 | 抽出 4 个人名与关系；缺 docx/pypdf 时提示对应格式不可用 |
| character-graph | 配置 API Key 后重跑 | 用大模型辅助抽取 | 关系更完整；缺 requests 时提示"大模型辅助抽取不可用" |
| **case-report** | `case-report/收网情况简报.txt` | 粘贴文本 → 解析 → 生成报表 → 导出 | 要素正确（案件名 1·7 专案 / 时间 / 主办大队 三大队 / 抓获人数 2 / 缴获物品 4 项）；导出 xlsx（缺 openpyxl 时仅 CSV） |
| case-report | `case-report/战果台账.xlsx` | 导入台账 | 数据入库、列表可见 |
| **shared-docs** | `shared-docs/文档正文.txt` | 新建文档 → 粘贴正文 → 保存 | 保存成功、列表可见、详情可读 |
| shared-docs | 上述文档 → 导入导出 | 导出 docx/xlsx | 可下载；缺 docx/openpyxl 时提示不可用 |
| **notice-board** | `notice-board/公告正文.txt` | 新建公告 → 保存 | 首页/公告列表可见；停用插件后不再展示 |
| **base64** | `base64/待编码文本.txt` | 粘贴文本 → 编码 → 再解码 | 解码结果与原文逐字一致（含中文与符号） |
| **json-formatter** | `json-formatter/待格式化.json` | 粘贴 → 格式化 / 压缩 / 校验 | 格式化后缩进正确；压缩后无多余空白；校验通过 |
| json-formatter | `json-formatter/待格式化-错误.json` | 粘贴 → 校验 | **报错并指出位置**（缺右括号），不是静默通过 |
| **md5-generator** | `md5-generator/待校验文本.txt` + `预期值.md` | 粘贴 → 计算 MD5 / HMAC-MD5（密钥 `jz-accept-key`） | 结果与 `预期值.md` 一致（大小写不敏感） |
| **map-marker** | `map-marker/坐标列表.csv` | 粘贴坐标 → 标点 / 生成移动轨迹 | 地图上出现 10 个点、轨迹连线正确（需高德 Key；无 Key 时提示配置） |
| **color-picker** | `color-picker/说明.md` | 取色 → 读 HEX/RGB/HSL → 复制 | 三个值互相一致；复制内容正确（交互式，无需文件） |

## 预生成产物（`--with-codes` 生成）

| 文件 | 说明 |
| --- | --- |
| `info-transfer/预生成-说明-静态码.zip`（或 `.png`） | `待传输-说明.txt` 的原件传输静态码（单张装不下则为多张 ZIP） |
| `qr-video-decode/预生成-说明-视频码.mp4` | 同一文件的视频码（每码多帧，约 5 秒/码） |
| `trajectory-convert/预生成-轨迹视频.mp4` | 轨迹表的视频码（trajectory-convert 自己的信封协议） |

预生成产物的意义：**不必先跑通出码**就能验收解码链路（也能在未装 cv2 的机器上验静态码解码）。

## 自校验（生成后已跑过，可随时重跑）

```powershell
python tools\e2e\make-acceptance-testdata.py --verify --base http://127.0.0.1:5000
```

| 校验项 | 口径 | 结果 |
| --- | --- | --- |
| 静态码 PNG → 说明文本 | 解码 + 导出，与原文件 **sha256 一致** | ✅ 通过 |
| 二维码视频 → 说明文本 | 同上（视频解码链路） | ✅ 通过 |
| 轨迹视频 | 是合法 mp4 且帧数正常 | ✅ 通过（**实际解码在 qr-video-decode 页面由浏览器完成，属人工验收项**） |

> 轨迹视频为什么不能自动校验：它用的是 trajectory-convert 自己的 QR-transfer 信封（`jzt` 与
> info-transfer 不同），且 qr-video-decode 的 `/reassemble` 只接收**浏览器已解码的分块**。
> 人工验收时在该页面上传这个 mp4，应能重组出轨迹 JSON。

## 注意

- 身份证号列在 Excel 里按**文本**存储（按数字存 18 位会丢精度，admin 导入会检测并提示）。
- 表格里的数据是虚构的，但**格式与真实业务一致**（含 18 位号码、11 位手机号），便于验证脱敏/过滤规则。
- `轨迹表.xlsx` 的列名按 trajectory-convert **默认字段**（`开始时间` / `经度` / `纬度`）：列名不一致时转换会以
  "未能在表头中找到字段"失败——这不是缺陷，改表头或在插件 `config.json` 里对齐字段名即可。
- 视频码（mp4）需要 cv2 的编码器：优先 H.264（avc1），缺 OpenH264 时自动退到 mp4v；
  两者都不可用时插件会给出明确提示（不会静默产出坏文件）。
- 文件名含中文：用于同时验证"中文名不乱码"（包内条目名、上传下载、页面显示）。

## 本次生成记录

- admin：单位 6 行 / 部门 7 行 / 人员 5 行 + 含 3 类错误行的对照文件
- file-filter：花名册 20 行 × 3 种格式（xlsx/xls/csv）
- info-transfer：说明文本（多张码体量）+ 花名册表格
- trajectory-convert/sketch：轨迹表 60 点（xlsx + xls）
- knowledge-base：通知 docx（含表格）+ 报表 xlsx + 手册 PDF（源 docx 已保留）
- character-graph：人物档案 docx / pdf（源已保留）/ txt
- case-report：收网简报（要素齐全）+ 战果台账 xlsx
- 纯前端工具：base64/md5 文本、json 正误样例、坐标列表、取色器说明（md5 附预期值）
- shared-docs / notice-board：正文文本各一份
- knowledge-base/旧版通知.doc（20 KB）
- knowledge-base/手册.pdf（71 KB）
- character-graph/人物档案.pdf（79 KB）
- knowledge-base/旧版报表.xls（xlwt 直接生成）
