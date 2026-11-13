# -*- coding: utf-8 -*-
"""crop_figs.py —— 按 structure.json 裁图（300dpi）+ 去白边 + 生成检查拼版。

用法: python crop_figs.py <pdf> <structure.json> <outdir>
                           [--dpi 300] [--max-px 2400] [--no-montage]

产出:
    fig<N>.png              正文插图（N 取自图注编号 caption_num，不是检出顺序）
    table<N>.png            表格区域预览（仅用于人工核对，不参与排版）
    _graphical_abstract.png 图形摘要（单独存放，不占 figN）
    _orphan_<id>.png        无图注的疑似图区（待人工判断）
    _figs_check.png         全部裁图的拼版，一次看完

v3：文件命名改用 caption_num，与 build_docx / verify 的引用彻底对齐；
    图形摘要与无主图区单独导出，不再挤进 fig1..N 造成编号错位。
"""
import json
import os
import sys

import _deps
_deps.ensure()
import pymupdf
from PIL import Image, ImageDraw


def trim_white(im, pad=8, thresh=245):
    """去掉四周纯白边，保留 pad 像素余量。
    若裁完只剩不到 30% 面积，说明整图偏白（如浅底图），放弃裁切以免切坏。"""
    gray = im.convert("L").point(lambda v: 255 if v < thresh else 0)
    box = gray.getbbox()
    if not box:
        return im
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(im.width, x1 + pad), min(im.height, y1 + pad)
    if (x1 - x0) * (y1 - y0) < 0.30 * im.width * im.height:
        return im
    return im.crop((x0, y0, x1, y1))


def limit_px(im, max_px):
    if max_px and im.width > max_px:
        h = round(im.height * max_px / im.width)
        return im.resize((max_px, h), Image.LANCZOS)
    return im


def target_name(f):
    """按角色/编号决定输出文件名。"""
    if f.get("role") == "graphical_abstract":
        return "_graphical_abstract.png"
    if f.get("role") == "orphan":
        return f"_orphan_{f['id']}.png"
    if f.get("fig_name"):
        return f["fig_name"] + ".png"
    return f"fig{f['id']}.png"


def main():
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 3:
        print("usage: crop_figs.py <pdf> <structure.json> <outdir>")
        return 1
    pdf, spath, outdir = args[0], args[1], args[2]
    dpi, max_px = 300, 2400
    for a in argv:
        if a.startswith("--dpi"):
            dpi = int(a.split("=")[1]) if "=" in a else 300
        if a.startswith("--max-px"):
            max_px = int(a.split("=")[1]) if "=" in a else 2400
    do_montage = "--no-montage" not in argv
    os.makedirs(outdir, exist_ok=True)

    st = json.load(open(spath, encoding="utf-8"))
    ov_path = os.path.join(os.path.dirname(os.path.abspath(spath)), "figs_override.json")
    override = {}
    if os.path.exists(ov_path):
        override = json.load(open(ov_path, encoding="utf-8"))
        print("override:", {k: v for k, v in override.items()})

    doc = pymupdf.open(pdf)
    made = []
    for f in st["figures"]:
        name = target_name(f)
        box = None
        for key in (str(f.get("caption_num")), str(f["id"])):
            if key and key in override:
                box = override[key]
                break
        if box is None:
            box = f["bbox"]
        page = doc[f["page"] - 1]
        pix = page.get_pixmap(clip=pymupdf.Rect(*box), dpi=dpi)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if f.get("kind") == "figure" or f.get("role"):
            im = trim_white(im)
        im = limit_px(im, max_px)
        path = os.path.join(outdir, name)
        im.save(path)
        tag = name.rsplit(".", 1)[0]
        made.append((tag, path, im.size))
        print(f"{tag} p{f['page']} {im.width}x{im.height}")

    if do_montage and made:
        cols, cw, ch = 4, 460, 620
        rows = (len(made) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
        dr = ImageDraw.Draw(sheet)
        for i, (tag, path, _) in enumerate(made):
            im = Image.open(path)
            im.thumbnail((cw - 16, ch - 34))
            x = (i % cols) * cw + (cw - im.width) // 2
            y = (i // cols) * ch + 26
            sheet.paste(im, (x, y))
            dr.text(((i % cols) * cw + 8, (i // cols) * ch + 8), tag, fill=(0, 0, 0))
        mp = os.path.join(outdir, "_figs_check.png")
        sheet.save(mp)
        print("montage ->", mp, sheet.size)

    ga = sum(1 for f in st["figures"] if f.get("role") == "graphical_abstract")
    orp = sum(1 for f in st["figures"] if f.get("role") == "orphan")
    names = [m[0] for m in made if m[0].startswith(("fig", "table"))]
    print(f"crop: {len(names)} 正文图 {names} | 图形摘要 {ga} | 无主图区 {orp}")
    if orp:
        print("  ! 无主图区已单独导出为 _orphan_*.png，请确认是否漏识别了图注")
    return 0


if __name__ == "__main__":
    sys.exit(main())
