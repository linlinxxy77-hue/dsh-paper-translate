# -*- coding: utf-8 -*-
"""glossary.py —— 术语口径：翻译前对齐、翻译后检查、译完沉淀。

用法:
    python glossary.py <structure.json>               # 列出本文命中词表的术语（按频次）
    python glossary.py <structure.json> --json g.json # 同时导出机器可读版
    python glossary.py <content.json> --check         # 译后检查：词表术语的中文译名是否都用了
    python glossary.py <content.json> --harvest       # 译完：列出"用得多但词表没有"的术语候选
    python glossary.py --merge new.md [--tag 论文简称] # 把补好中文的候选并进词表

为什么要它：术语表如果只活在上下文里，跨批次翻译时同一术语会出现不同译法
（uORF 一会「上游开放阅读框」一会「上游 ORF」），下一篇也无法复用。
把口径落到文件，模型每批都对着同一张表译 —— 这张表是唯一会「越用越准」的资产。
"""
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
# 允许用环境变量指定项目自己的词表（测试与多项目共用时有用）
TERMS = os.environ.get("PAPER_TRANSLATE_TERMS") or \
    os.path.join(os.path.dirname(HERE), "references", "terms.md")

# ---- 术语候选提取用的过滤表 ----
STOP = set("""
a an the of in on at to for from with by as is are was were be been being
that this these those it its their they them we our us which who whom whose and or but
not no than then thus so such can could may might must should would will does do did
have has had here there also both each other more most less least very much many few
all any some only same own per via using used use between among within without during
after before under over into out up down about above below while however therefore
because although since when where what how why
""".split())
NOISE = ("fig", "table", "supplement", "supplementary", "extended data", "draft",
         "author", "writing", "email", "interest", "competing", "correspond",
         "peer review", "copyright", "license")


def variants(en):
    """把 'ribosome profiling / Ribo-seq'、'5′ untranslated region (5′UTR)' 拆成可匹配写法。"""
    out = []
    for part in re.split(r"\s*/\s*", en):
        part = part.strip()
        if not part:
            continue
        out.append(part)
        for m in re.findall(r"\(([^)]+)\)", part):
            if m.strip():
                out.append(m.strip())
        base = re.sub(r"\([^)]*\)", "", part).strip()
        if base:
            out.append(base)
    seen, res = set(), []
    for v in out:
        v = re.sub(r"\s+", " ", v).strip()
        if v and v.lower() not in seen and len(v) > 1:
            seen.add(v.lower())
            res.append(v)
    return res


def load_terms():
    rows = []
    if not os.path.exists(TERMS):
        return rows
    with open(TERMS, encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("英文", "") or set(cells[0]) <= set("-: "):
                continue
            rows.append({"en": cells[0], "zh": cells[1],
                         "note": cells[2] if len(cells) > 2 else "",
                         "variants": variants(cells[0])})
    return rows


def norm(s):
    """统一上标、撇号与连字写法，否则 5′UTR 会因抽取成 '5 ~{′} UTR' 而匹配不到。"""
    s = re.sub(r"~\{([^{}]*)\}", r"\1", s or "")
    s = re.sub(r"(\d)\s*([′'’])\s*", r"\1\2", s)
    for k, v in (("\ufb00", "ff"), ("\ufb01", "fi"), ("\ufb02", "fl"),
                 ("\ufb03", "ffi"), ("\ufb04", "ffl"), ("\ufb05", "ft"),
                 ("\ufb06", "st")):
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s)


def count_hits(text, term):
    n = 0
    for v in term["variants"]:
        pat = r"(?<![A-Za-z])" + re.escape(norm(v)) + r"(?![A-Za-z])"
        n += len(re.findall(pat, text, re.I))
    return n


def do_structure(path, out_json):
    st = json.load(open(path, encoding="utf-8"))
    text = norm("\n".join(b["text"] for b in st["blocks"]))
    rows = []
    for t in load_terms():
        n = count_hits(text, t)
        if n:
            rows.append({"en": t["en"], "zh": t["zh"], "note": t["note"], "count": n})
    rows.sort(key=lambda r: -r["count"])
    if not rows:
        print("词表未命中任何术语（references/terms.md 可自行扩充）")
        return 0
    w = max(len(r["en"]) for r in rows)
    print(f"本文命中 {len(rows)} 条术语（按出现次数）—— 翻译时统一按此译名：")
    for r in rows:
        note = f"  // {r['note']}" if r["note"] else ""
        print(f"  {r['count']:>4}  {r['en']:<{w}}  {r['zh']}{note}")
    if out_json:
        json.dump(rows, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"-> {out_json}")
    return 0


def primary_zh(term):
    """取「/」前的主译法并去掉括注，用于一致性比对。"""
    zh = re.split(r"\s*/\s*", term["zh"])[0]
    return re.sub(r"（[^）]*）|\([^)]*\)", "", zh).strip()


def do_check(path):
    C = json.load(open(path, encoding="utf-8"))
    text = norm("\n".join(
        (it.get("caption") or it.get("text") or "")
        for it in (C.get("front", []) + C.get("body", []) + C.get("back", []))))
    src = ""
    sp = os.path.join(os.path.dirname(os.path.abspath(path)), "source.json")
    if os.path.exists(sp):
        S = json.load(open(sp, encoding="utf-8"))
        src = norm("\n".join(m.get("src") or "" for m in S["items"].values()))
    bad = []
    for t in load_terms():
        n_src = count_hits(src, t) if src else 0
        if not n_src:
            continue
        zh = primary_zh(t)
        if zh and zh not in text:
            bad.append((t["en"], zh, n_src))
    if not bad:
        print("术语一致性：OK（原文出现的词表术语，译文都用了约定译名）")
        return 0
    print("[告警] 以下术语原文出现过，但译文未使用约定译名：")
    for en, zh, n in bad:
        print(f"  - {en}（原文 {n} 次） 应译作「{zh}」")
    return 1


# ---------- 术语沉淀：译后挖掘候选 ----------

WORD = re.compile(r"[A-Za-z][A-Za-z0-9′'\-]*")
# 括号前的全称片段（限长，避免把整句当全称）+ 全大写缩写
ACRF = re.compile(r"([A-Za-z][^(){}]{2,80}?)\s*\(([A-Z][A-Za-z0-9\-]{1,7})s?\)")


def acro_ok(full, abbr):
    """缩写必须是全称首字母的子序列：translation efficiency → TE ✓。

    连字符词按段取首字母（squeeze-and-excitation → s,a,e），否则 MRL/SE 这类判不出来。
    """
    parts = [p for p in re.split(r"[\s\-/]+", full) if p]
    initials = [p[0].lower() for p in parts if p[0].isalpha()]
    it = iter(initials)
    return all(any(c.lower() == x for x in it) for c in abbr)


def minimal_name(before, abbr, span=6):
    """取括号前最短的一段使缩写成立 —— 否则会把半句正文当全称。"""
    toks = before.split()
    for k in range(1, min(span, len(toks)) + 1):
        cand = " ".join(toks[-k:])
        if len(cand) > 60 or len(WORD.findall(cand)) < k:
            continue
        if acro_ok(cand, abbr):
            return cand
    return None


def acro_candidates(text, known):
    """定义式缩写：'firefly luciferase (FL)' → ('firefly luciferase (FL)', 14)。"""
    found = {}
    for m in ACRF.finditer(text):
        before, abbr = m.group(1), m.group(2)
        if abbr.lower() in known or len(abbr) < 2:
            continue
        name = minimal_name(before, abbr)
        if not name or name.lower() in known:
            continue
        # 已被某条已知术语覆盖（已知 random forest regression model，就不必再收 random forest）
        if any(len(x) > 4 and (name.lower() in x or x in name.lower()) for x in known):
            continue
        prev = found.get(abbr)
        if prev is None or len(name) < len(prev[0]):   # 同一缩写取最短全称
            found[abbr] = (name, abbr)
    out = []
    for name, abbr in found.values():
        n = len(re.findall(r"(?<![A-Za-z])" + re.escape(abbr) + r"(?![A-Za-z])", text))
        if n >= 2:
            out.append((n, f"{name} ({abbr})", f"缩写 {abbr}，全文 {n} 次"))
    return out


def restore_case(text, phrase):
    """n-gram 是按小写统计的，输出时恢复原文大小写（HEK293T cells、Kozak strength）。"""
    pat = r"(?<![A-Za-z])" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?![A-Za-z])"
    m = re.search(pat, text, re.I)
    return m.group(0) if m else phrase


def ngram_candidates(text, known, min_count=5):
    """反复出现的 2–4 词组合（低频的才可能是术语，高频短词多是 the of 之类）。"""
    toks = re.findall(r"[a-z][a-z0-9′'\-]{1,}", text.lower())
    out = []
    for n in (2, 3, 4):
        c = Counter()
        for i in range(len(toks) - n + 1):
            g = toks[i:i + n]
            # 短词多为 PDF 拆词残留（pro ling / kit neb），术语基本都在 4 字母以上
            if any(w in STOP or len(w) < 4 for w in g):
                continue
            c[" ".join(g)] += 1
        for phrase, f in c.items():
            if f < min_count:
                continue
            if any(x in phrase for x in NOISE):
                continue
            if phrase in known:
                continue
            # 与已知术语互为子串的跳过（已知 5′UTR，就不必再收 "utr sequences"）
            if any(len(x) > 4 and (phrase in x or x in phrase) for x in known):
                continue
            out.append((f, restore_case(text, phrase), f"全文 {f} 次"))
    return out


def _tokens(phrase):
    return set(re.split(r"[\s()]+", phrase.lower())) - {""}


def prune_overlap(rows):
    """同频次的重叠短语只留一条 —— 否则 KAPA HiFi HotStart ReadyMix 会被切成六段。"""
    rows = sorted(rows, key=lambda r: (-r[0], -len(_tokens(r[1]))))
    kept = []
    for f, en, note in rows:
        toks = _tokens(en)
        drop = False
        rest = []
        for f2, en2, n2 in kept:
            t2 = _tokens(en2)
            if f2 >= f and toks <= t2 and len(t2) > len(toks):
                drop = True                   # 已被更长的同频短语覆盖
                break
            # 同频且高度重合（KAPA HiFi HotStart ReadyMix / hifi hotstart readymix roche）
            if f2 == f and len(toks & t2) / max(len(toks | t2), 1) >= 0.6:
                drop = True
                break
            if t2 < toks:                     # 已收的只是它的一段，换成长的那条
                continue
            rest.append((f2, en2, n2))
        if drop:
            continue
        rest.append((f, en, note))
        kept = rest
    return kept


def load_source_text(path):
    """structure.json / content.json 都支持；content.json 读同目录 source.json。"""
    d = os.path.dirname(os.path.abspath(path))
    o = json.load(open(path, encoding="utf-8"))
    name = ""
    if isinstance(o.get("meta"), dict):
        name = os.path.basename(o["meta"].get("source") or "")
    if not name:                       # content.json 的 meta 不含出处，借同目录 structure.json
        st = os.path.join(d, "structure.json")
        if os.path.exists(st):
            try:
                name = os.path.basename(
                    json.load(open(st, encoding="utf-8")).get("meta", {}).get("source") or "")
            except Exception:
                name = ""
    tag = re.sub(r"\.pdf$", "", name) or os.path.basename(path)
    if "blocks" in o:
        return norm("\n".join(b["text"] for b in o["blocks"])), tag
    sp = os.path.join(d, "source.json")
    if os.path.exists(sp):
        S = json.load(open(sp, encoding="utf-8"))
        return norm("\n".join(m.get("src") or "" for m in S["items"].values())), tag
    txt = "\n".join((it.get("caption") or it.get("text") or "")
                    for it in (o.get("front", []) + o.get("body", []) + o.get("back", [])))
    if txt.strip():
        return norm(txt), tag
    raise SystemExit("! 找不到可用于挖掘的原文（需要 structure.json 或其同目录 source.json）")


def do_harvest(path, top=15, min_count=5):
    text, srcname = load_source_text(path)
    known = {v.lower() for t in load_terms() for v in t["variants"]}
    rows = acro_candidates(text, known) + ngram_candidates(text, known, min_count)
    rows.sort(key=lambda r: -r[0])
    rows = prune_overlap(rows)

    seen, keep = set(), []
    for n, en, note in rows:                     # 同一英文只留频次最高的一条
        k = en.lower()
        if k in seen:
            continue
        seen.add(k)
        keep.append((n, en, note))
    keep = keep[:top]

    if not keep:
        print("没有发现词表之外的高频术语候选（这篇的表达已被词表覆盖得差不多）。")
        return 0

    tag = re.sub(r"\.pdf$", "", os.path.basename(srcname))[:40]
    print(f"本文 {len(known)} 条已知术语，另发现 {len(keep)} 个候选（词表未收录，按频次）：")
    print()
    print("| 英文 | 建议中文 | 说明 |")
    print("|---|---|---|")
    for n, en, note in keep:
        print(f"| {en} |  | {note} |")
    print()
    print("补中文（拿不准的整行删掉）：")
    print("  · 试剂盒/仪器/货号/厂商名（KAPA HiFi、NEBNext 之类）不是术语，删掉")
    print("  · 泛化短语（measure translation、reporter library）价值低，可删")
    print("  · 基因/蛋白/载体名中文列照抄英文")
    print("  1. 把上表补好存成 new_terms.md")
    print(f'  2. python glossary.py --merge new_terms.md --tag "{tag}"')
    print("下一篇自动带上这些口径；译完再 --check 就能查出漂移。")
    return 0


def _merge_block(rows, fresh_section):
    body = "\n".join(f"| {en} | {zh} | {note} |" for en, zh, note in rows)
    if not fresh_section:
        return [body]
    return ["## 自动沉淀", "",
            "由 `glossary.py --harvest` 收集、补中文后并入。与上表等价，按频次自动追加。",
            "", "| 英文 | 建议中文 | 说明 |", "|---|---|---|", body, ""]


def do_merge(path, tag="", dry=False):
    if not os.path.exists(path):
        print("! 文件不存在:", path)
        return 1
    kv = {v.lower() for t in load_terms() for v in t["variants"]}
    add, skip, blank = [], [], 0
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2:
                continue
            en, zh = cells[0], cells[1]
            note = cells[2] if len(cells) > 2 else ""
            if not en or en in ("英文", "EN", "English") or set(en) <= set("-: "):
                continue
            if not zh:
                blank += 1
                continue
            if en.lower() in kv:
                skip.append((en, "词表已有"))
                continue
            kv.add(en.lower())
            add.append((en, zh, note or (f"来自 {tag}" if tag else "")))

    if not add:
        print(f"没有可新增的术语（已存在 {len(skip)} 条，缺中文 {blank} 条未计入）。")
        return 0

    lines = open(TERMS, encoding="utf-8").read().splitlines()
    hdr = "## 自动沉淀"
    idx = next((i for i, l in enumerate(lines) if l.strip() == hdr), None)
    if idx is None:
        tail = next((i for i, l in enumerate(lines) if l.strip() == "## 排版约定"), None)
        block = _merge_block(add, True)
        if tail is None:
            lines = lines + [""] + block
        else:
            lines = lines[:tail] + block + lines[tail:]
    else:
        last = idx
        for i in range(idx + 1, len(lines)):
            if lines[i].strip().startswith("|"):
                last = i
            elif lines[i].strip().startswith("#"):
                break
        if last == idx:                        # 只有标题没有表
            lines = lines[:idx + 1] + _merge_block(add, True)[2:] + lines[idx + 1:]
        else:
            lines = lines[:last + 1] + [f"| {en} | {zh} | {note} |" for en, zh, note in add] \
                + lines[last + 1:]

    if dry:
        print(f"[dry-run] 将新增 {len(add)} 条：")
        for en, zh, note in add:
            print(f"  {en}  →  {zh}{('  // ' + note) if note else ''}")
        return 0

    with open(TERMS, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    print(f"已并入 {len(add)} 条到 {os.path.basename(TERMS)}：")
    for en, zh, note in add:
        print(f"  + {en}  →  {zh}")
    if skip:
        print(f"（跳过 {len(skip)} 条已在词表：{', '.join(e for e, _ in skip[:6])}）")
    if blank:
        print(f"（{blank} 条没填中文，未计入 —— 缺中文的行等同于放弃该术语）")
    print(f"词表现有 {len(load_terms())} 条。")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--merge":
        f = next((a for a in argv[1:] if not a.startswith("--")), None)
        if not f:
            print("usage: glossary.py --merge new.md [--tag 论文简称] [--dry-run]")
            return 1
        tag = ""
        for i, a in enumerate(argv):
            if a == "--tag" and i + 1 < len(argv):
                tag = argv[i + 1]
            elif a.startswith("--tag="):
                tag = a.split("=", 1)[1]
        return do_merge(f, tag, "--dry-run" in argv)

    path = argv[0]
    out, top = None, 15
    for i, a in enumerate(argv):
        if a == "--json" and i + 1 < len(argv):
            out = argv[i + 1]
        elif a.startswith("--json="):
            out = a.split("=", 1)[1]
        elif a == "--top" and i + 1 < len(argv):
            top = int(argv[i + 1])
        elif a.startswith("--top="):
            top = int(a.split("=", 1)[1])
    if "--harvest" in argv:
        return do_harvest(path, top)
    if "--check" in argv:
        return do_check(path)
    return do_structure(path, out)


if __name__ == "__main__":
    sys.exit(main())
