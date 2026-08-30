"""PDF 명세를 마크다운으로 변환한다.

왜 저장소에 두는가
------------------
PDF는 grep 이 안 되고 diff 가 안 된다. 명세가 바뀌었을 때 무엇이 달라졌는지
확인할 방법이 없고, 코드 리뷰에서 "명세 어디에 그렇게 적혀 있나"를 링크로
가리킬 수도 없다. 마크다운으로 옮겨 두면 둘 다 된다.

이미지 기반 PDF(스캔·슬라이드)는 텍스트가 없어 자동 변환이 안 된다. 그런
파일은 페이지 이미지를 뽑아 두고 사람이 읽어 옮긴다 — 이 스크립트는 그
이미지 추출까지만 한다.

    python -m scripts.pdf_to_md                 # PDF/ 전체
    python -m scripts.pdf_to_md --images-only   # 이미지만 추출
"""

from __future__ import annotations

import argparse
import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "PDF"
OUT_DIR = ROOT / "docs" / "spec"


def slugify(name: str) -> str:
    s = re.sub(r"[()]", "", name)
    return re.sub(r"[^\w가-힣.-]+", "_", s).strip("_")


def clean(text: str) -> str:
    """추출 텍스트를 읽을 만하게 다듬는다."""
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()
        # 머리말·꼬리말(문서 제목 반복, 페이지 번호)은 잡음이다
        if re.fullmatch(r"—\s*\d+\s*—", line.strip()):
            continue
        if re.fullmatch(r"\d+", line.strip()):
            continue
        lines.append(line)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def convert(path: pathlib.Path, images_only: bool = False) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    text_chars = 0

    for i, page in enumerate(reader.pages, 1):
        body = clean(page.extract_text() or "")
        text_chars += len(body)
        parts.append(f"\n---\n\n## p{i}\n\n{body}\n" if body else f"\n---\n\n## p{i}\n\n*(이미지 페이지 — 텍스트 없음)*\n")

    # 텍스트가 거의 없으면 이미지 기반 문서다
    if text_chars < 200 or images_only:
        img_dir = OUT_DIR / "images" / slugify(path.stem)
        img_dir.mkdir(parents=True, exist_ok=True)
        from PIL import Image

        n = 0
        for i, page in enumerate(reader.pages, 1):
            imgs = list(page.images)
            if not imgs:
                continue
            im = Image.open(io.BytesIO(imgs[0].data)).convert("RGB")
            w, h = im.size
            scale = min(1.0, 1600 / max(w, h))
            if scale < 1.0:
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            im.save(img_dir / f"p{i:02d}.png", optimize=True)
            n += 1
        return f"__IMAGES__:{img_dir}", n

    header = (
        f"# {path.stem}\n\n"
        f"> 원본: `PDF/{path.name}` · pypdf 자동 추출\n"
        f"> 표·그림 배치는 원본을 따르지 않습니다. 정확한 판단이 필요하면 원본을 보십시오.\n"
    )
    return header + "".join(parts) + "\n", text_chars


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PDF 명세 → 마크다운")
    ap.add_argument("--images-only", action="store_true")
    args = ap.parse_args(argv)

    if not PDF_DIR.exists():
        print(f"PDF 폴더가 없습니다: {PDF_DIR}")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        result, n = convert(pdf, args.images_only)
        if result.startswith("__IMAGES__:"):
            print(f"  이미지 기반 — 페이지 {n}장 추출: {result.split(':', 1)[1]}")
            print(f"    → 내용은 사람이 읽어 {OUT_DIR / (slugify(pdf.stem) + '.md')} 로 옮기십시오")
            continue
        out = OUT_DIR / f"{slugify(pdf.stem)}.md"
        out.write_text(result, encoding="utf-8")
        print(f"  {out.relative_to(ROOT)}  ({n:,}자)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
