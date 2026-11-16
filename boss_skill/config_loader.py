# 读配置文件的唯一入口
# 为什么要单独抽出来：所有判断规则都在 config/*.yaml 里，这里只负责读，
# 以后改规则不用动代码。
#
# 模板回退机制（开源后新增）：
#   优先读 config/<name>.yaml（真实配置）；找不到时回退到
#   config/<name>.example.yaml（脱敏模板），让 clone 下来的人填完模板就能跑。

import os
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")


def load(name):
    """读 <name>.yaml，不存在则回退 <name>.example.yaml。"""
    path = os.path.join(CONFIG_DIR, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    # 回退到 example 模板
    example = os.path.join(CONFIG_DIR, name.replace(".yaml", ".example.yaml"))
    if os.path.exists(example):
        with open(example, encoding="utf-8") as f:
            return yaml.safe_load(f)

    raise FileNotFoundError(
        f"找不到 {name}，也没有 {os.path.basename(example)}。"
        f"请复制 config/ 下的 example 模板并填成你自己的配置。"
    )


def roles():
    return load("target_roles.yaml")


def greeting_spec():
    return load("greeting_spec.yaml")


def path_of(rel):
    return os.path.join(ROOT, rel)
