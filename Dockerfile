# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAHARA_DATA_DIR=/var/lib/sahara/data \
    SAHARA_COOKIE_SECURE=1 \
    PORT=10000

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates tzdata libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sahara \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sahara
COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY server/requirements.txt ./server/requirements.txt
# O certificado de proxy é opcional para provedores públicos e nunca fica na imagem.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r server/requirements.txt

COPY server ./server
COPY index.html app.js address.js style.css ordering.js ordering.css pwa.js pwa.css service-worker.js system-config.js manifest.webmanifest logo-sahara.jpg esfihas.png admin.html admin.css admin.js ./
COPY images ./images
COPY icons ./icons
RUN find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} +

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/api/health',timeout=3)"
# A inicialização ajusta apenas os dados privados e reduz o processo para UID 10001.
CMD ["python", "-m", "server.run"]
