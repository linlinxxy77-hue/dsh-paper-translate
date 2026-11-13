# -*- coding: utf-8 -*-
"""env_check.py —— paper-translate 的依赖自检 / 项目内虚拟环境安装。

设计原则（硬性）：
  1. 默认只检查、不安装。缺包时把「缺什么、装到哪、多大」报出来，由调用方先告知用户。
  2. 安装只发生在**项目文件夹内**的虚拟环境里，绝不 pip install 到系统 / 用户级 site-packages。
  3. pip 的缓存与临时文件也重定向到项目文件夹，不写系统临时目录（C 盘）。

用法:
  python env_check.py                     # 只检查，缺包时退出码 2
  python env_check.py --bootstrap         # 创建项目内 venv 并安装缺失包（需用户已确认）
  python env_check.py --root D:/proj      # 指定项目根（默认自动向上查找 .workbuddy/.git）
  python env_check.py --venv D:/proj/.venv-pt   # 自定义虚拟环境位置
  python env_check.py --verbose           # 流式打印 pip 输出

退出码: 0 就绪 / 2 缺依赖（未安装） / 3 安装失败 / 4 Python 不可用
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 8)

# 模块名 → pip 包名（体积为粗略下载量，用于告知用户）
DEPS = [
    ("pymupdf", "pymupdf", "20 MB"),
    ("docx", "python-docx", "1 MB"),
    ("PIL", "pillow", "3 MB"),
]

PROBE = r"""
import importlib, json, sys
out = {"python": sys.version.split()[0], "exe": sys.executable, "mods": {}}
for mod in ("pymupdf", "docx", "PIL"):
    try:
        m = importlib.import_module(mod)
    except Exception:
        out["mods"][mod] = None
        continue
    v = getattr(m, "__version__", None)
    if not v and mod == "pymupdf":
        v = getattr(m, "VersionBind", "")
    if not v and mod == "docx":
        v = getattr(m, "version", "")
    out["mods"][mod] = str(v or "?").strip()
print("PROBE_JSON:" + json.dumps(out))
"""


def probe_local():
    """在当前进程内探测（省掉一次子进程启动，约快 0.3 s）。"""
    import importlib
    out = {"python": sys.version.split()[0], "exe": sys.executable, "mods": {}}
    for mod in ("pymupdf", "docx", "PIL"):
        try:
            m = importlib.import_module(mod)
        except Exception:
            out["mods"][mod] = None
            continue
        v = getattr(m, "__version__", None)
        if not v and mod == "pymupdf":
            v = getattr(m, "VersionBind", "")
        if not v and mod == "docx":
            v = getattr(m, "version", "")
        out["mods"][mod] = str(v or "?").strip()
    return out


def probe(py: str):
    """探测目标解释器的依赖：同一解释器就直接在本进程查，否则起子进程。"""
    try:
        if Path(py).resolve() == Path(sys.executable).resolve():
            return probe_local()
    except Exception:
        pass
    try:
        r = subprocess.run([py, "-c", PROBE], capture_output=True, text=True, timeout=90)
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith("PROBE_JSON:"):
            try:
                return json.loads(line[len("PROBE_JSON:"):])
            except Exception:
                return None
    return None


def detect_root(explicit):
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("PAPER_TRANSLATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for d in [cwd] + list(cwd.parents)[:6]:
        if any((d / m).is_dir() for m in (".workbuddy", ".dsh", ".git")):
            return d
    return cwd


def venv_python(vdir: Path) -> Path:
    return vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def isolated_env(root: Path) -> dict:
    """pip 缓存与临时目录都落在项目内，避免写 C 盘临时目录。"""
    base = root / ".workbuddy" / ".cache"
    tmp = base / "tmp"
    for d in (tmp, base / "pip"):
        d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PIP_CACHE_DIR"] = str(base / "pip")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONNOUSERSITE"] = "1"      # 屏蔽用户级 site-packages
    env.pop("PYTHONPATH", None)        # 屏蔽外部注入
    return env


def on_c_drive(p: Path) -> bool:
    try:
        return str(p.drive).upper().startswith("C")
    except Exception:
        return str(p).upper().startswith("C:")


def report(rows, note=None):
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k.ljust(width)}  {v}")
    if note:
        print(note)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", default=None)
    ap.add_argument("--venv", default=None)
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--index", default=None, help="pip 镜像地址（可选），如清华源")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    root = detect_root(a.root)
    vdir = Path(a.venv).expanduser().resolve() if a.venv else (root / ".workbuddy" / ".venv" / "paper-translate")
    vpy = venv_python(vdir)
    vpy_ok = vpy.exists()

    # 1) 优先已有的项目 venv，其次当前解释器
    info = probe(str(vpy)) if vpy_ok else None
    active = "venv" if info and all(info["mods"].values()) else None
    if not active:
        info2 = probe(sys.executable)
        if info2:
            info, active = info2, ("system" if all(info2["mods"].values()) else None)
    if not info:
        print("FAIL 无法运行 Python 解释器:", sys.executable)
        return 4

    ver = tuple(int(x) for x in info["python"].split(".")[:2] if x.isdigit())
    if ver and ver < MIN_PY:
        print(f"FAIL Python {info['python']} 过低，需要 ≥ {MIN_PY[0]}.{MIN_PY[1]}")
        return 4

    missing = [(mod, pkg, size) for mod, pkg, size in DEPS if not info["mods"].get(mod)]

    # 2) 就绪：压成两行，少占上下文
    if not missing:
        src = "项目虚拟环境" if active == "venv" else "系统解释器"
        print(f"PYTHON  {info['exe']}  ({info['python']}, {src})")
        print("OK      " + " · ".join(f"{pkg} {info['mods'][mod]}" for mod, pkg, _ in DEPS))
        print("RESULT: OK")
        return 0

    # 3) 缺依赖：只报告，绝不擅自安装
    rows = [("MISSING", ", ".join(p for _, p, _ in missing)),
            ("NEED", " ".join(p for _, p, _ in missing)),
            ("VENV_TARGET", str(vdir) + ("" if not vpy_ok else "  (已存在，但依赖不全)")),
            ("SOURCE", "当前解释器 " + info["exe"] + f" (Python {info['python']})")]
    rows.append(("DOWNLOAD", "约 " + "，".join(z for _, _, z in missing)))
    report(rows)
    if on_c_drive(vdir):
        print("WARN 虚拟环境位于 C 盘；建议把项目放在非系统盘，或改 --venv 到项目目录内。")

    if not a.bootstrap:
        print("")
        print("下一步（勿跳过）：先把上面的缺包清单告知用户，说明将创建项目内虚拟环境并安装，")
        print("取得同意后再执行：")
        print(f"  {sys.executable} {Path(__file__).as_posix()} --bootstrap")
        print("RESULT: NEED_INSTALL")
        return 2

    # 4) 用户已确认 → 在项目内建 venv 并安装
    env = isolated_env(root)
    if not vpy_ok:
        print(f"STEP 创建虚拟环境 {vdir}")
        r = subprocess.run([sys.executable, "-m", "venv", str(vdir)],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0 or not vpy.exists():
            print("FAIL 虚拟环境创建失败")
            print((r.stderr or r.stdout or "").strip()[-800:])
            return 3

    pkgs = [p for _, p, _ in missing]
    cmd = [str(vpy), "-m", "pip", "install", "--no-input", "--disable-pip-version-check"] + pkgs
    if a.index:
        cmd += ["-i", a.index]
    print(f"STEP pip install {' '.join(pkgs)}")
    if a.verbose:
        r = subprocess.run(cmd, env=env)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            print((r.stdout or "")[-600:])
            print((r.stderr or "")[-800:])
    if r.returncode != 0:
        print("FAIL 安装失败（检查网络或镜像：可加 --index 或设置 pip 全局镜像后重试）")
        return 3

    # 5) 复检
    info = probe(str(vpy))
    mods_now = (info or {}).get("mods", {})
    still = [p for m, p, _ in DEPS if not mods_now.get(m)]
    if still:
        print("FAIL 安装后仍缺:", ", ".join(still))
        return 3
    print(f"PYTHON  {vpy}  ({info['python']}, 项目虚拟环境)")
    print("OK      " + " · ".join(f"{pkg} {info['mods'][mod]}" for mod, pkg, _ in DEPS))
    print("RESULT: OK")
    print("后续所有脚本请用该解释器运行：")
    print(f'  PY="{vpy}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
