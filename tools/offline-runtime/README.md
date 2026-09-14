# runtime/ —— 离线运行组件

本目录存放**应用之外、但目标机运行环境需要**的第三方组件，随部署包一起分发，
使 JZToolsHub 可以在**完全无外网**的内网机器上完成部署与运行。

> 本目录的二进制文件**不入版本库**（`runtime/` 已 gitignore）。它们由打包机执行
> `python tools/fetch-offline-bundle.py` 下载到仓库根的 `runtime/`，
> 再由 `build-deploy.ps1` 复制进部署包；随包一起分发的还有本说明与
> `setup-offline-runtime.ps1` / `安装离线组件.bat`（这两个脚本是入库文件，
> 源在 `tools/offline-runtime/`）。

---

## 1. 组件清单

| 组件 | 版本 | 体积 | 安装方式 | 是否必需 |
| --- | --- | --- | --- | --- |
| Google Chrome（企业版 MSI，x64） | stable | ≈160 MB | `msiexec /i` 静默全机安装（**需管理员**） | 否（仅浏览器体验） |
| LibreOffice（Windows x86-64 MSI） | 26.8.0 | ≈340 MB | `msiexec /a` 管理安装 → 便携目录（**免管理员**） | 否（仅知识库旧格式高保真） |

各文件的 `sha256`、来源 URL、许可证记录在**同目录的 `manifest.json`** 中
（由获取脚本在下载时生成，打包前应与 `tools/offline-components.json` 的冻结值一致）。

## 2. 为什么需要它们

* **Chrome** —— 目标是内网机器，浏览器基线是 Chrome ≥ 72（见 `HANDOFF.md` §1.1）。
  内网无外网时无法在线装 Chrome，故随包分发离线安装包。企业版 MSI 面向受管环境
  批量部署，不自带 Google Update 自动更新（内网环境正合适）。
* **LibreOffice** —— 知识库插件的 `.doc` 归一化与 `.xls` 高保真渲染需要
  `soffice.exe`（vendor 引擎 `xhr/dhr` 调用）。缺失时**只有这两条窄路径降级**
  （`.xls` 走 xlrd 兜底、`.doc` 明确报错），上传/阅读/下载与其余插件不受影响。

## 3. 怎么装

推荐顺序：先装应用，再处理组件。`install.ps1` 在安装末尾会**自动调用**本目录的
`setup-offline-runtime.ps1`；若当时没提权，Chrome 那一步会打印指引，按提示补一次即可。

```
# 全量（Chrome + LibreOffice）
powershell -ExecutionPolicy Bypass -File runtime\setup-offline-runtime.ps1

# 只做其中一个
... -SkipChrome           # 只解包 LibreOffice
... -SkipLibreOffice      # 只装 Chrome

# 只看不做（报告将要执行的命令）
... -DryRun

# 已就绪也重做
... -Force
```

也可以直接双击 `安装离线组件.bat`（装 Chrome 时需右键「以管理员身份运行」）。

**两个组件的行为差异（重要）**

* Chrome 是**全机安装**，必须管理员权限。没有提权时脚本**不报错**，只打印
  手动安装指引 —— 因为 Chrome 不是应用运行的必要条件。
* LibreOffice 走 `msiexec /a`（管理安装）：**不写注册表、不需要管理员**，
  解出的目录可直接运行。解包目标固定为 `<本目录>\libreoffice\`。

## 4. 应用如何找到 LibreOffice（零配置）

解包完成后，应用会**自动探测**，优先级如下（见
`plugins/knowledge-base/backend/office_render.py::_apply_soffice_env`）：

1. 插件配置里的 `office.soffice_path` / `pdf.soffice_path`（`<数据根>/plugins/knowledge-base/config.json`）
2. 进程环境变量 `XHR_SOFFICE`
3. **本目录下的便携副本**：`runtime/libreoffice/program/soffice.exe`
   （不同解包方式多套一层目录时会在 `runtime/libreoffice/**/program/soffice.exe` 里有限深度搜索）

所以离线部署**不需要改任何配置**。验证方式：启动后访问
`GET /api/knowledge-base/status`，`soffice` 字段不为 `null` 即生效。

## 5. 校验

```powershell
# 重新核对已下载文件与冻结值是否一致（不下载）
python tools\fetch-offline-bundle.py --verify-only
```

`manifest.json` 里的 `sha256` 由打包机在下载时实测并记录。它能证明
「本次部署用的字节与打包时一致」，**不能证明上游来源真实性**：

* LibreOffice 官方对每个 MSI 提供 `.asc` GPG 签名，如需强验证请自行用 gpg 校验；
* Chrome 企业版 MSI 官方不发布独立哈希，若需强验证请从 Google 官方页面重新获取比对。

## 6. 许可证与再分发

| 组件 | 许可证 | 再分发说明 |
| --- | --- | --- |
| Google Chrome | 专有软件（Google Chrome 服务条款） | 企业版 MSI 面向企业受管环境批量部署；对外分发前请确认符合贵方与 Google 的约定 |
| LibreOffice | MPL-2.0（另含 LGPL-3.0 部分） | 可自由再分发，保留版权与许可证声明即可 |

## 7. 不想要这些组件怎么办

删掉本目录即可 —— 应用与插件不依赖它的存在。只是目标机需要自备浏览器，
且知识库的 `.doc` / `.xls` 高保真通道会降级。
