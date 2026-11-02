FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ARG RL_VERSION=dev
ARG RL_BUILD_SHA=
ARG RL_BUILD_DATE=
ENV RL_VERSION=$RL_VERSION RL_BUILD_SHA=$RL_BUILD_SHA RL_BUILD_DATE=$RL_BUILD_DATE

ENV TZ=Australia/Melbourne \
    RL_CONFIG_DIR=/config \
    RL_RECORDINGS_DIR=/recordings \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/recordings"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# A single worker: the recorders live inside the app process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
