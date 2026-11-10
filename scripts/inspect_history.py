#!/usr/bin/env python3
"""アカウントの投稿履歴を調べる（手動実行用）。キャプション全文ごとの件数と、カルーセルの冒頭を出す。"""
import collections, hashlib, json, os, urllib.parse, urllib.request
T = os.environ["IG_ACCESS_TOKEN"]; B = "https://graph.instagram.com/v23.0"
url = f"{B}/me/media?" + urllib.parse.urlencode({"fields": "timestamp,media_type,caption,thumbnail_url,permalink", "limit": "100", "access_token": T})
items = []
while url:
    d = json.load(urllib.request.urlopen(url)); items += d["data"]; url = d.get("paging", {}).get("next")
g = collections.defaultdict(list)
for m in items:
    g[hashlib.md5((m.get("caption") or "").encode()).hexdigest()[:8]].append(m)
for k, ms in sorted(g.items(), key=lambda x: -len(x[1])):
    c = (ms[0].get("caption") or "").replace("\n", " / ")
    print(f"[{k}] {len(ms)}件 {ms[-1]['timestamp'][:10]}〜{ms[0]['timestamp'][:10]} {ms[0]['media_type']}")
    print("   ", c[:160])
    print("   ", ms[0].get("permalink"))
