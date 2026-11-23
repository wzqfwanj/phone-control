#!/usr/bin/env python3
"""Patch production config for phone-control, idempotently.

Run this ON the target host (it edits a file on that host):

    # aliyun (Caddy reverse proxy)
    python3 patch-online.py caddy [--file /etc/caddy/Caddyfile] [--dry-run]

    # dev (frp client tunnel)
    python3 patch-online.py frpc  [--file /opt/frp/frpc.toml] [--dry-run]

What `caddy` changes in /etc/caddy/Caddyfile (inside the `wkeeli.cn { ... }` block):
  1. adds `not path /agent/*` to the existing `@not_local` block, so the phone device
     WebSocket channel bypasses the site-wide Basic Auth (the pairing code still guards it)
  2. inserts `handle_path /control/*` + `handle /agent/*` reverse proxies to
     127.0.0.1:18090 (flush_interval -1) right before the fallback
     `reverse_proxy localhost:5000`
  3. backs the file up to <file>.bak-phone-control-<timestamp>, validates with
     `caddy validate --config <file>`, and restores the backup if validation fails.

What `frpc` changes in /opt/frp/frpc.toml:
  1. appends the `[[proxies]] name = "phone-control"` tcp block (8090 -> 18090)
  2. backs up the file, verifies with `frpc verify -c <file>`, restores the backup
     if verification fails (it also checks the ports are not used by another proxy).

--dry-run only prints a unified diff and validates a temp copy; the real file is untouched.
This script NEVER reloads or restarts services; it prints the next command to run.
Running it twice is safe: the second run reports "already patched" and changes nothing.
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

TAB = chr(9)

# --- Caddy patch anchors -----------------------------------------------------
NOT_LOCAL_PATH_ANCHOR = "not path /context/s/*"   # last `not path` line in @not_local
AGENT_NOT_LOCAL_LINE = "not path /agent/*"
FALLBACK_LINE = "reverse_proxy localhost:5000"     # the site-wide fallback handler
CONTROL_MARKER = "handle_path /control/*"
AGENT_MARKER = "handle /agent/*"

CADDY_BLOCK_ROWS = [
    (0, "# ===== 手机远程控制（dev 机 127.0.0.1:18090 ← frp 隧道）====="),
    (0, "# /control/ 电脑端控制台：handle_path 剥前缀，走全站 Basic Auth"),
    (0, "# /agent/  手机设备 WebSocket：不剥前缀、免全站登录，由设备配对码鉴权"),
    (0, "redir /control /control/ 308"),
    (0, CONTROL_MARKER + " {"),
    (1, "reverse_proxy 127.0.0.1:18090 {"),
    (2, "flush_interval -1"),
    (1, "}"),
    (0, "}"),
    (0, ""),
    (0, AGENT_MARKER + " {"),
    (1, "reverse_proxy 127.0.0.1:18090 {"),
    (2, "flush_interval -1"),
    (1, "}"),
    (0, "}"),
]

# --- frpc patch --------------------------------------------------------------
FRPC_MARKER = 'name = "phone-control"'
FRPC_ROWS = [
    "# 手机远程控制：dev 主机 127.0.0.1:8090 ← frp → aliyun 127.0.0.1:18090",
    "[[proxies]]",
    'name = "phone-control"',
    'type = "tcp"',
    'localIP = "127.0.0.1"',
    "localPort = 8090",
    "remotePort = 18090",
]


class PatchError(Exception):
    pass


def indent_of(line):
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)]


def find_line(lines, stripped_value):
    return next((i for i, ln in enumerate(lines) if ln.strip() == stripped_value), None)


# --------------------------------------------------------------------- caddy
def patch_caddy(text):
    """Return (patched_text, list_of_changes). Raises PatchError when it cannot patch safely."""
    changes = []
    lines = text.splitlines(keepends=True)

    # 1. `not path /agent/*` inside @not_local
    if AGENT_NOT_LOCAL_LINE not in "".join(lines):
        idx = find_line(lines, NOT_LOCAL_PATH_ANCHOR)
        if idx is None:
            raise PatchError(
                "cannot find a line 'not path /context/s/*' inside the @not_local block; "
                "add 'not path /agent/*' by hand (see deploy/Caddyfile.snippet)"
            )
        lines.insert(idx + 1, indent_of(lines[idx]) + AGENT_NOT_LOCAL_LINE + "\n")
        changes.append("insert '" + AGENT_NOT_LOCAL_LINE + "' into @not_local")

    # 2. /control/* + /agent/* handlers before the fallback reverse_proxy
    if AGENT_MARKER not in "".join(lines):
        if CONTROL_MARKER in "".join(lines):
            raise PatchError(
                "found '%s' but no '%s' (partially patched?); finish the edit by hand."
                % (CONTROL_MARKER, AGENT_MARKER)
            )
        idx = find_line(lines, FALLBACK_LINE)
        if idx is None:
            raise PatchError("cannot find the fallback line 'reverse_proxy localhost:5000'")
        if sum(1 for ln in lines if ln.strip() == FALLBACK_LINE) != 1:
            raise PatchError("found more than one 'reverse_proxy localhost:5000' line; edit by hand")
        base = indent_of(lines[idx])
        block = []
        for depth, row in CADDY_BLOCK_ROWS:
            block.append("\n" if row == "" else base + TAB * depth + row + "\n")
        block.append("\n")
        lines[idx:idx] = block
        changes.append("insert /control/* and /agent/* handlers before the fallback reverse_proxy")

    return "".join(lines), changes


# ---------------------------------------------------------------------- frpc
def patch_frpc(text):
    if FRPC_MARKER in text:
        return text, []
    # refuse to append when the ports are already used by another tunnel
    if "remotePort = 18090" in text:
        raise PatchError("remotePort 18090 is already used by another proxy in this frpc.toml")
    if "localPort = 8090" in text:
        raise PatchError("localPort 8090 is already used by another proxy in this frpc.toml")
    if not text.endswith("\n"):
        text += "\n"
    text += "\n" + "\n".join(FRPC_ROWS) + "\n"
    return text, ["append [[proxies]] name=\"phone-control\" (127.0.0.1:8090 -> 18090)"]


# ----------------------------------------------------------------- validation
def validate(target, path):
    """Run the real validator. Returns (ok, tool_output)."""
    if target == "caddy":
        exe = shutil.which("caddy") or "/usr/bin/caddy"
        if not os.path.exists(exe):
            raise PatchError("'caddy' binary not found; cannot validate")
        # explicit adapter: the dry-run temp copy has no "Caddyfile" file name
        cmd = [exe, "validate", "--config", str(path), "--adapter", "caddyfile"]
    else:
        exe = shutil.which("frpc") or "/opt/frp/frpc"
        if not os.path.exists(exe):
            raise PatchError("'frpc' binary not found (expected /opt/frp/frpc); cannot verify")
        cmd = [exe, "verify", "-c", str(path)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "validator timed out: " + " ".join(cmd)
    out = proc.stdout.decode("utf-8", "replace").strip()
    return proc.returncode == 0, out


def summarize_validation_output(out, ok):
    """Keep tool output readable: one/two lines on success, the tail on failure."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return ""
    if ok:
        return "\n".join(lines[-2:])
    return "\n".join(lines[-40:])


# ----------------------------------------------------------------------- main
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Patch the Caddy / frp config for phone-control (idempotent, backs up first)."
    )
    sub = parser.add_subparsers(dest="target", required=True)

    p_caddy = sub.add_parser("caddy", help="patch aliyun /etc/caddy/Caddyfile")
    p_caddy.add_argument("--file", default="/etc/caddy/Caddyfile")
    p_caddy.add_argument("--dry-run", action="store_true")

    p_frpc = sub.add_parser("frpc", help="patch dev /opt/frp/frpc.toml")
    p_frpc.add_argument("--file", default="/opt/frp/frpc.toml")
    p_frpc.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    target, path = args.target, args.file
    patch = patch_caddy if target == "caddy" else patch_frpc
    next_command = "systemctl reload caddy" if target == "caddy" else "systemctl restart frpc"

    if not os.path.isfile(path):
        print("ERROR: config not found: %s" % path, file=sys.stderr)
        return 1

    with open(path, "r", encoding="utf-8") as fh:
        original = fh.read()
    original_mode = os.stat(path).st_mode

    try:
        patched, changes = patch(original)
    except PatchError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    diff = "".join(
        difflib.unified_diff(original.splitlines(keepends=True),
                             patched.splitlines(keepends=True),
                             fromfile=path + " (current)", tofile=path + " (patched)")
    )

    if not changes:
        print("already patched, nothing to do: %s" % path)
        return 0

    print("changes:")
    for change in changes:
        print("  - " + change)
    print()

    # ---------------------------------------------------------------- dry run
    if args.dry_run:
        print("--- dry run: diff (nothing written) ---")
        print(diff)
        fd, tmp = tempfile.mkstemp(prefix="phone-control-", suffix=".patched")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(patched)
            ok, out = validate(target, tmp)
        finally:
            os.unlink(tmp)
        print("--- dry run: validation %s ---" % ("OK" if ok else "FAILED"))
        summary = summarize_validation_output(out, ok)
        if summary:
            print(summary)
        print("nothing was written; re-run without --dry-run to apply.")
        return 0 if ok else 1

    # ---------------------------------------------------------------- real run
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("ERROR: must run as root to write %s" % path, file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-phone-control-%s" % (path, stamp)
    shutil.copy2(path, backup)
    print("backup: %s" % backup)

    tmp = "%s.tmp-phone-control" % path
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(patched)
        os.chmod(tmp, original_mode & 0o7777)
        os.replace(tmp, path)
    except OSError as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        print("ERROR: cannot write %s: %s" % (path, exc), file=sys.stderr)
        return 1

    ok, out = validate(target, path)
    print("--- validation: %s ---" % ("OK" if ok else "FAILED"))
    summary = summarize_validation_output(out, ok)
    if summary:
        print(summary)
    if not ok:
        shutil.copy2(backup, path)
        print("ERROR: validation failed, restored %s from %s" % (path, backup), file=sys.stderr)
        return 1

    print()
    print("config is valid and updated. Next: sudo %s" % next_command)
    if target == "caddy":
        print("then verify from the host: curl -sk https://127.0.0.1/control/healthz -H 'Host: wkeeli.cn'")
    else:
        print("then verify from aliyun:  curl -s http://127.0.0.1:18090/healthz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
