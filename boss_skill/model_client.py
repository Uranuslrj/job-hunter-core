# 大模型接入层 —— 统一入口 + 降级 + 缓存
#
# 设计原则（来自 docs/系统评估-现状对比与大模型接入优先级.md）：
#   1. 真正在投递的那一段，一次模型都不调 —— 每条之间只隔 12~35 秒，等不起。
#      招呼语改写这类不急的活，提前一晚批量生成。
#   2. 钱不是瓶颈（估算每月 ¥10 以内），延迟和输出不稳定才是 → 所以降级规则必须硬。
#   3. 模型只做"锦上添花"，所有调用点都必须有规则版兜底：
#      模型超时 / 编数字 / 连续挂 10 次 → 自动退回规则版，当天不再试。
#   4. 永不交给模型的四样东西：防串号六检、逐字回读、薪资下限/城市白名单、
#      简历里的数字（模型会编）。
#
# 接入哪个模型？配置写在 config/model.yaml：
#   backend: none      ← 现在的默认值。什么都不调，全部走规则版，一分钱不花
#   backend: openai    ← 任何"OpenAI 兼容"的接口都行（环境变量 OPENAI_API_KEY
#                         和 OPENAI_BASE_URL，或直接写在 yaml 里）
#
# 用法（其他模块怎么调）：
#   from model_client import client
#   out = client.chat([{"role":"user","content":"..."}], tag="intent")
#   if out is None:   # 模型没接 / 超时 / 被熔断 → 用规则版
#       out = rule_version(...)
#
# 缓存：同样的输入（tag+消息+温度）第二次直接读本地缓存，不花钱不花时间。
# 缓存文件：data/model_cache.json，上限 2000 条，超了就丢最旧的。

import csv
import hashlib
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import path_of
from privacy import scrub_messages, restore, audit

CFG_FILE = path_of("config/model.yaml")
CACHE_FILE = path_of("data/model_cache.json")
USAGE_FILE = path_of("data/model_usage.csv")
DISABLE_FILE = path_of("data/model_disabled.txt")

MAX_CONSECUTIVE_FAIL = 10     # 连续挂这么多次 → 当天停用（写降级文件）
TIMEOUT_SECONDS = 60          # 写长文（招呼语）比分类慢，给够时间
# ★ 2026-10-29 晚补：招呼语链路等不起 60 秒。
#   实测 model_usage.csv 里 evidence_pick 有多条正好卡在 60000ms —— 三次一叠加，
#   一条招呼语能卡 3 分钟，100 条就是 5 小时。所以「选编号」这种一句话的活
#   单独给短超时（正常 2~24 秒，给 20 秒）。
#   连挂 3 次就先歇 2 分钟：既不让整批陪它等，也别歇太久把模型白养着。
PICK_TIMEOUT_SECONDS = 20
COOLDOWN_FAILS = 3
COOLDOWN_SECONDS = 120


def _load_cfg():
    try:
        import yaml
        return yaml.safe_load(open(CFG_FILE, encoding="utf-8")) or {}
    except Exception:
        return {"backend": "none"}


def _disabled_today():
    try:
        return open(DISABLE_FILE, encoding="utf-8").read().strip() == date.today().isoformat()
    except Exception:
        return False


def _set_disabled_today():
    with open(DISABLE_FILE, "w", encoding="utf-8") as f:
        f.write(date.today().isoformat())


def _cache_get(key):
    try:
        d = json.load(open(CACHE_FILE, encoding="utf-8"))
        hit = d.get(key)
        return hit.get("out") if hit else None
    except Exception:
        return None


def _cache_put(key, out):
    try:
        try:
            d = json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            d = {}
        d[key] = {"out": out, "ts": time.time()}
        if len(d) > 2000:                      # 丢最旧的，防文件无限变大
            oldest = sorted(d.items(), key=lambda kv: kv[1].get("ts", 0))[:200]
            for k, _ in oldest:
                d.pop(k, None)
        json.dump(d, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def _log_usage(tag, n_chars, ms, ok):
    try:
        new = not os.path.exists(USAGE_FILE)
        with open(USAGE_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "tag", "chars", "ms", "ok"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), tag, n_chars, ms, ok])
    except Exception:
        pass


class ModelClient:
    def __init__(self):
        self.cfg = _load_cfg()
        self.fails = 0
        self.cooldown_until = 0.0     # 熔断到期时间（time.time()）

    @property
    def backend(self):
        if _disabled_today():
            return "none"
        return (self.cfg.get("backend") or "none").lower()

    def in_cooldown(self):
        return time.time() < self.cooldown_until

    def chat(self, messages, tag="misc", temperature=0.3, max_tokens=500,
             timeout=None, model=None):
        """返回文本；拿不到就返回 None（调用方必须处理 None → 规则版）。

        timeout：单次超时秒数。不传就用 TIMEOUT_SECONDS（写长文用）。
                 只要一个编号这种小活儿，调用方传个短超时，别让发送链路等 60 秒。
        model：可选，临时指定这次调用用哪个模型（覆盖 config 里的默认 model）。
               多 Agent 框架用它来区分「执行者」和「批判者」各用不同模型。
        """
        key = hashlib.md5(json.dumps({"m": messages, "t": temperature},
                                     ensure_ascii=False).encode("utf-8")).hexdigest()
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if self.backend == "none" or _disabled_today():
            return None
        # 连着挂过 → 这段时间内直接不问，免得每条都白等一个超时
        if self.in_cooldown():
            return None

        t0 = time.time()
        out = None
        # ★ 隐私脱敏：发送前把真值换成占位符，模型只看到 <PHONE> 这类代号；
        #   拿回结果再把占位符换回真值。真值自始至终不进模型输入。
        mapping = {}
        try:
            safe_messages, mapping = scrub_messages(messages)
        except Exception:
            safe_messages, mapping = messages, {}
        try:
            if self.backend == "openai":
                out = self._openai(safe_messages, temperature, max_tokens,
                                   timeout=timeout or TIMEOUT_SECONDS, model=model)
        except Exception:
            out = None
        # 恢复真值，并做最后一道闸：结果里绝不允许漏出真值
        try:
            out = restore(out, mapping)
            if out and audit(out, mapping):
                out = None        # 真值漏出来了 → 丢弃，走规则版兜底
        except Exception:
            pass
        ms = int((time.time() - t0) * 1000)

        if out is None:
            self.fails += 1
            if self.fails >= COOLDOWN_FAILS:
                self.cooldown_until = time.time() + COOLDOWN_SECONDS
            if self.fails >= MAX_CONSECUTIVE_FAIL:
                _set_disabled_today()
            _log_usage(tag, len(str(messages)), ms, False)
            return None

        self.fails = 0
        self.cooldown_until = 0.0
        _cache_put(key, out)
        _log_usage(tag, len(str(messages)), ms, True)
        return out

    def _openai(self, messages, temperature, max_tokens, timeout=None, model=None):
        import urllib.request
        # 默认走顶层配置（豆包/火山引擎）
        api_key = self.cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        base = (self.cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")
        model = model or self.cfg.get("model") or "gpt-4o-mini"
        # 若指定了 model 且 models 表里有对应条目，则换用该模型专属的 key / base_url
        # （多 Agent 框架：执行者走豆包、批判者走 DeepSeek，各用各的接口）
        models_cfg = self.cfg.get("models") or {}
        if model in models_cfg:
            entry = models_cfg[model]
            api_key = entry.get("api_key") or api_key
            base = (entry.get("base_url") or base).rstrip("/")
        if not api_key:
            return None
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({"model": model, "messages": messages,
                             "temperature": temperature, "max_tokens": max_tokens},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req,
                                    timeout=timeout or TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")

    def batch_dump(self, requests, name="batch"):
        """把一批不急的活儿写到文件里（前一天晚上跑），第二天再收结果。
        requests: [{id, messages, temperature}]  → data/model_batch_<name>.jsonl
        接了真模型之后，这里就是接 Batch API 的地方（便宜一半）。"""
        p = path_of(f"data/model_batch_{name}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for r in requests:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p


client = ModelClient()
