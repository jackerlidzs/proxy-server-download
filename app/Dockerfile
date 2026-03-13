FROM python:3.11-slim

# Install aria2c and dependencies for curl-impersonate
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        aria2 \
        wget \
        ca-certificates \
        libnss3 \
        libnghttp2-14 \
        libbrotli1 \
        libzstd1 \
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
    ln -sf /opt/curl-impersonate/curl_chrome /usr/local/bin/curl_chrome && \
    ln -sf /opt/curl-impersonate/curl-impersonate-chrome /usr/local/bin/curl-impersonate-chrome && \
    rm /tmp/curl-impersonate.tar.gz && \
    # Set library path
    echo "/opt/curl-impersonate" > /etc/ld.so.conf.d/curl-impersonate.conf && \
    ldconfig

# Set library path for curl-impersonate
ENV LD_LIBRARY_PATH="/opt/curl-impersonate:${LD_LIBRARY_PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static/ ./static/

# Create download directory
RUN mkdir -p /downloads

# Verify installations
RUN aria2c --version | head -1 && \
    curl_chrome --version 2>/dev/null | head -1 || echo "curl_chrome installed (version check may not work)"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
