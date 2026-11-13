# -*- coding: utf-8 -*-
"""audit.py —— 交付前语义抽检：挑几条译文与原文并排，人工/模型自查。

用法:
    python audit.py <content.json>              # 抽 3 条（1 条最高风险 + 2 条随机）
    python audit.py <content.json> --n 5        # 抽 5 条
    python audit.py <content.json> --seq 41,88  # 指定条目复查（不消耗抽样额度）
    python audit.py <content.json> --again      # 忽略"已抽过"的记录，重新抽
    python audit.py <content.json> --list       # 只看风险排行，不打印正文

为什么需要它：verify.py 只能做机械比对（数字、图号、引用、标点），
「整段意思译反」「漏掉 not」「may 译成 will」这类错它一条都查不出。
这类错占比不高，但一旦交付后果最重，所以留一道 3 条的语义抽检。
成本上限：默认 3 条、约 3–5k 字符，远低于整篇重译。

抽样策略：先按风险打分（长度 / 数字 / 术语 / 否定 / 比较级 / 图表引用），
必取第 1 名，其余从高分池里随机 —— 既不漏掉最危险的段，也不会每次抽同一条。
已抽过的序号记在 <work>/.audit.json，下批优先抽没看过的。
"""
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE,):
    if p not in sys.path:
        sys.path.insert(0, p)

import glossary as G                        # noqa: E402
from fill import filled, item_dst, item_src  # noqa: E402
from verify import key_nums                  # noqa: E402

NEG = re.compile(r"\b(?:not|no|none|never|without|lack\w*|absent|fail\w*|unable|"
                 r"only|except|neither|nor|rather than|instead of)\b", re.I)
COMP = re.compile(r"\b(?:increase|decrease|higher|lower|greater|reduc\w+|enhanc\w+|"
                  r"correlat\w+|significant\w*|trend|fold)\b", re.I)
REF = re.compile(r"\b(?:Figs?\.|Figures?|Tables?)\s*\d", re.I)
ENOUGH = re.compile(r"[A-Za-z]{2,}")

# 单条超过这个长度就只做截断展示（长段建议用 --seq 单独看）
DISPLAY_CAP = 2400


def load(cpath):
    C = json.load(open(cpath, encoding="utf-8"))
    sp = os.path.join(os.path.dirname(os.path.abspath(cpath)), "source.json")
    if not os.path.exists(sp):
        print("! 找不到 source.json（应与 content.json 同目录），没有原文就无法对照。")
        print("  请用 make_skeleton.py 重新生成骨架，或确认工作目录没被移动。")
        return C, None
    return C, json.load(open(sp, encoding="utf-8"))


def doc_terms(S):
    """只保留本文出现过的术语（避免对每条都扫全表）。"""
    allsrc = G.norm("\n".join(item_src(m) for m in S["items"].values()))
    out = []
    for t in G.load_terms():
        n = G.count_hits(allsrc, t)
        if n:
            out.append((t, n))
    return out


def risk(src, terms):
    """打分 + 理由，全部是本地字符串计算，不耗模型 token。"""
    s, why = 0, []
    n = len(src)
    if n > 1200:
        s += 3
        why.append(f"{n} 字")
    elif n > 600:
        s += 2
        why.append(f"{n} 字")
    k = len(key_nums(src))
    if k:
        s += min(3, k)
        why.append(f"数字 {k}")
    hit = [t["en"] for t, _ in terms if G.count_hits(src, t)]
    if hit:
        s += min(3, len(hit))
        why.append("术语 " + "/".join(x.split(" /")[0] for x in hit[:2]))
    if NEG.search(src):
        s += 2
        why.append("含否定")
    if COMP.search(src):
        s += 1
        why.append("比较/趋势")
    if REF.search(src):
        s += 1
        why.append("含图表引用")
    return s, why


def collect(C, S, terms, cap):
    rows = []
    for key, meta in S["items"].items():
        arr = C.get(meta["sect"]) or []
        if meta["i"] >= len(arr):
            continue
        item = arr[meta["i"]]
        if item["t"] not in ("p", "abstract", "h1", "h2", "fig", "table"):
            continue
        if not filled(item):
            continue
        src, dst = item_src(meta), item_dst(item)
        # 作者行、纯英文元信息、纯序列没有语义可查
        if len(src) < 60 or len(ENOUGH.findall(src)) < 6:
            continue
        s, why = risk(src, terms)
        rows.append({"seq": int(key), "score": s, "why": why, "t": item["t"],
                     "len": len(src), "src": src, "dst": dst})
    rows.sort(key=lambda r: (-r["score"], r["seq"]))
    return rows


def pick(rows, n, seed, seen):
    pool = [r for r in rows if r["seq"] not in seen]
    fresh = [r for r in pool if r["len"] <= DISPLAY_CAP]
    pool = fresh or pool or rows
    if len(pool) <= n:
        return pool
    rng = random.Random(seed)
    top = pool[0]
    rest = pool[1:]
    span = rest[:max(n * 3, 12)]
    return [top] + rng.sample(span, min(n - 1, len(span)))


def show(rows, total):
    print(f"抽检 {len(rows)} 条 / 全文可查 {total} 条 —— 逐条对照，只核对这四点：")
    print("  1 意思有没有译反、有没有漏掉半句      2 数字与单位是否与原文一致")
    print("  3 否定、条件、因果是否保留            4 术语是否与词表口径一致")
    print()
    for r in rows:
        tag = {"p": "正文", "abstract": "摘要", "h1": "标题", "h2": "小节",
               "fig": "图注", "table": "表格"}[r["t"]]
        head = f"@@{r['seq']}  [{tag}] 风险 {r['score']}"
        if r["why"]:
            head += " ｜ " + " ｜ ".join(r["why"])
        print(head)
        print("--- 原文 ---")
        print(cut(r["src"]))
        print("--- 译文 ---")
        print(cut(r["dst"]))
        print()
    print("发现问题的条目，用 --seq 单独复查并就地改 content.json；")
    print("或把该条重新译一遍写进批文件，再用 fill.py --file 覆盖。")


def cut(text):
    if len(text) <= DISPLAY_CAP:
        return text
    return text[:DISPLAY_CAP] + f"\n……（后略 {len(text) - DISPLAY_CAP} 字，用 --seq 看全文）"


def main():
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("--"):
        print(__doc__)
        return 1
    cpath = argv[0]
    rest = argv[1:]
    opt = {"n": 3, "seed": 0, "seq": None, "again": False, "list": False}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--again":
            opt["again"] = True
        elif a == "--list":
            opt["list"] = True
        elif a.startswith("--n"):
            v = a.split("=", 1)[1] if "=" in a else (rest[i + 1] if i + 1 < len(rest) else "3")
            opt["n"] = max(1, int(v))
            i += 0 if "=" in a else 1
        elif a.startswith("--seed"):
            v = a.split("=", 1)[1] if "=" in a else (rest[i + 1] if i + 1 < len(rest) else "0")
            opt["seed"] = int(v)
            i += 0 if "=" in a else 1
        elif a == "--seq" and i + 1 < len(rest):
            opt["seq"] = [int(x) for x in re.split(r"[,\s]+", rest[i + 1]) if x.strip().isdigit()]
            i += 1
        elif a.startswith("--seq="):
            opt["seq"] = [int(x) for x in re.split(r"[,\s]+", a.split("=", 1)[1]) if x.strip().isdigit()]
        i += 1

    C, S = load(cpath)
    if S is None:
        return 1
    terms = doc_terms(S)
    rows = collect(C, S, terms, DISPLAY_CAP)
    if not rows:
        print("没有可抽检的条目（都为空或过短）。")
        return 0

    hist = os.path.join(os.path.dirname(os.path.abspath(cpath)), ".audit.json")
    seen = set()
    if os.path.exists(hist) and not opt["again"] and not opt["seq"]:
        try:
            seen = set(json.load(open(hist, encoding="utf-8")))
        except Exception:
            seen = set()

    if opt["list"]:
        print(f"风险排行（前 20 / 共 {len(rows)}）：")
        for r in rows[:20]:
            mark = " 已抽" if r["seq"] in seen else ""
            print(f"  @@{r['seq']:<4} 分 {r['score']:<2} {r['t']:<8} "
                  f"{' ｜ '.join(r['why'])}{mark}")
        return 0

    if opt["seq"]:
        want = [r for r in rows if r["seq"] in set(opt["seq"])]
        lost = sorted(set(opt["seq"]) - {r["seq"] for r in want})
        if not want:
            print(f"! 这些序号不可抽检（未译 / 过短 / 非正文）：{lost}")
            return 1
        if lost:
            print(f"! 跳过：{lost}（未译、过短或不是正文类条目）\n")
        show(want, len(rows))
        return 0

    chosen = pick(rows, opt["n"], opt["seed"], seen)
    show(chosen, len(rows))
    try:
        json.dump(sorted(seen | {r["seq"] for r in chosen}),
                  open(hist, "w", encoding="utf-8"))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
