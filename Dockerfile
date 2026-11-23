# syntax=docker/dockerfile:1
# Ambiente de desenvolvimento: dependências Python do pipeline e da medição do site, ffmpeg, Chromium e Deno.
# gcc e libc6-dev existem só para compilar o webrtcvad, que não tem pacote pronto.
# O código não entra na imagem; monte o repositório em /app ao rodar (veja o README).
FROM python:3.13-slim-trixie

RUN apt update \
    && apt install -y --no-install-recommends chromium ffmpeg gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# O yt-dlp precisa de um runtime de JavaScript para os desafios do YouTube; o Deno é um binário único.
ARG DENO=https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
RUN python -c "import io, sys, urllib.request, zipfile; zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(sys.argv[1], timeout=300).read())).extract('deno', '/usr/local/bin')" "$DENO" \
    && chmod 755 /usr/local/bin/deno \
    && deno --version

# As dependências vêm dos grupos do pyproject.toml; `--group` precisa de pip 25.1 ou mais novo.
COPY pyproject.toml /tmp/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --cache-dir /root/.cache/pip --upgrade pip \
    && pip install --cache-dir /root/.cache/pip --group /tmp/pyproject.toml:dev

# Usuário comum com o mesmo UID de quem monta o repositório, para os arquivos gerados não saírem como root.
ARG UID=1000
RUN useradd --create-home --uid "$UID" dev
USER dev
WORKDIR /app
ENV CHROME=/usr/bin/chromium PYTHONDONTWRITEBYTECODE=1
CMD ["bash"]
