# -*- coding: utf-8 -*-
"""regress.py —— 黄金样本回归：改完脚本之后，一眼看出指标有没有变。

为什么要有它：改抽取、改打分、改词表，只能靠人眼看输出对不对。实测过一次疏漏
——重写词表时把 22 条核心术语整个丢掉，术语命中数从 30 掉到 8，当时没人发现。
把「改之前的指标」存成基线，改完跑一次比对，哪项变了立刻显形。

不重新抽取、不重新裁图，只读工作目录里已有的产物和过程记录，所以很快。

用法:
    python regress.py --save <baseline.json> <workdir> [<workdir> ...]   # 存基线
    python regress.py --check <baseline.json>                            # 按基线里的目录比对
    python regress.py --check <baseline.json> <workdir> [...]            # 只比这几个
    python regress.py --show <baseline.json>                             # 打印基线内容

退出码: 0 无变化 / 1 有变化 / 2 用法或读取错误。

指标分三类：
  - 抽取产物（structure.json 的 stats）：块数、字符数、图表数、图号序列、表格网格
    —— 抽取或裁图逻辑一改，这里最先反应。
  - 派生结果：词表命中条数、素材包字符数
    —— 词表或素材包预算改动的影响。
  - 过程记录（journal.jsonl）：待处理问题按 code 计数
    —— 校验逻辑改动的影响。
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import journal as J  # noqa: E402

STAT_KEYS = ("blocks", "chars", "figures", "tables", "table_grids",
             "captions_linked", "captions_stitched", "equations")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def terms_hit(work):
    """词表在这篇正文里命中多少条 —— 词表或匹配逻辑一变，这个数最先动。"""
    try:
        import glossary as G
        st = _load(os.path.join(work, "structure.json"))
        if not st:
            return None
        text = "\n".join(b.get("text") or "" for b in st.get("blocks") or [])
        return len(G.TermMatcher(G.load_terms()).counts(text))
    except Exception:
        return None


def metrics(work):
    """从一个工作目录提取可比的指标。缺什么就少什么，不编默认值。"""
    m = {"workdir": os.path.abspath(work)}
    st = _load(os.path.join(work, "structure.json"))
    s = ((st or {}).get("stats") or {})
    for k in STAT_KEYS:
        if k in s:
            m[k] = s[k]
    if s.get("fig_nums") is not None:
        m["fig_nums"] = list(s["fig_nums"])
    for k in ("layout", "graphical_abstract"):
        if k in s:
            m[k] = s[k]

    n = terms_hit(work)
    if n is not None:
        m["terms_hit"] = n

    pack = os.path.join(work, "summary.src.txt")
    if os.path.exists(pack):
        try:
            m["pack_chars"] = len(open(pack, encoding="utf-8").read())
        except OSError:
            pass

    # 待处理问题按 code 计数：校验项增删、判据放宽收紧都会体现在这里
    issues = {}
    try:
        for r in J.issues(work):
            if r["state"] == "open":
                issues[r["code"]] = issues.get(r["code"], 0) + r["count"]
    except Exception:
        pass
    m["issues"] = issues
    return m


def flatten(m):
    """把嵌套的 issues 摊平成 点号路径，比对时逐项对齐。"""
    out = {}
    for k, v in m.items():
        if k == "workdir":
            continue
        if k == "issues":
            for code, n in (v or {}).items():
                out[f"issues.{code}"] = n
        else:
            out[k] = v
    return out


def diff(base, cur):
    """返回 (不变的键, [(键, 旧值, 新值)])。"""
    same, changed = [], []
    for k in sorted(set(base) | set(cur)):
        if k not in base:
            changed.append((k, "（无）", cur[k]))
        elif k not in cur:
            changed.append((k, base[k], "（无）"))
        elif base[k] != cur[k]:
            changed.append((k, base[k], cur[k]))
        else:
            same.append(k)
    return same, changed


def _fmt(v):
    if isinstance(v, list):
        return "[" + ",".join(str(x) for x in v) + "]"
    return str(v)


def _delta(old, new):
    """变化量，只在两边都是数字时给，避免对 Fig 号列表瞎算。"""
    if isinstance(old, int) and isinstance(new, int):
        d = new - old
        return f"  ({d:+d})" if d else ""
    return ""


def do_save(path, workdirs):
    cases = {}
    for w in workdirs:
        if not os.path.isdir(w):
            print(f"! 目录不存在: {w}")
            return 2
        m = metrics(w)
        cases[os.path.basename(os.path.abspath(w))] = m
    doc = {"saved": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
           "cases": cases}
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    print(f"已存基线 {path}（{len(cases)} 篇）")
    for name, m in cases.items():
        fl = flatten(m)
        keys = ("blocks", "figures", "tables", "terms_hit", "pack_chars")
        print(f"  {name}: " + "，".join(f"{k}={_fmt(fl[k])}" for k in keys if k in fl))
    return 0


def do_show(path):
    doc = _load(path)
    if not doc:
        print(f"! 读不到基线: {path}")
        return 2
    print(f"基线 {path}  存于 {doc.get('saved', '?')}")
    for name, m in (doc.get("cases") or {}).items():
        print(f"\n{name}")
        for k, v in flatten(m).items():
            print(f"  {k:<20} {_fmt(v)}")
    return 0


def do_check(path, workdirs=None):
    doc = _load(path)
    if not doc:
        print(f"! 读不到基线: {path}")
        return 2
    cases = doc.get("cases") or {}
    if workdirs:
        # 显式给的目录按 basename 对上基线；对不上就现取指标当新样本
        cases = {}
        for w in workdirs:
            name = os.path.basename(os.path.abspath(w))
            old = (doc.get("cases") or {}).get(name)
            cases[name] = {"workdir": w, "_base": old}

    bad = 0
    for name, base in cases.items():
        work = base.get("workdir")
        cur = metrics(work) if (work and os.path.isdir(work)) else {}
        b = flatten(base.get("_base") or base)
        c = flatten(cur)
        same, changed = diff(b, c)
        mark = "变化" if changed else "一致"
        print(f"\n{name}  [{mark}]  {len(same)} 项未变，{len(changed)} 项变化")
        if not work or not os.path.isdir(work):
            print("  ! 目录不存在，跳过比对")
            bad += 1
            continue
        for k, old, new in changed:
            print(f"  ! {k:<22} {_fmt(old)} → {_fmt(new)}{_delta(old, new)}")
        bad += len(changed)

    if not bad:
        print("\n[回归通过] 所有指标与基线一致。")
        return 0
    print(f"\n[有变化] 共 {bad} 项。确认是有意改动后，重跑 --save 更新基线。")
    return 1


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    mode = argv[0]
    if mode in ("--save", "--check", "--show"):
        if len(argv) < 2:
            print(f"usage: regress.py {mode} <baseline.json> [workdir ...]")
            return 2
        path, rest = argv[1], argv[2:]
        if mode == "--save":
            if not rest:
                print("usage: regress.py --save <baseline.json> <workdir> [<workdir> ...]")
                return 2
            return do_save(path, rest)
        if mode == "--show":
            return do_show(path)
        return do_check(path, rest or None)
    print(f"! 不认识的参数: {mode}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
