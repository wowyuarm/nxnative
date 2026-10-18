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
import json, os, re, shutil, subprocess, sys, tempfile

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
# 非默认位置：安装时选了别的目录（例如 E:\app\Metal Developer Tools）。用文件名找一层，比穷举盘符可靠。
# Non-default installs (e.g. E:\app\Metal Developer Tools): look one level down for the folder by name, which is more
# reliable than enumerating drive letters.
WIN_SEARCH_DIRS = ['/mnt/' + d for d in 'cdefgh'] + ['/mnt/e/app']

_cached = {}


def _win_path(path):
    r"""WSL 路径 → Windows 能打开的路径。三种情况：
    /mnt/<盘>/...  → <盘>:\...（本来就是 Windows 本地盘，转成 UNC 反而绕远，而且 Windows 程序在那里改名会失败）
    其它 /...      → \\wsl.localhost\<发行版>\...（WSL 自己的文件系统，只能走 UNC）
    已是 Windows 路径 → 原样返回
    A WSL path → a path Windows can open: /mnt/<drive> maps to <drive>: (a Windows-local disk, and going through UNC there
    makes metal.exe's rename fail), anything else to \\wsl.localhost\<distro>\..., and Windows paths pass through."""
    if not path.startswith('/'): return path
    m = re.match(r'^/mnt/([a-zA-Z])(/.*)?$', path)
    if m: return m.group(1).upper() + ':' + (m.group(2) or '/').replace('/', '\\')
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
    if 'winmetal' in _cached: return _cached['winmetal']
    env = os.environ.get('DM_METAL_EXE')
    if env:
        p = _mount(env) if not env.startswith('/') else env
        _cached['winmetal'] = p
        return p
    found = None
    for d in WIN_CANDIDATES:
        for name in ('metal.exe', 'metal'):
            p = os.path.join(_mount(d), name)
            if os.path.exists(p): found = p; break
        if found: break
    if not found:
        for base in WIN_SEARCH_DIRS:
            if not os.path.isdir(base): continue
            try: entries = os.listdir(base)
            except OSError: continue
            for e in entries:
                if 'metal' not in e.lower(): continue
                for sub in ('bin', 'usr/bin', ''):
                    p = os.path.join(base, e, sub, 'metal.exe') if sub else os.path.join(base, e, 'metal.exe')
                    if os.path.exists(p): found = p; break
                if found: break
            if found: break
    if not found: found = shutil.which('metal.exe')
    _cached['winmetal'] = found
    return found


def _win_tempdir():
    """Windows 侧可写的临时目录（映射成 WSL 路径）。C:\\Windows\\Temp 普通用户没有写权限，要用用户自己的 Temp。
    A writable Windows-local temp dir (as a WSL path). C:\\Windows\\Temp is not writable for a normal user; the user's own Temp is."""
    # 只考虑映射到 Windows 本地盘的位置：WSL 自己的 ~ 是 UNC，Windows 程序在那里改名会失败
    # Only Windows-local drives: the WSL home is UNC, where a Windows program cannot rename
    cands = []
    wt = os.environ.get('DM_WIN_TEMP')
    if wt: cands.append(_mount(wt) if not wt.startswith('/') else wt)
    u = _win_user()
    if u: cands.append('/mnt/c/Users/%s/AppData/Local/Temp/dmsh_metal' % u)
    cands += ['/mnt/c/Temp/dmsh_metal', '/mnt/d/Temp/dmsh_metal']
    for d in cands:
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK): return d
        except OSError:
            pass
    sys.exit('找不到可写的 Windows 临时目录：设 DM_WIN_TEMP 指向 Windows 本地盘上的一个目录')


def _win_user():
    """Windows 用户名：取 /mnt/c/Users 下真实存在的目录，不靠 %USERNAME%。
    cmd.exe 经 WSL 互操作时拿到的可能是未展开的 \"(Current User Name)\"
    The Windows user: a directory that actually exists under /mnt/c/Users rather than %USERNAME%, which comes back
    unexpanded ("(Current User Name)") through WSL interop."""
    if 'winuser' in _cached: return _cached['winuser']
    skip = ('All Users', 'Default', 'Default User', 'Public', 'desktop.ini', '(Current User Name)')
    name = None
    try:
        cands = [e for e in os.listdir('/mnt/c/Users')
                 if e not in skip and os.path.isdir(os.path.join('/mnt/c/Users', e))]
        # 优先有 Windows Python 安装的那个：那是真正在用的账号（Default 之类是模板目录）
        # Prefer the one with a Windows Python install: that is the account in use (Default and friends are templates)
        withpy = [e for e in cands if os.path.isdir('/mnt/c/Users/%s/AppData/Local/Programs/Python' % e)]
        name = (withpy or cands or [None])[0]
    except OSError:
        name = None
    _cached['winuser'] = name
    return name


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


def _find_windows_python():
    """Windows 侧的 Python。shutil.which 在 WSL 里查的是 Linux 的 PATH，看不到 Windows 装的 Python。
    The Windows-side Python. shutil.which searches the Linux PATH, so it does not see a Windows install."""
    if 'winpy' in _cached: return _cached['winpy']
    found = None
    u = _win_user()
    cands = []
    if u:
        base = '/mnt/c/Users/%s/AppData/Local/Programs/Python' % u
        if os.path.isdir(base):
            for e in sorted(os.listdir(base), reverse=True):
                cands.append(os.path.join(base, e, 'python.exe'))
    cands += ['/mnt/c/Windows/py.exe', '/mnt/c/Python311/python.exe', '/mnt/c/Python312/python.exe', '/mnt/c/Python313/python.exe']
    for p in cands:
        if os.path.exists(p): found = p; break
    if not found: found = shutil.which('python.exe') or shutil.which('py.exe')
    _cached['winpy'] = found
    return found


def _win_flags(sdk, flags):
    """Windows 版 metal 不认 -sdk：换成 -target。iOS 的目标三元组与 Xcode 的 -sdk iphoneos 等价。
    另外必须显式给 -std：Mac 版从 -sdk 推出语言标准，Windows 版不会，默认 macos-metal1.x，
    而 spirv-cross 出的是 MSL 3.0（dmsh_build 的 --msl-version 20300），否则报
    "'id' attribute requires Metal language standard macos-metal2.0 or higher"。
    The Windows build has no -sdk: use -target. It also needs -std explicitly: the Mac build infers the language
    standard from -sdk, the Windows build defaults to macos-metal1.x while spirv-cross emits MSL 3.0
    (--msl-version 20300 in dmsh_build)."""
    tgt = {'iphoneos': 'air64-apple-ios', 'macosx': 'air64-apple-macos'}.get(sdk)
    out = list(flags)
    if tgt and not any(f.startswith('-target') for f in out): out += ['-target', tgt]
    if not any(f.startswith('-std=') for f in out): out += ['-std=metal3.0']
    return out


def _compile_batch_windows(jobs, sdk, flags, log):
    """一次 Windows 进程编译整批，避免每次 116 ms 的跨边界开销。"""
    metal = _find_windows_metal()
    if not metal: sys.exit('DM_METAL=windows 但找不到 metal.exe：设 DM_METAL_EXE，或装 Apple 的 Metal Developer Tools for Windows')
    # Windows 侧要有 Python：优先 python.exe，否则 py -3
    winpy = os.environ.get('DM_WIN_PYTHON') or _find_windows_python()
    if not winpy:
        sys.exit('找不到 Windows 侧的 python（Metal 工具链是 Windows 程序，批量驱动要在 Windows 侧跑）：装一个，或用 DM_WIN_PYTHON 指定')
    # 输出交给 Windows 侧写到本地临时目录：metal.exe 先写 .tmp 再改名，UNC 上这一步会失败（invalid argument）。
    # 输入可以直接读 UNC，输出不行。
    # Outputs go to a Windows-local temp dir: metal.exe writes a .tmp and renames it, which fails over UNC
    # (invalid argument). Inputs can be read from UNC; outputs cannot be written there.
    wtmp = _win_tempdir()
    # 作业目标放 out/，中间文件放 tmp/：同目录会让驱动分不清"要保留的产物"和"要删的中间物"
    # Job targets go in out/, intermediates in tmp/: in one directory the driver cannot tell an output it must keep
    # from an intermediate it must delete
    wout = os.path.join(wtmp, 'out')
    os.makedirs(wout, exist_ok=True)
    staged = []
    for i, (s, l) in enumerate(jobs):
        staged.append((_win_path(s), _win_path(os.path.join(wout, '%d.metallib' % i))))
    man = {'metal': _win_path(metal), 'sdk': sdk, 'flags': _win_flags(sdk, flags), 'jobs': [list(x) for x in staged],
           'work': _win_path(wtmp), 'workers': int(os.environ.get('J') or 8)}
    # 作业与结果文件也放在 Windows 本地目录：结果由 Windows 侧写回，写在 UNC 上会失败
    # The job and result files live in the Windows-local dir too: Windows writes the result, which fails over UNC
    mp = os.path.join(wtmp, 'job-%d.json' % os.getpid()); rp = mp + '.res'
    try:
        with open(mp, 'w', encoding='utf-8') as f: json.dump(man, f)
        r = subprocess.run([winpy, _win_path(BATCH), _win_path(mp), _win_path(rp)], capture_output=True, text=True)
        if log: open(log, 'w').write(r.stdout + r.stderr)
        if not os.path.exists(rp):
            sys.exit('Windows 侧批量编译没有产出结果文件（%s）：%s' % (rp, (r.stdout + r.stderr).strip()[-400:]))
        with open(rp, encoding='utf-8') as f: res = json.load(f)
        errs = res['errs']
        for i, ((src, lib), (_, wlib), e) in enumerate(zip(jobs, staged, errs)):
            if e: continue
            local = _mount(wlib)
            if not os.path.exists(local):
                errs[i] = 'Windows 侧没有产出 %s' % wlib; continue
            try:
                if os.path.realpath(local) != os.path.realpath(lib):
                    shutil.copyfile(local, lib)
            except OSError as ex:
                errs[i] = str(ex)[:200]
                continue
            try: os.unlink(local)
            except OSError: pass
        return errs
    finally:
        for p in (mp, rp):
            try: os.unlink(p)
            except FileNotFoundError: pass
