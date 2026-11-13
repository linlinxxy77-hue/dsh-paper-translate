# -*- coding: utf-8 -*-
"""run.py —— 一条命令跑完前置流程，减少往返。

用法:
    python run.py prepare <论文.pdf> <工作目录>      # 自检 → 抽取 → 骨架 → 裁图
    python run.py audit <工作目录> [n]               # 抽检 n 条，原文与译文并排
    python run.py finish <工作目录> [out.docx]       # 排版 → 自检（+ 可选导 PDF）
    python run.py harvest <工作目录>                 # 译完沉淀术语候选（补中文后 --merge）

prepare 成功后会直接打印下一步该敲的命令，不需要再看中间产物。
依赖缺失时以退出码 2 结束，并原样转达 env_check 的安装清单 —— 不会擅自安装。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def call(script, *args):
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


def line_with(text, *keys):
    for ln in text.splitlines():
        if all(k in ln for k in keys):
            return ln.strip()
    return ""


def prepare(pdf, work):
    if not os.path.exists(pdf):
        print("! 找不到 PDF:", pdf)
        return 1
    os.makedirs(work, exist_ok=True)

    rc, out = call("env_check.py")
    if rc != 0:
        print(out.strip())
        print("\n[停] 依赖未就绪，先按上面的清单处理，再重跑 prepare。")
        return 2
    py = line_with(out, "PYTHON")
    print("[1/4] 环境 OK ", py.replace("PYTHON", "").strip())

    rc, out = call("extract.py", pdf, work)
    if rc != 0:
        print(out.strip())
        return 3
    stats = line_with(out, "\"blocks\"")
    print("[2/4] 抽取   ", stats or out.strip().splitlines()[0])

    spath = os.path.join(work, "structure.json")
    cpath = os.path.join(work, "content.json")
    rc, out = call("make_skeleton.py", spath, cpath)
    if rc != 0:
        print(out.strip())
        return 4
    todo = line_with(out, "待翻译").replace("待翻译:", "").split("；")[0].strip()
    pre = line_with(out, "已预填").replace("已预填:", "").split("（")[0].strip()
    print(f"[3/4] 骨架    待译 {todo} ｜ 已预填 {pre}")

    rc, out = call("crop_figs.py", pdf, spath, os.path.join(work, "figs"))
    if rc != 0:
        print(out.strip())
        return 5
    crop = line_with(out, "crop:")
    print("[4/4] 裁图   ", crop.replace("crop:", "").strip() or "完成")
    warn = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("!")]
    for w in warn:
        print("      ", w)

    print(f"\n工作目录: {os.path.abspath(work)}")
    print("下一步（分批翻译，模型只输出译文）:")
    print(f'  python "{os.path.join(HERE, "fill.py")}" "{cpath}" --next 20')
    return 0


def finish(work, out_docx=None):
    cpath = os.path.join(work, "content.json")
    if not os.path.exists(cpath):
        print("! 找不到 content.json:", cpath)
        return 1
    rc, out = (0, "")
    if os.path.exists(os.path.join(work, "source.json")):
        rc, out = call("fill.py", cpath, "--status")
        print("[0/2] 进度   ", line_with(out, "合计").strip() or "未知")
        if rc != 0:
            print(out.strip())
            print("\n[停] 还有未译项。先 fill.py --next 继续，或确认要跳过再重跑。")
            return 2
    else:
        print("[0/2] 进度   跳过（无 source.json，按已填项直接排版）")

    figs = os.path.join(work, "figs")
    docx = out_docx or os.path.join(work, "译文.docx")
    rc, out = call("build_docx.py", cpath, figs, docx)
    if rc != 0:
        print(out.strip())
        return 3
    print("[1/2] 排版   ", line_with(out, "saved") or docx)

    rc, out = call("verify.py", cpath, figs, docx)
    print("[2/2] 自检")
    print(out.rstrip())
    if rc == 0:
        print(f"\n交付: {os.path.abspath(docx)}")
        print("需要 PDF 时: python run.py pdf <译文.docx>  （有 Word 用 COM，否则用 LibreOffice）")
        aud = os.path.join(work, ".audit.json")
        if not os.path.exists(aud):
            print("\n[提醒] 还没做过语义抽检。交付前建议先跑：")
            print(f'  python "{os.path.join(HERE, "run.py")}" audit "{work}" 3')
            print("  （机械检查抓不出「意思译反/漏掉 not」，这道要人看或模型自查）")
    return rc


def audit(work, n=3, extra=()):
    cpath = os.path.join(work, "content.json")
    if not os.path.exists(cpath):
        print("! 找不到 content.json:", cpath)
        return 1
    rc, out = call("audit.py", cpath, "--n", str(n), *extra)
    print(out.rstrip())
    return rc


def harvest(work, extra=()):
    # 译完用 content.json（借 source.json 取原文）；只抽出结构时也能用 structure.json
    cpath = os.path.join(work, "content.json")
    if not os.path.exists(cpath):
        cpath = os.path.join(work, "structure.json")
    if not os.path.exists(cpath):
        print("! 工作目录里没有 content.json 或 structure.json:", work)
        return 1
    rc, out = call("glossary.py", cpath, "--harvest", *extra)
    print(out.rstrip())
    return rc


def to_pdf(docx):
    rc, out = call("doc2pdf.py", docx)
    print(out.rstrip())
    return rc


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "prepare":
        if len(argv) < 3:
            print("usage: run.py prepare <pdf> <workdir>")
            return 1
        return prepare(argv[1], argv[2])
    if cmd == "finish":
        if len(argv) < 2:
            print("usage: run.py finish <workdir> [out.docx]")
            return 1
        return finish(argv[1], argv[2] if len(argv) > 2 else None)
    if cmd == "pdf":
        if len(argv) < 2:
            print("usage: run.py pdf <docx>")
            return 1
        return to_pdf(argv[1])
    if cmd == "audit":
        if len(argv) < 2:
            print("usage: run.py audit <workdir> [n] [--seq 12,34] [--list]")
            return 1
        rest = argv[2:]
        n = 3
        if rest and rest[0].isdigit():       # 位置参数 n，其余原样透传
            n = int(rest[0])
            rest = rest[1:]
        return audit(argv[1], n, tuple(rest))
    if cmd == "harvest":
        if len(argv) < 2:
            print("usage: run.py harvest <workdir> [--top 15]")
            return 1
        return harvest(argv[1], tuple(argv[2:]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
