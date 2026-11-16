# embedding 语义匹配层 —— 火山引擎豆包 embedding + n-gram 兜底
#
# 用途（调研「建议 5」的落地）：给「选经历 / 选块」加一层语义匹配，
# 弥补纯关键词的「同义盲区」—— JD 写「用户痛点」、经历块 skill 写「需求洞察」，
# 关键词匹配不到，但语义上说的是同一件事。
#
# 后端两级：
#   ① 真 embedding：火山引擎方舟 doubao-embedding（需要本人在控制台开通，见下）。
#     向量余弦相似度，能识别「转化率 vs 成交率」这种完全换说法的同义。
#   ② n-gram 替身：中文 2 字 n-gram 的单向覆盖（短 skill 对长 JD），零成本零依赖，
#     只能救「字面仍有重叠」的同义，救不了「完全换说法」。
#   调用方永远先试 ①，① 拿不到（没开通/超时/404）自动降 ②，② 也挑不出才走老逻辑。
#
# 为什么独立一个文件、不塞进 model_client：
#   model_client 管的是「生成文本」（chat），embedding 管的是「算向量」，输入输出、
#   超时、缓存键都不一样，混在一起会互相拖累。而且 embedding 是给「排序」用的，
#   批量、可离线预计算（所有经历块的向量可以提前算好缓存，不用每次现调）。
#
# 开通步骤（本人操作，一次性的）：
#   火山引擎控制台 → 方舟（ARK）→ 开通管理 → 找到「doubao-embedding / 豆包向量化」
#   → 点开通。开通后这里才能调到（否则报 InvalidEndpointOrModel.NotFound）。
#   模型 ID：doubao-embedding-text-240515（文本向量化），也可在控制台创建推理接入点
#   ep-xxx 后用 ep-xxx 当 model。
#
# 用法：
#   from embed_client import embed, sim
#   v = embed("评测意识")          # 返回向量 list 或 None（没开通/超时）
#   s = sim("评测意识", jd_text)   # 返回 0~1 语义相似度（真 embedding 或 n-gram 兜底）

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 配置（从 model.yaml 顶层读 key/base_url，不单独建 embedding 配置）──
# 2026-11-10 实测：本人账号开通的是【多模态向量化】版（vision），不是纯文本版。
#   vision 版走 /embeddings/multimodal 端点，input 是对象列表，返回 2048 维。
#   纯文本版（doubao-embedding-text-240515）走 /embeddings，本账号没开通。
_EMBED_MODEL = os.environ.get("BOSS_EMBED_MODEL", "doubao-embedding-vision-251215")
_EMBED_URL = os.environ.get("BOSS_EMBED_URL",
                            "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal")
_EMBED_TIMEOUT = 8            # 排序用，等不起 model_client 的 60 秒


def _read_cfg():
    try:
        import yaml
        from config_loader import path_of
        cfg = yaml.safe_load(open(path_of("config/model.yaml"), encoding="utf-8")) or {}
        return cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""
    except Exception:
        return os.environ.get("OPENAI_API_KEY") or ""


def _cache_get(key):
    try:
        from config_loader import path_of
        p = path_of("data/embed_cache.json")
        d = json.load(open(p, encoding="utf-8"))
        hit = d.get(key)
        return hit.get("v") if hit else None
    except Exception:
        return None


def _cache_put(key, v):
    try:
        from config_loader import path_of
        p = path_of("data/embed_cache.json")
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            d = {}
        d[key] = {"v": v, "ts": time.time()}
        if len(d) > 2000:
            oldest = sorted(d.items(), key=lambda kv: kv[1].get("ts", 0))[:200]
            for k, _ in oldest:
                d.pop(k, None)
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def embed(text):
    """算一段文本的向量。返回 list[float]，拿不到返回 None（调用方降级 n-gram）。"""
    text = (text or "").strip()
    if not text:
        return None
    key = "v:" + hashlib.md5(text.encode("utf-8")).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached
    api_key = _read_cfg()
    if not api_key:
        return None
    # vision 多模态版：input 是对象列表（每个对象 type=text + text 字段），
    # 不是纯文本版的字符串列表。
    req = urllib.request.Request(
        _EMBED_URL,
        data=json.dumps({"model": _EMBED_MODEL,
                         "input": [{"type": "text", "text": text}]},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # vision 版响应：data 是对象（embedding 在 data.embedding）；
        # 纯文本版响应：data 是数组（embedding 在 data[0].embedding）。两种都兼容。
        dd = data.get("data") or {}
        if isinstance(dd, list):
            v = dd[0].get("embedding") if dd else None
        else:
            v = dd.get("embedding")
        if v:
            _cache_put(key, v)
            return v
    except Exception:
        return None
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


# ── n-gram 替身（真 embedding 拿不到时兜底）──
def _ngrams(s):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "").lower()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _ngram_sim(short, long):
    """短词对长文的单向覆盖分 0~1。见 greeting._sem_sim 的注释（短对长算覆盖，
    不算对称 Jaccard；≤1 个 n-gram 的短词降权 0.5，防「质量/匹配」这种泛词满分）。"""
    sa, sb = _ngrams(short), _ngrams(long)
    if not sa:
        return 0.0
    hit = sum(1 for g in sa if g in sb)
    cov = hit / len(sa)
    if len(sa) == 1:
        return cov * 0.5
    return cov


def sim(short, long):
    """两段文本的语义相似度 0~1。优先真 embedding（余弦），拿不到降 n-gram。"""
    va, vb = embed(short), embed(long)
    if va is not None and vb is not None:
        return _cosine(va, vb)
    return _ngram_sim(short, long)


def prewarm(words):
    """预热：把一批固定的短词（所有经历块的 skill）提前算好向量并缓存。

    日常发送时，每条岗位的 JD 不同（JD 向量没法预缓存，每条要现调 1 次），
    但 14 个 skill 词是固定不变的——提前算好，就不用每条岗位重复调。
    省下的调用量 = (skill 词数 × 岗位数)。冷调用一次约 0.7s，缓存命中 16ms。
    """
    ok = 0
    for w in words:
        w = (w or "").strip()
        if w and embed(w) is not None:
            ok += 1
    return ok


if __name__ == "__main__":
    # 自测入口：python boss_skill/embed_client.py
    # 验证 embedding 是否开通、能拿到向量、语义排序是否合理。
    print("== embedding 自测 ==")
    v = embed("评测意识")
    print("embed('评测意识') ->",
          f"OK dim={len(v)}" if v else "None（未开通或调用失败）")
    jd = "音质评价 主观音质评价体系 听评标准 主客观评价 音频质量"
    print("\n对音频评测 JD 的语义相似度（越高越贴，应该是评测/评估类排前面）：")
    for sk in ["评估体系", "评测意识", "质量", "知识库", "RAG", "流程设计", "匹配"]:
        print(f"  sim({sk}) = {sim(sk, jd):.3f}")
