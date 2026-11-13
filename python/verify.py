# -*- coding: utf-8 -*-
"""verify.py —— 交付前自检（不开图、不导出，纯结构校验）。

用法: python verify.py <content.json> <figs_dir> [out.docx]
输出: 报告文本 + 退出码（0 通过 / 1 有告警 / 2 仍未译完）

v3 新增（都靠本地计算，不耗模型 token）：
  - 进度：还有未译项直接拦下
  - 数字一致性：原文出现的关键数值（≥2 位或含小数）在译文中应能找到
  - 图表引用完整性：原文 Fig. 3 / Table 1 在译文中应有对应「图 3」「表 1」
  - 公式误判：eq 项里出现多个英文单词 → 多半是正文段落被误判成公式
  - 图文件命名：figs/ 里的文件与 content.json 引用一一对应
"""
import json
import os
import re
import sys

CJK = re.compile(r"[\u4e00-\u9fff]")
FIG_EN = re.compile(r"\b(?:Figs?\.?|Figures?)\s*(\d+)", re.I)
TAB_EN = re.compile(r"\bTables?\s*(\d+)", re.I)
FIG_ZH = re.compile(r"图\s*(\d+)")
TAB_ZH = re.compile(r"表\s*(\d+)")
NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
CAP_HEAD = re.compile(r"^(?:图|表)\s*\d+\s*[|｜]")


def key_nums(text):
    """只取「>=2 位或含小数」的数值，单数字噪音太大（5′UTR、n=2 之类）。

    千分位先整体匹配再拆逗号：原文 ``11,088`` 与译文 ``11088`` 视为同一个数，
    否则会被切成 11 / 088 两段而永远对不上。
    """
    out = set()
    for m in NUM.finditer(text or ""):
        s = m.group(0).replace(",", "")
        if len(s.replace(".", "")) >= 2:
            out.add(s)
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: verify.py <content.json> <figs_dir> [out.docx]")
        return 1
    cpath, figdir = sys.argv[1], sys.argv[2]
    docx_path = sys.argv[3] if len(sys.argv) > 3 else None
    C = json.load(open(cpath, encoding="utf-8"))
    warns = []

    sp = os.path.join(os.path.dirname(os.path.abspath(cpath)), "source.json")
    S = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else None

    body = C.get("body", [])
    front = C.get("front", [])
    kinds = {}
    for it in body:
        kinds[it["t"]] = kinds.get(it["t"], 0) + 1

    # ---------- 1. 进度
    def is_filled(it):
        if it["t"] in ("fig", "table"):
            return bool((it.get("caption") or "").strip())
        return bool((it.get("text") or "").strip())

    todo = [i for i, it in enumerate(body + front) if not is_filled(it)]
    if todo:
        warns.append(f"仍有 {len(todo)} 项未译（首项 seq={todo[0]}），先跑 fill.py --status")

    # ---------- 2. 图号与图文件
    fig_blocks = [it for it in body if it["t"] == "fig"]
    fig_nums = [it["n"] for it in fig_blocks]
    want = list(range(1, len(fig_nums) + 1))
    if sorted(set(fig_nums)) != want:
        warns.append(f"图号不连续或重复: {sorted(set(fig_nums))}（应为 1..{len(want)}）")
    on_disk = set()
    if os.path.isdir(figdir):
        for fn in os.listdir(figdir):
            m = re.match(r"^(fig|table)(\d+)\.png$", fn)
            if m:
                on_disk.add((m.group(1), int(m.group(2))))
    for it in fig_blocks:
        p = os.path.join(figdir, f"fig{it['n']}.png")
        if not os.path.exists(p):
            warns.append(f"缺图文件 fig{it['n']}.png")
        elif os.path.getsize(p) < 8000:
            warns.append(f"fig{it['n']}.png 体积过小，可能裁空了")
    extra = sorted(n for k, n in on_disk if k == "fig" and n not in set(fig_nums))
    if extra:
        warns.append(f"figs/ 里有多余图文件未被引用: {extra}（编号可能错位）")

    # ---------- 3. 数字一致性
    if S:
        miss = []
        for key, meta in S["items"].items():
            if meta["sect"] not in ("front", "body"):
                continue
            arr = C.get(meta["sect"]) or []
            if meta["i"] >= len(arr):
                continue
            it = arr[meta["i"]]
            dst = it.get("caption") if it["t"] in ("fig", "table") else it.get("text")
            if not dst:
                continue
            lack = key_nums(meta.get("src")) - key_nums(dst)
            if lack:
                miss.append((key, sorted(lack)))
        if miss:
            detail = "; ".join(f"#{k} 缺 {v}" for k, v in miss[:8])
            warns.append(f"{len(miss)} 处数字与原文不一致: {detail}"
                         f"{' …' if len(miss) > 8 else ''}")

    # ---------- 4. 图表引用完整性
    src_all = "\n".join(m.get("src") or "" for m in (S["items"].values() if S else []))
    txt_all = "\n".join(
        (it.get("caption") or it.get("text") or "") for it in body + front)
    if S:
        ref_fig = {int(x) for x in FIG_EN.findall(src_all)}
        ref_tab = {int(x) for x in TAB_EN.findall(src_all)}
        got_fig = {int(x) for x in FIG_ZH.findall(txt_all)}
        got_tab = {int(x) for x in TAB_ZH.findall(txt_all)}
        lost_f = sorted(ref_fig - got_fig)
        lost_t = sorted(ref_tab - got_tab)
        if lost_f:
            warns.append(f"正文引用了图 {lost_f}，但译文中找不到「图 N」")
        if lost_t:
            warns.append(f"正文引用了表 {lost_t}，但译文中找不到「表 N」")

    # ---------- 5. 公式误判
    for i, it in enumerate(body):
        if it["t"] != "eq":
            continue
        words = re.findall(r"[A-Za-z]{3,}", it.get("text") or "")
        if len(words) >= 6:
            snip = (it.get("text") or "")[:60]
            warns.append(f"body[{i}] 标为公式但含 {len(words)} 个英文单词，"
                         f"疑似正文段落被误判：{snip!r}")

    # ---------- 6. 标点与漏译
    for bad in ("。。", "，，", "；；", "、、", "（）"):
        n = txt_all.count(bad)
        if n:
            warns.append(f"标点异常 {bad!r} x{n}")

    FUNC = {"the", "and", "of", "is", "are", "was", "were", "with", "that", "for",
            "to", "in", "we", "this", "as", "by", "from", "which", "not", "on"}

    def looks_like_sequence(t):
        seq = sum(c in "ACGTUacgtu-′'" for c in t) / max(len(t), 1)
        return seq > 0.5 or "′-" in t

    pure_en = 0
    for b in body:
        if b["t"] != "p" or len(b.get("text") or "") < 40:
            continue
        t = b["text"]
        if CJK.search(t) or looks_like_sequence(t):
            continue
        words = [w.lower() for w in re.findall(r"[A-Za-z']+", t)]
        if len(words) > 12 and sum(w in FUNC for w in words) >= 3:
            pure_en += 1
    if pure_en:
        warns.append(f"{pure_en} 个正文段落疑似未翻译")

    # ---------- 7. docx 结构
    if docx_path and os.path.exists(docx_path):
        from docx import Document
        d = Document(docx_path)
        imgs = len(d.inline_shapes)
        if imgs != len(fig_blocks):
            warns.append(f"docx 内图片 {imgs} 张，content 中图 {len(fig_blocks)} 个")
        tables = len(d.tables)
        want_t = kinds.get("table", 0)
        if tables != want_t:
            warns.append(f"docx 表格 {tables} 个，content 中表 {want_t} 个")
        empty = sum(1 for p in d.paragraphs if not p.text.strip() and not p._p.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"))
        print(f"docx: 段落 {len(d.paragraphs)}（空段 {empty}）图片 {imgs} 表格 {tables}")

    print("content:", {k: v for k, v in sorted(kinds.items())},
          "refs", len(C.get("refs", [])), "figs", len(fig_blocks),
          "未译", len(todo))
    if warns:
        print("\n[告警]")
        for w in warns:
            print(" -", w)
        # 未译属于「还没做完」，与「做完了但有疑点」区分开
        return 2 if todo else 1
    print("\n[通过] 结构自检无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
