"""정적 자산 캐시 버스팅 — app.html 의 ?v= 를 파일 내용 해시로 갱신한다.

왜 필요한가
-----------
브라우저는 같은 URL의 JS·CSS를 캐시한다. 배포 후에도 사용자는 옛 파일을 계속
쓰게 되고, HTML만 새로 받으면 새 마크업 + 옛 스크립트 조합이 되어 화면이
조용히 깨진다. 파일 내용이 바뀌면 URL도 바뀌게 만드는 것이 유일하게 확실한
해결책이다.

    python -m scripts.stamp_assets
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
HTML = FRONT / "app.html"

ASSET_RE = re.compile(r'(?P<attr>src|href)="(?P<path>/static/[^"?]+)(?:\?v=[0-9a-f]+)?"')


def digest(path: pathlib.Path) -> str:
    return hashlib.blake2b(path.read_bytes(), digest_size=4).hexdigest()


def main() -> int:
    src = HTML.read_text(encoding="utf-8")
    missing: list[str] = []
    stamped = 0

    def repl(m: re.Match) -> str:
        nonlocal stamped
        rel = m.group("path")
        target = FRONT / rel.removeprefix("/static/").join(("static/", ""))
        target = FRONT / rel.lstrip("/")
        if not target.exists():
            missing.append(rel)
            return m.group(0)
        stamped += 1
        return f'{m.group("attr")}="{rel}?v={digest(target)}"'

    out = ASSET_RE.sub(repl, src)
    if out != src:
        HTML.write_text(out, encoding="utf-8")

    print(f"자산 {stamped}개 스탬프 완료")
    for rel in missing:
        print(f"  경고 — 파일 없음: {rel}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
