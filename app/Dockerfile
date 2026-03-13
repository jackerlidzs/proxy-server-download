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
    && rm -rf /var/lib/apt/lists/*

# Install curl-impersonate (Chrome version)
# The release archive contains: curl-impersonate-chrome (binary), 
# curl_chrome116 (wrapper script), libcurl-impersonate-chrome.so (library)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        CURL_ARCH="x86_64-linux-gnu"; \
    else \
        CURL_ARCH="aarch64-linux-gnu"; \
    fi && \
    echo "Downloading curl-impersonate for ${CURL_ARCH}..." && \
    wget -q "https://github.com/lwthiker/curl-impersonate/releases/download/v0.6.1/curl-impersonate-v0.6.1.${CURL_ARCH}.tar.gz" \
        -O /tmp/curl-impersonate.tar.gz && \
    mkdir -p /opt/curl-impersonate && \
    tar -xzf /tmp/curl-impersonate.tar.gz -C /opt/curl-impersonate && \
    rm /tmp/curl-impersonate.tar.gz && \
    # List what was extracted for debugging
    ls -la /opt/curl-impersonate/ && \
    # Make all scripts executable
    chmod +x /opt/curl-impersonate/* && \
    # Create symlinks in /usr/local/bin for all binaries
    ln -sf /opt/curl-impersonate/curl-impersonate-chrome /usr/local/bin/curl-impersonate-chrome && \
    # Create curl_chrome symlink (use curl_chrome116 wrapper if exists, else main binary)
    if [ -f /opt/curl-impersonate/curl_chrome116 ]; then \
        ln -sf /opt/curl-impersonate/curl_chrome116 /usr/local/bin/curl_chrome; \
    elif [ -f /opt/curl-impersonate/curl_chrome110 ]; then \
        ln -sf /opt/curl-impersonate/curl_chrome110 /usr/local/bin/curl_chrome; \
    else \
        ln -sf /opt/curl-impersonate/curl-impersonate-chrome /usr/local/bin/curl_chrome; \
    fi && \
    # Setup shared libraries
    echo "/opt/curl-impersonate" > /etc/ld.so.conf.d/curl-impersonate.conf && \
    ldconfig && \
    # Verify installation
    echo "=== curl-impersonate installed ===" && \
    ls -la /usr/local/bin/curl_chrome && \
    curl_chrome --version || echo "Warning: curl_chrome version check failed (may still work)"

ENV LD_LIBRARY_PATH="/opt/curl-impersonate:${LD_LIBRARY_PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static/ ./static/

RUN mkdir -p /downloads

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
