FROM python:3.11-slim

# Install tools: aria2c, curl-impersonate, ffmpeg, 7z, zip, xz
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        aria2 \
        wget \
        ca-certificates \
        libnss3 \
        libnghttp2-14 \
        libbrotli1 \
        ffmpeg \
        p7zip-full \
        zip \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install curl-impersonate (Chrome version)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        CURL_ARCH="x86_64-linux-gnu"; \
    else \
        CURL_ARCH="aarch64-linux-gnu"; \
    fi && \
    wget -q "https://github.com/lwthiker/curl-impersonate/releases/download/v0.6.1/curl-impersonate-v0.6.1.${CURL_ARCH}.tar.gz" \
        -O /tmp/curl-impersonate.tar.gz && \
    mkdir -p /opt/curl-impersonate && \
    tar -xzf /tmp/curl-impersonate.tar.gz -C /opt/curl-impersonate && \
    rm /tmp/curl-impersonate.tar.gz && \
    chmod +x /opt/curl-impersonate/* && \
    ln -sf /opt/curl-impersonate/curl-impersonate-chrome /usr/local/bin/curl-impersonate-chrome && \
    if [ -f /opt/curl-impersonate/curl_chrome116 ]; then \
        ln -sf /opt/curl-impersonate/curl_chrome116 /usr/local/bin/curl_chrome; \
    elif [ -f /opt/curl-impersonate/curl_chrome110 ]; then \
        ln -sf /opt/curl-impersonate/curl_chrome110 /usr/local/bin/curl_chrome; \
    else \
        ln -sf /opt/curl-impersonate/curl-impersonate-chrome /usr/local/bin/curl_chrome; \
    fi && \
    echo "/opt/curl-impersonate" > /etc/ld.so.conf.d/curl-impersonate.conf && \
    ldconfig

ENV LD_LIBRARY_PATH="/opt/curl-impersonate:${LD_LIBRARY_PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application modules
COPY config.py .
COPY database.py .
COPY models.py .
COPY auth.py .
COPY utils.py .
COPY main.py .
COPY routers/ ./routers/
COPY services/ ./services/
COPY static/ ./static/

RUN mkdir -p /downloads /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
