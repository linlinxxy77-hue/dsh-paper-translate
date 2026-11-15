# -*- coding: utf-8 -*-
"""lexicon.py —— verify.py 与 summarize.py 共用的词级常量。

`EN_KEEP` 原来在 verify.py 和 summarize.py 各存一份 `set`。两份内容一致时看不出问题，
一旦只改一边就会出现「同一段译文，译后自检说违规、素材包自检说没问题」的口径打架。
这种表只该有一份，放在这里。

收录标准（按 `bare_en()` 的判据）：出现在中文句子里、按惯例既不译成中文、
也不需要写成「中文（English）」的词。三类：
  - 拉丁短语：in vitro / in vivo / de novo / in silico
  - 试剂与缓冲液简写：Tris、HEPES、Tween、Triton、EDTA、DMSO、NaCl、Ficoll
  - 单位与时间简写：hour、mmol、approx

注意：`bare_en()` 只抓「≥4 字母、全小写、纯字母」的词。三个字母的简写（pbs、min）
本来就被长度判据放过，写在这里不生效也不碍事 —— 留着是为了将来放宽长度阈值时
口径不断档。真正靠这张表生效的是 vitro / silico / approx / mmol 这类长词。
"""

EN_KEEP = set("""
vitro vivo silico novo poly et al vs etc eg ie approx resp
hpf hpi rpm min sec hour mmol um nm od wt vol
ficoll tris hepes tween triton edta dmso nacl kcl dmem hbss
""".split())
