# Etapa 1: embaralha (minifica + mangle) o código do site, para dificultar a cópia.
# O original continua legível no GitHub; só a versão servida é embaralhada.
FROM node:20-slim AS webmin
WORKDIR /w
COPY web/ web/
RUN (npx -y html-minifier-terser@7 --collapse-whitespace --remove-comments --minify-css true \
      --minify-js '{"module":true,"compress":{"passes":2,"drop_console":true},"mangle":{"toplevel":true}}' \
      web/index.html -o web/index.min.html && mv web/index.min.html web/index.html) \
    || echo "minify falhou, servindo original"

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
COPY --from=webmin /w/web web

ENV DATA_DIR=/tmp/neoncut PORT=8000
EXPOSE 8000
# atualiza o yt-dlp a cada início (as plataformas mudam com frequência)
CMD pip install -q -U "yt-dlp[default]" ; uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
