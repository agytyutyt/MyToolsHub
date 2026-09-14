# wheels/ —— 第三方预编译 wheel 制品

本目录用于存放**无法从 PyPI 直接获取对应版本 wheel** 的依赖制品，让打包与部署
不再依赖构建机上的 C 编译器。

## 为什么需要它

`zfec`（轨迹转换 / QR 视频流解码 / 信息传输三个插件的核心依赖，提供 Reed-Solomon
前向纠错）是**版本锁定型 C 扩展**——wheel 标签形如 `cp314-cp314-win_amd64`，
每个 Python 小版本都需要维护者单独发布，而它的发布节奏明显偏慢：

| 检查项 | 结果 |
| --- | --- |
| PyPI 上 `zfec 1.6.0.0`（2024-11 发布）的 win_amd64 wheel | 只到 **cp39 / cp310 / cp311 / cp312 / cp313**（无 cp38、无 cp314） |
| PyPI 上最新的 `1.6.0.1.post0` | win_amd64 wheel **最高仍只到 cp313** |
| 在 Python 3.14 上执行 `pip download zfec --only-binary :all:` | **`ERROR: No matching distribution found for zfec`** |

即在 3.14 上 `pip install zfec` 会回退到**源码编译**，要求构建机装有
MSVC Build Tools ——这与本项目"解压即打包"的预期不符，而且失败现场是 pip 的编译
报错，不直观。把编译产物固化成 wheel 后，任何机器都能直接安装。

## 本目录内容

| 文件 | 说明 |
| --- | --- |
| `zfec-1.6.0.0-cp314-cp314-win_amd64.whl` | zfec 1.6.0.0，CPython 3.14 / Windows x64 |

## 怎么用

```bash
# 方式一：安装依赖时让 pip 先看本目录
python -m pip install --find-links wheels -r plugins/trajectory-convert/backend/requirements.txt

# 方式二：显式装这一个包（其余依赖照常从 PyPI 取）
python -m pip install --find-links wheels zfec
```

> `--find-links` 只在**名称/版本匹配**时才使用本目录的 wheel；其它包仍走正常索引。
> 若把仓库拷到内网离线机器，配合 `--no-index` 可完全离线安装：
> `pip install --no-index --find-links wheels zfec`

## 制品来源与许可

- **来源**：`zfec 1.6.0.0` 官方 sdist（`zfec-1.6.0.0.tar.gz`，PyPI）在 Windows x64 /
  CPython 3.14 上本地编译（MSVC 14.51 + Windows SDK 10.0.26100），编译产物用
  `tools/build-zfec-wheel.py` 重新打包为标准 wheel。
- **验证**：`tools/build-zfec-wheel.py` 在生成后会做两步校验——① 在干净目录里
  `pip install --no-index --target` 实际安装；② 跑一次 k=8/m=12 的纠错往返
  （故意丢弃 4 块后仍能完整还原，还原内容 sha256 与原文一致）。
- **许可证**：zfec 采用 **GPL-2+**（元数据 `License: GPL-2+`，另有 Tahoe-LAFS 的
  transitory patent policy）。许可证原文已随 wheel 一并打包在
  `zfec-1.6.0.0.dist-info/licenses/` 下（`COPYING.GPL`、`COPYING.TGPPL.rst`）。
  **注意**：把二进制 wheel 放进仓库属于"再分发"，请确认你们的合规口径；
  若不便于再分发二进制，可删除本目录，改为在构建机上按下面的流程现场编译。

## 怎么重建

**从官方 sdist 重新编译（推荐，来源最干净）**

```bash
# 在装了 MSVC Build Tools 的构建机上，于"已进入 vcvars 环境"的命令行里执行：
python -m pip wheel zfec==1.6.0.0 --no-deps -w wheels
```

> 若在普通 PowerShell 里手工拼 `INCLUDE`/`LIB` 环境变量，可能遇到
> `error: [WinError 2] 系统找不到指定的文件` —— 这是 setuptools 在
> `DISTUTILS_USE_SDK` 模式下会把子进程的 `PATH` 剔除，导致裸名 `cl.exe` 找不到。
> **用 `vcvarsall.bat` / `VsDevCmd.bat` 建好的标准环境即可绕开**，不要手工拼环境。
> 本机（当前开发机）的 PowerShell 环境禁止调用 `cmd.exe` 与 `reg.exe`，
> 所以当时改用了下面的重打方案。

**重打已安装的 zfec（无需编译器）**

```bash
# 前提：当前解释器里已经装好可用的 zfec（任何方式装的都行）
python tools/build-zfec-wheel.py            # 重建 + 自动安装校验 + 纠错往返校验
python tools/build-zfec-wheel.py --no-verify
```

## 升级 Python 时的检查项

升级 Python 小版本（例如 3.14 → 3.15）前，**必须先确认**：

1. PyPI 上是否已出现对应版本的 `zfec` wheel（`pip download zfec --only-binary :all:` 能否成功）；
2. 若没有，用本脚本在有编译器的机器上重打一份，放进本目录并更新 `wheels/README.md` 的清单表；
3. `numpy` / `pillow` 同属版本锁定型（`cpXX-cpXX`），一并确认；
   `opencv-python`（`cp37-abi3`）与 `cryptography`（`cp311-abi3`）是稳定 ABI，无需担心。

详细评估与实测证据见 `docs/Python版本选型评估.md` §5。
