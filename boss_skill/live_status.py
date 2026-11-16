# -*- coding: utf-8 -*-
"""
实时状态心跳模块。

后台跑批时，在关键节点调用 touch() 把"当前跑到哪一步、第几条"写进
data/live_status.json，供前端看板定时轮询，实现"实时进度"展示。

设计原则：
  1. 轻量 —— 每次 touch 只写一个小 json，几十字节。
  2. 容错 —— 写失败绝不抛异常、绝不影响主流程（心跳是旁路，不是主线）。
  3. 幂等 —— 多次 touch 只覆盖，不会累积垃圾。

字段说明：
  step      当前步骤英文名（env/follow/accumulate/send/report/eval/idle）
  step_cn   当前步骤中文名
  progress  当前进度：{"done": 已处理条数, "total": 总条数}，无则 None
  company   当前正在处理的公司（发送时用，无则 ""）
  title     当前正在处理的岗位（发送时用，无则 ""）
  running   是否正在跑（bool）
  updated_at 最后更新时间
  msg       一句话状态描述
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "data", "live_status.json")

# 步骤中文名对照（前端也用这一份，改这里前端同步生效）
STEP_CN = {
    "env": "环境检查",
    "follow": "翻账跟进",
    "accumulate": "汇总攒量",
    "fetch": "抓取岗位",
    "pipeline": "打分生成",
    "send": "发送",
    "report": "报告",
    "eval": "评估",
    "idle": "空闲",
}


def touch(step="idle", running=True, done=None, total=None,
          company="", title="", msg=""):
    """写一次心跳。所有参数都可省略，只更新想更新的字段。"""
    data = {
        "step": step,
        "step_cn": STEP_CN.get(step, step),
        "running": running,
        "progress": {"done": done, "total": total} if total else None,
        "company": company or "",
        "title": title or "",
        "msg": msg or "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        # 先确保目录在
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, PATH)  # 原子替换，避免前端读到写一半的文件
    except Exception:
        pass  # 心跳失败不影响主流程


def idle():
    """标记空闲（跑批结束）。"""
    touch(step="idle", running=False, msg="当前空闲")


def read():
    """读当前心跳（前端/调试用）。文件不存在返回空 dict。"""
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


if __name__ == "__main__":
    # 自测
    touch(step="send", done=3, total=20, company="某公司", title="AI产品实习生",
          msg="正在发送")
    print("写入后读取：", read())
    idle()
    print("空闲后读取：", read())
