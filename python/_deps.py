# -*- coding: utf-8 -*-
"""_deps.py —— 第三方依赖缺失时给出可执行的修复提示，而不是抛 ImportError 栈。

用法（必须在 import pymupdf / docx / PIL 之前调用）:
    import _deps; _deps.ensure()
    import pymupdf
"""
import sys

PKG = {"pymupdf": "pymupdf", "docx": "python-docx", "PIL": "pillow"}
DEFAULT = ("pymupdf", "docx", "PIL")


def ensure(mods=DEFAULT):
    missing = []
    for m in mods:
        try:
            __import__(m)
        except Exception:
            missing.append(m)
    if not missing:
        return
    pkgs = [PKG.get(m, m) for m in missing]
    sys.stderr.write(
        "\n[paper-translate] 缺少依赖: " + ", ".join(pkgs) + "\n"
        "不要直接 pip install 到系统环境。请先执行环境自检：\n"
        "  python scripts/env_check.py\n"
        "它会列出缺失的包；获得用户同意后加 --bootstrap，在**项目文件夹内**建立\n"
        "虚拟环境并安装（不写系统目录、不写 C 盘临时目录）。\n"
        "安装完成后用报告中给出的 PYTHON 路径重新运行本脚本。\n\n"
    )
    sys.exit(2)
