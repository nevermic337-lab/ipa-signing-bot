FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git g++ make pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 https://github.com/zhlynn/zsign.git
RUN cd /opt/zsign/build/linux && make clean && make && \
    find /opt/zsign -type f -name zsign -executable -print -quit | \
    xargs -r -I{} cp {} /usr/local/bin/zsign && \
    test -x /usr/local/bin/zsign

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY bot.py .
ENV ZSIGN_PATH=/usr/local/bin/zsign
CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 700 --bind 0.0.0.0:${PORT:-10000} bot:app"]
