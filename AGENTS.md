## Project Rules

This workspace contains a minimum viable teaching research tool for:

- GitHub Pages static frontend
- Aliyun-hosted backend API
- Per-teacher login and TYCA cookie storage
- Markdown upload, review, dry-run, explicit submit flow

## Directory Structure

```text
server/        Backend API, SQLite storage, local run files
frontend/      Static frontend that can be deployed to GitHub Pages
work/          Temporary local experiments
outputs/       User-facing deliverables only
```

Do not place secrets, cookies, uploaded private questions, or generated run data under `frontend/` or `outputs/`.

## Security Rules

- TYCA cookies must never be exposed to the frontend after submission.
- TYCA cookies must never be printed in logs, test output, README examples, or final responses.
- The backend stores cookies encrypted with `APP_SECRET`.
- Production CORS must be restricted to the GitHub Pages origin.
- Formal TYCA submission must require an explicit user action after dry-run/review.
- Every run must be associated with an authenticated teacher account.
- Upload history must record uploader, file name, item count, dry-run state, submit state, and created TYCA IDs when available.

## MVP Boundaries

The MVP implements the account system, cookie update/status, Markdown upload, server-side adapter generation, adapter preview/correction, dry-run, explicit submit, and history.

The included TYCA adapter is intentionally a mock integration unless `TYCA_MODE=real` is implemented and configured. Do not claim real TYCA upload has been verified unless it has been run against TYCA and the results were read back.

## Validation

Before reporting completion after code changes:

```bash
python3 -m py_compile server/app.py server/smoke_test.py
python3 server/smoke_test.py
```

For frontend static syntax smoke:

```bash
node --check frontend/app.js
```
