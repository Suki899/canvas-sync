"""Read-only Canvas client with bounded requests and credential isolation."""

from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SyncError(Exception):
    pass


class AuthenticationError(SyncError):
    pass


class UnavailableError(SyncError):
    pass


class BoundedRetry(Retry):
    def get_retry_after(self, response):
        delay = super().get_retry_after(response)
        return min(delay, 60) if delay is not None else None


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SyncError("Canvas and download URLs must use HTTPS without embedded credentials.")
    try:
        return parsed.hostname.lower(), parsed.port or 443
    except ValueError as exc:
        raise SyncError("Invalid URL port.") from exc


def normalize_url(value):
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    origin(value)
    parsed = urlsplit(value)
    if parsed.path or parsed.query or parsed.fragment:
        raise SyncError("Enter only your Canvas site's URL, for example https://canvas.example.edu.")
    return value


class Client:
    def __init__(self, url, token):
        self.url = normalize_url(url)
        self.token = token
        self.session = requests.Session()
        # Do not let .netrc supply credentials to third-party download hosts.
        self.session.trust_env = False
        retries = BoundedRetry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods={"GET"}, respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def close(self):
        self.session.close()

    def get(self, url, *, download=False):
        for _ in range(6):
            same_site = origin(url) == origin(self.url)
            if not download and not same_site:
                raise SyncError("Canvas returned an API link to another site.")
            headers = {"Authorization": f"Bearer {self.token}"} if same_site else {}
            try:
                response = self.session.get(url, headers=headers, timeout=(10, 60),
                                            stream=download, allow_redirects=False)
            except requests.RequestException as exc:
                raise SyncError("Network request failed; check your connection and try again.") from exc
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise SyncError("Canvas returned a redirect without a destination.")
                url = urljoin(url, location)
                continue
            if response.status_code == 401:
                response.close()
                raise AuthenticationError("Canvas rejected your token. Run again with --login.")
            if response.status_code in (403, 404):
                response.close()
                raise UnavailableError("Canvas denied access or the resource is unavailable.")
            if not response.ok:
                status = response.status_code
                response.close()
                raise SyncError(f"Canvas request failed (HTTP {status}).")
            return response
        raise SyncError("Too many redirects from Canvas.")

    def one(self, path):
        with self.get(self.url + path) as response:
            try:
                return response.json()
            except ValueError as exc:
                raise SyncError("Canvas returned an invalid API response.") from exc

    def listing(self, path):
        url = self.url + path + ("&" if "?" in path else "?") + "per_page=100"
        seen = set()
        result = []
        while url:
            if url in seen:
                raise SyncError("Canvas returned a pagination loop.")
            seen.add(url)
            with self.get(url) as response:
                try:
                    page = response.json()
                except ValueError as exc:
                    raise SyncError("Canvas returned an invalid API response.") from exc
                if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                    raise SyncError("Canvas returned an invalid list response.")
                result.extend(page)
                next_url = response.links.get("next", {}).get("url")
                url = urljoin(url, next_url) if next_url else None
        return result
