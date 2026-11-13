# -*- coding: utf-8 -*-
"""make_skeleton.py —— 由 structure.json 生成待翻译的 content.json 骨架 + 原文对照表。

只输出结构（章节层级、图表位置、参考文献），需要翻译的正文 text 留空。

v3 的关键改动：**不需要翻译的内容直接预填**，模型只填中文。
  - refs：参考文献原样保留英文 → 直接写入，模型不必重抄（一篇 80 条 ≈ 6k token）
  - eq：公式行不翻译 → 直接写入原文
  - 图形摘要（graphical_abstract）与无主图区（orphan）不进入正文

同时产出 source.json：全局序号 → 原文。fill.py 靠它给出「下一批要译什么」，
模型因此不需要读 content.json，也不需要输出任何 JSON。

用法: python make_skeleton.py <structure.json> <out.json>
"""
import json
import os
import sys

AUTO_TYPES = ("title", "authors", "affil", "meta", "abstract", "h1", "h2", "p", "fig", "table")


def main():
    if len(sys.argv) < 3:
        print("usage: make_skeleton.py <structure.json> <out.json>")
        return 1
    st = json.load(open(sys.argv[1], encoding="utf-8"))
    blocks, figs = st["blocks"], st["figures"]
    fig_at = {}
    for f in figs:
        if f.get("caption_block") is not None and not f.get("role"):
            fig_at[f["caption_block"]] = f

    front, body, refs, back = [], [], [], []
    # 与 front / body / back 平行的原文记录：{"src": 文本, "grid": 表格网格或 None}
    src_front, src_body, src_back = [], [], []
    seen_abs = False
    prefilled_eq = 0
    prefilled_table = 0
    table_cells = 0
    skipped_fig = 0

    def rec(txt, grid=None):
        return {"src": txt, "grid": grid}

    def add_front(item, src, grid=None):
        front.append(item)
        src_front.append(rec(src, grid))

    def add_body(item, src, grid=None):
        body.append(item)
        src_body.append(rec(src, grid))

    for i, b in enumerate(blocks):
        t, txt = b["type"], b["text"]
        if t == "title" and not front:
            add_front({"t": "title", "text": ""}, txt)
        elif t == "h2" and len(front) < 3 and "authors" not in {x["t"] for x in front}:
            add_front({"t": "authors", "text": ""}, txt)
        elif t == "caption" and i in fig_at:
            f = fig_at[i]
            if f["kind"] == "table":
                g = f.get("grid") or {}
                header = [str(c) for c in (g.get("header") or [])]
                rows = [[str(c) for c in r] for r in (g.get("rows") or [])]
                item = {"t": "table", "n": f.get("caption_num") or f["id"],
                        "caption": "", "header": header, "rows": rows,
                        "note": "", "_src_block": i}
                if not header:
                    item["_manual"] = True          # 没重建出来，需人工填
                elif not g.get("confident", True):
                    item["_manual"] = True
                    item["_grid_note"] = g.get("reason", "")
                else:
                    prefilled_table += 1
                    table_cells += len(header) + sum(len(r) for r in rows)
                add_body(item, txt, grid=g)
            else:
                add_body({"t": "fig", "n": f.get("caption_num") or f["id"],
                          "caption": ""}, txt)
        elif t == "caption":
            # 未绑定图区的图注（图形摘要/orphan 的说明文字）不进正文
            skipped_fig += 1
            if not seen_abs and not body:
                add_front({"t": "abstract", "text": ""}, txt)
                seen_abs = True
            else:
                add_body({"t": "p", "text": ""}, txt)
        elif t == "h1":
            add_body({"t": "h1", "text": ""}, txt)
        elif t == "abstract":
            add_front({"t": "abstract", "text": ""}, txt)
            seen_abs = True
        elif t == "h2":
            add_body({"t": "h2", "text": ""}, txt)
        elif t == "p":
            add_body({"t": "p", "text": ""}, txt)
        elif t == "eq":
            add_body({"t": "eq", "text": txt}, txt)     # 公式不译，预填
            prefilled_eq += 1
        elif t == "ref":
            refs.append(txt)                             # 文献不译，预填
        elif t in ("refhead", "small"):
            continue
        else:
            add_body({"t": "p", "text": ""}, txt)

    content = {
        "meta": {"header_left": "", "header_right": "", "footer": ""},
        "front": front, "body": body, "refs": refs, "back": back,
    }
    out = sys.argv[2]
    json.dump(content, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    nf = len(front)
    nb = len(body)
    source = {}
    for k, (item, r) in enumerate(zip(front, src_front)):
        source[str(k)] = {"sect": "front", "i": k, "t": item["t"], "src": r["src"]}
    for k, (item, r) in enumerate(zip(body, src_body)):
        node = {"sect": "body", "i": k, "t": item["t"], "src": r["src"]}
        if r.get("grid"):                      # 表格：原文网格也留一份给 --next / 数字比对
            node["grid"] = {"header": r["grid"].get("header") or [],
                            "rows": r["grid"].get("rows") or []}
        source[str(nf + k)] = node
    for k, (item, r) in enumerate(zip(back, src_back)):
        source[str(nf + nb + k)] = {"sect": "back", "i": k, "t": item["t"], "src": r["src"]}
    spath = os.path.join(os.path.dirname(os.path.abspath(out)), "source.json")
    json.dump({"count": len(source), "items": source},
              open(spath, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    kinds = {}
    for x in body:
        kinds[x["t"]] = kinds.get(x["t"], 0) + 1
    todo = sum(1 for x in body if not x.get("text")) + sum(1 for x in front if not x.get("text"))
    print("skeleton:", kinds, "front", len(front), "refs", len(refs), "->", out)
    line = f"  已预填: 参考文献 {len(refs)} 条 / 公式 {prefilled_eq} 行"
    if prefilled_table:
        line += f" / 表格 {prefilled_table} 个 {table_cells} 格"
    print(line + "（文献与公式免译）")
    if prefilled_table:
        print("           表格行列已从坐标重建，只需替换文字，列数不要改")
    manual_t = sum(1 for x in body if x.get("_manual"))
    if manual_t:
        print(f"  ! 表格 {manual_t} 个行列不齐，已留空待人工填（见 content.json 的 _grid_note）")
    print(f"  待翻译: {todo} 项；跳过未绑定图注 {skipped_fig} 处")
    print(f"  原文对照: {spath}（{len(source)} 项，供 fill.py 使用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
