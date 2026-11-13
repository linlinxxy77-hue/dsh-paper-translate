# -*- coding: utf-8 -*-
"""summarize.py —— 文献总结：素材包、机械校验、意图判定。

用法:
    python summarize.py <workdir> --pack [--budget 10000] [--figs 3]  # 切素材包并落盘 summary.src.txt
    python summarize.py <workdir> --check                    # 校验 summary.json（六项 + 图表五项）
    python summarize.py --intent "帮我总结一下这篇"           # 关键词判定，不读文件

设计要点:
  - 素材包只喂「写总结必需的句子」（默认 10k 字符），不喂全文：单篇约 3.5k token。
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
FIND_WORDS = re.compile(
    r"\b(we (?:found|show|showed|demonstrate|demonstrated|report|identify|identified|"
    r"establish|established|discover|discovered|reveal|revealed)|here we|our results)\b", re.I)
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
FIG_REF = re.compile(r"\b(?:Fig(?:ure)?\.?)\s*(\d+)|图\s*(\d+)", re.I)
TAB_REF = re.compile(r"\b(?:Table)\s*(\d+)|表\s*(\d+)", re.I)
PANEL_A = re.compile(r"(?:^|(?<=[.;:!?])\s|\s{2,})([a-h])\s+(?=[A-Z(\[])")
PANEL_B = re.compile(r"\(([A-H])\)")
SCHEMA_WORDS = re.compile(r"\b(schematic|work ?flow|flow ?chart|overview|experimental design|"
                          r"pipeline|conceptual|procedures?)\b", re.I)
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
    """首页最长的一块（≥500 字符、非跑页眉）视为摘要。"""
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
def terms_block(body_text, limit=18):
    """只带源文命中的术语，保证总结与译文口径一致。"""
    try:
        import glossary as G
        rows = []
        for term in G.load_terms():
            hits = G.count_hits(body_text, term)
            if hits:
                en = re.split(r"\s*/\s*", term["en"])[0].strip()
                rows.append((hits, en, G.primary_zh(term)))
        rows.sort(key=lambda x: -x[0])
        return "\n".join(f"- {en} → {zh}" for _, en, zh in rows[:limit])
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

    def put(pri, tag, text):
        key = re.sub(r"\W+", "", text)[:60].lower()
        if not key or key in used:
            return
        used.add(key)
        b[pri].append((tag, text))

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
        elif m == "intro":
            if not intro_first:
                put(1, "INTRO", ss[0][:520])
                intro_first = True
                for x in ss[1:]:
                    if GAP_WORDS.search(x) and len(x) > 50:
                        put(2, "GAP", x[:320])
                        break
            elif len(b[2]) < 2:
                for x in ss:
                    if GAP_WORDS.search(x) and len(x) > 50:
                        put(2, "GAP", x[:320])
                        break
        elif m == "discussion":
            if not disc_first:
                put(1, "DISC", ss[0][:560])
                disc_first = True
            if len(b[3]) < 4:
                for x in ss:
                    if DISC_WORDS.search(x) and len(x) > 50:
                        put(3, "LIMIT", x[:300])
                        break
        elif m == "results":
            if len(b[4]) < 14:
                put(4, "RES", ss[0][:260])
            if len(b[3]) < 12:
                for x in ss[:8]:
                    if FIND_WORDS.search(x) and x != ss[0] and 60 < len(x) < 320:
                        put(3, "FIND", x[:260])
                        break
        elif m == "methods":
            if len(b[5]) < 3 and 60 < len(ss[0]) < 320:
                put(5, "METH", ss[0][:220])
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
    """图号：优先 caption_num，其次 fig_name 里的数字，最后退 id。"""
    n = f.get("caption_num")
    if n:
        return str(n)
    name = f.get("fig_name") or ""
    m = re.search(r"\d+", name)
    if m:
        return m.group(0)
    return str(f.get("id"))


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


def panel_cut(cap, limit=1000):
    """超长图注按 panel 截断：首句 + 每个 panel 的第一句，保住「各子图说什么」。"""
    cap = strip_markup(cap)
    if len(cap) <= limit:
        return cap
    marks = sorted([(m.start(1), m.group(1)) for m in PANEL_A.finditer(cap)]
                   + [(m.start(1), m.group(1).lower()) for m in PANEL_B.finditer(cap)])
    if len(marks) < 2:
        return cap[:limit].rstrip(" ,;") + " …"
    head = cap[:marks[0][0]].strip().rstrip(" ([").strip()
    out, used = [head], len(head)
    for i, (pos, letter) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(cap)
        seg = cap[pos:end]
        seg = re.sub(r"^[a-hA-H]\s*[).:]?\s*", "", seg)
        ss = sentences(seg)
        one = (ss[0] if ss else seg).strip()[:200]
        if used + len(one) + 4 > limit:
            break
        out.append(f"[{letter}] {one}")
        used += len(one) + 4
    return re.sub(r"\(\s*\[([a-h])\]", r"[\1]", " ".join(out))


def cite_sents(body, n, want_table=False, limit=3):
    """正文里引用该图/表的句子（排除 Supplementary 引用），最多 limit 条。"""
    rx = TAB_REF if want_table else FIG_REF
    hits = []
    for s in sentences(body):
        found = [g for m in rx.finditer(s) for g in m.groups() if g]
        if n in found and not re.search(r"(?i)supplementary\s+(?:fig|table)", s) \
                and 40 < len(s) < 400:
            hits.append(s.strip())
    return hits[:limit]


def figure_plan(st, body, max_figs=3):
    """挑候选图：先按「方法示意 / 引用最多 / 有网格的表 / 末图」各保一个，再按分数补位。

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
        r = {"key": key, "n": n, "kind": meta["kind"], "page": meta["page"],
             "cap": cap, "cites": len(cites), "sent": cites[:3], "grid": grid,
             "schema": bool(SCHEMA_WORDS.search(cap[:400])),
             "grid_ok": bool(grid.get("rows"))}
        r["score"] = (min(r["cites"] / 4.0, 3.0) + (1.5 if r["schema"] else 0)
                      + (2.5 if (tab and r["grid_ok"]) else 0))
        rows.append(r)
        if not tab:
            last_fig = r
    if last_fig:
        last_fig["score"] += 1.5

    keep = max(1, int(max_figs)) + 2
    picked = []

    def take(r):
        if r and r["key"] not in {x["key"] for x in picked}:
            picked.append(r)

    take(next((r for r in rows if r["schema"] and r["kind"] == "figure"), None))
    take(max(rows, key=lambda r: (r["cites"], -rows.index(r))) if rows else None)
    tabs = [r for r in rows if r["kind"] == "table" and r["grid_ok"]]
    if tabs:
        take(max(tabs, key=lambda r: len(r["grid"].get("rows") or [])))
    take(last_fig)
    for r in sorted(rows, key=lambda r: -r["score"]):
        if len(picked) >= keep:
            break
        take(r)

    keys = {r["key"] for r in picked[:keep]}
    return [r for r in rows if r["key"] in keys], [r for r in rows if r["key"] not in keys]


def figure_section(st, body, max_figs=3):
    """素材包里的「图表素材」段：候选给全图注，其余只给一行。"""
    cand, rest = figure_plan(st, body, max_figs)
    if not cand:
        return [], 0
    lines = ["", f"## 图表素材（候选 {len(cand)} 个，最多选 {max(1, int(max_figs))} 个；"
                 "图注已按子图保留，别描述图注没写的视觉细节）"]
    for r in cand:
        tag = "TAB" if r["kind"] == "table" else "FIG"
        lines.append("")
        lines.append(f"[{tag} {r['n']}] 第 {r['page']} 页 · 正文引用 {r['cites']} 次 · "
                     f"图注 {len(r['cap'])} 字符")
        lines.append(f"图注: {panel_cut(r['cap'], 1000)}")
        for s in r["sent"]:
            lines.append(f"[SENT] {strip_markup(s)[:200]}")
        g = (r.get("grid") or {})
        if r["kind"] == "table" and g.get("rows"):
            head = [str(c) for c in (g.get("header") or [])]
            lines.append(f"[GRID] 表头 {head[:8]}；{len(g['rows'])} 行 × "
                         f"{len(head)} 列（可据此说清对比维度，不必逐格复述）")
    if rest:
        brief = " ｜ ".join(f"{'表' if r['kind'] == 'table' else '图'}{r['n']}"
                            f"(引用{r['cites']}次)" for r in rest[:10])
        lines.append("")
        lines.append(f"[其他] {brief} —— 未展开。只能从上面的候选里选：没读到图注的图不许写解读。")
    return lines, len(cand)


def pack(workdir, budget=10000, out_file=None, figs=3):
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
        fig_lines, fig_cand = figure_section(st, "\n".join(body_all), figs)

    meta = st.get("meta") or {}
    stats = st.get("stats") or {}
    journal, doi = "", ""
    C = load_json(workdir, "content.json")
    if C:
        m = C.get("meta") or {}
        journal = (m.get("header_right") or "").strip()
        doi = (m.get("footer") or "").strip()
    tm = re.search(r"\b10\.\d{4,9}/\S+", doi + " " + journal)
    if tm:
        doi = tm.group(0).rstrip(".,;)")
    if re.match(r"^https?://", journal):
        journal = ""

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
    tail = ["", "## 术语对照（与本译文统一，勿自创译名）", terms_block("\n".join(body_all)) or "（本文未命中术语表）"]
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
        '    "difficulty": {"text": "…", "evidence": "…"},',
        '    "novelty":    {"text": "…", "evidence": "…"},',
        '    "limitation": {"text": "…", "evidence": "…"}',
        "  },",
        '  "figures": [',
        '    {"n": 1, "why": "<≤40 字：它承载哪个主张>",',
        '     "read": "<90–240 字：回答什么问题 → 按子图顺序说看什么 → 支撑哪个结论>",',
        '     "anchor": "<从**该图图注**里逐字复制的英文片段>"}',
        "  ]",
        "}",
        "",
        "五节口径与字数：",
        "- purpose 研究目的 60–120 字：要解决什么科学问题、为什么重要",
        "- methods 研究方法 120–200 字：体系/数据/关键技术，3–5 个要点，不写操作细节",
        "- difficulty 研究难点 60–120 字：当时的技术障碍，为什么此前做不了或做不准",
        "- novelty 创新点 80–150 字：与已有方法的**具体差异**，禁止「首次」这类空话",
        "- limitation 局限 50–120 字：作者自述的局限 + 样本/体系边界",
        "",
        "图表口径（没有合适的就写空数组，宁缺勿滥）：",
        "- 只从上面 [FIG n] / [TAB n] 候选里选，n 照抄图号；最多 "
        f"{max(0, int(figs))} 个，选承载核心主张的那几张；",
        "- read 先写「这张图回答什么问题」，再按子图（a→b→c）说展示了什么对照、"
        "关键统计与结论，最后一句说它支撑五节里的哪一点；",
        "- 图注没分 panel 就只说整图结论，不要编子图；",
        "- anchor 必须从**该图自己的图注**里逐字复制一段连续英文（8–30 词；"
        "图注短就整句照抄），不是正文的句子；",
        "",
        "硬规则：",
        "1. evidence 必须从上面素材里**逐字复制**一句英文，不得改写、不得拼接、不得翻译；",
        "2. 素材里确无依据时，text 写「原文未明确说明」+ 你据何种线索判断，evidence 留空；",
        "3. 不得出现素材里没有的数字；",
        "4. 术语按上面的「术语对照」，不要自创译名；",
        "5. 五节合计 370–710 字，超过会被判不合格；",
        "6. 图表解读里不许描述图注没写的东西（颜色、曲线走势、箭头位置、坐标轴范围）——"
        "会被检查出来并要求重写。",
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


# ---------------------------------------------------------------- 校验
KEYS = [("purpose", "研究目的", 60, 120), ("methods", "研究方法", 120, 200),
        ("difficulty", "研究难点", 60, 120), ("novelty", "创新点", 80, 150),
        ("limitation", "局限", 50, 120)]


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

    # 1 节齐全 + 字数
    for key, label, lo, hi in KEYS:
        node = sec.get(key)
        if isinstance(node, str):
            node = {"text": node, "evidence": ""}
        t = ((node or {}).get("text") or "").strip() if isinstance(node, dict) else ""
        ev = ((node or {}).get("evidence") or "").strip() if isinstance(node, dict) else ""
        if not t:
            fails.append(f"{label}缺失")
            lines.append(f"  x {label:6s} 缺失")
            continue
        texts[key] = (t, ev)
        n = len(t)
        ok = lo <= n <= hi
        lines.append(f"  {'v' if ok else '!'} {label:6s} {n:3d} 字（{lo}-{hi}）")
        if not ok:
            warns.append(f"{label} {n} 字，超出 {lo}-{hi}")

    if fails:
        print("\n".join(lines))
        print("\n[不合格] " + "；".join(fails))
        return 2

    # 2 证据逐字命中
    bad_ev = []
    for key, label, _, _ in KEYS:
        t, ev = texts[key]
        if not ev:
            warns.append(f"{label} 无证据" if "未明确" in t else f"{label} 缺 evidence")
            continue
        if squash(ev)[:200] not in squash(src):
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
            ok_len = FIG_MIN <= len(read) <= FIG_MAX
            fig_lines.append(f"  {'v' if ok_len else '!'} {tag} {n}   解读 {len(read):3d} 字"
                             f"（{FIG_MIN}-{FIG_MAX}）" + ("" if why else " · 缺 why"))
            if not ok_len:
                warns.append(f"{tag}{n} 解读 {len(read)} 字，超出 {FIG_MIN}-{FIG_MAX}")
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

    budget, figs, positional, i = 10000, 3, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--budget" and i + 1 < len(argv):
            budget = int(argv[i + 1])
            i += 2
            continue
        if a == "--figs" and i + 1 < len(argv):
            figs = int(argv[i + 1])
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
    if "--check" in argv:
        return check(workdir)
    return pack(workdir, budget, figs=max(0, min(6, figs)))


if __name__ == "__main__":
    sys.exit(main())
