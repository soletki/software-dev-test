#!/usr/bin/env python3

from __future__ import annotations

import argparse
import logging
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest


PRIMARY_URL = "https://jsonplaceholder.typicode.com/todos"
FALLBACK_URL = "https://dummyjson.com/todos"

# Counts how many times primary backend failed and fallback flow was activated.
FALLBACK_TRIGGERED_TOTAL = Counter(
    "todo_fallback_triggered_total",
    "Number of times fallback backend was triggered.",
)
TODOS_REQUESTS_TOTAL = Counter(
    "todo_requests_total",
    "Number of /todos requests by status.",
    ["status"],
)
LOGGER = logging.getLogger("todo_fallback")


@dataclass
class Todo:
    id: int
    title: str
    completed: bool
    user_id: int | None
    source: str


class BackendError(RuntimeError):
    """Raised when a backend request fails or returns invalid data."""


class TodoFallbackClient:
    def __init__(
        self,
        primary_url: str = PRIMARY_URL,
        fallback_url: str = FALLBACK_URL,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout_seconds = timeout_seconds

    def get_todos(self, limit: int = 10) -> list[Todo]:
        """Fetch TODOs from primary backend, then fallback on failure."""
        try:
            return self._fetch_from_primary(limit=limit)
        except BackendError as primary_error:
            FALLBACK_TRIGGERED_TOTAL.inc()
            self._log_fallback(primary_error=primary_error, limit=limit)
            try:
                return self._fetch_from_fallback(limit=limit)
            except BackendError as fallback_error:
                raise BackendError(
                    "Both backends failed. "
                    f"Primary error: {primary_error}. "
                    f"Fallback error: {fallback_error}."
                ) from fallback_error

    def _log_fallback(self, primary_error: BackendError, limit: int) -> None:
        LOGGER.info(
            json.dumps(
                {
                    "event": "fallback_triggered",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "limit": limit,
                    "primary_url": self.primary_url,
                    "fallback_url": self.fallback_url,
                    "primary_error": str(primary_error),
                }
            )
        )

    def _fetch_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                # Some public APIs reject requests without a user agent.
                "User-Agent": "python-fallback-client/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise BackendError(f"HTTP {response.status} from {url}")
                raw_payload = response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as exc:
            raise BackendError(f"Request failed for {url}: {exc}") from exc

        try:
            return json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise BackendError(f"Invalid JSON from {url}: {exc}") from exc

    def _fetch_from_primary(self, limit: int) -> list[Todo]:
        query = urllib.parse.urlencode({"_limit": limit})
        url = f"{self.primary_url}?{query}"
        payload = self._fetch_json(url)

        if not isinstance(payload, list):
            raise BackendError("Primary backend returned non-list payload")

        todos: list[Todo] = []
        for item in payload:
            try:
                todos.append(
                    Todo(
                        id=int(item["id"]),
                        title=str(item["title"]),
                        completed=bool(item["completed"]),
                        user_id=int(item["userId"]) if item.get("userId") is not None else None,
                        source="jsonplaceholder",
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BackendError(f"Invalid todo item from primary backend: {item}") from exc
        return todos

    def _fetch_from_fallback(self, limit: int) -> list[Todo]:
        query = urllib.parse.urlencode({"limit": limit})
        url = f"{self.fallback_url}?{query}"
        payload = self._fetch_json(url)

        if not isinstance(payload, dict) or "todos" not in payload:
            raise BackendError("Fallback backend returned invalid payload")

        todos_list = payload["todos"]
        if not isinstance(todos_list, list):
            raise BackendError("Fallback backend 'todos' field is not a list")

        todos: list[Todo] = []
        for item in todos_list:
            try:
                todos.append(
                    Todo(
                        id=int(item["id"]),
                        title=str(item["todo"]),
                        completed=bool(item["completed"]),
                        user_id=int(item["userId"]) if item.get("userId") is not None else None,
                        source="dummyjson",
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BackendError(f"Invalid todo item from fallback backend: {item}") from exc
        return todos


class AppRequestHandler(BaseHTTPRequestHandler):
    fallback_client: TodoFallbackClient
    default_limit: int = 5

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/todos":
            self._handle_todos(parsed.query)
            return
        if parsed.path == "/metrics":
            self._handle_metrics()
            return
        self._send_json(404, {"error": "Not found"})

    def _handle_todos(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string)

        limit = self.default_limit
        if "limit" in params:
            raw_limit = params["limit"][0]
            try:
                limit = int(raw_limit)
            except ValueError:
                TODOS_REQUESTS_TOTAL.labels(status="bad_request").inc()
                self._send_json(400, {"error": "Query parameter 'limit' must be an integer"})
                return

        if limit < 1:
            TODOS_REQUESTS_TOTAL.labels(status="bad_request").inc()
            self._send_json(400, {"error": "Query parameter 'limit' must be >= 1"})
            return

        force_fallback = _is_truthy(params.get("force_fallback", ["false"])[0])
        client = self.fallback_client
        if force_fallback:
            client = TodoFallbackClient(
                primary_url="https://jsonplaceholder.typicode.com/this-endpoint-does-not-exist",
                fallback_url=self.fallback_client.fallback_url,
                timeout_seconds=self.fallback_client.timeout_seconds,
            )

        try:
            todos = client.get_todos(limit=limit)
        except BackendError as exc:
            TODOS_REQUESTS_TOTAL.labels(status="error").inc()
            self._send_json(502, {"error": str(exc)})
            return

        TODOS_REQUESTS_TOTAL.labels(status="success").inc()
        self._send_json(200, [asdict(todo) for todo in todos])

    def _handle_metrics(self) -> None:
        payload = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status_code: int, payload: Any) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _is_truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _configure_fallback_logger(logs_dir: str) -> Path:
    logs_path = Path(logs_dir).resolve()
    logs_path.mkdir(parents=True, exist_ok=True)
    log_file = logs_path / "fallback_events.jsonl"

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(file_handler)
    return log_file


def _build_handler(client: TodoFallbackClient, default_limit: int) -> type[AppRequestHandler]:
    return type(
        "ConfiguredAppRequestHandler",
        (AppRequestHandler,),
        {"fallback_client": client, "default_limit": default_limit},
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TODO fallback API server with /todos and /metrics endpoints."
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host/interface to bind server to.")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP server (serves both /todos and /metrics).",
    )
    parser.add_argument(
        "--default-limit",
        type=int,
        default=5,
        help="Default number of TODOs returned by /todos when 'limit' query is not provided.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="HTTP timeout for backend calls.",
    )
    parser.add_argument(
        "--logs-dir",
        default="logs/fallback",
        help="Directory for fallback JSON logs.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.port < 1 or args.port > 65535:
        raise SystemExit("--port must be in range 1..65535")
    if args.default_limit < 1:
        raise SystemExit("--default-limit must be >= 1")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be > 0")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log_file = _configure_fallback_logger(args.logs_dir)

    client = TodoFallbackClient(timeout_seconds=args.timeout_seconds)
    handler = _build_handler(client=client, default_limit=args.default_limit)

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Server running at http://{args.host}:{args.port}")
    print("Endpoints: /todos, /metrics")
    print(f"Fallback JSON logs: {log_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
