"""Minimal GitHub REST client: retries, rate limits and visibility delays."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.github.com"
ATTEMPTS = 5
MAX_DELAY = 60


class ApiError(RuntimeError):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path}: HTTP {status}: {body[:300]}")
        self.status = status


class GitHub:
    def __init__(self, token, sleep=time.sleep):
        if not token:
            raise RuntimeError("Missing GitHub token.")
        self.token = token
        self.sleep = sleep

    def request(self, method, path, body=None, missing_ok=False):
        return self.exchange(method, path, body, missing_ok)[0]

    def exchange(self, method, path, body=None, missing_ok=False):
        """Send a request; return its decoded body and the response headers."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            request = Request(API + "/" + path, data=data, method=method, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "enroll",
            })
            try:
                with urlopen(request, timeout=30) as response:
                    text = response.read().decode()
                    return (json.loads(text) if text else None), response.headers
            except HTTPError as error:
                text = error.read().decode(errors="replace")
                if error.code == 404 and missing_ok:
                    return None, error.headers
                delay = retry_delay(error, attempt)
                if delay is None or last:
                    raise ApiError(method, path, error.code, text) from None
            except (URLError, TimeoutError) as error:
                if last:
                    raise RuntimeError(f"{method} {path}: {error}") from None
                delay = 2 ** attempt
            print(f"Retrying {method} {path} in {delay}s", flush=True)
            self.sleep(delay)

    def get(self, path, missing_ok=False):
        return self.request("GET", path, missing_ok=missing_ok)

    def post(self, path, body):
        return self.request("POST", path, body)

    def put(self, path, body=None):
        return self.request("PUT", path, body or {})

    def patch(self, path, body):
        return self.request("PATCH", path, body)

    def delete(self, path):
        return self.request("DELETE", path, missing_ok=True)

    def pages(self, path):
        sep = "&" if "?" in path else "?"
        page = 1
        while True:
            items = self.get(f"{path}{sep}per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
            page += 1

    def cursor_pages(self, path):
        """Yield every item of a list that pages by cursor, like repos/<repo>/activity, which ignores page=:
        each response's Link header carries the URL of the next page."""
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}per_page=100"
        while path:
            items, headers = self.exchange("GET", path)
            yield from items
            path = next_page(headers.get("Link"))


def next_page(link):
    match = re.search(r'<([^>]*)>;\s*rel="next"', link or "")
    return match[1].removeprefix(API + "/") if match else None


def retry_delay(error, attempt):
    if error.code >= 500:
        return 2 ** attempt
    if error.code in (403, 429):
        after = error.headers.get("retry-after")
        if after:
            return min(int(after), MAX_DELAY)
        if error.headers.get("x-ratelimit-remaining") == "0":
            reset = int(error.headers.get("x-ratelimit-reset", "0"))
            return min(max(reset - int(time.time()), 1), MAX_DELAY)
    return None


def eventually(fetch, sleep=time.sleep, delays=(0, 2, 4, 8, 16, 30)):
    """Retry a read that may 404 for a while after GitHub creates something."""
    for delay in delays:
        if delay:
            sleep(delay)
        try:
            result = fetch()
        except ApiError as error:
            if error.status != 404:
                raise
            result = None
        if result is not None:
            return result
    raise RuntimeError("GitHub did not expose the new resource in time.")
