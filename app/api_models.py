"""User-owned APIs, bridged into native OpenCode workers. No paid fallback.

Only an explicit connection test makes an HTTP inference request. Credentials
stay in private, ignored workspace files and never enter a worker's task spec.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import WORKSPACE
from app.db import add_audit, dumps, get_setting, loads, set_setting, utc_now
from app.security import _is_reparse_point, register_secret

IDENT = re.compile(r"^mdapi_[a-f0-9]{16}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
SETTING = "user_api_connections"


class APIModelError(Exception):
    def __init__(self, code, message):
        self.code, self.message, self.details = code, message, {}
        super().__init__(message)


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 500 or any(c.isspace() for c in value):
        raise APIModelError("api_url", "Enter an HTTPS API base URL without credentials or query parameters")
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise APIModelError("api_url", "Invalid API base URL") from None
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or port not in (None, 443):
        raise APIModelError("api_url", "Use an HTTPS API base URL on port 443, without credentials or query parameters")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", u.hostname) or u.hostname.lower() in ("localhost", "localhost.localdomain"):
        raise APIModelError("api_url", "Use the public hostname of your API provider")
    if "{" in value or "}" in value or "\\" in value or ".." in u.path.split("/"):
        raise APIModelError("api_url", "Invalid API base URL")
    try:
        if not ipaddress.ip_address(u.hostname).is_global:
            raise APIModelError("api_url", "Private and loopback API addresses are not supported")
    except ValueError:
        pass
    return f"https://{u.hostname.lower()}{u.path.rstrip('/')}"


def check_public_host(base_url):
    try:
        rows = socket.getaddrinfo(urlsplit(base_url).hostname, 443, type=socket.SOCK_STREAM)
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise APIModelError("api_url", "API hostname must resolve only to public addresses")
    except (socket.gaierror, ValueError):
        raise APIModelError("api_network", "Could not resolve the API hostname") from None


def opencode_executable():
    return os.environ.get("MISSIONDECK_OPENCODE_EXE", "").strip() or shutil.which("opencode")


def native_version():
    exe = opencode_executable()
    if not exe: raise APIModelError("api_native", "OpenCode is required to use this API in Orca workers")
    try:
        r = subprocess.run([exe, "--version"], shell=False, capture_output=True, timeout=15)
        match = re.search(rb"(?:^|\s|v)([12])\.\d+\.\d+", r.stdout[:4096])
        if r.returncode or not match: raise ValueError()
        return int(match[1])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise APIModelError("api_native", "Could not verify the installed OpenCode version") from None


def test_http(connection, key):
    """One bounded request; no automatic retries, redirects or tool execution."""
    check_public_host(connection["base_url"])
    tool = {"name": "missiondeck_connection_check", "description": "Confirm the test connection", "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}
    prompt = "Call missiondeck_connection_check once with ok=true. Do not perform any other action."
    body = {"model": connection["model"], "max_tokens": 128, "messages": [{"role": "user", "content": prompt}]}
    if connection["protocol"] == "anthropic":
        suffix = "/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body["tools"] = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}]
        body["tool_choice"] = {"type": "tool", "name": tool["name"]}
    else:
        suffix = "/chat/completions"
        headers = {"Authorization": "Bearer " + key}
        body["tools"] = [{"type": "function", "function": tool}]
        body["tool_choice"] = {"type": "function", "function": {"name": tool["name"]}}
    try:
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", connection["base_url"] + suffix, headers=headers, json=body) as response:
                if response.status_code != 200:
                    raise APIModelError("api_response", f"API returned HTTP {response.status_code}. Check the base URL, protocol, model and account allowance")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 128 * 1024: raise APIModelError("api_response", "API test response was too large")
        data = json.loads(raw)
        if connection["protocol"] == "anthropic":
            calls = [c for c in data.get("content", []) if c.get("type") == "tool_use" and c.get("name") == tool["name"]]
            ok = any(c.get("input", {}).get("ok") is True for c in calls)
        else:
            calls = data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            ok = any(c.get("function", {}).get("name") == tool["name"] and json.loads(c["function"]["arguments"]).get("ok") is True for c in calls)
        if not ok: raise APIModelError("api_tools", "The model did not return the required tool call. It is not enabled for agent work")
    except APIModelError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Provider errors may echo authentication headers; never surface them.
        raise APIModelError("api_response", "The connection test failed or returned an unsupported response") from None


class APIConnections:
    def __init__(self, conn, workspace=None):
        self.conn = conn
        self.root = Path(workspace or WORKSPACE).resolve()
        self.private = self.root / ".orca-runs" / "private" / "apis"
        self.config = self.root / ".opencode" / "opencode.json"

    def _rows(self):
        return loads(get_setting(self.conn, SETTING), [])

    def _safe_path(self, path):
        try: path.resolve().relative_to(self.root)
        except ValueError: raise APIModelError("api_storage", "API storage path escapes the workspace") from None
        current = path
        while current != self.root:
            if _is_reparse_point(current): raise APIModelError("api_storage", "API storage cannot use symbolic links or reparse points")
            current = current.parent
        return path

    def _atomic(self, path, text):
        self._safe_path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle: handle.write(text)
            os.replace(temp, path)
        finally:
            if temp.exists(): temp.unlink()

    def _key(self, row):
        path = self._safe_path(self.private / row["key_file"])
        try: value = path.read_text(encoding="utf-8")
        except OSError: raise APIModelError("api_key", "Re-enter the API key; its local file is missing") from None
        register_secret(value)
        return value

    def _write_rows(self, rows):
        set_setting(self.conn, SETTING, dumps(rows))
        self.conn.commit()

    def _config_data(self):
        self._safe_path(self.config)
        if not self.config.exists(): return {"$schema": "https://opencode.ai/config.json"}
        try:
            if self.config.stat().st_size > 1024 * 1024: raise ValueError()
            data = json.loads(self.config.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict): raise ValueError()
            return data
        except (ValueError, OSError):
            raise APIModelError("api_config", "Existing .opencode/opencode.json was preserved. It must be valid JSON before adding API routes") from None

    def _entry(self, row, version):
        reference = "{file:" + (self.private / row["key_file"]).as_posix() + "}"
        limits = {"context": row["context_budget"], "output": row["output_budget"]}
        model = {"name": row["model"], "limit": limits}
        if version == 1:
            model["tool_call"] = True
            return {"name": row["name"], "npm": "@ai-sdk/anthropic" if row["protocol"] == "anthropic" else "@ai-sdk/openai-compatible", "options": {"baseURL": row["base_url"], "apiKey": reference}, "models": {row["model"]: model}}
        model["capabilities"] = {"tools": True}
        return {"name": row["name"], "package": "@opencode/ai/providers/" + ("anthropic" if row["protocol"] == "anthropic" else "openai-compatible"), "settings": {"baseURL": row["base_url"], "apiKey": reference}, "models": {row["model"]: model}}

    def _install(self, row, version):
        config = self._config_data()
        for namespace in ("provider", "providers"):
            group = config.get(namespace, {})
            if not isinstance(group, dict): raise APIModelError("api_config", "The existing provider settings have an unsupported shape")
            if row["id"] in group:
                old = next((r for r in self._rows() if r["id"] == row["id"]), None)
                previous = (old or {}).get("installed_entry")
                if previous is None or group[row["id"]] != previous:
                    raise APIModelError("api_config", "This API's native configuration was changed outside MissionDeck. It was preserved")
                del group[row["id"]]
        namespace = "provider" if version == 1 else "providers"
        entry = self._entry(row, version)
        config.setdefault(namespace, {})[row["id"]] = entry
        self._atomic(self.config, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        row["installed_entry"], row["native_version"] = entry, version

    def _matches(self, row):
        if not row.get("verified") or not row.get("installed_entry"): return False
        try:
            data = self._config_data()
            namespace = "provider" if row.get("native_version") == 1 else "providers"
            self._key(row)
            return data.get(namespace, {}).get(row["id"]) == row["installed_entry"]
        except APIModelError: return False

    def public(self):
        items = []
        for r in self._rows():
            ok = self._matches(r)
            items.append({k: r.get(k) for k in ("id", "name", "protocol", "base_url", "model", "context_budget", "output_budget", "checked_at", "last_error")} | {
                "route": r["id"] + "/" + r["model"], "agent": "opencode", "has_key": (self.private / r["key_file"]).is_file(),
                "verified": ok, "status": "enabled" if ok else "needs_test", "billing": "user_supplied_api", "quota": None, "price": None,
            })
        return {"connections": items, "protocols": ["openai", "anthropic"], "inference": False}

    def save(self, body, connection_id=None):
        rows = self._rows()
        old = next((r for r in rows if r["id"] == connection_id), None)
        if connection_id and old is None: raise APIModelError("not_found", "API connection not found")
        if not old and len(rows) >= 8: raise APIModelError("validation", "Use at most eight API model connections")
        name, model, key = body.get("name"), body.get("model"), body.get("api_key", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise APIModelError("validation", "Enter a connection name of 1..80 characters")
        if not isinstance(model, str) or not MODEL.fullmatch(model) or ".." in model.split("/"):
            raise APIModelError("validation", "Enter the exact model ID using letters, numbers, /, :, ., _ or -")
        if body.get("protocol") not in ("openai", "anthropic"): raise APIModelError("validation", "Choose OpenAI-compatible or Anthropic Messages")
        base = normalize_url(body.get("base_url"))
        if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise APIModelError("api_key", "The API key must be a single line without spaces")
        if not key and (not old or old["base_url"] != base): raise APIModelError("api_key", "Enter the API key for this endpoint")
        context, output = body.get("context_budget", 16384), body.get("output_budget", 2048)
        if type(context) is not int or type(output) is not int or not 1024 <= context <= 1000000 or not 128 <= output <= min(context, 32768):
            raise APIModelError("validation", "Use a context budget of 1024..1000000 and an output budget of 128..32768, within context")
        cid = old["id"] if old else "mdapi_" + uuid.uuid4().hex[:16]
        row = {"id": cid, "name": name.strip(), "protocol": body["protocol"], "base_url": base, "model": model,
               "context_budget": context, "output_budget": output, "verified": False, "checked_at": None, "last_error": None,
               "key_file": old["key_file"] if old and not key else cid + "-" + uuid.uuid4().hex[:16] + ".key"}
        if old:
            for k in ("installed_entry", "native_version"): row[k] = old.get(k)
        if key:
            register_secret(key)
            self._atomic(self.private / row["key_file"], key)
        self._write_rows([r for r in rows if r["id"] != cid] + [row])
        if old and key:
            # Existing native entry may still reference the old key until the
            # explicit test reinstalls it. Keep it intact, but mark ineligible.
            pass
        add_audit(self.conn, "api_model_saved", {"id": cid})
        self.conn.commit()
        return self.public()

    def probe(self, cid):
        row = next((r for r in self._rows() if r["id"] == cid), None)
        if row is None: raise APIModelError("not_found", "API connection not found")
        row = dict(row)
        row.update(verified=False, checked_at=utc_now(), last_error=None)
        try:
            version = native_version()
            self._install(row, version)
            # Persist installation ownership even when the test subsequently
            # fails, so retry/delete never overwrite unowned settings.
            self._write_rows([r for r in self._rows() if r["id"] != cid] + [row])
            test_http(row, self._key(row))
            self._probe_native(row)
            row["verified"] = True
        except APIModelError as exc:
            row["last_error"] = exc.message
            self._write_rows([r for r in self._rows() if r["id"] != cid] + [row])
            raise
        self._write_rows([r for r in self._rows() if r["id"] != cid] + [row])
        add_audit(self.conn, "api_model_tested", {"id": cid, "tool_call": True})
        self.conn.commit()
        return self.public()

    def _probe_native(self, row):
        try:
            r = subprocess.run([opencode_executable(), "models", row["id"], "--verbose"], cwd=self.root, shell=False, capture_output=True, timeout=25)
            if r.returncode or len(r.stdout) > 2 * 1024 * 1024: raise ValueError()
            # Exact provider/model label must be discoverable. No raw CLI data,
            # config or credentials are stored in events or returned to clients.
            labels = r.stdout.decode("utf-8", errors="replace").splitlines()
            if row["id"] + "/" + row["model"] not in [line.strip() for line in labels]: raise ValueError()
        except (OSError, TypeError, ValueError, subprocess.TimeoutExpired):
            raise APIModelError("api_native", "The model was not found in OpenCode's project catalog. Check native provider configuration") from None

    def routes(self):
        return [{"id": r["route"], "name": r["name"] + " · " + r["model"], "agent": "opencode", "icon_key": "opencode",
                 "eligible": r["verified"], "billing": r["billing"], "quota": None, "price": None, "source": "user_api",
                 "reason": "Your API · tool-call test passed · cost and quota unknown" if r["verified"] else r["last_error"] or "Save then test this API to enable it"}
                for r in self.public()["connections"]]

    def require(self, model):
        row = next((r for r in self._rows() if r["id"] + "/" + r["model"] == model), None)
        if not row or not self._matches(row): raise APIModelError("model_unverified", "Save and test this exact API model before using it")
        check_public_host(row["base_url"])
        self._probe_native(row)
        return model

    def delete(self, cid):
        rows = self._rows()
        row = next((r for r in rows if r["id"] == cid), None)
        if row is None: raise APIModelError("not_found", "API connection not found")
        config = self._config_data()
        for namespace in ("provider", "providers"):
            group = config.get(namespace, {})
            if cid in group:
                if group[cid] != row.get("installed_entry"):
                    raise APIModelError("api_config", "Native configuration changed outside MissionDeck; it was preserved")
                del group[cid]
        if self.config.exists(): self._atomic(self.config, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        self._write_rows([r for r in rows if r["id"] != cid])
        for path in self.private.glob(cid + "-*.key"):
            self._safe_path(path).unlink(missing_ok=True)
        add_audit(self.conn, "api_model_deleted", {"id": cid})
        self.conn.commit()
        return self.public()
