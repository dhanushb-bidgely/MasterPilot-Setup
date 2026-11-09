"""REST client for the masterpilot productqa API (target pilot) with retries and token refresh.

Credentials, first match wins:
  MASTERPILOT_TOKEN                         a ready bearer token
  PRODUCTQA_CLIENT_ID + PRODUCTQA_CLIENT_SECRET   minted against the masterpilot host (client_credentials)
Those variables are read from the environment, else from the KEY=VALUE file named by SMB_ENV_FILE, else from the
target's token env_file (targets/<pilot>.json "token.env_file", SMB_AUTOMATION/.env, chmod 600). Nothing secret is printed.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .target import T

BASE_URL = os.environ.get("SMB_BASE_URL", T.base_url)
PILOT = T.pilot


class SetupError(Exception):
    """A precondition or an API call failed: the run stops and says why."""


def _load_env_file() -> None:
    root = Path(__file__).resolve().parent.parent
    env_file = Path(T.token.get("env_file", ".env")).expanduser()
    for p in (os.environ.get("SMB_ENV_FILE"), str(env_file if env_file.is_absolute() else root / env_file)):
        if p and Path(p).expanduser().is_file():
            for line in Path(p).expanduser().read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip('"').strip("'"))
            return


RETRY_STATUS = (502, 503, 504)          # nginx while an api-server instance restarts or is replaced
RETRY_DELAYS = (5, 10, 20, 40, 60, 90, 120)


def mint_token() -> str:
    _load_env_file()
    for var in (T.token.get("env", "MASTERPILOT_TOKEN"), "MASTERPILOT_TOKEN", "BIDGELY_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip().removeprefix("Bearer ")
    cid, sec = os.environ.get("PRODUCTQA_CLIENT_ID"), os.environ.get("PRODUCTQA_CLIENT_SECRET")
    if not (cid and sec):
        raise SetupError("no credentials: export MASTERPILOT_TOKEN, or PRODUCTQA_CLIENT_ID + PRODUCTQA_CLIENT_SECRET "
                         f"(or put them in {T.token.get('env_file', '.env')}, chmod 600)")
    req = urllib.request.Request(f"{BASE_URL}/oauth/token?grant_type=client_credentials&scope=all",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()})
    last: Exception | None = None
    for delay in (*RETRY_DELAYS, None):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_STATUS:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as e:
            last = e
        if delay is not None:
            print(f"  token mint: {last} -- retry in {delay}s", flush=True)
            time.sleep(delay)
    raise SetupError(f"could not mint a token: {last}")


class Api:
    def __init__(self, token: str | None = None):
        self.base, self.token = BASE_URL.rstrip("/"), token or mint_token()

    def call(self, method: str, path: str, body=None, allow: tuple = (), _retried: bool = False, timeout: int = 120,
             retry: bool = True) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        status, text = -1, ""
        for delay in (*RETRY_DELAYS, None):
            req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status, text = r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                status, text = e.code, e.read().decode(errors="replace")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                status, text = -1, str(getattr(e, "reason", e))
            if (status in RETRY_STATUS or status == -1) and delay is not None and retry:
                print(f"  {method} {path}: {'HTTP %s' % status if status > 0 else text} -- retry in {delay}s", flush=True)
                time.sleep(delay)
                continue
            break
        if status == -1:
            raise SetupError(f"{method} {path}: {text}")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"raw": text[:400]}
        if status == 401 and not _retried:
            print(f"  {method} {path}: HTTP 401 -- token rejected (api-server restarted?), minting a new one", flush=True)
            self.token = mint_token()
            return self.call(method, path, body, allow, _retried=True, timeout=timeout, retry=retry)
        if status >= 400 and status not in allow:
            raise SetupError(f"{method} {path} -> HTTP {status}: {text[:500]}".replace(self.token, "***"))
        return {"status": status, "body": data}

    def payload(self, method: str, path: str, body=None, allow: tuple = ()):
        r = self.call(method, path, body, allow)
        b = r["body"]
        return b.get("payload") if isinstance(b, dict) and "payload" in b else b

    # ---- configs / strings ------------------------------------------------------------------------------------------
    def read_config(self, entity: str, eid, ctype: str, query: str = "") -> dict[str, dict]:
        """entities/<user|pilot>/<id>/configs/<type>: {key: {val, configSource}}; the API serves it unwrapped."""
        body = self.call("GET", f"entities/{entity}/{eid}/configs/{ctype}{query}", allow=(400, 404, 500))["body"]
        res = (body.get("payload") if isinstance(body, dict) and "payload" in body else body) or {}
        raw = res.get(ctype) if isinstance(res, dict) else None
        try:
            return {kv["key"]: kv for kv in (json.loads(raw or "{}") or {}).get("kvs") or []}
        except (TypeError, ValueError, AttributeError):
            return {}

    def post_user_config(self, uuid: str, ctype: str, values: dict[str, str], who: str = "smb-kit") -> list[str]:
        """Write user-scope config keys and read them back; returns the keys that did not read back."""
        kvs = [{"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
                "configDocumentation": f"set by {who}"} for k, v in values.items()]
        self.call("POST", f"entities/{uuid}/configs", {"configType": ctype, "configKVs": kvs})
        cur = self.read_config("user", uuid, ctype)
        return [k for k, v in values.items() if str((cur.get(k) or {}).get("val")) != v]

    def put_string(self, entity: str, rid: str, text: str) -> bool:
        body = [{"locale": "en_US", "text": text}]
        r = self.call("PUT", f"2.1/stringResources/{entity}/resource/{rid}", body, allow=(400, 404, 500))
        if r["status"] != 200 or not ((r["body"] or {}).get("payload") or {}).get("status"):   # PUT only updates
            r = self.call("POST", f"2.1/stringResources/{entity}/resource/{rid}", body, allow=(400, 404, 409, 500))
        return r["status"] in (200, 201)
