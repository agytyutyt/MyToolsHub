# InfoParse（安卓 APP）打包手册（速查）

> **发版只读这一页。** 适用：`android-app\InfoParse`（扫码/解析 APP，离线分发到真机）。
> 桌面端主包 / 插件包发版看根目录 `docs\打包部署手册.md`；APP 功能与协议规格看根目录 `移动端APP.md`。
> 最后核对：2026-09-17（v2.0.0，AGP 8.5.2 / Gradle 8.7 / JDK 21；full 35.1 MB，lite 19.0 MB；
> 包结构与进一步瘦身的量化分析见 §3.1）。

---

## 0. 一页流程

```powershell
cd D:\JZToolsHub\android-app\InfoParse
$env:JAVA_HOME = "C:\Users\yfjz\.jdks\jbr-21.0.11"   # java 不在 PATH，命令行构建必设；Android Studio 内构建不用
.\gradlew.bat assembleRelease                          # 一次出 full + lite 两个 release 包（约 10 min，增量秒级）
```

| 变体 | 产物（自动带版本号命名） | 体积 | 用途 |
| --- | --- | --- | --- |
| full | `app\build\outputs\apk\full\release\InfoParse-2.0.0-release.apk` | 35.1 MB | 兼容包：含 x86 系 ABI，**模拟器可装** |
| lite | `app\build\outputs\apk\lite\release\InfoParse-2.0.0-lite-release.apk` | 19.0 MB | **真机分发主推**：仅 arm64-v8a + 中英文资源 |

只重出一个变体：`.\gradlew.bat assembleLiteRelease`（full 同理 `assembleFullRelease`）。

**发版前校验（必跑）**：

```powershell
$env:JAVA_HOME = "C:\Users\yfjz\.jdks\jbr-21.0.11"; $bt = "D:\Android\Sdk\build-tools\34.0.0"
& "$bt\apksigner.bat" verify --print-certs <apk>                  # 证书应为 CN=InfoParse（release.keystore）
& "$bt\aapt.exe" dump badging <apk> | Select-String "versionName" # 核对 versionName / versionCode
```

## 1. 环境

- **JDK**：`C:\Users\yfjz\.jdks\jbr-21.0.11`（Java 21，与 Android Studio 用的同款；命令行构建必须指 `JAVA_HOME`）。
- **Gradle**：8.7，**wrapper 已入库**（`gradlew.bat` + `gradle\wrapper\`，jar 必须随源码走）。缓存与发行版在
  `GRADLE_USER_HOME=D:\GradleHome`（机器环境变量已设）。升级版本用 `.\gradlew.bat wrapper --gradle-version <x>`，别手改 properties。
- **SDK**：`D:\Android\Sdk`（`local.properties` 已指向，该文件不入库，换机器要重建）。
- 版本套件：AGP 8.5.2 / Kotlin 1.9.24 / compileSdk 34 / minSdk 29 / targetSdk 34。

## 2. 版本号规则（`app\build.gradle.kts` → defaultConfig）

- `versionName` 三段式 `X.Y.Z`；**`versionCode` 必须比上一版大**，它是覆盖安装/升级的判定依据。
- full 与 lite **同 applicationId、同 versionCode、同签名**：可互相覆盖安装，用户可自由切换；发版只改一处，两个变体一起涨。
- 改完版本号产物文件名自动带新版本，**不需要手动改名**。

## 3. lite 瘦身口径（35.1 → 19.0 MB，-46%）

- **手段**（`productFlavors`，别动到 full）：lite 只保留 `arm64-v8a` + 资源裁剪到 zh / zh-rCN / en。
- **原理**：体积大头是 ML Kit bundled 条码模型与 native 库 ×4 个 ABI（约 20 MB）。x86/x86_64 只为模拟器存在，
  armeabi-v7a 在 minSdk 29（Android 10+）真机上近乎绝迹，砍掉 3 个 ABI 是唯一有效大头。
- **刻意不开 R8**（`minifyEnabled = false`）：dex 12.2 MB 里 ~95% 是库代码（Kotlin 标准库 20%、ML Kit+gms
  管道 17%、androidx 28%、material 7%），R8 预计 12.2 → 6~8 MB（APK ≈ 13~14 MB）。利好：协议层用的是 Gson
  **树模型**（`JsonParser`/`JsonObject`，无反射），Gson/自有协议类的 keep 规则基本不需要；但 CameraX/ML Kit
  内部有反射，要开必须「补 keep 规则 + 真机回归扫码/组包/导出全链路」。
- lite 装不上 x86 模拟器**属预期**，模拟器验证用 full 变体。

### 3.1 进一步压缩空间（2026-09-17 包结构分析，评估过、暂不实施）

lite 包构成：dex 12.17 MB（64%，**stored 未压缩**）+ libbarhopper_v3.so 4.95 MB（26%，stored）+
assets 0.88 MB（3 个 tflite 模型）+ resources.arsc 0.54 MB（targetSdk 30+ 强制 stored）+ res 0.33 MB。

| 路径 | 传输体积 | 代价 / 风险 | 结论 |
| --- | --- | --- | --- |
| R8 + shrinkResources（§3 上条） | → ≈13~14 MB（-25~30%） | keep 规则 + 真机回归；安装体积同步变小 | **首选**，待有真机回归条件再开 |
| dex 改压缩存储：`packaging { dex { useLegacyPackaging = true } }`（AGP 8.5.2 DSL 已确认支持） | → ≈11.5 MB（-40%，deflate 实测 dex 12.17→约 4.5 MB） | 一行配置零代码风险；但安装后 +约 5 MB（ART 的 vdex 要重存一份 dex）、失去 mmap 直读、冷启动略慢 | 传输体积优先时的快速选项 |
| 两者叠加 | ≈8~9 MB | 安装占用与启动性能两头吃亏 | 只在追求极致传输体积时用 |

**已评估、否决**（别再试）：
- `jniLibs` 改压缩（legacy packaging）：libbarhopper deflate 实测 4.95→2.09 MB，传输只省 2.9 MB，
  安装后却要解压出 5 MB 的 .so，净亏。
- 换 ZXing 替代 ML Kit：省 6 MB 级，但识别能力断崖式下降——核心场景是 QR 视频流传输（小码、多码、视频帧），
  不可接受。
- res/ 已裁到 0.33 MB、resources.arsc/tflite 模型是刚需，均无油水。

## 4. 签名

- `release.keystore` 在项目根，store/key 密码与别名写在 `app\build.gradle.kts` 的 `signingConfigs`
  （内部离线分发约定：密钥随仓库管理）。
- **keystore 丢失可重生成，但新签名包无法覆盖安装旧包**（要卸载重装、丢本地历史数据），务必保留原文件。

## 5. 坑清单（出包前扫一遍）

| # | 坑 | 对策 |
| --- | --- | --- |
| 1 | `gradlew` 报 JAVA_HOME 未设 / 找不到 java | 本机 java 不在 PATH，先 `$env:JAVA_HOME = "C:\Users\yfjz\.jdks\jbr-21.0.11"`（会话级即可） |
| 2 | 把 ML Kit 换成 `play-services-mlkit-barcode-scanning` | **禁止**：那是 unbundled 版依赖 GMS，华为/荣耀等无 GMS 手机扫码直接废；必须用 `com.google.mlkit:barcode-scanning`（bundled） |
| 3 | 想删 `org.tukaani:xz` 省体积 | **禁止**：桌面端 v2 试压可选 xz，两端算法集必须对齐（见 `移动端APP.md`） |
| 4 | 找不到 APK：`apk\release\` 下没有产物 | flavor 化后产物在 `apk\full|lite\release\`；`apk\release\` 是历史目录，可删 |
| 5 | 安装报「已存在同版本或更高版本」 | versionCode 没递增，改 `defaultConfig.versionCode` 再打 |
| 6 | 首次跑 gradlew 特别慢 | 正常：首次要解压 Gradle 发行版 + 拉依赖，之后增量秒级 |
| 7 | 手动改了产物文件名去分发 | 没必要：命名已在构建里自动带版本号，改名只会与 `output-metadata.json` 对不上 |

## 6. 深读指针

| 想了解 | 去哪 |
| --- | --- |
| APP 协议、错误码、测试清单 | 根目录 `移动端APP.md` |
| 桌面端主包 / 插件包发版 | `docs\打包部署手册.md` |
| ML Kit bundled/unbundled 区别 | `移动端APP.md` 末尾「ML Kit」段 |
