from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ["APP_SECRET"] = "test-secret-value-for-smoke"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tyca-mvp-")
os.environ["HOST"] = "127.0.0.1"
os.environ["PORT"] = "8877"
os.environ["CORS_ORIGINS"] = "null"

from app import Config, Store, create_server  # noqa: E402


BASE = "http://127.0.0.1:8877"


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    config = Config.from_env()
    store = Store(Path(os.environ["DATA_DIR"]) / "crypto-check.db", config.app_secret)
    encrypted_one = store.encrypt_cookie("sessionid=abcdefghijklmnopqrstuvwxyz")
    encrypted_two = store.encrypt_cookie("sessionid=abcdefghijklmnopqrstuvwxyz")
    assert encrypted_one != encrypted_two
    assert store.decrypt_cookie(encrypted_one) == "sessionid=abcdefghijklmnopqrstuvwxyz"

    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        assert request("GET", "/health")["ok"] is True
        store = server.RequestHandlerClass.store
        teacher = {"id": "smoke", "name": "Smoke Teacher"}
        user = store.find_or_create_teacher_user(teacher, "sessionid=abcdefghijklmnopqrstuvwxyz")
        token = store.create_session_for_user(user["id"])
        me = request("GET", "/api/me", token=token)
        assert me["cookie"]["hasCookie"] is True
        run = request(
            "POST",
            "/api/runs",
            {"fileName": "sample.md", "markdown": "# 题目1. A+B\nA. 1\nB. 2\n答案：A\n"},
            token,
        )["run"]
        assert run["review"]["items"]
        dry = request("POST", f"/api/runs/{run['id']}/dry-run", token=token)["run"]
        assert dry["status"] == "dry_run_passed"
        submitted = request(
            "POST",
            f"/api/runs/{run['id']}/submit",
            {"confirm": "CONFIRM_SUBMIT"},
            token,
        )["run"]
        assert submitted["status"] == "submitted"
        assert submitted["submit"]["created"]
        print("smoke test passed")
    finally:
        server.shutdown()
        Path(os.environ["DATA_DIR"]).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
