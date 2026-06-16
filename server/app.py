from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import urllib.request


QUESTION_RE = re.compile(r"(?m)^\s*(?:#{1,4}\s*)?(?:题目|Question)?\s*(\d+)[\.、:\)]\s*(.+)$")
ANSWER_RE = re.compile(r"(?im)^\s*(?:答案|Answer)\s*[:：]\s*(.+)$")
CODE_FENCE_RE = re.compile(r"```")
OPTION_RE = re.compile(r"(?m)^\s*([A-D])[\.、\)]\s*(.+)$")
COOKIE_ASSIGNMENT_RE = re.compile(r"(?i)(cookie|session|token|authorization)([=:])([^;\\s]+)")
SECTION_RE = re.compile(r"(?m)^(#{1,5})\s*(.+?)\s*$")
CRM_API = "https://api-live-class-crm.codemao.cn"
TYCA_LOGIN_URL = "https://internal-account.codemao.cn/login?redirect=https%3A%2F%2Ftyca.codemao.cn%2F"
QRCODE_TIMEOUT_SECONDS = 5 * 60


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(Path(__file__).with_name(".env"))


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    app_secret: str
    cors_origins: set[str]
    data_dir: Path
    tyca_mode: str
    tyca_project_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        app_secret = os.environ.get("APP_SECRET", "")
        if len(app_secret) < 16:
            raise RuntimeError("APP_SECRET must be at least 16 characters")
        data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        origins = {
            item.strip()
            for item in os.environ.get("CORS_ORIGINS", "null").split(",")
            if item.strip()
        }
        return cls(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8787")),
            app_secret=app_secret,
            cors_origins=origins,
            data_dir=data_dir,
            tyca_mode=os.environ.get("TYCA_MODE", "mock"),
            tyca_project_dir=Path(
                os.environ.get("TYCA_PROJECT_DIR", os.path.expanduser("~/Downloads/录题助手v5.19.11"))
            ).resolve(),
        )


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    def __init__(self, db_path: Path, app_secret: str):
        self.db_path = db_path
        self.app_secret = app_secret.encode("utf-8")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  tyca_cookie_encrypted TEXT,
                  tyca_cookie_updated_at INTEGER,
                  created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  file_name TEXT NOT NULL,
                  markdown TEXT NOT NULL,
                  adapter_json TEXT,
                  adapter_validation_json TEXT,
                  review_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  dry_run_json TEXT,
                  submit_json TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """
            )
            self.ensure_column(conn, "runs", "adapter_json", "TEXT")
            self.ensure_column(conn, "runs", "adapter_validation_json", "TEXT")

    def ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def hash_password(self, password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
        return f"pbkdf2_sha256${salt}${base64.b64encode(digest).decode('ascii')}"

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.app_secret, payload, hashlib.sha256).hexdigest()

    def encrypt_cookie(self, cookie: str) -> str:
        data = cookie.encode("utf-8")
        nonce = secrets.token_bytes(16)
        stream = self.key_stream(nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
        nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
        cipher_b64 = base64.urlsafe_b64encode(cipher).decode("ascii")
        mac = self.sign(nonce + cipher)
        return f"v1.{nonce_b64}.{mac}.{cipher_b64}"

    def decrypt_cookie(self, encrypted: str) -> str:
        if encrypted.startswith("v1."):
            return self.decrypt_cookie_v1(encrypted)
        mac, payload = encrypted.split(".", 1)
        cipher = base64.urlsafe_b64decode(payload.encode("ascii"))
        if not hmac.compare_digest(mac, self.sign(cipher)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "cookie signature invalid")
        stream = self.legacy_key_stream(len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")

    def decrypt_cookie_v1(self, encrypted: str) -> str:
        _version, nonce_b64, mac, payload = encrypted.split(".", 3)
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        cipher = base64.urlsafe_b64decode(payload.encode("ascii"))
        if not hmac.compare_digest(mac, self.sign(nonce + cipher)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "cookie signature invalid")
        stream = self.key_stream(nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")

    def key_stream(self, nonce: bytes, length: int) -> bytes:
        output = b""
        counter = 0
        while len(output) < length:
            block = hmac.new(self.app_secret, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            output += block
            counter += 1
        return output[:length]

    def legacy_key_stream(self, length: int) -> bytes:
        output = b""
        counter = 0
        while len(output) < length:
            block = hmac.new(self.app_secret, f"cookie:{counter}".encode("ascii"), hashlib.sha256).digest()
            output += block
            counter += 1
        return output[:length]

    def create_session_for_user(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        created = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), user_id, created, created + 86400 * 7),
            )
        return token

    def find_or_create_teacher_user(self, teacher: dict[str, Any], cookie: str) -> sqlite3.Row:
        teacher_id = str(teacher.get("id") or "unknown")
        email = f"dingtalk-{teacher_id}@teacher.local"
        encrypted = self.encrypt_cookie(cookie.strip())
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET tyca_cookie_encrypted = ?, tyca_cookie_updated_at = ? WHERE id = ?",
                    (encrypted, now(), row["id"]),
                )
                return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            cur = conn.execute(
                """
                INSERT INTO users(email, password_hash, tyca_cookie_encrypted, tyca_cookie_updated_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (email, self.hash_password(secrets.token_urlsafe(32)), encrypted, now(), now()),
            )
            return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

    def auth_user(self, token: str | None) -> sqlite3.Row:
        if not token:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "missing bearer token")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (hash_token(token), now()),
            ).fetchone()
            if not row:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid or expired token")
            return row

    def update_cookie(self, user_id: int, cookie: str) -> None:
        if len(cookie.strip()) < 20:
            raise ApiError(HTTPStatus.BAD_REQUEST, "cookie is too short")
        encrypted = self.encrypt_cookie(cookie.strip())
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET tyca_cookie_encrypted = ?, tyca_cookie_updated_at = ? WHERE id = ?",
                (encrypted, now(), user_id),
            )

    def get_cookie_status(self, user_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT tyca_cookie_encrypted, tyca_cookie_updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return {
            "hasCookie": bool(row and row["tyca_cookie_encrypted"]),
            "updatedAt": row["tyca_cookie_updated_at"] if row else None,
        }

    def require_cookie(self, user_id: int) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT tyca_cookie_encrypted FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row or not row["tyca_cookie_encrypted"]:
            raise ApiError(HTTPStatus.BAD_REQUEST, "TYCA cookie is not configured")
        return self.decrypt_cookie(row["tyca_cookie_encrypted"])

    def create_run(self, user_id: int, file_name: str, markdown: str) -> dict[str, Any]:
        if not file_name.endswith(".md"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "only .md files are accepted in MVP")
        if len(markdown.strip()) < 10:
            raise ApiError(HTTPStatus.BAD_REQUEST, "markdown is too short")
        adapter = build_adapter_from_markdown(file_name, markdown)
        review = review_from_adapter(adapter)
        validation = validate_adapter_for_ui(adapter)
        created = now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs(user_id, file_name, markdown, adapter_json, adapter_validation_json, review_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    file_name,
                    markdown,
                    json.dumps(adapter, ensure_ascii=False, indent=2),
                    json.dumps(validation, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                    "adapter_ready" if validation["ok"] else "adapter_warning",
                    created,
                    created,
                ),
            )
            run_id = cur.lastrowid
        return self.get_run(user_id, run_id)

    def create_adapter_run(self, user_id: int, file_name: str, adapter: dict[str, Any]) -> dict[str, Any]:
        validate_adapter_shape(adapter)
        review = review_from_adapter(adapter)
        validation = validate_adapter_for_ui(adapter)
        created = now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs(user_id, file_name, markdown, adapter_json, adapter_validation_json, review_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    file_name or "tyca-adapter.json",
                    "",
                    json.dumps(adapter, ensure_ascii=False, indent=2),
                    json.dumps(validation, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                    "adapter_ready" if validation["ok"] else "adapter_warning",
                    created,
                    created,
                ),
            )
            run_id = cur.lastrowid
        return self.get_run(user_id, run_id)

    def update_run_adapter(self, user_id: int, run_id: int, adapter: dict[str, Any]) -> dict[str, Any]:
        validate_adapter_shape(adapter)
        review = review_from_adapter(adapter)
        validation = validate_adapter_for_ui(adapter)
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)).fetchone()
            if not row:
                raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
            conn.execute(
                """
                UPDATE runs
                SET adapter_json = ?, adapter_validation_json = ?, review_json = ?, status = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    json.dumps(adapter, ensure_ascii=False, indent=2),
                    json.dumps(validation, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                    "adapter_ready" if validation["ok"] else "adapter_warning",
                    now(),
                    run_id,
                    user_id,
                ),
            )
        return self.get_run(user_id, run_id)

    def get_run(self, user_id: int, run_id: int, include_markdown: bool = False) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)).fetchone()
        if not row:
            raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
        return run_to_dict(row, include_markdown=include_markdown)

    def list_runs(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        return [run_to_dict(row) for row in rows]

    def update_run_result(self, user_id: int, run_id: int, status: str, field: str, result: dict[str, Any]) -> dict[str, Any]:
        if field not in {"dry_run_json", "submit_json"}:
            raise ValueError(field)
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)).fetchone()
            if not row:
                raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
            conn.execute(
                f"UPDATE runs SET status = ?, {field} = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (status, json.dumps(result, ensure_ascii=False), now(), run_id, user_id),
            )
        return self.get_run(user_id, run_id)


class TycaClient:
    def __init__(self, mode: str, data_dir: Path, project_dir: Path):
        self.mode = mode
        self.data_dir = data_dir
        self.project_dir = project_dir

    def dry_run(self, cookie: str, run: dict[str, Any]) -> dict[str, Any]:
        self.assert_cookie(cookie)
        validation = run.get("adapterValidation") or {}
        if validation and not validation.get("ok"):
            errors = validation.get("errors") or []
            message = "Adapter 存在阻断问题，请先在预览区修正后再预演。"
            if errors:
                message += " " + "；".join(str(item) for item in errors[:3])
            raise ApiError(HTTPStatus.BAD_REQUEST, message)
        review = run["review"]
        warnings = list(review.get("warnings", []))
        if self.mode == "real":
            return self.run_real_upload(cookie, run, submit=False)
        if self.mode != "mock":
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported TYCA_MODE: {self.mode}")
        return {
            "mode": "mock",
            "ok": True,
            "message": "Mock dry-run passed. Real TYCA was not contacted.",
            "itemCount": len(review["items"]),
            "warnings": warnings,
        }

    def submit(self, cookie: str, run: dict[str, Any], confirm: str) -> dict[str, Any]:
        self.assert_cookie(cookie)
        if confirm != "CONFIRM_SUBMIT":
            raise ApiError(HTTPStatus.BAD_REQUEST, "explicit confirmation is required")
        if run["status"] != "dry_run_passed":
            raise ApiError(HTTPStatus.BAD_REQUEST, "dry-run must pass before submit")
        if self.mode == "real":
            return self.run_real_upload(cookie, run, submit=True)
        if self.mode != "mock":
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unsupported TYCA_MODE: {self.mode}")
        created = []
        for item in run["review"]["items"]:
            created.append(
                {
                    "localId": item["localId"],
                    "tycaId": f"mock-{run['id']}-{item['index']}",
                    "title": item["title"],
                }
            )
        return {
            "mode": "mock",
            "ok": True,
            "message": "Mock submit finished. Real TYCA was not contacted.",
            "created": created,
        }

    def assert_cookie(self, cookie: str) -> None:
        if len(cookie) < 20:
            raise ApiError(HTTPStatus.BAD_REQUEST, "TYCA cookie appears invalid")

    def run_real_upload(self, cookie: str, run: dict[str, Any], submit: bool) -> dict[str, Any]:
        self.assert_project()
        run_dir = self.data_dir / "runs" / f"run-{run['id']}"
        tyca_dir = run_dir / "tyca"
        markdown_dir = run_dir / "markdown"
        tyca_dir.mkdir(parents=True, exist_ok=True)
        markdown_dir.mkdir(parents=True, exist_ok=True)

        markdown_path = markdown_dir / sanitize_filename(run["fileName"])
        markdown_path.write_text(run["markdown"], encoding="utf-8")
        adapter_path = tyca_dir / "tyca-adapter.json"
        adapter = run.get("adapter") or build_choice_adapter(run)
        adapter_path.write_text(json.dumps(adapter, ensure_ascii=False, indent=2), encoding="utf-8")

        validate_result = self.run_command(
            [
                "python3",
                os.path.expanduser("~/.codex/skills/tyca-question-pipeline/scripts/pipeline.py"),
                "validate",
                "--adapter-json",
                str(adapter_path),
            ],
            cwd=Path.cwd(),
            timeout=30,
        )
        if validate_result.returncode != 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "adapter validation failed: " + sanitize_output(validate_result.stderr))

        output_path = tyca_dir / ("upload-result.json" if submit else "dry-run-result.json")
        cookie_path = tyca_dir / "teacher-cookie.txt"
        cookie_path.write_text(cookie.strip(), encoding="utf-8")
        try:
            cookie_path.chmod(0o600)
        except OSError:
            pass
        cmd = [
            "python3",
            str(self.project_dir / "tyca传题请求" / "tyca_client.py"),
            "--adapter-json",
            str(adapter_path),
            "--cookie-file",
            str(cookie_path),
            "--output",
            str(output_path),
        ]
        if submit:
            cmd.append("--submit")
        upload_result = self.run_command(cmd, cwd=self.project_dir / "tyca传题请求", timeout=120)
        result_payload = {
            "mode": "real",
            "ok": upload_result.returncode == 0,
            "submitted": submit,
            "adapterPath": str(adapter_path),
            "outputPath": str(output_path),
            "itemCount": len(adapter["items"]),
            "stdout": sanitize_output(upload_result.stdout),
            "stderr": sanitize_output(upload_result.stderr),
        }
        if output_path.exists():
            try:
                result_payload["result"] = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result_payload["resultText"] = sanitize_output(output_path.read_text(encoding="utf-8"))
        if upload_result.returncode != 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "TYCA upload command failed: " + sanitize_output(upload_result.stderr or upload_result.stdout))
        return result_payload

    def assert_project(self) -> None:
        required = [
            self.project_dir / "server.py",
            self.project_dir / "tyca传题请求" / "tyca_client.py",
            self.project_dir / "tyca传题请求" / "预演上传.py",
            self.project_dir / "tyca传题请求" / "正式上传.py",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ApiError(HTTPStatus.BAD_REQUEST, "TYCA project is incomplete: " + ", ".join(missing))

    def run_command(self, cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


class QrcodeLoginManager:
    def __init__(self, store: Store):
        self.store = store
        self.sessions: dict[str, dict[str, Any]] = {}
        self.playwright = None
        self.browser = None
        self.tasks: queue.Queue[tuple[Future, Any, tuple[Any, ...]]] = queue.Queue()
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

    def run_on_worker(self, func: Any, *args: Any) -> Any:
        future: Future = Future()
        self.tasks.put((future, func, args))
        try:
            return future.result(timeout=30)
        except FutureTimeoutError:
            raise ApiError(HTTPStatus.REQUEST_TIMEOUT, "扫码服务响应超时，请重试")

    def worker_loop(self) -> None:
        while True:
            future, func, args = self.tasks.get()
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(func(*args))
                except Exception as exc:
                    future.set_exception(exc)

    def start(self) -> dict[str, Any]:
        return self.run_on_worker(self._start)

    def status(self, token: str) -> dict[str, Any]:
        return self.run_on_worker(self._status, token)

    def cancel(self, token: str) -> dict[str, Any]:
        return self.run_on_worker(self._cancel, token)

    def _start(self) -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        browser = self.shared_browser()
        context = browser.new_context(viewport={"width": 420, "height": 560})
        page = context.new_page()
        page.goto(TYCA_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        qrcode = self.screenshot_qrcode(page)
        self.sessions[token] = {"context": context, "page": page, "started_at": now()}
        return {"qrcodeToken": token, "qrcode": qrcode, "expiresIn": QRCODE_TIMEOUT_SECONDS}

    def _status(self, token: str) -> dict[str, Any]:
        state = self.sessions.get(token)
        if not state:
            return {"status": "idle"}
        if now() - int(state["started_at"]) > QRCODE_TIMEOUT_SECONDS:
            self.cleanup(token)
            return {"status": "expired", "error": "二维码已过期，请重新扫码"}

        cookie = self.cookie_string(state["context"])
        if "internal_account_token" not in cookie:
            return {"status": "pending"}

        teacher = fetch_teacher_info(cookie)
        user = self.store.find_or_create_teacher_user(teacher, cookie)
        session_token = self.store.create_session_for_user(int(user["id"]))
        self.cleanup(token)
        return {
            "status": "done",
            "token": session_token,
            "user": public_user(user),
            "teacher": teacher,
            "cookie": self.store.get_cookie_status(int(user["id"])),
        }

    def _cancel(self, token: str) -> dict[str, Any]:
        self.cleanup(token)
        return {"ok": True}

    def shared_browser(self) -> Any:
        if self.browser and self.browser.is_connected():
            return self.browser
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Playwright is not installed: {exc}")
        if not self.playwright:
            self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True, args=["--headless=new"])
        return self.browser

    def screenshot_qrcode(self, page: Any) -> str:
        try:
            frame = page.locator('iframe[src*="login.dingtalk.com"]')
            frame.wait_for(timeout=10000)
            box = frame.bounding_box()
            image = page.screenshot(type="png", clip=box if box else None)
        except Exception:
            image = page.screenshot(type="png")
        return base64.b64encode(image).decode("ascii")

    def cookie_string(self, context: Any) -> str:
        return "; ".join(f"{item['name']}={item['value']}" for item in context.cookies())

    def cleanup(self, token: str) -> None:
        state = self.sessions.pop(token, None)
        if not state:
            return
        try:
            state["context"].close()
        except Exception:
            pass


def parse_markdown(markdown: str) -> dict[str, Any]:
    items = []
    warnings = []
    matches = list(QUESTION_RE.finditer(markdown))
    if not matches:
        title = first_heading(markdown) or "未命名题目"
        matches = [SyntheticMatch(0, title)]

    answer_match = ANSWER_RE.search(markdown)
    answer = answer_match.group(1).strip() if answer_match else ""
    if not answer:
        warnings.append("未识别到答案字段，请在正式上传前补充或确认。")
    if len(CODE_FENCE_RE.findall(markdown)) % 2 != 0:
        warnings.append("Markdown 代码块数量为奇数，可能存在未闭合代码块。")

    for index, match in enumerate(matches, start=1):
        title = match.group(2).strip() if hasattr(match, "group") else match.title
        items.append(
            {
                "localId": f"q{index}",
                "index": index,
                "title": title[:80],
                "type": infer_type(markdown),
                "answer": answer,
                "knowledge": [],
                "difficulty": 3,
                "examDifficulty": default_exam_difficulty(infer_type(markdown)),
                "status": "warning" if warnings else "ready",
                "issues": warnings,
            }
        )
    return {"items": items, "warnings": warnings}


class SyntheticMatch:
    def __init__(self, index: int, title: str):
        self.index = index
        self.title = title

    def group(self, _number: int) -> str:
        return self.title


def first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return None


def infer_type(markdown: str) -> str:
    if re.search(r"(?m)^\s*[A-D][\.、\)]", markdown):
        return "single_choice"
    if "输入" in markdown and "输出" in markdown:
        return "programming"
    return "single_choice"


def default_exam_difficulty(item_type: str) -> str:
    return {
        "reading_program": "1010",
        "multiple_choice": "1030",
        "single_choice": "1040",
        "true_false": "1040",
        "complete_program": "1040",
        "programming": "1050",
    }.get(item_type, "1040")


def validate_adapter_shape(adapter: dict[str, Any]) -> None:
    if not isinstance(adapter, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "adapter must be a JSON object")
    if adapter.get("adapter") != "tyca" or adapter.get("version") != "tyca-v1":
        raise ApiError(HTTPStatus.BAD_REQUEST, "adapter must use adapter=tyca and version=tyca-v1")
    items = adapter.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(HTTPStatus.BAD_REQUEST, "adapter items must be a non-empty array")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"items[{index - 1}] must be an object")
        if not isinstance(item.get("payload"), dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"items[{index - 1}] missing payload object")


def validate_adapter_for_ui(adapter: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    local_ids: set[str] = set()
    type_to_group = {
        "single_choice": "choice",
        "multiple_choice": "choice",
        "true_false": "choice",
        "reading_program": "application",
        "complete_program": "application",
        "programming": "oj",
    }
    for index, item in enumerate(adapter.get("items") or [], start=1):
        label = f"items[{index - 1}]"
        local_id = str(item.get("localId") or "").strip()
        local_type = str(item.get("localType") or "").strip()
        target_group = str(item.get("targetGroup") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if not local_id:
            errors.append(f"{label} 缺少 localId")
        elif local_id in local_ids:
            errors.append(f"{label} localId 重复：{local_id}")
        local_ids.add(local_id)
        if local_type not in type_to_group:
            errors.append(f"{label} localType 不支持：{local_type or '(empty)'}")
        elif target_group != type_to_group[local_type]:
            errors.append(f"{label} targetGroup 应为 {type_to_group[local_type]}")
        if not str(payload.get("name") or "").strip():
            errors.append(f"{label} payload.name 不能为空")
        if safe_int(payload.get("syncOj")) != 1:
            errors.append(f"{label} syncOj 必须为 1")
        if safe_int(payload.get("difficulty")) not in {1, 2, 3, 4, 5}:
            errors.append(f"{label} difficulty 必须为 1-5")
        if not clean_knowledge(payload.get("knowledgeArr")):
            warnings.append(f"{label} knowledgeArr 为空，TYCA 脚本会继续但需要人工确认")
        if target_group == "choice" and len(payload.get("options") or []) < 2:
            errors.append(f"{label} 选择题至少需要 2 个选项")
        if target_group == "choice":
            if not any(option.get("isCorrect") for option in payload.get("options") or [] if isinstance(option, dict)):
                errors.append(f"{label} 选择题缺少正确答案")
            for issue in payload.get("generationIssues") or []:
                errors.append(f"{label} {issue}")
        if target_group == "application":
            if not payload.get("innerQuestionDetails"):
                errors.append(f"{label} 应用题缺少 innerQuestionDetails")
            if not str(payload.get("description") or "").strip():
                errors.append(f"{label} 应用题缺少程序材料")
            for issue in payload.get("generationIssues") or []:
                issue_text = str(issue)
                if "缺少" in issue_text or "未识别" in issue_text:
                    errors.append(f"{label} {issue_text}")
                else:
                    warnings.append(f"{label} {issue_text}")
        if target_group == "oj":
            oj_info = payload.get("ojInfo") if isinstance(payload.get("ojInfo"), dict) else {}
            if not oj_info.get("inputType") or not oj_info.get("outputType") or not oj_info.get("example"):
                errors.append(f"{label} OJ 题缺少输入/输出/样例")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def review_from_adapter(adapter: dict[str, Any]) -> dict[str, Any]:
    validation = validate_adapter_for_ui(adapter)
    items = []
    for index, item in enumerate(adapter.get("items") or [], start=1):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        local_type = str(item.get("localType") or "")
        items.append(
            {
                "localId": str(item.get("localId") or f"q{index}"),
                "index": index,
                "title": str(payload.get("name") or "未命名题目")[:80],
                "type": local_type,
                "answer": answer_summary(item),
                "knowledge": clean_knowledge(payload.get("knowledgeArr")),
                "difficulty": safe_int(payload.get("difficulty"), 3),
                "examDifficulty": str(payload.get("examDifficulty") or default_exam_difficulty(local_type)),
                "status": "ready" if validation["ok"] else "warning",
                "issues": validation["errors"] + validation["warnings"],
            }
        )
    return {"items": items, "warnings": validation["warnings"], "validation": validation}


def answer_summary(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    options = payload.get("options")
    if isinstance(options, list):
        letters = []
        for index, option in enumerate(options):
            if isinstance(option, dict) and option.get("isCorrect"):
                letters.append(chr(65 + index))
        return ",".join(letters)
    inner = payload.get("innerQuestionDetails")
    if isinstance(inner, list):
        answers = []
        for index, question in enumerate(inner, start=1):
            option_details = question.get("optionDetails") if isinstance(question, dict) else []
            letters = []
            if isinstance(option_details, list):
                for option_index, option in enumerate(option_details):
                    if isinstance(option, dict) and option.get("isCorrect"):
                        letters.append(chr(65 + option_index))
            answers.append(f"第{index}小题：" + (",".join(letters) or "待确认"))
        return "；".join(answers)
    return "-"


def clean_knowledge(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_adapter_from_markdown(file_name: str, markdown: str) -> dict[str, Any]:
    items = []
    sections = split_named_sections(markdown)
    consumed_ranges: list[tuple[int, int]] = []
    for section in sections:
        title = section["title"]
        text = section["text"]
        if is_reading_section(title):
            items.extend(build_application_items(text, "reading_program", len(items)))
            consumed_ranges.append(section["range"])
        elif is_complete_section(title):
            items.extend(build_application_items(text, "complete_program", len(items)))
            consumed_ranges.append(section["range"])
        elif is_programming_section(title):
            items.extend(build_programming_items(text, len(items)))
            consumed_ranges.append(section["range"])
    choice_markdown = remove_ranges(markdown, consumed_ranges)
    items.extend(build_choice_items(file_name, choice_markdown, len(items)))
    if not items:
        raise ApiError(HTTPStatus.BAD_REQUEST, "未能从 Markdown 生成任何 TYCA adapter item")
    return {
        "adapter": "tyca",
        "version": "tyca-v1",
        "generatedBy": "teaching-tool-server-rules",
        "items": items,
    }


def build_choice_adapter(run: dict[str, Any]) -> dict[str, Any]:
    return build_adapter_from_markdown(run["fileName"], run.get("markdown", ""))


def split_named_sections(markdown: str) -> list[dict[str, Any]]:
    matches = [
        match
        for match in SECTION_RE.finditer(markdown)
        if len(match.group(1)) <= 3 and is_type_section(match.group(2).strip())
    ]
    sections = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.append({"title": match.group(2).strip(), "text": markdown[match.end():end].strip(), "range": (start, end)})
    return sections


def is_type_section(title: str) -> bool:
    return is_reading_section(title) or is_complete_section(title) or is_programming_section(title)


def is_reading_section(title: str) -> bool:
    return bool(re.search(r"(阅读程序|程序阅读|阅读以下程序)", title))


def is_complete_section(title: str) -> bool:
    return bool(re.search(r"(完善程序|补全程序|程序填空)", title))


def is_programming_section(title: str) -> bool:
    return bool(re.search(r"(OJ\s*题|编程题|程序设计题)", title, flags=re.I))


def remove_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return text
    output = []
    cursor = 0
    for start, end in sorted(ranges):
        output.append(text[cursor:start])
        cursor = end
    output.append(text[cursor:])
    return "\n".join(part for part in output if part.strip())


def build_choice_items(file_name: str, markdown: str, offset: int) -> list[dict[str, Any]]:
    blocks = split_question_blocks(markdown)
    answer_map = extract_answer_map(markdown)
    items = []
    for block_index, block in enumerate(blocks, start=1):
        options = parse_options(block["text"])
        if len(options) < 2:
            continue
        answer = normalize_answer(answer_map.get(str(block["number"])) or extract_inline_answer(block["text"]))
        issues = []
        if not answer:
            issues.append(f"选择题第 {block['number']} 题缺少答案")
        option_letters = set(options.keys())
        if any(letter not in option_letters for letter in answer):
            issues.append(f"选择题第 {block['number']} 题答案 {''.join(sorted(answer))} 不在选项中")
            answer = answer.intersection(option_letters)
        local_type = "multiple_choice" if len(answer) > 1 else "single_choice"
        payload = build_choice_payload(
            name=clean_title(block["title"] or file_name),
            description=strip_answer_lines(block["text"]),
            analysis=extract_analysis(block["text"]),
            options=options,
            answer=answer,
            local_type=local_type,
            local_id=f"q{offset + block_index}",
        )
        payload["generationIssues"] = issues
        items.append({"localId": f"q{offset + block_index}", "localType": local_type, "targetGroup": "choice", "payload": payload})
    return items


def build_choice_payload(name: str, description: str, analysis: str, options: dict[str, str], answer: set[str], local_type: str, local_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": 2 if local_type == "multiple_choice" else 1,
        "syncOj": 1,
        "difficulty": 3,
        "examDifficulty": 1030 if local_type == "multiple_choice" else 1040,
        "contentFormat": 1,
        "languageTypes": [1],
        "audioUrl": "",
        "knowledgeArr": [],
        "sourceDictList": [],
        "description": description.strip(),
        "analysis": analysis,
        "optionsContentType": 4,
        "options": [
            {"text": text, "seq": index, "isCorrect": letter in answer, "uuid": f"{local_id}-{letter.lower()}", "audioUrl": ""}
            for index, (letter, text) in enumerate(options.items())
        ],
    }


def split_question_blocks(markdown: str) -> list[dict[str, Any]]:
    matches = list(QUESTION_RE.finditer(markdown))
    if not matches:
        return [{"number": 1, "title": first_heading(markdown) or "未命名题目", "text": markdown}]
    blocks = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks.append({"number": int(match.group(1)), "title": match.group(2).strip(), "text": markdown[start:end].strip()})
    return blocks


def build_application_items(section_text: str, local_type: str, offset: int) -> list[dict[str, Any]]:
    blocks = split_application_blocks(section_text)
    items = []
    for block_index, block in enumerate(blocks, start=1):
        material = extract_material(block["text"])
        issues = []
        if not material:
            material = infer_application_material(block["text"])
            if material:
                issues.append(f"{block['title']} 未发现 Markdown 代码块，已按普通程序材料兜底识别；请检查行号和代码显示。")
            else:
                issues.append(f"{block['title']} 缺少程序代码块/材料")
        sub_blocks = split_question_blocks(block["text"])
        sub_questions = []
        for sub_index, sub in enumerate(sub_blocks, start=1):
            if "```" in sub["text"]:
                continue
            options = parse_options(sub["text"])
            if len(options) < 2:
                continue
            answer = normalize_answer(extract_inline_answer(sub["text"]))
            if not answer:
                issues.append(f"{block['title']} 第 {sub_index} 小题缺少答案")
                continue
            sub_questions.append(
                {
                    "seq": len(sub_questions),
                    "description": markdown_to_basic_html(strip_answer_lines(remove_options(sub["text"]))),
                    "analysis": markdown_to_basic_html(extract_analysis(sub["text"])),
                    "optionType": 4,
                    "type": 2 if len(answer) > 1 else 1,
                    "optionDetails": [
                        {"text": text, "seq": option_index, "isCorrect": letter in answer, "uuid": f"q{offset + block_index}-s{sub_index}-{letter.lower()}"}
                        for option_index, (letter, text) in enumerate(options.items())
                    ],
                }
            )
        if not sub_questions:
            issues.append(f"{block['title']} 未识别到小题选项")
        payload = {
            "name": clean_title(block["title"]),
            "contentFormat": 0,
            "description": markdown_to_basic_html(material),
            "languageTypes": [1],
            "analysis": "",
            "difficulty": 3,
            "syncOj": 1,
            "examDifficulty": 1010 if local_type == "reading_program" else 1040,
            "sourceDictList": [],
            "knowledgeArr": [],
            "type": 6,
            "audioUrl": "",
            "subType": 1 if local_type == "reading_program" else 2,
            "innerQuestionDetails": sub_questions,
            "generationIssues": issues,
        }
        items.append({"localId": f"q{offset + block_index}", "localType": local_type, "targetGroup": "application", "payload": payload})
    return items


def split_application_blocks(section_text: str) -> list[dict[str, str]]:
    title_match = re.search(r"(?m)^#{4,5}\s*(.+?)\s*$", strip_code_fences_for_heading_scan(section_text))
    title = title_match.group(1).strip() if title_match else ("完善程序" if "____" in section_text else "阅读程序")
    return [{"title": title, "text": section_text}]


def strip_code_fences_for_heading_scan(text: str) -> str:
    return re.sub(r"```[A-Za-z0-9_+-]*\n[\s\S]*?\n```", "", text)


def extract_material(text: str) -> str:
    match = re.search(r"```[A-Za-z0-9_+-]*\n[\s\S]*?\n```", text)
    return match.group(0) if match else ""


def infer_application_material(text: str) -> str:
    explicit = extract_markdown_section(text, ["程序", "代码", "程序代码", "阅读程序", "材料"])
    if explicit:
        return fenced_or_plain_material(explicit)

    codeish_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if codeish_lines and codeish_lines[-1] != "":
                codeish_lines.append("")
            continue
        if stripped.startswith("#") or OPTION_RE.match(stripped) or ANSWER_RE.match(stripped):
            continue
        if re.match(r"(?i)^(解析|analysis|输入|输出|样例|题目描述)\s*[:：]", stripped):
            continue
        if looks_like_code_line(stripped):
            codeish_lines.append(line)

    while codeish_lines and codeish_lines[0] == "":
        codeish_lines.pop(0)
    while codeish_lines and codeish_lines[-1] == "":
        codeish_lines.pop()
    if len([line for line in codeish_lines if line.strip()]) >= 3:
        return "```cpp\n" + "\n".join(codeish_lines) + "\n```"
    return ""


def fenced_or_plain_material(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if "```" in stripped:
        return stripped
    return "```cpp\n" + stripped + "\n```"


def looks_like_code_line(line: str) -> bool:
    if re.search(r"\b(int|long|double|float|char|bool|string|void|for|while|if|else|return|cin|cout|include|using|namespace|main)\b", line):
        return True
    if re.search(r"[{};=<>+\-*/%]|//", line) and not re.match(r"^\d+[\.、:)]", line):
        return True
    return False


def remove_options(text: str) -> str:
    return OPTION_RE.sub("", text).strip()


def markdown_to_basic_html(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    html_blocks = []
    cursor = 0
    for match in re.finditer(r"```([A-Za-z0-9_+-]*)\n([\s\S]*?)\n```", raw):
        before = raw[cursor:match.start()].strip()
        if before:
            html_blocks.append("<p>" + "<br>".join(escape_html_line(line) for line in before.splitlines() if line.strip()) + "</p>")
        language = match.group(1) or "cpp"
        html_blocks.append(f'<pre><code class="language-{escape_attr(language)}">{escape_html_line(match.group(2))}</code></pre>')
        cursor = match.end()
    after = raw[cursor:].strip()
    if after:
        html_blocks.append("<p>" + "<br>".join(escape_html_line(line) for line in after.splitlines() if line.strip()) + "</p>")
    return "\n".join(html_blocks)


def build_programming_items(section_text: str, offset: int) -> list[dict[str, Any]]:
    blocks = split_programming_blocks(section_text)
    items = []
    for block_index, block in enumerate(blocks, start=1):
        content = parse_programming_block(block["text"])
        payload = {
            "name": clean_title(block["title"]),
            "contentFormat": 1,
            "description": content["description"],
            "languageTypes": [1],
            "analysis": content["analysis"],
            "difficulty": 3,
            "syncOj": 1,
            "publicFlag": 1,
            "examDifficulty": 1050,
            "sourceDictList": [],
            "knowledgeArr": [],
            "type": 5,
            "audioUrl": "",
            "ojInfo": {
                "inputType": content["inputDescription"],
                "outputType": content["outputDescription"],
                "example": content["samples"],
                "testData": content["testData"],
                "timeLimit": content["timeLimit"],
                "memoryLimit": content["memoryLimit"],
                "contentLimit": 0,
                "caseCount": len(content["testData"]),
                "dataRange": content["constraints"],
                "testDataType": 0,
            },
            "referenceCode": base64.b64encode(content["referenceCode"].encode("utf-8")).decode("ascii") if content["referenceCode"] else "",
        }
        items.append({"localId": f"q{offset + block_index}", "localType": "programming", "targetGroup": "oj", "payload": payload})
    return items


def split_programming_blocks(section_text: str) -> list[dict[str, str]]:
    field_headings = "题目描述|描述|输入描述|输入格式|输出描述|输出格式|样例输入|样例输入1|样例输出|样例输出1|样例解释|样例说明|数据范围|数据规模|参考代码|参考程序|题解|解析"
    headings = list(re.finditer(rf"(?m)^#{{2,5}}\s+(?!(?:{field_headings})\s*$)(.+?)\s*$", section_text))
    if not headings:
        return [{"title": "OJ 编程题", "text": section_text}]
    blocks = []
    for index, heading in enumerate(headings):
        start = heading.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(section_text)
        blocks.append({"title": heading.group(1).strip(), "text": section_text[start:end].strip()})
    return blocks


def parse_programming_block(text: str) -> dict[str, Any]:
    description = extract_markdown_section(text, ["题目描述", "描述"]) or text.split("## 输入描述")[0].strip()
    input_desc = extract_markdown_section(text, ["输入描述", "输入格式"])
    output_desc = extract_markdown_section(text, ["输出描述", "输出格式"])
    constraints = extract_markdown_section(text, ["数据范围", "数据规模"]) or ""
    analysis = extract_markdown_section(text, ["解析", "题解"]) or ""
    reference_code = extract_markdown_section(text, ["参考代码", "参考程序"]) or ""
    sample_input = extract_code_after_heading(text, ["样例输入", "样例输入1"])
    sample_output = extract_code_after_heading(text, ["样例输出", "样例输出1"])
    sample_explanation = extract_markdown_section(text, ["样例解释", "样例说明"]) or ""
    if not description or not input_desc or not output_desc or sample_input is None or sample_output is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, "OJ 题缺少题目描述、输入描述、输出描述、样例输入或样例输出")
    return {
        "description": description,
        "inputDescription": input_desc,
        "outputDescription": output_desc,
        "constraints": constraints,
        "analysis": analysis,
        "referenceCode": reference_code,
        "samples": [{"in": sample_input, "out": sample_output, "description": sample_explanation}],
        "testData": [],
        "timeLimit": extract_limit(text, "time", 1000),
        "memoryLimit": extract_limit(text, "memory", 256),
    }


def extract_markdown_section(text: str, headings: list[str]) -> str:
    heading_pattern = "|".join(re.escape(item) for item in headings)
    match = re.search(rf"(?ms)^#{{2,5}}\s*(?:{heading_pattern})\s*\n(.*?)(?=^#{{2,5}}\s+|\Z)", text)
    return match.group(1).strip() if match else ""


def extract_code_after_heading(text: str, headings: list[str]) -> str | None:
    section = extract_markdown_section(text, headings)
    if not section:
        return None
    match = re.search(r"```[A-Za-z0-9_+-]*\n([\s\S]*?)\n```", section)
    return match.group(1) + "\n" if match else section.strip() + "\n"


def extract_limit(text: str, kind: str, default: int) -> int:
    if kind == "time":
        match = re.search(r"(?:时间限制|time\s*limit)\s*[:：]?\s*(\d+)", text, flags=re.I)
    else:
        match = re.search(r"(?:空间限制|内存限制|memory\s*limit)\s*[:：]?\s*(\d+)", text, flags=re.I)
    return int(match.group(1)) if match else default


def extract_answer_map(markdown: str) -> dict[str, str]:
    answer_map: dict[str, str] = {}
    for line in markdown.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in {"题号", "编号"}:
            continue
        if len(cells) >= 2 and re.fullmatch(r"\d+", cells[0]) and re.fullmatch(r"[A-D,，、 ]+", cells[1], re.I):
            answer_map[cells[0]] = cells[1]
    for match in re.finditer(r"(?im)^\s*(?:第)?(\d+)\s*(?:题)?\s*(?:答案|Answer)\s*[:：]\s*([A-D,，、 ]+)", markdown):
        answer_map[match.group(1)] = match.group(2)
    return answer_map


def extract_inline_answer(block: str) -> str:
    match = ANSWER_RE.search(block)
    return match.group(1).strip() if match else ""


def normalize_answer(value: str) -> set[str]:
    return {letter.upper() for letter in re.findall(r"[A-D]", value or "", flags=re.I)}


def strip_answer_lines(block: str) -> str:
    return re.sub(r"(?im)^\s*(答案|Answer|解析|Analysis)\s*[:：].*$", "", block).strip()


def extract_analysis(block: str) -> str:
    match = re.search(r"(?im)^\s*(解析|Analysis)\s*[:：]\s*(.+)$", block)
    return match.group(2).strip() if match else ""


def parse_options(markdown: str) -> dict[str, str]:
    options: dict[str, str] = {}
    for match in OPTION_RE.finditer(markdown):
        letter = match.group(1).upper()
        if letter not in options:
            options[letter] = match.group(2).strip()
    return options


def clean_title(value: str) -> str:
    title = re.sub(r"^(题目|Question)?\s*\d+[\.、:\)]\s*", "", str(value or "")).strip()
    return title[:60] or "未命名题目"


def sanitize_filename(value: str) -> str:
    name = Path(value).name or "paper.md"
    return "".join(ch if ch.isalnum() or ch in ".-_" else "-" for ch in name)


def sanitize_output(value: str, limit: int = 12000) -> str:
    redacted = COOKIE_ASSIGNMENT_RE.sub(r"\1\2<hidden>", value or "")
    return redacted[-limit:]


def escape_html_line(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def escape_attr(value: str) -> str:
    return escape_html_line(value).replace('"', "&quot;")


def run_to_dict(row: sqlite3.Row, include_markdown: bool = False) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "fileName": row["file_name"],
        "review": json.loads(row["review_json"]),
        "adapter": json.loads(row["adapter_json"]) if row["adapter_json"] else None,
        "adapterValidation": json.loads(row["adapter_validation_json"]) if row["adapter_validation_json"] else None,
        "status": row["status"],
        "dryRun": json.loads(row["dry_run_json"]) if row["dry_run_json"] else None,
        "submit": json.loads(row["submit_json"]) if row["submit_json"] else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_markdown:
        data["markdown"] = row["markdown"]
    return data


def public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"]}


def fetch_teacher_info(cookie: str) -> dict[str, Any]:
    body = b"{}"
    request = urllib.request.Request(
        f"{CRM_API}/live/teacher/allByAuth",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Accept-Encoding": "gzip, identity",
            "Origin": "https://tyca.codemao.cn",
            "Referer": "https://tyca.codemao.cn/",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw_body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                raw_body = gzip.decompress(raw_body)
            payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise ApiError(HTTPStatus.UNAUTHORIZED, f"扫码登录已完成，但教师身份校验失败：{exc}")
    teachers = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(teachers, list) or not teachers:
        raise ApiError(HTTPStatus.UNAUTHORIZED, "扫码登录已完成，但未获取到教师身份")
    teacher = next((item for item in teachers if item.get("currentTeacherFlag")), teachers[0])
    return {
        "name": teacher.get("teacherName") or "教师",
        "id": teacher.get("internalTeacherId") or teacher.get("teacherId") or "unknown",
    }


def now() -> int:
    return int(time.time())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiHandler(BaseHTTPRequestHandler):
    store: Store
    config: Config
    tyca: TycaClient
    qrcode: QrcodeLoginManager

    def do_OPTIONS(self) -> None:
        self.send_json({"ok": True})

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def route(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if method == "GET" and path == "/health":
                self.send_json({"ok": True, "mode": self.config.tyca_mode})
                return
            if method == "GET" and not path.startswith("/api/"):
                self.serve_frontend(path)
                return
            if method == "POST" and path == "/api/qrcode-login/start":
                self.send_json(self.qrcode.start())
                return
            if method == "GET" and path == "/api/qrcode-login/status":
                token = parse_qs(parsed.query).get("token", [""])[0]
                self.send_json(self.qrcode.status(token))
                return
            if method == "POST" and path == "/api/qrcode-login/cancel":
                body = self.read_json()
                self.send_json(self.qrcode.cancel(str(body.get("token", ""))))
                return

            user = self.store.auth_user(self.bearer_token())
            if method == "GET" and path == "/api/me":
                self.send_json({"user": public_user(user), "cookie": self.store.get_cookie_status(user["id"])})
                return
            if method == "GET" and path == "/api/runs":
                self.send_json({"runs": self.store.list_runs(user["id"])})
                return
            if method == "POST" and path == "/api/runs":
                body = self.read_json(max_bytes=512 * 1024)
                run = self.store.create_run(user["id"], str(body.get("fileName", "")), str(body.get("markdown", "")))
                self.send_json({"run": run}, HTTPStatus.CREATED)
                return

            run_action = re.fullmatch(r"/api/runs/(\d+)/(dry-run|submit)", path)
            if method == "POST" and run_action:
                run_id = int(run_action.group(1))
                action = run_action.group(2)
                run = self.store.get_run(user["id"], run_id, include_markdown=True)
                cookie = self.store.require_cookie(user["id"])
                if action == "dry-run":
                    result = self.tyca.dry_run(cookie, run)
                    self.send_json({"run": self.store.update_run_result(user["id"], run_id, "dry_run_passed", "dry_run_json", result)})
                    return
                body = self.read_json()
                result = self.tyca.submit(cookie, run, str(body.get("confirm", "")))
                self.send_json({"run": self.store.update_run_result(user["id"], run_id, "submitted", "submit_json", result)})
                return

            run_detail = re.fullmatch(r"/api/runs/(\d+)", path)
            if method == "GET" and run_detail:
                self.send_json({"run": self.store.get_run(user["id"], int(run_detail.group(1)))})
                return

            adapter_detail = re.fullmatch(r"/api/runs/(\d+)/adapter", path)
            if method == "POST" and adapter_detail:
                body = self.read_json(max_bytes=1024 * 1024)
                adapter = body.get("adapter")
                if not isinstance(adapter, dict):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "missing adapter object")
                run = self.store.update_run_adapter(user["id"], int(adapter_detail.group(1)), adapter)
                self.send_json({"run": run})
                return

            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception:
            self.send_json({"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_frontend(self, path: str) -> None:
        frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (frontend_dir / relative).resolve()
        if not str(target).startswith(str(frontend_dir.resolve())) or not target.exists() or not target.is_file():
            target = frontend_dir / "index.html"
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid json")

    def bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return auth.removeprefix("Bearer ").strip()

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin", "")
        if origin in self.config.cors_origins or (origin == "null" and "null" in self.config.cors_origins):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        safe_path = self.path.split("?")[0]
        print(json.dumps({"ts": now(), "method": self.command, "path": safe_path, "client": self.client_address[0]}))


def create_server(config: Config) -> ThreadingHTTPServer:
    store = Store(config.data_dir / "app.db", config.app_secret)
    handler = ApiHandler
    handler.store = store
    handler.config = config
    handler.tyca = TycaClient(config.tyca_mode, config.data_dir, config.tyca_project_dir)
    handler.qrcode = QrcodeLoginManager(store)
    return ThreadingHTTPServer((config.host, config.port), handler)


def main() -> None:
    config = Config.from_env()
    server = create_server(config)
    print(f"server listening on http://{config.host}:{config.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
