#!/usr/bin/env python3
# Windows 侧的批量编译驱动，由 metal_tool._compile_batch_windows 一次调用跑完整批。
# 单独存在的理由：Apple 的 Metal 工具链是 Windows 程序，WSL 里每起一个 Windows 进程约 116 ms；
#   16000 个着色器逐个调用要一小时纯开销，所以整批只跨一次边界，在这里循环。
# 为什么源文件要先拷到本地：metal.exe 打不开 \\wsl.localhost\ 的 UNC 路径（Python 打得开，它自己不行），
#   所以在这边读出来写进本地临时目录再编，编好的 metallib 再写回去。
# 用法：metal_win_batch.py <作业.json> <结果.json>
#   作业：{"metal": metal.exe 路径, "sdk": "iphoneos", "flags": [...], "jobs": [[源, 目标], ...], "work": 本地临时目录}
#   结果：{"errs": [null 或 失败原因, ...]}，与 jobs 一一对应
import json, os, shutil, subprocess, sys
from concurrent.futures import ThreadPoolExecutor


def one(metal, metallib, flags, work, i, src, lib):
    """一个作业：源拷到本地、编译、链接、写回目标。返回失败原因或 None。
    metal.exe 每个进程约 0.6 s 的启动开销，所以并行跑；实测 8 路把 968 ms/个 降到 182 ms/个。
    metal.exe costs about 0.6 s of startup per process, so run several at once: 8 workers measured 182 ms/job
    against 968 ms/job serial."""
    local_src = os.path.join(work, '%d.metal' % i)
    local_air = os.path.join(work, '%d.air' % i)
    local_lib = os.path.join(work, '%d.metallib' % i)
    same = False
    try:
        shutil.copyfile(src, local_src)
        r = subprocess.run([metal, '-c', local_src, '-o', local_air, *flags], capture_output=True, text=True)
        if r.returncode: return (r.stderr or r.stdout or 'metal -c failed').strip()[:200]
        r = subprocess.run([metallib, local_air, '-o', local_lib], capture_output=True, text=True)
        if r.returncode: return (r.stderr or r.stdout or 'metallib failed').strip()[:200]
        same = os.path.realpath(local_lib) == os.path.realpath(lib)
        if not same: shutil.copyfile(local_lib, lib)
        return None
    except OSError as e:
        return str(e)[:200]
    finally:
        for p in (local_src, local_air) + (() if same else (local_lib,)):
            try: os.unlink(p)
            except OSError: pass


def main():
    if len(sys.argv) != 3: sys.exit(__doc__ or '用法：metal_win_batch.py <作业.json> <结果.json>')
    man_path, res_path = sys.argv[1:]
    with open(man_path, encoding='utf-8') as f: man = json.load(f)
    metal = man['metal']
    bin_dir = os.path.dirname(metal)
    metallib = os.path.join(bin_dir, 'metallib.exe')
    if not os.path.exists(metallib): metallib = os.path.join(bin_dir, 'metallib')
    work = man.get('work') or os.path.dirname(os.path.abspath(man_path))
    flags = list(man.get('flags') or [])
    jobs = man['jobs']
    workers = int(man.get('workers') or 8)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        errs = list(ex.map(lambda a: one(metal, metallib, flags, work, a[0], a[1][0], a[1][1]), enumerate(jobs)))
    with open(res_path, 'w', encoding='utf-8') as f: json.dump({'errs': errs}, f)


if __name__ == '__main__':
    main()
