# iOS 着色器出包链的唯一编译口径：任意来源的 MSL（PC 链 / 常数补丁 / 今后 Switch 原版经 Ryujinx 翻译）→ iphoneos metallib。
# 编译参数只在这里定义一次；反射 json 的 chain.metal 记录由同一份参数生成，记录 == 实际。
# 没有苹果编译器的机器（Linux / 没装工具链的 Windows）上，这里把 MSL 源码原样写进库文件，宿主首次用到时现编
# （nvn_mtl.m 的 DM_SHADER_FROM_SOURCE 分支）；工具链判定与跨边界批处理都在 metal_tool.py。
# The single compile standard for the iOS shader packaging chain: MSL from any source (PC chain / constant patches / in future, the Switch original translated via Ryujinx) → iphoneos metallib.
# Compile parameters are defined only once, here; the chain.metal record in the reflection json is generated from the same parameters, record == actual.
# On a machine without Apple's compiler (Linux / Windows without the toolchain) the MSL source is written into the library file as is, and the host compiles it when first used
# (the DM_SHADER_FROM_SOURCE branch of nvn_mtl.m); the toolchain choice and the cross-boundary batch live in metal_tool.py.
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../gfx/tools'))
import metal_tool

SDK = 'iphoneos'
# -fmetal-math-mode=relaxed：保留快速数学的重排 / 合并，但不许假设没有 NaN / Inf。翻译出的着色器靠 isnan 选择复现 Maxwell 的 NaN 语义（8337 / 12809 个用到），
# 缺省 fast 让各 GPU 的后端编译器可以把它们当成恒假；A20 Pro 上开场关卡的奇数帧主画面 NaN 72 万像素（M4 Pro 同一份 AIR 只有几十个），黑带 / 黑边光晕即由此而来
# -fmetal-math-mode=relaxed: keep fast math's reassociation / contraction, but forbid assuming there are no NaN / Inf. The translated shaders reproduce Maxwell's NaN semantics with isnan selects (8337 / 12809 use them),
# and the default fast mode lets each GPU's backend compiler treat them as always false; on the A20 Pro the opening course's odd frames had 727k NaN pixels in the main image (a few dozen with the same AIR on the M4 Pro), which became the black band / black-edged glow
FLAGS = ['-mios-version-min=18.0', '-fmetal-math-mode=relaxed']
RECORD = metal_tool.record(SDK)


def compile(msl, air, lib):
    """单个 MSL → metallib（air 参数保留给旧调用点，批量路径不产生中间文件）。"""
    errs = metal_tool.compile_batch([(msl, lib)], SDK, FLAGS)
    if errs and errs[0]:
        raise RuntimeError('iOS metal 编译失败 %s：%s' % (os.path.basename(msl), errs[0]))


def compile_many(pairs, log=None):
    """[(msl, lib)] 一次编完；返回与输入对应的失败原因（None = 成功）。"""
    return metal_tool.compile_batch(pairs, SDK, FLAGS, log)
