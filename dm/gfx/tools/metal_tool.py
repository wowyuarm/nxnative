#!/usr/bin/env python3
# 这台机器上用哪套 Metal 着色器编译器：一处判定，全链共用。
# 三种工具链，决定 dmsh_chain.py 选哪份锁文件、算哪个链版本，也决定产物里放的是 metallib 还是 MSL 源码：
#   macos    Xcode 的 xcrun metal / metallib
#   windows  Apple 的 Metal Developer Tools for Windows（metal.exe），在 WSL 里经 Windows 互操作调用
#   nometal  没有苹果编译器：MSL 源码原样进包，设备上首次用到时现编（宿主 DM_SHADER_FROM_SOURCE 分支）
# 选择：环境变量 DM_METAL=macos|windows|nometal 强制；不设则按平台与已安装的工具自动判定。
# 工具路径：DM_METAL_EXE 指到 metal.exe，否则在 Windows 常见安装位置里找。
# 跨边界：WSL 里每启动一个 Windows 进程约 116 ms，16000 个着色器逐个调用要一小时纯开销。所以批量编译走
#   metal_win_batch.py：一次跨越，Windows 侧循环编译。compile_batch() 是全链唯一入口。
# Why one module: the chain version must change exactly when the compiler changes, and every caller
# (chain lock, iOS compile record, offline build) has to agree on that, or the host refuses libraries it should accept.
import json, os, shutil, subprocess, sys, tempfile

PROFILES = ('macos', 'windows', 'nometal')
HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = os.path.join(HERE, 'metal_win_batch.py')

# Windows 版工具链的默认安装位置（Apple 的安装器把 bin 放在这些目录下）
WIN_CANDIDATES = [
    r'C:\Program Files\Apple\Metal Developer Tools\bin',
    r'C:\Program Files\Apple\Metal Developer Tools\usr\bin',
    r'C:\Program Files (x86)\Apple\Metal Developer Tools\bin',
    r'C:\Program Files\Apple\Metal\bin',
]

_cached = {}


def _win_path(path):
    """WSL 路径 → Windows 可读的 UNC 路径；已是 Windows 路径时原样返回。"""
    if not path.startswith('/'): return path
    distro = os.environ.get('WSL_DISTRO_NAME')
    if distro: return '\\\\wsl.localhost\\%s%s' % (distro, path.replace('/', '\\'))
    try:
        r = subprocess.run(['wslpath', '-w', path], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip()
    except OSError:
        pass
    return path


def _mount(d):
    """C:\\Program Files\\... → /mnt/c/Program Files/..."""
    return '/mnt/' + d[0].lower() + d[2:].replace('\\', '/')


def _find_windows_metal():
    env = os.environ.get('DM_METAL_EXE')
    if env: return env
    for d in WIN_CANDIDATES:
        for name in ('metal.exe', 'metal'):
            p = os.path.join(_mount(d), name)
            if os.path.exists(p): return p
    return shutil.which('metal.exe')


def _xcrun_works():
    if 'xcrun' in _cached: return _cached['xcrun']
    ok = False
    if shutil.which('xcrun'):
        r = subprocess.run(['xcrun', '-sdk', 'iphoneos', 'metal', '--version'], capture_output=True, text=True)
        ok = r.returncode == 0
    _cached['xcrun'] = ok
    return ok


def profile():
    """本机的工具链代号。DM_METAL 可强制。"""
    if 'profile' in _cached: return _cached['profile']
    forced = os.environ.get('DM_METAL')
    if forced:
        if forced not in PROFILES: sys.exit('DM_METAL=%s 不认识（可选 %s）' % (forced, ' / '.join(PROFILES)))
        p = forced
    elif sys.platform == 'darwin':
        p = 'macos' if _xcrun_works() else 'nometal'
    elif _find_windows_metal():
        p = 'windows'
    else:
        p = 'nometal'
    _cached['profile'] = p
    return p


def needs_source_shaders():
    """产物里放源码而不是 metallib。"""
    return profile() == 'nometal'


def version(sdk):
    """编译器版本行，写进工具锁。没有编译器时是 'none'（与 nometal_run.py 同口径）。"""
    p = profile()
    if p == 'nometal': return 'none'
    try:
        if p == 'macos':
            r = subprocess.run(['xcrun', '-sdk', sdk, 'metal', '--version'], capture_output=True, text=True)
        else:
            exe = _find_windows_metal()
            if not exe: return 'missing'
            r = subprocess.run([exe, '--version'], capture_output=True, text=True)
        return (r.stdout.splitlines() or ['unknown'])[0].strip()
    except OSError:
        return 'missing'


def record(sdk):
    """写进反射 json 与链版本的编译口径字符串：记录 == 实际。"""
    p = profile()
    if p == 'nometal': return 'no Apple Metal compiler: MSL source ships in the package, compiled on device'
    if p == 'macos': return 'xcrun -sdk %s metal -c; xcrun -sdk %s metallib' % (sdk, sdk)
    return 'Metal Developer Tools for Windows: metal.exe -c; metallib.exe'


def compile_batch(jobs, sdk='iphoneos', flags=(), log=None):
    """jobs = [(源 .metal, 目标 metallib)]，批量编译。返回每个 job 的失败原因（None = 成功）。
    nometal 时把源码原样写进目标（换行归一），宿主按 DM_SHADER_FROM_SOURCE 现编。"""
    if not jobs: return []
    p = profile()
    if p == 'nometal':
        errs = []
        for src, lib in jobs:
            try:
                with open(src, 'rb') as f: b = f.read()
                with open(lib, 'wb') as f: f.write(b.replace(b'\r\n', b'\n'))   # Windows 上的 spirv-cross 写 CRLF：归一化，同一份输入在任何系统上得到同一个包
                errs.append(None)
            except OSError as e:
                errs.append(str(e)[:200])
        return errs
    if p == 'macos':
        errs = []
        for src, lib in jobs:
            air = os.path.splitext(src)[0] + '.air'
            r = subprocess.run(['xcrun', '-sdk', sdk, 'metal', '-c', *flags, src, '-o', air], capture_output=True, text=True)
            if r.returncode: errs.append((r.stderr or '').strip()[:200] or 'metal failed'); continue
            r = subprocess.run(['xcrun', '-sdk', sdk, 'metallib', air, '-o', lib], capture_output=True, text=True)
            errs.append(None if r.returncode == 0 else ((r.stderr or '').strip()[:200] or 'metallib failed'))
            try: os.unlink(air)
            except FileNotFoundError: pass
        return errs
    return _compile_batch_windows(jobs, sdk, flags, log)


def _compile_batch_windows(jobs, sdk, flags, log):
    """一次 Windows 进程编译整批，避免每次 116 ms 的跨边界开销。"""
    metal = _find_windows_metal()
    if not metal: sys.exit('DM_METAL=windows 但找不到 metal.exe：设 DM_METAL_EXE，或装 Apple 的 Metal Developer Tools for Windows')
    # Windows 侧要有 Python：优先 python.exe，否则 py -3
    winpy = os.environ.get('DM_WIN_PYTHON') or shutil.which('python.exe') or shutil.which('py.exe')
    if not winpy:
        for c in (r'C:\Users\%s\AppData\Local\Programs\Python\Python313\python.exe',):
            pass
        cand = shutil.which('python3.exe')
        winpy = cand
    if not winpy:
        sys.exit('找不到 Windows 侧的 python.exe（Metal 工具链是 Windows 程序，批量驱动要在 Windows 侧跑）：装一个，或用 DM_WIN_PYTHON 指定')
    man = {'metal': _win_path(metal), 'sdk': sdk, 'flags': list(flags),
           'jobs': [[_win_path(s), _win_path(l)] for s, l in jobs]}
    fd, mp = tempfile.mkstemp(suffix='.json'); os.close(fd)
    rp = mp + '.res'
    try:
        with open(mp, 'w', encoding='utf-8') as f: json.dump(man, f)
        r = subprocess.run([winpy, _win_path(BATCH), _win_path(mp), _win_path(rp)], capture_output=True, text=True)
        if log: open(log, 'w').write(r.stdout + r.stderr)
        if not os.path.exists(rp):
            sys.exit('Windows 侧批量编译没有产出结果文件（%s）：%s' % (rp, (r.stdout + r.stderr).strip()[-400:]))
        with open(rp, encoding='utf-8') as f: res = json.load(f)
        return res['errs']
    finally:
        for p in (mp, rp):
            try: os.unlink(p)
            except FileNotFoundError: pass
