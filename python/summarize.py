# -*- coding: utf-8 -*-
"""summarize.py —— 文献总结：素材包、机械校验、意图判定。

用法:
    python summarize.py <workdir> --pack [--budget 12000] [--figs 3]  # 切素材包并落盘 summary.src.txt
    python summarize.py <workdir> --grep <正则> [--limit 20] [--sec results,discussion]
                                                       # 按关键词回原文捞句子（素材包漏了就靠它补）
    python summarize.py <workdir> --fig 4            # 展开某张图/表的完整图注（候选池外的也能取）
    python summarize.py <workdir> --check                    # 校验 summary.json（六项 + 图表五项）
    python summarize.py --intent "帮我总结一下这篇"           # 关键词判定，不读文件

设计要点:
  - 素材包只喂「写总结必需的句子」（默认 12k 字符），不喂全文：单篇约 3.5k token。
  - 切包是启发式挑句，必然有漏；--grep / --fig 是兜底通道，口径与素材包一致。
  - 用英文原文，不用译文，避免把译文的错误再传一层；术语靠 glossary 对齐口径。
  - 每节必须附一句原文证据（evidence），--check 要求它逐字命中原文 —— 编不出来。
  - 图表：只喂图注（按 panel 保留），模型选 1–3 张并写解读，anchor 必须命中该图图注。
  - 退出码：0 通过 / 1 有疑点 / 2 必须重写。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from lexicon import EN_KEEP  # noqa: E402  —— 与 verify.py 共用一份，见 lexicon.py
import journal as J  # noqa: E402  —— 过程记录；本文件里的局部变量 journal 指期刊名，别混

LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
       "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
UNI = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
       "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " ",
       "\u2032": "'", "\u02b9": "'"}

SEC_PAT = [
    ("abstract", r"^(abstract|summary)\b"),
    ("intro", r"^(introduction|background)\b"),
    ("results", r"^(results? and discussion|results?|findings)\b"),
    ("discussion", r"^(discussion|conclusions?|concluding remarks)\b"),
    ("methods", r"^(materials and methods|methods?|experimental procedures?|star\*?methods?)\b"),
    ("refs", r"^(references?|bibliography|literature cited)\b"),
    ("stop", r"^(acknowledg\w*|author contributions?|authors'? contributions?|competing interests?|"
             r"conflicts? of interest|data availability|code availability|additional information|"
             r"supplementary\w*|funding|ethics)\b"),
]
STOP_NAMES = {"refs", "stop"}
NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*|[IVX]+)[.)]?\s+")

GAP_WORDS = re.compile(
    r"\b(however|little is known|remains? (?:unclear|unknown|elusive)|poorly understood|"
    r"limited|limitation|lack(?:s|ing)?|challeng\w+|difficult|no (?:method|approach|tool)|"
    r"have not been|has not been|cannot|unable)\b", re.I)
SOLVE_WORDS = re.compile(
    r"\b(to (?:overcome|address|circumvent|solve|sidestep)|here,? we|"
    r"we (?:therefore|thus|instead|then)? ?(?:developed|develop|designed|design|built|build|"
    r"combined|combine|established|engineered|created|implemented)|"
    # 被动语态同样在报对策：「a clustering-based strategy was implemented」
    r"(?:was|were) (?:implemented|developed|designed|built|introduced|adopted|employed|"
    r"established|engineered|created|combined)|"
    r"(?:a|this|the|our) (?:[A-Za-z]+[- ]){0,2}(?:strategy|approach|method|pipeline|"
    r"workflow|framework|algorithm) (?:was|were|has been|had been)|"
    r"by combining|allows? us to|enabled us to)\b", re.I)
FIND_WORDS = re.compile(
    r"\b(we (?:found|show|showed|demonstrate|demonstrated|report|identify|identified|"
    r"establish|established|discover|discovered|reveal|revealed)|here we|our results)\b", re.I)
PROTO_REF = re.compile(          # 方法节里的「详见补充方法」纯指路句，没有信息量
    r"\b(?:see|refer to)\b[^.]{0,60}\b(?:supplement|methods? s\d|online|si\b)", re.I)
DISC_WORDS = re.compile(
    r"\b(limitation|caveat|future|further (?:work|studies)|remains? (?:unclear|unknown|to be)|"
    r"not (?:tested|examined|determined)|only|small sample|cannot|may not|generaliz\w+)\b", re.I)
JUNK = [
    re.compile(r"^(table|figure|fig\.?|scheme|box)\s*\d", re.I),
    re.compile(r"^(received|accepted|published|revised|available online)\b", re.I),
    re.compile(r"^(check for updates|open access|this article is licensed|copyright)\b", re.I),
    re.compile(r"\|\s*\(?\d{4}\)?\s*\d+[:\s]\d+"),          # 页脚期刊行
    re.compile(r"^(nature|science|cell|elife|pnas|bmc)\b.{0,40}\d+\s*$", re.I),
    re.compile(r"^\W*[A-Za-z]{1,2}\W*$"),                    # 图内字母标签
    re.compile(r"^[\d\s*†‡]+$"),                             # 纯编号
    re.compile(r"[\w.+-]+@[\w.-]+\.\w+"),                    # 邮箱（作者信息行）
    re.compile(r"^[\d\s,†‡*]*\s*(department|division|institute|school of|faculty|college|"
               r"centre? for|laboratory of|universit)", re.I),
    re.compile(r"^(correspondence|to whom correspondence|present address)", re.I),
]

INTENT_ZH = ["总结", "概括", "归纳", "提炼", "要点", "脉络", "大意", "讲了什么", "讲了啥",
             "说了什么", "说什么", "核心结论", "主要结论", "主要观点", "主要内容",
             "写个摘要", "摘要一下", "综述一下", "介绍一下这篇", "梳理", "读懂", "看懂",
             "亮点", "贡献"]
INTENT_EN = ["summary", "summarize", "summarise", "tldr", "tl;dr", "overview",
             "key points", "main findings", "abstract of", "what is this paper",
             "explain this paper", "walk me through"]
TRANS_ZH = ["翻译", "译成", "译为", "译文", "全文译", "逐句译"]
TRANS_EN = ["translate", "translation", "translate into"]

ABBR = re.compile(r"\b(e\.g|i\.e|et al|Fig|Figs|Eq|Eqs|Ref|Refs|vs|approx|ca|cf|"
                  r"No|Dr|Prof|St|Inc|Ltd|resp|Suppl|Extended Data)\.")

# ---- 图表素材用 ----
FIG_REF = re.compile(r"\b(?:Fig(?:ure)?s?\.?)\s*(\d+(?:\s*(?:,|and|&|-|–|to)\s*\d+)*)|图\s*(\d+)", re.I)
TAB_REF = re.compile(r"\b(?:Tables?)\s*(\d+(?:\s*(?:,|and|&|-|–|to)\s*\d+)*)|表\s*(\d+)", re.I)
# 子图标记：三种写法都要认 —— `(A)`、`a,`（Nature 系 "a, Adult humans…"）、`a `（a 后空格+大写）。
# 还有 Nature 系的连写 `c,d,`（"c,d, Data in c…"）—— 字母后跟「逗号+小写字母」，也要认。
# 只写一个 `\(([A-H])\)` 会把 Nature/Cell 系 "a, …" 与 "c,d, …" 格式整篇漏掉。
PANEL_A = re.compile(
    r"(?:^|(?<=[.;:!?])\s|\s{2,}|(?<=[,;])\s*)([a-h])"
    r"(?=\s+[A-Z(\[]|\s*[,;]\s*[a-h]|,|;\s+[A-Z(\[])")
# 子图标记要连 (E–E’)、(D′)、(D,E)、(A–E) 一起认：只写 \(([A-H])\) 会把图注里真实存在的
# 子图判成缺失，于是 GAP 自检报「正文提到但图注未写 E」——本来是有的，属于误报。
PANEL_B = re.compile(r"\(\s*([A-H])(?:\s*[–\-—]\s*[A-H])?[′’']?\s*\)")
# 打包时用的宽版：`(D,E)` 和 `(A–E)` 这种合写要按多个子图处理
PANEL_MARK_ALL = re.compile(r"\(\s*([A-H](?:\s*[,–\-—]\s*[A-H])*[′’']?)\s*\)")
# 正文里的子图引用：`Fig. 1E`、`Figure 3B`、`图 2c`。用来和「图注写了哪些子图」求差集，
# 差集非空说明图注没覆盖到 —— 可能是抽取截断，也可能原文就这样，两种情况模型都别硬写。
FIG_PANEL_REF = re.compile(r"(?:Fig(?:ure)?s?\.?|图)\s*\d+\s*([A-Ha-h])(?![A-Za-z])")
# panel_cut 压缩后的标记写作 `[a]`（合写时 `[d/e]`），不算它是子图就会把压缩过的图注
# 判成「子图全丢」
PANEL_OUT = re.compile(r"\[([a-h](?:\s*/\s*[a-h])*)\]")
# 示意图关键词：匹配时先把空白全部抹掉（见 schema_hit），所以这里的关键词也不带空格。
# 目的：PDF 把 `flow chart` 抽成 `flo w chart`、`Research` 抽成 `Researc h` 时仍能认出。
SCHEMA_FLAT = re.compile(r"(schematic|workflow|flowchart|overview|experimentaldesign|"
                         r"pipeline|conceptual|procedures?)", re.I)

# 视觉细节：中文说法 → 图注里应对应的英文。图注没写就不许描述（防止看图作文）。
VIS_ZH = [("蓝", r"\bblu\w*"), ("红", r"\bred\b"), ("绿", r"\bgreen\b"),
          ("黄", r"\byellow\b"), ("紫", r"\bp(?:urple|ink)\b"), ("灰", r"\bgr[ae]y\b"),
          ("黑", r"\bblack\b"), ("橙", r"\borange\b"), ("粉", r"\bpink\b"),
          ("曲线", r"\bcurv\w*|\blines?\b"), ("坐标轴", r"\bax[ie]s\b|x-axis|y-axis"),
          ("横轴", r"x-axis|horizontal axis"), ("纵轴", r"y-axis|vertical axis"),
          ("箭头", r"\barrow\w*"), ("虚线", r"\bdash\w*"), ("阴影", r"\bshad\w*"),
          ("热图", r"\bheat ?map\w*"), ("树状图", r"\bdendro\w*"),
          ("柱状", r"\bbars?\b"), ("散点", r"\bscatter\w*")]
PANEL_ZH = re.compile(r"图\s*\d+\s*([a-h])(?![A-Za-z])|子图\s*([a-h])|"
                      r"(?:panel|面板)\s*([a-h])|（([a-h])）|\(([a-h])\)")
FIG_MIN, FIG_MAX = 90, 240
SPLIT = re.compile(r"(?<=[.!?])[\s]+(?=[\"'(\[]?[A-Z0-9\u2018\u201c])")


# ---------------------------------------------------------------- 文本工具
def fix_text(s):
    s = s or ""
    for k, v in LIG.items():
        s = s.replace(k, v)
    return s


def strip_markup(s):
    """把 ~{′} 这类上标标记、[ 2 , 5 ] 这类引文空格压平，供模型阅读。"""
    s = re.sub(r"~\{([^{}]*)\}", r"\1", s or "")
    s = re.sub(r"(?<=\d)\s*[′'\u2032]\s*(?=U|u)", "′", s)
    s = re.sub(r"\[\s*(\d[\d,\s\u2013\-]*)\s*\]", lambda m: "[" + re.sub(r"\s+", "", m.group(1)) + "]", s)
    s = re.sub(r"\s+([,.;:)\]])", r"\1", s)
    s = re.sub(r"([([])\s+", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def squash(s):
    """只留字母数字：证据/标题比对用，容忍 PDF 的断词与标点差异。"""
    return re.sub(r"[^a-z0-9]", "", fix_text(s).lower())


def norm_loose(s):
    """证据比对用：统一引号/破折号/连字，压空白，忽略大小写。"""
    s = fix_text(s)
    for k, v in UNI.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip().casefold()


def sentences(text):
    t = ABBR.sub(lambda m: m.group(0).replace(".", "\x01"), fix_text(text))
    parts = [p.replace("\x01", ".").strip() for p in SPLIT.split(t)]
    return [p for p in parts if p]


def cjk_ratio(s):
    if not s:
        return 0.0
    zh = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    lat = sum(1 for c in s if c.isascii() and c.isalpha())
    return zh / max(1, zh + lat)


ZH_PUNCT = "，。、；：！？（）「」《》【】…—"


def is_cjk(c):
    return bool(c) and "\u4e00" <= c <= "\u9fff"


def is_zh_ctx(c):
    """中文语境：汉字或中文标点。英文夹在「，」和「。」之间也算中文句子。"""
    return is_cjk(c) or (bool(c) and c in ZH_PUNCT)


def in_paren(text, i):
    """位置 i 是否还在某个括号里 —— 「非翻译区（untranslated regions，UTRs）」
    里的 regions 只是英文全称的一部分，不该单独判为裸术语。"""
    opened = max(text.rfind("（", 0, i), text.rfind("(", 0, i))
    if opened < 0:
        return False
    return opened > max(text.rfind("）", 0, i), text.rfind(")", 0, i))


# 按惯例不译、也不必配中文的英文：拉丁短语与单位。定义在 lexicon.py，与 verify.py 共用
BARE_EN = re.compile(r"[A-Za-z]+(?:[-'\u2032][A-Za-z]+)*")


def bare_en(text):
    """中文句子里「只写英文」的术语，返回去重后的词表。

    只抓全小写的英文实词：含大写或数字的是基因名（eIF5A）、方法缩写（NaP-TRAP）、
    试剂与仪器型号，按惯例保留原文，不在「必须配中文」之列。
    合规写法是「中文（English）」，即英文词前紧邻中文左括号；写成纯中文也不报。
    """
    out = []
    text = text or ""
    for m in BARE_EN.finditer(text):
        w = m.group(0)
        if len(w) < 4 or w != w.lower() or w.lower() in EN_KEEP:
            continue
        if "-" in w or "'" in w or "\u2032" in w:   # pull-down / scikit-learn 这类固定写法
            continue
        i, j = m.start(), m.end()
        if in_paren(text, i):
            continue                           # 英文括注里的全称，逐词判会碎成一片
        k = i - 1
        while k >= 0 and text[k] in " \t":     # 中英文之间常有一个空格
            k -= 1
        prev = text[k] if k >= 0 else ""
        n = j
        while n < len(text) and text[n] in " \t":
            n += 1
        nxt = text[n] if n < len(text) else ""
        if not (is_zh_ctx(prev) or is_zh_ctx(nxt)):
            continue                           # 整串英文（证据、英文名）不在此列
        if prev in "（(" and k >= 1 and is_cjk(text[k - 1]):
            continue                           # 合规：中文（English）
        if w not in out:
            out.append(w)
    return out


def clean_title(t):
    t = fix_text(t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\bdoi:\s*\S+", " ", t, flags=re.I)
    t = re.sub(r"^(article|research article|letter|review|open access)\b[:\s]*", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .;:—-")


def ok_sent(s):
    """一句话是否值得进素材包：太短、以括号/图表引用开头的一律不要。"""
    s = s.strip()
    return len(s) >= 40 and not s.startswith(("(", "[", "Fig", "Table")) and s.count(" ") >= 5


def is_junk(t):
    if len(t) < 25:
        return True
    for rx in JUNK:
        if rx.search(t):
            return True
    digits = sum(c.isdigit() for c in t) / len(t)
    return digits > 0.22


# ---------------------------------------------------------------- 结构解析
def load_json(workdir, name):
    p = os.path.join(workdir, name)
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def header_set(blocks):
    seen = {}
    for b in blocks:
        key = re.sub(r"\s+", " ", (b.get("text") or "")).strip()
        if not key:
            continue
        seen.setdefault(key, set()).add(b.get("page"))
    return {k for k, pages in seen.items() if len(pages) >= 3}


def match_sec(low):
    for name, pat in SEC_PAT:
        m = re.match(pat, low)
        if m:
            return name, m.end()
    return None, 0


def heading_of(text, btype):
    """返回 (section, 标题文本, 标题后残留正文)。section 为 other 时是子标题。"""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    name, end = match_sec(low)
    if not name:                                   # 带编号的标题：2.1 Results
        m = NUMBERING.match(t)
        if m:
            name, e2 = match_sec(t[m.end():].lower())
            if name:
                end = m.end() + e2
    if name:
        return name, t[:end].strip(), t[end:].strip().lstrip(" .:—-")
    if btype in ("h1", "h2"):
        ss = sentences(t)
        # PDF 常把标题与首段并成一块：取首个句子当子标题，余下留给正文
        if ss and len(ss[0]) <= 110:
            return "other", ss[0], t[len(ss[0]):].strip()
    if len(t) > 240:
        return None
    if btype in ("h1", "h2") and len(t) <= 90 and not t.endswith((".", "。", "?", "!")):
        return "other", t, ""
    m = re.match(r"^(\d+(?:\.\d+)*)\s+([A-Z][^.]{2,80})$", t)
    if m:
        return "other", t, ""
    return None


def pick_abstract(blocks, skip):
    """首页的摘要块。

    优先找 `abstract` 类型块（抽取层标好的）；没有的话，退回「首页最长块
    （≥500 字符、非跑页眉）」。摘要被切碎（第 5 篇）时，最长块可能不是摘要，
    因此再兜一层：多块合起来 ≥2 块且平均 >200 字符，取第一个 `abstract`/`h2` 块。
    """
    typed = [i for i, b in enumerate(blocks[:40])
             if i not in skip and b.get("page") == 1
             and b.get("type") == "abstract" and len((b.get("text") or "").strip()) >= 200]
    if typed:
        return max(typed, key=lambda i: len((blocks[i].get("text") or "").strip()))
    best, size = None, 0
    for i, b in enumerate(blocks[:40]):
        if i in skip:
            continue
        t = (b.get("text") or "").strip()
        if b.get("page") != 1 or len(t) < 500 or b.get("type") not in ("h2", "p", "abstract"):
            continue
        if len(t) > size:
            best, size = i, len(t)
    return best


def scan(blocks):
    """返回 (英文标题, 摘要正文, 条目列表)。条目 = dict(major, sub, text, kind)。"""
    hdr = header_set(blocks)
    skip = {i for i, b in enumerate(blocks)
            if re.sub(r"\s+", " ", (b.get("text") or "")).strip() in hdr}
    ai = pick_abstract(blocks, skip)
    title = ""
    for b in blocks[:6]:
        if b.get("type") == "title" and (b.get("text") or "").strip():
            title = clean_title(b["text"])
            break
    if not title:
        # 页眉被降级后 title 可能为空（PMC 稿件常见）：回落首页最长的大标题块，
        # 但排除跑页眉词（Articles / Author Manuscript 这类不是标题）。
        cands = [b for b in blocks[:12]
                 if b.get("type") in ("h1", "h2")
                 and len((b.get("text") or "").strip()) > 20
                 and re.sub(r"\s+", " ", (b.get("text") or "")).strip() not in hdr]
        if cands:
            title = clean_title(max(cands, key=lambda b: len(b["text"].strip()))["text"])
    if ai is not None:
        skip.add(ai)

    entries, major, sub = [], "intro", ""
    started = ai is None
    for i, b in enumerate(blocks):
        if i in skip:
            continue
        if not started and ai is not None and i > ai:
            started = True                      # 摘要之后就是正文，别再等标题
        t = (b.get("text") or "").strip()
        if not t or b.get("type") in ("caption", "small", "eq", "ref", "refhead"):
            continue
        if not started:
            continue
        h = heading_of(t, b.get("type"))
        if h:
            name, head, rest = h
            if name in STOP_NAMES:
                nxt = [b.get("type") for b in blocks[i + 1:i + 6]]
                if "ref" in nxt:                   # 确认后面真是文献列表才截断
                    break
                continue
            if name != "other":
                major, sub = name, ""
                entries.append({"major": major, "sub": "", "text": head, "kind": "head"})
            else:
                sub = head
                entries.append({"major": major, "sub": sub, "text": head, "kind": "subhead"})
            if rest:
                entries.append({"major": major, "sub": sub, "text": rest, "kind": "para"})
            started = True
            continue
        if not started:
            continue
        if is_junk(t):                          # 页脚/作者单位/图内标签：合并前先剔掉
            continue
        # 方法节里的「Zebrafish maintenance and mating.」这类小节标题（带句点、很短）
        if major == "methods" and len(t) <= 70 and t.endswith(".") and len(t.split()) <= 9:
            entries.append({"major": major, "sub": t, "text": t, "kind": "subhead"})
            sub = t
            continue
        entries.append({"major": major, "sub": sub, "text": t, "kind": "para"})

    # 合并同小节内的连续段（PDF 按栏切块会把一段拆成多块，首句因此成为半句）
    merged = []
    for e in entries:
        if (e["kind"] == "para" and merged and merged[-1]["kind"] == "para"
                and merged[-1]["major"] == e["major"] and merged[-1]["sub"] == e["sub"]
                and len(merged[-1]["text"]) + len(e["text"]) <= 1200):
            merged[-1]["text"] = merged[-1]["text"].rstrip("-") + " " + e["text"]
        else:
            merged.append(dict(e))
    abstract = fix_text(blocks[ai]["text"]).strip() if ai is not None else ""
    return title, abstract, merged


# ---------------------------------------------------------------- 素材包
def terms_block(body_text, limit=28, workdir=""):
    """只带源文命中的术语，保证总结与译文口径一致。

    名额按三分之二的短语、三分之一的单词分配：词表扩到上百条后，纯按命中数排会
    被 translation、assay 这类泛词占满（它们天然出现最多），而 western blot、
    signal-to-noise ratio 这种真正需要对齐写法的短语反而挤不进去。单词里也留了
    三分之一名额，polyadenylation、confluence 这类不能一概让位。
    """
    try:
        import glossary as G
        terms = G.load_terms()
        counts = G.TermMatcher(terms).counts(body_text)
        multi, single = [], []
        for i, hits in counts.items():
            term = terms[i]
            en = re.split(r"\s*/\s*", term["en"])[0].strip()
            keep = G.keep_original(term)
            # 短的全小写单词（plasmid、template、primer）谁都译不错，占着名额没价值；
            # 缩写（cDNA）、复合词（t-test）、长词（polyadenylation）才需要对照。
            if not keep and " " not in en and "-" not in en and len(en) < 10 and en == en.lower():
                continue
            if keep:
                # 照原文写的词显示缩写形态（5′UTR），写全称既费字数又容易让模型联想到中文长名
                en = min(term["variants"], key=len)
            zh = "原文照写（不加括注）" if keep else G.primary_zh(term)
            row = (hits, en, zh)
            (multi if " " in en else single).append(row)
        multi.sort(key=lambda r: (-r[0], r[1]))
        single.sort(key=lambda r: (-r[0], r[1]))
        n_multi = min(len(multi), (limit * 2 + 2) // 3)
        picked = multi[:n_multi] + single[:limit - n_multi]
        if workdir:
            J.decide(workdir, "summary", "TERMS_HIT",
                     " | ".join(f"{en}({h})" for h, en, _ in picked[:8]),
                     f"词表命中 {len(counts)} 条，带进素材包 {len(picked)} 条")
        return "\n".join(f"- {en} → {zh}" for _, en, zh in picked)
    except Exception:
        return ""


def select(entries, abstract):
    """按五节所需挑句，返回 {优先级: [(标签, 文本)]}。"""
    b = {i: [] for i in range(6)}
    body_all = [abstract] if abstract else []
    if abstract:
        b[0].append(("ABS", abstract[:2400]))
    intro_first = disc_first = False
    used = set()
    solve_n = [0]
    meth = []

    def put(pri, tag, text):
        key = re.sub(r"\W+", "", text)[:60].lower()
        if not key or key in used:
            return
        used.add(key)
        b[pri].append((tag, text))
        if tag == "SOLVE":
            solve_n[0] += 1

    def gap_n():
        return sum(1 for _, tag in b[2] if tag == "GAP")

    def take_solve(ss, limit=2, lo=60, hi=380):
        """补「作者怎么解决的」原文：素材包缺它，模型就只能编。

        limit = 本次最多取几句；solve_n 是全文封顶（4 句），防止解法句淹没别的节。
        旧版把 limit 当全局计数用，摘要取到一句就会把后面的调用一并堵死。
        """
        got = 0
        for x in ss:
            if got >= limit or solve_n[0] >= 4:
                return
            if SOLVE_WORDS.search(x) and lo < len(x) < hi:
                n0 = solve_n[0]
                put(2, "SOLVE", x[:340])
                if solve_n[0] > n0:
                    got += 1

    for e in entries:
        m, s, t, k = e["major"], e["sub"], e["text"], e["kind"]
        if k in ("head", "subhead"):
            continue
        body_all.append(t)
        if is_junk(t):
            continue
        ss = sentences(t)
        if not ss or ss[0][:1].islower():       # 半句开头 = 接上一段的残片
            continue
        ss = [x for x in ss if ok_sent(x)]
        if not ss:
            continue
        if m == "abstract":
            if not b[0]:
                put(0, "ABS", t[:2400])
            take_solve(ss, limit=1)
        elif m == "intro":
            if not intro_first:
                # 引言首句要排除单位块（Institute of… 常被并进引言第一块）
                first = ss[0]
                if is_junk(first):
                    first = next((x for x in ss[1:] if not is_junk(x)), ss[0])
                put(1, "INTRO", first[:520])
                intro_first = True
                for x in ss[1:]:
                    if GAP_WORDS.search(x) and len(x) > 50:
                        put(2, "GAP", x[:320])
                        break
                take_solve(ss[1:])
            else:
                if gap_n() < 2:
                    for x in ss:
                        if GAP_WORDS.search(x) and len(x) > 50:
                            put(2, "GAP", x[:320])
                            break
                take_solve(ss)
        elif m == "discussion":
            if not disc_first:
                put(1, "DISC", ss[0][:560])
                disc_first = True
            take_solve(ss)
            if len(b[3]) < 4:
                for x in ss:
                    if DISC_WORDS.search(x) and len(x) > 50:
                        # future studies / further work 是「展望」，不是「本方法局限」
                        if re.search(r"\b(?:future|further) (?:studies|work|directions|"
                                     r"investigations?|experiments?|research)\b", x, re.I) \
                                and not re.search(r"\b(limitation|caveat|only|small sample|"
                                                 r"cannot|may not|generaliz)\w*\b", x, re.I):
                            continue
                        put(3, "LIMIT", x[:300])
                        break
        elif m == "results":
            if len(b[4]) < 14:
                put(4, "RES", ss[0][:260])
            if len(b[4]) < 20 and len(ss) > 1:
                # 段内最长的一句：关键发现与统计细节常在段中，不在段首
                rest = max(ss[1:], key=len)
                if 80 < len(rest) < 420:
                    put(4, "RES", rest[:260])
            if len(b[3]) < 12:
                for x in ss[:8]:
                    if FIND_WORDS.search(x) and x != ss[0] and 60 < len(x) < 320:
                        put(3, "FIND", x[:260])
                        break
            # 新方法往往在结果节首次出场（「we developed…」），不在这里取就永远缺解法
            take_solve(ss)
        elif m == "methods":
            # 方法节动辄两三百句，按顺序取只会全挤在最前面（试剂、质粒构建），
            # 真正的关键步骤在中后段；这里先收起来，循环结束后按全节均匀取样。
            meth.append([x for x in ss
                         if 50 < len(x) < 420 and not PROTO_REF.search(x)])
            if solve_n[0] < 2:            # 方法节里的「我们构建了…」也是解法，但优先级最低
                take_solve(ss, limit=2)
    if meth:
        step = max(1, len(meth) // 5)
        for k in range(0, len(meth), step):
            if len(b[5]) >= 6:
                break
            ss = meth[k]
            if not ss:
                continue
            put(5, "METH", ss[0][:220])
            if len(ss) > 1 and len(b[5]) < 6:
                put(5, "METH", max(ss[1:], key=len)[:240])
    return b, body_all


# ---------------------------------------------------------------- 图表
def clean_caption(s):
    """图注清洗：PDF 字距伪影会让 ( A ) → (A)。

    单词内部的断字（Researc h / w orkflow）两个方向都可能，没有词典分不清，
    强修会把「Experimental w orkflow」粘成「Experimentalworkflow」，所以只做括号。
    """
    s = fix_text(s or "")
    s = re.sub(r"\(\s*([A-H])\s*\)", r"(\1)", s)
    return re.sub(r"\s+", " ", s).strip()


def fig_number(f):
    """图号：优先 caption_num，其次 fig_name 里的数字，最后退 id。

    caption_num ≥100 的是扩展数据图/补充图（extract 里加了 100 偏移避免撞号），
    对外显示成 S1、S2 —— 与正文 Fig. 1 区分，符合学术写法。
    """
    n = f.get("caption_num")
    if n:
        return f"S{n - 100}" if n >= 100 else str(n)
    name = f.get("fig_name") or ""
    m = re.search(r"\d+", name)
    if m:
        return m.group(0)
    return str(f.get("id"))


def schema_hit(cap, chars=300):
    """整图是否「示意图 / 流程图」——只看图注的**标题段**（第一个子图标记之前）。

    复合图常在图注第一个子图上写 "a Schematic of…"，按整段匹配会把验证图、结果图
    全判成示意图（实测 8 张里 5 张被误判，把真正的方法图挤下去）。
    匹配前抹掉空白：PDF 常把单词拆开（`Research flo w chart`）时也能认出。
    """
    cap = re.sub(r"\s+", " ", strip_markup(cap or "")).strip()
    seg = cap[:chars]
    marks = [m.start() for m in PANEL_A.finditer(seg)]
    marks += [m.start() for m in PANEL_B.finditer(seg)]
    if marks:
        seg = seg[:min(marks)]
    return bool(SCHEMA_FLAT.search(re.sub(r"\s+", "", seg)))


def panel_count(cap):
    """图注里数得出几个子图标记（a/b/c 或 (A)(B)）。"""
    cap = cap or ""
    marks = [m.group(1) for m in PANEL_A.finditer(cap)]
    marks += [m.group(1) for m in PANEL_B.finditer(cap)]
    return len({m.lower() for m in marks})


def fig_ref_nums(s, want_table=False):
    """一句正文里引用了哪些图号：`Fig. 4 and 5` → {4,5}，`Fig. 4–6` → {4,5,6}。

    旧的单号匹配会把并列/范围引用只算第一张，导致别的图少记引用。
    """
    # 「补充图/表」不算正文引用；只跳过该处，不整句作废（同句里的正文图仍要计数）。
    # PDF 会把词拆成 "Supple- mentary" / "Sup- plementary"，所以先抹掉空白与连字符再比对。
    rx = TAB_REF if want_table else FIG_REF
    out = set()
    for m in rx.finditer(s):
        head = re.sub(r"[\s\-–—]", "", s[max(0, m.start() - 32):m.start()]).lower()
        if head.endswith("supplementary"):
            continue
        grp = next((g for g in m.groups() if g), "")
        if not grp:
            continue
        for seg in re.split(r"\s*(?:,|and|&)\s*", grp):
            nums = [int(x) for x in re.split(r"\s*(?:-|–|—|to)\s*", seg) if x.strip().isdigit()]
            if len(nums) == 2 and 0 < nums[1] - nums[0] <= 10:
                out.update(range(nums[0], nums[1] + 1))
            else:
                out.update(nums)
    return out


def fig_index(st):
    """真实图区（排除图形摘要与无主图区）→ {"fig4"/"tab1": {kind, page, caption, grid}}。

    图与表共用编号（Fig. 1 与 Table 1 都叫 1），所以键带类型前缀。
    图注索引对不上（老版本 structure.json）时按图号在 caption 块里回落匹配。
    """
    blocks = st.get("blocks") or []
    caps = []
    for b in blocks:
        if b.get("type") == "caption":
            m = re.match(r"\s*(Fig(?:ure)?\.?|Table)\s*0*(\d+)", b.get("text") or "", re.I)
            if m:
                caps.append((("tab" if m.group(1).lower().startswith("tab") else "fig")
                             + str(int(m.group(2))), clean_caption(b.get("text"))))
    out = {}
    for f in st.get("figures") or []:
        if f.get("role") in ("graphical_abstract", "orphan"):
            continue
        kind = "table" if f.get("kind") == "table" else "figure"
        key = ("tab" if kind == "table" else "fig") + fig_number(f)
        cap = ""
        ci = f.get("caption_block")
        if isinstance(ci, int) and 0 <= ci < len(blocks) and blocks[ci].get("type") == "caption":
            cap = clean_caption(blocks[ci].get("text"))
        if not cap:
            cap = next((t for k, t in caps if k == key), "")
        if key in out and len(cap) <= len(out[key]["caption"]):
            continue
        out[key] = {"kind": kind, "page": f.get("page"), "n": fig_number(f),
                    "caption": cap, "grid": f.get("grid")}
    return out


PANEL_LIMIT = 2600      # 图注进素材包的总上限（旧值 1000 会在第 4 个子图处砍断）
PANEL_ONE = 240         # 单个子图首句的上限


def _cut_words(s, n):
    """按字数截断，尽量断在空格处，别把单词切成两半。"""
    if len(s) <= n:
        return s
    head = s[:n]
    sp = head.rfind(" ")
    return (head[:sp] if sp > n * 0.6 else head).rstrip(" ,;:") + "…"


def _mark_letters(inner):
    """`A` / `D′` / `D,E` / `A–E` → 子图字母集合。范围展开只做跨度 ≤6 的。

    `(A–E)` 这种合写在方法类图注里很常见（「A–E 都是同一批测量」），
    当成单个 a 会让 D/E 在压缩后看起来「不见了」。
    """
    out = set()
    for tok in re.split(r"\s*,\s*", inner):
        rng = re.match(r"([A-H])\s*[–\-—]\s*([A-H])", tok)
        if rng:
            a, b = ord(rng.group(1)), ord(rng.group(2))
            if 0 < b - a <= 6:
                out |= {chr(c).lower() for c in range(a, b + 1)}
                continue
        letters = re.findall(r"[A-H]", tok)
        if letters:
            out.add(letters[0].lower())
    return out


def panel_marks(cap):
    """图注里的子图起点：[(位置, {字母})]，已排除行内引用。

    两件事要同时成立才好用：
    - 切点取「(」的位置，不取字母位置：`(E–E′)/(D′)` 从字母处切会剩下一截 `–E′)`，
      打包后变成「[e] –E′) Correlation…」，读起来像乱码。
    - 排除行内引用：图注正文里常有 `…varying strengths (B) is tested…`、`(B). In (B),…`，
      按它切会切出 `[b] , RLuc values…` 这种碎片。真子图说明要么在句首，要么后面紧跟
      大写词/数字；行内引用两条都不满足。
    - 反过来不能只要求「前面是句号」：PDF 会把上句句点吃掉，`…the distribution (E–E′)
      Correlation…` 是真子图起点，按句号判会把 E 整段丢掉（实测踩过）。
    """
    cap = cap or ""
    marks = [(m.start(1), {m.group(1).lower()}) for m in PANEL_A.finditer(cap)]
    for m in PANEL_MARK_ALL.finditer(cap):
        before = cap[:m.start()].rstrip()
        after = cap[m.end():].lstrip()
        at_start = not before or before[-1] in ".;:!?"
        starts_word = bool(after) and (after[0].isupper() or after[0].isdigit()
                                      or after[0] == "(")
        if at_start or starts_word:
            marks.append((m.start(), _mark_letters(m.group(1))))
    return sorted(marks)


def panel_letters(cap):
    """图注「提到」的全部子图字母（小写），含行内引用。

    与 panel_marks 的严格判定不同：这里关心的是「图注有没有说到这个子图」，
    而不是「子图说明从哪切」。图注里 `Renilla (A) and firefly (B) luciferase activity`
    这种写法确实交代了 A 和 B，不该报成「图注未写」。
    压缩过的图注里子图写作 `[a]`（合写 `[d/e]`），也算 —— 否则压缩结果会被自己
    算成「子图全丢」，GAP 自检变成误报。
    """
    cap = cap or ""
    packed = {x.strip().lower() for m in PANEL_OUT.finditer(cap)
              for x in m.group(1).split("/")}
    if packed:
        return packed
    out = {m.group(1).lower() for m in PANEL_A.finditer(cap)}
    for m in PANEL_MARK_ALL.finditer(cap):
        out |= _mark_letters(m.group(1))
    return out


def panel_desc_letters(cap):
    """有独立说明段的子图字母（panel_marks 的口径），压缩后不该丢这些。"""
    return set().union(*[s for _, s in panel_marks(cap)]) if cap else set()


def panel_cut(cap, limit=PANEL_LIMIT):
    """超长图注按子图压缩：标题段 + 每个子图的首句，**一个子图都不丢**。

    旧实现按 1000 字封顶、超了就 break —— 第 1 篇 Fig.4 的 (D) 正是这样被砍掉的，
    模型于是只写到 C，从文档上看不出自己漏了。子图消失比句子变短严重得多：
    解读会停在半截，而且没有任何迹象。
    所以预算不够时逐级缩短每段（240→180→140→100→70），最后均分兜底；
    宁可每段短，也不整段消失。
    """
    cap = strip_markup(cap)
    if len(cap) <= limit:
        return cap
    marks = panel_marks(cap)
    if len(marks) < 2:
        return cap[:limit].rstrip(" ,;") + " …"
    head = cap[:marks[0][0]].strip().rstrip(" ([").strip()
    segs = []
    for i, (pos, letters) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(cap)
        seg = cap[pos:end]
        seg = re.sub(r"^\s*\(\s*[a-hA-H](?:\s*[,–\-—]\s*[A-H])*[′’']?\s*\)\s*", "", seg)
        # 小写写法「a Schematic」与「A) …」分别去掉；大写字母后必须跟标点，
        # 否则 "Changing the base…" 的首字母 C 会被当成子图标记吃掉（实测出现过）。
        seg = re.sub(r"^\s*[a-h]\s+", "", seg)
        seg = re.sub(r"^\s*[A-H]\s*[).:]\s*", "", seg)
        ss = sentences(seg)
        segs.append(("/".join(sorted(letters)), (ss[0] if ss else seg).strip()))
    for per in (PANEL_ONE, 180, 140, 100, 70):
        out = [head] + [f"[{l}] {_cut_words(one, per)}" for l, one in segs]
        joined = re.sub(r"\(\s*\[([a-h/]+)\]", r"[\1]", " ".join(out))
        if len(joined) <= limit:
            return joined
    avail = max(40, (limit - len(head) - 4 * len(segs)) // max(1, len(segs)))
    out = [head] + [f"[{l}] {_cut_words(one, avail)}" for l, one in segs]
    return re.sub(r"\(\s*\[([a-h/]+)\]", r"[\1]", " ".join(out))


def cite_sents(body, n, want_table=False, limit=3):
    """正文里引用该图/表的句子（排除 Supplementary 引用），最多 limit 条。"""
    want = int(n) if str(n).strip().isdigit() else n
    hits = []
    for s in sentences(body):
        if want in fig_ref_nums(s, want_table=want_table) and 40 < len(s) < 400:
            hits.append(s.strip())
    return hits[:limit]


def figure_plan(st, body, max_figs=3):
    """挑候选图：先按「方法示意 / 引用最多 / 有网格的表 / 末图」各保一个，再按分数补位。

    末图要引用不低于中位才保；示意加分只给「整图就是示意图」的（关键词必须出现在
    图注标题段，而不是某个子图），避免复合图的第一个子图把整张图带成示意图。

    只做结构判断（引用次数、位置、图注关键词），不猜内容；候选按页序输出，
    这样模型读到的是原文顺序，不易张冠李戴。
    """
    idx = fig_index(st)
    if not idx:
        return [], []
    order = sorted(idx.items(), key=lambda kv: (kv[1]["page"] or 0, kv[0]))
    rows, last_fig = [], None
    for key, meta in order:
        n, tab = meta["n"], meta["kind"] == "table"
        cites = cite_sents(body, n, want_table=tab, limit=99)
        cap = meta["caption"]
        grid = meta.get("grid") or {}
        raw = schema_hit(cap)
        panels = panel_count(cap)
        r = {"key": key, "n": n, "kind": meta["kind"], "page": meta["page"],
             "cap": cap, "cites": len(cites), "sent": cites[:3], "grid": grid,
             "schema": bool(raw), "panels": panels,
             "grid_ok": bool(grid.get("rows")),
             # 正文引用句里点到的子图字母，用来和图注覆盖求差集
             "cite_letters": sorted({m.group(1).lower()
                                     for s in cites for m in FIG_PANEL_REF.finditer(s)})}
        r["score"] = (min(r["cites"] / 4.0, 3.0) + (1.5 if raw else 0)
                      + (2.5 if (tab and r["grid_ok"]) else 0))
        rows.append(r)
        if not tab:
            last_fig = r

    # 末图常是总结图，也常是「顺带一提」的补充图：只给 0.5 分，且引用不低于中位才加、
    # 才占保位名额（原 1.5 分 + 无条件保位会挤掉被反复引用的正文图）。
    fig_cites = sorted(r["cites"] for r in rows if r["kind"] == "figure")
    mid_cite = fig_cites[len(fig_cites) // 2] if fig_cites else 0
    last_ok = bool(last_fig) and last_fig["cites"] >= mid_cite
    if last_ok:
        last_fig["score"] += 0.5

    keep = max(1, int(max_figs)) + 2
    picked = []

    def take(r):
        if r and r["key"] not in {x["key"] for x in picked}:
            picked.append(r)

    # 方法示意图：整图就是示意的那张；一张都没有就不占这个名额（由分数补位）
    schema_pool = [r for r in rows if r["kind"] == "figure" and r["schema"]]
    take(max(schema_pool, key=lambda r: r["cites"], default=None))
    take(max(rows, key=lambda r: (r["cites"], -rows.index(r))) if rows else None)
    tabs = [r for r in rows if r["kind"] == "table" and r["grid_ok"]]
    if tabs:
        take(max(tabs, key=lambda r: len(r["grid"].get("rows") or [])))
    if last_ok:
        take(last_fig)
    for r in sorted(rows, key=lambda r: -r["score"]):
        if len(picked) >= keep:
            break
        take(r)

    keys = {r["key"] for r in picked[:keep]}
    return [r for r in rows if r["key"] in keys], [r for r in rows if r["key"] not in keys]


def cap_head(cap, limit=70):
    """图注开头一小段（落选图在素材包里只露这一行）。"""
    cap = re.sub(r"\s+", " ", strip_markup(cap or "")).strip()
    cap = re.sub(r"^(?:Fig(?:ure)?s?\.?|Tables?)\s*\d+\s*[.:)]?\s*", "", cap, flags=re.I)
    if len(cap) > limit:
        cap = cap[:limit].rstrip(" ,;.-—…") + "…"
    return cap


def figure_section(st, body, max_figs=3, workdir=""):
    """素材包里的「图表素材」段：候选给全图注，其余只给一行。"""
    cand, rest = figure_plan(st, body, max_figs)
    if not cand:
        if workdir:
            J.decide(workdir, "summary", "FIG_PICK", "没有可用的图表候选", msg="图表段为空")
        return [], 0
    if workdir:
        J.decide(workdir, "summary", "FIG_PICK",
                 " | ".join(f"{'表' if r['kind'] == 'table' else '图'}{r['n']} "
                            f"引用{r['cites']} 分{r['score']:.1f}" for r in cand),
                 f"候选 {len(cand)} 个，落选 {len(rest)} 个（最多选 {max_figs} 个）")
    lines = ["", f"## 图表素材（候选 {len(cand)} 个，最多选 {max(1, int(max_figs))} 个；"
                 "图注已按子图全部保留，别描述图注没写的视觉细节）"]
    gaps = []
    for r in cand:
        tag = "TAB" if r["kind"] == "table" else "FIG"
        packed = panel_cut(r["cap"])
        cap_l = panel_letters(r["cap"])
        # 「漏」按有独立说明段的子图算（panel_desc_letters）：行内引用本来就没有独立段落，
        # 拿它去比会把压缩后的正常结果报成丢失。
        lost = sorted(panel_desc_letters(r["cap"]) - panel_letters(packed))
        miss = sorted(set(r.get("cite_letters") or []) - cap_l)  # 正文提到、图注没写
        lines.append("")
        lines.append(f"[{tag} {r['n']}] 第 {r['page']} 页 · 正文引用 {r['cites']} 次 · "
                     f"图注 {len(r['cap'])} 字符"
                     + (f" · 子图 {'/'.join(sorted(cap_l))}" if cap_l and r["kind"] == "figure"
                        else ""))
        lines.append(f"图注: {packed}")
        if lost or miss:
            bits = []
            if lost:
                bits.append(f"素材包漏了 {'/'.join(x.upper() for x in lost)}")
            if miss:
                bits.append(f"正文提到但图注未写 {'/'.join(x.upper() for x in miss)}")
            lines.append(f"[PANEL] {'；'.join(bits)} —— 这些子图只能按正文说结论，"
                         "图上画了什么不要编")
            gaps.append((r, lost, miss))
        for s in r["sent"]:
            lines.append(f"[SENT] {strip_markup(s)[:200]}")
        g = (r.get("grid") or {})
        if r["kind"] == "table" and g.get("rows"):
            head = [str(c) for c in (g.get("header") or [])]
            lines.append(f"[GRID] 表头 {head[:8]}；{len(g['rows'])} 行 × "
                         f"{len(head)} 列（可据此说清对比维度，不必逐格复述）")
    if workdir and gaps:
        for r, lost, miss in gaps:
            bits = []
            if lost:
                bits.append("素材包漏 " + "/".join(x.upper() for x in lost))
            if miss:
                bits.append("正文提到但图注未写 " + "/".join(x.upper() for x in miss))
            J.issue(workdir, "summary", "FIG_PANEL_GAP", where=f"图{r['n']}",
                    detail="；".join(bits),
                    msg=f"图注 {len(r['cap'])} 字符，子图覆盖不全")
    if rest:
        # 落选图给一行图注开头：模型至少知道被砍掉的是什么，判断值不值得要回来。
        # 只给开头，不给全文 —— 全文没进上下文时不许凭印象写解读（会被 anchor 校验拒）。
        bits = []
        for r in rest[:10]:
            one = cap_head(r.get("cap"))
            bits.append(f"{'表' if r['kind'] == 'table' else '图'}{r['n']}"
                        f"(引用{r['cites']}次)" + (f" {one}" if one else ""))
        lines.append("")
        lines.append("[其他] " + " ｜ ".join(bits))
        lines.append("[其他] 以上只有图注开头，未展开。要选其中一张，先用 "
                     "`paper_grep` 传 `fig=<图号>` 取全文图注再写；没读到全文图注的图不许写解读。")
    return lines, len(cand)


def pack(workdir, budget=12000, out_file=None, figs=3):
    st = load_json(workdir, "structure.json")
    if not st:
        print("! 找不到 structure.json，先跑 prepare。")
        return 2
    blocks = st["blocks"]
    title, abstract, entries = scan(blocks)
    if not entries and not abstract:
        print("! 没扫到正文分节，可能是扫描版或双栏错位，需要人工看一下 structure.json。")
        return 2

    buckets, body_all = select(entries, abstract)
    subs = {"results": [], "methods": []}
    for e in entries:
        if e["kind"] == "subhead" and e["major"] in subs:
            if e["text"] not in subs[e["major"]] and len(subs[e["major"]]) < 14:
                subs[e["major"]].append(e["text"])

    map_line = ""
    if subs["results"]:
        map_line = "结果小节：" + " ｜ ".join(subs["results"][:12])
    meth_line = ""
    if subs["methods"]:
        meth_line = "方法小节：" + " / ".join(subs["methods"][:12])

    budget = max(2500, int(budget))
    total, lines, dropped = 0, [], 0
    for pri in range(6):
        for tag, text in buckets[pri]:
            item = f"[{tag}] {text}"
            if total + len(item) + 1 > budget:
                dropped += 1
                continue
            lines.append(item)
            total += len(item) + 1
    if map_line and total + len(map_line) < budget:
        lines.insert(1 if abstract else 0, "[MAP] " + map_line)

    fig_lines, fig_cand = ([], 0)
    if int(figs) > 0:
        fig_lines, fig_cand = figure_section(st, "\n".join(body_all), figs, workdir=workdir)

    meta = st.get("meta") or {}
    stats = st.get("stats") or {}
    journal, doi = "", ""
    # DOI / 期刊卷期在首页正文块里（meta.header_right/footer 抽取时基本是空的）：
    # 扫首页前若干块，用正则抓 DOI 和「年份;卷(期)」式期刊引文行。
    C = load_json(workdir, "content.json")
    if C:
        m = C.get("meta") or {}
        journal = (m.get("header_right") or "").strip()
        doi = (m.get("footer") or "").strip()
    if not doi:
        head_txt = " ".join((b.get("text") or "") for b in st.get("blocks", [])[:20])
        dm = re.search(r"\b(?:doi\s*[:=]?\s*)?(10\.\d{4,9}/\S+)", head_txt, re.I)
        if dm:
            doi = dm.group(1).rstrip(".,;)")
    if not journal:
        head_txt = " ".join((b.get("text") or "") for b in st.get("blocks", [])[:20])
        jm = re.search(r"\b((?:19|20)\d{2})\b[^\n]{0,60};\s*\d+\s*\(\d+\)", head_txt)
        if jm:
            journal = jm.group(0).strip()
    tm = re.search(r"\b10\.\d{4,9}/\S+", doi + " " + journal)
    if tm:
        doi = tm.group(0).rstrip(".,;)")
    if re.match(r"^https?://", journal):
        journal = ""

    J.decide(workdir, "summary", "PACK_BUDGET",
             f"实际 {total} 字符 / 预算 {budget}，丢弃 {dropped} 句，图表候选 {fig_cand} 个",
             "素材包已切好")
    head = [
        "# 论文摘要素材包",
        f"TITLE_EN: {strip_markup(title) or '（未识别到标题，请据摘要首句判断）'}",
        f"JOURNAL: {journal or '（未识别）'}",
        f"DOI: {doi or '（未识别）'}",
        f"SCALE: {stats.get('blocks', '?')} 块 / 图 {stats.get('figures', 0)} / 表 {stats.get('tables', 0)}",
        f"FIGS: 候选 {fig_cand} 个（最多选 {max(0, int(figs))} 个放进总结文档）",
        f"PDF: {meta.get('source', '（未知）')}",
        f"BUDGET: {total} 字符（超预算丢弃 {dropped} 句；五节所需信息已保留）",
        "",
        "## 素材（按优先级：摘要 → 引言 → 讨论 → 结果 → 方法，已剔除页眉与参考文献）",
    ]
    tail = ["", "## 术语对照（与本译文统一，勿自创译名）",
            terms_block("\n".join(body_all), workdir=workdir) or "（本文未命中术语表）"]
    if meth_line:
        tail += ["", "## " + meth_line]
    tail += [
        "",
        "## 你要做的事",
        "写五节中文总结 + 图表解读，**只输出一个 JSON**，不要任何解释文字：",
        "{",
        '  "title_en": "<照抄上面的 TITLE_EN，不要改写>",',
        '  "title_zh": "<中文标题，与已有译名一致；尚无译文时自拟>",',
        '  "sections": {',
        '    "purpose":    {"text": "…", "evidence": "<从素材里逐字复制的英文原句>"},',
        '    "methods":    {"text": "…", "evidence": "…"},',
        '    "difficulty": {"text": "…", "evidence": ["<难点的原文>", "<解法的原文>"]},',
        '    "novelty":    {"text": "…", "evidence": "…"},',
        '    "limitation": {"text": "…", "evidence": "…"}',
        "  },",
        '  "figures": [',
        '    {"n": 1, "why": "<≤40 字：它承载哪个主张>",',
        '     "read": "<逐子图介绍：回答什么问题 → 每个子图(a/b/c…)分别看什么、对照组、'
        '统计结论 → 支撑哪个结论；子图越多越长，最多 340 字>",',
        '     "anchor": "<从**该图图注**里逐字复制的英文片段>"}',
        "  ]",
        "}",
        "",
        "五节口径与字数：",
        "- purpose 研究目的 60–120 字：要解决什么科学问题、为什么重要",
        "- methods 研究方法 120–200 字：体系/数据/关键技术，3–5 个要点，不写操作细节",
        "- difficulty 研究难点与解决方案 100–200 字：**先说难点是什么**（当时的技术障碍、"
        "为什么此前做不了或做不准），**再说作者用什么办法绕过去**（具体手段 + "
        "为什么这样能解决那个难点）；两半都必须有，只写困难会被判不合格",
        "- novelty 创新点 80–150 字：与已有方法的**具体差异**，禁止「首次」这类空话",
        "- limitation 局限 50–120 字：作者自述的局限 + 样本/体系边界",
        "",
        "图表口径（没有合适的就写空数组，宁缺勿滥）：",
        "- 只从上面 [FIG n] / [TAB n] 候选里选，n 照抄图号；最多 "
        f"{max(0, int(figs))} 个，选承载核心主张的那几张；",
        "- read 先写「这张图回答什么问题」，然后**按子图顺序逐个介绍**："
        "对组图里的每一张子图（a、b、c…）单独说它画了什么、对照组是什么、"
        "关键统计与结论是什么，一个子图都不能跳过；",
        "- 图注里每个子图标注 `[a]`/`[b]`/`[c]` 的，都要在 read 里点出对应的 "
        "「子图 a/（a）…」并给一句话介绍；子图越多 read 可以越长；",
        "- 图注没分 panel 就只说整图结论，不要编子图；",
        "- anchor 必须从**该图自己的图注**里逐字复制一段连续英文（8–30 词；"
        "图注短就整句照抄），不是正文的句子；",
        "",
        "硬规则：",
        "1. evidence 必须从上面素材里**逐字复制**一句英文，不得改写、不得拼接、不得翻译；"
        "difficulty 可给两句的数组（难点一句、解法一句），每句仍须逐字照抄、各自独立；",
        "2. 素材里确无依据时，text 写「原文未明确说明」+ 你据何种线索判断，evidence 留空；",
        "3. 不得出现素材里没有的数字；",
        "4. 英文该不该留，按下面顺序判定，命中即停（不要靠「这是不是术语」猜）：",
        "   ① 术语对照里标了「原文照写」的（如 5′UTR）→ 照原样写，不加括注；",
        "   ② 术语对照有中文译名的 → 首次写「中文（English）」，此后只用中文；",
        "   ③ 全大写、或含数字、或含 ′ - 的记号（PCR、HEK293T、T7、5′-poly(A)）→ 原样写；",
        "   ④ 首字母大写的专名（Kozak、Drosophila、DAVID、Flybase）→ 原样写；",
        "   ⑤ 其余全小写实词（processivity、lysate、primer）→ 必须译；译名没把握就写"
        "「中文（English）」，只写英文算不合格；",
        "   ⑥ 拉丁短语与单位（in vitro、5 h、37 °C、p < 0.05）→ 原样写；",
        "5. 五节合计 410–790 字；",
        "6. 图表解读里不许描述图注没写的东西（颜色、曲线走势、箭头位置、坐标轴范围）——"
        "会被检查出来并要求重写；",
        "7. difficulty 写的是「难点 → 对策」的对应关系：只描述困难、不写作者怎么解决，"
        "会被检查出来并退回重写；与 methods 分工——methods 讲整体流程，这一节只讲"
        "「哪个障碍用什么手段绕过」。",
    ]
    lines = [strip_markup(x) for x in lines + fig_lines]
    head = [strip_markup(x) for x in head]
    text = "\n".join(head + lines + tail)
    path = out_file or os.path.join(workdir, "summary.src.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\n[已保存素材包] {path}（{len(text)} 字符）")
    return 0


# ---------------------------------------------------------------- 原文检索
def fig_detail(workdir, num):
    """展开某张图/表的完整图注 + 正文引用句：候选池外的图也能取到。

    兜底「代码没选中的图，模型永远看不见」这条：查到图注就能判断值不值得选，
    anchor 也能从这里逐字复制。
    """
    st = load_json(workdir, "structure.json")
    if not st:
        print("! 找不到 structure.json，先跑 prepare。")
        return 2
    idx = fig_index(st)
    if not idx:
        print("! 没识别到图区（可能是扫描版，或图注没抽出来）。")
        return 1
    n = re.sub(r"\D", "", str(num)) or str(num)
    keys = [k for k in ("fig" + n, "tab" + n) if k in idx]
    if not keys:
        print(f"! 没有编号 {n} 的图/表。现有的：" + ("、".join(sorted(idx)) or "（无）"))
        return 1
    _, abstract, entries = scan(st["blocks"])
    _, body_all = select(entries, abstract)          # 与 figure_plan 同一份正文，引用数才对得上
    body = "\n".join(body_all)
    for key in keys:
        meta = idx[key]
        tab = meta["kind"] == "table"
        tag = "表" if tab else "图"
        cap = meta.get("caption") or "（图注未抽出）"
        print(f"[{tag} {meta['n']}] 第 {meta.get('page')} 页 · 图注 {len(cap)} 字符 · "
              f"{panel_count(cap)} 个子图标记")
        print("图注: " + panel_cut(cap))     # 与素材包同一上限，别在这里另设更小的截断
        g = meta.get("grid") or {}
        if tab and g.get("rows"):
            print(f"[GRID] 表头 {(g.get('header') or [])[:10]}；{len(g['rows'])} 行")
        cs = cite_sents(body, meta["n"], want_table=tab, limit=6)
        if cs:
            print("正文引用：")
            for i, s in enumerate(cs, 1):
                print(f"  {i}. {strip_markup(s)[:300]}")
        else:
            print("正文引用：未找到（可能是新图，或只在图注里被提到）")
        print()
    print("[fig] anchor 从这里逐字复制（图注原文），不要用正文的句子。")
    return 0


def grep(workdir, pat, limit=20, secs=None):
    """按关键词回原文捞句子：素材包没给的内容，用它能当场补上。

    与 pack 共用 scan()/is_junk() 口径，所以捞回来的句子可以直接当 evidence 用，
    不会出现「素材包校验能过、原文校验不过」的两套标准。
    """
    st = load_json(workdir, "structure.json")
    if not st:
        print("! 找不到 structure.json，先跑 prepare。")
        return 2
    _, abstract, entries = scan(st["blocks"])
    try:
        rx = re.compile(pat, re.I)
    except re.error:
        rx = re.compile(re.escape(pat), re.I)

    def want(sec):
        if not secs:
            return True
        return any(sec.startswith(s.rstrip("s")) or s.rstrip("s") in sec for s in secs)

    hits, seen = [], set()

    def add(sec, s):
        k = re.sub(r"\W+", "", s)[:60].lower()
        if not k or k in seen:
            return
        seen.add(k)
        hits.append((sec, strip_markup(s)))          # 与素材包同一种干净形式，可直接复制

    if abstract and want("abstract"):
        for x in sentences(abstract):
            if rx.search(x):
                add("abstract", x)
    for e in entries:
        if e["kind"] in ("head", "subhead") or not want(e["major"]):
            continue
        if is_junk(e["text"]):
            continue
        for x in sentences(e["text"]):
            if len(x) >= 30 and rx.search(x):
                add(e["major"], x)

    if not hits:
        print(f"[grep] 没有命中（关键词：{pat}）。换个更宽的词，比如 "
              "challenge|difficult|limitation|we developed")
        return 1
    print(f"[grep] 命中 {len(hits)} 句（关键词：{pat}）；与素材包同一套清洗口径，可直接当 evidence")
    for i, (sec, s) in enumerate(hits[:limit], 1):
        print(f"{i}. [{sec}] {s[:420]}")
    if len(hits) > limit:
        print(f"…另有 {len(hits) - limit} 句未显示；换更具体的关键词，或调大 --limit")
    return 0


# ---------------------------------------------------------------- 校验
KEYS = [("purpose", "研究目的", 60, 120), ("methods", "研究方法", 120, 280),
        ("difficulty", "难点与解决方案", 100, 200), ("novelty", "创新点", 80, 150),
        ("limitation", "局限", 50, 120)]


# 解法表述：只认明确的「对策」语义，避免把「核糖体结合阶段」这类词误判成解法。
SOLVE_ZH = [
    re.compile(r"对策|解决办法|解决方案|解决之道|应对之策|弥补|绕开"),
    re.compile(r"(?:解决|克服|绕过|回避|规避)"),
    re.compile(r"(?:改用|改为|换用|替换|替代|换一种)"),
    re.compile(r"(?:作者|他们|该研究|本文|研究团队|团队)[^。；]{0,24}?"
               r"(?:开发|构建|设计|提出|采用|建立|搭建|训练|引入|整合|借助|利用|组合|改造)"),
    re.compile(r"为此|于是|转而|只有这样"),
    re.compile(r"通过(?:引入|构建|设计|采用|建立|搭建|训练|整合|结合|改造|优化|添加|修改)"),
]
NEG_HEAD = re.compile(r"难以|无法|不能|尚未|没有|缺少|缺乏|不足以")


def has_solution(text):
    """是否写出了作者的对策（反向排除「难以解决」「无法克服」这类只讲困难的说法）。"""
    for rx in SOLVE_ZH:
        for m in rx.finditer(text):
            if not NEG_HEAD.search(text[max(0, m.start() - 4):m.start()]):
                return True
    return False


def load_summary(workdir):
    p = os.path.join(workdir, "summary.json")
    if not os.path.exists(p):
        return None, p
    return json.load(open(p, encoding="utf-8")), p


def source_text(workdir):
    st = load_json(workdir, "structure.json")
    if not st:
        return ""
    return "\n".join(b.get("text") or "" for b in st["blocks"])


def check(workdir):
    S, path = load_summary(workdir)
    if not S:
        print(f"! 找不到 {path}")
        return 2
    sec = S.get("sections") or {}
    src = source_text(workdir)
    if not src:
        print("! 找不到 structure.json，无法核对证据与数字。")
        return 2
    nsrc = norm_loose(src)
    from verify import key_nums
    snums = key_nums(src)

    fails, warns, lines, texts = [], [], [], {}

    def _code(msg):
        """把自由文本归成稳定 code —— 日志按 code 聚合，措辞改了历史才对得上。"""
        for kw, c in (("缺失", "SUMMARY_SECTION_MISSING"), ("超出", "SUMMARY_LEN"),
                      ("解法", "SUMMARY_NO_SOLUTION"), ("困难", "SUMMARY_NO_SOLUTION"),
                      ("anchor", "SUMMARY_ANCHOR"), ("数字", "SUMMARY_NUM"),
                      ("子图", "SUMMARY_PANEL"), ("视觉细节", "SUMMARY_VISUAL"),
                      ("title_zh", "SUMMARY_TITLE")):
            if kw in msg:
                return c
        return "SUMMARY_OTHER"

    def _where(msg):
        """定位取「哪一节 / 哪张图」，且不带数字 —— 数字一变就会分裂成新条目。"""
        m = re.match(r"(图|表)\s*(\d+)", msg)
        if m:
            return m.group(1) + m.group(2)
        m = re.match(r"[\u4e00-\u9fff]{2,10}", msg)
        if not m:
            return ""
        w = m.group(0)
        return w[:-2] if w.endswith("缺失") and len(w) > 4 else w

    def _flush():
        """落盘。check 有两处提前返回（节缺失 / 全部跑完），两处都要走这里。"""
        for msg in fails:
            J.issue(workdir, "summary_check", _code(msg), where=_where(msg),
                    detail=msg[:200], msg=msg, level="error")
        for msg in warns:
            J.issue(workdir, "summary_check", _code(msg), where=_where(msg),
                    detail=msg[:200], msg=msg)

    # 1 节齐全 + 字数
    for key, label, lo, hi in KEYS:
        node = sec.get(key)
        if isinstance(node, str):
            node = {"text": node, "evidence": ""}
        t = ((node or {}).get("text") or "").strip() if isinstance(node, dict) else ""
        raw = (node or {}).get("evidence") if isinstance(node, dict) else ""
        evs = [x.strip() for x in (raw if isinstance(raw, list) else [raw])
               if isinstance(x, str) and x.strip()]
        if not t:
            fails.append(f"{label}缺失")
            lines.append(f"  x {label:6s} 缺失")
            continue
        texts[key] = (t, evs)
        n = len(t)
        ok = lo <= n <= hi
        lines.append(f"  {'v' if ok else '!'} {label:6s} {n:3d} 字（{lo}-{hi}）")
        if not ok:
            warns.append(f"{label} {n} 字，超出 {lo}-{hi}")

    if fails:
        _flush()
        print("\n".join(lines))
        print("\n[不合格] " + "；".join(fails))
        return 2

    # 1b 难点必须带解法：只写困难、不写作者如何解决 → 不合格
    d_txt = texts.get("difficulty", ("", []))[0]
    if not has_solution(d_txt):
        fails.append("「难点与解决方案」只写了困难，没写作者如何解决")
        lines.append("  x 难点→解法    未见解法表述（对策/解决/改用/替代/为此/作者+手段），"
                     "请补上作者的对策")
    else:
        lines.append("  v 难点→解法    含解法表述")
        if "未明确" in d_txt:
            warns.append("难点一节的解法部分标为「原文未明确说明」，请复核是否真无依据")
        if len(d_txt) < 140:
            warns.append("难点与解决方案偏短，解法可能写得太笼统")

    if fails:
        print("\n".join(lines))
        print("\n[不合格] " + "；".join(fails))
        return 2

    # 2 证据逐字命中
    # 素材包给模型看的是 strip_markup 后的干净句，原文里却带 ~{44} 这类上标标记；
    # 两个基准都算命中，否则照素材包原样复制的证据会被误判成「改写」。
    src_plain = re.sub(r"~\{[^{}]*\}", "", src)
    ns_src, ns_plain = squash(src), squash(src_plain)
    bad_ev = []
    for key, label, _, _ in KEYS:
        t, evs = texts[key]
        if not evs:
            warns.append(f"{label} 无证据" if "未明确" in t else f"{label} 缺 evidence")
            continue
        if any(not (squash(e)[:200] in ns_src or squash(e)[:200] in ns_plain) for e in evs):
            bad_ev.append(label)
    if bad_ev:
        fails.append("证据未命中原文：" + "、".join(bad_ev))
        lines.append(f"  x 证据逐字命中  失败：{'、'.join(bad_ev)}（改写或拼接了，请逐字复制原句）")
    else:
        lines.append("  v 证据逐字命中")

    # 3 数字白名单
    all_text = "".join(t for t, _ in texts.values())
    extra = sorted(key_nums(all_text) - snums)
    if extra:
        warns.append("数字未在原文出现：" + "、".join(extra[:8]))
        lines.append(f"  ! 数字核对    多出 {len(extra)} 个：{'、'.join(extra[:8])}")
    else:
        lines.append("  v 数字核对")

    # 4 中文纯度（术语密集的段落中文占比天然偏低，真正管用下面那条连续英文检测）
    worst = min(cjk_ratio(t) for t, _ in texts.values())
    if worst < 0.55:
        warns.append(f"中文占比最低 {worst:.2f}，可能夹了未译内容")
        lines.append(f"  ! 中文占比    {worst:.2f}（阈值 0.55）")
    else:
        lines.append(f"  v 中文占比    {worst:.2f}")
    runs = re.findall(r"\b(?:[A-Za-z][A-Za-z\-]{2,})(?:\s+(?:[A-Za-z][A-Za-z\-]{2,})){3,}\b", all_text)
    if runs:
        warns.append(f"疑似未译英文片段 {len(runs)} 处，如「{runs[0][:40]}」")

    # 4b 术语括注：保留英文时必须写「中文（English）」，只写英文算不合格
    bare = []
    for key, label, _, _ in KEYS:
        bare += [(label, w) for w in bare_en(texts[key][0])]
    for f in (S.get("figures") or []):
        if isinstance(f, dict):
            tag = f"图 {f.get('n')}"
            bare += [(tag, w) for w in bare_en(f"{f.get('read') or ''} {f.get('why') or ''}")]
    bare += [("中文标题", w) for w in bare_en(S.get("title_zh") or "")]
    if bare:
        groups = {}
        for where, w in bare:
            groups.setdefault(w, []).append(where)
        detail = "；".join(f"{w} ×{len(ws)}（{'、'.join(ws[:4])}）"
                           for w, ws in list(groups.items())[:4])
        others = f" 等 {len(groups)} 个词" if len(groups) > 4 else ""
        fails.append(f"术语只写了英文：{detail}{others}")
        lines.append(f"  x 术语括注    {detail}{others} → 英文须配中文，写成「中文（English）」")
    else:
        lines.append("  v 术语括注")

    # 5 标题可溯源
    t_en = (S.get("title_en") or "").strip()
    if not t_en:
        warns.append("缺 title_en")
    elif squash(t_en)[:80] not in squash(src):
        warns.append("title_en 未能在原文中找到（可能被改写）")
        lines.append("  ! 英文标题溯源  失败")
    else:
        lines.append("  v 英文标题溯源")

    # 6 节间重复
    grams, dup = {}, 0
    for key, _, _, _ in KEYS:
        t = re.sub(r"\s+", "", texts[key][0])
        g = {t[i:i + 25] for i in range(max(0, len(t) - 24))}
        for gs in grams.values():
            if g & gs:
                dup += 1
        grams[key] = g
    if dup:
        warns.append(f"{dup} 对节内容有 ≥25 字重复，疑似互相抄")
        lines.append(f"  ! 节间重复    命中 {dup} 对")

    # 7–11 图表：存在性 / 图注溯源 / 子图 / 数字 / 视觉细节
    st = load_json(workdir, "structure.json") or {}
    idx = fig_index(st)
    figs = S.get("figures")
    if figs is None:
        if idx:
            warns.append("本文有图表但没给 figures（要放图就选 1–3 张）")
    elif not isinstance(figs, list):
        fails.append("figures 必须是数组")
    elif figs:
        fig_lines, seen = [], []
        for f in figs:
            if not isinstance(f, dict):
                fails.append("figures 里每项必须是对象")
                break
            n = str(f.get("n") or "").strip()
            read = (f.get("read") or "").strip()
            why = (f.get("why") or "").strip()
            anc = (f.get("anchor") or "").strip()
            want_tab = str(f.get("kind") or "").lower() in ("tab", "table", "表")
            key = ("tab" if want_tab else "fig") + n
            if key not in idx:                       # 类型猜错时回落到另一种
                key = ("fig" if want_tab else "tab") + n
            if not n or key not in idx:
                fails.append(f"图 {n or '（缺 n）'} 不在原文图区里（图形摘要/无主图区不能选）")
                continue
            if key in seen:
                fails.append(f"图 {n} 重复选了")
                continue
            seen.append(key)
            cap = idx[key]["caption"]
            tag = "表" if idx[key]["kind"] == "table" else "图"
            # 图解读字数上限按子图数动态放宽：4 子图要 300，2 子图 220 即可
            n_panels = len({m.group(1).lower() for m in PANEL_A.finditer(cap)}
                           | {m.group(1).lower() for m in PANEL_B.finditer(cap)})
            fig_max = min(FIG_MAX + max(0, n_panels - 1) * 20, 340)
            ok_len = FIG_MIN <= len(read) <= fig_max
            fig_lines.append(f"  {'v' if ok_len else '!'} {tag} {n}   解读 {len(read):3d} 字"
                             f"（{FIG_MIN}-{fig_max}）" + ("" if why else " · 缺 why"))
            if not ok_len:
                warns.append(f"{tag}{n} 解读 {len(read)} 字，超出 {FIG_MIN}-{fig_max}")
            if not why:
                warns.append(f"{tag}{n} 缺 why（为什么值得看）")
            elif len(why) > 60:
                warns.append(f"{tag}{n} 的 why 太长（{len(why)} 字），压到 40 字内")
            if not anc:
                fails.append(f"{tag}{n} 缺 anchor：必须从该图图注里逐字复制一段")
            elif squash(anc)[:120] not in squash(cap):
                fails.append(f"{tag}{n} 的 anchor 没在该图图注里逐字出现（可能照正文或改写）")
            # 子图字母必须在图注里出现过
            letters = {g for m in PANEL_ZH.finditer(read) for g in m.groups() if g}
            cap_marks = ({m.group(1).lower() for m in PANEL_A.finditer(cap)}
                         | {m.group(1).lower() for m in PANEL_B.finditer(cap)})
            if letters and not cap_marks:
                warns.append(f"{tag}{n} 图注未分 panel，解读里别写子图 {''.join(sorted(letters))}")
            elif letters - cap_marks:
                warns.append(f"{tag}{n} 提到子图 {''.join(sorted(letters - cap_marks))}，图注里没有")
            # 方案 E：组图的每个子图都应被逐一介绍。图注标了 [a]/[b]/[c] 的，
            # 解读里若漏掉某个子图（只概括整图、没逐一说），报缺漏。
            missed = cap_marks - letters
            if cap_marks and len(cap_marks) >= 2 and missed:
                warns.append(f"{tag}{n} 组图有子图 {''.join(sorted(missed))} 未在解读里逐一介绍，"
                             f"请按 a→b→c 顺序补齐每张子图的作用")
            # 数字：全文 + 该图图注，别处的一律不许出现
            extra_f = sorted(key_nums(read + why) - (snums | key_nums(cap)))
            if extra_f:
                warns.append(f"{tag}{n} 数字未在该图图注/原文出现：" + "、".join(extra_f[:5]))
            # 视觉细节：中文说了颜色/曲线/坐标轴，图注里却没有对应英文
            miss = [zh for zh, en in VIS_ZH
                    if re.search(zh, read) and not re.search(en, cap, re.I)]
            if miss:
                warns.append(f"{tag}{n} 解读描述了图注未提及的视觉细节：" + "、".join(miss[:4]))
        if fig_lines:
            lines.extend(["  · 图表"] + fig_lines)

    # 中文标题与译文一致性
    C = load_json(workdir, "content.json")
    if C:
        for it in (C.get("front") or []):
            if it.get("t") == "title" and (it.get("text") or "").strip():
                zh = it["text"].strip()
                if cjk_ratio(zh) > 0.5 and zh != (S.get("title_zh") or "").strip():
                    warns.append(f"title_zh 与译文标题不一致：{zh}")
                break

    total = sum(len(t) for t, _ in texts.values())
    print("\n".join(lines))
    print(f"  · 合计 {total} 字")
    _flush()

    if fails:
        print("\n[不合格] " + "；".join(fails))
        return 2
    if warns:
        print("\n[有疑点] " + "；".join(warns))
        return 1
    print("\n[通过] 五节齐全，证据全部命中原文。")
    return 0


# ---------------------------------------------------------------- 意图判定
def intent(text):
    low = (text or "").lower()
    hit = [w for w in INTENT_ZH if w in low]
    hit += [w for w in INTENT_EN if w in low]
    trans = [w for w in TRANS_ZH if w in low]
    trans += [w for w in TRANS_EN if w in low]
    if hit:
        print(f"INTENT: HIT ({hit[0]})")
    else:
        print("INTENT: MISS（未出现总结/概括类词，不生成总结）")
    # 同时出现「翻译」= 两件都做；只出现「翻译」= 只翻译，不总结
    print(f"TRANS: {'YES' if trans else 'NO'}" + (f" ({trans[0]})" if trans else ""))
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--intent":
        return intent(argv[1] if len(argv) > 1 else "")

    budget, figs, positional, i = 12000, 3, [], 0
    grep_kw, gsecs, glimit, fig_no = None, None, 20, None
    while i < len(argv):
        a = argv[i]
        if a in ("--budget", "--figs", "--grep", "--limit", "--sec", "--fig") and i + 1 < len(argv):
            v = argv[i + 1]
            if a == "--budget":
                budget = int(v)
            elif a == "--figs":
                figs = int(v)
            elif a == "--grep":
                grep_kw = v
            elif a == "--fig":
                fig_no = v
            elif a == "--limit":
                glimit = int(v)
            else:
                gsecs = [x.strip() for x in v.split(",") if x.strip()]
            i += 2
            continue
        if not a.startswith("-"):
            positional.append(a)
        i += 1
    if not positional:
        print(__doc__)
        return 1
    workdir = positional[0]
    if not os.path.isdir(workdir):
        print("! 工作目录不存在:", workdir)
        return 1
    if fig_no is None and "--fig" in argv:
        print("! --fig 后面要跟图号，例如 `--fig 4`。")
        return 1
    if fig_no is not None:
        return fig_detail(workdir, fig_no)
    if grep_kw is not None:
        return grep(workdir, grep_kw, max(1, min(200, glimit)), gsecs)
    if "--check" in argv:
        return check(workdir)
    return pack(workdir, budget, figs=max(0, min(6, figs)))


if __name__ == "__main__":
    sys.exit(main())
