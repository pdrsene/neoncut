FROM python:3.12-slim

# ffmpeg corta e converte os vídeos; deno é exigido pelo yt-dlp para o YouTube
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY server server
COPY web web

ENV DATA_DIR=/tmp/neoncut PORT=8000
EXPOSE 8000
# atualiza o yt-dlp a cada início (as plataformas mudam com frequência)
CMD pip install -q -U "yt-dlp[default]" ; uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
