"""1回だけのフィード投稿（ピン留め用の告知など）。毎日の順番・4日に1回のフィードとは別扱い。

  ONEOFF=<oneoff/ の下のフォルダ名> python3 scripts/post_oneoff.py
  中身は 1.jpg, 2.jpg …（JPEG）＋ caption.txt。記録は state/oneoff.json（同じものは二度出さない）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily  # noqa: E402

ROOT = daily.ROOT
LOG = ROOT / "state" / "oneoff.json"


def main():
    name = os.environ.get("ONEOFF", "").strip()
    d = ROOT / "oneoff" / name
    if not name or not d.is_dir():
        raise SystemExit(f"oneoff/{name} が見つかりません")
    imgs = sorted((p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg")),
                  key=lambda p: int(p.stem) if p.stem.isdigit() else 999)
    caption = (d / "caption.txt").read_text(encoding="utf-8").strip()
    log = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    if any(x.get("name") == name for x in log):
        print(f"oneoff/{name} は投稿済みです。終了。")
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "diskiwk-star/senjo-post-media")
    urls = [f"https://raw.githubusercontent.com/{repo}/main/oneoff/{name}/{p.name}" for p in imgs]
    print("---- キャプション ----\n" + caption + "\n----------------------")
    for u in urls:
        print(f"  画像: {u}")
    if daily.DRY_RUN:
        print("[DRY RUN] 投稿はしません。")
        return
    uid = daily.USER_ID
    if len(urls) == 1:
        c = daily.api("POST", f"/{uid}/media", {"image_url": urls[0], "caption": caption})
    else:
        kids = []
        for u in urls:
            kids.append(daily.api("POST", f"/{uid}/media", {"image_url": u, "is_carousel_item": "true"})["id"])
            time.sleep(2)
        c = daily.api("POST", f"/{uid}/media", {"media_type": "CAROUSEL", "children": ",".join(kids),
                                                "caption": caption})
    res = daily.publish(c["id"])
    mid = res.get("id", "")
    link = daily.api("GET", f"/{mid}", {"fields": "permalink"}).get("permalink", "")
    print(f"投稿しました: {mid} {link}")
    log.append({"date": datetime.now(daily.JST).isoformat(timespec="minutes"), "name": name,
                "media_id": mid, "permalink": link})
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
