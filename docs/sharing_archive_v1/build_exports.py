#!/usr/bin/env python3
"""Build publication-grade PDF and editable DOCX from the UTF-8 Markdown whitepaper."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
MD_PATH = BASE_DIR / "NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.md"
PDF_PATH = BASE_DIR / "NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.pdf"
DOCX_PATH = BASE_DIR / "NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.docx"
CHROME_EXE = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

ARCH_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 920 490" width="100%" style="font-family:'Microsoft YaHei','Segoe UI',sans-serif;background:#faf8f3;border:1px solid #dfd8ca;border-radius:12px;">
  <defs>
    <marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#52655d"/>
    </marker>
    <filter id="sh" x="-4%" y="-4%" width="108%" height="112%">
      <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#000" flood-opacity="0.06"/>
    </filter>
  </defs>

  <text x="460" y="26" text-anchor="middle" font-size="13" font-weight="700" fill="#1e2a25">NotebookLM-Fleet 三通道融合与本地编排全景架构</text>

  <!-- Row 1: Clients -->
  <rect x="24" y="42" width="276" height="68" rx="10" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh)"/>
  <text x="162" y="67" text-anchor="middle" font-size="12.5" font-weight="700" fill="#1e2a25">Web 控制台 + 全工作区侧栏</text>
  <text x="162" y="88" text-anchor="middle" font-size="10.5" fill="#5a655e">127.0.0.1:8765 · 选材篮 · 额度仪表盘</text>

  <rect x="322" y="42" width="276" height="68" rx="10" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh)"/>
  <text x="460" y="67" text-anchor="middle" font-size="12.5" font-weight="700" fill="#1e2a25">AI 编程软件 (Skills + MCP)</text>
  <text x="460" y="88" text-anchor="middle" font-size="10.5" fill="#5a655e">Antigravity / Claude Code / Cursor / Codex</text>

  <rect x="620" y="42" width="276" height="68" rx="10" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh)"/>
  <text x="758" y="67" text-anchor="middle" font-size="12.5" font-weight="700" fill="#1e2a25">独立自研应用 &amp; CI 流水线</text>
  <text x="758" y="88" text-anchor="middle" font-size="10.5" fill="#5a655e">Sidecar REST 调用 · Git 自动化触发</text>

  <line x1="162" y1="110" x2="260" y2="142" stroke="#52655d" stroke-width="1.6" marker-end="url(#arr)"/>
  <line x1="460" y1="110" x2="460" y2="142" stroke="#52655d" stroke-width="1.6" marker-end="url(#arr)"/>
  <line x1="758" y1="110" x2="660" y2="142" stroke="#52655d" stroke-width="1.6" marker-end="url(#arr)"/>

  <!-- Row 2: Control Plane -->
  <rect x="24" y="146" width="872" height="114" rx="12" fill="#eef6f4" stroke="#136f63" stroke-width="1.8" filter="url(#sh)"/>
  <text x="460" y="170" text-anchor="middle" font-size="13" font-weight="700" fill="#0f574e">Fleet 智能编排与统一网关平面 (FastAPI 47 HTTP 操作 + 本机 Stdio MCP 10 工具)</text>

  <rect x="42" y="184" width="198" height="62" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.2"/>
  <text x="141" y="207" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">Capability Router</text>
  <text x="141" y="226" text-anchor="middle" font-size="10" fill="#465952">能走官方优先走官方</text>

  <rect x="256" y="184" width="198" height="62" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.2"/>
  <text x="355" y="207" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">Quota Poller 探针</text>
  <text x="355" y="226" text-anchor="middle" font-size="10" fill="#465952">5h / 周双窗 + 29 项动作表</text>

  <rect x="470" y="184" width="198" height="62" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.2"/>
  <text x="569" y="207" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">Tier-Aware Scheduler</text>
  <text x="569" y="226" text-anchor="middle" font-size="10" fill="#465952">分级权重 · later · waiting</text>

  <rect x="684" y="184" width="194" height="62" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.2"/>
  <text x="781" y="207" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">Coupling &amp; Security</text>
  <text x="781" y="226" text-anchor="middle" font-size="10" fill="#465952">跨号共享镜像 · 敏感拦截</text>

  <line x1="162" y1="260" x2="162" y2="294" stroke="#136f63" stroke-width="1.7" marker-end="url(#arr)"/>
  <line x1="460" y1="260" x2="460" y2="294" stroke="#b76e1e" stroke-width="1.7" marker-end="url(#arr)"/>
  <line x1="758" y1="260" x2="758" y2="294" stroke="#435663" stroke-width="1.7" marker-end="url(#arr)"/>

  <!-- Row 3: 3 Channels -->
  <rect x="24" y="298" width="276" height="92" rx="10" fill="#f3fbf9" stroke="#136f63" stroke-width="1.6" filter="url(#sh)"/>
  <text x="162" y="322" text-anchor="middle" font-size="12" font-weight="700" fill="#0f574e">Channel 1 · Google 官方 API</text>
  <text x="162" y="344" text-anchor="middle" font-size="10.5" fill="#2d443d">REST v1 + Upload v1 (&lt;=200MiB)</text>
  <text x="162" y="362" text-anchor="middle" font-size="10.5" fill="#2d443d">Remote MCP 7 工具 (Streamable HTTP)</text>
  <text x="162" y="379" text-anchor="middle" font-size="9.8" fill="#5a655e">OAuth 2.0 重试鉴权 · 零风控建库灌料</text>

  <rect x="322" y="298" width="276" height="92" rx="10" fill="#fdf8ef" stroke="#c47d2b" stroke-width="1.6" filter="url(#sh)"/>
  <text x="460" y="322" text-anchor="middle" font-size="12" font-weight="700" fill="#9a5c14">Channel 2 · 增强协议通道</text>
  <text x="460" y="344" text-anchor="middle" font-size="10.5" fill="#4f3d21">notebooklm-py (>=0.8.6) 异步池</text>
  <text x="460" y="362" text-anchor="middle" font-size="10.5" fill="#4f3d21">11 类 Studio · Chat 问答 · 测额</text>
  <text x="460" y="379" text-anchor="middle" font-size="9.8" fill="#745f3e">独立 Profile 锁 · 适配 ?authuser=N</text>

  <rect x="620" y="298" width="276" height="92" rx="10" fill="#f2f5f7" stroke="#435663" stroke-width="1.6" filter="url(#sh)"/>
  <text x="758" y="322" text-anchor="middle" font-size="12" font-weight="700" fill="#2d3e4a">Channel 3 · 隔离浏览器接管</text>
  <text x="758" y="344" text-anchor="middle" font-size="10.5" fill="#3b4a54">专属 Chrome CDP (:9222+)</text>
  <text x="758" y="362" text-anchor="middle" font-size="10.5" fill="#3b4a54">Storage.getCookies 无锁采集</text>
  <text x="758" y="379" text-anchor="middle" font-size="9.8" fill="#5c6c77">免关日常 Chrome · 兼容 cURL 导入</text>

  <line x1="162" y1="390" x2="260" y2="416" stroke="#52655d" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="460" y1="390" x2="460" y2="416" stroke="#52655d" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="758" y1="390" x2="660" y2="416" stroke="#52655d" stroke-width="1.5" marker-end="url(#arr)"/>

  <!-- Row 4: Persistence -->
  <rect x="24" y="420" width="424" height="54" rx="10" fill="#ffffff" stroke="#b5afa1" stroke-width="1.4"/>
  <text x="236" y="443" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">SQLite WAL 状态机与断点恢复 (7 张核心表)</text>
  <text x="236" y="461" text-anchor="middle" font-size="10" fill="#5a655e">accounts / quota_snapshots / topics / notebooks / tasks / artifacts / app_settings</text>

  <rect x="472" y="420" width="424" height="54" rx="10" fill="#ffffff" stroke="#b5afa1" stroke-width="1.4"/>
  <text x="684" y="443" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">Google Drive Hub 标准化资产归档库</text>
  <text x="684" y="461" text-anchor="middle" font-size="10" fill="#5a655e">00_Control (每日热备) / 01_Inbox / 02_Topics / 03_Exports</text>
</svg>
"""

SKILLS_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 920 350" width="100%" style="font-family:'Microsoft YaHei','Segoe UI',sans-serif;background:#faf8f3;border:1px solid #dfd8ca;border-radius:12px;">
  <defs>
    <marker id="arr2" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#136f63"/>
    </marker>
    <filter id="sh2" x="-4%" y="-4%" width="108%" height="112%">
      <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#000" flood-opacity="0.06"/>
    </filter>
  </defs>

  <text x="460" y="26" text-anchor="middle" font-size="13" font-weight="700" fill="#1e2a25">原子化 Skills 生态与跨 AI 编程工具连接拓扑</text>

  <!-- Left: AI Coding Tools -->
  <text x="116" y="54" text-anchor="middle" font-size="11.5" font-weight="700" fill="#465952">AI 编程软件 / 自研工具</text>
  <rect x="24" y="66" width="184" height="52" rx="8" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="116" y="89" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">Antigravity / Codex</text>
  <text x="116" y="106" text-anchor="middle" font-size="10" fill="#5a655e">.agents/skills/ + Stdio MCP</text>

  <rect x="24" y="130" width="184" height="52" rx="8" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="116" y="153" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">Claude Code</text>
  <text x="116" y="170" text-anchor="middle" font-size="10" fill="#5a655e">~/.claude/skills/ + .mcp.json</text>

  <rect x="24" y="194" width="184" height="52" rx="8" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="116" y="217" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">Cursor / Windsurf</text>
  <text x="116" y="234" text-anchor="middle" font-size="10" fill="#5a655e">Rules/Skills + MCP Settings</text>

  <rect x="24" y="258" width="184" height="52" rx="8" fill="#ffffff" stroke="#c8c2b4" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="116" y="281" text-anchor="middle" font-size="12" font-weight="700" fill="#1e2a25">外部独立软件 / CI</text>
  <text x="116" y="298" text-anchor="middle" font-size="10" fill="#5a655e">REST API (http://127.0.0.1:8765)</text>

  <!-- Center: 6 Atomic Skills -->
  <text x="460" y="54" text-anchor="middle" font-size="11.5" font-weight="700" fill="#0f574e">6 大单职责原子 Skill (atomic_skills/nlm-*)</text>
  <rect x="260" y="64" width="400" height="252" rx="12" fill="#eef6f4" stroke="#136f63" stroke-width="1.6"/>

  <rect x="276" y="80" width="178" height="68" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.3"/>
  <text x="365" y="103" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">nlm-quota-guard</text>
  <text x="365" y="121" text-anchor="middle" font-size="10" fill="#465952">纯只读 · 双窗额度巡检</text>
  <text x="365" y="136" text-anchor="middle" font-size="9.5" fill="#687872">防超额调度与成本估算</text>

  <rect x="466" y="80" width="178" height="68" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.3"/>
  <text x="555" y="103" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">nlm-official-ingest</text>
  <text x="555" y="121" text-anchor="middle" font-size="10" fill="#465952">官方 API 建本与批量灌料</text>
  <text x="555" y="136" text-anchor="middle" font-size="9.5" fill="#687872">URL / MD / &lt;=200MiB 二进制</text>

  <rect x="276" y="158" width="178" height="68" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.3"/>
  <text x="365" y="181" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">nlm-grounded-qa</text>
  <text x="365" y="199" text-anchor="middle" font-size="10" fill="#465952">可溯源问答与 Note 存档</text>
  <text x="365" y="214" text-anchor="middle" font-size="9.5" fill="#687872">带引用回答 + 导出 .md</text>

  <rect x="466" y="158" width="178" height="68" rx="8" fill="#fdf8ef" stroke="#d19a4c" stroke-width="1.3"/>
  <text x="555" y="181" text-anchor="middle" font-size="11.5" font-weight="700" fill="#9a5c14">nlm-studio-factory</text>
  <text x="555" y="199" text-anchor="middle" font-size="10" fill="#5e4720">11 类多模态内容生产</text>
  <text x="555" y="214" text-anchor="middle" font-size="9.5" fill="#7e663f">音频 / PPT / 导图 / 视频</text>

  <rect x="276" y="236" width="178" height="68" rx="8" fill="#ffffff" stroke="#88bdb4" stroke-width="1.3"/>
  <text x="365" y="259" text-anchor="middle" font-size="11.5" font-weight="700" fill="#136f63">nlm-repo-to-notebook</text>
  <text x="365" y="277" text-anchor="middle" font-size="10" fill="#465952">代码库正交切片 &amp; 脱敏</text>
  <text x="365" y="292" text-anchor="middle" font-size="9.5" fill="#687872">硬拦截密钥 · 一键知识化</text>

  <rect x="466" y="236" width="178" height="68" rx="8" fill="#f2f5f7" stroke="#6f828f" stroke-width="1.3"/>
  <text x="555" y="259" text-anchor="middle" font-size="11.5" font-weight="700" fill="#2d3e4a">nlm-account-ops</text>
  <text x="555" y="277" text-anchor="middle" font-size="10" fill="#3b4a54">免密双通道认证自愈</text>
  <text x="555" y="292" text-anchor="middle" font-size="9.5" fill="#5c6c77">302 OAuth / CDP / cURL</text>

  <!-- Right: Protocols & Deliverables -->
  <text x="804" y="54" text-anchor="middle" font-size="11.5" font-weight="700" fill="#465952">底层接口与标准交付物</text>
  <rect x="712" y="66" width="184" height="58" rx="8" fill="#ffffff" stroke="#136f63" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="804" y="91" text-anchor="middle" font-size="11.5" font-weight="700" fill="#0f574e">本机 Stdio MCP</text>
  <text x="804" y="109" text-anchor="middle" font-size="10" fill="#465952">10 编排工具 · fleet_*</text>

  <rect x="712" y="134" width="184" height="58" rx="8" fill="#ffffff" stroke="#136f63" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="804" y="159" text-anchor="middle" font-size="11.5" font-weight="700" fill="#0f574e">官方 REST + Remote MCP</text>
  <text x="804" y="177" text-anchor="middle" font-size="10" fill="#465952">v1 / Upload v1 / 7 MCP 工具</text>

  <rect x="712" y="202" width="184" height="108" rx="8" fill="#fdf8ef" stroke="#c47d2b" stroke-width="1.4" filter="url(#sh2)"/>
  <text x="804" y="228" text-anchor="middle" font-size="11.5" font-weight="700" fill="#9a5c14">自动归档交付产物</text>
  <text x="804" y="249" text-anchor="middle" font-size="10" fill="#4f3d21">· 云端 ACTIVE 笔记本</text>
  <text x="804" y="267" text-anchor="middle" font-size="10" fill="#4f3d21">· 播客 .m4a / PPT .pdf</text>
  <text x="804" y="285" text-anchor="middle" font-size="10" fill="#4f3d21">· 报告 .md / 归档 .pdf/.docx</text>

  <!-- Arrows -->
  <line x1="208" y1="92" x2="256" y2="114" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>
  <line x1="208" y1="156" x2="256" y2="156" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>
  <line x1="208" y1="220" x2="256" y2="210" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>
  <line x1="208" y1="284" x2="256" y2="266" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>

  <line x1="660" y1="112" x2="708" y2="95" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>
  <line x1="660" y1="168" x2="708" y2="163" stroke="#136f63" stroke-width="1.5" marker-end="url(#arr2)"/>
  <line x1="660" y1="252" x2="708" y2="252" stroke="#c47d2b" stroke-width="1.5" marker-end="url(#arr2)"/>
</svg>
"""


def _render_svgs_to_pngs(tmp_dir: Path) -> tuple[Path, Path]:
    """Use .venv Playwright + Chrome headless to rasterize the 2 SVG diagrams to crisp 2x PNGs."""
    venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    py_exe = str(venv_py) if venv_py.exists() else sys.executable

    arch_svg_file = tmp_dir / "arch.svg"
    skills_svg_file = tmp_dir / "skills.svg"
    arch_png = tmp_dir / "arch.png"
    skills_png = tmp_dir / "skills.png"
    arch_svg_file.write_text(ARCH_SVG, encoding="utf-8")
    skills_svg_file.write_text(SKILLS_SVG, encoding="utf-8")

    worker = f"""
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=r"{CHROME_EXE}", headless=True)
    page = browser.new_page(device_scale_factor=2)
    for svg_p, png_p in [(r"{arch_svg_file}", r"{arch_png}"), (r"{skills_svg_file}", r"{skills_png}")]:
        svg_txt = Path(svg_p).read_text(encoding="utf-8")
        page.set_content(f"<!doctype html><html><body style='margin:0;padding:8px;background:#ffffff'><div id='wrap' style='width:920px'>{{svg_txt}}</div></body></html>")
        page.locator("#wrap").screenshot(path=png_p)
    browser.close()
"""
    subprocess.run([py_exe, "-c", worker], check=True)
    return arch_png, skills_png


def _build_pdf(md_text: str, tmp_dir: Path, pdf_path: Path = PDF_PATH) -> None:
    import markdown  # type: ignore

    body_md = re.sub(
        r"<!-- DIAGRAM:ARCH_START -->.*?<!-- DIAGRAM:ARCH_END -->",
        "[[DIAGRAM_ARCH]]",
        md_text,
        flags=re.DOTALL,
    )
    body_md = re.sub(
        r"<!-- DIAGRAM:SKILLS_START -->.*?<!-- DIAGRAM:SKILLS_END -->",
        "[[DIAGRAM_SKILLS]]",
        body_md,
        flags=re.DOTALL,
    )
    # Strip the top H1 title because we render a dedicated editorial cover block.
    lines = body_md.splitlines()
    title = "NotebookLM-Fleet 系统能力全景、开放架构与原子化扩展白皮书（v1.0）"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    body_md_clean = "\n".join(lines).lstrip()

    html_body = markdown.markdown(
        body_md_clean,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
    )
    html_body = html_body.replace(
        "<p>[[DIAGRAM_ARCH]]</p>",
        f'<figure class="diagram-box">{ARCH_SVG}<figcaption>图 2-1｜NotebookLM-Fleet 三通道融合、智能调度与持久化全景架构图</figcaption></figure>',
    )
    html_body = html_body.replace(
        "<p>[[DIAGRAM_SKILLS]]</p>",
        f'<figure class="diagram-box">{SKILLS_SVG}<figcaption>图 5-1｜NotebookLM 原子化 Skills 生态与跨 AI 编程工具接入拓扑图</figcaption></figure>',
    )

    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{html.escape(title)}</title>
<style>
  @page {{
    size: A4;
    margin: 20mm 16mm 20mm 16mm;
  }}
  @page :first {{
    margin-top: 14mm;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", sans-serif;
    font-size: 10.2pt;
    line-height: 1.68;
    color: #202623;
    background: #ffffff;
    margin: 0;
  }}
  .cover-card {{
    background: linear-gradient(135deg, #f5f2ea 0%, #eef6f4 100%);
    border: 1px solid #d4cfc2;
    border-top: 5px solid #136f63;
    border-radius: 12px;
    padding: 24px 26px 20px 26px;
    margin-bottom: 18px;
    break-inside: avoid;
  }}
  .cover-kicker {{
    font-size: 9pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: #136f63;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .cover-title {{
    font-size: 20pt;
    line-height: 1.32;
    font-weight: 800;
    color: #17211d;
    margin: 0 0 10px 0;
  }}
  .cover-sub {{
    font-size: 10.2pt;
    color: #495650;
    margin: 0 0 14px 0;
  }}
  .cover-meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .badge {{
    display: inline-block;
    font-size: 8.6pt;
    padding: 3px 9px;
    border-radius: 999px;
    background: #ffffff;
    border: 1px solid #c9d6d1;
    color: #136f63;
    font-weight: 600;
  }}
  h2 {{
    font-size: 13.8pt;
    font-weight: 800;
    color: #0f574e;
    border-bottom: 2px solid #dfeae7;
    padding-bottom: 5px;
    margin-top: 22px;
    margin-bottom: 10px;
    break-after: avoid;
  }}
  h3 {{
    font-size: 11.4pt;
    font-weight: 700;
    color: #1f2b27;
    margin-top: 15px;
    margin-bottom: 6px;
    break-after: avoid;
  }}
  h4 {{
    font-size: 10.4pt;
    font-weight: 700;
    color: #9a5c14;
    margin-top: 12px;
    margin-bottom: 4px;
    break-after: avoid;
  }}
  p {{
    margin: 0 0 8px 0;
    text-align: justify;
  }}
  ul, ol {{
    margin: 4px 0 10px 0;
    padding-left: 20px;
  }}
  li {{
    margin-bottom: 4px;
  }}
  blockquote {{
    margin: 10px 0 12px 0;
    padding: 10px 14px;
    background: #f0f7f5;
    border-left: 4px solid #136f63;
    border-radius: 0 8px 8px 0;
    color: #213731;
    break-inside: avoid;
  }}
  blockquote p:last-child {{ margin-bottom: 0; }}
  code {{
    font-family: "Consolas", "Cascadia Code", monospace;
    font-size: 9pt;
    background: #f3efe6;
    color: #135a51;
    padding: 1px 4px;
    border-radius: 4px;
  }}
  pre {{
    font-family: "Consolas", "Cascadia Code", monospace;
    font-size: 8.5pt;
    line-height: 1.5;
    background: #f6f3ec;
    border: 1px solid #dfd8ca;
    border-radius: 8px;
    padding: 10px 12px;
    overflow-x: hidden;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 8px 0 12px 0;
    break-inside: avoid;
  }}
  pre code {{
    background: transparent;
    color: #1f2622;
    padding: 0;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 14px 0;
    font-size: 9pt;
    line-height: 1.5;
    table-layout: auto;
  }}
  thead {{ display: table-header-group; }}
  tr {{ break-inside: avoid; }}
  th {{
    background: #136f63;
    color: #ffffff;
    font-weight: 700;
    text-align: left;
    padding: 6px 8px;
    border: 1px solid #115e54;
  }}
  td {{
    padding: 6px 8px;
    border: 1px solid #dcd6c9;
    vertical-align: top;
    word-break: break-word;
  }}
  tbody tr:nth-child(even) {{
    background: #faf8f3;
  }}
  hr {{
    border: none;
    border-top: 1px dashed #d5cfc2;
    margin: 14px 0;
  }}
  .diagram-box {{
    margin: 12px 0 16px 0;
    text-align: center;
    break-inside: avoid;
  }}
  .diagram-box figcaption {{
    font-size: 8.8pt;
    color: #5c6862;
    margin-top: 5px;
    font-weight: 600;
  }}
  a {{
    color: #136f63;
    text-decoration: none;
  }}
</style>
</head>
<body>
  <section class="cover-card">
    <div class="cover-kicker">NotebookLM-Fleet Technical Whitepaper &amp; Team Archive</div>
    <h1 class="cover-title">{html.escape(title)}</h1>
    <p class="cover-sub">面向新人科普、独立软件架构复用与跨 AI 编程工具原子化集成的完整能力全景手册</p>
    <div class="cover-meta">
      <span class="badge">版本 v1.0</span>
      <span class="badge">归档日期 2026-11-24</span>
      <span class="badge">编码 UTF-8</span>
      <span class="badge">三通道架构 Ch1 + Ch2 + Ch3</span>
      <span class="badge">含 6 大 Atomic Skills</span>
      <span class="badge">已通过对外共享脱敏校验</span>
    </div>
  </section>
  {html_body}
</body>
</html>
"""
    html_file = tmp_dir / "whitepaper.html"
    html_file.write_text(full_html, encoding="utf-8")

    venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    py_exe = str(venv_py) if venv_py.exists() else sys.executable

    header_tpl = (
        '<div style="width:100%;font-size:8px;font-family:\'Microsoft YaHei\',sans-serif;'
        'color:#707c75;padding:0 16mm;display:flex;justify-content:space-between;">'
        '<span>NotebookLM-Fleet 系统能力全景与开放架构白皮书</span>'
        '<span>v1.0 · Team &amp; Public Sharing Edition</span></div>'
    )
    footer_tpl = (
        '<div style="width:100%;font-size:8px;font-family:\'Microsoft YaHei\',sans-serif;'
        'color:#707c75;padding:0 16mm;display:flex;justify-content:space-between;">'
        '<span>UTF-8 归档版本 · 已脱敏</span>'
        '<span>第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</span></div>'
    )

    pdf_worker = f"""
from pathlib import Path
from playwright.sync_api import sync_playwright

html_uri = Path(r"{html_file}").resolve().as_uri()
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=r"{CHROME_EXE}", headless=True)
    page = browser.new_page()
    page.goto(html_uri, wait_until="networkidle")
    page.pdf(
        path=r"{pdf_path}",
        format="A4",
        print_background=True,
        display_header_footer=True,
        header_template={header_tpl!r},
        footer_template={footer_tpl!r},
        margin={{"top": "20mm", "bottom": "20mm", "left": "16mm", "right": "16mm"}},
    )
    browser.close()
"""
    subprocess.run([py_exe, "-c", pdf_worker], check=True)


def _set_run_font(run, font_name: str = "Microsoft YaHei", *, size_pt: float | None = None, bold: bool | None = None, italic: bool | None = None, color_hex: str | None = None) -> None:
    from docx.oxml.ns import qn  # type: ignore
    from docx.shared import Pt, RGBColor  # type: ignore

    run.font.name = font_name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), font_name)
    rfonts.set(qn("w:ascii"), font_name)
    rfonts.set(qn("w:hAnsi"), font_name)
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color_hex:
        run.font.color.rgb = RGBColor.from_string(color_hex)


def _shade_elm(elm, fill_hex: str) -> None:
    from docx.oxml import OxmlElement  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    elm.append(shd)


def _add_inline_runs(para, text: str, *, base_size: float = 10.5, base_color: str = "202623") -> None:
    pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*)")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            r = para.add_run(text[pos : m.start()])
            _set_run_font(r, size_pt=base_size, color_hex=base_color)
        tok = m.group(0)
        if tok.startswith("**") and tok.endswith("**"):
            inner = tok[2:-2]
            if inner.startswith("`") and inner.endswith("`"):
                r = para.add_run(inner[1:-1])
                _set_run_font(r, "Consolas", size_pt=base_size - 0.5, bold=True, color_hex="135A51")
            else:
                r = para.add_run(inner)
                _set_run_font(r, size_pt=base_size, bold=True, color_hex="17211D")
        elif tok.startswith("`") and tok.endswith("`"):
            r = para.add_run(tok[1:-1])
            _set_run_font(r, "Consolas", size_pt=base_size - 0.8, color_hex="135A51")
        elif tok.startswith("[") and "](" in tok:
            lbl = tok[1 : tok.index("](")]
            r = para.add_run(lbl)
            _set_run_font(r, size_pt=base_size, color_hex="136F63")
        elif tok.startswith("*") and tok.endswith("*"):
            r = para.add_run(tok[1:-1])
            _set_run_font(r, size_pt=base_size, italic=True, color_hex=base_color)
        pos = m.end()
    if pos < len(text):
        r = para.add_run(text[pos:])
        _set_run_font(r, size_pt=base_size, color_hex=base_color)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _set_table_borders(table) -> None:
    from docx.oxml import OxmlElement  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{side}")
        b.set(qn("w:val"), "single")
        b.set(qn("w:sz"), "4")
        b.set(qn("w:space"), "0")
        b.set(qn("w:color"), "D5CFC2")
        borders.append(b)
    tbl_pr.append(borders)


def _set_cell_margins(cell, top: int = 80, bottom: int = 80, left: int = 110, right: int = 110) -> None:
    from docx.oxml import OxmlElement  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        tc_mar.append(node)
    tc_pr.append(tc_mar)


def _build_docx(md_text: str, arch_png: Path, skills_png: Path, docx_path: Path = DOCX_PATH) -> None:
    from docx import Document  # type: ignore
    from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
    from docx.shared import Inches, Mm, Pt  # type: ignore

    clean_md = re.sub(
        r"<!-- DIAGRAM:ARCH_START -->.*?<!-- DIAGRAM:ARCH_END -->",
        "[[IMG_ARCH]]",
        md_text,
        flags=re.DOTALL,
    )
    clean_md = re.sub(
        r"<!-- DIAGRAM:SKILLS_START -->.*?<!-- DIAGRAM:SKILLS_END -->",
        "[[IMG_SKILLS]]",
        clean_md,
        flags=re.DOTALL,
    )

    doc = Document()
    for sec in doc.sections:
        sec.page_width = Mm(210)
        sec.page_height = Mm(297)
        sec.top_margin = Mm(20)
        sec.bottom_margin = Mm(20)
        sec.left_margin = Mm(18)
        sec.right_margin = Mm(18)
        hdr = sec.header.paragraphs[0]
        hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        hr = hdr.add_run("NotebookLM-Fleet 系统能力全景与开放架构白皮书 · v1.0")
        _set_run_font(hr, size_pt=8.5, color_hex="707C75")

    normal_style = doc.styles["Normal"]
    _set_run_font(normal_style, size_pt=10.5, color_hex="202623")
    normal_style.paragraph_format.line_spacing = 1.45
    normal_style.paragraph_format.space_after = Pt(5)

    lines = clean_md.splitlines()
    i = 0
    in_code = False
    code_lines: list[str] = []

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            if not in_code:
                in_code = True
                code_lines = []
            else:
                in_code = False
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.15)
                p.paragraph_format.right_indent = Inches(0.15)
                p.paragraph_format.space_before = Pt(4)
                p.paragraph_format.space_after = Pt(8)
                p.paragraph_format.line_spacing = 1.25
                _shade_elm(p._p.get_or_add_pPr(), "F4F1EA")
                r = p.add_run("\n".join(code_lines))
                _set_run_font(r, "Consolas", size_pt=8.8, color_hex="1E2521")
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped == "---":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            i += 1
            continue

        if stripped in ("[[IMG_ARCH]]", "[[IMG_SKILLS]]"):
            img_path = arch_png if stripped == "[[IMG_ARCH]]" else skills_png
            caption = (
                "图 2-1｜NotebookLM-Fleet 三通道融合、智能调度与持久化全景架构图"
                if stripped == "[[IMG_ARCH]]"
                else "图 5-1｜NotebookLM 原子化 Skills 生态与跨 AI 编程工具接入拓扑图"
            )
            p_img = doc.add_paragraph()
            p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_img.paragraph_format.space_before = Pt(6)
            p_img.paragraph_format.space_after = Pt(2)
            p_img.add_run().add_picture(str(img_path), width=Inches(6.6))
            p_cap = doc.add_paragraph()
            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_cap.paragraph_format.space_after = Pt(8)
            cr = p_cap.add_run(caption)
            _set_run_font(cr, size_pt=9.0, bold=True, color_hex="5C6862")
            i += 1
            continue

        # Headings
        if line.startswith("# "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(8)
            _shade_elm(p._p.get_or_add_pPr(), "EEF6F4")
            r = p.add_run(line[2:].strip())
            _set_run_font(r, size_pt=19, bold=True, color_hex="136F63")
            i += 1
            continue

        if line.startswith("## "):
            h = doc.add_heading(level=1)
            h.paragraph_format.space_before = Pt(14)
            h.paragraph_format.space_after = Pt(5)
            h.paragraph_format.keep_with_next = True
            r = h.add_run(line[3:].strip())
            _set_run_font(r, size_pt=14, bold=True, color_hex="0F574E")
            i += 1
            continue

        if line.startswith("### "):
            h = doc.add_heading(level=2)
            h.paragraph_format.space_before = Pt(10)
            h.paragraph_format.space_after = Pt(4)
            h.paragraph_format.keep_with_next = True
            r = h.add_run(line[4:].strip())
            _set_run_font(r, size_pt=11.8, bold=True, color_hex="1F2B27")
            i += 1
            continue

        if line.startswith("#### "):
            h = doc.add_heading(level=3)
            h.paragraph_format.space_before = Pt(8)
            h.paragraph_format.space_after = Pt(3)
            h.paragraph_format.keep_with_next = True
            r = h.add_run(line[5:].strip())
            _set_run_font(r, size_pt=10.8, bold=True, color_hex="9A5C14")
            i += 1
            continue

        # Tables
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            headers = _split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_table_row(lines[i]))
                i += 1

            ncols = max(len(headers), max((len(r) for r in rows), default=0))
            table = doc.add_table(rows=1 + len(rows), cols=ncols)
            _set_table_borders(table)
            for c_idx in range(ncols):
                cell = table.rows[0].cells[c_idx]
                _shade_elm(cell._tc.get_or_add_tcPr(), "136F63")
                _set_cell_margins(cell)
                cp = cell.paragraphs[0]
                cp.paragraph_format.space_after = Pt(2)
                cp.paragraph_format.line_spacing = 1.25
                txt = headers[c_idx] if c_idx < len(headers) else ""
                _add_inline_runs(cp, txt, base_size=9.2, base_color="FFFFFF")
                for run in cp.runs:
                    run.bold = True
                    _set_run_font(run, size_pt=9.2, bold=True, color_hex="FFFFFF")
            for r_idx, row_vals in enumerate(rows):
                bg = "FAF8F3" if r_idx % 2 == 1 else "FFFFFF"
                for c_idx in range(ncols):
                    cell = table.rows[r_idx + 1].cells[c_idx]
                    _shade_elm(cell._tc.get_or_add_tcPr(), bg)
                    _set_cell_margins(cell)
                    cp = cell.paragraphs[0]
                    cp.paragraph_format.space_after = Pt(2)
                    cp.paragraph_format.line_spacing = 1.3
                    txt = row_vals[c_idx] if c_idx < len(row_vals) else ""
                    _add_inline_runs(cp, txt, base_size=9.0, base_color="202623")
            sp = doc.add_paragraph()
            sp.paragraph_format.space_after = Pt(4)
            continue

        # Blockquotes (coalesce consecutive > lines)
        if stripped.startswith(">"):
            q_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.18)
            p.paragraph_format.right_indent = Inches(0.12)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            _shade_elm(p._p.get_or_add_pPr(), "F0F7F5")
            _add_inline_runs(p, "  ".join(q_lines), base_size=9.8, base_color="213731")
            continue

        # Lists
        bullet_m = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        num_m = re.match(r"^(\s*)(\d+\.)\s+(.*)$", line)
        if bullet_m:
            indent_lv = len(bullet_m.group(1)) // 2
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.24 + indent_lv * 0.22)
            p.paragraph_format.first_line_indent = Inches(-0.14)
            p.paragraph_format.space_after = Pt(3)
            bullet_r = p.add_run("•  ")
            _set_run_font(bullet_r, size_pt=10.2, bold=True, color_hex="136F63")
            _add_inline_runs(p, bullet_m.group(2).strip(), base_size=10.2)
            i += 1
            continue
        if num_m:
            indent_lv = len(num_m.group(1)) // 2
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.26 + indent_lv * 0.22)
            p.paragraph_format.first_line_indent = Inches(-0.2)
            p.paragraph_format.space_after = Pt(3)
            nr = p.add_run(f"{num_m.group(2)} ")
            _set_run_font(nr, size_pt=10.2, bold=True, color_hex="136F63")
            _add_inline_runs(p, num_m.group(3).strip(), base_size=10.2)
            i += 1
            continue

        # Normal paragraph (coalesce consecutive plain lines)
        para_parts = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            nxts = nxt.strip()
            if (
                not nxts
                or nxts.startswith("#")
                or nxts.startswith(">")
                or nxts.startswith("```")
                or nxts.startswith("|")
                or nxts == "---"
                or nxts in ("[[IMG_ARCH]]", "[[IMG_SKILLS]]")
                or re.match(r"^(\s*)([-*]|\d+\.)\s+", nxt)
            ):
                break
            para_parts.append(nxts)
            i += 1
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(5)
        _add_inline_runs(p, " ".join(para_parts), base_size=10.3)

    doc.save(str(docx_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export PDF and DOCX whitepaper.")
    parser.add_argument("--out-dir", type=Path, default=BASE_DIR, help="Output directory for generated files")
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / PDF_PATH.name
    docx_path = out_dir / DOCX_PATH.name

    os.environ.setdefault("PYTHONUTF8", "1")
    md_text = MD_PATH.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        arch_png, skills_png = _render_svgs_to_pngs(tmp_dir)
        _build_pdf(md_text, tmp_dir, pdf_path)
        _build_docx(md_text, arch_png, skills_png, docx_path)
    print(f"PDF exported:  {pdf_path} ({pdf_path.stat().st_size} bytes)")
    print(f"DOCX exported: {docx_path} ({docx_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
