# KO-Detect 컨테이너 이미지
#
# Render 는 render.yaml 의 native Python 런타임으로 배포하는 편이 빠릅니다.
# 이 Dockerfile 은 다음 경우에 씁니다.
#   - 사내 폐쇄망 온프레미스 배포
#   - Render 를 Docker 런타임으로 쓰고 싶을 때 (render.yaml 의 runtime 을 docker 로 변경)
#   - 로컬에서 배포와 동일한 환경을 재현해 확인할 때
#
#   docker build -t kodetect .
#   docker run -p 8077:8077 --env-file .env kodetect

FROM python:3.12-slim

# opencv-python-headless 는 GUI 라이브러리가 필요 없지만,
# libGL/glib 없이 import 가 실패하는 배포판이 있어 최소 런타임만 넣습니다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend:/app \
    STORAGE_DIR=/data/storage \
    PORT=8077

WORKDIR /app

# 의존성을 먼저 넣어 소스 변경 시 레이어 캐시를 살립니다.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY datagen/ ./datagen/
COPY rl/ ./rl/

# 업로드 원본·오버레이는 볼륨으로 빼둡니다.
RUN mkdir -p /data/storage
VOLUME ["/data"]

EXPOSE 8077
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8077)}/healthz')"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --app-dir backend"]
