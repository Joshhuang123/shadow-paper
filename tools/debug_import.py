"""复现导入失败：跑一遍清洗+pandoc，把**完整**错误打出来。

用法：.venv/bin/python tools/debug_import.py 2611.32606
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extensions import arxiv_import, latex_clean  # noqa: E402

pid = sys.argv[1] if len(sys.argv) > 1 else "2611.32606"

with tempfile.TemporaryDirectory(prefix="arx_") as td:
    work = Path(td)
    base = arxiv_import.download_source(pid, work)
    print("解包根目录:", base)
    print("文件:", sorted(p.name for p in base.rglob("*.tex")))
    main_tex = arxiv_import.find_main_tex(base)
    print("主文件:", main_tex.name)

    clean = work / "clean.tex"
    latex_clean.clean_tex(main_tex, clean, base)
    s = arxiv_import._neutralize_bibliography(clean.read_text(encoding="utf-8"))
    clean.write_text(s, encoding="utf-8")

    bib = None
    for cand in sorted(base.glob("*.bib")):
        bib = cand
        break
    print("bib:", bib)

    p = subprocess.run(
        ["pandoc", str(clean), "-f", "latex", "-t", "json"]
        + (["--citeproc", f"--bibliography={bib.resolve()}"] if bib else []),
        cwd=base, capture_output=True, text=True, timeout=900)
    print("returncode:", p.returncode)
    if p.returncode != 0:
        print("=== 完整 stderr ===")
        print(p.stderr[:4000])
        # 定位出错行附近的原文
        lines = s.splitlines()
        import re
        m = re.search(r"line (\d+), column (\d+)", p.stderr)
        if m:
            ln = int(m.group(1))
            print(f"=== clean.tex 第 {ln} 行附近 ===")
            for i in range(max(0, ln - 6), min(len(lines), ln + 4)):
                mark = ">>" if i + 1 == ln else "  "
                print(f"{mark} {i+1}: {lines[i][:200]}")
    else:
        ast = json.loads(p.stdout)
        print("blocks:", len(ast.get("blocks", [])))
