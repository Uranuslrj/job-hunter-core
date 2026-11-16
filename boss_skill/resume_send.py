# -*- coding: utf-8 -*-
"""
定制简历 → 上传附件 → 发送给 HR（Boss 直聘附件简历链路）

背景：识别到「HR 要简历」后，系统按 JD 定制简历 PDF，上传到 Boss 直聘
「附件管理」，再发送给对应 HR。上传/发送走人工确认闸门（红线，不可绕过）。

用法：
  python resume_send.py build  --job "AI产品实习生" --company "某某公司" --type intern
      定制简历 PDF（套母版 + 清单规则 + 岗位切换），输出到 resumes/ 目录并跑验收

  python resume_send.py upload --pdf <PDF路径>
      上传 PDF 到 Boss 直聘附件管理（需 chrome-use 已登录，人工确认后执行）

  python resume_send.py send --job-id <encryptJobId>
      在聊天会话里给指定 HR 发送附件简历（待补：聊天发附件的选择器）

依赖：
  - 母版：resume/简历母版-v1.html
  - 清单：resume/简历规则清单.md（唯一真相源）
  - 验收：resume/check_resume.py
  - 驱动：plugin_driver（chrome-use 插件，须 BOSS_DRIVER=plugin）

真实选择器来源：docs/附件简历上传-真页面探测-20261114.md
"""

import argparse
import base64
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin_driver as PD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESUME_DIR = os.path.join(ROOT, "resume")
MASTER_HTML = os.path.join(RESUME_DIR, "简历母版-v1.html")
CHECK_SCRIPT = os.path.join(RESUME_DIR, "check_resume.py")
OUT_DIR = os.path.join(ROOT, "resumes")   # 定制简历 PDF 输出目录

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 附件管理页 + 上传控件（真页面探测 2026-11-14）
RESUME_URL = "https://www.zhipin.com/web/geek/resume"
UPLOAD_BTN_SEL = "a.btn.btn-primary.btn-file"
UPLOAD_INPUT_SEL = "a.btn.btn-primary.btn-file input[type=file]"
UPLOAD_ENTRY_SEL = "a.header-resume-upload"

# 附件栏上限（真页面探测：文件（N/3））
ATTACH_MAX = 3

# 附件数量计数：div.resume-type-title「文件（1/3）」
COUNT_JS = r"""
(function(){
  var el = document.querySelector('div.resume-type-title');
  if(!el) return JSON.stringify({err:'NO_COUNT'});
  var t = (el.innerText||'').trim();
  var m = t.match(/(\d+)\s*\/\s*(\d+)/);
  if(!m) return JSON.stringify({err:'NO_MATCH', text:t});
  return JSON.stringify({cur:parseInt(m[1]), max:parseInt(m[2]), text:t});
})()
"""

# 删除指定附件：调 SiderResume 组件的 handleDelResume + 执行确认回调
# （不要点三点菜单/li.annex-operate-delete——那是 hover 菜单，合成事件点不到，
#   且坐标点击会误触跳转到 cv.zhipin.com/smart-resume）
DELETE_RESUME_JS = r"""
(async function(){
  var rid = "__RESUME_ID__";
  var item = document.querySelector('div.annex-item');
  if(!item) return JSON.stringify({ok:false, err:'NO_ITEM'});
  var node = item;
  while(node && !node.__vue__){ node = node.parentElement; }
  if(!node || !node.__vue__) return JSON.stringify({ok:false, err:'NO_VUE'});
  var vm = node.__vue__;

  // 拦 $dialog 拿 onConfirm，不真弹 UI（直接执行确认回调 = 点"确认"按钮）
  var onConfirm = null;
  var origDialog = vm.$dialog;
  vm.$dialog = function(opts){
    if(opts && opts.onConfirm) onConfirm = opts.onConfirm;
    return;
  };

  vm.handleDelResume(rid, 0);
  await new Promise(function(r){ setTimeout(r, 500); });

  if(!onConfirm) return JSON.stringify({ok:false, err:'NO_CONFIRM'});
  onConfirm();
  await new Promise(function(r){ setTimeout(r, 3500); });

  // 读删除后的附件数
  var after = (vm.resumeInfo && vm.resumeInfo.attachmentList) ? vm.resumeInfo.attachmentList.length : -1;
  return JSON.stringify({ok:true, attachmentAfter: after});
})()
"""


# ─────────────────────────────────────────────────────────────
# 上传附件简历（真·打通方案：调页面 Vue 组件方法）
# ─────────────────────────────────────────────────────────────
#
# 为什么不能用 chrome-use 的 upload 命令 / JS 塞 input 触发 change：
#   Boss 的上传组件（upload-resume，Vue）不认「合成文件事件」——它只认真人
#   点出来的系统文件对话框。chrome-use upload 和 DataTransfer 塞 input.files
#   都会被组件忽略（input.files 赋完即被清空），文件收不到。
#
# 真正能打通的姿势（2026-11-14 实测成功）：
#   找到页面上 upload-resume 组件的 Vue 实例（挂在 DOM 节点的 __vue__ 上），
#   用 JS 把 PDF 还原成 File → FormData，直接调组件自己的两个方法：
#     vm.uploadResume(fd)  → 走 Boss 自己的上传逻辑，拿到 previewUrl
#     vm.confirmAdd()      → 走 Boss 自己的保存逻辑，写进附件栏
#   全程是 Boss 自己的代码在发请求（自动带对 cookie / __zp_stoken__ / 请求头），
#   浏览器层面看就是一个正常用户在操作，不碰接口、不逆向、不模拟事件。
#
# 文件内容从 Python 侧读成 base64，塞进 JS 的 __B64__ 占位符。

# 上传组件找到 Vue 实例的锚点：上传按钮（附件管理面板 / 发简历弹窗共用此类）
UPLOAD_BTN_SEL = "a.btn.btn-primary.btn-file"

# 调组件 uploadResume + 保存/发送 的完整 JS（一次完成上传并保存）
# 2026-11-16 修：附件管理场景调 confirmAdd()（保存进附件栏）；
# 发简历场景调 confirmAddAction()（确认并发送给当前 HR）。之前一律 confirmAdd，
# 导致发简历只保存没发出。动作名通过 __ACTION__ 占位符注入。
UPLOAD_COMPONENT_JS = r"""
(async function(){
  var b64 = "__B64__";
  var fname = "__FNAME__";
  var actionName = "__ACTION__";

  var bin = atob(b64);
  var bytes = new Uint8Array(bin.length);
  for(var i=0;i<bin.length;i++){ bytes[i] = bin.charCodeAt(i); }
  var file = new File([bytes], fname, {type:'application/pdf'});
  var fd = new FormData();
  fd.append('file', file, fname);
  fd.append('fileType', '1');

  // 找 upload-resume 组件的 Vue 实例（从上传按钮往上找到带 __vue__ 的节点）
  var btn = document.querySelector('__BTN_SEL__');
  if(!btn) return JSON.stringify({ok:false, err:'NO_BTN'});
  var node = btn;
  while(node && !node.__vue__){ node = node.parentElement; }
  if(!node || !node.__vue__) return JSON.stringify({ok:false, err:'NO_VUE'});
  var vm = node.__vue__;

  // 调组件自己的上传方法（Boss 自己发请求，带对 header/stoken）
  vm.uploadResume(fd);

  // 等上传完成（最多 15 秒）
  var waited = 0;
  while(vm.uploadStatus !== 'uploadSuccess' && vm.uploadStatus !== 'uploadFail' && waited < 15000){
    await new Promise(function(r){ setTimeout(r, 500); });
    waited += 500;
  }
  if(vm.uploadStatus !== 'uploadSuccess'){
    return JSON.stringify({ok:false, err:'UPLOAD_FAIL', status:vm.uploadStatus});
  }
  var previewUrl = vm.previewUrl;

  // 调保存/发送方法（confirmAdd=保存进附件栏；confirmAddAction=发简历确认并发送）
  if (typeof vm[actionName] !== 'function') {
    return JSON.stringify({ok:false, err:'NO_METHOD', method:actionName});
  }
  vm[actionName]();
  await new Promise(function(r){ setTimeout(r, 2500); });

  return JSON.stringify({ok:true, previewUrl:previewUrl, fileName:file.name, fileSize:file.size, action:actionName});
})()
"""


def _upload_via_component(pdf_path, btn_sel=UPLOAD_BTN_SEL, timeout=90, action="confirmAdd"):
    """调页面 upload-resume 组件的 Vue 实例方法，上传并保存/发送 PDF。

    pdf_path：本机 PDF 绝对路径
    action：上传后调用的组件方法名。附件管理=confirmAdd（保存进附件栏）；
            发简历=confirmAddAction（确认并发送给当前 HR）。
    返回 (ok: bool, detail: dict)
    """
    if not os.path.exists(pdf_path):
        return False, {"err": "文件不存在", "path": pdf_path}

    with open(pdf_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    fname = os.path.basename(pdf_path)

    js = (UPLOAD_COMPONENT_JS
          .replace("__B64__", b64)
          .replace("__FNAME__", fname)
          .replace("__BTN_SEL__", btn_sel)
          .replace("__ACTION__", action))

    r = PD.eval_json(js, timeout=timeout)
    return bool(r.get("ok")), r


# ─────────────────────────────────────────────────────────────
# 1. 定制简历 PDF
# ─────────────────────────────────────────────────────────────

def build_resume(job_title, company, job_type="intern", city=""):
    """按 JD 定制简历：改母版 → 生成 PDF → 跑验收。返回 PDF 路径或 None。

    job_title：岗位名（写进简历顶部「求职方向」和文件名）
    company：公司名（写进文件名，用于发简历时在「选择简历」列表里区分是哪家公司）
    job_type：intern=实习岗 / formal=正式岗（决定到岗句写法）
    city：岗位所在城市（写进「目标地点」；不传则保留母版默认「北京」）
    """
    os.makedirs(OUT_DIR, exist_ok=True)

    # 文件名：{姓名}-{公司}-{岗位}.pdf（本人 2026-11-16 拍板：加回公司名，
    # 因为发简历时 BOSS 弹「请选择要发送的简历」列表，不带公司名分不清是哪份）。
    # 姓名从环境变量 RESUME_NAME 读，开源版不硬编码真名。
    owner = os.environ.get("RESUME_NAME", "我的简历")
    safe_job = job_title.replace("/", " ").replace("\\", " ").strip()
    safe_company = (company or "").replace("/", " ").replace("\\", " ").strip()
    if safe_company:
        fname = f"{owner}-{safe_company}-{safe_job}".strip()
    else:
        fname = f"{owner}-{safe_job}".strip()
    pdf_path = os.path.join(OUT_DIR, fname + ".pdf")

    # 读母版，做岗位相关的替换：求职方向 + 目标地点（含到岗句）
    html = open(MASTER_HTML, encoding="utf-8").read()

    # 求职方向行（母版里是「AI产品实习生 ｜ 酒店Agent推荐策略」）
    html = _replace_role(html, job_title)

    # 目标地点 + 到岗句：目标地点跟岗位城市走
    target_city = city.strip() if city else "北京"
    if job_type == "intern":
        onset = f'目标地点：{target_city} ｜ 每周5天 ｜ 可实习<b>6个月以上，可随时到岗</b>'
    else:
        onset = f'目标地点：{target_city}'
    html = _replace_onset(html, onset)

    # 照片 src 改成绝对路径（临时 HTML 输出到 resumes/，相对路径会断）
    photo_abs = os.path.join(RESUME_DIR, "photo.jpg").replace(os.sep, "/")
    html = html.replace('src="photo.jpg"', f'src="file:///{photo_abs}"')

    # 写临时 HTML（不污染母版）
    tmp_html = os.path.join(OUT_DIR, fname + ".html")
    open(tmp_html, "w", encoding="utf-8").write(html)

    # Edge headless 打印 PDF
    ok = _edge_print(tmp_html, pdf_path)
    if not ok:
        print("✗ PDF 生成失败")
        return None

    # 跑验收（针对临时 HTML，用对应岗位类型口径）
    r = subprocess.run(
        [sys.executable, CHECK_SCRIPT, tmp_html, job_type],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(r.stdout)
    if r.returncode != 0:
        print("✗ 验收未通过，不交付。请检查上面 [×] 项。")
        return None

    print(f"✓ 定制简历完成：{pdf_path}")
    return pdf_path


def _replace_role(html, job_title):
    """替换求职方向行。母版默认「AI产品实习生 ｜ 酒店Agent推荐策略」。"""
    import re
    # 求职方向在 .role 这个 div 里
    m = re.search(r'(<div class="role">)([^<]*)(</div>)', html)
    if m:
        new_role = job_title
        return html[:m.start(2)] + new_role + html[m.end(2):]
    return html


def _replace_onset(html, onset):
    """替换目标地点行（到岗句）。"""
    import re
    # 目标地点行在第一个含「目标地点」的 .line div，中间可能有 <b> 标签，用非贪婪跨标签匹配
    m = re.search(r'<div class="line">目标地点.*?</div>', html, re.S)
    if m:
        return html[:m.start()] + f'<div class="line">{onset}</div>' + html[m.end():]
    return html


def _edge_print(html_path, pdf_path):
    """Edge headless 打印 HTML 为 PDF。"""
    try:
        r = subprocess.run(
            [EDGE, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f'--print-to-pdf={pdf_path}', f"file:///{html_path.replace(os.sep, '/')}"],
            capture_output=True, text=True, timeout=120,
        )
        return os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 10000
    except Exception as e:
        print(f"Edge 打印失败：{e}")
        return False


# ─────────────────────────────────────────────────────────────
# 2. 上传附件简历到附件管理
# ─────────────────────────────────────────────────────────────

def _get_attach_count():
    """读附件栏当前份数。返回 dict(cur, max, text) 或 None。"""
    r = PD.eval_json(COUNT_JS)
    if isinstance(r, dict) and "cur" in r:
        return r
    return None


# 读附件列表（resumeId + 文件名），供删除/判满用
LIST_ATTACH_JS = r"""
(function(){
  var item = document.querySelector('div.annex-item');
  if(!item) return JSON.stringify({err:'NO_ITEM'});
  var node = item;
  while(node && !node.__vue__){ node = node.parentElement; }
  if(!node || !node.__vue__) return JSON.stringify({err:'NO_VUE'});
  var vm = node.__vue__;
  var list = (vm.resumeInfo && vm.resumeInfo.attachmentList) || [];
  return JSON.stringify(list.map(function(a){
    return {resumeId:a.resumeId, showName:a.showName, sizeDesc:a.resumeSizeDesc};
  }));
})()
"""


def _list_attachments():
    """读附件栏所有附件的 (resumeId, 文件名)。返回 list 或 None。"""
    r = PD.eval_json(LIST_ATTACH_JS)
    if isinstance(r, list):
        return r
    return None


def _delete_attachment(resume_id):
    """按 resumeId 删除一份附件（调组件 handleDelResume + 确认回调）。返回 (ok, detail)。"""
    js = DELETE_RESUME_JS.replace("__RESUME_ID__", resume_id)
    r = PD.eval_json(js, timeout=60)
    return bool(r.get("ok")), r


def _delete_first_attachment():
    """删除附件栏最旧的一份。返回 True/False。

    注意：BOSS 附件列表 attachmentList 是「最新在前、最旧在后」排序
    （2026-11-16 实测：lst[0]=刚上传的最新一份，lst[-1]=最旧）。
    所以「最旧」要删 lst[-1]，不是 lst[0]（之前删 lst[0] 会把刚上传的
    简历删掉，陷入「上传→删自己刚传的」死循环）。
    """
    lst = _list_attachments()
    if not lst:
        print("✗ 读不到附件列表，无法删除。")
        return False
    oldest = lst[-1]
    print(f"准备删除最旧附件：{oldest.get('showName')}")
    ok, d = _delete_attachment(oldest.get("resumeId"))
    if not ok:
        print(f"✗ 删除失败：{d}")
        return False
    time.sleep(2)
    print("✓ 已删除最旧的一份附件")
    return True


def upload_resume(pdf_path, confirm=True):
    """把 PDF 上传到 Boss 直聘附件管理（自动判断满没满，满了先删最旧的）。

    流程：
      1. 导航到简历页，读附件栏当前份数
      2. 若已满（cur >= max），先删除最旧一份（人工确认）
      3. 上传 PDF（chrome-use upload 命令，不模拟对话框）
      4. 上传后 BOSS 会跳 resumeAnalyze，重新导航回简历页

    confirm=True 时，删附件/上传前都打印动作要求人工确认（红线）。
    """
    if not os.path.exists(pdf_path):
        print(f"✗ 文件不存在：{pdf_path}")
        return False

    # 确认在 Boss 简历页（附件面板只在 /web/geek/resume 有）
    url = PD.current_url() or ""
    if "/web/geek/resume" not in url:
        print(f"当前不在简历页（{url}），先导航过去…")
        PD.open_url(RESUME_URL)
        time.sleep(4)

    # 读当前份数
    cnt = _get_attach_count()
    if cnt is None:
        print("⚠ 读不到附件栏计数（可能页面未加载或不在简历页），按「未满」继续尝试上传。")
    else:
        print(f"当前附件栏：{cnt.get('text', '?')}（上限 {cnt.get('max', ATTACH_MAX)}）")

    # 判断满没满
    need_delete = bool(cnt and cnt.get("cur", 0) >= cnt.get("max", ATTACH_MAX))
    if need_delete:
        print("⚠ 附件栏已满，需要先删除最旧的一份再上传。")
        if confirm:
            print("=" * 50)
            print("即将删除附件栏最旧的一份简历，然后上传新简历。")
            print("删除不可撤回，确认继续吗？(y/n)")
            print("=" * 50)
            ans = input("> ").strip().lower()
            if ans not in ("y", "yes"):
                print("已取消。")
                return False
        if not _delete_first_attachment():
            print("✗ 删除失败，中止上传。")
            return False
        # 删除后回到简历页（删除动作可能也触发跳转）
        PD.open_url(RESUME_URL)
        time.sleep(3)

    # 人工确认上传（红线）
    if confirm:
        print("=" * 50)
        print(f"即将上传附件简历：\n  {pdf_path}")
        print("确认继续吗？(y/n)")
        print("=" * 50)
        ans = input("> ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return False

    # 调页面组件方法上传（真·打通方案，见 UPLOAD_COMPONENT_JS 注释）
    ok, d = _upload_via_component(pdf_path)
    if not ok:
        print("✗ 上传失败：", d)
        return False
    print(f"✓ 已上传并保存：{d.get('fileName')}（{d.get('fileSize')} 字节）")

    # 上传后 BOSS 会跳到 resumeAnalyze，重新导航回简历页（便于后续操作）
    time.sleep(3)
    PD.open_url(RESUME_URL)
    return True


# ─────────────────────────────────────────────────────────────
# 3. 发送给 HR（发简历链路，真页面探测 2026-11-14）
# ─────────────────────────────────────────────────────────────

# 会话列表 / 打开会话的三段 JS（从 send_cdp.py 原样搬过来，换插件驱动的 PD.eval_js 执行）。
# 逻辑层不变，只把 F.evaluate 换成 PD.eval_js。
JS_CONV_ITEMS = r"""
(async () => {
  // BOSS 会话列表是懒加载/虚拟滚动：初始只渲染视口内的一小段 li，
  // 往下滚才加载更多。不滚就 read，靠后的会话根本不在 DOM 里，
  // 导致「列表里找不到这家」误判（2026-11-16 实测：24 家后半段 12 家全漏）。
  // 这里先反复滚到底，直到 li 数量不再增长，再读全量列表。
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const findScroller = () => {
    // 优先 .user-list 本身；若它不可滚，找它的可滚动父容器
    const ul = document.querySelector('div.user-list');
    const cand = [];
    if (ul) cand.push(ul);
    let p = ul ? ul.parentElement : null;
    while (p && p !== document.body) { cand.push(p); p = p.parentElement; }
    for (const el of cand) {
      if (el.scrollHeight > el.clientHeight + 10) return el;
    }
    return ul || document.scrollingElement || document.documentElement;
  };
  const scroller = findScroller();
  let prev = -1;
  for (let round = 0; round < 60; round++) {
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(120);
    const n = document.querySelectorAll('div.user-list li').length;
    if (n === prev) {
      // 连续两轮数量没变，说明滚到底了
      if (round > 2) break;
    }
    prev = n;
  }
  const arr = [];
  document.querySelectorAll('div.user-list li').forEach((e, i) => {
    arr.push([i, (e.innerText || '').replace(/[\r\n]+/g, ' ').slice(0, 140)]);
  });
  return JSON.stringify(arr);
})()
"""

JS_CLICK_ITEM = r"""
(() => {
  const list = document.querySelectorAll('div.user-list li');
  const el = list[__IDX__];
  if (!el) return 'NO_ITEM';
  const target = el.querySelector('.friend-content') || el;
  target.scrollIntoView({block: 'center'});
  target.click();
  return 'CLICKED';
})()
"""

# 单步：滚一段 + 检查目标 + 命中即点。会话列表是虚拟滚动（2026-11-16 实测），
# 静态读 li 只能拿到视口附近约 66 条。真正的滚动容器是 div.user-list-content。
# 这个短 JS 每次只滚一段、查一次，单次 <2s，避免长循环触发 CDP relay 8s 超时。
# 由 _open_chat_by_match 在 Python 侧循环调用，逐段往下扫直到命中或到底。
# 返回：'CLICKED:<文本>' 命中并已点 / 'NOT_FOUND' 本段没命中 / 'BOTTOM' 到底了
#       / 'NO_LIST' 没有列表。
JS_SCROLL_STEP = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const NEEDS = __NEEDS__;
  const KEY = __KEY__;
  const POS = __POS__;   // 要滚到的 scrollTop（由 Python 累加）
  const ulc = document.querySelector('div.user-list-content');
  if (!ulc) return 'NO_LIST';
  const textOf = (li) => (li.innerText || '').replace(/\r?\n/g, ' ').trim();
  const match = (t) => {
    for (const need of [NEEDS, NEEDS.slice(0,2), NEEDS.slice(0,1)]) {
      const keys = need.filter(x => x && x.length >= 2);
      if (keys.length && keys.every(k => t.includes(k))) return true;
    }
    return (KEY && t.includes(KEY));
  };
  ulc.scrollTop = POS;
  await sleep(250);
  // 先看当前渲染的 li 有没有命中
  for (const li of document.querySelectorAll('div.user-list li')) {
    const t = textOf(li);
    if (t && match(t)) {
      const target = li.querySelector('.friend-content') || li;
      target.scrollIntoView({block:'center'});
      target.click();
      return 'CLICKED:' + t.slice(0,80);
    }
  }
  // 判定是否到底（scrollTop 不再能往下 + 出现「没有更多了」或 scrollHeight 稳定）
  const atBottom = ulc.scrollTop + ulc.clientHeight >= ulc.scrollHeight - 5;
  return atBottom ? 'BOTTOM' : 'NOT_FOUND';
})()
"""

JS_CONV_READ = r"""
(() => {
  const conv = document.querySelector('.chat-conversation');
  if (!conv) return null;
  const heads = [];
  ['.name-text', '.base-title', '.position-name', '.base-info']
    .forEach(s => conv.querySelectorAll(s).forEach(e => {
      const t = (e.innerText || '').trim();
      if (t && t.length < 60 && heads.indexOf(t) < 0) heads.push(t);
    }));
  const bar = conv.querySelector('.base-info') || conv;
  const first = bar.querySelectorAll('span');
  if (first.length >= 2) {
    const maybe = (first[1].innerText || '').trim();
    if (maybe && maybe.length < 40 && heads.indexOf(maybe) < 0) heads.push(maybe);
  }
  const msgs = [];
  conv.querySelectorAll('.text-content').forEach(e => {
    const t = (e.innerText || '').trim();
    if (t) msgs.push(t.slice(0, 300));
  });
  const inp = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'))
    .find(e => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 20; });
  return JSON.stringify({heads: heads.slice(0, 8),
                         last: msgs.length ? msgs[msgs.length - 1] : null,
                         n: msgs.length,
                         has_input: !!inp});
})()
"""


def _open_chat_by_match(boss="", company="", job_title=""):
    """在会话列表里按 HR 名+公司名+岗位名 打开目标会话（插件驱动）。

    返回 (True, 说明) / (False, 原因)。点完回读抬头核对，点错当场停。
    2026-11-16 改：会话列表是虚拟滚动，静态读 li 只能拿到视口附近约 66 条，
    靠后的会话（含今天刚聊过的）会漏。改成「滚动逐屏找 + 命中即点」，
    用 JS_SCROLL_FIND_CLICK 一个连贯 JS 内完成，避免多次 eval 往返导致
    页面状态漂移。
    """
    import json as _json
    boss = (boss or "").strip()
    comp = (company or "").strip()
    jt = (job_title or "").strip()

    # 先把会话列表滚到顶（从最上往下找，覆盖最新会话）
    PD.eval_js("(function(){var u=document.querySelector('div.user-list-content');if(u)u.scrollTop=0;return 'ok';})()", timeout=20)

    needs = [x for x in (boss, comp, jt) if x and len(x) >= 2]
    key = comp[:6] if len(comp) >= 6 else ""

    # Python 侧循环：逐段滚动，每段一个短 JS（<2s），直到命中或到底。
    # 步长取视口高度的 2/3，保证相邻两段重叠、不漏。上限 40 段（覆盖长列表）。
    pos = 0
    hit_text = None
    for _ in range(40):
        js = (JS_SCROLL_STEP
              .replace("__NEEDS__", _json.dumps(needs))
              .replace("__KEY__", _json.dumps(key))
              .replace("__POS__", str(pos)))
        r = PD.eval_js(js, timeout=15)
        if r is None:
            # 单次失败重试一次（可能是页面瞬时状态）
            r = PD.eval_js(js, timeout=15)
        if r == "NO_LIST":
            return False, "读不到会话列表（可能不在聊天页）"
        if r and str(r).startswith("CLICKED:"):
            hit_text = str(r)[len("CLICKED:"):]
            break
        if r == "BOTTOM":
            break
        # 没命中、没到底：往下滚一段（步长 500，等下次循环读取最新 scrollHeight 后自适应）
        pos += 500
    if hit_text is None:
        return False, f"列表里找不到这家（{boss}｜{comp}）"
    how = "滚动命中"

    time.sleep(3.0)

    raw2 = PD.eval_js(JS_CONV_READ, timeout=30)
    try:
        info = _json.loads(raw2 or "{}")
    except Exception:
        info = {}
    if not info:
        return False, "点开之后读不到聊天区"
    heads = info.get("heads") or []
    joined = " ".join(heads)
    if boss and boss not in joined:
        return False, f"点开的不是这个人（抬头「{joined[:30]}」，找「{boss}」）"
    if jt and len(jt) >= 3 and not (jt in joined or jt[:4] in joined):
        return False, f"点开的不是这个岗位（抬头「{joined[:30]}」，找「{jt}」）"
    if not info.get("has_input"):
        return False, "点开了但没有输入框，不敢发"
    return True, f"打开成功（{how}；命中「{hit_text[:24]}」；抬头：{joined[:24]}）"


# 聊天页「发简历」按钮：div.chat-controls 内 text="发简历" 的 toolbar-btn-content
SEND_RESUME_BTN_JS = r"""
(function(){
  var cc = document.querySelector('div.chat-controls');
  if(!cc) return 'NO_CHAT_CONTROLS';
  var kids = cc.children;
  for(var i=0;i<kids.length;i++){
    if((kids[i].innerText||'').trim()==='发简历'){ return 'FOUND'; }
  }
  return 'NOT_FOUND';
})()
"""

SEND_RESUME_CLICK_JS = r"""
(function(){
  var cc = document.querySelector('div.chat-controls');
  if(!cc) return 'NO_CHAT_CONTROLS';
  var kids = cc.children;
  for(var i=0;i<kids.length;i++){
    if((kids[i].innerText||'').trim()==='发简历'){
      kids[i].click();
      return 'CLICKED';
    }
  }
  return 'NOT_FOUND';
})()
"""

# 弹窗里的上传控件（发简历弹窗与附件管理弹窗共用这个类）
DIALOG_UPLOAD_INPUT_SEL = "a.btn.btn-primary.btn-file input[type=file]"


def send_resume(job_id, pdf_path=None, confirm=True):
    """在聊天会话里给 HR 发附件简历。

    前提：浏览器已停在目标会话的聊天页（由调用方/人工先打开好会话）。
    动作链：点「发简历」→ 弹窗 → 上传本地 PDF → 自动发出。

    job_id：仅用于日志标识，真正动作靠当前打开的会话。
    pdf_path：要发的 PDF；不传则报错（必须先 build 出简历）。
    confirm：True 时发送前要求人工确认（红线）；flow 里已确认过可传 False。
    """
    if not pdf_path or not os.path.exists(pdf_path):
        print("✗ 必须先 build 出简历 PDF，并传入 --pdf 路径。")
        return False

    # 确认在聊天页
    url = PD.current_url() or ""
    if "/web/geek/chat" not in url:
        print(f"✗ 当前不在聊天页（{url}），请先打开目标会话。")
        return False

    # 人工确认（红线）
    if confirm:
        print("=" * 50)
        print(f"即将给当前会话发送附件简历：\n  {pdf_path}")
        print("发送不可撤回，确认继续吗？(y/n)")
        print("=" * 50)
        ans = input("> ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return False

    # 1. 点「发简历」按钮
    r = PD.eval_js(SEND_RESUME_CLICK_JS)
    print(f"点发简历按钮结果：{r}")
    if r != "CLICKED":
        print("✗ 没点到「发简历」按钮，中止。")
        return False
    time.sleep(2)

    # 2. 弹窗出现后，调组件方法上传 PDF 并发送（真·打通方案）
    #    发简历场景：uploadResume + confirmAddAction（确认并发送给当前 HR）
    ok, d = _upload_via_component(pdf_path, action="confirmAddAction")
    if not ok:
        print("✗ 上传失败：", d)
        return False
    print(f"✓ 已上传：{d.get('fileName')}，已触发发送（{d.get('action')}）…")

    print("✓ 附件简历已发送（上传后走 confirmAddAction 确认发送）")
    return True


# ─────────────────────────────────────────────────────────────
# 4. 一键流程：生成 → 判空间 → 删旧 → 上传 → 投递
# ─────────────────────────────────────────────────────────────

def flow(job_title, company, job_type="intern", city="", boss="", job_id="",
         no_confirm=False):
    """端到端一键：定制简历 → 判附件空间 → 满则删旧 → 上传 → 打开会话投递。

    步骤：
      1. build_resume 生成定制 PDF（跑验收，不过验收即停）
      2. 导航简历页，读附件栏计数 cur/max（判空间）
      3. cur >= max（3 份满）→ 删最旧一份（人工确认闸门，no_confirm 跳过）
      4. _upload_via_component 上传新 PDF 到附件栏
      5. 打开目标 HR 会话（boss/company/job_title 匹配），点「发简历」投递
    """
    print("=" * 60)
    print("一键投递流程开始")
    print("=" * 60)

    # ── 1. 生成简历 ──
    print("\n[1/5] 生成定制简历…")
    pdf = build_resume(job_title, company, job_type, city)
    if not pdf:
        print("✗ 简历生成失败，流程中止。")
        return False

    # ── 2. 判空间 ──
    print("\n[2/5] 判断附件栏空间…")
    url = PD.current_url() or ""
    if "/web/geek/resume" not in url:
        PD.open_url(RESUME_URL)
        time.sleep(4)
    cnt = _get_attach_count()
    if cnt is None:
        print("⚠ 读不到附件计数，按「未满」继续。")
        need_delete = False
    else:
        cur = cnt.get("cur", 0)
        maxn = cnt.get("max", ATTACH_MAX)
        print(f"   附件栏：{cnt.get('text', '?')}")
        need_delete = cur >= maxn

    # ── 3. 满了删旧 ──
    if need_delete:
        print("⚠ 附件栏已满，需先删最旧一份。")
        if not no_confirm:
            print("=" * 50)
            print("即将删除附件栏最旧的一份简历，然后上传新简历。")
            print("删除不可撤回，确认继续吗？(y/n)")
            ans = input("> ").strip().lower()
            if ans not in ("y", "yes"):
                print("已取消。")
                return False
        if not _delete_first_attachment():
            print("✗ 删除失败，流程中止。")
            return False
        PD.open_url(RESUME_URL)
        time.sleep(3)
    else:
        print("   附件栏有空间，无需删除。")

    # ── 4. 上传 ──
    print("\n[4/5] 上传新简历…")
    if not no_confirm:
        print("=" * 50)
        print(f"即将上传：\n  {pdf}")
        print("确认继续吗？(y/n)")
        ans = input("> ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return False
    ok, d = _upload_via_component(pdf)
    if not ok:
        print("✗ 上传失败：", d)
        return False
    print(f"✓ 已上传：{d.get('fileName')}（{d.get('fileSize')} 字节）")
    time.sleep(3)
    PD.open_url(RESUME_URL)

    # ── 5. 打开会话投递 ──
    print("\n[5/5] 打开目标会话并投递…")
    if not no_confirm:
        print("=" * 50)
        print(f"即将给「{boss or company}｜{job_title}」发送附件简历。")
        print("发送不可撤回，确认继续吗？(y/n)")
        ans = input("> ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消（简历已上传到附件栏，未投递）。")
            return False

    # 先到聊天页，再按匹配打开会话
    if "/web/geek/chat" not in (PD.current_url() or ""):
        PD.open_url("https://www.zhipin.com/web/geek/chat")
        time.sleep(4)
    ok2, why = _open_chat_by_match(boss=boss, company=company, job_title=job_title)
    if not ok2:
        print(f"✗ 打开会话失败：{why}")
        print("  （简历已上传到附件栏；请人工打开会话后再跑 send。）")
        return False
    print(f"✓ {why}")

    # 发简历（复用 send_resume 的核心动作，跳过它内部的人工确认，避免重复问）
    sent = send_resume(job_id, pdf_path=pdf, confirm=False)
    print("=" * 60)
    if sent:
        print("✓ 一键流程完成：简历已生成、上传、并触发发送。")
    else:
        print("△ 简历已生成并上传，但发送环节未完成（见上方提示）。")
    print("=" * 60)
    return sent


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="定制简历→上传→发送（附件简历链路）")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="定制简历 PDF")
    b.add_argument("--job", required=True, help="岗位名")
    b.add_argument("--company", required=True, help="公司名（仅日志用，不进文件名）")
    b.add_argument("--type", choices=["intern", "formal"], default="intern",
                   help="intern=实习岗 / formal=正式岗")
    b.add_argument("--city", default="", help="岗位所在城市（写进目标地点，默认北京）")

    u = sub.add_parser("upload", help="上传附件简历")
    u.add_argument("--pdf", required=True, help="PDF 路径")
    u.add_argument("--no-confirm", action="store_true", help="跳过人工确认（默认要确认）")

    s = sub.add_parser("send", help="发送附件简历给 HR")
    s.add_argument("--job-id", required=True, help="encryptJobId")
    s.add_argument("--pdf", help="PDF 路径（可选）")

    f = sub.add_parser("flow", help="一键：生成→判空间→删旧→上传→投递")
    f.add_argument("--job", required=True, help="岗位名（写进简历求职方向）")
    f.add_argument("--company", required=True, help="公司名（定位会话 + 日志）")
    f.add_argument("--type", choices=["intern", "formal"], default="intern",
                   help="intern=实习岗 / formal=正式岗")
    f.add_argument("--city", default="", help="岗位城市（写进目标地点，默认北京）")
    f.add_argument("--boss", default="", help="HR 名（定位会话，可选）")
    f.add_argument("--job-id", default="", help="encryptJobId（仅日志）")
    f.add_argument("--no-confirm", action="store_true", help="跳过人工确认闸门")

    args = p.parse_args()

    if args.cmd == "build":
        pdf = build_resume(args.job, args.company, args.type, args.city)
        sys.exit(0 if pdf else 1)
    elif args.cmd == "upload":
        ok = upload_resume(args.pdf, confirm=not args.no_confirm)
        sys.exit(0 if ok else 1)
    elif args.cmd == "send":
        ok = send_resume(args.job_id, args.pdf)
        sys.exit(0 if ok else 1)
    elif args.cmd == "flow":
        ok = flow(args.job, args.company, args.type, args.city,
                  boss=args.boss, job_id=args.job_id, no_confirm=args.no_confirm)
        sys.exit(0 if ok else 1)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
