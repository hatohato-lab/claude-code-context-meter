#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-code-context-meter の機械判定 eval（8項目）。

すべて一時フォルダ内の架空データで行う。実プロセス・実ログには触れない。
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import context_meter as cm  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + str(detail)[:120] if detail and not ok else ""))


def jline(obj):
    return json.dumps(obj, ensure_ascii=False)


def usage_line(inp, create, read):
    return jline({"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read, "output_tokens": 5}}})


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ctxmeter_eval_"))
    try:
        # 1. usage の3項目合算
        p = tmp / "a.jsonl"
        p.write_text(usage_line(2, 2699, 76692) + "\n", encoding="utf-8")
        check("1 usage合算", cm.tail_usage(p) == 2 + 2699 + 76692, cm.tail_usage(p))

        # 2. 末尾の usage が優先される（古い値を読まない）
        p = tmp / "b.jsonl"
        p.write_text(usage_line(1, 1, 1) + "\n" + usage_line(10, 100, 1000) + "\n", encoding="utf-8")
        check("2 末尾優先", cm.tail_usage(p) == 1110, cm.tail_usage(p))

        # 3. 名前: custom-title が ai-title より優先
        p = tmp / "c.jsonl"
        p.write_text(
            jline({"type": "ai-title", "aiTitle": "自動タイトル"}) + "\n"
            + jline({"type": "custom-title", "customTitle": "レシピ工房"}) + "\n",
            encoding="utf-8")
        meta = cm.scan_meta(p)
        check("3 custom-title優先", cm.resolve_name("x", meta) == "レシピ工房", meta)

        # 4. 指示文の「チャット名は『〜』」を読み取る
        p = tmp / "d.jsonl"
        p.write_text(
            jline({"type": "user", "message": {"content": "チャット名は『試食会準備』にしてください。今日の作業を始めます"}}) + "\n",
            encoding="utf-8")
        meta = cm.scan_meta(p)
        check("4 指示文の名乗り", meta["declared"] == "試食会準備", meta)

        # 5. 対応表フォールバック（custom/ai/指示文が無いとき）
        ov = Path(cm.__file__).with_name("session-names.json")
        backup = ov.read_text(encoding="utf-8") if ov.exists() else None
        ov.write_text(jline({"_説明": "テスト", "sid-123": "在庫棚卸し"}), encoding="utf-8")
        try:
            empty = {"custom": "", "ai": "", "declared": "", "first_prompt": "初回発話", "compacts": 0}
            ok5 = cm.resolve_name("sid-123", empty) == "在庫棚卸し" and cm.resolve_name("sid-999", empty) == "初回発話"
        finally:
            if backup is None:
                ov.unlink()
            else:
                ov.write_text(backup, encoding="utf-8")
        check("5 対応表フォールバック", ok5)

        # 6. --resume のID抽出（= と スペース の両形式）と、無い場合の None
        sid = "0123abcd-1111-2222-3333-0123456789ab"
        ok6 = (cm.sid_from_args(f"claude --resume={sid} --x") == sid
               and cm.sid_from_args(f"claude --resume {sid}") == sid
               and cm.sid_from_args("claude --output-format stream-json") is None)
        check("6 resume抽出", ok6)

        # 7. コンパクティング回数を数える
        p = tmp / "e.jsonl"
        p.write_text(
            jline({"compactMetadata": {"trigger": "auto", "preTokens": 1000094}}) + "\n"
            + usage_line(1, 1, 1) + "\n"
            + jline({"compactMetadata": {"trigger": "auto", "preTokens": 999511}}) + "\n",
            encoding="utf-8")
        check("7 圧縮回数", cm.scan_meta(p)["compacts"] == 2, cm.scan_meta(p))

        # 8. 警告: 70%以上で⚠、圧縮2回以上で乗り換え検討
        cfg = dict(cm.DEFAULTS)
        results = [
            {"label": "満杯", "tokens": 820_000, "rss_mb": 100, "file_mb": 1.0,
             "mtime": "12:00", "compacts": 0, "is_self": False, "guessed": False},
            {"label": "常連", "tokens": 10_000, "rss_mb": 50, "file_mb": 0.5,
             "mtime": "12:01", "compacts": 3, "is_self": True, "guessed": False},
        ]
        rep = cm.format_report(results, cfg, now="2099-01-01 00:00")
        ok8 = ("⚠️ 「満杯」が 82%" in rep and "乗り換えを検討" in rep
               and "←今ここ" in rep and rep.count("| ") > 2)
        check("8 警告としきい値", ok8, rep[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(RESULTS)} / {len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
