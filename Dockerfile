FROM python:3.12-slim

# ffmpeg corta e converte os vídeos
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# deno é usado pelo yt-dlp no YouTube (se falhar, o resto funciona igual)
RUN pip install --no-cache-dir deno || (curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y) || echo "deno nao instalado"

COPY server server
COPY web web

ENV DATA_DIR=/tmp/neoncut PORT=8000
EXPOSE 8000
# atualiza o yt-dlp a cada início (as plataformas mudam com frequência)
CMD pip install -q -U "yt-dlp[default]" ; uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
