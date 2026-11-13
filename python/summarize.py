# -*- coding: utf-8 -*-
"""summarize.py —— 文献总结：素材包、机械校验、意图判定。

用法:
    python summarize.py <workdir> --pack [--budget 10000]   # 切素材包并落盘 summary.src.txt
    python summarize.py <workdir> --check                    # 校验 summary.json（六项）
    python summarize.py --intent "帮我总结一下这篇"           # 关键词判定，不读文件

设计要点:
  - 素材包只喂「写总结必需的句子」（默认 10k 字符），不喂全文：单篇约 3.5k token。
  - 用英文原文，不用译文，避免把译文的错误再传一层；术语靠 glossary 对齐口径。
  - 每节必须附一句原文证据（evidence），--check 要求它逐字命中原文 —— 编不出来。
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

INTENT_ZH = ["总结", "概括", "归纳", "提炼", "要点", "脉络", "大意", "讲了什么",
             "说了什么", "核心结论", "主要结论", "写个摘要", "摘要一下", "综述一下"]
INTENT_EN = ["summary", "summarize", "summarise", "tldr", "tl;dr", "overview",
             "key points", "main findings", "abstract of"]

ABBR = re.compile(r"\b(e\.g|i\.e|et al|Fig|Figs|Eq|Eqs|Ref|Refs|vs|approx|ca|cf|"
                  r"No|Dr|Prof|St|Inc|Ltd|resp|Suppl|Extended Data)\.")
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


def pack(workdir, budget=10000, out_file=None):
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
        "写五节中文总结，**只输出一个 JSON**，不要任何解释文字：",
        "{",
        '  "title_en": "<照抄上面的 TITLE_EN，不要改写>",',
        '  "title_zh": "<中文标题，与已有译名一致；尚无译文时自拟>",',
        '  "sections": {',
        '    "purpose":    {"text": "…", "evidence": "<从素材里逐字复制的英文原句>"},',
        '    "methods":    {"text": "…", "evidence": "…"},',
        '    "difficulty": {"text": "…", "evidence": "…"},',
        '    "novelty":    {"text": "…", "evidence": "…"},',
        '    "limitation": {"text": "…", "evidence": "…"}',
        "  }",
        "}",
        "",
        "五节口径与字数：",
        "- purpose 研究目的 60–120 字：要解决什么科学问题、为什么重要",
        "- methods 研究方法 120–200 字：体系/数据/关键技术，3–5 个要点，不写操作细节",
        "- difficulty 研究难点 60–120 字：当时的技术障碍，为什么此前做不了或做不准",
        "- novelty 创新点 80–150 字：与已有方法的**具体差异**，禁止「首次」这类空话",
        "- limitation 局限 50–120 字：作者自述的局限 + 样本/体系边界",
        "",
        "硬规则：",
        "1. evidence 必须从上面素材里**逐字复制**一句英文，不得改写、不得拼接、不得翻译；",
        "2. 素材里确无依据时，text 写「原文未明确说明」+ 你据何种线索判断，evidence 留空；",
        "3. 不得出现素材里没有的数字；",
        "4. 术语按上面的「术语对照」，不要自创译名；",
        "5. 五节合计 370–710 字，超过会被判不合格。",
    ]
    lines = [strip_markup(x) for x in lines]
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

    # 4 中文纯度
    worst = min(cjk_ratio(t) for t, _ in texts.values())
    if worst < 0.8:
        warns.append(f"中文占比最低 {worst:.2f}，可能夹了未译内容")
        lines.append(f"  ! 中文占比    {worst:.2f}（阈值 0.80）")
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
    if hit:
        print(f"INTENT: HIT ({hit[0]})")
    else:
        print("INTENT: MISS（未出现总结/概括类词，不生成总结）")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--intent":
        return intent(argv[1] if len(argv) > 1 else "")

    budget, positional, i = 10000, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--budget" and i + 1 < len(argv):
            budget = int(argv[i + 1])
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
    return pack(workdir, budget)


if __name__ == "__main__":
    sys.exit(main())
