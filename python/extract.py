# -*- coding: utf-8 -*-
"""extract.py —— PDF 一次性结构化抽取（v3）。

用法:
    python extract.py <input.pdf> <outdir>

产出 <outdir>/structure.json:
    meta      页数、页面尺寸、正文字号、版式（single/double/multi）
    blocks    阅读顺序排列的文本块（title/h1/h2/p/eq/caption/ref/refhead）
    figures   图区(page,bbox,kind,role,caption_num,caption_pos,fig_name) + 图注块索引
    stats     统计摘要（agent 只读这段，不回灌全文）

v3 相对 v2 的修复：
  1. 块内按字号档位 + 全粗体短行重切 —— 解决「小节标题与首段并在同一块」。
  2. 公式碎片识别为 eq 并合并 —— 不再把 "ρ ∼N"、"μ = e + 0 . 5" 当正文送去翻译。
  3. 图注支持在上方 —— v2 只认下方 46pt 内，图注在上方的图会整张丢失。
  4. caption_num 权威化 —— 图号取自图注文本（Fig. 3 → 3），不再依赖检出顺序。
  5. 图形摘要显式识别为 role=graphical_abstract —— 不占据 Fig.1..N 的编号。
  6. 图区内只有「比正文小」的文字被剔除 —— 避免 bbox 偏大时吞掉正文。
  7. 版式栏数探测 —— 硬编码的双栏阈值改为按实际行宽分布推断。
"""
import json
import os
import re
import sys
from collections import Counter

import _deps
_deps.ensure()
import pymupdf

HEADER_BAND = 34
FOOTER_BAND = 34
DPI = 110

CAPTION_HINT = re.compile(
    r"^\s*(Fig\.|Figure|Extended Data Fig|Supplementary Fig|Table\s*\d|图\s*\d|表\s*\d)", re.I)
CAP_NUM = re.compile(
    r"^\s*(?:Fig(?:ure)?\.?|Extended\s+Data\s+Fig\.?|Supplementary\s+Fig\.?|Table|图|表)\s*(\d+)",
    re.I)
# 编号后紧跟句点/冒号/竖线，是「标准图注」的排版特征；正文里的 "Figure 3 shows that…"
# 编号后接的是空格加小写词，不会命中。用它给「与正文同字号的长图注」放行。
# 「Fig.」的点必须可选（`Fig\.?`）：Nature/Cell 系一律写 "Fig. 1 |"，原来只认 "Fig 1"，
# 这类图注整篇漏检，图区全成 orphan。
# 注意这里**不含** Extended Data / Supplementary Fig：它们的编号自成一套，
# 与正文 Fig 1、Fig 2 撞号会让 fig_name 重名、裁图互相覆盖。扩展图要支持得单独做编号空间。
CAP_LEAD = re.compile(r"^\s*(?:Fig(?:ure)?\.?|Table|图|表)\s*\d+\s*[.:：|]", re.I)
# 表格题头：`Table 1` / `Table 1.` / `Table 1:` / `表 1`。
# 与 CAP_LEAD 的区别是要在「编号后面没有标点」时也算 —— 第 4 篇写的是 `Table 1.`，
# 第 3 篇的 `Table 1:` 与表体之间还夹一段描述，编号后是否带标点并不稳定。
TABLE_LEAD = re.compile(r"^\s*(?:Table|TABLE|表)\s*\d+")
# 扩展数据图/补充图自成一套编号（Fig. 1 与 Extended Data Fig. 1 可以同时存在）。
# 它们也进正文插图，但编号要加偏移，否则 fig_name 重名、裁出的图互相覆盖。
SUPP_CAP = re.compile(r"^\s*(?:Extended\s+Data\s+Fig|Supplementary\s+Fig)", re.I)
SUPP_OFFSET = 100
# 子图标记：(A) (b) (E–E′) (D′) —— 括号内单个字母，可带撇号或字母区间。
# 图注续接一律靠它判断，不看「有没有句号」：图注合法地可以不以句号收尾（(D′)、缩写），
# 也可以用句号收尾却仍缺子图（第 1 篇 Fig.4 的 D 就是在打包层被砍掉的）。
PANEL_TOKEN = re.compile(r"\(\s*([A-Za-z])(?:\s*[–\-—]\s*[A-Za-z])?\s*[′’']?\s*\)")
PANEL_LEAD = re.compile(r"^\s*\(\s*[A-Za-z](?:\s*[–\-—]\s*[A-Za-z])?\s*[′’']?\s*\)")
# 子图说明的开头不一定是 "(A)"：第 4 篇图 3/4 写的是 "(A-D) …"，只认单个字母的
# 正则会把整段子图说明判成正文，图注于是永远只剩一句标题。
TERM_END = re.compile(r"[.!?。！？][\"'”’)\]）]*\s*$")
SENT_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[A-Z(])")
# 页眉与水印：字号常比真标题还大（PMC 的 "HHS Public Access" 22pt），classify 会判成
# title 抢走标题位。命中即降级，不参与标题选取。
HEADER_WORDS = re.compile(
    r"(?i)^\s*(HHS Public Access|Author [Mm]anuscript|Downloaded from|Accepted [Mm]anuscript|"
    r"This article is protected|Check for updates|Preprint\b|Provided by\b)")
# 页眉/页脚/水印的通用特征。单看这几个词不够（期刊名、"Articles"、"e59" 都不含这些词），
# 所以真正的判据是「同一个短行在多页重复出现」，见 scan_running_heads()。
RUN_HEAD_LINE = re.compile(
    r"(author manuscript|hhs public access|downloaded from|available in pmc|"
    r"published in final edited form|page \d+ of \d+|correspondence to|"
    r"all rights reserved|for personal use only|nucleic acids research,"
    r"|journal of visualized experiments)", re.I)
GA_CAPTION = re.compile(r"^\s*(?:Graphical\s*Abstract|图文摘要|图形摘要)", re.I)
ABSTRACT_HINT = re.compile(r"^\s*(?:Abstract|ABSTRACT|摘\s*要)\b")
REF_HEAD = re.compile(r"^\s*(References|REFERENCES|参考文献)\s*$")
BACK_HEAD = re.compile(
    r"^\s*(Acknowledg|Author contribution|Competing interest|Additional information|"
    r"Open Access|Data availability|Code availability|Methods)", re.I)
TERMINAL = ".!?:;。！？：；"

MATH_CHARS = set("∑∏∫√≈≤≥∼∝∞∂∇±×÷·⟨⟩∈∀∃∆※")
GREEK = set("αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ")

# PDF 常用连字字符（ﬁ/ﬂ 等）。不还原会让文本里出现 "zebraﬁsh"、"signi cant"，
# 下游术语匹配、检索、复制粘贴全部失效，Word 里也可能显示成方框。
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
             "\ufb04": "ffl", "\ufb05": "ft", "\ufb06": "st"}

# TeX 数学字体把常用符号放在 0x02–0x04 这类控制码位上，PyMuPDF 原样透出。
# 这类码位**没有统一含义**：同一个 0x03 在 MathematicalPi-One 里是 μ（μl），
# 在 RMTMI 里是 Δ（ΔTFA）。所以只能按「字体 + 码位」查，不能全局替换 ——
# 全局把 \x03 换成 μ，ΔTFA 就成了 μTFA，错误比丢字更隐蔽。
# 下列映射逐个用页面渲染确认过（不是按编码表猜的）。
PRIVATE_GLYPH = {
    ("MathematicalPi-One", 0x02): "β",     # β-galactosidase
    ("MathematicalPi-One", 0x03): "μ",     # 30 \x03l → 30 μl
    ("MathematicalPi-One", 0x04): "α",     # \x04-helical / α/β domain
    ("RMTMI", 0x03): "Δ",                 # ΔTFA = TFA_Fluc − TFA_Rluc
    ("MTEX", 0x02): "√",                  # Pcodon = N√Pgeneral
}
# 控制字符会让 python-docx 抛 ValueError（"All strings must be XML compatible"），
# 整份文档一个字符都出不来。查不到映射的一律剔除，不许漏到下游。
CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def fix_glyphs(s, font):
    """按字体把私有码位还原成真字符，并剔除其余控制字符。"""
    if not s:
        return s
    base = (font or "").split("+")[-1]          # 去掉子集前缀 "CDKDAB+"
    if any(ord(c) < 0x20 for c in s):
        out = []
        for c in s:
            rep = PRIVATE_GLYPH.get((base, ord(c))) if ord(c) < 0x20 else None
            out.append(rep if rep is not None else c)
        s = "".join(out)
    return CTRL_CHARS.sub("", s)


def sanitize(s):
    """落盘前的最后一道：任何路径漏出来的控制字符都在这里清掉。"""
    return CTRL_CHARS.sub("", s) if isinstance(s, str) else s


def sanitize_obj(o):
    """递归清理整个 structure 里的字符串。"""
    if isinstance(o, str):
        return sanitize(o)
    if isinstance(o, list):
        return [sanitize_obj(v) for v in o]
    if isinstance(o, dict):
        return {k: sanitize_obj(v) for k, v in o.items()}
    return o


def fix_text(s):
    """还原连字字符并去掉软连字符（\u00ad）。"""
    if not s:
        return s
    for k, v in LIGATURES.items():
        if k in s:
            s = s.replace(k, v)
    return s.replace("\u00ad", "")
EQ_NO = re.compile(r"[\(（]\s*\d{1,3}[a-z]?\s*[\)）]\s*$")


def rect_union(a, b):
    return pymupdf.Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


def near(a, b, gap):
    return not (a.x1 + gap < b.x0 or b.x1 + gap < a.x0 or a.y1 + gap < b.y0 or b.y1 + gap < a.y0)


# ---------------------------------------------------------------- 版式探测
def layout_mode(doc, W):
    """按长文本行宽度分布判断单栏 / 双栏 / 多栏，返回 (mode, 说明)。

    双栏期刊每栏行宽约 0.35–0.45 W，单栏约 0.70–0.90 W。
    """
    wide_cnt = narrow_cnt = 0
    starts = Counter()
    for p in doc:
        for b in p.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                x0, _, x1, _ = l["bbox"]
                w = x1 - x0
                if w < 0.18 * W:
                    continue
                if w > 0.62 * W:
                    wide_cnt += 1
                else:
                    narrow_cnt += 1
                    starts[round(x0 / (W * 0.1))] += 1
    if wide_cnt + narrow_cnt < 8:
        return "double"
    ratio = wide_cnt / (wide_cnt + narrow_cnt)
    if ratio > 0.70:
        return "single"
    if len([k for k, v in starts.items() if v > 20]) >= 3:
        return "multi"
    return "double"


# ---------------------------------------------------------------- 图区检测
def graphic_rects(page, W, H):
    rects = []
    try:
        for d in page.get_drawings():
            r = pymupdf.Rect(d["rect"])
            if r.width >= 3 and r.height >= 3:
                rects.append(r)
    except Exception:
        pass
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                rects.append(pymupdf.Rect(r))
        except Exception:
            continue
    return [r for r in rects
            if r.get_area() > 120 and r.y0 > HEADER_BAND and r.y1 < H - FOOTER_BAND]


def cluster(rects, gap=16):
    merged = True
    while merged:
        merged = False
        out = []
        for r in rects:
            for i, o in enumerate(out):
                if near(r, o, gap):
                    out[i] = rect_union(o, r)
                    merged = True
                    break
            else:
                out.append(pymupdf.Rect(r))
        rects = out
    return rects


def occupied_rows(page, H, W, mode="double"):
    """按栏标记「内容行」：宽度接近栏宽的长文本行（正文段落、图注）占用该行。
    图内短标签宽度远小于栏宽，不占用 —— 图区因此自然成带。
    按左右栏分别统计，避免右栏图注把左栏的图切断。单栏时只用一条。"""
    n = int(H) + 3
    occL, occR = bytearray(n), bytearray(n)
    if mode == "single":
        thresh, wide = W * 0.30, W * 0.60
    else:
        thresh, wide = W * 0.22, W * 0.72
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            x0, y0, x1, y1 = l["bbox"]
            txt = "".join(s["text"] for s in l["spans"]).strip()
            if not txt or (x1 - x0) < thresh:
                continue
            if mode == "single":
                targets = (occL,)
            else:
                # 跨栏行有两种：明显宽于单栏的（wide），以及宽度中等但水平居中的
                # （页脚、通栏脚注、居中公式）。后者若只记左栏，右栏会凭空多出
                # 一条空白带，把页脚一并圈进图区。
                center = (x0 + x1) / 2.0
                cross = ((x1 - x0) > wide
                         or ((x1 - x0) > W * 0.50 and abs(center - W / 2.0) < W * 0.18))
                targets = (occL, occR) if cross else ((occL,) if x0 < W * 0.5 else (occR,))
            for y in range(max(0, int(y0)), min(n, int(y1) + 1)):
                for t in targets:
                    t[y] = 1
    return occL, occR


def zones_from_occupancy(occL, occR, H, W, grects, text_left, text_right, mode="double"):
    """空白带（未被长文本行占用）× 含图形元素 → 图区；跨栏同高的带合并。"""
    bands = []
    srcs = (occL,) if mode == "single" else (occL, occR)
    for occ in srcs:
        i = int(HEADER_BAND)
        while i < H - FOOTER_BAND:
            if occ[i]:
                i += 1
                continue
            j = i
            while j < H - FOOTER_BAND and not occ[j]:
                j += 1
            if j - i >= 40:
                bands.append((i, j))
            i = j
    bands.sort()
    merged = []
    for a, b in bands:
        if merged and a - merged[-1][1] <= 12:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    # 左右栏空白带取并集后，某一栏的正文行可能被并进来
    # （左栏 421 起、右栏 412 起 → 合并成 412，正好圈进左上角那行正文）。
    # 用「任一栏被占用即视为占用」的掩码把头尾各收一刀，中间不动，避免把图切断。
    occ_any = bytearray(len(occL))
    for y in range(len(occL)):
        if occL[y] or (y < len(occR) and occR[y]):
            occ_any[y] = 1

    out = []
    for y0, y1 in merged:
        while y0 < y1 - 20 and occ_any[y0]:
            y0 += 1
        while y1 > y0 + 20 and occ_any[y1 - 1]:
            y1 -= 1
        hits = [g for g in grects if g.y0 < y1 and g.y1 > y0]
        if not hits:
            continue
        x0 = min(g.x0 for g in hits)
        x1 = max(g.x1 for g in hits)
        if x1 - x0 > 0.55 * (text_right - text_left):
            x0, x1 = text_left, text_right
        if y1 - y0 < 80:
            continue
        out.append(pymupdf.Rect(x0, y0, x1, y1))
    return out


# ---------------------------------------------------------------- 文本块
def span_text(span, body):
    t = span["text"]
    if not t.strip():
        return t
    t = fix_glyphs(t, span.get("font"))
    if span.get("flags", 0) & 1:
        return "~{" + t + "}"
    return t


def body_font_size(doc):
    """正文字号：先按字符数统计每个字号，再在「占比 ≥20%」的字号里取**最大**的那个。

    不能直接取占比最大的：参考文献（7.5pt）常常比正文（9.3pt）字符更多，
    取众数会把整篇的判据基线压低 ~24%，于是正文被判成 h1、图注被判成「比正文大」
    而排除在图注候选之外。正文一定是「占相当篇幅的最大字号」，参考文献不会比它大。
    """
    cnt = Counter()
    for p in doc:
        for b in p.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    if s["text"].strip() and 4 < s["size"] < 20:
                        cnt[round(s["size"] * 10) / 10] += len(s["text"])
    if not cnt:
        return 9.0
    tot = sum(cnt.values())
    big = [sz for sz, n in cnt.items() if n >= 0.20 * tot]
    return max(big) if big else cnt.most_common(1)[0][0]


def classify(size, body):
    if size >= body * 1.55:
        return "title"
    if size >= body * 1.22:
        return "h1"
    if size >= body * 1.07:
        return "h2"
    if size >= body * 0.87:
        return "p"
    return "small"


def split_by_style(lines, body):
    """把一个 PDF block 内的行按「字号档位 + 全粗体短行」重新分组。

    PDF 常把小节标题与紧随其后的首段并进同一个 block（行距近、同栏），
    整块取最大字号会让首段也被当成标题。这里在块内再切一刀。

    粗体短行单独成组，且其后的行必定另起一组 —— 否则标题与正文档位相同
    时会被重新并回去（多数期刊的小节标题就是正文同字号 + 粗体）。
    """
    groups = []
    for l in lines:
        lvl = classify(l["size"], body)
        # 粗体短行才可能是标题；单字符（图面板 a/b/c）与超短标记一律不算
        headingish = l["bold"] and 3 <= l["nchar"] <= 70
        prev = groups[-1] if groups else None
        # 「两边性质相同」才合组。原来是「两边都不是标题才合」，等于粗体行永远各自成组：
        # 摘要整段是粗体（PDF 里 flags 带 bold），26 行就成了 24 个 h2；
        # 三行大标题也各成一块，英文标题因此无法整条溯源。
        # 粗体标题行后面跟非粗体正文，性质不同，仍然分开 —— 标题与首段不会重新粘连。
        # 比的是 raw（没被改写的字号档位）：粗体正文行的 lvl 会被改写成 h2，
        # 拿改写后的值比，第二行就永远对不上第一行，摘要照样被拆。
        can_join = (prev is not None and prev["raw"] == lvl
                    and prev["head"] == headingish)
        if can_join:
            prev["lines"].append(l)
        else:
            groups.append({"lvl": "h2" if (headingish and lvl in ("p", "h2")) else lvl,
                           "raw": lvl, "head": headingish, "lines": [l]})
    return groups


def _head_key(text):
    """页眉判定用的归一化键：数字一律变 #，空白压成一个空格，转小写。

    页码会变（"Lei et al. Page 3" / "Page 4"），不归一化就统计不到「重复出现」。
    但归一化会把所有纯数字行压成同一个 "#" —— 表格里的 420/252 全撞在一起，
    所以另有一条硬要求：这一行至少要含 3 个字母（见 scan_running_heads）。
    """
    t = re.sub(r"\d+", "#", (text or "").strip())
    t = re.sub(r"\s+", " ", t)
    return t.lower()


def _has_words(text, n=3):
    return len([c for c in (text or "") if c.isalpha()]) >= n


def scan_running_heads(doc, maxlen=120, min_pages=3, y_tol=8):
    """预扫全篇，找出「在多页重复出现、而且高度位置固定」的短行 —— 页眉、页脚、水印。

    为什么单独预扫、而不是等块建好再统计：页眉会**卡在两段之间**，先建块再删，
    合并段落那一步已经因为中间多了个块而断开，句子就永久被切成两半
    （第 3 篇 `ethidium` / `bromide)`、第 4 篇 `can also be targeted` / `by antibodies`）。
    所以必须在 `page_blocks` 出来的第一时间就丢掉。

    为什么按**行**扫而不是按块扫：`get_text("blocks")` 会把页眉和旁边的 URL 并成一块
    （"Nature Medicine | www.nature.com/naturemedicine"），块级精确匹配因此漏掉整批页眉
    —— 实测第 1 篇 14 个 `Nature Medicine` 在块级只出现 1 次。

    为什么还要求「高度稳定」：光看重复会把表格行标签算进来 —— 第 1 篇的 `LAIV8-IIV5`
    在多页表格里出现 11 次，位置随表格移动，y 跨度一刀就能分开。真页眉的 y0 几乎不动。
    """
    heads = {}
    for pno, page in enumerate(doc, start=1):
        if pno > 8:
            break                          # 页眉每页都有，看前 8 页足够；全篇扫一次要多花 2–6 秒
        for blk in page.get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                t = "".join(s.get("text") or "" for s in line.get("spans", [])).strip()
                if len(t) < 2 or len(t) > maxlen or not _has_words(t):
                    continue
                h = heads.setdefault(_head_key(t), {"pages": set(), "ys": []})
                h["pages"].add(pno)
                h["ys"].append(round(line["bbox"][1], 1))
    out = {}
    for k, h in heads.items():
        if len(h["pages"]) < min_pages:
            continue
        if max(h["ys"]) - min(h["ys"]) > y_tol:
            continue                       # 高度在变 → 表格行标签之类，不是页眉
        out[k] = h["pages"]
    return out


def is_running_head(b, heads, H):
    """这个块是不是页眉/页脚/水印。

    判据必须**带上位置**，只有「重复」是不够的：表格的行标签（`Day`、
    `LAIV8-IIV5`）也会在多页重复，而它们是要译的内容。页眉只会待在页面上下边缘。
    两条路：
      ① 贴边 + 文本里含某个「多页同高度重复」的短行（期刊名、`Articles`、`Author Manuscript…`）。
         用「含」而不是「等于」：块常把页眉和旁边的 URL 并在一起。
      ② 命中 RUN_HEAD_LINE 且很短（`PAGE 3 OF 12`、`Correspondence to:` 这类只出现一两次的）。
    图注与摘要不参与：多篇图注都以 "Fig. 1 |" 开头，重复率天然高。
    """
    t = (b.get("text") or "").strip()
    if not t or len(t) > 160 or b.get("type") in ("caption", "abstract", "eq"):
        return False
    if not _has_words(t):
        return False
    bb = b.get("bbox") or [0, 0, 0, 0]
    near_edge = bb[1] <= 90 or bb[3] >= H - 90
    if near_edge:
        k = _head_key(t)
        for hk in heads:
            if hk == k or hk in k:
                return True
    return len(t) <= 120 and bool(RUN_HEAD_LINE.search(t))


def mark_abstract(blocks, body):
    """把首页摘要正文标成 abstract。

    多数期刊摘要用比正文大一号的字（如正文 8.2、摘要 10.0），
    单纯按字号分类会被误判成小节标题，进而被当正文送去翻译。
    优先按 "Abstract" 标题定位；没有该标题时，退回「首页第一段超长且字号偏大」。
    """
    idx = None
    for i, b in enumerate(blocks):
        if re.match(r"^\s*(?:Abstract|ABSTRACT|摘\s*要)\s*$", b["text"]):
            idx = i
            break
        if re.match(r"^\s*(?:Abstract|ABSTRACT)\b", b["text"]) and len(b["text"]) < 20:
            idx = i
            break
    if idx is not None:
        blocks[idx]["type"] = "h2"
        for b in blocks[idx + 1: idx + 5]:
            if len(b["text"]) > 400:
                b["type"] = "abstract"
                return blocks
        return blocks
    # 没有独立 Abstract 标题（如 NaP-TRAP）：靠「首页 + 超长 + 字号偏大」兜底
    for b in blocks:
        if (len(b["text"]) > 800 and b["size"] > body * 1.05
                and b["type"] in ("h1", "h2", "p")):
            b["type"] = "abstract"
            break
    return blocks


def eq_score(text):
    """公式/碎片行打分：数学符号密度、运算符密度、单字符 token 比例。"""
    s = text.strip()
    if not s or len(s) > 90:
        return 0.0
    n = len(s.replace(" ", ""))
    if n < 2:
        return 0.0
    math = sum(1 for c in s if c in MATH_CHARS)
    greek = sum(1 for c in s if c in GREEK)
    op = sum(1 for c in s if c in "=+-*/^_<>")
    score = (math + 1.6 * greek) / n + 0.55 * op / n
    toks = s.split()
    if len(toks) >= 3:
        score += 0.35 * sum(1 for t in toks if len(t) <= 2) / len(toks)
    if EQ_NO.search(s):
        score += 0.30
    if len(re.findall(r"[A-Za-z]{3,}", s)) >= 3:
        score -= 0.55
    if re.search(r"[\u4e00-\u9fff]", s):
        score -= 0.8
    return score


def page_blocks(page, pno, H, body):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        raw = []
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            seg = "".join(span_text(s, body) for s in l["spans"])
            if not seg.strip():
                continue
            raw.append({"text": seg, "bbox": list(l["bbox"]),
                        "size": max(s["size"] for s in spans),
                        "bold": all(s.get("flags", 0) & 16 for s in spans),
                        "nchar": len(seg.strip())})
        if not raw:
            continue
        for grp in split_by_style(raw, body):
            lines = grp["lines"]
            txt = fix_text(" ".join(" ".join(l["text"] for l in lines).split()))
            if not txt.strip():
                continue
            bb = pymupdf.Rect(lines[0]["bbox"])
            for l in lines[1:]:
                bb = rect_union(bb, pymupdf.Rect(l["bbox"]))
            if bb.y1 < HEADER_BAND + 4 or bb.y0 > H - FOOTER_BAND - 4:
                continue
            lvl = grp["lvl"]
            # 小字号不等于「该丢」：Nature 系的 Methods、致谢、利益声明常比正文小一号
            # （正文 9.3、Methods 7.5），按字号判成 small 会被骨架层整段丢弃，整节消失。
            # 长段落一律当正文；只有短条（DOI、邮箱、URL 脚注）才留在 small。
            if lvl == "small" and len(txt) >= 200:
                lvl = "p"
            out.append({"page": pno, "type": lvl, "text": txt,
                        "bbox": [round(v, 1) for v in bb],
                        "size": round(max(l["size"] for l in lines), 1),
                        "_rect": bb})
    return out


def merge_equations(blocks, body):
    """把被 PDF 拆碎的公式碎片接回一行 eq。"""
    out = []
    for b in blocks:
        p = out[-1] if out else None
        if (p and p["type"] == "eq" and b["type"] == "eq" and p["page"] == b["page"]
                and b["bbox"][1] - p["bbox"][3] < body * 1.9
                and not (b["bbox"][0] > p["bbox"][2] or p["bbox"][0] > b["bbox"][2])):
            joiner = "" if (p["text"].endswith(("=", "+", "-", "(", "~{")) or
                            b["text"].startswith((")", "=", "+", "-"))) else " "
            p["text"] = p["text"].rstrip() + joiner + b["text"].lstrip()
            p["bbox"][2] = max(p["bbox"][2], b["bbox"][2])
            p["bbox"][3] = b["bbox"][3]
            continue
        out.append(b)
    return out


def inside(rect, box, ratio=0.75):
    inter = pymupdf.Rect(rect) & pymupdf.Rect(box)
    if inter.is_empty:
        return False
    return inter.get_area() >= ratio * max(rect.get_area(), 1e-6)


def is_figure_noise(b, found, bar_w):
    """判断文本块是否为图内文字（面板标签 / 坐标轴 / 图例）。

    判据不用字号 —— 图内标签常只比正文小 5%，按字号筛会漏。
    被误圈进图区的正文段落有个稳定特征：**接近栏宽且较长**。
    其余落在图区里的文字一律视为图内文字，不进译文。
    """
    bb = pymupdf.Rect(b["bbox"])
    for f in found:
        if f["kind"] != "figure":
            continue
        if not inside(bb, f["rect"], 0.7):
            continue
        if bb.width > 0.75 * bar_w and len(b["text"]) > 180:
            return False                       # 误圈进来的正文，保留
        return True
    return False


def reading_order(blocks, W, mode="double"):
    narrow = W * (0.92 if mode == "single" else 0.68)
    wide = [b for b in blocks if b["_rect"].width > narrow]
    left = [b for b in blocks if b["_rect"].width <= narrow and b["_rect"].x0 < W * 0.5]
    right = [b for b in blocks if b["_rect"].width <= narrow and b["_rect"].x0 >= W * 0.5]
    for g in (left, right, wide):
        g.sort(key=lambda b: (b["_rect"].y0, b["_rect"].x0))
    ordered, li, ri = [], 0, 0
    for wb in wide:
        cut = wb["_rect"].y0
        while li < len(left) and left[li]["_rect"].y1 <= cut + 2:
            ordered.append(left[li]); li += 1
        while ri < len(right) and right[ri]["_rect"].y1 <= cut + 2:
            ordered.append(right[ri]); ri += 1
        ordered.append(wb)
    ordered.extend(left[li:])
    ordered.extend(right[ri:])
    return ordered


def _broken_line(prev, cur):
    """后一块是不是「同一段被折断的下半截」。

    双栏 PDF 常把一段拆成多行块，且行首可能因粗体/字号被判成 h2，于是
    `merge_paragraphs` 的「两边都是 p」条件一挡，整段就再也接不回去
    （第 2 篇摘要被拆成 20 个 h2，每块 4–8 个词，`contribu-`/`tor` 都分开）。
    两个稳定特征：上一块没有句末标点；下一块以小写字母、连字符续词或左括号开头。
    反过来，真标题后面的正文以大写字母起句，不会被吞。
    """
    pt = (prev.get("text") or "").rstrip()
    ct = (cur.get("text") or "").lstrip()
    if not pt or not ct or pt[-1] in TERMINAL:
        return False
    return pt.endswith("-") or ct[0].islower() or ct[0] in "(（[【"


def merge_drop_caps(blocks, body):
    """把正文首段的装饰首字母（drop cap）接回残句。

    期刊常在正文第一段用超大号首字母（如 "Influenza" 的 I 排成 41.6pt 竖排），
    PDF 把它单独成一个块，classify 后误判成 title/h1；紧随其后的 "nfluenza …"
    是缺首字母的残句。两者垂直重叠、水平相邻，既不是标题跨行、也不满足
    merge_paragraphs 的「上下排列」条件，必须单独归并。

    判据：前块是单字符的 title/h1（大字号装饰字），后块是 p 且以小写字母开头、
    同页、水平左边界接近、垂直有重叠或紧贴。归并后整体降为 p。
    """
    out = []
    for b in blocks:
        p = out[-1] if out else None
        if (p and p.get("page") == b.get("page")
                and p.get("type") in ("title", "h1")
                and b.get("type") == "p"
                and len((p.get("text") or "").strip()) == 1
                and (p.get("text") or "").strip().isalpha()
                and (b.get("text") or "").lstrip()[:1].islower()):
            pbb, bbb = p.get("bbox") or [0, 0, 0, 0], b.get("bbox") or [0, 0, 0, 0]
            # 垂直重叠（drop cap 与正文首行同高带）或紧贴；水平左边界接近
            v_overlap = pbb[1] <= bbb[3] and bbb[1] <= pbb[3]
            v_tight = -4 <= bbb[1] - pbb[3] <= body * 1.2
            if (v_overlap or v_tight) and abs(bbb[0] - pbb[0]) <= body:
                p["text"] = (p["text"].rstrip() + b["text"].lstrip()).strip()
                p["type"] = "p"
                p["bbox"] = [min(pbb[0], bbb[0]), min(pbb[1], bbb[1]),
                             max(pbb[2], bbb[2]), max(pbb[3], bbb[3])]
                continue
        out.append(b)
    return out


def merge_paragraphs(blocks, body):
    """把同一栏内被切断的段落接回去。"""
    out = []
    for b in blocks:
        p = out[-1] if out else None
        if (p and p["page"] == b["page"]
                and not p.get("_was_head") and not b.get("_was_head")
                and abs(p["bbox"][0] - b["bbox"][0]) < 3
                and -3.5 <= b["bbox"][1] - p["bbox"][3] <= body * 1.6
                and p["text"] and p["text"][-1] not in TERMINAL
                and (p["type"] == b["type"] == "p"
                     or (p["type"] in ("p", "h1", "h2", "title", "small")
                         and b["type"] in ("p", "h1", "h2", "title", "small")
                         and _broken_line(p, b)))):
            # 类型不一致却被判定为断行 → 那是正文被误判成小标题，统一改回 p
            if p["type"] != "p" or b["type"] != "p":
                p["type"] = "p"
            p["text"] = p["text"].rstrip() + (" " if p["text"][-1].isascii() else "") + b["text"].lstrip()
            p["bbox"][2] = max(p["bbox"][2], b["bbox"][2])
            p["bbox"][3] = b["bbox"][3]
            continue
        out.append(b)
    return out


def panel_letters(text):
    """图注里出现过的子图标记字母（统一小写）。"""
    return {m.group(1).lower() for m in PANEL_TOKEN.finditer(text or "")}


def _same_flow(cur, nxt, body, loose_x=False):
    """cur 之后紧接的块是否属于同一条文本流：同页下方，或次页同一栏。"""
    if nxt.get("page") == cur.get("page"):
        dp = nxt["bbox"][1] - cur["bbox"][3]
        dx = abs(nxt["bbox"][0] - cur["bbox"][0])
        return -2 <= dp <= body * 2.2 and dx <= 8
    if nxt.get("page") == cur.get("page") + 1:
        dx = abs(nxt["bbox"][0] - cur["bbox"][0])
        return dx <= 8 or (loose_x and nxt["bbox"][1] < 60)
    return False


def _tight_flow(cur, nxt):
    """续句的判据比子图标记更严：必须紧贴栏底/页顶。

    子图标记（(E–E′)/(F)）本身就是强证据，位置宽松一点也安全；而「下一块以小写开头」
    太常见了（mRNA、p-values、in vivo 都能起句），只靠它会把下一段正文吞进图注。
    所以这类额外要求「紧贴在页面或栏的边界上」，实测两处真实续片都落在次页 y0=44。
    """
    if nxt.get("page") == cur.get("page"):
        return -2 <= nxt["bbox"][1] - cur["bbox"][3] <= 6
    return (nxt.get("page") == cur.get("page") + 1 and nxt["bbox"][1] < 75
            and abs(nxt["bbox"][0] - cur["bbox"][0]) <= 8)


def _cut_point(cap, txt):
    """在后继块里找图注的续接位置 → ("panel"|"tail", 下标)，找不到返回 (None, 0)。

    panel：块里出现「图注还没有的子图字母」（第 1 篇 Fig.1 的 (E–E′)/(F)），
           它前面的文字是上一段正文的尾巴，从标记处切开。
    tail ：块以小写词开头（第 1 篇 Fig.3 的 "in translation of…"），
           取到第一个句末为止 —— 这是图注被页底切断的续句。
    """
    have = panel_letters(cap)
    best = None
    for m in PANEL_TOKEN.finditer(txt):
        if m.start() > 400:                    # 续片总在块首，靠后的是正文自己的括号
            break
        if m.group(1).lower() not in have:
            best = m.start()
            break
    if best is not None:
        return "panel", best
    # 图注以 "(D′)"、缩写、数字收尾是合法的，这类不吃「小写续句」这条
    if txt[:1].islower() and not re.search(r"[)′)）\d]$", cap.rstrip()):
        # 续片可能就是短短一个词（第 4 篇图 1 被页/栏切成 "…underside of NA head" + "domain."），
        # 短到没有句末空格时 SENT_END 找不到切点，整块就是续片
        if len(txt) <= 60 and TERM_END.search(txt):
            return "tail", len(txt)
        m = SENT_END.search(txt)
        if m and 25 <= m.end() <= 400:
            return "tail", m.end()
    return None, 0


def stitch_captions(blocks, body, max_panel=1800):
    """图注跨栏/跨页续接，以及「标题句 + 子图说明分块」的合并。

    实测三种被切断的形态（都在 test1 里出现过）：
      ① 图注写到页底，续片在次页顶部，且与上一段正文粘成一块
         （第 1 篇 Fig.1：块首 "compared to an A at +4. (E–E′) Correlation…"）；
      ② 图注末句被页底切断，续句在次页块首
         （第 1 篇 Fig.3："…causes a larger drop" + "in translation of…strong Kozak sequences."）；
      ③ 图注只有标题句，子图说明是紧随其后的独立块，以 "(A)" 开头
         （第 2 篇 Fig.2/3：50 字标题 + 502/884 字 "(A)…(B)…" 块）。

    ①② 切下来的正文部分要还回正文：①的前缀是上一段的尾巴，并回前一块；
    ②的后缀是新段落，留在原块。返回新块列表与接续次数（进 stats 备查）。
    """
    out = list(blocks)
    i, n_fixed = 0, 0
    while i < len(out):
        b = out[i]
        if b.get("type") != "caption":
            i += 1
            continue
        nxt = out[i + 1] if i + 1 < len(out) else None
        # ③ 标题句 + 紧随其后的子图块（可能不止一块，接完再看）
        # 同页同栏的判断先严后宽：子图说明常比图注缩进更深/更浅几个点，严格的 x0 对齐
        # 会把它判成「不是同一段」（第 4 篇图 3/5/6 就是这么只剩一句标题的）。
        # 放宽只在「图注本身很短」时启用 —— 长图注后面跟的多半是正文，不能吸进来。
        short_cap = len(b["text"]) <= 300
        if (nxt and nxt.get("type") in ("p", "small")
                and PANEL_LEAD.match(nxt.get("text") or "")
                and len(panel_letters(nxt["text"])) >= 2
                and len(nxt["text"]) <= max_panel
                and (_same_flow(b, nxt, body, loose_x=False)
                     or (short_cap and _same_flow(b, nxt, body, loose_x=True)))):
            b["text"] = b["text"].rstrip() + " " + nxt["text"].lstrip()
            b["bbox"] = [min(b["bbox"][0], nxt["bbox"][0]), min(b["bbox"][1], nxt["bbox"][1]),
                         max(b["bbox"][2], nxt["bbox"][2]), max(b["bbox"][3], nxt["bbox"][3])]
            del out[i + 1]
            n_fixed += 1
            continue
        if TERM_END.search(b["text"] or ""):
            i += 1
            continue
        n = out[i + 1] if i + 1 < len(out) else None
        if not n or n.get("type") not in ("p", "small") \
                or not _same_flow(b, n, body, loose_x=True):
            i += 1
            continue
        kind, cut = _cut_point(b["text"], n.get("text") or "")
        if kind == "tail" and not _tight_flow(b, n):
            i += 1
            continue
        if not kind:
            i += 1
            continue
        txt = n["text"]
        head, tail = txt[:cut].strip(), txt[cut:].strip()
        if kind == "panel":
            b["text"] = (b["text"].rstrip() + " " + tail).strip()
            prev = out[i - 1] if i > 0 else None
            if head and prev and prev.get("type") in ("p", "small") \
                    and not TERM_END.search(prev["text"] or ""):
                prev["text"] = prev["text"].rstrip() + " " + head
                prev["bbox"][2] = max(prev["bbox"][2], n["bbox"][2])
                del out[i + 1]
            elif head:
                n["text"] = head
            else:
                del out[i + 1]
        else:
            b["text"] = (b["text"].rstrip() + " " + head).strip()
            if tail:
                n["text"] = tail
            else:
                del out[i + 1]
        n_fixed += 1
        # 不前进：接完再看这条图注是否还需要继续接（可能跨两块）
    return out, n_fixed


def split_ref_list(blocks, minlen=400):
    """参考文献常被 PDF 并成一大段（"1. ... 2. ... 3. ..."），按编号拆回条目。"""
    out = []
    for b in blocks:
        if b["type"] == "ref" and len(b["text"]) > minlen:
            parts = re.split(r"(?=(?<![\d.])\b\d{1,3}\.\s+[A-Z])", b["text"])
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 3:
                for p in parts:
                    nb = dict(b)
                    nb["text"] = p
                    out.append(nb)
                continue
        out.append(b)
    return out


def caption_anchors(all_figs, blocks):
    """记下每个 caption_block 指向的块对象本身（不是序号）。

    后面合并标题、拆分文献都会增删块，序号会整体错位（实测第 2 篇错位 34 位，
    图注指到了「8. Cao S…」这条文献），按对象重新定位才不会张冠李戴。
    """
    out = []
    for f in all_figs:
        i = f.get("caption_block")
        out.append((f, id(blocks[i]) if i is not None and 0 <= i < len(blocks) else None))
    return out


def restore_caption_index(anchors, blocks):
    """块结构变了之后，把 caption_block 按「块对象」重新换算回序号。"""
    where = {id(b): i for i, b in enumerate(blocks)}
    for f, bid in anchors:
        f["caption_block"] = where.get(bid) if bid is not None else None


def merge_title_lines(blocks):
    """把同一行标题被 PDF 拆成的多个块（含首字母下沉残行、跨行标题）合并回一条。

    实测形态（方案 B）：
      · 第 1 篇标题首字母下沉，`A` 被单独切成一块 "A"，其后才是 " chimeric hemagglutinin…"，
        残行 "A" 与主行同字号但 x0 略偏、垂直间距极小 —— 按「同字号 + 上下紧贴」归并；
      · 第 2 篇封面期刊标记 "OPEN" 与标题并排（不同字号/不同 x0），靠字号分组天然分开。
    只并 title/h1/h2（标题与作者行），且放宽到同页同字号 + 左边对齐或垂直紧贴，
    不要求字面连续 —— 残行可能是孤立的单字符块。
    """
    out, last = [], {}
    for b in blocks:
        if b.get("type") not in ("title", "h1", "h2"):
            out.append(b)
            last.clear()                     # 中间夹了别的块，标题段到此为止
            continue
        bb = b.get("bbox") or [0, 0, 0, 0]
        k = (b.get("page"), round((b.get("size") or 0) * 2) / 2)
        prev = last.get(k)
        pbb = (prev.get("bbox") or [0, 0, 0, 0]) if prev is not None else None
        if pbb is None:
            last[k] = b
            out.append(b)
            continue
        # 归并条件：同字号，且「左边界基本对齐」或「上下紧贴（垂直间距小）」。
        # 首字母下沉残行 x0 偏、但紧贴下一行，走第二支；封面标记字号不同，不会撞。
        aligned = abs(bb[0] - pbb[0]) <= 6
        tight = -2 <= bb[1] - pbb[3] <= 4
        if aligned or tight:
            prev["text"] = (prev["text"].rstrip() + " " + b["text"].lstrip()).strip()
            prev["bbox"] = [min(pbb[0], bb[0]), min(pbb[1], bb[1]),
                            max(pbb[2], bb[2]), max(pbb[3], bb[3])]
            continue
        last[k] = b
        out.append(b)
    return out


# ---------------------------------------------------------------- 图注绑定
def link_captions(zones, caps):
    """把图注挂到对应图区上，并记录图注在图的上方还是下方。

    v3：额外接受「图注在图区正上方 60pt 内」的排法（部分 Elsevier/IEEE 期刊）。
    采用全局距离排序配对，避免一张图注被两个图区抢走。
    """
    pairs = []
    for zi, r in enumerate(zones):
        for ci, c in enumerate(caps):
            cy0, cy1 = c["bbox"][1], c["bbox"][3]
            pos, dist = None, 1e9
            if r.y0 - 6 <= cy0 and cy1 <= r.y1 + 6:
                pos, dist = "inside", 0
            elif cy0 >= r.y1 - 6 and cy0 - r.y1 <= 46:
                pos, dist = "below", cy0 - r.y1
            elif cy1 <= r.y0 + 6 and r.y0 - cy1 <= 60:
                pos, dist = "above", r.y0 - cy1
            if pos:
                pairs.append((dist, zi, ci, pos))
    pairs.sort()
    got, used = {}, set()
    for dist, zi, ci, pos in pairs:
        if zi in got or ci in used:
            continue
        got[zi] = (caps[ci], pos)
        used.add(ci)
    return [(r, *got.get(zi, (None, None))) for zi, r in enumerate(zones)]


def table_grid(page, rect, gap=6.0, tol=3.0):
    """把表格区域按词坐标重建成行列。

    为什么能这么做：学术表格里「词内空格」和「列间空隙」差一个数量级。
    NaP-TRAP 的 Table 1 实测词间隙 1.7–2.0pt、列间隙 15.9–84.6pt，
    取 6pt 阈值就能把两者干净分开，不必依赖表格线（三线表本来也没有列线）。

    返回 None 表示没有可用网格；confident=False 表示各行列数不齐
    （合并单元格 / 表头折行），此时交由人工确认，**不猜**。
    """
    if rect.width < 40 or rect.height < 20:
        return None
    words = [w for w in page.get_text("words")
             if pymupdf.Rect(w[:4]).intersects(rect) and (w[4] or "").strip()]
    if len(words) < 8:
        return None

    rows = []
    for w in sorted(words, key=lambda x: x[1]):
        if rows and abs(w[1] - rows[-1][0]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w[1], [w]])

    grids = []
    for _, ws in rows:
        ws.sort(key=lambda x: x[0])
        cells, cur, last = [], [], None
        for w in ws:
            if last is not None and w[0] - last > gap:
                cells.append(" ".join(cur))
                cur = []
            cur.append(w[4].strip())
            last = w[2]
        if cur:
            cells.append(" ".join(cur))
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells if c.strip()]
        if cells:
            grids.append(cells)

    # 表题若被圈进表区，第一行只有一个单元格且以 Table/表 开头 → 剔除
    if grids and len(grids[0]) == 1 and re.match(r"^\s*(Table|表)\b", grids[0][0]):
        grids = grids[1:]
    if len(grids) < 2:
        return None

    counts = Counter(len(g) for g in grids)
    ncol, freq = counts.most_common(1)[0]
    if ncol < 2 or freq < 2:
        return None
    confident = freq == len(grids)
    return {
        "header": grids[0],
        "rows": grids[1:],
        "cols": ncol,
        "confident": confident,
        "reason": "" if confident else f"各行列数不一致 {sorted(counts)}，需人工确认",
    }


def extract_caption_num(text):
    m = CAP_NUM.match(text or "")
    return int(m.group(1)) if m else None


def cell_text(page, rect):
    """单元格文字，按行返回。

    三条路都试过，只有这条同时满足「顺序对」和「符号对」：
      · `get_textbox()` 顺序正确，但走另一条解码路径 —— `RMTMI` 的 \\x03（Δ）
        会变成 U+FFFD（第 5 篇表 1 的 `dΔTFA` 出来是 `d�TFA`）；
      · `get_text("words")` 能拿到原字符，但符号词有亚像素级 y 偏移，按 y 排序后
        `2.40 ± 0.12` 会变成 `± 2.40 0.12`；
      · 逐行取 span 则两者兼得：行内顺序由 PDF 保证，font 也在手上，可走 `fix_glyphs`。
    """
    lines = []
    for blk in page.get_text("dict", clip=rect)["blocks"]:
        for ln in blk.get("lines", []):
            txt = "".join(fix_glyphs(s.get("text") or "", s.get("font") or "")
                          for s in ln["spans"])
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt:
                lines.append(txt)
    return lines


def grid_from_table(page, t):
    """把 PyMuPDF 的表格对象转成骨架层要的 {header, rows, cols, confident, reason}。

    字段与 `table_grid()` 保持一致，下游 `make_skeleton` 不必区分两条来源。

    行要自己再切一刀：`find_tables` 按水平线分行，密排三线表里两行数据常被并成一行
    （第 5 篇表 1：`bTFARluc (min)` 和 `cTFAFluc (min)` 挤在同一格里）。判据是
    **同一行里有两个以上单元格都含 k 行文字** —— 那说明是 k 行被并了，拆开；
    只有一格折行则是正常换行，并回一行。
    """
    rows = []
    try:
        for row in t.rows:
            cells = []
            for c in row.cells:
                cells.append(cell_text(page, pymupdf.Rect(c)) if c else [])
            k = max((len(x) for x in cells), default=0)
            if not k:
                continue
            if k > 1 and sum(1 for x in cells if len(x) == k) >= 2:
                for i in range(k):
                    rows.append([(x[i] if i < len(x) else "") for x in cells])
            else:
                # 只有一格多行 = 单元格内正常折行（长序列、长试剂名），并回一行，
                # 不算错位（第 3 篇表 6 的 Sequence 列就折成两行，内容是对的）
                rows.append([" ".join(x) for x in cells])
    except Exception:
        return None
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return None
    # 整列为空的多半是表线识别多切了一刀（第 3 篇表 4 就多出一个空列），删掉再算列数
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i] for r in rows)]
    if len(keep) < width:
        rows = [[r[i] for i in keep] for r in rows]
    counts = Counter(len(r) for r in rows)
    ncol, freq = counts.most_common(1)[0]
    if ncol < 2:
        return None
    ok = freq == len(rows)
    return {"header": rows[0], "rows": rows[1:], "cols": ncol, "confident": ok,
            "reason": "" if ok else f"各行列数不一致 {sorted(counts)}，需人工确认"}


def words_below(page, bb, y0):
    """取某个文本块里、顶边在 y0 之下的词 —— 用来捞「和表体挤在同一块里」的表注。"""
    box = pymupdf.Rect(bb)
    ws = [w for w in page.get_text("words")
          if w[1] >= y0 - 1 and pymupdf.Rect(w[:4]).intersects(box)]
    if not ws:
        return ""
    ws.sort(key=lambda w: (round(w[1], 1), w[0]))
    return re.sub(r"\s+", " ", " ".join(w[4].strip() for w in ws)).strip()


def scan_tables(page, blocks, body):
    """本页带 `Table N` 题头的表格。

    为什么必须单独开一条通道：图注是靠 `graphic_rects` 找到的**图片区**来配对的，
    而三线表是纯文字、根本没有图片区，于是 `Table 1` 一直留在正文里当小标题，
    `stats.tables` 恒为 0（第 3、4、5 篇都是这样；第 1、2 篇全文没有表，0 是对的）。
    这里改用 PyMuPDF 的表格识别拿表区，再按纵向距离把题头接上去。

    只认带编号的题头：第 4 篇 p32–34 的 `ITEMS/RESOURCES` 清单跨三页、不编号，
    硬收进来会得到一个 38 行 × 3 列的杂物表，还不如留给正文。
    """
    try:
        tf = page.find_tables()
    except Exception:
        return []
    zones = []
    for t in tf.tables:
        g = grid_from_table(page, t)
        r = pymupdf.Rect(t.bbox)
        if g and r.height >= 18 and r.width >= 40:
            zones.append({"rect": r, "grid": g})
    if not zones:
        return []

    out, used = [], set()
    for i, b in enumerate(blocks):
        t = (b.get("text") or "").strip()
        if not TABLE_LEAD.match(t) or len(t) > 200:
            continue
        n = extract_caption_num(t)
        if n is None:
            continue
        best, pick, pos = 1e9, None, None
        for zi, z in enumerate(zones):
            if zi in used:
                continue
            r = z["rect"]
            below = r.y0 - b["bbox"][3]        # 题头在表上方：距离越小越对
            above = b["bbox"][1] - r.y1        # 题头在表下方
            if -3 <= below <= 150 and below < best:
                best, pick, pos = below, zi, "above"
            elif -3 <= above <= 60 and above + 400 < best:
                best, pick, pos = above + 400, zi, "below"
        if pick is None:
            continue
        used.add(pick)
        rc = zones[pick]["rect"]

        # 题头与表区之间可能夹两样东西，都不能丢：
        #   ① 表题正文 —— 第 3 篇 `Table 1:` 只写编号，真表题「Sequences used…」是下一块；
        #   ② 表注 —— 第 5 篇表体和「For each parameter…」挤在同一个块里，
        #      块整体被剔掉就会连带丢注。所以先把表区下方那截词捞出来并进题头。
        for nb in blocks[i + 1:]:
            bb = nb["bbox"]
            if bb[1] < b["bbox"][3] - 2:
                continue
            if bb[1] > rc.y1 + 2:
                break
            if nb["type"] == "caption":
                break
            if bb[3] > rc.y1 + 8:                       # 表体块里带着表注
                tail = words_below(page, bb, rc.y1)
                if len(tail) >= 40:
                    b["text"] = b["text"].rstrip() + " " + tail
                break
            if nb["type"] in ("p", "small") and bb[3] <= rc.y0 + 1 and len(nb["text"]) <= 600:
                b["text"] = b["text"].rstrip() + " " + nb["text"].lstrip()
                nb["_table_desc"] = True                # 已并入题头，正文里剔除
        out.append({"rect": rc, "kind": "table", "grid": zones[pick]["grid"],
                    "cap": b, "cap_pos": pos, "num": n, "role": None})
    return out


def is_table_cell(b, rects):
    """落在表区纵向带里的文字块 —— 表格已由 grid 重建，这些块不能再当正文译一遍。"""
    bb = b["bbox"]
    for r in rects:
        if r.y0 - 3 <= bb[1] <= r.y1 + 3 and bb[0] >= r.x0 - 10 and bb[2] <= r.x1 + 12:
            return True
    return False


def is_caption_cand(b, body):
    """图注候选：以 Fig./Table/图/表 开头，且字号不显著大于正文。

    多数期刊图注比正文小一号；少数（如 Nature 系列的表标题）与正文同号或略大，
    对这类短标题单独放宽，但要求长度很短，避免把「Table 1 shows that...」这种正文抓进来。
    """
    t = b["text"]
    if not (CAPTION_HINT.match(t) or GA_CAPTION.match(t)):
        return False
    if b["type"] in ("abstract", "ref", "refhead", "eq"):
        return False
    # 与正文同字号的长图注：Scientific Reports 一类把图注排成 9pt（正文也 9pt）、
    # 长度 1500 字上下，原来的「短于 120 字」一律排掉，整篇 4 张图就全成 orphan。
    # 这类图注的排版特征是「编号紧跟句点/冒号」，据此放行。
    if CAP_NUM.match(t) and CAP_LEAD.match(t):
        return True
    if b["size"] < body * 0.98:
        return True
    return len(t) < 120 and b["size"] < body * 1.18


def ga_score(rect, page_no, pages, W, H, near_abstract):
    """图形摘要打分（只在「无正常图注」时使用，有 Fig.N 图注的图区不走这里）。"""
    if page_no not in (1, pages):
        return 0                       # 图形摘要只出现在首页或末页
    score = 0
    if page_no == 1:
        score += 3
    else:
        score += 2
    score += 2                         # 无图注
    if rect.get_area() > 0.20 * W * H:
        score += 2
    if rect.y0 < H * 0.55:
        score += 1
    if near_abstract:
        score += 2
    if rect.width > 0.60 * W:
        score += 1
    return score


def main():
    if len(sys.argv) < 3:
        print("usage: extract.py <input.pdf> <outdir>")
        return 1
    pdf, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    doc = pymupdf.open(pdf)
    body = body_font_size(doc)
    W, H = doc[0].rect.width, doc[0].rect.height
    mode = layout_mode(doc, W)
    pages = doc.page_count

    all_blocks, all_figs = [], []
    head_keys = scan_running_heads(doc)
    n_heads = 0
    for pno, page in enumerate(doc, start=1):
        blocks = page_blocks(page, pno, H, body)
        kept = [b for b in blocks if not is_running_head(b, head_keys, H)]
        n_heads += len(blocks) - len(kept)
        blocks = kept
        if pno == 1:
            blocks = mark_abstract(blocks, body)
        for b in blocks:
            # 真正的标题不会超过 400 字符；超长说明标题与正文仍并在一起，降级为正文
            if b["type"] in ("h1", "h2") and len(b["text"]) > 400:
                b["type"] = "p"
                # 打标：这类块是「字号像标题、内容其实是整段」的块（作者列表 + 单位），
                # 不能当普通段落参与 merge_paragraphs，否则会把后面的摘要整段吞进来
                b["_was_head"] = True
            # 「10.」「11.」这类列表序号被当成小节标题（第 3 篇 protocol 的编号步骤）
            elif b["type"] in ("h1", "h2") and re.fullmatch(r"\d{1,2}[.)]", b["text"].strip()):
                b["type"] = "p"
        for b in blocks:
            if b["type"] in ("p", "small") and eq_score(b["text"]) >= 0.20:
                b["type"] = "eq"
        blocks = merge_equations(blocks, body)

        rects = graphic_rects(page, W, H)
        tl = min((g.x0 for g in rects), default=36.0)
        tr = max((g.x1 for g in rects), default=W - 36.0)
        text_l = min([b["bbox"][0] for b in blocks
                      if b["type"] in ("p", "abstract", "h1", "h2", "title")] or [tl])
        text_r = max([b["bbox"][2] for b in blocks
                      if b["type"] in ("p", "abstract", "h1", "h2", "title")] or [tr])
        caps = [b for b in blocks if is_caption_cand(b, body)]
        zones = zones_from_occupancy(*occupied_rows(page, H, W, mode),
                                     H, W, rects, text_l, text_r, mode)
        has_abstract = any(ABSTRACT_HINT.match(b["text"]) for b in blocks)

        found = []
        for r, c, cpos in link_captions(zones, caps):
            if r.height < 55 or r.width < 60:
                continue
            if c is None and r.get_area() < 0.25 * W * H:
                continue                      # 无图注且面积小：多半是页边装饰/色条
            role = None
            if c is not None and GA_CAPTION.match(c["text"]):
                role = "graphical_abstract"
            elif c is None:
                role = ("graphical_abstract"
                        if ga_score(r, pno, pages, W, H, has_abstract) >= 7 else "orphan")
            kind = "table" if (c and re.match(r"^\s*(Table|表)", c["text"])) else "figure"
            if role:
                kind = "figure"
            found.append({"rect": r, "kind": kind, "cap": c, "cap_pos": cpos,
                          "num": extract_caption_num(c["text"]) if c else None,
                          "role": role})

        # 表格单独走一条通道：三线表是纯文字，`link_captions` 配的是图片区，永远挂不上
        taken = {id(f["cap"]): f for f in found if f["cap"]}
        for tf in scan_tables(page, blocks, body):
            old = taken.get(id(tf["cap"]))
            if old is not None:
                # 题头已当图注接过一次（少数期刊把表排在有底色的框里），补上网格即可
                if old["kind"] == "table" and not old.get("grid"):
                    old["grid"] = tf["grid"]
                continue
            found.append(tf)
            taken[id(tf["cap"])] = tf

        cap_ids = {id(f["cap"]) for f in found if f["cap"]}
        tab_rects = [f["rect"] for f in found if f["kind"] == "table"]

        bar_w = (text_r - text_l) if mode == "single" else (text_r - text_l) / 2.0
        kept = []
        for b in blocks:
            if id(b) in cap_ids:
                b["type"] = "caption"
                kept.append(b)
                continue
            if b.get("_table_desc"):
                continue
            if is_table_cell(b, tab_rects):
                continue
            if is_figure_noise(b, found, bar_w):
                continue
            kept.append(b)

        ordered = merge_paragraphs(merge_drop_caps(reading_order(kept, W, mode), body), body)
        pos = {id(b): i for i, b in enumerate(ordered)}
        base = len(all_blocks)
        for f in sorted(found, key=lambda z: z["rect"].y0):
            ci = pos.get(id(f["cap"])) if f["cap"] else None
            all_figs.append({
                "id": len(all_figs) + 1, "page": pno, "kind": f["kind"],
                "role": f["role"],
                "bbox": [round(v, 1) for v in f["rect"]],
                "caption_block": None if ci is None else base + ci,
                "caption_num": f["num"],
                "caption_pos": f["cap_pos"],
                # 表格顺带把行列重建出来，骨架层直接预填，模型不必重打结构。
                # scan_tables 已经给过网格（PyMuPDF 识别，单元格更准），不再用坐标法覆盖
                "grid": (f.get("grid") or table_grid(page, f["rect"]))
                        if f["kind"] == "table" else None,
            })
        all_blocks.extend(ordered)

    # 参考文献标题常与首条文献同块（"References 1. de Boer..."），拆开
    for idx, b in enumerate(all_blocks):
        m = re.match(r"^\s*(References|REFERENCES|参考文献)\s+(?=\S)", b["text"])
        if not m:
            continue
        rest = b["text"][m.end():].strip()
        if not rest:
            continue
        b["text"] = rest
        b["type"] = "ref"
        all_blocks.insert(idx, {"page": b["page"], "type": "refhead",
                                "text": m.group(1), "bbox": b["bbox"], "size": b["size"]})
        for f in all_figs:
            if f["caption_block"] is not None and f["caption_block"] >= idx:
                f["caption_block"] += 1
        break

    # 页眉/水印的字号常比真标题还大（PMC 的 "HHS Public Access" 22pt），先降级再选标题
    for b in all_blocks:
        if b.get("type") in ("title", "h1") and HEADER_WORDS.match(b.get("text") or ""):
            b["type"] = "p"

    in_refs = False
    for b in all_blocks:
        if b["type"] == "refhead" or (REF_HEAD.match(b["text"]) and b["type"] in ("h1", "h2", "p", "small")):
            in_refs, b["type"] = True, "refhead"
            continue
        if in_refs:
            if b["type"] == "caption":     # 图注常排在参考文献之后（图放文末），不能当文献
                in_refs = False
                continue
            if BACK_HEAD.match(b["text"]):
                in_refs = False
                continue
            b["type"] = "ref"

    # 图注续接要在参考文献拆分、标题合并之前做：那两步会增删块，序号会错位
    anchors = caption_anchors(all_figs, all_blocks)
    all_blocks, n_stitched = stitch_captions(all_blocks, body)
    restore_caption_index(anchors, all_blocks)
    # 合并跨行标题、拆分文献都会增删块 —— 先记下块对象，处理完再换算 caption_block 序号
    all_blocks = merge_title_lines(all_blocks)
    all_blocks = split_ref_list(all_blocks)
    restore_caption_index(anchors, all_blocks)
    for b in all_blocks:
        b.pop("_rect", None)
        b.pop("_was_head", None)
    for i, b in enumerate(all_blocks):
        b["i"] = i

    # 图号权威化：有 caption_num 用图注编号；缺编号的按页序补位；GA/orphan 不占号
    real = [f for f in all_figs if not f["role"]]
    used_nums = {f["caption_num"] for f in real if f["caption_num"]}
    auto = 1
    for f in sorted(real, key=lambda x: (x["page"], x["bbox"][1])):
        if f["caption_num"]:
            continue
        while auto in used_nums:
            auto += 1
        f["caption_num"] = auto
        used_nums.add(auto)
        auto += 1
    for f in all_figs:
        ci = f.get("caption_block")
        cap = all_blocks[ci]["text"] if (ci is not None and ci < len(all_blocks)) else ""
        if f["caption_num"] and SUPP_CAP.match(cap):
            f["caption_num"] += SUPP_OFFSET      # fig101，与正文 fig1 不撞名
        f["fig_name"] = None if f["role"] else (
            f"fig{f['caption_num']}" if f["kind"] == "figure" else f"table{f['caption_num']}")

    total_chars = sum(len(b["text"]) for b in all_blocks)
    structure = {
        "meta": {"source": os.path.basename(pdf), "pages": pages,
                 "page_size": [round(W, 1), round(H, 1)], "body_font_size": body,
                 "layout": mode},
        "blocks": all_blocks,
        "figures": all_figs,
        "stats": {
            "blocks": len(all_blocks),
            "chars": total_chars,
            "by_type": dict(Counter(b["type"] for b in all_blocks)),
            "figures": sum(1 for f in real if f["kind"] == "figure"),
            "tables": sum(1 for f in real if f["kind"] == "table"),
            "table_grids": sum(1 for f in all_figs if f.get("grid")),
            "table_grid_ok": sum(1 for f in all_figs if (f.get("grid") or {}).get("confident")),
            "graphical_abstract": sum(1 for f in all_figs if f["role"] == "graphical_abstract"),
            "orphans": [f["id"] for f in all_figs if f["role"] == "orphan"],
            "equations": sum(1 for b in all_blocks if b["type"] == "eq"),
            "captions_linked": sum(1 for f in all_figs if f["caption_block"] is not None),
            "captions_stitched": n_stitched,
            "headers_removed": n_heads,
            "fig_nums": sorted(f["caption_num"] for f in real if f["kind"] == "figure"),
            "fig_pages": [f["page"] for f in real if f["kind"] == "figure"],
            "caption_above": [f["caption_num"] for f in all_figs if f["caption_pos"] == "above"],
            "layout": mode,
        },
    }
    structure = sanitize_obj(structure)
    path = os.path.join(outdir, "structure.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(structure, fh, ensure_ascii=False, indent=1)
    print(json.dumps(structure["stats"], ensure_ascii=False))
    print("body_size", body, "pages", pages, "layout", mode, "->", path)
    if total_chars < 120 * pages:
        print(f"WARN 全文仅 {total_chars} 字符 / {pages} 页，疑似扫描版（无文本层）。"
              f"需要先 OCR，例如：ocrmypdf --skip-text in.pdf out.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
