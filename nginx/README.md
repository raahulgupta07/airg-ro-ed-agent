# Nginx Reverse Proxy

Nginx terminates TLS and proxies to the FastAPI `app` service over the internal docker network. The `app` service no longer exposes port 9000 to the host — all traffic must enter via nginx (80 → 443).

## Layout

```
nginx/
  conf.d/default.conf   server blocks (HTTP redirect + HTTPS proxy)
  ssl/                  cert.pem + key.pem (gitignored)
  logs/                 access.log + error.log (gitignored)
```

## Local Development (self-signed cert)

```bash
bash scripts/generate_dev_cert.sh
docker compose up -d
```

Then open `https://localhost/`. The browser will warn about the self-signed cert — click **Advanced → Proceed**.

The cert is valid for `localhost`, `ro-ed.local`, and `127.0.0.1` for 365 days. Re-run the script (or delete `nginx/ssl/cert.pem`) to regenerate.

## Production (Let's Encrypt)

```bash
# 1. Point DNS A record at server
# 2. Stop nginx, run certbot in standalone mode:
#    docker run --rm -p 80:80 -v $(pwd)/nginx/ssl:/etc/letsencrypt certbot/certbot certonly --standalone -d your.domain.com
# 3. Update default.conf paths to fullchain.pem / privkey.pem
# 4. Add renewal cron
```

Sample renewal cron (every Sunday 03:00):

```cron
0 3 * * 0 cd /opt/ro-ed && docker run --rm -p 80:80 -v $(pwd)/nginx/ssl:/etc/letsencrypt certbot/certbot renew --standalone && docker compose restart nginx
```

When using Let's Encrypt, point `default.conf` at the real cert paths:

```nginx
ssl_certificate     /etc/nginx/ssl/live/your.domain.com/fullchain.pem;
ssl_certificate_key /etc/nginx/ssl/live/your.domain.com/privkey.pem;
```

## Notes

- `client_max_body_size 60M` matches PDF upload limits.
- `proxy_read_timeout 600s` keeps V11 SSE streams alive for 5+ minute extractions.
- `proxy_buffering off` is required for SSE — without it, events get buffered and the live router stream stalls.
- HSTS is enabled (`max-age=31536000`). If you ever need to roll back to HTTP, clear HSTS in browser first.
