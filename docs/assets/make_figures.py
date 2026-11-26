#!/usr/bin/env python3
"""Generate docs/assets/banner.svg and terminal.svg from real results.

    python docs/assets/make_figures.py

Inputs (all produced by tools in this repo, see README "Results"):
  docs/results/perf.json          IPC per program, RTL simulation
  docs/results/orbit-os-uart.txt  UART output of orbit-os booting on the RTL
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE.parent / "results"
ACCENT = "#E879F9"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def banner() -> str:
    perf = json.loads((RES / "perf.json").read_text())["programs"]
    names = [n for n in perf if n != "irqtest"]
    ipcs = [perf[n]["ipc"] for n in names]
    w, h = 1280, 400
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
         '<defs><pattern id="g" width="32" height="32" patternUnits="userSpaceOnUse">'
         '<path d="M32 0H0V32" fill="none" stroke="#161b22" stroke-width="1"/></pattern></defs>',
         f'<rect width="{w}" height="{h}" fill="#0d1117"/><rect width="{w}" height="{h}" fill="url(#g)"/>',
         f'<text x="64" y="170" font-family="{MONO}" font-size="88" font-weight="700" fill="#e6edf3">orbit-rv</text>',
         f'<text x="66" y="222" font-family="{MONO}" font-size="21" fill="#8b949e">5-stage RV64IMA core · Sv39 MMU · caches · IRQs</text>',
         f'<text x="66" y="258" font-family="{MONO}" font-size="21" fill="#8b949e">Amaranth HDL · ISS lockstep · boots orbit-os</text>']
    # motif: IPC per program measured in RTL simulation
    x0, base, top = 830, 330, 70
    bw, gap = 32, 13
    s.append(f'<text x="{x0}" y="{top - 22}" font-family="{MONO}" font-size="15" fill="#8b949e">'
             'IPC per program (RTL simulation)</text>')
    for k, (n, v) in enumerate(zip(names, ipcs)):
        x = x0 + k * (bw + gap)
        bh = (base - top) * v
        s.append(f'<rect x="{x}" y="{base - bh:.1f}" width="{bw}" height="{bh:.1f}" rx="3" '
                 f'fill="{ACCENT}" fill-opacity="{0.55 + 0.45 * v:.2f}"/>')
        s.append(f'<text x="{x + bw / 2}" y="{base - bh - 8:.1f}" text-anchor="middle" '
                 f'font-family="{MONO}" font-size="13" fill="#e6edf3">{v:.2f}</text>')
        s.append(f'<text x="{x + bw / 2}" y="{base + 16}" text-anchor="start" font-family="{MONO}" '
                 f'font-size="11" fill="#8b949e" transform="rotate(35 {x + bw / 2} {base + 16})">{escape(n)}</text>')
    s.append(f'<line x1="{x0 - 8}" y1="{base}" x2="{x0 + len(names) * (bw + gap)}" y2="{base}" '
             'stroke="#30363d" stroke-width="2"/>')
    s.append("</svg>")
    return "\n".join(s) + "\n"


def terminal(max_lines: int = 48) -> str:
    text = (RES / "orbit-os-uart.txt").read_text(encoding="utf-8", errors="replace")
    lines = [ln.rstrip("\r") for ln in text.strip("\n").split("\n")]
    # shorten the 'help' listing (marked as elided); everything else is verbatim
    if "orbit> help" in lines:
        i = lines.index("orbit> help")
        j = next(k for k in range(i, len(lines)) if lines[k].strip().startswith("demos:"))
        if j - i > 4:
            lines = lines[:i + 3] + [f"  [... {j - i - 3} more lines of help elided here ...]"] + lines[j:]
    keep = lines[:max_lines]
    if len(lines) > max_lines:
        keep.append(f"[... {len(lines) - max_lines} more lines in docs/results/orbit-os-uart.txt ...]")
    lh, w = 19, 980
    h = 56 + lh * len(keep) + 20
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
         f'<rect width="{w}" height="{h}" rx="10" fill="#0d1117" stroke="#30363d"/>',
         '<circle cx="22" cy="20" r="6" fill="#ff5f56"/><circle cx="42" cy="20" r="6" fill="#ffbd2e"/>'
         '<circle cx="62" cy="20" r="6" fill="#27c93f"/>',
         f'<text x="{w / 2}" y="25" text-anchor="middle" font-family="{MONO}" font-size="13" fill="#8b949e">'
         'UART · orbit-os on the orbit-rv RTL (CXXRTL simulation, 25 MHz model, 115200 8N1)</text>']
    for k, ln in enumerate(keep):
        color = "#e6edf3"
        if ln.startswith("orbit>"):
            color = ACCENT
        elif ln.startswith("[...") or ln.startswith("  [..."):
            color = "#6e7681"
        elif ln.startswith("["):
            color = "#7ee787"
        elif ln.startswith("OrbitOS"):
            color = "#79c0ff"
        s.append(f'<text x="18" y="{56 + k * lh}" font-family="{MONO}" font-size="13.5" fill="{color}" '
                 f'xml:space="preserve">{escape(ln)}</text>')
    s.append("</svg>")
    return "\n".join(s) + "\n"


if __name__ == "__main__":
    (HERE / "banner.svg").write_text(banner(), encoding="utf-8", newline="\n")
    (HERE / "terminal.svg").write_text(terminal(), encoding="utf-8", newline="\n")
    print("wrote banner.svg, terminal.svg")
