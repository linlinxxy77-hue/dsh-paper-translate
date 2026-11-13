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
    if span.get("flags", 0) & 1:
        return "~{" + t + "}"
    return t


def body_font_size(doc):
    cnt = Counter()
    for p in doc:
        for b in p.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    if s["text"].strip() and 4 < s["size"] < 20:
                        cnt[round(s["size"] * 10) / 10] += len(s["text"])
    return cnt.most_common(1)[0][0] if cnt else 9.0


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
        can_join = (prev is not None and prev["lvl"] == lvl
                    and not headingish and not prev["head"])
        if can_join:
            prev["lines"].append(l)
        else:
            groups.append({"lvl": "h2" if (headingish and lvl in ("p", "h2")) else lvl,
                           "head": headingish, "lines": [l]})
    return groups


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
            out.append({"page": pno, "type": grp["lvl"], "text": txt,
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


def merge_paragraphs(blocks, body):
    """把同一栏内被切断的段落接回去。"""
    out = []
    for b in blocks:
        p = out[-1] if out else None
        if (p and p["type"] == "p" and b["type"] == "p" and p["page"] == b["page"]
                and abs(p["bbox"][0] - b["bbox"][0]) < 3
                and -1 <= b["bbox"][1] - p["bbox"][3] <= body * 1.6
                and p["text"] and p["text"][-1] not in TERMINAL):
            p["text"] = p["text"].rstrip() + (" " if p["text"][-1].isascii() else "") + b["text"].lstrip()
            p["bbox"][2] = max(p["bbox"][2], b["bbox"][2])
            p["bbox"][3] = b["bbox"][3]
            continue
        out.append(b)
    return out


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
    for pno, page in enumerate(doc, start=1):
        blocks = page_blocks(page, pno, H, body)
        if pno == 1:
            blocks = mark_abstract(blocks, body)
        for b in blocks:
            # 真正的标题不会超过 400 字符；超长说明标题与正文仍并在一起，降级为正文
            if b["type"] in ("h1", "h2") and len(b["text"]) > 400:
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

        cap_ids = {id(f["cap"]) for f in found if f["cap"]}

        bar_w = (text_r - text_l) if mode == "single" else (text_r - text_l) / 2.0
        kept = []
        for b in blocks:
            if id(b) in cap_ids:
                b["type"] = "caption"
                kept.append(b)
                continue
            if is_figure_noise(b, found, bar_w):
                continue
            kept.append(b)

        ordered = merge_paragraphs(reading_order(kept, W, mode), body)
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
                # 表格顺带把行列重建出来，骨架层直接预填，模型不必重打结构
                "grid": table_grid(page, f["rect"]) if f["kind"] == "table" else None,
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

    in_refs = False
    for b in all_blocks:
        if b["type"] == "refhead" or (REF_HEAD.match(b["text"]) and b["type"] in ("h1", "h2", "p", "small")):
            in_refs, b["type"] = True, "refhead"
            continue
        if in_refs:
            if BACK_HEAD.match(b["text"]):
                in_refs = False
                continue
            b["type"] = "ref"
    all_blocks = split_ref_list(all_blocks)
    for b in all_blocks:
        b.pop("_rect", None)
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
            "fig_nums": sorted(f["caption_num"] for f in real if f["kind"] == "figure"),
            "fig_pages": [f["page"] for f in real if f["kind"] == "figure"],
            "caption_above": [f["caption_num"] for f in all_figs if f["caption_pos"] == "above"],
            "layout": mode,
        },
    }
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
