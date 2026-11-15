# -*- coding: utf-8 -*-
"""verify.py —— 交付前自检（不开图、不导出，纯结构校验）。

用法: python verify.py <content.json> <figs_dir> [out.docx]
输出: 报告文本 + 退出码（0 通过 / 1 有告警 / 2 仍未译完）

v3 新增（都靠本地计算，不耗模型 token）：
  - 进度：还有未译项直接拦下
  - 数字一致性：原文出现的关键数值（≥2 位或含小数）在译文中应能找到
  - 图表引用完整性：原文 Fig. 3 / Table 1 在译文中应有对应「图 3」「表 1」
  - 公式误判：eq 项里出现多个英文单词 → 多半是正文段落被误判成公式
  - 图文件命名：figs/ 里的文件与 content.json 引用一一对应
  - 术语括注：中文句子里只写英文术语会点名，要写成「中文（English）」
"""
import json
import os
import re
import sys

CJK = re.compile(r"[\u4e00-\u9fff]")
FIG_EN = re.compile(r"\b(?:Figs?\.?|Figures?)\s*(\d+)", re.I)
TAB_EN = re.compile(r"\bTables?\s*(\d+)", re.I)
FIG_ZH = re.compile(r"图\s*(\d+)")
TAB_ZH = re.compile(r"表\s*(\d+)")
NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
# 数量词：40 million ↔ 4000 万 是同一个数，不展开就会把「40」判成缺失。
# million/billion 在原文侧展开成数值，万/亿 在译文侧展开成数值，两侧再比纯数字。
SCALE_EN = re.compile(r"(\d+(?:\.\d+)?)\s*(million|billion|thousand)\b", re.I)
SCALE_ZH = re.compile(r"(\d+(?:\.\d+)?)\s*(万亿|亿|万)\b")
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "万": 1e4, "亿": 1e8, "万亿": 1e12}
CAP_HEAD = re.compile(r"^(?:图|表)\s*\d+\s*[|｜]")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from lexicon import EN_KEEP  # noqa: E402  —— 与 summarize.py 共用一份，见 lexicon.py
import journal as J  # noqa: E402  —— 过程记录，见 journal.py

# 术语括注：保留英文时必须写成「中文（English）」，只写英文不行
BARE_EN = re.compile(r"[A-Za-z]+(?:[-'\u2032][A-Za-z]+)*")


ZH_PUNCT = "，。、；：！？（）「」《》【】…—"


def is_cjk(c):
    return bool(c) and "\u4e00" <= c <= "\u9fff"


def is_zh_ctx(c):
    """中文语境：汉字或中文标点。英文夹在「，」和「。」之间也算中文句子。"""
    return is_cjk(c) or (bool(c) and c in ZH_PUNCT)


def in_paren(text, i):
    """位置 i 是否还在某个括号里 —— 「非翻译区（untranslated regions，UTRs）」
    里的 regions 只是英文全称的一部分，不该单独判为裸术语。"""
    opened = max(text.rfind("（", 0, i), text.rfind("(", 0, i))
    if opened < 0:
        return False
    return opened > max(text.rfind("）", 0, i), text.rfind(")", 0, i))


def bare_en(text):
    """中文句子里「只写英文」的术语，返回去重后的词表。

    含大写或数字的是基因名（eIF5A）、方法缩写（NaP-TRAP）、试剂型号，按惯例保留原文。
    合规写法是「中文（English）」；写成纯中文也不报。
    """
    out = []
    text = text or ""
    for m in BARE_EN.finditer(text):
        w = m.group(0)
        if len(w) < 4 or w != w.lower() or w.lower() in EN_KEEP:
            continue
        if "-" in w or "'" in w or "\u2032" in w:   # pull-down / scikit-learn 这类固定写法
            continue
        i, j = m.start(), m.end()
        if in_paren(text, i):
            continue                           # 英文括注里的全称，逐词判会碎成一片
        k = i - 1
        while k >= 0 and text[k] in " \t":     # 中英文之间常有一个空格
            k -= 1
        prev = text[k] if k >= 0 else ""
        n = j
        while n < len(text) and text[n] in " \t":
            n += 1
        nxt = text[n] if n < len(text) else ""
        if not (is_zh_ctx(prev) or is_zh_ctx(nxt)):
            continue                           # 整串英文（序列、货号）不在此列
        if prev in "（(" and k >= 1 and is_cjk(text[k - 1]):
            continue                           # 合规：中文（English）
        if w not in out:
            out.append(w)
    return out


def _expand_scale(text):
    """把「数字+数量词」展开成纯数字，再归一化千分位/小数点，两侧可比。

    - 原文 `40 million` → `40000000`；译文 `4000 万` → `40000000`，缺「40」消失。
    - 千分位逗号与欧洲小数点：`11,088`、`11.088`、`11088` 都归一成 `11088`；
      但 `85.5` 这种真小数不能动 —— 判据是「点后恰好 3 位且点前 1–3 位」，
      且不是 1 位整数后跟小数（`3.5` 不在此列）。
    """
    s = text or ""
    for m in SCALE_EN.finditer(s):
        s = s.replace(m.group(0), str(int(float(m.group(1)) * _SCALE[m.group(2).lower()])), 1)
    for m in SCALE_ZH.finditer(s):
        s = s.replace(m.group(0), str(int(float(m.group(1)) * _SCALE[m.group(2)])), 1)
    # 千分位：1–3 位整数 + 逗号 + 3 位一组，或欧洲写法点号分组的，都拆掉分组符
    s = re.sub(r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+)(?!\d)", lambda m: m.group(1).replace(",", "").replace(".", ""), s)
    return s


def key_nums(text):
    """只取「>=2 位或含小数」的数值，单数字噪音太大（5′UTR、n=2 之类）。

    千分位先整体匹配再拆逗号：原文 ``11,088`` 与译文 ``11088`` 视为同一个数，
    否则会被切成 11 / 088 两段而永远对不上。
    """
    out = set()
    for m in NUM.finditer(_expand_scale(text)):
        s = m.group(0).replace(",", "")
        if len(s.replace(".", "")) >= 2:
            out.add(s)
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: verify.py <content.json> <figs_dir> [out.docx]")
        return 1
    cpath, figdir = sys.argv[1], sys.argv[2]
    docx_path = sys.argv[3] if len(sys.argv) > 3 else None
    C = json.load(open(cpath, encoding="utf-8"))
    warns = []
    work = os.path.dirname(os.path.abspath(cpath))

    def warn(code, msg, where=""):
        """code 是跨 agent 对齐用的稳定标识，msg 是给人看的话，两者都不该随手改。"""
        warns.append((code, where, msg))

    sp = os.path.join(os.path.dirname(os.path.abspath(cpath)), "source.json")
    S = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else None

    body = C.get("body", [])
    front = C.get("front", [])
    kinds = {}
    for it in body:
        kinds[it["t"]] = kinds.get(it["t"], 0) + 1

    # ---------- 1. 进度
    def is_filled(it):
        if it["t"] in ("fig", "table"):
            return bool((it.get("caption") or "").strip())
        return bool((it.get("text") or "").strip())

    todo = [i for i, it in enumerate(body + front) if not is_filled(it)]
    if todo:
        warn("TODO_LEFT", f"仍有 {len(todo)} 项未译（首项 seq={todo[0]}），先跑 fill.py --status",
             f"seq={todo[0]}")

    # ---------- 2. 图号与图文件
    fig_blocks = [it for it in body if it["t"] == "fig"]
    fig_nums = [it["n"] for it in fig_blocks]
    want = list(range(1, len(fig_nums) + 1))
    if sorted(set(fig_nums)) != want:
        warn("FIG_SEQ", f"图号不连续或重复: {sorted(set(fig_nums))}（应为 1..{len(want)}）")
    on_disk = set()
    if os.path.isdir(figdir):
        for fn in os.listdir(figdir):
            m = re.match(r"^(fig|table)(\d+)\.png$", fn)
            if m:
                on_disk.add((m.group(1), int(m.group(2))))
    for it in fig_blocks:
        p = os.path.join(figdir, f"fig{it['n']}.png")
        if not os.path.exists(p):
            warn("FIG_MISSING", f"缺图文件 fig{it['n']}.png", f"fig{it['n']}")
        elif os.path.getsize(p) < 8000:
            warn("FIG_TINY", f"fig{it['n']}.png 体积过小，可能裁空了", f"fig{it['n']}")
    extra = sorted(n for k, n in on_disk if k == "fig" and n not in set(fig_nums))
    if extra:
        warn("FIG_EXTRA", f"figs/ 里有多余图文件未被引用: {extra}（编号可能错位）")

    # ---------- 3. 数字一致性
    if S:
        miss = []
        for key, meta in S["items"].items():
            if meta["sect"] not in ("front", "body"):
                continue
            arr = C.get(meta["sect"]) or []
            if meta["i"] >= len(arr):
                continue
            it = arr[meta["i"]]
            dst = it.get("caption") if it["t"] in ("fig", "table") else it.get("text")
            if not dst:
                continue
            lack = key_nums(meta.get("src")) - key_nums(dst)
            if lack:
                miss.append((key, sorted(lack)))
        if miss:
            detail = "; ".join(f"#{k} 缺 {v}" for k, v in miss[:8])
            warn("NUM_MISMATCH", f"{len(miss)} 处数字与原文不一致: {detail}"
                 f"{' …' if len(miss) > 8 else ''}",
                 ",".join(k for k, _ in miss[:8]))

    # ---------- 3b. 符号丢失（希腊字母 / 度数，与数字错位分开报）
    # 学术符号（μ、Δ、α、° 等）翻译时不能丢：`30 μl` → `30 l` 单位差了 10⁶。
    # 数字检查（key_nums）看不到这种丢失（数字都在），要单独盯符号。
    if S:
        sym = re.compile(r"[\u00b0\u03b1-\u03c9\u0391-\u03a9\u221a\u00b1\u00d7\u2032]")
        lost_sym = []
        for key, meta in S["items"].items():
            if meta["sect"] not in ("front", "body"):
                continue
            arr = C.get(meta["sect"]) or []
            if meta["i"] >= len(arr):
                continue
            it = arr[meta["i"]]
            dst = it.get("caption") if it["t"] in ("fig", "table") else it.get("text")
            if not dst:
                continue
            s_src, s_dst = set(sym.findall(meta.get("src") or "")), set(sym.findall(dst))
            gone = s_src - s_dst
            if gone:
                lost_sym.append((key, sorted(gone)))
        if lost_sym:
            detail = "; ".join(f"#{k} 丢 {''.join(v)}" for k, v in lost_sym[:6])
            warn("SYMBOL_LOST", f"{len(lost_sym)} 处学术符号缺失: {detail}"
                 f"{' …' if len(lost_sym) > 6 else ''}",
                 ",".join(k for k, _ in lost_sym[:6]))

    # ---------- 4. 图表引用完整性
    src_all = "\n".join(m.get("src") or "" for m in (S["items"].values() if S else []))
    txt_all = "\n".join(
        (it.get("caption") or it.get("text") or "") for it in body + front)
    if S:
        ref_fig = {int(x) for x in FIG_EN.findall(src_all)}
        ref_tab = {int(x) for x in TAB_EN.findall(src_all)}
        got_fig = {int(x) for x in FIG_ZH.findall(txt_all)}
        got_tab = {int(x) for x in TAB_ZH.findall(txt_all)}
        lost_f = sorted(ref_fig - got_fig)
        lost_t = sorted(ref_tab - got_tab)
        if lost_f:
            warn("FIGREF_LOST", f"正文引用了图 {lost_f}，但译文中找不到「图 N」",
                 ",".join(f"fig{n}" for n in lost_f))
        if lost_t:
            warn("TABREF_LOST", f"正文引用了表 {lost_t}，但译文中找不到「表 N」",
                 ",".join(f"tab{n}" for n in lost_t))

    # ---------- 5. 公式误判
    for i, it in enumerate(body):
        if it["t"] != "eq":
            continue
        words = re.findall(r"[A-Za-z]{3,}", it.get("text") or "")
        if len(words) >= 6:
            snip = (it.get("text") or "")[:60]
            warn("EQ_FALSE", f"body[{i}] 标为公式但含 {len(words)} 个英文单词，"
                 f"疑似正文段落被误判：{snip!r}", f"body|{i}")

    # ---------- 6. 标点与漏译
    for bad in ("。。", "，，", "；；", "、、", "（）"):
        n = txt_all.count(bad)
        if n:
            warn("PUNCT_ODD", f"标点异常 {bad!r} x{n}", bad)

    FUNC = {"the", "and", "of", "is", "are", "was", "were", "with", "that", "for",
            "to", "in", "we", "this", "as", "by", "from", "which", "not", "on"}

    # ---------- 6b. 正文段落漏译（PARA_UNTRANSLATED）
    # body 项 → 原文（source.json 里 sect=body 且 i 对应）
    src_by_i = {}
    if S:
        for meta in S["items"].values():
            if meta["sect"] == "body":
                src_by_i[meta["i"]] = meta.get("src") or ""
    pure_en = 0
    for i, b in enumerate(body):
        if b["t"] != "p" or len(b.get("text") or "") < 40:
            continue
        t = b["text"]
        if CJK.search(t) or looks_like_sequence(t):
            continue
        # 作者/单位/DOI/期刊引文/声明按惯例保留原文，不算漏译（P2-2）
        if keep_original_text(src_by_i.get(i, "")):
            continue
        words = [w.lower() for w in re.findall(r"[A-Za-z']+", t)]
        if len(words) > 12 and sum(w in FUNC for w in words) >= 3:
            pure_en += 1
    if pure_en:
        warn("PARA_UNTRANSLATED", f"{pure_en} 个正文段落疑似未翻译")

    # ---------- 6c. 术语括注：保留英文时必须写「中文（English）」，只写英文算不合格
    src_all = "\n".join((m.get("src") or "") for m in (S or {}).get("items", {}).values())
    bare = []
    for b in body:
        if b["t"] not in ("p", "caption"):
            continue
        for w in bare_en(b.get("caption") or b.get("text") or ""):
            if w in bare:
                continue
            # 原文里都写作大写（NanoLuc、Clean and Concentrator）→ 专名被小写化了，不算裸术语；
            # 原文压根没有这个词（引物编号被拆）也放过。
            if src_all:          # 没有原文就免谈专名，宁可多报也别静默放过
                hits = re.findall(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])",
                                  src_all, re.I)
                # 只认「全大写或带内部大写」的形态（NanoLuc、PCR）；句首大写（Western）不算专名
                if hits and all(h.upper() == h or any(c.isupper() for c in h[1:])
                                for h in hits):
                    continue
            bare.append(w)
    if bare:
        warn("TERM_BARE", f"{len(bare)} 个术语只写了英文：" + "、".join(bare[:6])
             + ("…" if len(bare) > 6 else "")
             + "。保留英文时写成「中文（English）」",
             ",".join(bare[:6]))

    # ---------- 7. docx 结构
    if docx_path and os.path.exists(docx_path):
        from docx import Document
        d = Document(docx_path)
        imgs = len(d.inline_shapes)
        if imgs != len(fig_blocks):
            warn("DOCX_FIG_COUNT", f"docx 内图片 {imgs} 张，content 中图 {len(fig_blocks)} 个")
        tables = len(d.tables)
        want_t = kinds.get("table", 0)
        if tables != want_t:
            warn("DOCX_TAB_COUNT", f"docx 表格 {tables} 个，content 中表 {want_t} 个")
        empty = sum(1 for p in d.paragraphs if not p.text.strip() and not p._p.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"))
        print(f"docx: 段落 {len(d.paragraphs)}（空段 {empty}）图片 {imgs} 表格 {tables}")

    print("content:", {k: v for k, v in sorted(kinds.items())},
          "refs", len(C.get("refs", [])), "figs", len(fig_blocks),
          "未译", len(todo))
    # 落盘：同一 (code, where) 在日志里会自动聚合，跨 agent 校对时不必再靠复述对账
    for code, where, msg in warns:
        J.issue(work, "verify", code, where, "", msg)
    J.append(work, "verify", "verify.py", "info", code="VERIFY_RUN",
             detail=f"告警 {len(warns)} 条，未译 {len(todo)} 项")

    if warns:
        print("\n[告警]")
        for code, where, w in warns:
            print(" -", w)
        # 未译属于「还没做完」，与「做完了但有疑点」区分开
        return 2 if todo else 1
    print("\n[通过] 结构自检无异常")
    return 0


def looks_like_sequence(t):
    seq = sum(c in "ACGTUacgtu-′'" for c in t) / max(len(t), 1)
    return seq > 0.5 or "′-" in t


# 学术惯例「保留原文、不译」的块 —— 作者、单位、期刊引文、DOI、通讯信息、声明。
# 这些块翻成中文反而不对（人名、机构名、DOI 本就照抄），verify 与 fill 都要跳过
# 「未见中文」检查，否则每批都会误报（报告 P2-2：第 4 篇单批 13 条）。
#
# 判据要分「强弱」：邮箱/DOI/期刊引文是强特征（正文几乎不会整段出现），命中必 keep；
# 单位词、姓名角标这类是弱特征，单独出现可能是正文（「Institutional Review Board」、
# 参考文献角标 `~{10}`），必须和其它证据叠加才判 keep。见 keep_original_text。
_KEEP_STRONG = (
    re.compile(r"@[\w.\-]+\.[a-z]{2,}", re.I),                     # 邮箱
    re.compile(r"\bdoi\s*[:：]\s*\S+", re.I),                      # doi: 前缀
    re.compile(r"\b10\.\d{4,9}/\S+"),                               # 裸 DOI
    re.compile(r"\b\d{4}\s+\w+\s+\d{1,2}[;:]\s*\d+\s*\(\d+\)"),     # 期刊引文 Immunity. 2023 Nov 14; 56(11)
)
_KEEP_URL = re.compile(r"https?://\S+", re.I)                     # URL（弱：方法正文常引数据库链接）
_KEEP_SECTION = re.compile(                                       # 声明类标题（整段以它开头）
    r"(?i)^\s*(?:declaration of (?:competing )?interests|author contributions?|"
    r"authors'? contributions?|competing interests?|data availability|"
    r"acknowledg?ments?|correspondence|lead contact|"
    r"to whom correspondence may be addressed)")
_KEEP_AFFIL = re.compile(                                        # 单位词（弱，需叠加）
    r"(?i)\b(?:department|university|institute|school of|college of|"
    r"laboratory of|division of|faculty of|centres? of|centers? of)\b")
_KEEP_NAME_TAG = re.compile(r"~\{\d+(?:,[\d*§])*\}")            # 姓名角标 Lei~{1,*} / ~{12,13,§}
_KEEP_ADDR = re.compile(r"(?i)\b(?:[A-Z]{2}\s+\d{5}|\d{5},?\s+[A-Z]{2})\b"
                        r"|\b(?:USA|China|UK|Japan|Germany|France)\b")  # 邮编/国名地址
# 谓语动词：元信息块（标题/作者/单位）几乎无谓语，正文块必有。抽出来供多条判据复用。
_VERB = re.compile(r"\b(?:was|were|are|is|showed?|found|used|performed|detected|"
                   r"observed|identified|shown|described|analyzed|evaluated|"
                   r"demonstrated|contained?|encoded?|consist|cleav\w*|express\w*)\b", re.I)


def _sentence_count(t):
    """完整句数量：句号/问号/叹号后跟「空格 + 大写」的边界数。

    用来区分「纯元信息」和「正文 + 脚注混合」：作者列表、单位、DOI 这类元信息
    几乎没有完整句；而第 5 篇引言段末尾混进 `Email: ttamm@ut.ee` 这种脚注，
    主体是 3 个完整句，整块判 keep 就会漏译一整段正文。
    """
    return len(re.findall(r"[.?!]\s+[A-Z(]", t or ""))


def keep_original_text(t):
    """这段原文是否按学术惯例保留原文（作者/单位/DOI/期刊引文/声明）。

    强特征（邮箱/DOI/期刊引文/声明开头）命中还要看「是不是整块都是元信息」——
    如果块里有 ≥2 个完整句，说明主体是正文、只是末尾混了脚注，仍要翻译。
    弱特征（单位词、姓名角标）必须满足「整段像元信息」才 keep。
    """
    t = (t or "").strip()
    if not t:
        return False
    n_sent = _sentence_count(t)
    # 强特征（邮箱/DOI/期刊引文）：只有「整块就是这一条元信息」才 keep。
    # 旧判据 `n_sent < 2` 太松——1 句正文 + 末尾脚注（Email:…）也会命中，漏译整段。
    # 改成「短块 + 无完整句」，任何含完整句的块即使带邮箱也不整块 keep。
    if any(p.search(t) for p in _KEEP_STRONG) and n_sent == 0 and len(t) < 160:
        return True
    # URL 是弱特征：方法正文里到处都是数据库/软件链接（GISAID、GraphPad），
    # 只有「很短的纯链接/脚注块、且无谓语动词」才 keep（否则正文带链接会被整段漏译）。
    if _KEEP_URL.search(t) and len(t) < 150 and not _VERB.search(t):
        return True
    if _KEEP_SECTION.search(t):
        return True
    # 作者列表：多个姓名角标（每个作者一个 `~{...}`，含 * / , / § 才是姓名角标，
    # 参考文献角标 `~{24}` 是单个纯数字、偶发），逗号串联，几乎无谓语。
    tags = _KEEP_NAME_TAG.findall(t)
    if len(tags) >= 3 and t.count(",") >= 3 and n_sent < 2:
        if not _VERB.search(t):
            return True
    # 单位块：短段 + 单位词 + 地址（邮编/国名），且无句号结尾
    if _KEEP_AFFIL.search(t) and _KEEP_ADDR.search(t) and not t.endswith("."):
        return True
    # —— 三条盲区补丁（P2-2 修正，2026-11-15）：上面只认「带角标作者」「美式地址」，
    # 重抽后作者角标丢失、地址非美式，这类最常见的元信息块全漏。共同安全闸门：
    # 短块（len<160，正文实测 ≥500 字）+ 无完整句（n_sent==0）+ 无谓语动词。
    # 标题（type=title）不在这里判——sentence case 标题（仅首词大写）文本特征不可靠，
    # 改由 fill.py 按 type==title 直接放行，避免误伤章节小标题（h2）。
    if n_sent == 0 and not _VERB.search(t) and len(t) < 160:
        # B｜人名列表：逗号/and 串联多个首字母大写片段（"Ivan Kisly, Carolin Kattel…"）
        names = [w for w in re.split(r"[,;]|\band\b", t) if w.strip()]
        if len(names) >= 2 and all(n.strip()[0].isupper() for n in names if n.strip()):
            return True
        # C｜邮编地址：单位词 + 邮编数字 + 无句号结尾（"…Tartu 51010, Estonia"）
        if _KEEP_AFFIL.search(t) and re.search(r"\b\d{4,6}\b", t) \
                and not t.endswith("."):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
