# Building on Windows and Linux (no Mac required)

The README's route needs a Mac: `build_ipa.sh` runs `xcodegen` and `xcodebuild`, and the last shader step calls Apple's `metal` compiler through `xcrun`.
This page describes the route that runs on Linux and on Windows (through WSL) instead. It produces the same unsigned IPA, from the same game files.

中文说明见本页末尾。

## What replaces what

| On a Mac | Here | Notes |
|---|---|---|
| `xcodegen` + `xcodebuild` | `dm/ios/tools/build_ipa_nowin.py` | `clang` compiles, `ld64.lld` links against a public iPhoneOS SDK, Python assembles `Payload/` and zips |
| `xcrun metal` / `metallib` | `dm/gfx/tools/metal_tool.py` | Three toolchains: Xcode (macOS), Apple's Windows Metal Developer Tools, or none at all |
| `sips` (app icon) | Pillow | Optional; without it the home-screen icon is blank |
| `cp -c` (APFS clone) | `os.link` / `shutil.copy2` | The scripts already fall back |
| macOS's Arial Unicode / Arial Rounded Bold | any TTF on the system | Stand-ins for the shared fonts; `STD_FONT` and the second argument of `make_ext_font.py` override them |

Everything else — NSP extraction, module rewriting, shader translation, the game profiles — runs unchanged.

## The three Metal toolchains

`dm/gfx/tools/metal_tool.py` picks one, and the choice feeds the translation chain version, so the host only ever loads libraries built by the same compiler:

| `DM_METAL` | Needs | What goes into the package |
|---|---|---|
| `macos` | Xcode | compiled `metallib` |
| `windows` | Apple's Metal Developer Tools for Windows (free Apple ID) | compiled `metallib` |
| `nometal` | nothing | the MSL source, compiled on the phone the first time each shader is used |

Detection is automatic: `xcrun` works → `macos`; `metal.exe` found → `windows`; otherwise `nometal`. Set `DM_METAL` to override, and `DM_METAL_EXE` if the Windows tools are installed somewhere unusual.

**`nometal` costs one thing:** the first launch compiles every shader on the device. Measured elsewhere on a similar workload (d2r-mobile, 7212 shaders, iPhone 17 Pro Max): about 7 minutes, once. Mario has 16268 shaders across both versions, so expect longer. Afterwards the system caches the result and later launches are normal.

The chain version differs per toolchain (`dmsh_chain.<toolchain>.lock`), so a package built on one machine never silently mixes with libraries from another.

## Setup

### Linux

```
sudo apt install clang lld zstd dotnet-sdk-10.0
python3 -m pip install lz4 capstone fonttools pillow
```

`spirv-cross` must be exactly **v1.4.357.0** (a different version translates shaders differently — d2r-mobile measured 231 shaders differing between versions):

```
git clone --branch vulkan-sdk-1.4.357.0 --depth 1 https://github.com/KhronosGroup/SPIRV-Cross
cmake -S SPIRV-Cross -B SPIRV-Cross/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DSPIRV_CROSS_ENABLE_TESTS=OFF
cmake --build SPIRV-Cross/build --target spirv-cross
export PATH="$PWD/SPIRV-Cross/build:$PATH"
```

An iPhoneOS SDK. Xcode ships one; outside a Mac, a public mirror works (for example `xybp888/iOS-SDKs`, which carries `iPhoneOS26.5.sdk`):

```
git clone --depth 1 --filter=blob:none --sparse https://github.com/xybp888/iOS-SDKs ~/ios-sdk
cd ~/ios-sdk && git sparse-checkout set iPhoneOS26.5.sdk
```

`build_ipa_nowin.py` looks in `~/ios-sdk`, `/opt/ios-sdk` and `<repo>/.ios-sdk`; `DM_IOS_SDK` points anywhere else. The SDK provides headers and `.tbd` stubs only — it is not needed at run time, and nothing from it ships in the IPA.

### Windows (through WSL)

Do the Linux setup inside WSL, then optionally install Apple's **Metal Developer Tools for Windows** (Apple Developer downloads, free account) on the Windows side. With them installed, `metal_tool.py` finds `metal.exe` and uses the `windows` toolchain; without them everything still works through `nometal`.

Crossing the WSL boundary costs about 116 ms per Windows process, so the shader compiler is driven in one batch per build (`metal_win_batch.py`) rather than once per shader. Windows-side Python is required for that batch; set `DM_WIN_PYTHON` if it is not on `PATH`.

## Build

The same command as on a Mac:

```
python3 scripts/build_ipa_from_nsp.py ~/Games/MarioWonder -k ~/keys/prod.keys
```

`scripts/build_ipa_from_nsp.py` picks the route itself: on macOS it calls `build_ipa.sh`, elsewhere `build_ipa_nowin.py`. To build by hand:

```
GAME=010015100B514000 W="$PWD/010015100B514000-work"
IOSOUT=$W/build/ios/dmsh_b DM_GAME=$GAME python3 dm/gfx/tools/dmsh_build.py $W/shader/pkg $W/build/cache/metal_shader_cache
GAME=$GAME W=$W python3 dm/ios/tools/build_ipa_nowin.py out.ipa
```

Add `DM_SHADER_FROM_SOURCE=1` when building with `DM_METAL=nometal` so the host carries the compile-on-device branch.

## What is verified, and what is not

Verified while writing this (WSL, Ubuntu 24.04, x86_64):

- The whole host — 127 sources plus libvpx — compiles with `clang-19 -target arm64-apple-ios26.0` and links with `ld64.lld` into an `MH_EXECUTE` with the embedded module sections (`__dmmainx0` / `__DMmainW` / `__dmmainx1`) at the alignment `gen_embed.py` requires. An IPA comes out.
- `dmsh_xlate` is a .NET 10 assembly; rebuilt on Linux, `--source-sha` matches the in-repo source, so the translator fingerprint does not depend on the platform.
- `nometal` puts MSL source into the library files and the host's `DM_SHADER_FROM_SOURCE` branch is compiled in only then (checked in the object file).
- `MTLMathModeRelaxed` (iOS 18+) is set through `setMathMode:`, the same semantics as `-fmetal-math-mode=relaxed`.

Not verified, because this machine has neither game files nor an Apple account:

- A full build from real NSPs, and a run on a device.
- Apple's Windows toolchain: its command line (whether it takes `-mios-version-min`, `-fmetal-math-mode`, what target flag it wants) is guessed from public documentation. If it rejects the flags, `windows` degrades to `nometal` — the build still works, with the first-launch cost.
- Whether sideloading tools accept a bundle assembled without `actool` (no `Assets.car`). The icon falls back to loose PNG files; everything else is plain files.

## Trade-offs

| | Mac | Linux / Windows |
|---|---|---|
| Shader compile | on the build machine | same with the Windows tools; otherwise first launch on the phone |
| App icon | `actool` builds `Assets.car` | loose PNGs; some sideload tools may not show it |
| Debugging | Xcode, Metal debugger | no Xcode; `log.txt` in the app's Documents, and the Mac host still runs natively if you have a Mac |
| Speed | `cp -c` clones | hard links when possible; RomFS copy is the slow part on `drvfs` |

The build itself is the same work; what changes is where the last two steps happen.

---

# 中文说明

README 那条路需要 Mac：`build_ipa.sh` 调 `xcodegen` 和 `xcodebuild`，着色器最后一步走 `xcrun` 调苹果的 `metal`。本页讲另一条路：在 Linux 和 Windows（经 WSL）上跑，用同一份游戏文件出同一个未签名 IPA。

## 替代关系

| Mac 上 | 这里 | 说明 |
|---|---|---|
| `xcodegen` + `xcodebuild` | `dm/ios/tools/build_ipa_nowin.py` | `clang` 编译，`ld64.lld` 对着公开的 iPhoneOS SDK 链接，Python 组装 `Payload/` 并打包 |
| `xcrun metal` / `metallib` | `dm/gfx/tools/metal_tool.py` | 三种工具链：Xcode、苹果的 Windows 版 Metal 工具、或者完全没有 |
| `sips`（应用图标） | Pillow | 可选；没有的话主屏图标空白 |
| `cp -c`（APFS clone） | `os.link` / `shutil.copy2` | 脚本本来就有回退 |
| macOS 的 Arial Unicode / Arial Rounded Bold | 系统里任意 TTF | 共享字体的替身，可用 `STD_FONT` 与 `make_ext_font.py` 的第二个参数指定 |

其余（NSP 解包、模块改写、着色器翻译、游戏档案）完全不变。

## 三种着色器工具链

由 `dm/gfx/tools/metal_tool.py` 判定，结论进翻译链版本，所以宿主只会加载同一个编译器出的库：

| `DM_METAL` | 需要 | 包里放什么 |
|---|---|---|
| `macos` | Xcode | 编好的 `metallib` |
| `windows` | 苹果的 Metal Developer Tools for Windows（免费 Apple ID） | 编好的 `metallib` |
| `nometal` | 什么都不用 | MSL 源码，手机上首次用到时现编 |

自动判定：`xcrun` 能用 → `macos`；找到 `metal.exe` → `windows`；否则 `nometal`。可用 `DM_METAL` 强制，工具装在别处时用 `DM_METAL_EXE` 指过去。

**`nometal` 的代价只有一项**：首次启动在手机上把全部着色器编一遍。同类工作量的实测（d2r-mobile，7212 个着色器，iPhone 17 Pro Max）约 7 分钟，一次性。马力欧两个版本合计 16268 个，会更久。之后系统缓存结果，后续启动正常。

## 安装依赖

### Linux

```
sudo apt install clang lld zstd dotnet-sdk-10.0
python3 -m pip install lz4 capstone fonttools pillow
```

`spirv-cross` 必须是 **v1.4.357.0**（版本不同翻出来的着色器不同，d2r-mobile 实测两个版本差 231 个）：

```
git clone --branch vulkan-sdk-1.4.357.0 --depth 1 https://github.com/KhronosGroup/SPIRV-Cross
cmake -S SPIRV-Cross -B SPIRV-Cross/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DSPIRV_CROSS_ENABLE_TESTS=OFF
cmake --build SPIRV-Cross/build --target spirv-cross
export PATH="$PWD/SPIRV-Cross/build:$PATH"
```

一个 iPhoneOS SDK。Xcode 自带；Mac 之外可以用公开镜像（例如 `xybp888/iOS-SDKs`，里面有 `iPhoneOS26.5.sdk`）：

```
git clone --depth 1 --filter=blob:none --sparse https://github.com/xybp888/iOS-SDKs ~/ios-sdk
cd ~/ios-sdk && git sparse-checkout set iPhoneOS26.5.sdk
```

`build_ipa_nowin.py` 会找 `~/ios-sdk`、`/opt/ios-sdk` 和 `<repo>/.ios-sdk`；`DM_IOS_SDK` 指到别处。SDK 只提供头文件和 `.tbd` 存根，运行时不参与，IPA 里不含它。

### Windows（经 WSL）

在 WSL 里做完上面的 Linux 部分，然后可选地在 Windows 侧装苹果的 **Metal Developer Tools for Windows**（Apple 开发者下载页，免费账号）。装了之后 `metal_tool.py` 会找到 `metal.exe` 走 `windows` 工具链；不装也能全程用 `nometal`。

跨 WSL 边界每起一个 Windows 进程约 116 ms，所以着色器编译器是每次构建整批驱动一次（`metal_win_batch.py`），不是每个着色器一次。批处理需要 Windows 侧的 Python，不在 `PATH` 上用 `DM_WIN_PYTHON` 指。

## 出包

命令和 Mac 上一样：

```
python3 scripts/build_ipa_from_nsp.py ~/Games/MarioWonder -k ~/keys/prod.keys
```

`scripts/build_ipa_from_nsp.py` 自己选路：macOS 上调 `build_ipa.sh`，其他平台调 `build_ipa_nowin.py`。手工出包：

```
GAME=010015100B514000 W="$PWD/010015100B514000-work"
IOSOUT=$W/build/ios/dmsh_b DM_GAME=$GAME python3 dm/gfx/tools/dmsh_build.py $W/shader/pkg $W/build/cache/metal_shader_cache
GAME=$GAME W=$W python3 dm/ios/tools/build_ipa_nowin.py out.ipa
```

用 `DM_METAL=nometal` 时加 `DM_SHADER_FROM_SOURCE=1`，让宿主带上设备端现编的分支。

## 已验证与未验证

验证过的（WSL，Ubuntu 24.04，x86_64）：

- 整个宿主（127 个源文件加 libvpx）用 `clang-19 -target arm64-apple-ios26.0` 编译、`ld64.lld` 链接成 `MH_EXECUTE`，内嵌模块段（`__dmmainx0` / `__DMmainW` / `__dmmainx1`）的对齐符合 `gen_embed.py` 的要求，能出 IPA。
- `dmsh_xlate` 是 .NET 10 程序集；在 Linux 上重编后 `--source-sha` 与仓内源码一致，翻译器指纹不随平台变。
- `nometal` 会把 MSL 源码写进库文件，宿主的 `DM_SHADER_FROM_SOURCE` 分支只在那种情况下编进去（在目标文件里核对过）。
- `MTLMathModeRelaxed`（iOS 18+）通过 `setMathMode:` 设置，语义与 `-fmetal-math-mode=relaxed` 一致。

未验证的（本机没有游戏文件，也没有苹果账号）：

- 用真实 NSP 跑完整流程，以及真机运行。
- 苹果的 Windows 工具链：它的命令行（是否接受 `-mios-version-min`、`-fmetal-math-mode`、target 参数怎么写）是按公开资料推的。若它不认这些参数，`windows` 会退化到 `nometal`——出包照样能成，代价是首次启动那一次编译。
- 不经 `actool` 组装的包能否被侧载工具接受（没有 `Assets.car`）。图标退回散装 PNG，其余都是普通文件。

## 取舍

| | Mac | Linux / Windows |
|---|---|---|
| 着色器编译 | 在构建机上 | 有 Windows 工具链时相同；否则在手机首次启动时 |
| 应用图标 | `actool` 生成 `Assets.car` | 散装 PNG，部分侧载工具可能不显示 |
| 调试 | Xcode、Metal 调试器 | 没有 Xcode；看应用 Documents 里的 `log.txt`，有 Mac 仍可原生跑 Mac 宿主 |
| 速度 | `cp -c` 克隆 | 尽量用硬链接；在 `drvfs` 上 RomFS 复制是慢的部分 |

构建工作量本身没变，变的是最后两步在哪台机器上做。
