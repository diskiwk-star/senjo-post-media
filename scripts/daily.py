#!/usr/bin/env python3
"""
ガイヘキ洗浄バスターズ - Instagram リール毎日自動投稿（GitHub Actions 実行用）

UaFloors の ua-post-media（publish.py）を土台に、次の点を変えている。
  * 予約キューは作らない。Googleドライブ「SNS用動画」とキャプション文書を毎回直接読み、
    「文書に載っていて、まだ出していない動画」のうち文書順で次の1本を選ぶ
  * 冒頭に時期の告知（intros.json）、末尾にエンディング（config.json の ending）をつなぐ
  * 1080x1920 / H.264 / 30fps / AAC にそろえて書き出す（元は4K・HEVCが混在）
  * 書き出した動画は media ブランチに一時的に置いて Instagram に渡し、投稿後に消す

ドライブも文書も「リンクを知っている全員が閲覧可」なので、ログインなしで読む。

使い方:
  python3 scripts/daily.py            # 本番（Actions から）
  python3 scripts/daily.py --list     # 投稿順の一覧（何も変更しない）
  DRY_RUN=1 python3 scripts/daily.py  # 選ぶ・書き出すまで行い、アップロードと投稿はしない

環境変数:
  IG_ACCESS_TOKEN / IG_USER_ID : Instagram Login API（Secret）
  DRIVE_FOLDER_ID / CAPTION_DOC_ID : ドライブのフォルダと文書のID（Secret。公開ログに出さないため）
  POST_NOW=1                   : 投稿時刻を待たない（手動実行用）
  GH_TOKEN, GITHUB_REPOSITORY  : media ブランチへの push 用（Actions が渡す）
  DRY_RUN=1                    : 投稿しない
  SKIP_ENCODE=1                : DRY_RUN 時に書き出しも省く（一覧確認用）
  TODAY=YYYY-MM-DD             : 日付の上書き（テスト用）
"""

from __future__ import annotations
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://graph.instagram.com/v23.0"
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (senjo-post-media)"

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
POSTED_FILE = ROOT / "state" / "posted.json"
DONE_DIR = ROOT / "state" / "done"
ASSETS = ROOT / "assets"

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER_ID = os.environ.get("IG_USER_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
SKIP_ENCODE = os.environ.get("SKIP_ENCODE", "") == "1"
MAX_WAIT_SEC = 90 * 60
MAX_PUSH_BYTES = 95 * 1024 * 1024  # GitHub は 100MB 超のファイルを受け付けない

# 文書の絵文字は Slack からの貼り付けで「画像」になっているので、文字の絵文字に戻す
EMOJI = {
    "家": "🏠", "チェックマーク大": "✔️", "シャワー": "🚿", "ピカピカ": "✨", "郵送": "📩",
    "両目": "👀", "警告": "⚠️", "レンチ": "🔧", "考え中": "🤔", "手": "✋", "泡": "🫧",
    "しずく": "💧", "下向き指差し": "👇", "訴えるような顔": "🥺", "画鋲": "📌", "ぽっ": "😊",
    "email": "✉️", "照れちゃう": "😳", "目覚まし時計": "⏰", "ストップウォッチ": "⏱️",
    "ok_女性": "🙆‍♀️", "sparkles": "✨", "woman-gesturing-ok": "🙆‍♀️",
    "envelope_with_arrow": "📩", "バンザイ": "🙌", "虫眼鏡": "🔍",
}


# ---------------------------------------------------------------- 取得まわり

def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_folder(folder_id: str) -> list[dict]:
    """フォルダ直下の動画一覧（ログイン不要の埋め込み表示を読む）。"""
    page = http_get(f"https://drive.google.com/embeddedfolderview?id={folder_id}").decode("utf-8")
    items = re.findall(
        r'<a href="https://drive\.google\.com/file/d/([^/"]+)/[^"]*"[^>]*>.*?flip-entry-title">([^<]*)<',
        page, re.S)
    if not items and "flip-entry" not in page:
        raise RuntimeError("ドライブのフォルダ一覧が読めません（共有設定が変わった可能性）")
    vids = []
    for fid, name in items:
        name = unicodedata.normalize("NFC", html.unescape(name))
        if "mp4" in name.lower() or "mov" in name.lower():
            vids.append({"id": fid, "name": name})
    return vids


def download(file_id: str, dest: Path):
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        ctype = r.headers.get("Content-Type", "")
        if "html" in ctype:
            raise RuntimeError(f"動画ではなくページが返りました（{file_id}）。共有設定を確認してください")
        shutil.copyfileobj(r, f, 1024 * 1024)
    print(f"  ダウンロード: {dest.stat().st_size / 1e6:.1f}MB")


def fetch_captions(doc_id: str) -> list[dict]:
    """キャプション文書を Markdown で取り、見出し（###）ごとの {title, caption} に分ける。"""
    md = http_get(f"https://docs.google.com/document/d/{doc_id}/export?format=md").decode("utf-8")
    entries, cur = [], None
    for line in md.splitlines():
        if line.startswith("[image") and "]: <data:" in line:
            break  # 末尾の画像データ定義
        m = re.match(r"^#{1,3}\s+(.*)$", line)
        if m:
            title = clean_inline(m.group(1)).strip()
            if cur:
                entries.append(cur)
            cur = {"title": title, "lines": []}
            continue
        if cur is not None:
            cur["lines"].append(line)
    if cur:
        entries.append(cur)

    out = []
    for e in entries:
        cap = tidy_caption(e["lines"])
        # 同じ見出しが2回出る形式（# と ###）にも耐える：本文が空の見出しは捨てる
        if cap:
            out.append({"title": e["title"], "caption": cap})
    return out


def clean_inline(s: str) -> str:
    s = re.sub(r"!\[:([^\]]+):\]\[image\d+\]",
               lambda m: EMOJI.get(m.group(1).replace("\\_", "_"), ""), s)
    s = s.replace("**", "")
    s = re.sub(r"\\(.)", r"\1", s)  # Markdown のエスケープを外す
    return s.rstrip()


def tidy_caption(lines: list[str]) -> str:
    text = "\n".join(clean_inline(l) for l in lines)
    text = re.sub(r"[ \t　]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ---------------------------------------------------------------- 選ぶ

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\.?(mp4|mov)$", "", s, flags=re.I)
    return re.sub(r"[\W_]+", "", s).lower()


def match_videos(captions: list[dict], videos: list[dict]) -> list[dict]:
    """文書の見出しと動画名を突き合わせる。年月（先頭4桁）が同じものの中で一番近い名前を採る。"""
    used, plan = set(), []
    for c in captions:
        key = norm(c["title"])
        ym = key[:4]
        best, score = None, 0.0
        for v in videos:
            if v["id"] in used:
                continue
            vk = norm(v["name"])
            if vk[:4] != ym:
                continue
            r = difflib.SequenceMatcher(None, key, vk).ratio()
            if r > score:
                best, score = v, r
        if best and score >= 0.6:
            used.add(best["id"])
            plan.append({**c, "video": best, "score": round(score, 2)})
        else:
            plan.append({**c, "video": None, "score": round(score, 2)})
    return plan


def load_posted() -> list[dict]:
    return json.loads(POSTED_FILE.read_text(encoding="utf-8")) if POSTED_FILE.exists() else []


def load_skip() -> list[str]:
    p = ROOT / "skip.txt"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(norm(line))
    return out


def upcoming(plan: list[dict]) -> list[dict]:
    posted = load_posted()
    posted_ids = {p.get("drive_id") for p in posted}
    posted_names = {norm(p.get("name", "")) for p in posted}
    skip = load_skip()
    start = norm(CONFIG.get("start_from") or "")
    started = not start
    out = []
    for p in plan:
        if not started:
            if start and start in norm(p["title"]):
                started = True
            else:
                continue
        v = p["video"]
        if not v:
            continue
        if v["id"] in posted_ids or norm(v["name"]) in posted_names:
            continue
        if any(s in norm(v["name"]) for s in skip):
            continue
        out.append(p)
    return out


def active_intro(today: date) -> dict | None:
    p = ROOT / "intros.json"
    if not p.exists():
        return None
    for it in json.loads(p.read_text(encoding="utf-8")):
        if not it.get("enabled", True):
            continue
        frm = date.fromisoformat(it["from"]) if it.get("from") else date.min
        til = date.fromisoformat(it["until"]) if it.get("until") else date.max
        if frm <= today <= til:
            return it
    return None


# ---------------------------------------------------------------- 書き出し

def resolve_asset(ref: str, workdir: Path) -> Path:
    if ref.startswith("drive:"):
        dest = workdir / f"asset_{ref[6:]}.mp4"
        if not dest.exists():
            download(ref[6:], dest)
        return dest
    p = ASSETS / ref
    if not p.exists():
        raise RuntimeError(f"assets/{ref} がありません")
    return p


def probe(path: Path) -> tuple[float, bool]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    has_audio = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
    return float(d["format"]["duration"]), has_audio


def build_video(segments: list[Path], out: Path, bitrate_k: int | None = None):
    """各素材を 1080x1920/30fps/48kHz ステレオにそろえてつなぐ。音声の無い素材には無音を足す。"""
    args, fc, labels = [], [], []
    for i, seg in enumerate(segments):
        dur, has_audio = probe(seg)
        args += ["-i", str(seg)]
        fc.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                  f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,setsar=1,format=yuv420p[v{i}]")
        if has_audio:
            fc.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    fc.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[v][a]")
    vopt = (["-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k}k", "-bufsize", f"{bitrate_k * 2}k"]
            if bitrate_k else ["-crf", "21", "-maxrate", "8M", "-bufsize", "16M"])
    cmd = ["ffmpeg", "-loglevel", "error", "-y", *args, "-filter_complex", ";".join(fc),
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
           *vopt, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- 配信（media ブランチ）

def push_media(src: Path | None, name: str) -> str:
    """media ブランチを「この1ファイルだけ」の状態に作り直して push する。src=None で空にする。"""
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    with tempfile.TemporaryDirectory() as d:
        run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
        run("init", "-q", "-b", "media")
        run("config", "user.name", "senjo-autopost-bot")
        run("config", "user.email", "actions@github.com")
        (Path(d) / "README.md").write_text(
            "投稿待ちの動画を一時的に置くブランチ。投稿後に自動で空になる。\n", encoding="utf-8")
        if src:
            shutil.copy(src, Path(d) / name)
        run("add", "-A")
        run("commit", "-q", "-m", f"media: {name if src else 'cleared'}")
        run("push", "-q", "--force", remote, "media")
    return f"https://raw.githubusercontent.com/{repo}/media/{urllib.parse.quote(name)}"


def wait_url(url: str, size: int, timeout: int = 300):
    """raw 配信に新しいファイルが出るまで待つ（サイズで確認）。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                if int(r.headers.get("Content-Length", 0)) == size:
                    print("  配信URLの準備OK")
                    return
        except urllib.error.HTTPError:
            pass
        time.sleep(10)
    raise RuntimeError("配信URLに動画が出てきません")


# ---------------------------------------------------------------- Instagram

def api(method: str, path: str, params: dict) -> dict:
    params = {**params, "access_token": TOKEN}
    if method == "GET":
        req = urllib.request.Request(f"{API}{path}?" + urllib.parse.urlencode(params))
    else:
        req = urllib.request.Request(f"{API}{path}", data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code} on {path}: {e.read().decode(errors='replace')}") from e


def wait_container(cid: str, timeout: int = 900):
    start = time.time()
    while time.time() - start < timeout:
        st = api("GET", f"/{cid}", {"fields": "status_code,status"})
        code = st.get("status_code")
        print(f"  container {cid}: {code}")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Instagram側の動画処理でエラー: {st}")
        time.sleep(15)
    raise RuntimeError("Instagram側の動画処理が終わりません")


def publish(cid: str) -> dict:
    last = None
    for attempt in range(6):
        try:
            return api("POST", f"/{USER_ID}/media_publish", {"creation_id": cid})
        except RuntimeError as e:
            if not any(t in str(e) for t in ("2207027", "2207006", '"code":9007')):
                raise
            last = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"公開の再試行が上限に達しました: {last}")


def already_posted_today(today: str, caption: str) -> bool:
    head = caption[:20].strip()
    try:
        res = api("GET", "/me/media", {"fields": "caption,timestamp", "limit": "5"})
        for m in res.get("data", []):
            ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")).astimezone(JST)
            if ts.strftime("%Y-%m-%d") == today and (m.get("caption") or "").startswith(head):
                return True
    except Exception as e:
        print(f"  （投稿済み確認をスキップ: {e}）")
    return False


def wait_until(target_hhmm: str):
    now = datetime.now(JST)
    hh, mm = (int(x) for x in target_hhmm.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (target - now).total_seconds()
    if delta <= 0:
        print(f"目標 {target_hhmm} を{int(-delta // 60)}分過ぎています。すぐ投稿します。")
        return
    if delta > MAX_WAIT_SEC:
        print(f"目標 {target_hhmm} まで{int(delta // 60)}分あり、待機上限を超えるため中止。")
        sys.exit(0)
    print(f"目標 {target_hhmm} まで{int(delta // 60)}分待ちます。")
    time.sleep(delta)


# ---------------------------------------------------------------- フィード（カルーセル）

FEED_DIR = ROOT / "feed"


def feed_items() -> list[dict]:
    """feed/<名前>/1.jpg, 2.jpg … ＋ caption.txt を1投稿とする。名前順。"""
    out = []
    if not FEED_DIR.exists():
        return out
    for d in sorted(p for p in FEED_DIR.iterdir() if p.is_dir()):
        imgs = sorted((p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg")),
                      key=lambda p: int(p.stem) if p.stem.isdigit() else 999)
        cap = d / "caption.txt"
        if imgs and cap.exists():
            out.append({"name": d.name, "images": imgs,
                        "caption": cap.read_text(encoding="utf-8").strip()})
    return out


def is_feed_day(posted: list[dict]) -> bool:
    """直近のリールが (feed_every - 1) 本続いていたらフィードの番。例: 4 → リール3本ごとにフィード1本。"""
    every = int(CONFIG.get("feed_every") or 0)
    if every < 2 or not feed_items():
        return False
    streak = 0
    for p in reversed(posted):
        if p.get("type") == "feed":
            break
        streak += 1
    return streak >= every - 1


def next_feed(posted: list[dict]) -> dict:
    """出した回数が一番少ないもの（同数なら名前順）。尽きたら最初から繰り返す。"""
    items = feed_items()
    count = {i["name"]: 0 for i in items}
    for p in posted:
        if p.get("type") == "feed" and p.get("name") in count:
            count[p["name"]] += 1
    return min(items, key=lambda i: count[i["name"]])


def post_carousel(item: dict, today: str):
    repo = os.environ.get("GITHUB_REPOSITORY", "diskiwk-star/senjo-post-media")
    urls = [f"https://raw.githubusercontent.com/{repo}/main/feed/{item['name']}/{p.name}"
            for p in item["images"]]
    print("---- キャプション ----\n" + item["caption"] + "\n----------------------")
    for url in urls:
        print(f"  画像: {url}")
    if len(item["caption"]) > 2200:
        raise SystemExit("キャプションが2,200字を超えています")
    if DRY_RUN:
        print("[DRY RUN] 投稿はしません。")
        return None
    if not TOKEN or not USER_ID:
        print("::notice::IG_ACCESS_TOKEN / IG_USER_ID が未設定のため、投稿せずに終了します")
        return None

    if os.environ.get("POST_NOW") == "1":
        print("手動実行のため、待たずに投稿します。")
    else:
        wait_until(CONFIG.get("post_time", "17:00"))
    if already_posted_today(today, item["caption"]):
        print("同じ内容が今日すでに投稿されています。二重投稿を避けて終了。")
        return {"skipped": True}

    if len(urls) == 1:
        c = api("POST", f"/{USER_ID}/media", {"image_url": urls[0], "caption": item["caption"]})
    else:
        children = []
        for url in urls:
            children.append(api("POST", f"/{USER_ID}/media",
                                {"image_url": url, "is_carousel_item": "true"})["id"])
            time.sleep(2)
        c = api("POST", f"/{USER_ID}/media", {"media_type": "CAROUSEL",
                                              "children": ",".join(children),
                                              "caption": item["caption"]})
    res = publish(c["id"])
    media_id = res.get("id", "")
    link = ""
    try:
        link = api("GET", f"/{media_id}", {"fields": "permalink"}).get("permalink", "")
    except Exception:
        pass
    print(f"投稿しました: {media_id} {link}")
    return {"media_id": media_id, "permalink": link}


# ---------------------------------------------------------------- 本体

def show_list(plan: list[dict]):
    nxt = upcoming(plan)
    print(f"文書の見出し {len(plan)}件 / 投稿待ち {len(nxt)}件\n")
    print("■ 投稿順（先頭が次の投稿）")
    for i, p in enumerate(nxt, 1):
        print(f"  {i:2d}. {p['video']['name']}")
    miss = [p for p in plan if not p["video"]]
    if miss:
        print("\n■ 文書にあるが、フォルダに動画が見つからない見出し（投稿されない）")
        for p in miss:
            print(f"  - {p['title']}（最も近い候補との一致度 {p['score']}）")


def main():
    today_d = date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else datetime.now(JST).date()
    today = today_d.isoformat()

    folder_id = CONFIG.get("folder_id") or os.environ.get("DRIVE_FOLDER_ID", "")
    doc_id = CONFIG.get("caption_doc_id") or os.environ.get("CAPTION_DOC_ID", "")
    if not folder_id or not doc_id:
        raise SystemExit("DRIVE_FOLDER_ID / CAPTION_DOC_ID が未設定です")
    videos = list_folder(folder_id)
    captions = fetch_captions(doc_id)
    plan = match_videos(captions, videos)

    if "--list" in sys.argv:
        in_doc = {p["video"]["id"] for p in plan if p["video"]}
        show_list(plan)
        fi = feed_items()
        posted = load_posted()
        print(f"\n■ フィード（{CONFIG.get('feed_every')}日に1回・{len(fi)}件を繰り返し）"
              f" 次回が今日なら: {'はい' if is_feed_day(posted) else 'いいえ'}")
        if fi:
            print(f"  次のフィード: {next_feed(posted)['name']}")
        rest = [v["name"] for v in videos if v["id"] not in in_doc]
        if rest:
            print("\n■ フォルダにあるが、文書にキャプションが無い動画（投稿されない）")
            for n in rest:
                print(f"  - {n}")
        return

    done = DONE_DIR / f"{today}.done"
    if done.exists():
        print(f"{today} は投稿済み。終了。")
        return

    posted = load_posted()
    force = os.environ.get("FORCE_KIND", "")
    if force == "feed" or (force != "reel" and is_feed_day(posted)):
        item = next_feed(posted)
        n = sum(1 for p in posted if p.get("type") == "feed" and p.get("name") == item["name"])
        print(f"date={today} dry_run={DRY_RUN}")
        print(f"今日はフィードの日: {item['name']}（画像{len(item['images'])}枚・{n + 1}回目）")
        res = post_carousel(item, today)
        if res and not res.get("skipped"):
            posted.append({"date": today, "type": "feed", "name": item["name"],
                           "media_id": res["media_id"], "permalink": res["permalink"]})
            POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")
        if res:
            DONE_DIR.mkdir(parents=True, exist_ok=True)
            done.write_text(datetime.now(JST).isoformat(), encoding="utf-8")
        return

    nxt = upcoming(plan)
    if not nxt:
        print("投稿待ちの動画がありません。フォルダとキャプション文書を確認してください。")
        return
    item = nxt[0]
    v = item["video"]
    caption = item["caption"]
    print(f"date={today} dry_run={DRY_RUN}")
    print(f"次の動画: {v['name']}（文書の見出し: {item['title']} / 一致度 {item['score']}）")
    print(f"残り {len(nxt) - 1} 本")
    print("---- キャプション ----\n" + caption + "\n----------------------")
    if len(caption) > 2200:
        raise SystemExit("キャプションが2,200字を超えています")
    if caption.count("#") > 30:
        raise SystemExit("ハッシュタグが30個を超えています")

    if not DRY_RUN and (not TOKEN or not USER_ID):
        # 初期設定が終わるまでは毎日失敗扱いにしない
        print("::notice::IG_ACCESS_TOKEN / IG_USER_ID が未設定のため、投稿せずに終了します")
        return

    intro = active_intro(today_d)
    ending = CONFIG.get("ending") or ""
    print(f"冒頭の告知: {intro['label'] if intro else 'なし'} / 末尾: {ending or 'なし'}")

    work = Path(tempfile.mkdtemp(prefix="senjo_"))
    main_path = work / "main.mp4"
    out_path = work / f"{today}.mp4"
    thumb_ms = int(float(CONFIG.get("cover_offset_sec") or 0) * 1000)

    if DRY_RUN and SKIP_ENCODE:
        print("（書き出しは省略）")
        return

    download(v["id"], main_path)
    segs = [main_path]
    if intro:
        ip = resolve_asset(intro["file"], work)
        segs.insert(0, ip)
        thumb_ms += int(probe(ip)[0] * 1000)  # カバーは本編の位置で取る
    if ending:
        segs.append(resolve_asset(ending, work))

    build_video(segs, out_path)
    dur, _ = probe(out_path)
    size = out_path.stat().st_size
    if size > MAX_PUSH_BYTES:
        kbps = int(MAX_PUSH_BYTES * 8 / dur / 1000 * 0.9) - 128
        print(f"  {size / 1e6:.0f}MB で大きすぎるため {kbps}kbps で書き出し直します")
        build_video(segs, out_path, bitrate_k=kbps)
        size = out_path.stat().st_size
    print(f"  書き出し: {dur:.1f}秒 / {size / 1e6:.1f}MB / カバー位置 {thumb_ms / 1000:.1f}秒")
    if not 3 <= dur <= 900:
        raise SystemExit(f"リールの長さ（3秒〜15分）の範囲外です: {dur:.1f}秒")

    if DRY_RUN:
        keep = ROOT / "out"
        keep.mkdir(exist_ok=True)
        shutil.copy(out_path, keep / out_path.name)
        print(f"[DRY RUN] 書き出し結果を out/{out_path.name} に保存。投稿はしません。")
        return

    url = push_media(out_path, out_path.name)
    print(f"  配信URL: {url}")
    wait_url(url, size)

    if os.environ.get("POST_NOW") == "1":
        print("手動実行のため、待たずに投稿します。")
    else:
        wait_until(CONFIG.get("post_time", "17:00"))

    if already_posted_today(today, caption):
        print("同じ内容が今日すでに投稿されています。二重投稿を避けて終了。")
        DONE_DIR.mkdir(parents=True, exist_ok=True)
        done.write_text(datetime.now(JST).isoformat(), encoding="utf-8")
        try:
            push_media(None, "")
        except Exception:
            pass
        return

    params = {"media_type": "REELS", "video_url": url, "caption": caption,
              "share_to_feed": "true", "thumb_offset": str(thumb_ms)}
    c = api("POST", f"/{USER_ID}/media", params)
    wait_container(c["id"])
    res = publish(c["id"])
    media_id = res.get("id", "")
    link = ""
    try:
        link = api("GET", f"/{media_id}", {"fields": "permalink"}).get("permalink", "")
    except Exception:
        pass
    print(f"投稿しました: {media_id} {link}")

    posted = load_posted()
    posted.append({"date": today, "type": "reel", "name": v["name"], "drive_id": v["id"], "title": item["title"],
                   "media_id": media_id, "permalink": link,
                   "intro": intro["label"] if intro else "", "ending": ending})
    POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    done.write_text(datetime.now(JST).isoformat(), encoding="utf-8")

    try:
        push_media(None, "")
        print("media ブランチを空にしました")
    except Exception as e:
        print(f"（media ブランチの片付けに失敗: {e}）")


if __name__ == "__main__":
    main()
