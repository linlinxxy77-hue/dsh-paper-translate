# -*- coding: utf-8 -*-
"""build_docx.py —— 由 content.json 生成中文译文 DOCX（通用排版引擎）。

用法:
    python build_docx.py <content.json> <figs_dir> <out.docx>

content.json 结构:
{
  "meta":   {"header_left": "...", "header_right": "...", "footer": "..."},
  "front":  [{"t":"title|authors|affil|meta|abstract", "text": "..."}],
  "body":   [{"t":"h1|h2|p", "text":"..."},
             {"t":"fig", "n":1, "caption":"图 1 | ..."},
             {"t":"table", "caption":"...", "header":[...], "rows":[[...]], "note":"..."}],
  "refs":   ["参考文献 1 ...", ...],
  "back":   [{"t":"h2|p", "text":"..."}]
}

文本中 ~{x} 表示上标，Unicode 上标字符会自动转换。
"""
import json
import os
import re
import sys

import _deps
_deps.ensure()
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image

CN, CN_HEAD, EN = "宋体", "黑体", "Times New Roman"
BODY_PT = 10.5
SUP_RE = re.compile(r"~\{([^{}]*)\}")
SUP_MAP = {"\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3", "\u2074": "4",
           "\u2075": "5", "\u2076": "6", "\u2077": "7", "\u2078": "8", "\u2079": "9",
           "\u207b": "\u2212", "\u207a": "+", "\u2071": "i", "\u207f": "n"}
SUPCHARS = "".join(SUP_MAP)


def normalize_sup(text):
    return re.sub(f"[{SUPCHARS}]+",
                  lambda m: "~{" + "".join(SUP_MAP[c] for c in m.group(0)) + "}", text)


def set_run_font(run, en=EN, cn=CN, size=None, bold=None, color=None):
    run.font.name = en
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), en)
    rFonts.set(qn("w:hAnsi"), en)
    rFonts.set(qn("w:eastAsia"), cn)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color:
        run.font.color.rgb = color


def add_rich_text(p, text, size=BODY_PT, cn=CN, en=EN, bold=False, color=None):
    text = normalize_sup(text)
    pos = 0
    for m in SUP_RE.finditer(text):
        if m.start() > pos:
            r = p.add_run(text[pos:m.start()])
            set_run_font(r, en=en, cn=cn, size=size, bold=bold, color=color)
        r = p.add_run(m.group(1))
        set_run_font(r, en=en, cn=cn, size=size, bold=bold, color=color)
        r.font.superscript = True
        pos = m.end()
    if pos < len(text):
        r = p.add_run(text[pos:])
        set_run_font(r, en=en, cn=cn, size=size, bold=bold, color=color)


def set_spacing(p, before=0, after=0, line=1.28):
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    pf.widow_control = True


def para_border_bottom(p, sz=6, color="808080"):
    pPr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    b.set(qn("w:val"), "single")
    b.set(qn("w:sz"), str(sz))
    b.set(qn("w:space"), "4")
    b.set(qn("w:color"), color)
    pbdr.append(b)
    pPr.insert_element_before(
        pbdr,
        "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
        "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
        "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
        "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
        "w:textDirection", "w:textAlignment", "w:textboxTightWrap",
        "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
        "w:pPrChange",
    )


def add_page_number(p, size=8.5):
    run = p.add_run()
    set_run_font(run, size=size)
    f1 = OxmlElement("w:fldChar"); f1.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve"); it.text = " PAGE "
    f2 = OxmlElement("w:fldChar"); f2.set(qn("w:fldCharType"), "end")
    for e in (f1, it, f2):
        run._r.append(e)


def cell_borders(cell, top=None, bottom=None):
    tcPr = cell._tc.get_or_add_tcPr()
    bs = tcPr.find(qn("w:tcBorders"))
    if bs is None:
        bs = OxmlElement("w:tcBorders")
        tcPr.append(bs)
    for name, sz in (("top", top), ("bottom", bottom)):
        if sz:
            el = OxmlElement(f"w:{name}")
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), str(sz))
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "000000")
            bs.append(el)


def main():
    if len(sys.argv) < 4:
        print("usage: build_docx.py <content.json> <figs_dir> <out.docx>")
        return 1
    cpath, figdir, out = sys.argv[1], sys.argv[2], sys.argv[3]
    C = json.load(open(cpath, encoding="utf-8"))
    meta = C.get("meta", {})

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.3)
    sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(2.3)
    sec.header_distance, sec.footer_distance = Cm(1.3), Cm(1.3)

    st = doc.styles["Normal"]
    st.font.name = EN
    st.font.size = Pt(BODY_PT)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), CN)
    st.paragraph_format.line_spacing = 1.28
    st.paragraph_format.space_after = Pt(0)

    if meta.get("header_left") or meta.get("header_right"):
        hdr = sec.header.paragraphs[0]
        parts = [x for x in (meta.get("header_left"), meta.get("header_right")) if x]
        r = hdr.add_run("　｜　".join(parts))
        set_run_font(r, size=8)
        para_border_bottom(hdr)

    if meta.get("footer"):
        ftr = sec.footer.paragraphs[0]
        ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = ftr.add_run(meta["footer"] + "　·　第 ")
        set_run_font(r, size=8.5)
        add_page_number(ftr)
        r = ftr.add_run(" 页")
        set_run_font(r, size=8.5)

    def h1(text, before=15):
        p = doc.add_paragraph()
        set_spacing(p, before, 7, 1.2)
        p.paragraph_format.keep_with_next = True
        add_rich_text(p, text, size=14, cn=CN_HEAD, bold=True)

    def h2(text):
        p = doc.add_paragraph()
        set_spacing(p, 11, 4, 1.2)
        p.paragraph_format.keep_with_next = True
        add_rich_text(p, text, size=11.5, cn=CN_HEAD, bold=True)

    def body(text):
        p = doc.add_paragraph()
        set_spacing(p, 0, 3.5, 1.3)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Pt(BODY_PT * 2)
        add_rich_text(p, text, size=BODY_PT)

    def equation(text):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, 6, 6, 1.2)
        add_rich_text(p, text, size=BODY_PT)

    def figure(n, caption):
        path = os.path.join(figdir, f"fig{n}.png")
        if not os.path.exists(path):
            print("! missing figure", path)
            return
        with Image.open(path) as im:
            w_px, h_px = im.size
        w_cm, max_w, max_h = 15.4, 15.4, 20.0
        h_cm = w_cm * h_px / w_px
        if h_cm > max_h:
            h_cm, w_cm = max_h, max_h * w_px / h_px
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, 10, 5, 1.0)
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(path, width=Cm(w_cm))
        cp = doc.add_paragraph()
        cp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        set_spacing(cp, 0, 12, 1.22)
        if "\u3002" in caption:
            head, rest = caption.split("\u3002", 1)
            add_rich_text(cp, head + "\u3002", size=9, bold=True)
            add_rich_text(cp, rest, size=9)
        else:
            add_rich_text(cp, caption, size=9, bold=True)

    def table(tb):
        cp = doc.add_paragraph()
        set_spacing(cp, 9, 5, 1.2)
        cp.paragraph_format.keep_with_next = True
        add_rich_text(cp, tb["caption"], size=9, bold=True)
        ncol = len(tb["header"])
        t = doc.add_table(rows=1 + len(tb["rows"]), cols=ncol)
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        widths = [Cm(15.4 / ncol)] * ncol
        if tb.get("widths"):
            widths = [Cm(w) for w in tb["widths"]]

        def fill(cell, text, bold=False, size=8.5):
            cell.text = ""
            p = cell.paragraphs[0]
            set_spacing(p, 1.6, 1.6, 1.1)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_rich_text(p, text, size=size, bold=bold)

        for j, h in enumerate(tb["header"]):
            c = t.rows[0].cells[j]
            fill(c, h, bold=True)
            cell_borders(c, top=12, bottom=6)
        for i, row in enumerate(tb["rows"]):
            for j, v in enumerate(row):
                c = t.rows[i + 1].cells[j]
                fill(c, v, bold=(j == 0 and tb.get("first_col_bold", True)))
                if i == len(tb["rows"]) - 1:
                    cell_borders(c, bottom=12)
        for trow in t.rows:
            for j, c in enumerate(trow.cells):
                c.width = widths[j]
        if tb.get("note"):
            np_ = doc.add_paragraph()
            np_.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            set_spacing(np_, 5, 13, 1.2)
            add_rich_text(np_, tb["note"], size=9)

    for item in C.get("front", []):
        t = item["t"]
        p = doc.add_paragraph()
        if t == "title":
            set_spacing(p, 4, 12, 1.25)
            add_rich_text(p, item["text"], size=19, cn=CN_HEAD, bold=True)
        elif t == "authors":
            set_spacing(p, 0, 8, 1.3)
            add_rich_text(p, item["text"], size=10)
        elif t == "affil":
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            set_spacing(p, 0, 6, 1.22)
            add_rich_text(p, item["text"], size=8)
        elif t == "meta":
            set_spacing(p, 2, 11, 1.2)
            add_rich_text(p, item["text"], size=8.5, color=RGBColor(0x50, 0x50, 0x50))
        elif t == "abstract":
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            set_spacing(p, 0, 8, 1.3)
            r = p.add_run("摘要　")
            set_run_font(r, size=9.5, bold=True, cn=CN_HEAD)
            add_rich_text(p, item["text"], size=9.5)
            sep = doc.add_paragraph()
            set_spacing(sep, 0, 8, 1.0)
            para_border_bottom(sep, sz=4, color="BFBFBF")

    for item in C.get("body", []):
        t = item["t"]
        if t == "h1":
            h1(item["text"], before=item.get("before", 15))
        elif t == "h2":
            h2(item["text"])
        elif t == "p":
            body(item["text"])
        elif t == "eq":
            equation(item["text"])
        elif t == "fig":
            figure(item["n"], item["caption"])
        elif t == "table":
            table(item)

    refs = C.get("refs") or []
    if refs:
        h1("参考文献", before=16)
        for ref in refs:
            p = doc.add_paragraph()
            set_spacing(p, 0, 3, 1.18)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.first_line_indent = Cm(-0.75)
            add_rich_text(p, ref, size=8)

    for item in C.get("back", []):
        if item["t"] == "h2":
            h2(item["text"])
        else:
            body(item["text"])

    doc.save(out)
    print("saved", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
