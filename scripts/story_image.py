"""フィード用の画像（4:5 など）をストーリーズ用の 1080×1920（9:16）に整える。

画像は横幅いっぱいに置き、上下の余白は
- 画像の上端・下端がほぼ一色（文字入りの告知画像など）→ その色で塗る（つなぎ目が見えない）
- 写真など色がばらつく → 同じ画像を強くぼかして暗くしたもので埋める
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

W, H = 1080, 1920


def _edge_rows(src: Path) -> list[tuple[int, int, int]]:
    """上端と下端の数行の画素（RGB）を返す。"""
    out = []
    for crop in (f"iw:8:0:0", f"iw:8:0:ih-8"):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"crop={crop},scale=108:1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True).stdout
        out += [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    return out


def flat_color(src: Path) -> str | None:
    px = _edge_rows(src)
    avg = [sum(p[c] for p in px) / len(px) for c in range(3)]
    spread = max(max(abs(p[c] - avg[c]) for c in range(3)) for p in px)
    if spread <= 24:
        return "0x%02x%02x%02x" % tuple(int(round(v)) for v in avg)
    return None


def make_story_image(src: Path, dst: Path) -> Path:
    color = flat_color(src)
    if color:
        vf = f"scale={W}:-2,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={color}"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf, "-q:v", "2", str(dst)]
    else:
        fc = (f"[0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"gblur=sigma=60,eq=brightness=-0.22[bg];[0]scale={W}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc, "-q:v", "2", str(dst)]
    subprocess.run(cmd, check=True)
    return dst


if __name__ == "__main__":
    make_story_image(Path(sys.argv[1]), Path(sys.argv[2]))
    print(sys.argv[2])
