"""決まった時刻のストーリーズ投稿（stories.json）。

  SLOT=12:00 python3 scripts/story.py   # その枠で期間内のものを全部出す
  POST_NOW=1 で時刻を待たない。記録は state/story/YYYY-MM-DD_HHMM.done（同じ日の同じ枠は二度出さない）
stories.json の1件: {"label", "slot": "HH:MM", "file": リポジトリ内のパス（.mp4 か .jpg）, "from", "until"}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily  # noqa: E402

ROOT = daily.ROOT
DONE = ROOT / "state" / "story"


def active(slot: str, today: date) -> list[dict]:
    items = json.loads((ROOT / "stories.json").read_text(encoding="utf-8"))
    out = []
    for it in items:
        if it.get("slot") != slot or not it.get("enabled", True):
            continue
        frm = date.fromisoformat(it["from"]) if it.get("from") else date.min
        til = date.fromisoformat(it["until"]) if it.get("until") else date.max
        if frm <= today <= til:
            out.append(it)
    return out


def main():
    slot = os.environ.get("SLOT", "").strip()
    today = datetime.now(daily.JST).date()
    marker = DONE / f"{today}_{slot.replace(':', '')}.done"
    if marker.exists():
        print(f"{today} {slot} のストーリーズは投稿済み。終了。")
        return
    items = active(slot, today)
    print(f"{today} {slot} の枠: {len(items)}件")
    for it in items:
        print(f"  - {it['label']} / {it['file']}")
    if not items:
        return
    if not daily.TOKEN or not daily.USER_ID:
        print("::notice::トークン未設定のため終了")
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "diskiwk-star/senjo-post-media")
    if os.environ.get("POST_NOW") != "1":
        daily.wait_until(slot)
    ok = 0
    for it in items:
        if not (ROOT / it["file"]).exists():
            print(f"::warning::ファイルがありません: {it['file']}")
            continue
        url = f"https://raw.githubusercontent.com/{repo}/main/{urllib.parse.quote(it['file'])}"
        video = it["file"].lower().endswith(".mp4")
        try:
            sid = daily.post_story(url, video)
            print(f"ストーリーズを投稿しました: {sid}（{it['label']}）")
            ok += 1
        except Exception as e:
            print(f"::error::ストーリーズの投稿に失敗（{it['label']}）: {e}")
    if ok:
        DONE.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(daily.JST).isoformat(), encoding="utf-8")
    if ok < len(items):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
