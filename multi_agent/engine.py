# -*- coding: utf-8 -*-
"""多 Agent 协作框架 · 执行引擎（真跑 + 写日志）

把「执行者 + 批判者 + 人审裁决」三角色流程，落成能真正运行、边跑边写 JSON 日志的引擎。

核心设计（务必保持）：
1. 引擎「模型无关」——不写死 DeepSeek/Step 这些型号，而是通过一个「调用函数」注入模型能力。
   调用函数签名：call_model(model_name, messages, tag) -> str | None
   （返回 None 表示这次调用失败/没接模型，引擎要能兜底。）
2. 角色与流程固定，换领域只读 domain.yaml 的三样东西：外部抓手 / 指标清单 / 规则手册。
3. 引擎每跑一步就 append 一条日志（JSONL），前端看板读这份日志实时展示。
   日志文件：multi_agent/runs/<run_id>.jsonl

用法（命令行）：
    python multi_agent/engine.py --domain job_application --task "读这条JD并给出投递方案" \
        --task-file path/to/task_input.txt
    python multi_agent/engine.py --domain knowledge_research --task "调研xxx" --dry

    --dry：不真调模型，用内置的「假执行者/假批判者」跑一遍，验证流程和日志格式（适合先自测）。

真实运行时，模型能力怎么注入？
    引擎内置一个默认的 call_model：优先读 boss_skill/model_client.py 的 client（已接的模型）。
    要换模型/换接口，改 run() 里的 call_model 参数即可，引擎逻辑一行不动。
"""

import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "boss_skill"))

RUNS_DIR = os.path.join(HERE, "runs")
MAX_CRITIC_ROUNDS = 2        # 批判者硬上限（框架铁律）
PASS_AFTER_CLEAN = 1         # 连续 1 轮无新缺陷 → 通过
EXECUTOR_FIX_TOP_N = 3       # 执行者每轮只改前 N 个最严重的缺陷


# ─────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────

class RunLogger:
    """边跑边写 JSONL，前端看板读它。每行一个事件。"""

    def __init__(self, domain, task):
        os.makedirs(RUNS_DIR, exist_ok=True)
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = os.path.join(RUNS_DIR, self.run_id + ".jsonl")
        self._w(json.dumps({"event": "run_start", "ts": time.time(),
                            "domain": domain, "task": task}, ensure_ascii=False))

    def _w(self, line):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def log(self, event, **kw):
        kw["event"] = event
        kw["ts"] = time.time()
        self._w(json.dumps(kw, ensure_ascii=False))

    def finish(self, summary):
        self.log("run_end", summary=summary)


# ─────────────────────────────────────────────────────────────
# 领域配置加载
# ─────────────────────────────────────────────────────────────

def load_domain(name):
    """读 domains/<name>/domain.yaml，返回三样东西的 dict。"""
    p = os.path.join(HERE, "domains", name, "domain.yaml")
    if not os.path.exists(p):
        raise FileNotFoundError("领域配置不存在：%s" % p)
    import yaml
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────
# 模型调用（可插拔）
# ─────────────────────────────────────────────────────────────

def default_call_model(model_name, messages, tag):
    """默认实现：按角色（executor/critic）从 config/model.yaml 的 multi_agent 段
    读各自该用的模型名，再走 boss_skill/model_client 的 client 调用。

    现状：两个角色都指向同一个豆包模型（手上只有这个 key）。
    以后有第二个模型的 key 时，改 config/model.yaml 的 multi_agent.critic 即可，
    引擎代码一行不动。

    model_name 取值：run() 里调用时传的是 "executor" 或 "critic"，
    这里据此去读配置里对应的真实模型名。"""
    try:
        from model_client import client
        # 读 multi_agent 段里对应角色的模型名；读不到就退回 client 自己的默认 model
        ma_cfg = (client.cfg.get("multi_agent") or {})
        real_model = ma_cfg.get(model_name) or client.cfg.get("model")
        out = client.chat(messages, tag=tag, timeout=60, model=real_model)
        return out
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 两个角色的提示词构建
# ─────────────────────────────────────────────────────────────

def executor_prompt(domain, task):
    anchors = domain.get("external_anchors", [])
    a = "\n".join("- " + x for x in anchors)
    return (
        "你是「执行者」，负责读任务、收集信息、加工比对，产出结构化方案。\n"
        "任务：\n%s\n\n"
        "本领域的外部抓手（这些是「对错」的客观依据，供你判断方案时参考，"
        "不是让你复述一遍核验流程）：\n%s\n\n"
        "硬要求：\n"
        "1. 必须给出【确定的结论】，不要用「先核验再决定」这种悬空说法。"
        "如果信息够判断，就直接下结论（例如「建议投递」或「不建议投递」）。\n"
        "2. 依据要具体、能落到任务内容上，不要空泛复述任务。\n"
        "3. 只负责产出方案，不要自己给自己挑毛病（检查是批判者的事）。\n"
        "必须用下面这个 JSON 结构输出（不要输出任何多余文字，不要输出 JSON 以外的解释）：\n"
        '{"方案":"结论/做法","依据":["依据1","依据2"],'
        '"引用来源":["url/文件/数据"],"自信度":"高/中/低"}'
    ) % (task, a)


def critic_prompt(domain, task, executor_output):
    ca = domain.get("checklist_A_correctness", [])
    cb = domain.get("checklist_B_compliance", [])
    rb = domain.get("rulebook", [])
    a = "\n".join("- " + x for x in ca)
    b = "\n".join("- " + x for x in cb)
    r = "\n".join("- " + x for x in rb)
    return (
        "你是「批判者」，用两套清单挑执行者方案的毛病，输出缺陷列表，不要重写答案。\n"
        "原始任务：%s\n\n"
        "执行者的产出：\n%s\n\n"
        "清单A（查对不对）：\n%s\n\n"
        "清单B（查合不合规）：\n%s\n\n"
        "规则手册：\n%s\n\n"
        "硬规则：\n"
        "1. 只能挑「有依据的毛病」——每条缺陷必须指向「清单/规则手册的某一条」或「某个来源核验结果」。\n"
        "2. 禁止写「感觉不够好」「可以更全面」这类空话。\n"
        "3. 写完缺陷后逐条自检：指不出违反哪条规则的，直接删掉。\n"
        "4. 如果确实挑不出新缺陷，明确输出「无缺陷」。\n"
        "必须用下面这个 JSON 结构输出（不要输出任何多余文字）：\n"
        '{"缺陷":[{"类别":"清单A或清单B","内容":"...","依据":"违反哪条规则/来源"}],'
        '"结论":"有缺陷 或 无缺陷"}'
    ) % (task, executor_output, a, b, r)


# ─────────────────────────────────────────────────────────────
# 解析模型输出为 JSON（容错）
# ─────────────────────────────────────────────────────────────

def parse_json(text):
    """从模型返回的文本里抠出 JSON。模型常会多输出解释文字，这里兜底。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    # 抠第一个 { 到最后一个 }
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────
# 假执行者/假批判者（--dry 用，验证流程与日志格式）
# ─────────────────────────────────────────────────────────────

def _fake_executor(domain, task):
    return {"方案": "（dry 示例方案）建议投递，针对 RAG 能力写简历。",
            "依据": ["JD 要求 RAG，本人剧本杀项目覆盖。",
                     "薪资、城市在目标范围。"],
            "引用来源": ["job_id=xxxx 的 JD 原文", "个人 GitHub 仓库"],
            "自信度": "中"}


def _fake_critic(domain, task, executor_output, round_no):
    if round_no == 1:
        return {"缺陷": [
            {"类别": "清单A", "内容": "引用来源第2条「GitHub 仓库」未给可点击 URL",
             "依据": "违反「来源必须真实可查」"},
            {"类别": "清单B", "内容": "方案缺少到岗字段",
             "依据": "违反规则手册「到岗只写每周5天、6个月以上」"},
        ], "结论": "有缺陷"}
    return {"缺陷": [], "结论": "无缺陷"}


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def run(domain_name, task, call_model=None, dry=False):
    """跑一遍三角色流程。返回 (summary dict, logger)。
    summary 供看板/调用方直接读；logger.path 是 JSONL 日志文件路径。"""
    call_model = call_model or default_call_model
    domain = load_domain(domain_name)
    logger = RunLogger(domain_name, task)

    t0 = time.time()

    # ── 执行者 ──
    logger.log("step_start", role="执行者")
    if dry:
        ex_out = _fake_executor(domain, task)
    else:
        raw = call_model("executor", [{"role": "user",
                                        "content": executor_prompt(domain, task)}],
                         tag="ma_executor")
        ex_out = parse_json(raw) or {"方案": "(模型未返回有效结果)",
                                      "依据": [], "引用来源": [], "自信度": "低"}
    logger.log("executor_output", role="执行者", output=ex_out,
               elapsed=int((time.time() - t0) * 1000))

    # ── 批判者循环（最多 2 轮） ──
    critic_rounds = 0
    all_defects = []
    executor_final = ex_out
    passed = False

    for rnd in range(1, MAX_CRITIC_ROUNDS + 1):
        critic_rounds = rnd
        logger.log("step_start", role="批判者", round=rnd)
        t_r = time.time()
        if dry:
            cr_out = _fake_critic(domain, task, executor_final, rnd)
        else:
            raw = call_model("critic",
                             [{"role": "user",
                               "content": critic_prompt(domain, task,
                                                       json.dumps(executor_final,
                                                                  ensure_ascii=False))}],
                             tag="ma_critic")
            cr_out = parse_json(raw) or {"缺陷": [], "结论": "无缺陷"}
        defects = cr_out.get("缺陷", []) or []
        all_defects.extend(defects)
        logger.log("critic_output", role="批判者", round=rnd,
                   defects=defects, conclusion=cr_out.get("结论"),
                   elapsed=int((time.time() - t_r) * 1000))

        if not defects:
            passed = True
            logger.log("critic_pass", round=rnd, reason="连续1轮无新缺陷")
            break

        # 有缺陷 → 打回执行者改（只改前 N 个最严重的）
        logger.log("executor_fix", round=rnd, fix_count=min(len(defects), EXECUTOR_FIX_TOP_N))
        if not dry:
            # 真实模式下，把缺陷回传给执行者让它改
            fix_prompt = (
                "批判者指出你的方案有以下缺陷（按严重度排），只改前 %d 个最严重的：\n%s\n"
                "重新输出修正后的 JSON 方案（结构不变）。"
            ) % (EXECUTOR_FIX_TOP_N,
                 "\n".join("- " + d.get("内容", "") for d in defects[:EXECUTOR_FIX_TOP_N]))
            raw = call_model("executor", [{"role": "user",
                                           "content": fix_prompt}], tag="ma_executor_fix")
            executor_final = parse_json(raw) or executor_final
        else:
            executor_final = dict(executor_final)
            executor_final["方案"] = executor_final.get("方案", "") + "（已按缺陷修正）"
        logger.log("executor_output", role="执行者", round=rnd,
                   output=executor_final, is_fix=True)

    # ── 汇总 ──
    summary = {
        "run_id": logger.run_id,
        "domain": domain_name,
        "task": task,
        "critic_rounds": critic_rounds,
        "total_defects": len(all_defects),
        "passed": passed,
        "final_output": executor_final,
        "all_defects": all_defects,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    logger.finish(summary)
    return summary, logger


def main():
    ap = argparse.ArgumentParser(description="多 Agent 协作框架执行引擎")
    ap.add_argument("--domain", required=True, help="领域名（domains/ 下的目录名）")
    ap.add_argument("--task", required=True, help="任务描述")
    ap.add_argument("--task-file", help="从文件读任务（优先于 --task）")
    ap.add_argument("--dry", action="store_true", help="用假执行者/假批判者跑，验证流程")
    args = ap.parse_args()

    task = args.task
    if args.task_file and os.path.exists(args.task_file):
        task = open(args.task_file, encoding="utf-8").read().strip()

    summary, logger = run(args.domain, task, dry=args.dry)

    print()
    print("  ===== 多 Agent 运行完成 =====")
    print("  领域：%s" % summary["domain"])
    print("  批判者轮数：%d / %d" % (summary["critic_rounds"], MAX_CRITIC_ROUNDS))
    print("  挑出缺陷：%d 条" % summary["total_defects"])
    print("  是否通过：%s" % ("是" if summary["passed"] else "否"))
    print("  最终方案：%s" % str(summary["final_output"].get("方案", ""))[:80])
    print("  日志文件：%s" % logger.path)
    print()


if __name__ == "__main__":
    main()
