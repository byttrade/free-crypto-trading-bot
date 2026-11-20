# Free Crypto Trading Bot by BYTtrade: the web panel and the executor in one container.
# Everything that must survive the container (.env with keys, state/, data/) lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH

RUN useradd --create-home --uid 10001 bot \
 && mkdir -p /data /opt/venv /src \
 && chown bot:bot /data /opt/venv /src

# The package is installed into a virtual environment owned by the unprivileged user, so that the bytx auto-update
# works the same way as on a desktop.
USER bot
RUN python -m venv /opt/venv
COPY --chown=bot:bot . /src
RUN pip install --no-cache-dir '/src[live,agent]'

WORKDIR /data
VOLUME /data
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/', timeout=4)"

ENTRYPOINT ["free-crypto-trading-bot"]
# 0.0.0.0 inside the container needs a panel login: create it first with `panel-user` (see docs/guide.md, Docker).
CMD ["panel", "--host", "0.0.0.0"]
