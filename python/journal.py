# -*- coding: utf-8 -*-
"""journal.py —— 过程记录：问题、决策、耗时。

为什么要有它：verify 的十几项检查原来只 print 到终端，模型读完就丢，工作目录里
不留痕迹。「第 2 篇为什么没抽出图」这种问题只能靠人重跑一遍去猜。不同 agent 互相
校对时也没有共享的问题清单——一个说「这里术语没译」，另一个说「western blot 该写
中文」，两句话对不上，人还得手工比对。

所以这里做三件事：

  1. 问题用「稳定 code + 定位」标识（`TERM_BARE` @ `body|42`）。同一处、同一类
     问题永远同一个 key，跨 agent、跨次运行、跨版本都能对上。
  2. 问题有状态：open / resolved / waived。日志只追加不改写，当前状态由同一 key
     的最后一条事件决定 —— 这样既保留完整历史，又不会互相覆盖。
  3. 每个阶段记一条 timing（耗时毫秒）。翻译最慢的那段在模型侧，不在这里，所以
     本地耗时要和壳侧记录的工具调用耗时合起来看。

存储：<work>/journal.jsonl，一行一个 JSON 对象（JSON Lines）。追加写只需在末尾
写入一行，不用读全文再重写；某行损坏只影响那一行。

用法：
    from journal import append, timed, issue, resolve, waive, decide

    with timed("extract", work, "extract.py"):
        ...
    issue(work, "verify", "TERM_BARE", where="body|42", detail="processivity",
          msg="中文句子里只写了英文")
    decide(work, "summary", "FIG_PICK", detail="fig4 cites=14", msg="选图 4")
"""
import json
import os
import time
from datetime import datetime

NAME = "journal.jsonl"
OLD = "journal.1.jsonl"
MAX_BYTES = 1 << 20          # 单文件上限 1MB，超了轮转一次
RUNLOG_V = 2                 # runs.jsonl 的格式版本，与 runner.js logRun 保持一致
# 插件的调用记录。真实数据落在 runner.js 的 dataDir()（受 DSH_PAPER_DATA 环境变量控制）；
# 这里优先读环境变量，否则回落到 <插件>/data/ 这个默认位置。否则经 plugin-cli 隔离
# 目录跑时，runlog() 会读到真实插件目录，而不是测试隔离目录，`--runs`/`--e2e` 全空。
RUNLOG = os.path.join(os.environ.get("DSH_PAPER_DATA")
                      or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "runs.jsonl")

# 一次运行的起点。取 env_check 而不是 prepare —— 没有叫 "prepare" 的阶段，
# prepare() 里的第一个 timed 就是 env_check（见 run.py）。
RUN_START = "env_check"

LEVELS = ("info", "timing", "warn", "error", "fixed", "waive", "decision")


def _ts():
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def path(work):
    return os.path.join(work, NAME)


def append(work, stage="", actor="", level="info", code="", where="",
           detail="", msg="", ms=None):
    """追加一条事件。

    刻意吞掉所有异常：记日志失败绝不能把翻译/排版打断。工作目录不合法、磁盘满、
    权限不足，都只是这条记录没写成，主流程照常。
    """
    try:
        if not work or not os.path.isdir(work) or level not in LEVELS:
            return False
        p = path(work)
        try:
            if os.path.getsize(p) > MAX_BYTES:
                os.replace(p, os.path.join(work, OLD))
        except OSError:
            pass
        ev = {"ts": _ts(), "stage": stage, "actor": actor, "level": level,
              "code": code, "where": str(where or ""),
              "detail": str(detail or ""), "msg": msg}
        if ms is not None:
            ev["ms"] = int(ms)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


class timed:
    """阶段计时上下文。

        with timed("extract", work, "extract.py"):
            ...

    正常退出记一条 level=timing；抛异常记 level=error + code=STAGE_FAIL，
    然后照常把异常抛出去 —— 只记录，不吞。
    """

    def __init__(self, stage, work, actor="", **extra):
        self.stage, self.work, self.actor = stage, work, actor
        self.extra = extra
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, etype, eval_, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        if etype is None:
            append(self.work, self.stage, self.actor, "timing", ms=ms, **self.extra)
        else:
            append(self.work, self.stage, self.actor, "error", code="STAGE_FAIL",
                   detail=f"{etype.__name__}: {eval_}", ms=ms, **self.extra)
        return False


def issue(work, stage, code, where="", detail="", msg="", level="warn", actor=""):
    """记一个问题。level=warn 表示可继续，error 表示会拦下流程。"""
    return append(work, stage, actor, level, code, where, detail, msg)


def resolve(work, stage, code, where="", actor="", msg="已修复"):
    """把同一 (code, where) 的问题标为已解决。"""
    return append(work, stage, actor, "fixed", code, where, "", msg)


def waive(work, stage, code, where="", actor="", detail=""):
    """人工确认可接受，不再报。"""
    return append(work, stage, actor, "waive", code, where, detail, "已豁免")


def decide(work, stage, code, detail="", msg="", where="", actor=""):
    """记一条可解释的决策：为什么选这张图、为什么丢这些句子等。"""
    return append(work, stage, actor, "decision", code, where, detail, msg)


def read(work, limit=None):
    """读回全部事件（轮转过的旧文件在前）。坏行跳过，不抛错。"""
    rows = []
    for name in (OLD, NAME):
        p = os.path.join(work, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows[-limit:] if limit else rows


def _state(evs):
    """把事件折叠成问题单：同一 (code, where) 只留一条，状态取最后一次事件。"""
    cur = {}
    for ev in evs:
        code = ev.get("code")
        if not code:
            continue
        st = {"warn": "open", "error": "open", "fixed": "resolved", "waive": "waived"}.get(
            ev.get("level"))
        if st is None:
            continue
        key = (code, ev.get("where", ""))
        it = cur.get(key)
        if it is None:
            it = cur[key] = {"code": code, "where": ev.get("where", ""), "state": st,
                             "count": 0, "first": ev.get("ts", ""), "last": "",
                             "detail": "", "msg": "", "stages": []}
        if st == "open":
            it["count"] += 1
            it["detail"] = ev.get("detail") or it["detail"]
            it["msg"] = ev.get("msg") or it["msg"]
        it["state"] = st
        it["last"] = ev.get("ts", "")
        if ev.get("stage") and ev["stage"] not in it["stages"]:
            it["stages"].append(ev["stage"])
    return cur


def _rank(cur):
    """按「还开着 → 次数多 → code」排序。"""
    return sorted(cur.values(), key=lambda x: (x["state"] != "open", -x["count"], x["code"]))


def issues(work, last=False):
    """聚合成问题单。

    last=True 只算最后一次运行，并给每条标 trend：new（上一轮没有）/ kept（上一轮也有）。
    注意不能简单"只取最后一段"就完事 —— 上一轮 open、本轮消失的问题会查无此事，
    看起来像"问题没了"，实际是这轮没检查到。所以 trend 要跟上一轮比。
    """
    evs = read(work)
    if not last:
        return _rank(_state(evs))
    chunks = _runs(evs)
    if not chunks:
        return []
    cur = _state(chunks[-1])
    prev = _state(chunks[-2]) if len(chunks) > 1 else {}
    for k, it in cur.items():
        it["trend"] = "kept" if k in prev else "new"
        if not it["msg"] and k in prev:      # 本轮才修好的，描述得从上一轮借
            it["msg"], it["detail"] = prev[k]["msg"], prev[k]["detail"]
    return _rank(cur)


def _runs(evs):
    """把事件按运行切段：每条 RUN_START 事件开启新的一段。

    第一个起点之前的零散事件（比如只跑了 summary、没跑 prepare）自成一段，排最前。
    这样「最后一次运行」要么是最近的 prepare 及其后续，要么是那段零散事件，
    不会因为缺 env_check 就返回空。
    """
    chunks, cur = [], None
    for ev in evs:
        start = (ev.get("stage") == RUN_START
                 and ev.get("level") in ("timing", "error"))
        if start or cur is None:
            cur = []
            chunks.append(cur)
        cur.append(ev)
    return chunks


def runs(work):
    """按运行切段后的原始事件列表（每段一个 list）。"""
    return _runs(read(work))


def _agg(evs):
    """把 (timing|error) 事件按阶段汇总成 {stage: {n, ms, max, fail}}。"""
    agg = {}
    for ev in evs:
        if ev.get("level") not in ("timing", "error") or "ms" not in ev:
            continue
        s = agg.setdefault(ev.get("stage", ""), {"n": 0, "ms": 0, "max": 0, "fail": 0})
        s["n"] += 1
        s["ms"] += ev["ms"]
        s["max"] = max(s["max"], ev["ms"])
        if ev.get("level") == "error":
            s["fail"] += 1
    return agg


def timing(work, last=False):
    """按阶段汇总耗时：{stage: {n, ms, max, fail}}。

    last=True 只算最后一次运行。为什么需要它：这个函数原本把日志里所有事件相加，
    同一篇 prepare 两次就报「次数 2、总耗时＝两次相加」，拿去跟上次比是错的。
    """
    evs = read(work)
    if last:
        chunks = _runs(evs)
        if chunks:
            evs = chunks[-1]
    return _agg(evs)


def decisions(work):
    """所有决策事件（按时间正序），用于回答「为什么是这张图」。"""
    return [ev for ev in read(work) if ev.get("level") == "decision"]


def _secs(ms):
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _dkey(ev):
    """决策的去重键：阶段 + 编码 + 文案 + 细节，全同才算同一条。"""
    return (ev.get("stage", ""), ev.get("code", ""),
            ev.get("msg", ""), ev.get("detail", ""))


def runlog():
    """读 runs.jsonl 并归一化。文件不存在返回 None。

    老记录（没有 v 字段）的 flags/path 一律置 None —— 不是 [] 或 ""，因为那时
    这两个字段根本没写，用空值表示会把「没有开关」和「记不到」混成一件事。
    """
    p = RUNLOG
    if not os.path.exists(p):
        return None
    rows = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            old = r.get("v", 1) < RUNLOG_V
            rows.append({"ts": r.get("ts", ""), "script": r.get("script", ""),
                         "sub": r.get("sub", ""), "ms": r.get("ms", 0),
                         "code": r.get("code", 0),
                         "flags": None if old else r.get("flags", []),
                         "path": None if old else r.get("path", ""), "old": old})
    return rows


def _local(ts):
    """UTC 的 ISO 时间转本地显示。runs.jsonl 记 UTC，journal.jsonl 记本地，差 8 小时。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return ts[:19]


def _runs_report(work):
    """runs.jsonl 里属于这个工作目录的调用：每条工具调用花了多少墙钟。"""
    rows = runlog()
    if rows is None:
        return f"（没有调用记录：{RUNLOG} 不存在）"
    base = os.path.normcase(os.path.abspath(work)) + os.sep
    # 加分隔符再比，否则 probe-last 会把 probe-last-2 也算进来
    mine = [r for r in rows
            if r["path"] and os.path.normcase(os.path.abspath(r["path"]) + os.sep).startswith(base)]
    old = len([r for r in rows if r["old"]])
    if not mine:
        extra = f"；{old} 条是老格式，认不出归属" if old else ""
        return (f"（{os.path.basename(os.path.abspath(work))} 没有调用记录，"
                f"共 {len(rows)} 条属其它任务{extra}）")
    out = ["时间 | 脚本 | 子命令 | 参数 | 墙钟 | 结果"]
    for r in mine:
        arg = "—" if r["flags"] is None else (" ".join(r["flags"]) or "（无）")
        out.append(f"{_local(r['ts'])} | {r['script']} | {r['sub'] or '—'} | {arg} | "
                   f"{_secs(r['ms'])} | " + ("ok" if r["code"] == 0 else f"code={r['code']}"))
    out += ["", f"本篇 {len(mine)} 条 / 共 {len(rows)} 条，墙钟合计 "
                f"{_secs(sum(r['ms'] for r in mine))}（含进程启动，与 journal 的本地耗时不可直接相加）"]
    if old:
        out.append(f"另有 {old} 条老格式记录没有 flags/path，参数与归属都未知")
    return "\n".join(out)


def _ts2dt(ts):
    """UTC ISO 时间字符串转 datetime。runs.jsonl 记 UTC（带 Z）。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _model_time(work):
    """从 runs.jsonl 差分重建模型侧耗时。

    插件进程感知不到模型思考/生成的时间，但每笔工具调用的 ts 都留在 runs.jsonl：
      - 模型翻译 = 每批 paper_next（flags 含 --next）到紧邻 paper_fill（flags 含 --file）的间隔
      - 模型总结 = summary_brief（sub=summary）到最后一轮 summary-check（sub=summary-check）的间隔
    返回 (翻译秒, 总结秒, 翻译批次数)。无记录时返回 (0.0, 0.0, 0)。
    """
    rows = runlog()
    if not rows:
        return 0.0, 0.0, 0
    base = os.path.normcase(os.path.abspath(work)) + os.sep
    mine = [r for r in rows
            if r["path"] and r["flags"] is not None
            and os.path.normcase(os.path.abspath(r["path"]) + os.sep).startswith(base)]
    mine.sort(key=lambda r: r["ts"])

    tr, n_batch = 0.0, 0
    for i in range(len(mine) - 1):
        a, b = mine[i], mine[i + 1]
        fa = a["flags"] or []
        fb = b["flags"] or []
        if a["script"] == "fill.py" and "--next" in fa \
           and b["script"] == "fill.py" and ("--file" in fb or "--status" in fb):
            ta, tb = _ts2dt(a["ts"]), _ts2dt(b["ts"])
            if ta and tb and tb >= ta:
                tr += (tb - ta).total_seconds()
                n_batch += 1

    sum_s = 0.0
    brief = last_check = None
    for r in mine:
        if r["script"] == "run.py" and r.get("sub") == "summary":
            brief = _ts2dt(r["ts"])
        if r["script"] == "run.py" and r.get("sub") == "summary-check":
            last_check = _ts2dt(r["ts"])
    if brief and last_check and last_check > brief:
        sum_s = (last_check - brief).total_seconds()

    return tr, sum_s, n_batch


def _e2e_report(work):
    """端到端耗时：本地脚本墙钟 + 模型翻译/总结。

    端到端口径用「本地 + 模型翻译 + 模型总结」三者相加，而不是首末事件跨度——
    首末跨度会被事后的查询命令（journal/audit）把末事件拖到几小时后而失真。
    模型时间是「思考 + 生成 + 工具往返」的上界，非纯模型推理时间。
    """
    if not os.path.isdir(work):
        return f"! 工作目录不存在: {work}"
    tr, sum_s, n_batch = _model_time(work)
    local = sum(v["ms"] for v in timing(work, False).values()) if os.path.exists(path(work)) else 0
    total = tr + sum_s + local / 1000.0
    out = [
        "端到端耗时（本地脚本 + 模型翻译 + 模型总结）",
        "",
        f"  模型翻译  {_secs(tr * 1000):>10}" + (f"（{n_batch} 批 next→fill）" if n_batch else "（无记录）"),
        f"  模型总结  {_secs(sum_s * 1000):>10}" + ("（brief→最后一轮 check）" if sum_s else "（无记录）"),
        f"  本地脚本  {_secs(local):>10}（journal 累计口径）",
        "  " + "-" * 30,
        f"  端到端 ≈  {_secs(total * 1000):>10}",
        "",
        "说明：模型时间是 next→fill / brief→check 的间隔差分，含思考、生成与工具往返开销，",
        "是模型侧耗时的上界；端到端为三者相加，不把事后的查询命令计入。",
    ]
    if n_batch == 0 and not sum_s:
        out.append("")
        out.append("（runs.jsonl 里没有这篇的 next/fill 记录，可能是老格式或非翻译任务）")
    return "\n".join(out)


def report(work, what="summary", last=False):
    """生成给人看的报告文本。what = summary / issues / timing / decisions / tail / runs / e2e。

    last=True 时，timing 与 issues 只算最后一次运行（见 timing()）。其它 what 不区分。
    runs / e2e 读的是插件的 data/runs.jsonl，不依赖 work 里的 journal.jsonl，所以在前面分流。
    """
    if what == "runs":
        return _runs_report(work)
    if what == "e2e":
        return _e2e_report(work)
    if not os.path.isdir(work):
        return f"! 工作目录不存在: {work}"
    if not os.path.exists(path(work)) and not os.path.exists(os.path.join(work, OLD)):
        return "（这篇还没有任何过程记录，先跑 prepare 或 verify）"

    if what == "timing":
        agg = timing(work, last)
        if not agg:
            return "（最后一次运行没有耗时记录）" if last else "（没有耗时记录）"
        total = sum(v["ms"] for v in agg.values()) or 1
        rows = sorted(agg.items(), key=lambda kv: -kv[1]["ms"])
        w = max(len(k) for k in agg) + 2
        out = [f"{'阶段':<{w}}{'次数':>5}{'总耗时':>10}{'占比':>8}{'单次上限':>10}"]
        for name, v in rows:
            out.append(f"{name:<{w}}{v['n']:>5}{_secs(v['ms']):>10}"
                       f"{v['ms'] / total * 100:>7.0f}%{_secs(v['max']):>10}"
                       + ("  （有失败）" if v["fail"] else ""))
        out.append("")
        out.append(f"{'本次' if last else '本地'}合计 {_secs(total)}（不含模型侧思考与生成时间）")
        if last:
            chunks = _runs(read(work))
            if chunks:
                first = chunks[-1][0]
                if first.get("stage") == RUN_START:
                    out.insert(0, f"仅最后一次运行（起点 {first.get('ts', '')[:19]}，"
                                  f"{len(chunks[-1])} 个事件）")
                else:
                    out.insert(0, f"未找到 prepare 起点，按全部事件计（共 {len(chunks[-1])} 个）")
                out.insert(1, "")
        return "\n".join(out)

    if what == "issues":
        rows = issues(work, last)
        if not rows:
            return "（最后一次运行没有问题记录）" if last else "没有问题记录。"
        op = [r for r in rows if r["state"] == "open"]
        fresh = len([r for r in op if r.get("trend") == "new"])
        out = [f"待处理 {len(op)} 条" + (f"（本次新增 {fresh}）" if last else "")
               + f" / 共记录 {len(rows)} 条", ""]
        for r in rows:
            mark = {"open": "○", "resolved": "✓", "waived": "—"}[r["state"]]
            loc = f" @{r['where']}" if r["where"] else ""
            trend = r.get("trend")
            tag = ""
            if trend == "new":
                tag = "  ← 本次新增"
            elif trend == "kept":
                tag = "  ← 本次已修" if r["state"] == "resolved" else "  ← 历史遗留"
            # detail 常常就是 msg 的截断版，重复显示只是噪音
            det = f"  {r['detail']}" if r["detail"] and r["detail"] != r["msg"] else ""
            out.append(f"{mark} [{r['code']}{loc}] {r['msg']}{det}"
                       + (f"  ×{r['count']}" if r["count"] > 1 else "") + tag)
            if r["stages"]:
                out.append(f"    节点: {' → '.join(r['stages'])}")
        out.append("")
        out.append("○ 待处理   ✓ 已修复   — 已豁免")
        return "\n".join(out)

    if what == "decisions":
        rows = decisions(work)
        if not rows:
            return "（没有决策记录）"
        # 同一次决策在反复跑 pack 时会重复落盘；只留最新一条并标注次数，
        # 否则「为什么选图 4」会被三条一模一样的内容埋掉。
        cnt = {}
        for ev in rows:
            cnt[_dkey(ev)] = cnt.get(_dkey(ev), 0) + 1
        out, seen = [], set()
        for ev in reversed(rows):
            k = _dkey(ev)
            if k in seen:
                continue
            seen.add(k)
            line = (f"[{ev.get('stage', '')}] {ev.get('code', '')}"
                    f"  {ev.get('msg', '')}"
                    + (f"  // {ev['detail']}" if ev.get("detail") else ""))
            if cnt[k] > 1:
                line += f"  ×{cnt[k]}"
            out.append(line)
        out.reverse()
        return "\n".join(out)

    if what == "tail":
        rows = read(work, limit=30)
        out = []
        for ev in rows:
            ms = f"  {_secs(ev['ms'])}" if "ms" in ev else ""
            out.append(f"{ev.get('ts', '')[11:19]}  {ev.get('level', ''):<8}"
                       f" {ev.get('stage', ''):<12} {ev.get('code', '')}"
                       + (f" @{ev['where']}" if ev.get("where") else "") + ms)
        return "\n".join(out)

    # summary：一次运行的体检表
    evs = read(work)
    agg = timing(work)
    rows = issues(work)
    open_n = len([r for r in rows if r["state"] == "open"])
    first = evs[0].get("ts", "") if evs else ""
    last = evs[-1].get("ts", "") if evs else ""
    out = ["# 过程记录", ""]
    out.append(f"事件 {len(evs)} 条   首次 {first[:19]}   最近 {last[:19]}")
    out.append(f"耗时合计 {_secs(sum(v['ms'] for v in agg.values()))}"
               f"   问题 {len(rows)} 条（待处理 {open_n}）")
    out.append("")
    if agg:
        out.append("## 耗时")
        out.append(report(work, "timing").rstrip())
        out.append("")
    fails = [ev for ev in evs if ev.get("level") == "error"]
    if fails:
        out.append("## 失败的阶段")
        for ev in fails:
            out.append(f"- {ev.get('stage')} @{ev.get('ts', '')[11:19]}  {ev.get('detail', '')}")
        out.append("")
    if rows:
        out.append("## 问题")
        out.append(report(work, "issues"))
    return "\n".join(out)
