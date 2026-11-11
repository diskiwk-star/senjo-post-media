# senjo-post-media

ガイヘキ洗浄バスターズの Instagram リールを、毎日 17:00（JST）に自動投稿するリポジトリです。
UaFloors の `ua-post-media` を土台にしています。

## 動き

1. Googleドライブの動画フォルダと、キャプション文書を読む
2. 「文書に見出しがあって、まだ出していない動画」のうち、**文書の順で次の1本**を選ぶ
3. 冒頭に時期の告知（`intros.json`）、末尾にエンディング（`config.json` の `ending`）をつなぎ、1080×1920 にそろえて書き出す
4. `media` ブランチに一時的に置いて Instagram に渡し、17:00 に投稿する。投稿後は `media` を空にする
5. `state/posted.json` に記録する（同じ動画は二度出さない）
6. **4日に1回（リール3本ごと）はフィードの日**。`feed/<名前>/1.jpg…` ＋ `caption.txt` をカルーセルで出す。feed は尽きたら最初から繰り返す（`config.json` の `feed_every`）

## 担当の方の運用

- 新しい動画を**ドライブのフォルダに入れる**
- キャプション文書に **`### 動画名`（ファイル名から .mp4 を除いたもの）＋本文** を書き足す
  - 文書に無い動画は投稿されません。文書の下に足した分は、今ある分の後に回ります

## 設定ファイル

| ファイル | 内容 |
|---|---|
| `config.json` | 投稿時刻、開始位置（`start_from`）、エンディング、カバー位置 |
| `intros.json` | 冒頭に挟む告知。`from`〜`until` の期間だけ使う |
| `skip.txt` | 投稿しない動画（名前の一部でよい） |
| `stories.json` | 決まった時刻のストーリーズ（12:00 / 18:00 枠、期間指定） |
| `oneoff/` | 1回だけのフィード投稿（Actions → post → `oneoff`） |
| `feed/` | フィード用の施工事例（画像はJPEG。1080×1350） |
| `assets/` | 告知・エンディングの動画を置く場所（`"drive:<ID>"` でドライブから直接も可） |

## Secrets（Settings → Secrets and variables → Actions）

| 名前 | 内容 |
|---|---|
| `IG_ACCESS_TOKEN` | Instagram Login API のトークン（約60日。`GH_PAT` があれば自動延長） |
| `IG_USER_ID` | 洗浄バスターズの IG ユーザーID |
| `DRIVE_FOLDER_ID` / `CAPTION_DOC_ID` | 動画フォルダと文書のID（公開ログに出さないため Secret） |
| `GH_PAT` | トークン自動延長用（このリポジトリの Secrets 読み書き権限のみ） |

## 手動実行（Actions → post → Run workflow）

- `list` … 投稿順の確認だけ
- `dry_run` … 書き出しまで（投稿しない）
- `post` … 今すぐ投稿（その日の分として記録されるので、同じ日の 17:00 には出ない）

## ストーリーズ

- `story.yml` が 12:00 / 18:00 の枠で、`stories.json` の期間内の分を出す（手動: Actions → story → 枠を選ぶ）
- リール・フィードを出したときは、同じ中身をストーリーズにも出す（`config.json` の `story_after_post`）。リールは60秒まで、フィードは画像を1枚ずつ

## 定時起動が遅れたとき（見張り役）

GitHub の定時実行は数時間遅れることがある。クラウドの見張り役（claude.ai の routine、12:20 / 17:20 / 19:20）が
`state/` の記録を見て、未投稿なら `claude/kick-*` ブランチに `kick/request.txt`（post / story-1200 / story-1800）を push し、
`post-kick.yml` が起動する。手元からも同じ手順で起動できる。

## その他の手動実行

- `extra` … 今日の分とは別に、次の動画を今すぐ追加で出す（その日の記録・フィードの数え方には入れない）
