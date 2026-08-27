#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-code-context-meter — 稼働中の全 Claude Code チャットの容量計。

いま動いている各チャットについて、コンテキスト使用量（トークン）・上限比・
残り・実メモリ・チャット名を1つの Markdown 表で出す。

使い方:
    python context_meter.py

- Windows / Linux（コンテナ・Codespaces）両対応。標準ライブラリのみ。
- ファイルサイズは使用量ではない（記録は増え続けるが、要約が起きると使用量は減る）。
  使用量は生ログ末尾の usage（input + cache_creation + cache_read）から読む。
"""

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"

DEFAULTS = {
    "limit": 1_000_000,   # コンテキスト上限（トークン）。モデルに合わせて設定で変更可
    "projects_dir": "",   # 空なら現在のフォルダから自動導出
    "warn_pct": 70,       # この%以上で警告
    "compact_warn": 2,    # コンパクティングがこの回数以上で乗り換え検討を出す
}


def load_config():
    path = os.environ.get("CONTEXT_METER_CONFIG") or str(
        Path(__file__).with_name("context-meter.config.json")
    )
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except Exception:
        pass
    return cfg


def sanitize_cwd(cwd):
    """作業フォルダのパス → projects 配下のフォルダ名（英数字以外は '-'）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def default_projects_dir():
    derived = CLAUDE_DIR / "projects" / sanitize_cwd(os.getcwd())
    if derived.exists():
        return derived
    # 大文字小文字の揺れ（Windows の C:\ と c--… 等）を吸収する
    try:
        for d in (CLAUDE_DIR / "projects").iterdir():
            if d.is_dir() and d.name.lower() == derived.name.lower():
                return d
    except Exception:
        pass
    return derived


# 実行ファイルのパスとして claude 本体が現れる形に限定する。
# （コマンド文字列に 'claude' を含むだけの別プロセスや、このスクリプト自身の
# 起動コマンドを拾わないため。実測で踏んだ罠。）
PROC_PATTERN = re.compile(r"native-binary[/\\]claude(\.exe)?(\"|\s|$)", re.IGNORECASE)

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def sid_from_args(args):
    """コマンドラインの --resume からセッションIDを取り出す。無ければ None。"""
    m = re.search(r"--resume[=\s]\s*(" + UUID_RE + r")", args)
    return m.group(1) if m else None


def sid_from_sessions_file(pid):
    """~/.claude/sessions/<pid>.json に sessionId があれば返す（Codespaces形式）。
    Windows ローカルではこのファイルに sessionId が無いので None になる。"""
    try:
        data = json.loads((CLAUDE_DIR / "sessions" / f"{pid}.json").read_text(encoding="utf-8"))
        sid = data.get("sessionId")
        return sid if isinstance(sid, str) and re.fullmatch(UUID_RE, sid) else None
    except Exception:
        return None


def running_processes():
    """claude 本体のプロセスを拾い、[{pid, rss_mb, args}] を返す。"""
    rows = []
    if platform.system() == "Windows":
        # コマンドラインに日本語パスが混ざると既定の CP932 で壊れるため、
        # PowerShell 側で UTF-8 を宣言し、バイト列で受けてから自前でデコードする
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name LIKE 'claude%'\" | "
            "Select-Object ProcessId,WorkingSetSize,CommandLine | ConvertTo-Json -Depth 2"
        )
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=30,
        ).stdout
        try:
            out = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                out = raw.decode("cp932")
            except UnicodeDecodeError:
                out = raw.decode("utf-8", "replace")
        try:
            data = json.loads(out)
        except Exception:
            return rows
        if isinstance(data, dict):
            data = [data]
        for p in data or []:
            args = p.get("CommandLine") or ""
            if not PROC_PATTERN.search(args):
                continue
            rows.append({
                "pid": int(p["ProcessId"]),
                "rss_mb": (p.get("WorkingSetSize") or 0) / 1024 / 1024,
                "args": args,
            })
    else:
        out = subprocess.run(
            ["ps", "-eo", "pid,rss,args"], capture_output=True, text=True, timeout=30
        ).stdout
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
            if not m or not PROC_PATTERN.search(m.group(3)):
                continue
            rows.append({
                "pid": int(m.group(1)),
                "rss_mb": int(m.group(2)) / 1024,
                "args": m.group(3),
            })
    return rows


def assign_session_ids(rows, projects_dir):
    """各プロセスにセッションIDを割り当てる。

    確度の高い順に 1) sessions/<pid>.json 2) --resume 3) 自分自身は環境変数
    4) 残りは更新の新しい生ログを充てる（推定）。
    「--resume が無いものが自分」という判定はしない（自分も --resume 付きで
    動いていることがある。実測で踏んだ罠）。
    """
    self_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    self_pid = os.environ.get("CLAUDE_PID", "")

    for r in rows:
        r["sid"] = sid_from_sessions_file(r["pid"]) or sid_from_args(r["args"])
        if self_pid and str(r["pid"]) == self_pid:
            r["sid"] = self_sid or r["sid"]
    for r in rows:
        r["is_self"] = bool(self_sid) and r.get("sid") == self_sid

    known = {r["sid"] for r in rows if r.get("sid")}
    try:
        files = sorted(projects_dir.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    cands = [f.stem for f in files if f.stem not in known]
    for r in rows:
        if not r.get("sid") and cands:
            r["sid"] = cands.pop(0)
            r["guessed"] = True
    return rows


def find_jsonl(sid, projects_dir):
    """セッションIDの生ログを探す。まず対象フォルダ、無ければ全プロジェクト。"""
    p = projects_dir / f"{sid}.jsonl"
    if p.exists():
        return p
    hits = list((CLAUDE_DIR / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def tail_usage(path, max_bytes=16 * 1024 * 1024):
    """末尾から最初に見つかる usage の合計を返す（現在のコンテキスト使用量）。

    合計 = input_tokens + cache_creation_input_tokens + cache_read_input_tokens。
    """
    size = path.stat().st_size
    chunk = 2 * 1024 * 1024
    with path.open("rb") as fh:
        while chunk <= max_bytes:
            fh.seek(max(0, size - chunk))
            lines = fh.read().split(b"\n")
            for raw in reversed(lines):
                if b'"usage"' not in raw or b'"input_tokens"' not in raw:
                    continue
                try:
                    u = json.loads(raw.decode("utf-8", "replace")).get("message", {}).get("usage", {})
                except Exception:
                    continue
                if not isinstance(u, dict) or "input_tokens" not in u:
                    continue
                return (
                    (u.get("input_tokens") or 0)
                    + (u.get("cache_creation_input_tokens") or 0)
                    + (u.get("cache_read_input_tokens") or 0)
                )
            if chunk >= size:
                break
            chunk *= 4
    return None


# 立ち上げ時の指示文で「チャット名は『〜』」と名乗りを指定する運用のための読み取り
NAME_PATTERNS = [
    re.compile(r"チャット名(?:は)?\s*[「『\"']([^」』\"'\n]{1,40})[」』\"']"),
    re.compile(r"チャット名(?:を)?\s*([^\s」』\n]{1,40})\s*(?:に(?:して|変更)|とする)"),
]

OVERRIDES_FILE = Path(__file__).with_name("session-names.json")


def override_name(sid):
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    name = data.get(sid)
    return name if isinstance(name, str) and not name.startswith("_") else None


def scan_meta(path, head_lines=60):
    """生ログを1回だけ通読して、名前の材料とコンパクティング回数を集める。

    返り値: {custom, ai, declared, first_prompt, compacts}
    - custom / ai … タイトル行（後の行が優先）。ローカル版はここに名前が保存される
    - declared   … 冒頭の指示文に書かれた「チャット名は『〜』」
    - first_prompt … 最初の発話（名前が全く無いときの識別用）
    - compacts   … compactMetadata の出現回数（過去のコンパクティング回数）
    """
    meta = {"custom": "", "ai": "", "declared": "", "first_prompt": "", "compacts": 0}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if '"compactMetadata"' in line:
                meta["compacts"] += 1
            if '"custom-title"' in line or '"ai-title"' in line:
                try:
                    o = json.loads(line)
                    if o.get("type") == "custom-title":
                        meta["custom"] = o.get("customTitle") or o.get("title") or meta["custom"]
                    elif o.get("type") == "ai-title":
                        meta["ai"] = o.get("aiTitle") or o.get("title") or meta["ai"]
                except Exception:
                    pass
            if i < head_lines and not meta["declared"]:
                for pat in NAME_PATTERNS:
                    m = pat.search(line)
                    if m:
                        meta["declared"] = m.group(1).strip()
                        break
            if not meta["first_prompt"] and '"type":"user"' in line.replace(" ", ""):
                try:
                    d = json.loads(line)
                    c = d.get("message", {}).get("content")
                    text = c if isinstance(c, str) else "".join(
                        b.get("text", "") for b in c if isinstance(b, dict)
                    ) if isinstance(c, list) else ""
                    text = re.sub(r"<[^>]+>", "", text).strip().replace("\n", " ")
                    if text and not text.startswith("Caveat:"):
                        meta["first_prompt"] = shorten(text)
                except Exception:
                    pass
    return meta


def shorten(text, width=34):
    """先頭が長いパスだと全チャットが同じ文字列に見えるため、末尾側を残す。"""
    m = re.match(r"\s*(?:[A-Za-z]:)?[\\/][^\s]{12,}", text)
    if m:
        head = m.group(0)
        rest = text[len(head):].strip()
        tail = re.split(r"[\\/]", head.rstrip("\\/"))[-1]
        text = f"…{tail} {rest}".strip() if rest else f"…{tail}"
    return text[:width]


def resolve_name(sid, meta):
    return (
        meta["custom"] or meta["ai"] or meta["declared"]
        or override_name(sid) or meta["first_prompt"] or "(不明)"
    )


def bar(pct, width=10):
    filled = min(width, int(pct / 100 * width + 0.5))
    return "█" * filled + "░" * (width - filled)


def format_report(results, cfg, now=None):
    """計測結果 → Markdown 表＋警告。純関数（eval から直接検証できる）。"""
    limit = cfg["limit"]
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"## チャットのメモリー使用量（{now} 時点・上限 {limit:,} トークン）", ""]
    out.append("| # | チャット名 | コンテキスト使用 | 上限比 | 残り | 実メモリ | 記録 | 最終 |")
    out.append("|---|---------|-----------------|--------|------|---------|------|------|")
    for i, x in enumerate(sorted(results, key=lambda r: -r["tokens"]), 1):
        mark = " ←今ここ" if x.get("is_self") else (" (推定)" if x.get("guessed") else "")
        pct = x["tokens"] / limit * 100
        out.append(
            f"| {i} | {x['label']}{mark} | {x['tokens']:,} | "
            f"{bar(pct)} {pct:.1f}% | {limit - x['tokens']:,} | "
            f"{x['rss_mb']:.0f} MB | {x['file_mb']:.1f} MB | {x['mtime']} |"
        )
    total = sum(x["tokens"] for x in results)
    rss = sum(x["rss_mb"] for x in results)
    out.append("")
    out.append(f"**合計**: {len(results)} チャット / コンテキスト {total:,} トークン / 実メモリ {rss:.0f} MB")
    for x in results:
        pct = x["tokens"] / limit * 100
        if pct >= cfg["warn_pct"]:
            out.append(
                f"\n⚠️ 「{x['label']}」が {pct:.0f}% に達しています"
                f"（残り {limit - x['tokens']:,} トークン）。上限到達でコンパクティング"
                f"（自動要約。約9割が要約に置き換わり、1〜4分停止）が起きます。"
            )
        if x.get("compacts", 0) >= cfg["compact_warn"]:
            out.append(
                f"\n💡 「{x['label']}」は過去にコンパクティングが {x['compacts']} 回起きています。"
                f"ファイルへの記録を確かめた上で、新しいチャットへの乗り換えを検討してください。"
            )
    return "\n".join(out)


def main():
    cfg = load_config()
    projects_dir = Path(cfg["projects_dir"]) if cfg["projects_dir"] else default_projects_dir()
    rows = assign_session_ids(running_processes(), projects_dir)
    results = []
    for r in rows:
        if not r.get("sid"):
            continue
        path = find_jsonl(r["sid"], projects_dir)
        if not path:
            continue
        meta = scan_meta(path)
        st = path.stat()
        results.append({
            "label": resolve_name(r["sid"], meta),
            "tokens": tail_usage(path) or 0,
            "rss_mb": r["rss_mb"],
            "file_mb": st.st_size / 1024 / 1024,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%H:%M"),
            "compacts": meta["compacts"],
            "is_self": r.get("is_self", False),
            "guessed": r.get("guessed", False),
        })
    if not results:
        print("稼働中の Claude Code チャットが見つかりませんでした。")
        print(f"（探した場所: {projects_dir}。projects_dir を設定で指定できます）")
        return 1
    print(format_report(results, cfg))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
