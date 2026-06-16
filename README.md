# TYCA Teaching Tool MVP

This project is a minimum viable version of a team-facing TYCA question upload tool.

- `frontend/`: static HTML/CSS/JS, suitable for GitHub Pages.
- `server/`: Python backend API, suitable for Aliyun ECS.
- Each teacher logs in, submits their own TYCA cookie, uploads Markdown, reviews the server-generated `tyca-adapter.json`, dry-runs, then explicitly submits.

The default TYCA integration is a safe mock adapter. It proves the product flow. `TYCA_MODE=real` can call the local question assistant project's `tyca_client.py` for dry-run and submit.

## Run Locally

```bash
cd server
cp .env.example .env
python3 app.py
```

Open:

```text
frontend/index.html
```

Set the API base URL in the page to:

```text
http://127.0.0.1:8787
```

Default demo account:

```text
teacher@example.com / change-me
```

Change this before deployment.

## Verify

```bash
python3 -m py_compile server/app.py server/smoke_test.py
python3 server/smoke_test.py
node --check frontend/app.js
```

## Production Notes

- Set a strong `APP_SECRET`.
- Set `CORS_ORIGINS` to the GitHub Pages origin.
- Replace the demo account with real teacher accounts.
- Keep `TYCA_MODE=mock` for platform smoke tests; use `TYCA_MODE=real` only after configuring `TYCA_PROJECT_DIR`.
- Put Aliyun behind HTTPS.

## Deployment Shape

Frontend on GitHub Pages:

```bash
# edit frontend/config.js:
# apiBase: "https://<api-domain.example.com>"
# publish the contents of frontend/
```

Backend on Aliyun ECS:

```bash
cd server
cp .env.example .env
# edit .env:
# APP_SECRET=<long random secret>
# CORS_ORIGINS=https://<your-user>.github.io
# HOST=0.0.0.0
# PORT=8787
# TYCA_MODE=real
# TYCA_PROJECT_DIR=/path/to/录题助手v5.19.11
python3 app.py
```

For a real team deployment, put the Python service behind Nginx or another HTTPS reverse proxy. The browser must call the HTTPS API URL, not the raw private server address.

Deployment templates are in `deploy/`:

- `deploy/server.env.example`
- `deploy/tyca-tool.service`
- `deploy/nginx.tyca-tool.conf`
- `deploy/deploy_server.sh`

Publishing to GitHub Pages and deploying on Aliyun are external side-effect actions. Do those only after confirming the repository, domain, server path, and secrets.

## Real TYCA Integration Boundary

`TYCA_MODE=real` generates `tyca-adapter.json` on the server from uploaded Markdown, then calls:

```text
<TYCA_PROJECT_DIR>/tyca传题请求/tyca_client.py
```

Dry-run does not create remote questions. Submit still requires the frontend confirmation value `CONFIRM_SUBMIT` and then passes `--submit`.

Teachers do not upload `tyca-adapter.json` manually. The page shows the generated adapter for preview and correction; saving corrections sends the edited JSON back to the backend for validation and later dry-run/submit.

Current real-mode MVP limitations:

- Choice-question Markdown with `A.`/`B.` style options and answer lines/tables is converted.
- Reading-program and complete-program sections are converted when the section contains one program code block and answer-marked choice subquestions.
- OJ programming sections are converted when they contain `题目描述`、`输入描述`、`输出描述`、`样例输入`、`样例输出` headings.
- The Markdown parser is intentionally conservative and is not a replacement for the full question assistant AI recognition pipeline. Ambiguous PDFs/images/Word files should still go through the full question assistant recognition and human preview.

## Current API Surface

```text
POST /api/login
GET  /api/me
POST /api/me/tyca-cookie
GET  /api/runs
POST /api/runs
GET  /api/runs/:id
POST /api/runs/:id/dry-run
POST /api/runs/:id/submit
```

`/api/runs/:id/submit` requires:

```json
{"confirm":"CONFIRM_SUBMIT"}
```
