#!/usr/bin/env python3
# Windows 侧的批量编译驱动，由 metal_tool._compile_batch_windows 一次调用跑完整批。
# 单独存在的理由：Apple 的 Metal 工具链是 Windows 程序，WSL 里每起一个 Windows 进程约 116 ms；
#   16000 个着色器逐个调用要一小时纯开销，所以整批只跨一次边界，在这里循环。
# 用法：metal_win_batch.py <作业.json> <结果.json>
#   作业：{"metal": metal.exe 路径, "sdk": "iphoneos", "flags": [...], "jobs": [[源, 目标], ...]}
#   结果：{"errs": [null 或 失败原因, ...]}，与 jobs 一一对应
import json, os, subprocess, sys


def main():
    if len(sys.argv) != 3: sys.exit(__doc__ or '用法：metal_win_batch.py <作业.json> <结果.json>')
    man_path, res_path = sys.argv[1:]
    with open(man_path, encoding='utf-8') as f: man = json.load(f)
    metal = man['metal']
    bin_dir = os.path.dirname(metal)
    metallib = os.path.join(bin_dir, 'metallib.exe')
    if not os.path.exists(metallib): metallib = os.path.join(bin_dir, 'metallib')
    # -sdk iphoneos 在 Windows 版里对应 -target 或 -mios-version-min；这里两种都传，工具不认的会被忽略或报错，
    # 由上层把失败原因带回去，而不是在这里猜。
    common = list(man.get('flags') or [])
    errs = []
    for src, lib in man['jobs']:
        air = os.path.splitext(src)[0] + '.air'
        try:
            r = subprocess.run([metal, '-c', *common, src, '-o', air], capture_output=True, text=True)
            if r.returncode:
                errs.append((r.stderr or r.stdout or 'metal -c failed').strip()[:200]); continue
            r = subprocess.run([metallib, air, '-o', lib], capture_output=True, text=True)
            errs.append(None if r.returncode == 0 else ((r.stderr or r.stdout or 'metallib failed').strip()[:200]))
        except OSError as e:
            errs.append(str(e)[:200])
        finally:
            try: os.unlink(air)
            except OSError: pass
    with open(res_path, 'w', encoding='utf-8') as f: json.dump({'errs': errs}, f)


if __name__ == '__main__':
    main()
