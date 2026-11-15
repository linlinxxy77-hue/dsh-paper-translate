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
import re
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
    # 标题作者段已经结束（一旦进入正文/摘要就不再允许新的 title/authors 进入 front）
    front_done = False

    def rec(txt, grid=None):
        return {"src": txt, "grid": grid}

    def add_front(item, src, grid=None):
        front.append(item)
        src_front.append(rec(src, grid))

    def add_body(item, src, grid=None):
        body.append(item)
        src_body.append(rec(src, grid))

    def _has_author(item):
        return item["t"] == "authors"

    for i, b in enumerate(blocks):
        t, txt = b["type"], b["text"]
        # title 无条件进 front：论文标题语义上就是 front 首位，不能被前面的脏块
        # （如第 4 篇 `e59` 这类页眉残留 h2）挡住而降级成 p（否则 fill 按 t==p 报「未见中文」）。
        # 方案 C：正文开始前的 h1 也可能是真标题（第 3、5 篇标题被抽取层标成 h1 而非 title），
        # 只要 front 里还没有 title 就把它当标题收进来；一旦正文/摘要开始（front_done）就只当小标题。
        if t == "title" and not front_done:
            add_front({"t": "title", "text": ""}, txt)
        elif t == "h1" and not front_done and not any(x["t"] == "title" for x in front) \
                and not seen_abs and not body:
            add_front({"t": "title", "text": ""}, txt)
        elif t == "h2" and len(front) < 3 and not front_done \
                and not any(x["t"] == "authors" for x in front):
            # 方案 D：收紧 h2→authors 的误抓。原判据「front<3 且无 authors」会把
            # 摘要里的 `Results`/`Abstract` 标题、`摘要`、`e59` 页眉残留误当作者行。
            # 新增四条：页眉残留词直接排除；块位置必须在首页最前；作者行通常含
            # 数字(上标/单位)或标点(逗号/顿号/分号分隔)或常见作者特征词；
            # 一旦 seen_abs / body 已开始就绝不再当作者。
            tt = txt.strip()
            head_hit = bool(re.search(
                r"(?i)^(?:h[0-9]|results?|abstract|introduction|methods?|materials|discussion|"
                r"conclusions?|references|supplementary|acknowledg|author manuscript|"
                r"published in final edited form|doi\b|摘要|引言|结果|方法|讨论|结论|参考文献)", tt))
            short_numeral = bool(re.match(r"^[\w\s.,:;'’·-]{0,3}$", tt) or
                                 re.match(r"^[A-Za-z]{1,4}\d*$", tt))
            authorish = bool(re.search(r"\d", tt) or re.search(r"[,，;；·]", tt) or
                             re.search(r"(?i)(university|institute|department|college|"
                                       r"laboratory|centre|center|school of|@)", tt))
            if not head_hit and not short_numeral and authorish:
                add_front({"t": "authors", "text": ""}, txt)
            else:
                add_body({"t": "h2", "text": ""}, txt)
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
            front_done = True
        elif t == "abstract":
            add_front({"t": "abstract", "text": ""}, txt)
            seen_abs = True
            front_done = True
        elif t == "h2":
            add_body({"t": "h2", "text": ""}, txt)
        elif t == "p":
            add_body({"t": "p", "text": ""}, txt)
            front_done = True
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
