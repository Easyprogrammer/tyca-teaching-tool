from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import re

os.environ["APP_SECRET"] = "test-secret-value-for-smoke"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tyca-mvp-")
os.environ["HOST"] = "127.0.0.1"
os.environ["PORT"] = "8877"
os.environ["CORS_ORIGINS"] = "null"
os.environ["AI_PARSER_MODE"] = "mock"

from app import Config, KNOWLEDGE_FALLBACK_RULES, Store, create_server  # noqa: E402


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
        for pattern, _tags in KNOWLEDGE_FALLBACK_RULES:
            re.compile(pattern, re.I)
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
        assert run["review"]["items"][0]["knowledge"]
        assert not any("knowledgeArr 为空" in issue for issue in run["review"]["items"][0]["issues"])
        application_run = request(
            "POST",
            "/api/runs",
            {
                "fileName": "reading.md",
                "markdown": (
                    "# 阅读程序\n"
                    "#### 累加程序\n"
                    "int main() {\n"
                    "  int s = 0;\n"
                    "  for (int i = 1; i <= 3; i++) s += i;\n"
                    "  cout << s;\n"
                    "  return 0;\n"
                    "}\n\n"
                    "1. 程序输出是多少？\n"
                    "A. 3\n"
                    "B. 6\n"
                    "C. 9\n"
                    "D. 10\n"
                    "答案：B\n"
                ),
            },
            token,
        )["run"]
        assert application_run["adapter"]
        assert application_run["status"] == "adapter_ready"
        assert not any("knowledgeArr 为空" in warning for warning in application_run["adapterValidation"]["warnings"])
        assert len(application_run["review"]["items"]) == 1
        assert application_run["review"]["items"][0]["knowledge"]
        assert "位运算" not in application_run["review"]["items"][0]["knowledge"]
        assert len(application_run["review"]["items"][0]["issues"]) == 1
        assert all("items[" not in issue for issue in application_run["review"]["items"][0]["issues"])
        missing_answer_run = request(
            "POST",
            "/api/runs",
            {"fileName": "missing-answer.md", "markdown": "# 题目1. 缺答案题\nA. 1\nB. 2\n"},
            token,
        )["run"]
        assert missing_answer_run["adapter"]
        assert missing_answer_run["status"] == "adapter_warning"
        assert missing_answer_run["adapterValidation"]["errors"]
        assert missing_answer_run["review"]["items"][0]["issues"] == [
            "选择题缺少正确答案",
            "选择题第 1 题缺少答案",
        ]
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
        app_source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        assert ".codex/skills" not in app_source
        assert "pipeline.py" not in app_source
        print("smoke test passed")
    finally:
        server.shutdown()
        Path(os.environ["DATA_DIR"]).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
