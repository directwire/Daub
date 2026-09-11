"""W12 验收拼板：参考 | 回放终帧 | daub.exe 真渲 | 浏览器 wasm。

先跑两套门禁（web/_smoke/_check_replay.py、web/_smoke/_check_wasm.py）
再跑本脚本——拼板直接取门禁产物，不自己渲。输出
web/_smoke/build/w12_acceptance_sheet.png（build/ 不入库，图随门禁再生）。
"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
B = os.path.join(ROOT, "web", "_smoke", "build")
OUT = os.path.join(B, "w12_acceptance_sheet.png")
FONT = r"C:\Windows\Fonts\msyh.ttc"

PAIRS = [
    (os.path.join(ROOT, "web", "_smoke", "ref512.png"), "1 参考 ref512"),
    (os.path.join(B, "replay.png"), "2 回放终帧 SVG mean|diff|=7.66"),
    (os.path.join(B, "truth.png"), "3 daub.exe 真渲"),
    (os.path.join(B, "wasm_render.png"), "4 浏览器 wasm mean|diff|=0.00"),
]


def main():
    font = ImageFont.truetype(FONT, 16)
    tiles = []
    for path, label in PAIRS:
        im = Image.open(path).convert("RGB")
        if im.size != (512, 512):
            im = im.resize((512, 512))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 511, 24], fill=(18, 18, 18))
        d.text((8, 4), label, font=font, fill=(255, 255, 255))
        tiles.append(im)
    sheet = Image.new("RGB", (512 * len(tiles) + 10 * (len(tiles) - 1), 512),
                      (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * (512 + 10), 0))
    sheet.save(OUT)
    print("sheet:", OUT, sheet.size)


if __name__ == "__main__":
    main()
