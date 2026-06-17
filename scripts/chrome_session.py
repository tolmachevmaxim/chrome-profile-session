#!/usr/bin/env python3
"""Launch system Chrome with a cloned profile and attach Playwright over CDP.

Why this exists:
- Chrome cookies are encrypted with a per-user key in the OS keychain (Chrome Safe Storage).
- Decrypting them manually requires the keychain password and is fragile.
- Easier path: let Chrome itself decrypt by running the real Chrome.app binary
  with --user-data-dir pointing at a CLONE of the user's profile, plus
  --remote-debugging-port. Then Playwright attaches over CDP.

The clone avoids locking the live profile (Chrome may be running normally).

Usage examples:
    # List Chrome profiles + their Google account emails
    chrome_session.py list

    # Take screenshot of Google Maps in the signed-in session
    chrome_session.py run --email you@example.com --url https://www.google.com/maps \
        --screenshot /tmp/maps.png

    # Dump HTML for an authenticated page
    chrome_session.py run --email you@example.com --url https://myaccount.google.com \
        --html /tmp/account.html

    # Keep Chrome open for interactive inspection (CDP url printed)
    chrome_session.py run --email you@example.com --url about:blank --keep-open

    # Use specific profile dir name instead of email
    chrome_session.py run --profile "Profile 2" --url ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

CHROME_USER_DATA = pathlib.Path.home() / 'Library/Application Support/Google/Chrome'
CHROME_BINARY = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

# Files/dirs to copy from the chosen profile (relative to profile dir).
# Cookies live in Network/Cookies on modern Chrome.
PROFILE_FILES = [
    'Cookies',
    'Cookies-journal',
    'Login Data',
    'Login Data-journal',
    'Preferences',
    'Bookmarks',
    'History',
    'Local Storage',
    'IndexedDB',
    'Network',           # contains Network/Cookies on newer Chrome
    'Session Storage',
    'Service Worker',
    'shared_proto_db',
    'databases',
    'Web Data',
    'Web Data-journal',
]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def list_profiles() -> list[dict]:
    """Return list of profile dicts: {'dir', 'name', 'email'}."""
    state = json.loads((CHROME_USER_DATA / 'Local State').read_text())
    cache = state.get('profile', {}).get('info_cache', {})
    out = []
    for d, info in cache.items():
        out.append({
            'dir': d,
            'name': info.get('name'),
            'email': info.get('user_name') or info.get('gaia_name'),
        })
    return out


def find_profile_by_email(email: str) -> str | None:
    for p in list_profiles():
        if p['email'] and p['email'].lower() == email.lower():
            return p['dir']
    return None


def clone_profile(profile_dir: str, dest: pathlib.Path) -> pathlib.Path:
    """Clone the chosen profile into dest as a valid Chrome user-data-dir.

    The clone layout is: dest/Local State + dest/<profile_dir>/...
    Returns dest.
    """
    src_profile = CHROME_USER_DATA / profile_dir
    dest.mkdir(parents=True, exist_ok=True)
    # Top-level Local State (contains encrypted_key shared across profiles)
    shutil.copy2(CHROME_USER_DATA / 'Local State', dest / 'Local State')
    # First Run flag silences first-run UI
    (dest / 'First Run').write_bytes(b'')
    # Clone the profile dir itself
    dst_profile = dest / profile_dir
    dst_profile.mkdir(exist_ok=True)
    for entry in PROFILE_FILES:
        src = src_profile / entry
        if not src.exists():
            continue
        dst = dst_profile / entry
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False,
                            ignore_dangling_symlinks=True)
        else:
            shutil.copy2(src, dst)
    return dest


def launch_chrome(user_data_dir: pathlib.Path, profile_dir: str, port: int) -> subprocess.Popen:
    """Start Chrome with CDP enabled. Returns Popen handle."""
    args = [
        CHROME_BINARY,
        f'--user-data-dir={user_data_dir}',
        f'--profile-directory={profile_dir}',
        f'--remote-debugging-port={port}',
        '--remote-allow-origins=*',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-features=Translate,InfiniteSessionRestore',
        '--restore-last-session=false',
        '--no-startup-window',  # we open tabs via CDP
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_cdp(port: int, timeout: float = 25.0) -> str:
    """Poll http://127.0.0.1:<port>/json/version until ready. Return ws url."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as r:
                data = json.loads(r.read())
                ws = data.get('webSocketDebuggerUrl')
                if ws:
                    return ws
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f'Chrome CDP not ready on :{port} within {timeout}s')


async def do_work(cdp_ws: str, *, url: str, screenshot: str | None,
                  html_out: str | None, wait_ms: int, full_page: bool) -> dict:
    """Attach Playwright to CDP, navigate, perform actions."""
    from playwright.async_api import async_playwright
    result: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_ws)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Reuse first page if any; else create one
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        except Exception as e:
            result['nav_error'] = repr(e)
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        # Sanity: is the user signed into Google?
        try:
            cookies = await ctx.cookies(url)
            has_sid = any(c['name'] in ('SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-1PSID')
                          for c in cookies)
            result['signed_in_google'] = has_sid
            result['cookie_count'] = len(cookies)
        except Exception:
            pass
        result['url'] = page.url
        result['title'] = await page.title()
        if screenshot:
            await page.screenshot(path=screenshot, full_page=full_page)
            result['screenshot'] = screenshot
        if html_out:
            html = await page.content()
            pathlib.Path(html_out).write_text(html, encoding='utf-8')
            result['html'] = html_out
            result['html_bytes'] = len(html)
        # Don't close the browser — caller decides via --keep-open
        return result


def cmd_list(args):
    profiles = list_profiles()
    for p in profiles:
        print(f'{p["dir"]:<14}  name={p["name"]!r:<24}  email={p["email"]!r}')


def cmd_run(args):
    # Resolve profile
    profile_dir = args.profile
    if args.email and not profile_dir:
        profile_dir = find_profile_by_email(args.email)
        if not profile_dir:
            sys.exit(f'No Chrome profile found for email {args.email!r}. '
                     f'Run: chrome_session.py list')
    if not profile_dir:
        sys.exit('Provide --email or --profile')

    # Clone
    if args.user_data_dir:
        clone_dir = pathlib.Path(args.user_data_dir)
    else:
        clone_dir = pathlib.Path(tempfile.mkdtemp(prefix='chrome-clone-'))
    print(f'[clone] {CHROME_USER_DATA}/{profile_dir} → {clone_dir}/{profile_dir}',
          file=sys.stderr)
    clone_profile(profile_dir, clone_dir)

    # Launch
    port = args.port or find_free_port()
    print(f'[launch] Chrome --user-data-dir={clone_dir} --remote-debugging-port={port}',
          file=sys.stderr)
    proc = launch_chrome(clone_dir, profile_dir, port)
    try:
        ws = wait_for_cdp(port)
        print(f'[cdp] {ws}', file=sys.stderr)
        result = asyncio.run(do_work(
            ws,
            url=args.url,
            screenshot=args.screenshot,
            html_out=args.html,
            wait_ms=args.wait_ms,
            full_page=args.full_page,
        ))
        # Output JSON result to stdout (only thing on stdout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.keep_open:
            print(f'\n[keep-open] Chrome PID={proc.pid}, CDP={ws}', file=sys.stderr)
            print(f'[keep-open] Press Ctrl+C or `kill {proc.pid}` to close', file=sys.stderr)
            proc.wait()
    finally:
        if not args.keep_open:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if not args.keep_clone and not args.user_data_dir:
            shutil.rmtree(clone_dir, ignore_errors=True)
            print(f'[cleanup] removed {clone_dir}', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('list', help='List Chrome profiles + emails').set_defaults(func=cmd_list)

    p = sub.add_parser('run', help='Launch Chrome with cloned profile, do work')
    p.add_argument('--email', help='Match profile by Google account email')
    p.add_argument('--profile', help='Profile dir name (e.g. "Default", "Profile 2")')
    p.add_argument('--url', required=True, help='URL to open')
    p.add_argument('--screenshot', help='Save screenshot to this path')
    p.add_argument('--full-page', action='store_true', help='Full-page screenshot')
    p.add_argument('--html', help='Save page HTML to this path')
    p.add_argument('--wait-ms', type=int, default=1500,
                   help='Sleep after navigation before snapshot (default: 1500ms)')
    p.add_argument('--port', type=int, default=0,
                   help='CDP port (0 = random free port)')
    p.add_argument('--user-data-dir',
                   help='Reuse this clone dir (skips clone+cleanup if exists)')
    p.add_argument('--keep-clone', action='store_true',
                   help='Don\'t delete cloned profile dir on exit')
    p.add_argument('--keep-open', action='store_true',
                   help='Keep Chrome running after action (for interactive use)')
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
