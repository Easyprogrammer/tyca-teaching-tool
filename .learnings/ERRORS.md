# Errors

Command failures and integration errors.

---

## [ERR-20260616-001] server_start_missing_app_secret

**Logged**: 2026-06-16T03:28:27Z
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
Starting the backend without `.env` or `APP_SECRET` fails fast.

### Error
```text
RuntimeError: APP_SECRET must be at least 16 characters
```

### Context
- Command attempted: `python3 server/app.py`
- The MVP intentionally validates `APP_SECRET` at startup.
- Local verification used a temporary environment variable instead of writing a real secret.

### Suggested Fix
Create `server/.env` from `server/.env.example` before manual startup, or pass a temporary `APP_SECRET` in the shell for local testing.

### Metadata
- Reproducible: yes
- Related Files: server/app.py, server/.env.example

---
