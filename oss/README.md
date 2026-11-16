# job-hunter-core · 求职助手核心

一套「岗位筛选 + 打分 + 招呼语生成」的可复用方法论库。

帮你把「投简历」这件事里**能自动化、且不该靠感觉**的部分，做成可配置、可验证、不编造的规则。本仓库只包含**通用逻辑层**，不包含任何招聘平台的接口调用或自动化发送代码。

> ⚠️ 本项目定位是「方法论 / 工具库」，不是「一键投递机器人」。请勿用它绕过任何平台的用户协议或风控机制。

---

## 它解决什么问题

海投时最怕两件事：**投错岗位**、**话术编造**。这套代码把这两个问题都拆成了可度量的规则：

| 模块 | 干什么 | 解决什么痛点 |
|---|---|---|
| `scorer.py` | 六维加权打分（岗位匹配/AI核心度/要求契合度/需求拆解/成长空间/性价比） | 不再凭感觉判断"这岗位合不合适" |
| `filters.py` | 分层筛选（硬过滤→岗位族匹配→JD有效性→标题黑名单） | 把明显不合适的岗位先拦掉 |
| `greeting.py` | 六段框架招呼语（称呼→学历→能力→动机→到岗→结尾） | 每条话术结构稳定、不跑偏 |
| `privacy.py` | 发送前脱敏、返回后还原 | 隐私词（姓名/电话/公司）不直接暴露 |
| `evaluate.py` | 回复率台账评测 | 用数据判断哪种话术真的有用 |
| `blocklist.py` | 公司黑名单去重 | 不重复投同一家 |

---

## 快速开始

### 1. 装依赖

```bash
pip install PyYAML
```

> 本项目只用 Python 标准库 + PyYAML，零其他第三方依赖。
> 如果运行时报 `ModuleNotFoundError: No module named 'yaml'`，通常是 pip 装到了别的 Python 环境，换 `python -m pip install PyYAML` 再试。

### 2. 配置（关键一步）

所有规则都写在 `config/*.yaml` 里，改配置不用改代码。仓库里给的是**空模板**（`.example.yaml`），你需要：

```bash
# 把模板复制成真实配置
cp config/target_roles.example.yaml  config/target_roles.yaml
cp config/greeting_spec.example.yaml config/greeting_spec.yaml
cp config/privacy.example.yaml       config/privacy.yaml
cp config/model.example.yaml         config/model.yaml
```

然后打开这些 `.yaml`，把里面所有 `{{占位符}}` 换成你自己的信息：

- `target_roles.yaml` → 你的学校、专业、届别、目标城市、薪资、岗位族关键词
- `greeting_spec.yaml` → 你的招呼语框架、能力证据、项目经历、软技能素材
- `privacy.yaml` → 你的姓名、电话、邮箱、公司名（用于发送前脱敏）
- `model.yaml` → 你接入的大模型 API key（不接模型可留空）

**详细填写说明**见 `docs/配置模板填写说明.md`。

### 3. 跑一个最小例子

> ⚠️ **务必先完成上面第 2 步的配置填写**。如果直接跑、不填配置，程序也能运行，但结果里会出现 `{{占位符}}`（比如岗位族显示成 `{{岗位族ID}}`、招呼语里是 `{{我是某学校...}}`），那不是 bug，是空模板还没填。

```python
import scorer, filters

job = {
    "title": "AI产品经理实习生",
    "jd_text": "负责AI应用产品需求分析与PRD撰写，要求本科在读，有RAG知识库经验优先。",
    "salary": "150元/天",
    "city": "北京",
}

result = filters.layer1(job)          # 第一步：筛选
print("是否通过:", result["passed"])

score = scorer.score_job(job, result)  # 第二步：打分
print("打分:", score["score"], "档位:", score["band"])
```

---

## 核心设计：为什么「防编造」是硬要求

招呼语生成有一条铁律：**能力可以合理迁移论证，但经历和数据绝不编造**。

实现方式是「锚点回溯」——生成的话术里每个数字、项目名、公司名，都必须能在 `resume_anchors` 里找到出处，查无对应就判编造、整条退回。配合 `quality_gate`（质量门）的四道检查（锚点/长度/禁用词/证据核对），保证发出去的每句话都有据可查。

---

## 目录结构

```
.
├── config/                  # 配置（.example.yaml 是空模板）
│   ├── target_roles.example.yaml    # 身份/城市/薪资/岗位族
│   ├── greeting_spec.example.yaml   # 招呼语六段框架+能力证据
│   ├── privacy.example.yaml         # 隐私词表
│   └── model.example.yaml           # 模型 key
├── scorer.py                # 六维打分
├── filters.py               # 分层筛选
├── greeting.py              # 招呼语生成
├── privacy.py               # 脱敏/还原
├── evaluate.py              # 回复率评测
├── blocklist.py             # 黑名单去重
├── model_client.py          # 大模型调用（可选）
└── config_loader.py         # 配置读取入口
```

---

## 免责声明

本项目仅供学习交流。使用本项目产生的任何后果（包括但不限于账号受限、平台封禁）由使用者自行承担。请遵守各招聘平台的用户协议，勿将其用于任何违规用途。

## License

MIT
