"""Sync Superset roles from the department API.

Each department becomes a role named "<deptcd> - <deptnm>". The deptcd
prefix is the role's identity: a role is matched to its department by
prefix, never by full name, so when deptnm changes upstream the existing
role is renamed in place (same role id -> user assignments and dashboard
grants survive the rename).

Departments missing from the API are left untouched (no deletes).
Stdlib only, so it runs on a stock python image.
"""
import argparse
import http.cookiejar
import json
import logging
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ROLE_NAME_MAX = 64  # ab_role.name is varchar(64)
CODE_PREFIX = re.compile(r"^(\S+) - ")

log = logging.getLogger("dept-role-sync")


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"missing required env var {name}")
    return value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_secrets_files():
    """Load credentials from JSON files (e.g. rendered by Vault Agent).

    SECRETS_FILES is a comma-separated list of paths, each holding a flat JSON
    object like {"SUPERSET_PASSWORD": "..."}. Keys become env vars and
    override values already in the environment.
    """
    for path in filter(None, (p.strip() for p in os.environ.get("SECRETS_FILES", "").split(","))):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit(f"cannot read secrets file {path}: {e}")
        if not isinstance(data, dict):
            sys.exit(f"secrets file {path} must hold a JSON object")
        for key, value in data.items():
            os.environ[key] = str(value)
        log.info("loaded %d keys from %s", len(data), path)


def ssl_context(prefix):
    """TLS settings for one endpoint: <prefix>_INSECURE_SKIP_VERIFY, <prefix>_CA_BUNDLE."""
    if env_bool(f"{prefix}_INSECURE_SKIP_VERIFY"):
        log.warning("%s: TLS certificate verification is DISABLED", prefix)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ca = os.environ.get(f"{prefix}_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def http_timeout():
    return float(os.environ.get("HTTP_TIMEOUT", "30"))


def fetch_departments(url, token):
    headers = {"Accept": "application/json"}
    if token:
        header = os.environ.get("DEPT_API_AUTH_HEADER", "Authorization")
        scheme = os.environ.get("DEPT_API_AUTH_SCHEME", "Bearer")
        headers[header] = f"{scheme} {token}" if scheme else token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout(), context=ssl_context("DEPT_API")) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {url} -> {e.code}: {e.read().decode(errors='replace')[:500]}") from None
    if not body.get("success"):
        raise RuntimeError(f"department API returned success=false: {body.get('message')}")
    return body["data"]["departments"]


def role_name(dept):
    name = f"{str(dept['deptcd']).strip()} - {dept['deptnm'].strip()}"
    if len(name) > ROLE_NAME_MAX:
        log.warning("truncating role name to %d chars: %s", ROLE_NAME_MAX, name)
        name = name[:ROLE_NAME_MAX]
    return name


class Superset:
    def __init__(self, base_url, username, password, provider):
        self.base = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl_context("SUPERSET")),
        )
        token = self._call("POST", "/api/v1/security/login", {
            "username": username, "password": password, "provider": provider, "refresh": False,
        }, auth=False)["access_token"]
        self.headers = {"Authorization": f"Bearer {token}", "Referer": self.base + "/"}
        # CSRF token is bound to a session cookie -- same opener (cookie jar)
        # must be used for this call and every write after it.
        self.headers["X-CSRFToken"] = self._call("GET", "/api/v1/security/csrf_token/")["result"]

    def _call(self, method, path, body=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self.headers)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=http_timeout()) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')}") from None

    def roles(self):
        out, page = [], 0
        while True:
            q = urllib.parse.quote(f"(page:{page},page_size:100)")
            batch = self._call("GET", f"/api/v1/security/roles/?q={q}")["result"]
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def create_role(self, name):
        return self._call("POST", "/api/v1/security/roles/", {"name": name})

    def rename_role(self, role_id, name):
        return self._call("PUT", f"/api/v1/security/roles/{role_id}", {"name": name})


def sync(departments, ss, dry_run):
    by_code = {}
    for role in ss.roles():
        m = CODE_PREFIX.match(role["name"])
        if m:
            by_code.setdefault(m.group(1), []).append(role)

    stats = {"created": 0, "renamed": 0, "unchanged": 0, "skipped": 0}
    seen = set()
    for dept in departments:
        code = str(dept["deptcd"]).strip()
        if code in seen:
            log.warning("deptcd %s appears twice in API response, using first", code)
            stats["skipped"] += 1
            continue
        seen.add(code)
        desired = role_name(dept)
        existing = by_code.get(code, [])

        if len(existing) > 1:
            log.error("deptcd %s matches several roles %s -- fix by hand, skipping",
                      code, [r["name"] for r in existing])
            stats["skipped"] += 1
        elif not existing:
            log.info("create  %s", desired)
            if not dry_run:
                ss.create_role(desired)
            stats["created"] += 1
        elif existing[0]["name"] != desired:
            log.info("rename  [%s] %s -> %s", existing[0]["id"], existing[0]["name"], desired)
            if not dry_run:
                ss.rename_role(existing[0]["id"], desired)
            stats["renamed"] += 1
        else:
            stats["unchanged"] += 1

    log.info("done%s: %s", " (dry run)" if dry_run else "", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report changes without making them")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    load_secrets_files()
    dry_run = args.dry_run or env_bool("DRY_RUN")

    departments = fetch_departments(env("DEPT_API_URL"), os.environ.get("DEPT_API_TOKEN", ""))
    log.info("fetched %d departments", len(departments))
    ss = Superset(env("SUPERSET_URL"), env("SUPERSET_USERNAME"), env("SUPERSET_PASSWORD"),
                  os.environ.get("SUPERSET_AUTH_PROVIDER", "ldap"))
    sync(departments, ss, dry_run)


if __name__ == "__main__":
    main()
