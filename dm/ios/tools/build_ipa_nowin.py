#!/usr/bin/env python3
# 不用 Xcode 出未签名 IPA：clang + ld64.lld 直接编出 arm64 iOS 可执行文件，组装 Payload 并打成 ipa。
# 与 build_ipa.sh 的分工：那边走 xcodegen + xcodebuild（需要 macOS）；这边走公开的 iPhoneOS SDK + LLVM 的 Mach-O 链接器，
# 在 Linux / Windows 的 WSL 上都能跑。两者共用同一套源文件清单（来自 dm/host/Makefile 的展开）与同一份 Gen/ 产物。
# 产物结构对齐 build_ipa.sh：DmIOS.app/{DmIOS, dm/, msl/b-<链版本>/, romfs/, aoc/, font/}。
# 用法：build_ipa_nowin.py [输出.ipa]；环境：GAME（必需）、W、PATCHED、ROMFS、MSL_IOS、AOC、DM_IOS_SDK（SDK 路径）、
#   DM_CC / DM_LD（编译器与链接器，缺省 clang / ld64.lld）、DM_DISPLAY_NAME、DM_BUNDLE_ID、DM_ICON、DM_ENV_FILE、DM_SHADER_FROM_SOURCE
# Without Xcode: build the unsigned IPA with clang + ld64.lld against a public iPhoneOS SDK, then assemble Payload and zip it.
# Where build_ipa.sh needs macOS (xcodegen + xcodebuild), this runs on Linux and on Windows through WSL. Both share the source list
# (expanded from dm/host/Makefile) and the same Gen/ outputs. The bundle layout matches build_ipa.sh.
import json, os, plistlib, shutil, subprocess, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
IOS = os.path.dirname(HERE); DM = os.path.dirname(IOS); REPO = os.path.dirname(DM)


def say(s): print('[%s] %s' % (__import__('time').strftime('%H:%M:%S'), s), flush=True)
def die(s): sys.exit('[x] ' + s)


def sh(cmd, what, log=None, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if log: open(log, 'w').write(r.stdout + r.stderr)
    if r.returncode: die('%s 失败（退出码 %d）：%s' % (what, r.returncode, (r.stdout + r.stderr).strip()[-1500:]))
    return r.stdout


def find_sdk():
    """iPhoneOS SDK：DM_IOS_SDK 优先，否则在常见位置找。"""
    env = os.environ.get('DM_IOS_SDK')
    if env:
        if not os.path.isdir(env): die('DM_IOS_SDK 指的目录不存在：%s' % env)
        return os.path.abspath(env)
    for base in (os.path.expanduser('~/ios-sdk'), '/opt/ios-sdk', os.path.join(REPO, '.ios-sdk')):
        if not os.path.isdir(base): continue
        cands = sorted((d for d in os.listdir(base) if d.startswith('iPhoneOS') and d.endswith('.sdk')),
                       key=lambda d: [int(x) for x in d[len('iPhoneOS'):-len('.sdk')].split('.') if x.isdigit()], reverse=True)
        if cands: return os.path.join(base, cands[0])
    die('找不到 iPhoneOS SDK：设 DM_IOS_SDK=/path/to/iPhoneOS26.5.sdk（公开 SDK 仓库 xybp888/iOS-SDKs，或从 Xcode 里取）')


def sdk_version(sdk):
    p = os.path.join(sdk, 'SDKSettings.json')
    if os.path.exists(p):
        with open(p) as f: return json.load(f).get('Version') or '26.0'
    return '26.0'


def main():
    out_ipa = sys.argv[1] if len(sys.argv) > 1 else None
    GAME = os.environ.get('GAME') or die('GAME 没设：GAME=<基础版标题号>')
    W = os.environ.get('W') or die('W 没设：W=<NSP 文件夹>/%s-work' % GAME)
    GD = os.path.join(DM, 'games', GAME)
    gp = lambda k: sh([sys.executable, os.path.join(DM, 'games', 'game.py'), GAME, k], '读档案 %s' % k).strip()
    PKG = gp('package')
    PATCHED = os.environ.get('PATCHED') or os.path.join(W, 'build', 'patched_' + PKG)
    ROMFS = os.environ.get('ROMFS') or os.path.join(W, 'data', PKG, 'RomFS')
    MSL_IOS = os.environ.get('MSL_IOS') or os.path.join(W, 'build', 'ios', 'dmsh_b')
    AOC = os.environ.get('AOC', os.path.join(W, 'data', 'aoc'))
    MODS = gp('modules').split()
    MARKER = gp('romfs_marker')
    O = os.path.join(W, 'build', 'ios', 'pkg_nowin')
    SDK = find_sdk()
    VER = sdk_version(SDK)   # SDK 版本，写进 DTSDKName
    MIN = os.environ.get('DM_IOS_MIN') or '26.0'   # 最低系统版本（与 project.yml 的 deploymentTarget 一致）| deployment target, as in project.yml
    CC = os.environ.get('DM_CC') or 'clang'
    LD = os.environ.get('DM_LD') or 'ld64.lld'
    CHAIN = None
    hdr = os.path.join(GD, 'dmsh_chain.h')
    for ln in open(hdr):
        if ln.startswith('#define DMSH_CHAIN'): CHAIN = ln.split('"')[1]
    if not CHAIN: die('%s 里没有 DMSH_CHAIN' % hdr)
    for f in MODS:
        if not os.path.exists(os.path.join(PATCHED, f + '.patched.bin')): die('没有改写后的模块 %s（先跑 patch_nso.py）' % f)
    if not os.path.exists(os.path.join(ROMFS, MARKER)): die('没有 RomFS %s（缺 %s）' % (ROMFS, MARKER))
    msl_dir = os.path.join(MSL_IOS, 'b-' + CHAIN)
    if not os.path.isdir(msl_dir): die('没有 iOS 着色器库 %s（IOSOUT=%s dmsh_build.py ...）' % (msl_dir, MSL_IOS))

    say('SDK %s（最低 iOS %s），游戏 %s，链版本 %s' % (SDK, MIN, GAME, CHAIN))
    # ---- Gen/：源文件清单、嵌入的模块映像、宿主表与着色器库（与 build_ipa.sh 同一步） ----
    GEN = os.path.join(IOS, 'Gen')
    shutil.rmtree(GEN, ignore_errors=True)
    os.makedirs(os.path.join(GEN, 'dm')); os.makedirs(os.path.join(GEN, 'msl'))
    xcc = sh([sys.executable, os.path.join(HERE, 'gen_build.py'), os.path.join(DM, 'host'), GEN], 'gen_build')
    sh([sys.executable, os.path.join(HERE, 'gen_embed.py'), PATCHED, os.path.join(GEN, 'embed.S')] + MODS, 'gen_embed')
    for pat in ('.fixups.tsv', '.gates.tsv', '.params.tsv', '.cuts.tsv', '.obs.tsv'):
        for f in os.listdir(PATCHED):
            if f.endswith(pat): shutil.copyfile(os.path.join(PATCHED, f), os.path.join(GEN, 'dm', f))
    for f in ('shader_map.tsv', 'pso_manifest.tsv', 'c1_full.tsv'):
        shutil.copyfile(os.path.join(GD, f), os.path.join(GEN, 'dm', f))
    shutil.copytree(msl_dir, os.path.join(GEN, 'msl', 'b-' + CHAIN))
    if os.environ.get('DM_ENV_FILE'):
        shutil.copyfile(os.environ['DM_ENV_FILE'], os.path.join(GEN, 'dm', 'env.txt'))
    # 共享字体替身：标准字体（STD_FONT 或系统里的一个 Unicode TTF）+ 按键图标字体
    fontdir = os.path.join(GEN, 'dm', 'font'); os.makedirs(fontdir)
    std = os.environ.get('STD_FONT')
    if not std:
        for c in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                  '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
                  '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'):
            if os.path.exists(c): std = c; break
    if std and os.path.exists(std): shutil.copyfile(std, os.path.join(fontdir, 'standard.ttf'))
    else: say('没有标准字体替身（STD_FONT 指一个 TTF）：游戏里的系统文字会显示成空白')
    sh([sys.executable, os.path.join(DM, 'tools', 'make_ext_font.py'), os.path.join(fontdir, 'nintendo_ext.ttf')], 'make_ext_font')

    # ---- 编译：C / ObjC / 汇编 ----
    # 头文件路径与 -D 来自 gen_build 的输出（与 Makefile 展开同源），再加 SDK 的 framework 目录
    incs, defs, srcs = [], [], []
    for ln in xcc.splitlines():
        if ln.startswith('HEADER_SEARCH_PATHS ='): incs = [t.strip('"') for t in ln.split('=', 1)[1].split('$(inherited)', 1)[1].split() if t.startswith('"')]
        elif ln.startswith('OTHER_CFLAGS ='): defs = ln.split('=', 1)[1].split('$(inherited)', 1)[1].split()
    srcs = sorted(os.path.join(GEN, 'src', f) for f in os.listdir(os.path.join(GEN, 'src')))
    srcs = [s for s in srcs if not os.path.basename(s).endswith('_mac__front_mac.m')]   # Mac 专用源不进 iOS（与 gen_build 的 _mac 约定一致）
    # gen_build 跳过 ios/Sources（xcodebuild 把它们当独立 target 的源编）；这里要自己收进来，与 project.yml 的 Sources 一致
    # gen_build skips ios/Sources (xcodebuild compiles them as the target's own sources); collect them here, matching project.yml's Sources
    iossrc = os.path.join(IOS, 'Sources')
    srcs += sorted(os.path.join(iossrc, f) for f in os.listdir(iossrc) if f.endswith(('.m', '.c')))
    srcs.append(os.path.join(GEN, 'main', 'main.c'))
    fw = ['-F' + os.path.join(SDK, d) for d in ('System/Library/Frameworks', 'System/Library/PrivateFrameworks', 'System/Library/SubFrameworks')]
    common = ['-target', 'arm64-apple-ios%s' % MIN, '-isysroot', SDK] + fw + ['-w', '-O1', '-g'] + ['-I' + d for d in incs] + defs
    if os.environ.get('DM_SHADER_FROM_SOURCE') == '1': common.append('-DDM_SHADER_FROM_SOURCE')
    objd = os.path.join(O, 'obj'); shutil.rmtree(objd, ignore_errors=True); os.makedirs(objd)
    objs = []
    say('编译 %d 个源文件' % (len(srcs) + 1))
    for s in srcs:
        o = os.path.join(objd, os.path.basename(s) + '.o')
        extra = ['-fobjc-arc'] if s.endswith(('.m', '.mm')) else []
        if os.path.dirname(s) == os.path.join(GEN, 'main'): extra.append('-Dmain=dm_main')   # 只有宿主入口改名；ios/Sources/main.m 保留 main()
        sh([CC] + common + extra + ['-c', s, '-o', o], '编译 ' + os.path.basename(s))
        objs.append(o)
    eo = os.path.join(objd, 'embed.o')
    sh([CC] + common + ['-c', os.path.join(GEN, 'embed.S'), '-o', eo], '编译嵌入段')
    objs.append(eo)
    # libvpx：只有 VP9 解码的源，逐个编成静态库（与 Makefile 一致）
    vpxdir = os.path.join(DM, 'third_party', 'libvpx')
    vpx_objs = []
    for root, _, files in os.walk(vpxdir):
        for f in sorted(files):
            if not f.endswith('.c'): continue
            s = os.path.join(root, f)
            o = os.path.join(objd, 'vpx_' + os.path.relpath(s, vpxdir).replace(os.sep, '_') + '.o')
            sh([CC] + common + ['-I' + vpxdir, '-I' + os.path.join(vpxdir, 'config'), '-c', s, '-o', o], '编译 libvpx ' + f)
            vpx_objs.append(o)
    vpxlib = os.path.join(objd, 'libvpx.a')
    if os.path.exists(vpxlib): os.unlink(vpxlib)
    ar = os.environ.get('DM_AR') or 'llvm-ar'
    sh([ar, 'rcs', vpxlib] + vpx_objs, '打包 libvpx')
    sh([os.environ.get('DM_RANLIB') or 'llvm-ranlib', vpxlib], '建 libvpx 索引')

    # ---- 链接 ----
    # gen_embed 输出 clang 驱动格式（-Wl,-segprot,__DMmainW,rw,rw）；ld64.lld 要的是 -segprot __DMmainW rw rw
    segprot = []
    for t in sh([sys.executable, os.path.join(HERE, 'gen_embed.py'), PATCHED, os.devnull] + MODS, '取链接参数').split():
        segprot += t[len('-Wl,'):].split(',') if t.startswith('-Wl,') else [t]
    exe = os.path.join(O, 'DmIOS')
    os.makedirs(O, exist_ok=True)
    link = [LD, '-arch', 'arm64', '-platform_version', 'ios', MIN, VER, '-syslibroot', SDK, '-o', exe] + objs + [vpxlib] + segprot + [
        '-framework', 'UIKit', '-framework', 'Metal', '-framework', 'QuartzCore', '-framework', 'AVFAudio',
        '-framework', 'GameController', '-framework', 'CoreText', '-framework', 'ImageIO', '-framework', 'AudioToolbox',
        '-framework', 'UniformTypeIdentifiers', '-framework', 'Foundation', '-framework', 'CoreGraphics',
        '-L' + os.path.join(SDK, 'usr', 'lib'), '-lSystem']
    say('链接')
    sh(link, '链接（ld64.lld）', os.path.join(O, 'link.log'))

    # ---- 组装 .app ----
    APP = os.path.join(O, 'Payload', 'DmIOS.app')
    shutil.rmtree(os.path.join(O, 'Payload'), ignore_errors=True); os.makedirs(APP)
    shutil.copyfile(exe, os.path.join(APP, 'DmIOS')); os.chmod(os.path.join(APP, 'DmIOS'), 0o755)
    # 游戏名取自 NACP；读不到就用档案里的 name 键（build_ipa.sh 在这种情况下直接报错，这里退一步，
    # 因为不经过 nx_extract 的调用方拿不到 control.nacp）
    # The display name comes from NACP; fall back to the profile's name key when it cannot be read (build_ipa.sh dies
    # there instead, but callers that did not run nx_extract have no control.nacp)
    name = os.environ.get('DM_DISPLAY_NAME')
    if not name:
        nacp = os.path.join(W, 'data', 'v0', 'control.nacp')
        if os.path.exists(nacp):
            r = subprocess.run([sys.executable, os.path.join(DM, 'tools', 'nacp.py'), nacp], capture_output=True, text=True)
            name = r.stdout.strip() if r.returncode == 0 else ''
        name = name or gp('name') or GAME
    bid = os.environ.get('DM_BUNDLE_ID') or 'org.nxnative.' + GAME.lower()
    info = {'CFBundleExecutable': 'DmIOS', 'CFBundleIdentifier': bid, 'CFBundleName': name,
            'CFBundleDisplayName': name, 'CFBundlePackageType': 'APPL',
            'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1',
            'CFBundleSupportedPlatforms': ['iPhoneOS'], 'MinimumOSVersion': MIN,
            'DTPlatformName': 'iphoneos', 'DTSDKName': 'iphoneos' + VER, 'DTPlatformVersion': VER,
            'UIDeviceFamily': [1, 2], 'UIFileSharingEnabled': True, 'LSSupportsOpeningDocumentsInPlace': True,
            'UISupportedInterfaceOrientations': ['UIInterfaceOrientationLandscapeRight', 'UIInterfaceOrientationLandscapeLeft'],
            'UIRequiresFullScreen': True, 'UIStatusBarHidden': True, 'UIViewControllerBasedStatusBarAppearance': False,
            'LSApplicationCategoryType': gp('app_category') or 'public.app-category.action-games',
            'LSSupportsGameMode': True, 'GCSupportsControllerUserInteraction': True,
            'GCSupportedGameControllers': [{'ProfileName': 'ExtendedGamepad'}],
            'CADisableMinimumFrameDurationOnPhone': True,
            'UIApplicationSceneManifest': {'UIApplicationSupportsMultipleScenes': False,
                'UISceneConfigurations': {'UIWindowSceneSessionRoleApplication': [
                    {'UISceneConfigurationName': 'Default', 'UISceneDelegateClassName': 'SceneDelegate'}]}}}
    with open(os.path.join(APP, 'Info.plist'), 'wb') as f: plistlib.dump(info, f)
    shutil.copytree(os.path.join(GEN, 'dm'), os.path.join(APP, 'dm'))
    shutil.copytree(os.path.join(GEN, 'msl'), os.path.join(APP, 'msl'))
    # RomFS：硬链接省空间与时间（同一文件系统时），否则普通复制
    say('放 RomFS')
    def link_or_copy(src, dst):
        try: os.link(src, dst)
        except OSError: shutil.copy2(src, dst)
    shutil.copytree(ROMFS, os.path.join(APP, 'romfs'), copy_function=link_or_copy)
    if AOC and os.path.isdir(AOC): shutil.copytree(AOC, os.path.join(APP, 'aoc'), copy_function=link_or_copy)
    icon = os.environ.get('DM_ICON') or os.path.join(os.path.dirname(ROMFS), 'icon.jpg')
    if os.path.exists(icon):
        # 无 actool：直接把 1024 PNG 放进包，Info.plist 里按 CFBundleIcons 的旧式键指过去
        try:
            from PIL import Image
            dst = os.path.join(APP, 'AppIcon60x60@2x.png'); Image.open(icon).convert('RGBA').resize((120, 120)).save(dst)
            info['CFBundleIconFiles'] = ['AppIcon60x60@2x']
            with open(os.path.join(APP, 'Info.plist'), 'wb') as f: plistlib.dump(info, f)
            say('图标 %s' % icon)
        except ImportError:
            say('没有 Pillow，跳过图标（pip install pillow）')

    # ---- zip ----
    out_ipa = out_ipa or os.path.join(os.path.dirname(W), gp('ipa'))
    store = tuple(gp('zip_store').split(':')) if gp('zip_store') else ('.zs', '.bwav', '.metallib')
    say('打包 %s' % out_ipa)
    with zipfile.ZipFile(out_ipa, 'w', zipfile.ZIP_STORED) as z:
        for root, _, files in os.walk(os.path.join(O, 'Payload')):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, O)
                method = zipfile.ZIP_STORED if f.endswith(store) else zipfile.ZIP_DEFLATED
                z.write(p, rel, compress_type=method)
    say('完成：%s（%.1f MB）' % (out_ipa, os.path.getsize(out_ipa) / 1e6))


if __name__ == '__main__':
    main()
