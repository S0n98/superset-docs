"""Minimal Superset REST client (stdlib only), shared by the scripts here.

Env: SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD,
SUPERSET_AUTH_PROVIDER (default ldap), SUPERSET_HOST_HEADER (optional, for
reaching Superset through an ingress by IP), SUPERSET_INSECURE_SKIP_VERIFY.
"""
import http.cookiejar
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request


class SupersetError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status


class Superset:
    def __init__(self, url=None, username=None, password=None, provider=None, host_header=None):
        self.base = (url or os.environ["SUPERSET_URL"]).rstrip("/")
        self.host_header = host_header if host_header is not None else os.environ.get("SUPERSET_HOST_HEADER")
        ctx = ssl.create_default_context()
        if os.environ.get("SUPERSET_INSECURE_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.headers = {}
        token = self.call("POST", "/api/v1/security/login", {
            "username": username or os.environ["SUPERSET_USERNAME"],
            "password": password or os.environ["SUPERSET_PASSWORD"],
            "provider": provider or os.environ.get("SUPERSET_AUTH_PROVIDER", "ldap"),
            "refresh": False,
        })["access_token"]
        self.headers = {"Authorization": f"Bearer {token}", "Referer": self.base + "/"}
        # The CSRF token is bound to the session cookie: same opener for all calls.
        self.headers["X-CSRFToken"] = self.call("GET", "/api/v1/security/csrf_token/")["result"]

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", **self.headers}
        if self.host_header:
            headers["Host"] = self.host_header
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise SupersetError(e.code, e.read().decode(errors="replace")[:500]) from None

    def list(self, resource, filters=None):
        """All rows of /api/v1/<resource>/, optionally filtered [(col, opr, value)]."""
        out, page = [], 0
        while True:
            q = {"page": page, "page_size": 100}
            if filters:
                q["filters"] = [{"col": c, "opr": o, "value": v} for c, o, v in filters]
            rison = to_rison(q)
            rows = self.call("GET", f"/api/v1/{resource}/?q={urllib.parse.quote(rison)}")["result"]
            out.extend(rows)
            if len(rows) < 100:
                return out
            page += 1


def to_rison(obj):
    """Tiny rison encoder, enough for Superset list queries."""
    if isinstance(obj, bool):
        return "!t" if obj else "!f"
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, str):
        if obj and all(c.isalnum() or c in "-_./~" for c in obj) and not obj[0].isdigit() and obj[0] != "-":
            return obj
        return "'" + obj.replace("!", "!!").replace("'", "!'") + "'"
    if isinstance(obj, list):
        return "!(" + ",".join(to_rison(x) for x in obj) + ")"
    if isinstance(obj, dict):
        return "(" + ",".join(f"{k}:{to_rison(v)}" for k, v in obj.items()) + ")"
    raise TypeError(type(obj))
