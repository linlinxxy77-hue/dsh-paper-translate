# -*- coding: utf-8 -*-
"""check_layout.py —— 生成一张校验拼版：每页缩略图 + 检测框，一次看完所有图区。

用法: python check_layout.py <pdf> <structure.json> <out.png>
"""
import json
import sys

import _deps
_deps.ensure()
import pymupdf
from PIL import Image, ImageDraw

DPI = 72
COLS = 5
PAD = 6


def main():
    if len(sys.argv) < 4:
        print("usage: check_layout.py <pdf> <structure.json> <out.png>")
        return 1
    pdf, spath, out = sys.argv[1], sys.argv[2], sys.argv[3]
    st = json.load(open(spath, encoding="utf-8"))
    doc = pymupdf.open(pdf)
    by_page = {}
    for f in st["figures"]:
        by_page.setdefault(f["page"], []).append(f)
    pages = sorted(by_page)
    if not pages:
        print("no figures")
        return 0

    thumbs = []
    for pno in pages:
        page = doc[pno - 1]
        pix = page.get_pixmap(dpi=DPI)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        d = ImageDraw.Draw(im)
        for f in by_page[pno]:
            x0, y0, x1, y1 = f["bbox"]
            role = f.get("role")
            if role == "graphical_abstract":
                color = (30, 150, 80)        # 绿：图形摘要（正常，不进正文）
            elif role == "orphan":
                color = (210, 130, 0)        # 橙：无图注，待人工判断
            elif f["kind"] == "figure":
                color = (220, 40, 40)        # 红：正文插图
            else:
                color = (30, 120, 200)       # 蓝：表格
            d.rectangle([x0, y0, x1, y1], outline=color, width=3)
            label = f.get("fig_name") or (f"GA#{f['id']}" if role == "graphical_abstract"
                                          else f"orphan#{f['id']}" if role == "orphan"
                                          else f"#{f['id']} {f['kind']}")
            d.text((x0 + 5, max(2, y0 - 12)), label, fill=color)
        thumbs.append((pno, im))

    w, h = thumbs[0][1].size
    rows = (len(thumbs) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * (w + PAD) + PAD, rows * (h + PAD + 14) + PAD), "white")
    d = ImageDraw.Draw(sheet)
    for i, (pno, im) in enumerate(thumbs):
        r, c = divmod(i, COLS)
        x = PAD + c * (w + PAD)
        y = PAD + r * (h + PAD + 14)
        d.text((x, y), f"p{pno}", fill=(0, 0, 0))
        sheet.paste(im, (x, y + 14))
        d.rectangle([x, y + 14, x + w - 1, y + 14 + h - 1], outline=(180, 180, 180), width=1)
    sheet.save(out)
    print("montage ->", out, sheet.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
