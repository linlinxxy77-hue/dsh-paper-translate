# -*- coding: utf-8 -*-
"""build_summary.py —— 把 summary.json 排成【总结】DOCX（单篇或多篇合一份）。

用法:
    python build_summary.py <out.docx> <workdir> [<workdir> ...]

每个 workdir 需已有 summary.json（summarize.py --check 通过后才有）。
单篇：标题 = 中文标题，五节正文。
多篇：先目录（序号 + 中英文名），再每篇一章，页间分页。

排版与译文共用一套字体函数（build_docx.py），字号略放大，便于通读。
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _deps
_deps.ensure()
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

from build_docx import (BODY_PT, CN, CN_HEAD, EN, add_page_number, add_rich_text,
                        normalize_sup, para_border_bottom, set_run_font, set_spacing)

GRAY = RGBColor(0x59, 0x59, 0x59)
LIGHT = RGBColor(0x80, 0x80, 0x80)
SECTIONS = [("purpose", "研究目的"), ("methods", "研究方法"), ("difficulty", "研究难点"),
            ("novelty", "创新点"), ("limitation", "局限")]
CN_NUM = "一二三四五六七八九十"


def load_one(workdir):
    p = os.path.join(workdir, "summary.json")
    if not os.path.exists(p):
        raise SystemExit(f"! 缺少 {p}（先跑 summarize.py <workdir> --pack，模型写完再 --check）")
    S = json.load(open(p, encoding="utf-8"))
    S["_workdir"] = workdir
    C = os.path.join(workdir, "content.json")
    S["_docx"] = ""
    if os.path.exists(C):
        c = json.load(open(C, encoding="utf-8"))
        S["_journal"] = ((c.get("meta") or {}).get("header_right") or "").strip()
        S["_doi"] = ((c.get("meta") or {}).get("footer") or "").strip()
    else:
        S.setdefault("_journal", "")
        S.setdefault("_doi", "")
    return S


def source_hint(S):
    parts = []
    if S.get("_journal"):
        parts.append(S["_journal"])
    if S.get("_doi") and S["_doi"] not in S.get("_journal", ""):
        parts.append(S["_doi"])
    return "　·　".join(parts)


def make_doc(rows, out):
    multi = len(rows) > 1
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.5)
    sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(2.3)

    st = doc.styles["Normal"]
    st.font.name = EN
    st.font.size = Pt(BODY_PT)
    st.element.rPr.rFonts.set(qn_eastasia(), CN)
    st.paragraph_format.line_spacing = 1.35
    st.paragraph_format.space_after = Pt(0)

    ftr = sec.footer.paragraphs[0]
    ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = ftr.add_run("paper-translate · 文献总结　·　第 ")
    set_run_font(r, size=8.5, color=LIGHT)
    add_page_number(ftr)
    r = ftr.add_run(" 页")
    set_run_font(r, size=8.5, color=LIGHT)

    def doc_title(text):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, 0, 6, 1.25)
        add_rich_text(p, text, size=15.5, cn=CN_HEAD, bold=True)

    def sub_line(text, size=10, italic=False, color=GRAY, before=0, after=2):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, before, after, 1.2)
        r = p.add_run(normalize_sup(text))
        set_run_font(r, size=size, color=color)
        r.italic = italic
        return p

    def h1(text):
        p = doc.add_paragraph()
        set_spacing(p, 16, 6, 1.2)
        p.paragraph_format.keep_with_next = True
        add_rich_text(p, text, size=13, cn=CN_HEAD, bold=True)
        para_border_bottom(p, sz=4, color="BFBFBF")

    def h2(text):
        p = doc.add_paragraph()
        set_spacing(p, 11, 3, 1.2)
        p.paragraph_format.keep_with_next = True
        add_rich_text(p, text, size=11, cn=CN_HEAD, bold=True)

    def body(text, gray=False):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        set_spacing(p, 0, 4, 1.45)
        p.paragraph_format.first_line_indent = Pt(BODY_PT * 2)
        add_rich_text(p, text, size=BODY_PT, color=GRAY if gray else None)
        return p

    def note(text):
        p = doc.add_paragraph()
        set_spacing(p, 14, 0, 1.2)
        add_rich_text(p, text, size=8.5, color=LIGHT)

    if multi:
        doc_title("文献总结")
        sub_line(f"共 {len(rows)} 篇　·　生成于 {datetime.date.today().isoformat()}",
                 size=10, color=LIGHT, after=10)
        h2("目录")
        for i, S in enumerate(rows, 1):
            p = doc.add_paragraph()
            set_spacing(p, 2, 2, 1.3)
            p.paragraph_format.left_indent = Pt(10)
            add_rich_text(p, f"{i}. {S.get('title_zh') or '（无标题）'}", size=10.5)
            en = (S.get("title_en") or "").strip()
            if en:
                q = doc.add_paragraph()
                set_spacing(q, 0, 3, 1.2)
                q.paragraph_format.left_indent = Pt(24)
                r = q.add_run(normalize_sup(en))
                set_run_font(r, size=8.5, color=LIGHT)
                r.italic = True

    for i, S in enumerate(rows, 1):
        if i > 1 or multi:
            doc.add_page_break()
        zh = (S.get("title_zh") or "").strip() or "（未提供中文标题）"
        en = (S.get("title_en") or "").strip()
        if multi:
            doc_title(f"{i}. {zh}")
        else:
            doc_title(zh + "【总结】")
        if en:
            sub_line(en, size=9.5, italic=True, color=GRAY, before=2)
        hint = source_hint(S)
        if hint:
            sub_line(hint, size=8.5, color=LIGHT, before=3, after=6)
        else:
            sub_line("", size=8, after=4)

        for idx, (key, label) in enumerate(SECTIONS):
            node = (S.get("sections") or {}).get(key) or {}
            text = (node.get("text") if isinstance(node, dict) else str(node)) or ""
            text = text.strip()
            if not text:
                continue
            h2(f"{CN_NUM[idx]}、{label}")
            body(text, gray=("未明确" in text[:12]))

        note("本总结由 paper-translate 依据原文自动生成，供快速把握文献要点；"
             "涉及具体数值与结论请核对原文。")

    doc.save(out)
    return out


def qn_eastasia():
    from docx.oxml.ns import qn
    return qn("w:eastAsia")


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 1
    out, dirs = argv[0], argv[1:]
    rows = []
    for d in dirs:
        if not os.path.isdir(d):
            print("! 工作目录不存在:", d)
            return 1
        rows.append(load_one(d))
    make_doc(rows, out)
    print(f"saved: {out}（{len(rows)} 篇，{os.path.getsize(out) / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
