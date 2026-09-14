# LibreOffice 核心组件「一键安装」可行性评估

> 结论先行：**可行，且已实现。** 本文件评估的三个硬要求 —— 装完项目能继续用、不影响目标机的
> Microsoft Office / WPS 与默认应用、对目标机使用者不可见 —— **全部满足**，并附实测证据。
> 相关实现：`tools/offline-runtime/install-libreoffice-core.ps1`、
> `tools/build-offline-component.ps1`。打包侧见 `docs/离线部署包说明.md`。

---

## 1. 结论：需求逐条对照

| 要求 | 满足 | 依据（详见 §2、§3） |
| --- | --- | --- |
| 项目安装后能继续调用 `.doc` / `.xls` 高保真预览 | ✅ | 实测真 `.doc→.docx`、`.xls→.xlsx` 经**应用自己的引擎**转换成功，内容无损（§3.4） |
| 目标机仍可正常使用 Microsoft Office / WPS | ✅ | 不注册任何文件关联、不写 `HKCR`，无 COM/外壳争用（§2.3） |
| 不影响目标机默认应用 | ✅ | 从不注册为 `.doc/.xls` 的处理程序，文件图标与「打开方式」不变（§2.3） |
| 不显示 LibreOffice 图标 / 对使用者不可见 | ✅ | 无开始菜单/桌面快捷方式、无「程序和功能」条目、无托盘；headless 无窗口（§2.2、§2.5） |
| 一键安装 | ✅ | 解压组件包后双击「安装LibreOffice核心组件.bat」，**免管理员**（§4） |

---

## 2. 为什么它对目标机是「隐身」的（技术依据）

### 2.1 只做「解压」，不做「安装」

本方案不执行任何安装动作：核心包已经是官方 MSI 经 `msiexec /a`（管理安装）解包后裁剪的**便携目录**，
一键安装器只是把它 `Expand-Archive` 到程序目录下。因此——

- 不写注册表（未调用 `msiexec /i`，未写 `New-ItemProperty`/`reg add`）；
- 不注册 COM 组件、不注册 shell 扩展；
- 不产生「程序和功能」条目（**管理安装不会注册产品**，这也是为什么目标机在控制面板里看不到它）。

> 静态核对：`install-libreoffice-core.ps1` 全文只有一处出现 `HKEY_CLASSES_ROOT`，
> 且是注释里说明「不写它」；全文无 `New-ItemProperty` / `New-Shortcut` / `msiexec`。

### 2.2 没有图标、没有入口

- 不创建开始菜单 / 桌面快捷方式（安装器全文无 `CreateShortcut` / `WScript.Shell`）；
- 不写 `Run`/启动项，不常驻托盘；
- 安装位置是 `<程序目录>\runtime\libreoffice\`，默认 `%LOCALAPPDATA%\JZToolsHub\` 下——
  普通使用者平时根本不会浏览到；可选 `-Hide` 再加隐藏属性。

### 2.3 不改默认应用、不影响 MS Office / WPS

Windows 的「默认应用」与文件类型图标由 `HKCR\<扩展名>` / `UserChoice` 决定。
本方案**完全不触碰**这些键，因此：

- `.doc` / `.xls` / `.docx` 仍然由目标机原来的程序（MS Office 或 WPS）打开，图标不变；
- 「打开方式」列表里不会多出 LibreOffice；
- MS Office / WPS 与本组件之间没有共享的注册表项或 COM 注册，**互不干扰**。

### 2.4 不干扰目标机已有的 LibreOffice（若有）

应用调用 soffice 时固定带 `--headless --norestore --nolockcheck` 与
**唯一临时 profile**：`-env:UserInstallation=file:///<tempfile.mkdtemp()>`。

- 不读写目标机任何既有 LibreOffice 用户配置（那份配置在 `%APPDATA%\LibreOffice`，我们从不碰）；
- 目标机若另装了完整版 LibreOffice，两份各自独立运行，互不覆盖；
- 唯一临时 profile 同时是**并发安全**的前提（vendor 铁律：复用 profile 时第二个进程会静默失败）。

### 2.5 无窗口、无提示

进程以 `CREATE_NO_WINDOW` / `UseShellExecute=false` + `--headless` 启动，
不弹窗、不出现「首次运行向导」、不产生"安装无法完成"之类的对话框。

---

## 3. 实测证据（2026-09-14，LibreOffice 26.8.0 裁剪核心包）

### 3.1 一键安装器（在模拟目标机上真跑）

| 项 | 结果 |
| --- | --- |
| 退出码 | **0** |
| 解压用时 | **12.7 秒**（164.5 MB zip → 557.9 MB / 2824 文件） |
| 产物布局 | `<应用目录>\runtime\libreoffice\program\soffice.exe` ✅；`presets\` 存在 ✅ |
| 自检 | 全新临时 profile 真实转换一次：**通过**（输出文件非空） |
| 安装标记 | 写入 `runtime\libreoffice-core.installed.json` ✅ |
| 卸载 | 退出码 0，`runtime\libreoffice\` 与标记均删除，应用目录不受影响 ✅ |

### 3.2 应用侧「零配置」命中

把 `office_render._program_dir()` 指向装了组件的模拟目录后：

- `_bundled_soffice()` → 直接命中 `...\runtime\libreoffice\program\soffice.exe` ✅
- `soffice_available()` → **True** ✅

也就是说**不需要改配置**（`office.soffice_path`）、**不需要设环境变量**（`XHR_SOFFICE`）。

### 3.3 不需要重启服务

`office_render._apply_soffice_env()` 在**每次渲染调用时**重新探测（无缓存），
且探测顺序为「配置 → 环境变量 → 随目录探查」。因此**运行中装好组件，后续预览即刻生效**，
无需重启工具箱（配置显式指定过错误路径的情况除外）。

### 3.4 真实功能链路（真 `.doc` / 真 `.xls`，不是模拟）

用裁剪核心包解出的 soffice 先造出真实样本，再喂给应用自己的引擎：

| 链路 | 结果 |
| --- | --- |
| `dhr.normalize_doc_to_docx(.doc 字节)` | ✅ 产出 docx **8169 字节**（`PK` 头，合法 zip） |
| `xhr.normalize.convert_to_xlsx(.xls → .xlsx)` | ✅ 产出 xlsx **5933 字节** |
| 转换结果内容校验（openpyxl 回读） | ✅ `A2="测试"`、`B2=42`，**内容无损** |
| 夹具 `.xls` 的格式确认 | ✅ OLE2 复合文档头（`D0 CF 11 E0 A1 B1 1A E1`），是真 BIFF 文件 |

> 顺带证明：裁剪后的核心包**保留了 MS-97 导入/导出过滤器**（soffice 能导出 `.xls`），
> 而不只是 CSV/ODF 通路。
>
> 注：早前一轮测试里 `.xls` 造不出来是**测试脚本自身**的缺陷（两次转换复用同一个 profile，
> 触发 LibreOffice 单实例限制），改为每次唯一 profile 后即通过。应用本身全程使用唯一 profile，
> 不存在该问题。

### 3.5 组件包与主包体积

| 产物 | 体积 |
| --- | --- |
| `JZToolsHub-离线组件-LibreOffice核心-26.8.0.zip` | **162.8 MB**（一键安装包） |
| 主程序包（组件已移出，实测） | **121.9 MB** / 解压 260.5 MB / 2888 文件 |
| 主程序包（组件随包，v1.8 对照） | 437.5 MB / 解压 584.6 MB / 2891 文件 |

---

## 4. 方案设计

### 4.1 安装位置与定位

安装器按以下优先级定位应用目录，保证与真实安装一致：

1. 显式参数 `-InstallDir`
2. 注册表 `HKCU:\...\Uninstall\JZToolsHub` 的 `InstallLocation`（`install.ps1` 安装时写入）
3. 默认 `%LOCALAPPDATA%\JZToolsHub`

解压目标是 `<应用目录>\runtime\`（核心包内根目录本身就是 `libreoffice\`），
最终形成 `runtime\libreoffice\program\soffice.exe` —— 正是应用探测链的第三档。

### 4.2 免管理员

全程不需要提权：`%LOCALAPPDATA%` 是当前用户可写目录，解压 + 起一次 headless 进程都不触碰系统区。
（对比：Chrome 是全机 MSI，必须管理员；所以 Chrome 组件包仍要求「以管理员身份运行」。）

### 4.3 幂等、自检与回滚

- **幂等**：已存在 `soffice.exe` 时默认只做校验，不重复解压（`-Force` 才覆盖）；
- **自检**：解压后立刻用**全新 profile** 真跑一次转换。**不能只看退出码**，必须确认输出文件存在
  （vendor 铁律：LibreOffice 可能返回 0 却不产出文件）；
- **回滚**：解压或自检失败时删除半成品目录，避免留下"看起来能用"的不完整树被应用误判。

### 4.4 卸载

- 双击「卸载LibreOffice核心组件.bat」（带二次确认），或 `-Uninstall`；
- 等价于删除 `runtime\libreoffice\`，**不留注册表与快捷方式残余**（因为当初就没写）；
- 卸载后工具箱本体照常运行，仅 `.doc`/`.xls` 高保真通道降级为简化渲染。

### 4.5 空间

解压后约 558 MB；组件包内约 163 MB。安装成功后**可删掉 `libreoffice-core.zip` 回收约 164 MB**
（默认保留以便重装；加 `-DeleteZip` 会自动删除）。

---

## 5. 与「完整版 LibreOffice 安装」的对比

| 维度 | 本方案（裁剪核心包 + 一键解压） | 完整版 LibreOffice 正常安装 |
| --- | --- | --- |
| 目标机可见性 | **不可见**（无快捷方式、无控制面板条目、无图标） | 开始菜单/桌面图标、控制面板条目 |
| 默认应用 | **完全不变** | 安装时可能请求接管 `.doc/.xls/.odt` 等 |
| 与 MS Office/WPS 共存 | 无冲突（不注册关联） | 通常也无冲突，但关联设置可能被改动 |
| 管理员权限 | **不需要** | 通常需要 |
| 占用 | 解包后 558 MB（可按需再删 164 MB payload） | 约 1.2~1.5 GB |
| 能力范围 | 仅 `.doc→.docx` / `.xls→.xlsx`（正是应用所需） | 全套（Writer/Calc/Impress/Base/Math） |
| 可卸载性 | 删目录即可 | 走卸载程序 |

> 取舍很明确：**牺牲通用性换隐身与轻量**。若目标机使用者还想用 LibreOffice 办公，
> 应单独正常安装完整版，本组件与之并不冲突。

---

## 6. 风险与边界（部署前请过一遍）

1. **多用户**：组件装在当前用户的 `%LOCALAPPDATA%`；多用户共用一台机器时，**每个用户需各装一次**
   （或把应用目录指向公共路径后统一安装）。
2. **首次转换较慢**：唯一临时 profile 的首次启动约 30~60 秒（复用 profile 可降到 15~18 秒，
   但会破坏并发安全）。当前设计选择安全优先；预览页有缓存，重复预览不再付这个代价。
3. **字体依赖**：中文 PDF/渲染依赖 Windows 自带字体（宋体/微软雅黑/黑体）。若目标机系统被精简掉
   这些字体，可能影响渲染效果（与 LibreOffice 是否注册无关）。
4. **杀毒软件**：个别安全软件会拦截 headless 进程或大体积解压。安装器自检失败时会明确报错，
   此时可加白名单后 `-Force` 重装（不会留下半成品）。
5. **目标机已装完整版 LibreOffice**：功能不冲突，但会**多占约 558 MB**；可选择不加装本组件，
   改用插件配置 `office.soffice_path` 指向目标机已有的 `soffice.exe`。
6. **组件被误删/被清理**：`office_render` 用 `except Exception` 兜住导入与调用失败，
   表现为**静默降级**（预览变简化渲染），只在 `/api/knowledge-base/status` 报
   `office_render.available=false` / `soffice` 为空。排查时先看这个接口。
7. **`-Hide` 的取舍**：加隐藏属性可进一步降低可见性，但少数备份/同步工具会跳过隐藏目录，
   建议仅在明确需要时使用。

---

## 7. 对打包与分发的影响

主包已改为**默认不含离线组件**（`build-deploy.ps1` 默认瘦身），组件走独立包：

| 分发物 | 体积 | 目标机操作 |
| --- | --- | --- |
| `JZToolsHub-v<版本>.zip`（主包） | **≈122 MB** | 双击「一键安装.bat」 |
| `JZToolsHub-离线组件-LibreOffice核心-<版本>.zip` | **≈163 MB** | 解压后双击「安装LibreOffice核心组件.bat」（**免管理员**） |
| `JZToolsHub-离线组件-Chrome-<版本>.zip`（可选） | ≈160 MB | 解压后**以管理员身份**运行「安装Chrome浏览器.bat」 |

- **浏览器**：主包不再带 Chrome。目标机有 Chrome / Edge 即可（应用是 Web UI，浏览器基线 Chrome ≥ 72）。
- **旧式「组件随包」胖包**仍可出：`build-deploy.ps1 -WithOfflineRuntime`
  （`-SkipOfflineRuntime` 已等价于默认行为，仅为兼容旧命令行保留）。
- 组件包构建：`powershell -ExecutionPolicy Bypass -File tools\build-offline-component.ps1`
  （`-Component Chrome|All` 可另出 Chrome 包）。

### 7.1 主包瘦身带来的一个行为变化

`install.ps1` 判断「是否处理离线组件」的依据从「目标目录有没有 setup 脚本」
改为「**源包**里有没有 `runtime\manifest.json`」。原因：目标目录可能残留上一版胖包的 `runtime\`，
若按目标目录判断，旧脚本会在每次更新时被误触发。瘦包安装时会打印一段指引，告诉部署者按需另装组件。

---

## 8. 后续可选优化

1. **profile 复用**：在数据根下维护一个受锁保护的固定 profile，可把首次转换从 30~60 秒降到 15~18 秒
   （需解决并发安全，收益明显时可做）。
2. **按需装载**：核心包进一步细分为「Writer 子集 / Calc 子集」，只装用到的那一半。
3. **组件自注册路径**：安装器把 `soffice` 路径写进数据根配置，
   支持把组件装到任意位置（当前依赖固定目录实现零配置）。
4. **Chrome 组件免管理员化**：目前 Chrome 仍是全机 MSI；若确需免提权，可考虑
   Chrome 便携版（但企业环境合规性需另行评估）。
