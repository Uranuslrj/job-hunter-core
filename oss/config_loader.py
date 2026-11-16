# 读配置文件的唯一入口
# 为什么要单独抽出来：所有判断规则都在 config/*.yaml 里，这里只负责读，
# 以后改规则不用动代码。

# 说明（开源版）：
#   ROOT 指向本文件所在目录（oss/），不是项目根目录。
#   这样这套「安全层」代码是独立的一包，跟使用者的本地投递环境完全隔离。
#   配置文件优先读 config/*.yaml（使用者自己填），没有就回退到
#   config/*.example.yaml（空模板），保证 clone 下来开箱即用。

import os
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")


def load(name):
    """优先读真实配置，没有就回退到 example 模板。"""
    real = os.path.join(CONFIG_DIR, name)
    if os.path.exists(real):
        path = real
    else:
        example = os.path.join(CONFIG_DIR, name.replace(".yaml", ".example.yaml"))
        path = example
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def roles():
    return load("target_roles.yaml")


def greeting_spec():
    return load("greeting_spec.yaml")


def path_of(rel):
    return os.path.join(ROOT, rel)
