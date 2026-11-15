# -*- coding: utf-8 -*-
"""fill.py —— 增量回填译文（模型只输出纯文本，不碰 JSON）。

用法:
    python fill.py <content.json> --status             # 进度 + 下一批建议
    python fill.py <content.json> --next 20            # 打印下 20 个待译项（含原文）
    python fill.py <content.json> --file batch.txt     # 把译文写回 content.json
    python fill.py <content.json> --file batch.txt --dry-run

批文件格式（每条以 @@序号 开头，序号取自 --next 的输出）:

    @@12
    中文译文第一段……

    @@13
    图 1 | 首句加粗的图注。其余说明文字。

设计要点:
  - 模型输出量 = 译文本身。不再重写整个 content.json（省掉结构字段与转义，
    约占原输出量的 15–25%），也不会因 JSON 语法错误导致整批丢失。
  - 参考文献与公式已在骨架里预填，不进入待译队列。
  - --status 给出「已填/待填/建议下一批」，长论文中断后可直接续译。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import glossary as G            # noqa: E402  术语口径（跨批次一致）
import journal as J             # noqa: E402  过程记录：本批发现的问题当场落盘
from verify import key_nums, keep_original_text  # noqa: E402  数字比对 + 保留原文判定（与交付自检同一套判据）

HDR = re.compile(r"^@@\s*(\d+)\s*$")
CJK = re.compile(r"[\u4e00-\u9fff]")
FIG_PREFIX = re.compile(r"^图\s*\d*\s*[|｜]\s*")
TAB_PREFIX = re.compile(r"^表\s*\d*\s*[|｜]\s*")
EN_PREFIX = re.compile(r"^(?:Table|Figure|Figs?\.?)\s*\d*\s*[|｜]\s*", re.I)


def load(cpath):
    C = json.load(open(cpath, encoding="utf-8"))
    sp = os.path.join(os.path.dirname(os.path.abspath(cpath)), "source.json")
    if not os.path.exists(sp):
        print("! 找不到 source.json（应与 content.json 同目录）。"
              "请用 make_skeleton.py 重新生成骨架。")
        return C, None
    return C, json.load(open(sp, encoding="utf-8"))


def filled(item):
    if item["t"] in ("fig", "table"):
        return bool((item.get("caption") or "").strip())
    return bool((item.get("text") or "").strip())


def walk(C, S):
    for key in sorted(S["items"], key=lambda k: int(k)):
        meta = S["items"][key]
        arr = C.get(meta["sect"]) or []
        if meta["i"] >= len(arr):
            continue
        yield int(key), meta, arr[meta["i"]]


def do_status(C, S):
    rows = {"front": [0, 0], "body": [0, 0], "back": [0, 0]}
    pending = []
    for idx, meta, item in walk(C, S):
        sect = meta["sect"]
        rows[sect][1] += 1
        if filled(item):
            rows[sect][0] += 1
        else:
            pending.append(idx)
    total = sum(v[1] for v in rows.values())
    done = sum(v[0] for v in rows.values())
    for sect in ("front", "body", "back"):
        d, t = rows[sect]
        if t:
            print(f"  {sect:<6} {d}/{t}")
    print(f"  合计   {done}/{total}")
    if not pending:
        print("RESULT: DONE 全部已填")
        return 0
    print(f"下一批建议: {pending[0]}–{pending[min(19, len(pending) - 1)]}"
          f"（剩余 {len(pending)} 项；缺口 {pending[:8]}{'...' if len(pending) > 8 else ''}）")
    print(f"RESULT: PENDING {len(pending)}")
    return 1


def do_next(C, S, n, max_chars):
    out, chars, batch = [], 0, []
    for idx, meta, item in walk(C, S):
        if filled(item):
            continue
        src = meta.get("src") or ""
        if batch and (len(batch) >= n or chars + len(src) > max_chars):
            break
        batch.append(idx)
        chars += len(src)
        tag = meta["t"]
        if tag == "fig":
            n_ = item.get("n")
            out.append(f"@@{idx}  [fig n={n_}] 译注：须以「图 {n_} | 」开头，首句与其余用「。」分隔")
            out.append(src)
        elif tag == "table":
            n_ = item.get("n")
            grid = meta.get("grid") or {}
            hdr, rows = grid.get("header") or [], grid.get("rows") or []
            cap = EN_PREFIX.sub("", src).strip()
            if hdr:
                out.append(f"@@{idx}  [table n={n_}] 译注：首行是表题；以 | 分隔的是表格"
                           f"（第一行表头，共 {len(hdr)} 列），逐格换成中文，"
                           f"专有名词/缩写/数值保留原文；列数不要改动")
            else:
                out.append(f"@@{idx}  [table n={n_}] 译注：表题以「表 {n_} | 」开头；"
                           f"本表行列未能自动重建，若原文是表格，请用 | 分隔逐行提交")
            out.append(cap)
            if hdr:
                out.append("| " + " | ".join(hdr) + " |")
                for r in rows:
                    out.append("| " + " | ".join(r) + " |")
        else:
            out.append(f"@@{idx}  [{tag}]")
            out.append(src)
        out.append("")
    if not batch:
        print("没有待译项。用 --status 查看进度。")
        return 1
    print("\n".join(out))
    print(f"--- 本批 {len(batch)} 项 / {chars} 字符，序号 {batch[0]}–{batch[-1]}")
    print("把上面的每条原文译成中文，写成批文件（同 @@序号），然后执行：")
    print("  python fill.py <content.json> --file batch.txt")
    return 0


def parse_batch(path):
    items, cur, buf = {}, None, []
    with open(path, encoding="utf-8-sig") as fh:
        raw = fh.read()
    for line in raw.splitlines():
        m = HDR.match(line.strip())
        if m:
            if cur is not None:
                items[cur] = "\n".join(buf).strip()
            cur, buf = int(m.group(1)), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        items[cur] = "\n".join(buf).strip()
    return {k: v for k, v in items.items() if v}


def split_cells(text):
    """批文件内容拆成 (表题/正文, 表格行)。以 | 开头的行是表格行。"""
    cap, tbl = [], []
    for ln in text.splitlines():
        if ln.strip().startswith("|"):
            tbl.append(ln.strip())
        else:
            cap.append(ln)
    return "\n".join(cap).strip(), tbl


def item_src(meta):
    """原文比对文本：表格要把预填网格也拼进来，否则数值列查不到。"""
    parts = [meta.get("src") or ""]
    grid = meta.get("grid") or {}
    if grid:
        parts.extend(" ".join(r) for r in
                     [grid.get("header") or []] + (grid.get("rows") or []))
    return "\n".join(p for p in parts if p)


def item_dst(item):
    if item["t"] in ("fig", "table"):
        parts = [item.get("caption") or ""]
        if item["t"] == "table":
            parts.extend(" ".join(r) for r in
                         [item.get("header") or []] + (item.get("rows") or []))
        return "\n".join(p for p in parts if p)
    return item.get("text") or ""


def do_fill(C, S, path, dry, cpath):
    t0 = time.perf_counter()
    work = os.path.dirname(os.path.abspath(cpath))
    got = parse_batch(path)
    if not got:
        print("! 批文件里没有解析到任何 @@序号。检查格式：每条以单独一行 @@12 开头。")
        return 1
    applied, warns, nums, terms = [], [], [], []
    srcs = []
    for idx, raw in sorted(got.items()):
        key = str(idx)
        if key not in S["items"]:
            warns.append(f"@@{idx} 超出范围（共 {S['count']} 项），已忽略")
            continue
        meta = S["items"][key]
        arr = C.get(meta["sect"]) or []
        if meta["i"] >= len(arr):
            warns.append(f"@@{idx} 定位失败（{meta['sect']}[{meta['i']}]）")
            continue
        item = arr[meta["i"]]
        cap, tbl = split_cells(raw.strip())

        if item["t"] == "fig":
            n_ = item.get("n")
            body = EN_PREFIX.sub("", FIG_PREFIX.sub("", cap)).strip()
            item["caption"] = f"图 {n_} | {body}".strip() if n_ else body
        elif item["t"] == "table":
            n_ = item.get("n")
            body = EN_PREFIX.sub("", TAB_PREFIX.sub("", cap)).strip()
            item["caption"] = f"表 {n_} | {body}".strip() if n_ else body
            if tbl:
                parsed = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in tbl]
                parsed = [r for r in parsed if any(c for c in r)]
                if parsed:
                    ncol = len(parsed[0])
                    off = [i + 1 for i, r in enumerate(parsed) if len(r) != ncol]
                    if off:
                        warns.append(f"@@{idx} 表格第 {off} 行不是 {ncol} 列，"
                                     f"已原样写入，请核对（合并单元格请手工改 content.json）")
                    item["header"] = parsed[0]
                    item["rows"] = parsed[1:]
                    item.pop("_manual", None)
            elif item.get("header"):
                warns.append(f"@@{idx} 只提交了表题，表内仍是英文预填"
                             f"（要译就逐行用 | 分隔提交）")
        elif item["t"] == "eq":
            warns.append(f"@@{idx} 是公式（已预填），已跳过")
            continue
        else:
            item["text"] = cap or raw.strip()
            # 论文标题也要译成中文（方案 A：此前 title 放行不译，导致所有篇标题都是英文）。
            # 作者/单位/DOI 靠 keep_original_text 内容识别继续保留原文。
            if item["t"] in ("title", "p", "h1", "h2", "abstract") \
                    and not CJK.search(item["text"]) and len(item["text"]) > 30 \
                    and not keep_original_text(meta.get("src") or ""):
                warns.append(f"@@{idx} 未见中文，确认是否漏译")
        applied.append(idx)

        # 逐项查数字：交付时才发现要返工整篇，当场发现只需改这一条
        st, dt = item_src(meta), item_dst(item)
        lack = key_nums(st) - key_nums(dt)
        if lack:
            nums.append((idx, sorted(lack)))
        srcs.append(st)

    # 查术语：只看本批原文出现过的词。两处口径修正（详见 G.term_status 的说明）：
    #   · 判据用 G.term_status，与译后自检共用 —— 以前这里另写一套「primary_zh 必须
    #     出现在本批译文里」，把词表写明的「缩写保留 ORF」「原文照写 5′UTR」全判成漂移；
    #   · 范围用全篇译文而非本批 —— 首现括注「中文（English）」只在全文第一次出现时
    #     写，此后各批合法地只用缩写，按批判等于每一批都要重写一遍中文。
    if srcs:
        s_all = "\n".join(srcs)
        whole = "\n".join(
            (it.get("caption") or it.get("text") or "")
            for it in (C.get("front") or []) + (C.get("body") or []) + (C.get("back") or []))
        for t in G.load_terms():
            if not G.count_hits(s_all, t):
                continue
            if G.term_status(t, whole):
                terms.append((t["en"], G.primary_zh(t) or G.abbr_of(t)))

    if not dry and applied:
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(C, fh, ensure_ascii=False, indent=1)
    print(f"{'[dry-run] ' if dry else ''}写入 {len(applied)} 项："
          f"{applied[:12]}{'...' if len(applied) > 12 else ''}")
    for w in warns:
        print(" -", w)
    for idx, lack in nums[:8]:
        print(f" 数字 @@{idx} 原文有而译文缺 {lack}")
    if len(nums) > 8:
        print(f" （另有 {len(nums) - 8} 处数字疑点）")
    for en, zh in terms[:8]:
        print(f" 术语 {en} 本批未用约定译名「{zh}」")
    if len(terms) > 8:
        print(f" （另有 {len(terms) - 8} 个术语未按约定译名）")
    if not dry:
        print("（建议接着跑 --status 确认进度）")

    # 翻译侧当场发现的问题也落盘：数字缺项、术语漂移、格式告警。
    # 这些在交付自检里会再报一次，但在 fill 阶段记下来才能知道「哪一批出的」
    for w in warns:
        J.issue(work, "fill", "FILL_WARN", detail=w, msg=w)
    for idx, lack in nums:
        J.issue(work, "fill", "NUM_MISMATCH", f"item {idx}", f"原文有而译文缺 {lack}",
                "本批数字与原文不一致")
    for en, zh in terms:
        J.issue(work, "fill", "TERM_DRIFT", en, f"应译作「{zh}」", f"本批未用约定译名：{en}")
    J.append(work, "fill", "fill.py", "timing", code="FILL_BATCH",
             detail=f"写入 {len(applied)} 项｜告警 {len(warns)}｜数字 {len(nums)}｜术语 {len(terms)}"
                    f"{'（dry-run）' if dry else ''}",
             ms=(time.perf_counter() - t0) * 1000)

    return 0 if not (warns or nums) else 2


def main():
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("--"):
        print(__doc__)
        return 1
    cpath, rest = argv[0], argv[1:]
    opt = {"status": False, "next": None, "file": None, "dry": False, "max_chars": 9000}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--status":
            opt["status"] = True
        elif a == "--dry-run":
            opt["dry"] = True
        elif a.startswith("--next"):
            if "=" in a:
                opt["next"] = int(a.split("=", 1)[1])
            elif i + 1 < len(rest) and rest[i + 1].isdigit():
                opt["next"] = int(rest[i + 1]); i += 1
            else:
                opt["next"] = 20
        elif a.startswith("--max-chars"):
            if "=" in a:
                opt["max_chars"] = int(a.split("=", 1)[1])
            elif i + 1 < len(rest) and rest[i + 1].isdigit():
                opt["max_chars"] = int(rest[i + 1]); i += 1
        elif a == "--file" and i + 1 < len(rest):
            opt["file"] = rest[i + 1]; i += 1
        elif a.startswith("--file="):
            opt["file"] = a.split("=", 1)[1]
        i += 1

    C, S = load(cpath)
    if S is None:
        return 1
    if opt["next"] is not None:
        return do_next(C, S, opt["next"], opt["max_chars"])
    if opt["file"]:
        if not os.path.exists(opt["file"]):
            print("! 批文件不存在:", opt["file"])
            return 1
        return do_fill(C, S, opt["file"], opt["dry"], cpath)
    return do_status(C, S)


if __name__ == "__main__":
    sys.exit(main())
