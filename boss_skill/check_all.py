# -*- coding: utf-8 -*-
"""一键体检入口：改完代码必跑，把"改后三步验证"和回归体检串起来。

用法：
    python check_all.py              # 快速版：静态检查 + 3 个快回归（跳过会调模型的 evidence_pick）
    python check_all.py --full       # 全量版：额外跑 _check_evidence_pick（会真调模型，慢）

顺序（对应"改后三步验证"纪律）：
    ① 语法体检（py_compile 全部 .py）
    ② 静态卫生（code-doctor：import 了没装/调用了不存在的函数/裸 except 等）
    ③ 招呼语回归三连（锚点 / 招呼语质量门 / 打分分母口径）

设计说明：
    - 各 _check_* 脚本是"脚本体直接执行"（非 def main），所以用 subprocess 逐个跑，
      不能 import 调函数。
    - _check_evidence_pick 会真调大模型、每条几十秒，默认跳过，--full 才跑。
    - 用隔离环境那个 Python（系统 Python 没装 yaml，这是历史坑）。
"""
import os
import subprocess
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
# 用当前解释器跑子命令，避免硬编码某台机器的 Python 路径（开源通用）。
PY = sys.executable


def _run(cmd, timeout=300):
    """跑一条命令，返回 (exit_code, 输出最后几行)。"""
    env = dict(os.environ)
    env.setdefault("BOSS_DRIVER", "plugin")
    try:
        p = subprocess.run(cmd, cwd=HERE, env=env,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        tail = (p.stdout or p.stderr or "").strip().splitlines()
        tail = tail[-12:] if tail else []
        return p.returncode, "\n".join(tail)
    except subprocess.TimeoutExpired:
        return 124, "(超时)"


def main():
    full = "--full" in sys.argv
    results = []  # (步骤名, 是否通过, 输出)

    # ── ① 语法体检 ──
    print("=" * 70)
    print("① 语法体检（py_compile 全部 .py）")
    print("=" * 70)
    pys = glob.glob(os.path.join(HERE, "*.py"))
    bad = []
    for p in pys:
        rc, out = _run([PY, "-m", "py_compile", p], timeout=120)
        if rc != 0:
            bad.append(os.path.basename(p))
    ok = not bad
    results.append(("① 语法体检", ok,
                    "全部通过" if ok else f"以下文件语法错误：{bad}"))
    print(("✓ 通过" if ok else "✗ 失败") + ("" if ok else f"：{bad}"))

    # ── ② 静态卫生（code-doctor）──
    print()
    print("=" * 70)
    print("② 静态卫生（code-doctor：未装的 import / 不存在的函数 / 裸 except）")
    print("=" * 70)
    # code-doctor 是可选体检工具，未安装就跳过这一步（开源版不强制依赖）
    doctor = os.environ.get("CODE_DOCTOR", "")
    if doctor and os.path.exists(doctor):
        rc, out = _run([PY, doctor, HERE], timeout=180)
        # code-doctor 有"错误"级才判失败；只报"提示"不算失败
        ok = "错误" not in out
        results.append(("② 静态卫生", ok, out))
        print(out)
        print("→" + ("通过（无错误级问题）" if ok else "有错误级问题，先处理"))
    else:
        results.append(("② 静态卫生", False, "找不到 code-doctor 的 check.py，跳过"))
        print("（code-doctor 未安装，跳过此步）")

    # ── ③ 招呼语回归三连 ──
    regressions = [
        ("③a 锚点体检（有没有编数字）", "_check_anchors.py", None, 120),
        ("③b 招呼语质量门（114 条回归）", "_check_greeting.py", None, 300),
        ("③c 打分分母口径", "_check_denominator.py", None, 300),
    ]
    if full:
        regressions.append(
            ("③d 证据选（模型 vs 规则，慢）", "_check_evidence_pick.py", None, 900))

    for name, script, _arg, timeout in regressions:
        print()
        print("=" * 70)
        print(name)
        print("=" * 70)
        rc, out = _run([PY, os.path.join(HERE, script)], timeout=timeout)
        ok = rc == 0
        results.append((name, ok, out))
        print(out if out else f"(退出码 {rc})")
        print("✓ 通过" if ok else "✗ 失败/超时")

    # ── 汇总 ──
    print()
    print("=" * 70)
    print("体检汇总")
    print("=" * 70)
    n_fail = 0
    for name, ok, _out in results:
        print(("  ✓ " if ok else "  ✗ ") + name)
        if not ok:
            n_fail += 1
    print()
    if n_fail == 0:
        print("全部通过。可以继续改/投。")
        return 0
    print(f"有 {n_fail} 项没过，先处理再往下走。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
