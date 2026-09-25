# 轨迹转换（trajectory-convert）

Excel 轨迹表 → QR-transfer 二维码视频流 / 静态二维码图片。

## 功能

- **上传 Excel**：支持 .xlsx / .xls，需含「时间、经度、纬度」三列（列名由数据根 `<数据根>/plugins/trajectory-convert/config.json` 自定义，默认「开始时间、经度、纬度」）；
  - **读表不信任工作表 `<dimension>` 声明**：该声明只是"提示"，部分工具会写出与实际内容不符的值
    （如声明 `A1` 而实际有 A1:A4）。openpyxl 只读模式以它为遍历范围上界，会**只读到首格**，表现为
    「找不到表头字段」或只解析出一行。现改为清掉声明、按 `sheetData` 实际内容扫描并统一补齐行宽；
- **时间抽样**：从最早时刻起按固定间隔取最近点，过高速（>120km/h）点位自动过滤；
- **二维码视频流**：经 zfec 前向纠错 → qrcode 渲染 → opencv 写 H.264 MP4，与 QR-transfer 协议兼容；
- **静态二维码**：单张 PNG 或数据溢出时自动拆分为多张图片打包 ZIP，支持勾选部分下载；
- **后台任务**：异步转换 + 进度轮询（`POST /convert` → `GET /status/<task_id>` → `GET /download/<task_id>`）。
  - **接口清单**（前缀 `/api/trajectory-convert`）：`POST /convert`（提交转换任务）、`GET /status`（依赖自检）、`GET /status/<task_id>`（任务进度轮询）、`GET /download/<task_id>`（及 `/filename` 变体，下载产物）、`GET /image/<task_id>/<index>`（逐张静态码预览）、`POST /download-selected`（打包所选静态码）、`GET /config`（读取字段名配置）。

## 依赖与升级

| 项 | 内容 |
| --- | --- |
| 框架已打包依赖 | `flask` >=3.0（必需，随主包） |
| 依赖组件包（不随主包） | `cv2` >=4.7、`numpy` >=1.24（视频码流模式需要）、`openpyxl` >=3.1、`xlrd` >=2.0（表格读写）、`qrcode` >=7.4、`zfec` >=1.6（出码与纠错） |
| 插件自带依赖（`backend/vendor/`） | 无 |
| 外部程序组件 | `libopenh264`（可选；仓库根已随包提供 `openh264-2.5.0-win64.dll`，缺失时视频编码回退 mp4v） |
| 能否单独升级 | ✅ 可以——业务依赖走「依赖组件包」，插件不 import 其他插件（构建期 C-4/C-10/C-11 校验） |
| 升级是否需重启 | 含 `backend/**` 改动**需要**重启（后台/安装器会自动重启，约 5~10 秒）；纯前端改动免重启，Ctrl+F5 即可 |

> 声明真源是 `manifest.json` 的 `requires`（三类依赖：框架包 / 自带 vendor / 外部程序组件）；
> 出包工具构建期校验「声明 ↔ 实测 import」一致（C-10）。后台「插件管理」按它显示
> 逐依赖徽标与可运行性：缺**必需**依赖 → 标记不可运行并暂不加载，补齐后重启自动恢复；
> 缺**可选**依赖 → 照常加载但标注功能降级。规范依据：《插件设计规范》§15 U-7a / U-8。

## 配置

字段名映射在数据根 `<数据根>/plugins/trajectory-convert/config.json` 中定义（默认数据根 `<用户主目录>\.jztoolshub`，可用 `JZTOOLS_DATA_ROOT` 环境变量指定；无此文件时使用内置默认列名）。可写键名只有三个，缺省即下表默认值：

```json
{
  "time_field": "开始时间",
  "lng_field": "经度",
  "lat_field": "纬度"
}
```
任务缓存位于数据根 `plugins/trajectory-convert/.task_cache/`（属部署数据，不在仓库内），30 分钟 TTL 自动清理。