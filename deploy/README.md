# Deployment Templates

These files prepare the GitHub Pages + Aliyun ECS deployment.

They do not contain real secrets. Do not commit real `.env` files, cookies, TLS keys, or server IPs.

## Files

- `server.env.example`: production backend environment template.
- `tyca-tool.service`: systemd unit template for the Python backend.
- `nginx.tyca-tool.conf`: HTTPS reverse proxy template.
- `deploy_server.sh`: server-side deploy/update script.

## Expected Server Layout

```text
/opt/tyca-tool/
  app/
    server/
    frontend/
  data/
  logs/
  venv/
```

The backend runs on `127.0.0.1:8787`; Nginx exposes HTTPS to the frontend.

## GitHub Pages Setup

1. Commit `.github/workflows/pages.yml` and `frontend/`.
2. In GitHub repository settings, set Pages source to GitHub Actions.
3. Edit `frontend/config.js` before publishing so `apiBase` points to the HTTPS Aliyun API domain.

## Aliyun ECS Setup Outline

Run these steps manually on the server after creating the project user:

```bash
sudo useradd --system --home /opt/tyca-tool --shell /usr/sbin/nologin tyca-tool || true
sudo mkdir -p /opt/tyca-tool/app /opt/tyca-tool/data /opt/tyca-tool/logs
sudo chown -R tyca-tool:tyca-tool /opt/tyca-tool
```

Copy the application code to:

```text
/opt/tyca-tool/app
```

Copy and edit environment:

```bash
sudo cp deploy/server.env.example /opt/tyca-tool/server.env
sudo chmod 600 /opt/tyca-tool/server.env
```

Install systemd unit:

```bash
sudo cp deploy/tyca-tool.service /etc/systemd/system/tyca-tool.service
sudo systemctl daemon-reload
sudo systemctl enable tyca-tool
sudo systemctl start tyca-tool
```

Install Nginx proxy after replacing `<api-domain.example.com>`:

```bash
sudo cp deploy/nginx.tyca-tool.conf /etc/nginx/conf.d/tyca-tool.conf
sudo nginx -t
sudo systemctl reload nginx
```

Do not enable team use until HTTPS is configured and `CORS_ORIGINS` matches the GitHub Pages URL.
