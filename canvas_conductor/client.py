"""HTTP client for the Canvas REST API.

Responsibilities:
- Bearer-token auth via a persistent `requests.Session`.
- Automatic Link-header pagination on `get_all`.
- Retry with exponential backoff on 429 and 5xx responses.
- Soft rate-limit awareness: when `x-rate-limit-remaining` falls below a
  threshold, sleep briefly before the next request.
- Structured exception mapping for common Canvas error codes.
- Two-step file upload (`upload_file`).
"""
from __future__ import annotations

import json as _json
import sys
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .config import require_credentials
from .exceptions import (
    CanvasAuthError,
    CanvasError,
    CanvasNotFoundError,
    CanvasPermissionError,
    CanvasRateLimitError,
    CanvasServerError,
    CanvasValidationError,
)


DEFAULT_PER_PAGE = 100
RATE_LIMIT_SLEEP_THRESHOLD = 50.0
RATE_LIMIT_SLEEP_SECONDS = 2.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # 1s, 2s, 4s


class CanvasClient:
    def __init__(self, base_url: str, token: str, verbose: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_root = f"{self.base_url}/api/v1"
        self.token = token
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "canvas-conductor/0.1.0",
            }
        )
        self._last_remaining: float | None = None

    # -- URL helpers -----------------------------------------------------

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/api/v1"):
            return f"{self.base_url}{path}"
        if path.startswith("/"):
            return f"{self.api_root}{path}"
        return f"{self.api_root}/{path}"

    # -- Logging / rate limiting -----------------------------------------

    def _log(self, method: str, url: str, status: int, elapsed_ms: float) -> None:
        if not self.verbose:
            return
        remaining = self._last_remaining
        rem_str = f" rem={remaining:.0f}" if remaining is not None else ""
        print(
            f"[canvas] {method} {url} -> {status} ({elapsed_ms:.0f} ms){rem_str}",
            file=sys.stderr,
        )

    def _track_rate_limit(self, response: requests.Response) -> None:
        header = response.headers.get("x-rate-limit-remaining")
        if not header:
            return
        try:
            remaining = float(header)
        except ValueError:
            return
        self._last_remaining = remaining
        if remaining < RATE_LIMIT_SLEEP_THRESHOLD:
            if self.verbose:
                print(
                    f"[canvas] rate-limit budget low ({remaining}); "
                    f"sleeping {RATE_LIMIT_SLEEP_SECONDS}s",
                    file=sys.stderr,
                )
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    # -- Error mapping ---------------------------------------------------

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        if response.ok:
            return
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text

        message = CanvasClient._extract_message(body) or response.reason or "Canvas error"
        status = response.status_code

        if status == 401:
            raise CanvasAuthError(message, status, body)
        if status == 403:
            raise CanvasPermissionError(message, status, body)
        if status == 404:
            raise CanvasNotFoundError(message, status, body)
        if status == 422:
            raise CanvasValidationError(message, status, body)
        if status == 429:
            raise CanvasRateLimitError(message, status, body)
        if 500 <= status < 600:
            raise CanvasServerError(message, status, body)
        raise CanvasError(message, status, body)

    @staticmethod
    def _extract_message(body: Any) -> str | None:
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict) and "message" in first:
                    return str(first["message"])
                return str(first)
            if isinstance(errors, dict):
                # Format like {"name": [{"message": "..."}]}
                for field, problems in errors.items():
                    if isinstance(problems, list) and problems:
                        first = problems[0]
                        if isinstance(first, dict) and "message" in first:
                            return f"{field}: {first['message']}"
            if "message" in body:
                return str(body["message"])
            if "error" in body:
                return str(body["error"])
        if isinstance(body, str) and body:
            # Some Canvas routes (a missing route in particular) answer with a
            # full HTML error page. Dumping 200 characters of `<!DOCTYPE html>`
            # tells the user nothing, so say what actually happened instead.
            if body.lstrip()[:400].lower().lstrip("﻿").startswith(
                ("<!doctype html", "<html")
            ):
                return (
                    "Canvas returned an HTML error page rather than a JSON error. "
                    "This usually means the URL does not match a real API route "
                    "(wrong path, or wrong HTTP method for that path)."
                )
            return body[:200]
        return None

    # -- Core request loop ----------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
        data: Any | None = None,
        files: Any | None = None,
        allow_redirects: bool = True,
        send_auth: bool = True,
    ) -> requests.Response:
        attempt = 0
        last_exc: Exception | None = None
        headers: dict[str, str] | None = None
        if not send_auth:
            # For step-2 uploads we POST to S3-like URLs that must not get our token.
            headers = {"Authorization": ""}  # blank to suppress the session header

        while attempt <= MAX_RETRIES:
            start = time.monotonic()
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    allow_redirects=allow_redirects,
                    headers=headers,
                    timeout=60,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    raise CanvasError(f"Network error: {exc}") from exc
                time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                attempt += 1
                continue

            elapsed_ms = (time.monotonic() - start) * 1000
            self._log(method, url, response.status_code, elapsed_ms)
            self._track_rate_limit(response)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    sleep_for = RETRY_BACKOFF_BASE * (2 ** attempt)
                    if self.verbose:
                        print(
                            f"[canvas] {response.status_code} — retrying in {sleep_for}s",
                            file=sys.stderr,
                        )
                    time.sleep(sleep_for)
                    attempt += 1
                    continue

            return response

        # Should be unreachable: loop either returns or raises.
        raise CanvasError(f"Request failed after retries: {last_exc}")

    # -- Public methods --------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> Any:
        url = self._full_url(path)
        response = self._request("GET", url, params=params)
        self._raise_for_status(response)
        if not response.content:
            return None
        return response.json()

    def get_all(self, path: str, params: dict | None = None) -> list[Any]:
        """Follow `Link: rel="next"` pagination, returning all results."""
        merged_params: dict[str, Any] = {"per_page": DEFAULT_PER_PAGE}
        if params:
            merged_params.update(params)

        results: list[Any] = []
        url: str | None = self._full_url(path)
        first_call = True

        while url:
            response = self._request(
                "GET",
                url,
                params=merged_params if first_call else None,
            )
            self._raise_for_status(response)
            page = response.json()
            if isinstance(page, list):
                results.extend(page)
            else:
                # Some endpoints return a top-level dict; just return as a single item.
                return [page]

            url = self._next_link(response)
            first_call = False

        return results

    @staticmethod
    def _next_link(response: requests.Response) -> str | None:
        link = response.headers.get("Link") or response.headers.get("link")
        if not link:
            return None
        for part in link.split(","):
            section = part.strip()
            if not section.startswith("<"):
                continue
            try:
                url_part, *params = section.split(";")
            except ValueError:
                continue
            url = url_part.strip().lstrip("<").rstrip(">")
            for param in params:
                key, _, value = param.strip().partition("=")
                if key.strip() == "rel" and value.strip().strip('"') == "next":
                    return url
        return None

    def post(
        self,
        path: str,
        data: dict | None = None,
        files: dict | None = None,
        json: Any | None = None,
    ) -> Any:
        url = self._full_url(path)
        if files is not None:
            response = self._request("POST", url, data=data, files=files)
        elif json is not None:
            response = self._request("POST", url, json=json)
        else:
            response = self._request("POST", url, json=data)
        self._raise_for_status(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def put(self, path: str, data: dict | None = None) -> Any:
        url = self._full_url(path)
        response = self._request("PUT", url, json=data)
        self._raise_for_status(response)
        if not response.content:
            return None
        return response.json()

    def delete(
        self,
        path: str,
        params: dict | None = None,
        parse: bool = False,
    ) -> Any:
        """DELETE `path`. Returns `True` on success, or the body if `parse`.

        `params` goes on the query string — Canvas's destructive endpoints
        take their modifiers there (notably `?task=conclude` on
        `DELETE /courses/:id/enrollments/:id`, where the task decides
        whether the record is preserved or destroyed).
        """
        url = self._full_url(path)
        response = self._request("DELETE", url, params=params)
        if response.status_code in (200, 202, 204):
            if not parse:
                return True
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text
        self._raise_for_status(response)
        return False

    # -- File upload (two-step) ------------------------------------------

    def upload_file(
        self,
        course_id: int,
        local_path: str,
        folder_path: str | None = None,
        content_type: str | None = None,
        on_duplicate: str = "rename",
    ) -> dict:
        """Implement Canvas's two-step upload protocol.

        Step 1: POST metadata to `/courses/{cid}/files`. Step 2: POST the
        actual bytes to the returned `upload_url` (do NOT include the bearer
        token on this leg). Canvas redirects back to the API on success.
        """
        from pathlib import Path

        path_obj = Path(local_path)
        if not path_obj.is_file():
            raise CanvasError(f"File not found: {local_path}")

        meta: dict[str, Any] = {
            "name": path_obj.name,
            "size": path_obj.stat().st_size,
            "on_duplicate": on_duplicate,
        }
        if content_type:
            meta["content_type"] = content_type
        if folder_path:
            meta["parent_folder_path"] = folder_path

        step1 = self.post(f"/courses/{course_id}/files", data=meta)
        if not isinstance(step1, dict) or "upload_url" not in step1:
            raise CanvasError(
                "Step 1 of upload failed: no upload_url returned",
                body=step1,
            )

        upload_url = step1["upload_url"]
        upload_params = step1.get("upload_params", {}) or {}

        with path_obj.open("rb") as fh:
            files = {"file": (path_obj.name, fh, content_type or "application/octet-stream")}
            step2 = self._request(
                "POST",
                upload_url,
                data=upload_params,
                files=files,
                allow_redirects=True,
                send_auth=False,
            )

        if step2.status_code >= 400:
            try:
                body = step2.json()
            except ValueError:
                body = step2.text
            raise CanvasError(
                f"Step 2 of upload failed (status {step2.status_code})",
                step2.status_code,
                body,
            )

        try:
            return step2.json()
        except ValueError:
            return {"raw": step2.text, "status": step2.status_code}


def get_client(verbose: bool = False) -> CanvasClient:
    """Build a CanvasClient from the environment configuration."""
    base_url, token = require_credentials()
    return CanvasClient(base_url=base_url, token=token, verbose=verbose)
