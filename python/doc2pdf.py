# -*- coding: utf-8 -*-
"""doc2pdf.py —— DOCX → PDF 的跨平台兜底。

用法: python doc2pdf.py <in.docx> [out.pdf]

顺序：LibreOffice（命令行，最稳）→ Microsoft Word（COM，仅 Windows 且装了 pywin32）
→ 都没有则明确告知，不静默失败。PDF 是可选的终检产物，失败不影响 DOCX 交付。
"""
import os
import shutil
import subprocess
import sys

SOFFICE = [
    shutil.which("soffice"),
    shutil.which("libreoffice"),
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
]


def find_soffice():
    for p in SOFFICE:
        if p and os.path.exists(p):
            return p
    return None


def via_soffice(exe, src, dst):
    outdir = os.path.dirname(os.path.abspath(dst)) or "."
    os.makedirs(outdir, exist_ok=True)
    cmd = [exe, "--headless", "--norestore", "--convert-to", "pdf",
           "--outdir", outdir, src]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    made = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if os.path.exists(made) and os.path.getsize(made) > 2000:
        if os.path.abspath(made) != os.path.abspath(dst):
            shutil.move(made, dst)
        return True, f"LibreOffice: {exe}"
    return False, (r.stdout or "") + (r.stderr or "")


def via_word_ps(src, dst):
    """用 PowerShell 调 Word COM —— 不需要 pywin32，Windows 自带。

    路径通过环境变量传入，命令串本身保持纯 ASCII，
    避免中文路径在 -Command 里因代码页问题被破坏。
    """
    import platform
    if platform.system() != "Windows":
        return False, "非 Windows"
    ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
          "$ErrorActionPreference='Stop';"
          "$w=New-Object -ComObject Word.Application;$w.Visible=$false;"
          "$d=$w.Documents.Open($env:PT_SRC,$false,$true);"
          "$d.SaveAs([ref]$env:PT_DST,[ref]17);$d.Close(0);$w.Quit()"]
    env = dict(os.environ, PT_SRC=os.path.abspath(src), PT_DST=os.path.abspath(dst))
    try:
        r = subprocess.run(ps, capture_output=True, text=True, timeout=240,
                           encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return False, f"PowerShell 调用失败: {e}"
    if os.path.exists(dst) and os.path.getsize(dst) > 2000:
        return True, "Microsoft Word (PowerShell COM)"
    return False, (r.stdout or r.stderr or "")[:200]


def via_word(src, dst):
    try:
        import win32com.client as win32
    except Exception:
        return False, "未安装 pywin32"
    try:
        word = win32.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(os.path.abspath(src), ReadOnly=True)
        doc.ExportAsFixedFormat(os.path.abspath(dst), 17)   # 17 = wdExportFormatPDF
        doc.Close(0)
        word.Quit()
        return os.path.exists(dst), "Microsoft Word (COM)"
    except Exception as e:
        return False, f"Word COM 失败: {e}"


def main():
    if len(sys.argv) < 2:
        print("usage: doc2pdf.py <in.docx> [out.pdf]")
        return 1
    src = sys.argv[1]
    if not os.path.exists(src):
        print("! 找不到", src)
        return 1
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".pdf"

    exe = find_soffice()
    if exe:
        ok, msg = via_soffice(exe, src, dst)
        if ok:
            print(f"PDF: {dst}\n  经由 {msg}")
            return 0
        print("! LibreOffice 转换失败：", msg.strip()[:300])
    for attempt in (via_word_ps, via_word):
        ok, msg = attempt(src, dst)
        if ok:
            print(f"PDF: {dst}\n  经由 {msg}")
            return 0
    print("! 无法导出 PDF：", msg)
    print("  LibreOffice 与 Word 都不可用。DOCX 本身不受影响，可直接交付；")
    print("  需要 PDF 时请安装 LibreOffice，或手工用 Word「另存为 PDF」。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
