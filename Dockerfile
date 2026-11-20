# Etapa 1: embaralha (minifica + mangle) o código do site, para dificultar a cópia.
# O original continua legível no GitHub; só a versão servida é embaralhada.
FROM node:20-slim AS webmin
WORKDIR /w
COPY web/ web/
# Pinned browser-only renderer, served locally; failure leaves the recorder fallback available.
RUN (npm pack mediabunny@1.66.0 --silent --pack-destination /tmp \
    && tar -xzf /tmp/mediabunny-1.66.0.tgz -C /tmp \
    && cp /tmp/package/dist/bundles/mediabunny.min.mjs web/vendor/mediabunny.min.mjs \
    && cp /tmp/package/LICENSE web/vendor/mediabunny.LICENSE.txt) \
    || (rm -f web/vendor/mediabunny.min.mjs && echo "WebCodecs renderer unavailable; recorder fallback")
RUN node -e 'const fs=require("fs");const h=fs.readFileSync("web/index.html","utf8");const m=h.match(/<script type="module">([\s\S]*?)<\/script>/);if(!m)throw Error("missing module");fs.writeFileSync("/tmp/neoncut-check.mjs",m[1])' && node --check /tmp/neoncut-check.mjs
RUN (npx -y html-minifier-terser@7 --collapse-whitespace --remove-comments --minify-css true \
      --minify-js '{"module":true,"compress":{"passes":2,"drop_console":true},"mangle":{"toplevel":true}}' \
      web/index.html -o web/index.min.html && mv web/index.min.html web/index.html) \
    || echo "minify falhou, servindo original"

FROM python:3.12-slim

# ffmpeg corta e converte os vídeos
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# deno é usado pelo yt-dlp no YouTube (se falhar, o resto funciona igual)
RUN pip install --no-cache-dir deno || (curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y) || echo "deno nao instalado"

# provedor de "PO token" do YouTube (passa na verificação anti-robô em servidores de nuvem).
# Binário pronto + plugin do yt-dlp. Se o download falhar, o resto funciona igual.
ARG POT_VER=v0.8.1
RUN mkdir -p /app/yt-plugins \
    && (curl -fsSL -o /usr/local/bin/bgutil-pot https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/${POT_VER}/bgutil-pot-linux-x86_64 \
        && chmod +x /usr/local/bin/bgutil-pot \
        && curl -fsSL -o /app/yt-plugins/bgutil.zip https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/${POT_VER}/bgutil-ytdlp-pot-provider-rs.zip) \
    || echo "provedor de PO token nao instalado"

COPY server server
COPY tests/test_creator.py tests/test_creator.py
COPY tests/test_workspace.py tests/test_workspace.py
RUN PYTHONPATH=/app python tests/test_creator.py && PYTHONPATH=/app python tests/test_workspace.py && python -m compileall -q server
COPY --from=webmin /w/web web

ENV DATA_DIR=/tmp/neoncut PORT=8000
EXPOSE 8000
# atualiza o yt-dlp a cada início (as plataformas mudam com frequência)
CMD pip install -q -U "yt-dlp[default]" ; (command -v bgutil-pot >/dev/null && bgutil-pot server --host 127.0.0.1 --port 4416 >/tmp/bgutil.log 2>&1 &) ; uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
