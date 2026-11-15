# -*- coding: utf-8 -*-
"""consistency.py —— 篇内与跨篇的术语译法一致性检查。

用法:
    python consistency.py <work目录 或 content.json> [更多…] [--json] [--strict] [--all]

为什么查「自己前后是否一致」而不是「有没有用词表词」：
    词表是参考，不是标准答案。同一篇里 reporter assay 前面写「报告基因检测」、
    后面写「报告基因测定」，这是真问题；而模型把 backbone 在蛋白结构里写「主链」、
    在病毒基因组里写「骨架」，是对的，不该罚。前者词表查不出来（两种写法都可能是
    词表外的自造词），后者词表只会误报。一致性只有拿译文自己跟自己比才查得到。

为什么必须锚定词表（第一版的教训）：
    只按「中文（English）」这个形式抽括注对，会把试剂-厂商括注也算进来 ——
    「胎牛血清（Gibco）」「DMEM（Thermo Fisher）」在方法学里遍地都是，于是
    Gibco 被当成术语，前面那句试剂名被当成它的「译名」，一篇就报出十几处假不一致。
    所以只认词表里有的英文；写法则用词表的中文候选去比对，命中就归一，不靠截字符串。
    `--all` 关掉这层过滤，用来排查词表外的自造术语（会吵，默认不开）。

判据来源：译文里自带的「中文（English）」首现括注。规范要求术语首次出现写成这个
形式，所以它天然是一张「英文 → 中文」映射表，不必再维护第二份数据。没有括注的
术语这里查不到 —— 这是已知覆盖上限，宁可漏报也不拿猜测当中文译名。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import glossary as G  # noqa: E402

# 括注里的英文：允许连字符、撇号、空格、斜杠（reporter assay / ribosome profiling）
EN_IN_PAREN = re.compile(r"[（(]\s*([A-Za-z][A-Za-z0-9′'\- ]{1,40}?)\s*[）)]")
CUT = re.compile(r"[。；;，,、\n：:]")
TAIL_ZH = re.compile(r"[\u4e00-\u9fff]{2,12}$")


def index_terms(terms):
    """英文变体（归一化）→ 词条。用于判定括注里的英文是不是词表术语。"""
    idx = {}
    for t in terms:
        for v in t["variants"]:
            idx.setdefault(G.norm(v).lower(), t)
    return idx


def lookup(k, idx):
    """精确匹配不上时，退到「括注短语里包含的最长词表变体」。

    实际括注常写成完整短语：`荧光素酶报告基因测定（luciferase reporter assay）`，
    而词表里只有 `luciferase reporter` 与 `reporter assay` 两条，整串对不上就漏检。
    取最长包含者 —— 短变体（ORF、SD 这类 3 字母）不参与，否则 `pdb 7u4f` 里的
    `pdb` 会把无关括注全吸进来。
    """
    if k in idx:
        return idx[k]
    best = ""
    for v in idx:
        if len(v) >= 6 and v in k and len(v) > len(best):
            best = v
    return idx[best] if best else None


def load_text(path):
    """目录 → 取其中的 content.json；文件 → 直接读。返回 (显示名, 全文)。"""
    if os.path.isdir(path):
        p = os.path.join(path, "content.json")
        name = os.path.basename(os.path.abspath(path))
    else:
        p, name = path, os.path.basename(os.path.dirname(os.path.abspath(path)))
    if not os.path.exists(p):
        return name, ""
    C = json.load(open(p, encoding="utf-8"))
    parts = []
    for sec in ("front", "body", "back"):
        for it in C.get(sec) or []:
            for k in ("caption", "text"):
                v = it.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
    return name, "\n".join(parts)


def en_key(raw):
    s = re.split(r"\s*/\s*", raw.strip())[0]
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_form(term, pre):
    """把括注前的文本归一到词表候选写法；对不上返回末尾汉字串（未知写法）。"""
    cands = G.zh_candidates(term)
    for c in sorted(cands, key=len, reverse=True):
        if pre.endswith(c):
            return c
    m = TAIL_ZH.search(pre)
    return m.group(0) if m else ""


def extract(text, idx, all_terms=False):
    """返回 [(术语英文, 写法)]。idx 为空且 all_terms 才不过滤。"""
    out = []
    for m in EN_IN_PAREN.finditer(text):
        raw = m.group(1)
        k = en_key(raw)
        term = lookup(k, idx) if idx else None
        if term is None:
            if not all_terms:
                continue
            pre = CUT.split(text[max(0, m.start() - 30):m.start()])[-1]
            tail = TAIL_ZH.search(pre.strip())
            if tail:
                out.append((k, tail.group(0)))
            continue
        pre = CUT.split(text[max(0, m.start() - 40):m.start()])[-1].strip()
        form = norm_form(term, pre)
        if form:
            out.append((term["en"], form))
    return out


def block_pairs(work):
    """逐块取 (原文, 译文)。靠 source.json 的 ({sect,i}) 索引对回 content.json。

    为什么不用全文：全文判「用过一次就算过」，一块都没用上也能蒙混过关；块级才能
    看出「18 块里 17 块用的是另一个词」，这正是另立译法的样子。
    """
    sp = os.path.join(work, "source.json")
    cp = os.path.join(work, "content.json")
    if not (os.path.exists(sp) and os.path.exists(cp)):
        return []
    S = json.load(open(sp, encoding="utf-8"))
    C = json.load(open(cp, encoding="utf-8"))
    out = []
    for m in (S.get("items") or {}).values():
        sect, i = m.get("sect"), m.get("i")
        arr = C.get(sect) or []
        if i is None or i >= len(arr):
            continue
        it = arr[i]
        dst = it.get("caption") or it.get("text") or ""
        if it.get("t") == "table":
            rows = [it.get("header") or []] + (it.get("rows") or [])
            dst = "\n".join([dst] + [" ".join(r) for r in rows])
        out.append((m.get("src") or "", dst))
    return out


def coverage(work, terms):
    """块级术语覆盖：原文含该术语的块里，有多少块的译文用上了约定译名。

    低比例不等于错 —— 可能是另立译法（报告基因测定 vs 检测），也可能是该块译文
    本来就不需要重复译名（用的是缩写、指代）。所以只做提示，不判失败；它给的是
    「值得看一眼的术语」，不是「必须改的地方」。
    """
    pairs = block_pairs(work)
    if not pairs:
        return []
    rows = []
    for t in terms:
        nb = nh = 0
        for src, dst in pairs:
            if not G.count_hits(src, t):
                continue
            nb += 1
            if not G.term_status(t, dst):
                nh += 1
        if nb >= 3 and nh / nb < 0.5:
            rows.append({"term": t["en"], "blocks": nb, "used": nh,
                         "zh": " / ".join(G.zh_candidates(t))})
    return rows


def same(a, b):
    return a == b or a.endswith(b) or b.endswith(a)


def cluster(vals):
    """后缀包含的归一组，组代表取最长那个。"""
    groups = []
    for v in sorted(set(vals), key=len, reverse=True):
        for g in groups:
            if same(g[0], v):
                g.append(v)
                break
        else:
            groups.append([v])
    return groups


def scan(path, idx, all_terms=False):
    _, text = load_text(path)
    acc = defaultdict(Counter)
    for k, v in extract(text, idx, all_terms):
        acc[k][v] += 1
    return acc


def report(paths, as_json=False, strict=False, all_terms=False):
    idx = {} if all_terms else index_terms(G.load_terms())
    terms = G.load_terms()
    per = [(p, scan(p, idx, all_terms)) for p in paths]
    intro, cross, cov = [], [], []

    for p in paths:
        name = os.path.basename(os.path.abspath(p))
        for r in coverage(p, terms):
            cov.append(dict(r, work=name))

    for p, acc in per:
        name = os.path.basename(os.path.abspath(p))
        for k, cnt in acc.items():
            groups = cluster(list(cnt))
            if len(groups) > 1:
                intro.append({"work": name, "term": k,
                              "forms": [g[0] for g in groups],
                              "counts": {g[0]: sum(cnt[x] for x in g) for g in groups}})

    if len(per) > 1:
        rep = defaultdict(dict)
        for p, acc in per:
            name = os.path.basename(os.path.abspath(p))
            for k, cnt in acc.items():
                groups = cluster(list(cnt))
                top = max(groups, key=lambda g: sum(cnt[x] for x in g))
                rep[k][name] = top[0]
        for k, d in rep.items():
            if len(cluster(list(d.values()))) > 1:
                cross.append({"term": k, "by_work": d})

    if as_json:
        json.dump({"coverage": cov, "within": intro, "across": cross}, sys.stdout,
                  ensure_ascii=False, indent=1)
        print()
        return 1 if (strict and (cov or intro or cross)) else 0

    scope = "全部括注（--all）" if all_terms else "词表内术语"
    print(f"[一致性] 扫描 {len(per)} 篇，范围：{scope}")
    if not cov and not intro and not cross:
        print("  OK：未发现术语译法异常。")
        return 0

    if cov:
        print(f"\n一、覆盖率偏低 {len(cov)} 处（原文出现过，多数块没用约定译名 → 疑似另立写法）")
        for r in sorted(cov, key=lambda x: x["used"] / x["blocks"]):
            print(f"  - {r['work'][:30]:32} {r['term'][:28]:30} "
                  f"英文 {r['blocks']:2} 块／用「{r['zh']}」{r['used']} 块")
    if intro:
        print(f"\n二、篇内写法不一致 {len(intro)} 处：")
        for r in intro:
            forms = " / ".join(f"{f}×{r['counts'][f]}" for f in r["forms"])
            print(f"  - {r['work'][:32]:34} {r['term'][:30]:32} → {forms}")
    if cross:
        print(f"\n三、跨篇不一致 {len(cross)} 处（合并成册前建议统一）：")
        for r in cross:
            print(f"  - {r['term']}")
            for w, f in sorted(r["by_work"].items()):
                print(f"      {w[:34]:36} {f}")

    print("\n说明：一为块级对齐（不依赖括注，覆盖全）；二、三依赖「中文（English）」"
          "括注，只覆盖带括注的术语。均为提示，不判失败。")
    return 1 if strict else 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help or not a.paths:
        print(__doc__)
        return 0 if a.help else 2
    return report(a.paths, a.json, a.strict, a.all)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
