#!/usr/bin/env python3
"""External acceptance check for phone-control (run from the public internet).

Checks:
  1. GET  https://<host>/control/          without credentials -> HTTP 401 (site Basic Auth)
  2. GET  https://<host>/control/          with credentials    -> HTTP 200, correct page
  3. GET  https://<host>/control/healthz   with credentials    -> HTTP 200, {"ok": true}
  4. WSS  wss://<host>/agent/ws/device?key=WRONG_KEY           -> handshake rejected with HTTP 401
  5. WSS  wss://<host>/agent/ws/device?key=DEVICE_KEY          -> handshake accepted
     NOTE: the server keeps only ONE device; this check kicks any phone currently connected.
  6. WSS  wss://<host>/control/ws/web      with Basic auth     -> handshake accepted, ping -> pong

Credentials come from CLI flags or environment variables:
  WKEELI_USER / WKEELI_PASSWORD   site-wide Basic Auth account
  CONTROL_DEVICE_KEY              phone pairing code (get it on the dev host with:
                                  ssh dev 'grep ^CONTROL_DEVICE_KEY= /opt/phone-control/.env')

Usage:
  python deploy/verify-online.py
  python deploy/verify-online.py --base https://wkeeli.cn --user wkeeli --password '...' \
      --device-key '<hex>'

Exit code 0 = all checks passed.
"""

import argparse
import asyncio
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websockets
except ImportError:  # pragma: no cover - dependency check below
    websockets = None

PAGE_TITLE = "手机控制台"  # noqa: the deployed index.html must contain this
ASSET_HINTS = ('src="./assets/', 'href="./assets/')  # relative paths required (base: './')


# --------------------------------------------------------------------------- HTTP
def http_get(url, auth=None, timeout=20, insecure=False):
    """GET url; returns (status, body_text, headers). HTTP errors are returned, not raised."""
    headers = {}
    if auth:
        token = base64.b64encode(("%s:%s" % auth).encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(url, headers=headers)
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers
    except urllib.error.URLError as exc:
        return None, "connection error: %s" % exc.reason, {}


# ---------------------------------------------------------------------------- WS
def ws_connect_kwargs(headers, insecure=False):
    """Build websockets.connect() kwargs that work across websockets 12..17+."""
    version = getattr(websockets, "__version__", "0")
    try:
        major = int(str(version).split(".")[0])
    except (TypeError, ValueError):
        major = 0
    kwargs = {"open_timeout": 15, "close_timeout": 3}
    if headers:
        if major >= 14:
            kwargs["additional_headers"] = headers
        else:
            kwargs["extra_headers"] = headers
    if major >= 15:
        kwargs["proxy"] = None  # ignore HTTP(S)_PROXY env for the acceptance check
    if insecure:
        kwargs["ssl"] = ssl._create_unverified_context()
    return kwargs


def auth_headers(auth):
    if not auth:
        return None
    token = base64.b64encode(("%s:%s" % auth).encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}


def describe_ws_error(exc):
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    return status, "%s: %s" % (type(exc).__name__, exc)


async def check_wrong_device_key(url, insecure):
    try:
        ws = await websockets.connect(url, **ws_connect_kwargs(None, insecure))
    except Exception as exc:  # noqa: BLE001 - status inspection below
        status, text = describe_ws_error(exc)
        if status == 401:
            return True, "handshake rejected with HTTP 401"
        return False, "expected HTTP 401, got %s (%s)" % (status, text)
    await ws.close()
    return False, "wrong device key was accepted (expected HTTP 401)"


async def check_right_device_key(url, insecure):
    try:
        ws = await websockets.connect(url, **ws_connect_kwargs(None, insecure))
    except Exception as exc:  # noqa: BLE001 - report instead of crashing
        status, text = describe_ws_error(exc)
        return False, "handshake failed: status=%s (%s)" % (status, text)
    async with ws:
        await ws.send(json.dumps({
            "t": "hello", "name": "verify-online", "w": 1080, "h": 2400,
            "accessibility": True, "version": 1,
        }))
        try:
            await asyncio.wait_for(ws.recv(), timeout=2)
        except asyncio.TimeoutError:
            pass
    return True, "device websocket accepted (any connected phone was replaced)"


async def check_browser_ws(url, headers, insecure):
    try:
        ws = await websockets.connect(url, **ws_connect_kwargs(headers, insecure))
    except Exception as exc:  # noqa: BLE001 - report instead of crashing
        status, text = describe_ws_error(exc)
        return False, "handshake rejected: status=%s (%s)" % (status, text)
    async with ws:
        await ws.send(json.dumps({"t": "ping"}))
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "no {\"t\":\"pong\"} within 10s"
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return False, "no {\"t\":\"pong\"} within 10s"
            try:
                message = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(message, dict) and message.get("t") == "pong":
                return True, "handshake + ping/pong OK"


# --------------------------------------------------------------------------- main
def main(argv=None):
    parser = argparse.ArgumentParser(description="External acceptance check for phone-control.")
    parser.add_argument("--base", default="https://wkeeli.cn", help="public base URL")
    parser.add_argument("--user", default=os.environ.get("WKEELI_USER", ""))
    parser.add_argument("--password", default=os.environ.get("WKEELI_PASSWORD", ""))
    parser.add_argument("--device-key", default=os.environ.get("CONTROL_DEVICE_KEY", ""))
    parser.add_argument("--skip-device", action="store_true",
                        help="skip the /agent/ device checks (e.g. you cannot kick the phone now)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (troubleshooting only)")
    args = parser.parse_args(argv)

    if websockets is None:
        print("ERROR: the 'websockets' package is required: python -m pip install websockets==17.4",
              file=sys.stderr)
        return 2
    if not args.user or not args.password:
        print("ERROR: site Basic Auth credentials required (--user/--password or "
              "WKEELI_USER/WKEELI_PASSWORD).", file=sys.stderr)
        return 2
    if not args.skip_device and not args.device_key:
        print("ERROR: --device-key (or CONTROL_DEVICE_KEY) is required for the /agent/ checks "
              "(use --skip-device to skip them).", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("https", "http"):
        print("ERROR: --base must start with https:// (or http:// for local tests)", file=sys.stderr)
        return 2
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_base = "%s://%s" % (ws_scheme, parsed.netloc)
    auth = (args.user, args.password)
    headers = auth_headers(auth)

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok))
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))

    # 1. site-wide Basic Auth still protects /control/
    status, _body, resp_headers = http_get(base + "/control/", insecure=args.insecure)
    www_auth = (resp_headers.get("WWW-Authenticate") or "") if resp_headers else ""
    record("1. /control/ without credentials -> 401",
           status == 401 and "Basic" in www_auth,
           "status=%s WWW-Authenticate=%r" % (status, www_auth))

    # 2. authenticated page loads, correct app, relative asset paths
    status, body, _headers = http_get(base + "/control/", auth=auth, insecure=args.insecure)
    page_ok = status == 200 and PAGE_TITLE in body and any(hint in body for hint in ASSET_HINTS)
    record("2. /control/ with credentials -> 200 + correct page",
           page_ok,
           "status=%s title=%s relative_assets=%s"
           % (status, PAGE_TITLE in body, any(hint in body for hint in ASSET_HINTS)))

    # 3. healthz through the same proxy chain
    status, body, _headers = http_get(base + "/control/healthz", auth=auth, insecure=args.insecure)
    health_ok = status == 200
    try:
        health_ok = health_ok and json.loads(body).get("ok") is True
    except (ValueError, AttributeError):
        health_ok = False
    record("3. /control/healthz -> 200 {\"ok\": true}", health_ok,
           "status=%s body=%r" % (status, body.strip()[:80]))

    if not args.skip_device:
        device_url = ws_base + "/agent/ws/device?key=" + urllib.parse.quote(args.device_key, safe="")
        wrong_url = ws_base + "/agent/ws/device?key=verify-online-wrong-key"

        # 4. wrong key rejected before upgrade
        ok, detail = asyncio.run(check_wrong_device_key(wrong_url, args.insecure))
        record("4. /agent/ws/device?key=wrong -> 401", ok, detail)

        # 5. right key accepted
        ok, detail = asyncio.run(check_right_device_key(device_url, args.insecure))
        record("5. /agent/ws/device?key=<DEVICE_KEY> -> accepted", ok, detail)

    # 6. browser websocket via /control/ (Basic auth, same-origin relative path)
    browser_url = ws_base + "/control/ws/web"
    ok, detail = asyncio.run(check_browser_ws(browser_url, headers, args.insecure))
    record("6. /control/ws/web -> handshake + ping/pong", ok, detail)

    failed = [name for name, ok in results if not ok]
    print()
    print("SUMMARY: %d/%d passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED:")
        for name in failed:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
