# Download Proxy Server

Download file từ sources cần auth/headers → phục vụ lại qua Nginx cho user download nhanh.

## Quick Deploy

```bash
# 1. Copy thư mục download-proxy lên server
scp -r download-proxy/ user@your-server:~/

# 2. SSH vào server
ssh user@your-server

# 3. Chạy setup
cd download-proxy
chmod +x setup.sh
./setup.sh
```

## Cách dùng

### 1. Submit download (JSON)

```bash
curl -X POST http://YOUR_SERVER:8080/api/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.org/down/downloader/.../file.rar",
    "headers": {
      "referer": "https://dl.example.org/",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
  }'
```

### 2. Submit download (Paste curl command)

```bash
curl -X POST http://YOUR_SERVER:8080/api/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "curl_command": "curl '\''https://example.org/.../file.rar'\'' -H '\''referer: https://dl.example.org/'\'' -H '\''user-agent: Mozilla/5.0...'\''",
    "filename": "my_file.rar"
  }'
```

### 3. Check status

```bash
curl http://YOUR_SERVER:8080/api/status/TASK_ID \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 4. User download file

```
http://YOUR_SERVER:8080/files/my_file.rar
```

Link này share cho ai cũng download được, không cần API key.

### 5. Xem tất cả files

```bash
curl http://YOUR_SERVER:8080/api/files \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 6. Xóa file

```bash
curl -X DELETE http://YOUR_SERVER:8080/api/files/my_file.rar \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## API Docs

Mở trình duyệt: `http://YOUR_SERVER:8080/docs` → Swagger UI tự động.

## Config (.env)

| Variable | Default | Mô tả |
|---|---|---|
| `PORT` | `8080` | Port expose |
| `API_KEY` | `changeme` | API key (set `public` để tắt auth) |
| `SERVER_URL` | `http://localhost:8080` | URL server (cho link download) |
| `MAX_CONNECTIONS` | `16` | Số connection download (aria2c) |
| `CLEANUP_HOURS` | `48` | Tự xóa file sau X giờ |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Số download đồng thời tối đa |

## Quản lý

```bash
# Xem logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Update
docker compose build && docker compose up -d
```
