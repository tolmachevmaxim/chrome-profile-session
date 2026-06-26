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


def resolve_scope(page, *, frame_selector=None, frame_url=None, frame_name=None):
    """Resolve where actions/snapshots apply: main page, a Frame, or a FrameLocator.

    Targeting precedence: frame_selector > frame_url > frame_name > main frame.

    - frame_selector: CSS of the <iframe> element. Most robust for cross-origin /
      dynamic iframes (uses Playwright FrameLocator with auto-wait + re-resolution).
      May be a list of CSS selectors to drill into NESTED iframes.
    - frame_url / frame_name: substring (url) / exact (name) match against the
      live frame tree. Use when you don't have a stable <iframe> CSS selector.

    The returned object always exposes `.locator(css)`, so callers don't care
    whether it's a Page main frame, a Frame, or a FrameLocator.
    """
    if frame_selector:
        chain = frame_selector if isinstance(frame_selector, list) else [frame_selector]
        fl = page.frame_locator(chain[0])
        for sel in chain[1:]:
            fl = fl.frame_locator(sel)
        return fl
    if frame_url or frame_name:
        for fr in page.frames:
            if frame_url and frame_url in (fr.url or ''):
                return fr
            if frame_name and frame_name == (fr.name or ''):
                return fr
        raise RuntimeError(f'iframe not found (url~={frame_url!r} name={frame_name!r}); '
                           f'check the "frames" list in the JSON output')
    return page.main_frame


async def run_action(page, step: dict, default_timeout: int) -> dict:
    """Execute one ordered action step. Returns a per-step result dict.

    Each step is a dict: {"action": <name>, ...}. Frame targeting keys
    (frame_selector / frame_url / frame_name) are optional per step — when set,
    the action runs INSIDE that iframe instead of the main document.

    Supported actions:
      click   {selector}                 - click an element
      fill    {selector, value}          - set an input's value (clears first)
      type    {selector, value}          - keystroke-by-keystroke typing
      press   {selector?, key}           - press a key (on element, or globally)
      select  {selector, value}          - choose an <option> by value/label
      check / uncheck {selector}         - toggle a checkbox/radio
      hover   {selector}                 - hover an element
      wait_for {selector, state?}        - wait until element visible (default)
      wait    {ms}                       - sleep N milliseconds
    """
    action = step.get('action')
    timeout = step.get('timeout', default_timeout)
    rec: dict = {'action': action}
    for k in ('selector', 'frame_selector', 'frame_url', 'frame_name'):
        if step.get(k):
            rec[k] = step[k]
    try:
        if action == 'wait':
            await asyncio.sleep(step.get('ms', 0) / 1000)
            rec['ok'] = True
            return rec

        scope = resolve_scope(
            page,
            frame_selector=step.get('frame_selector'),
            frame_url=step.get('frame_url'),
            frame_name=step.get('frame_name'),
        )

        if action in ('press',) and not step.get('selector'):
            # Global keypress (no element target)
            await page.keyboard.press(step['key'])
        else:
            loc = scope.locator(step['selector'])
            if action == 'click':
                await loc.click(timeout=timeout)
            elif action == 'fill':
                await loc.fill(step.get('value', ''), timeout=timeout)
            elif action == 'type':
                await loc.press_sequentially(step.get('value', ''), timeout=timeout)
            elif action == 'press':
                await loc.press(step['key'], timeout=timeout)
            elif action == 'select':
                await loc.select_option(step['value'], timeout=timeout)
            elif action == 'check':
                await loc.check(timeout=timeout)
            elif action == 'uncheck':
                await loc.uncheck(timeout=timeout)
            elif action == 'hover':
                await loc.hover(timeout=timeout)
            elif action == 'wait_for':
                await loc.wait_for(state=step.get('state', 'visible'), timeout=timeout)
            else:
                raise RuntimeError(f'unknown action {action!r}')
        rec['ok'] = True
    except Exception as e:
        rec['ok'] = False
        rec['error'] = repr(e)
    return rec


async def do_work(cdp_ws: str, *, url: str, screenshot: str | None,
                  html_out: str | None, wait_ms: int, full_page: bool,
                  actions: list | None = None, frame_selector=None,
                  frame_url: str | None = None, frame_name: str | None = None,
                  action_timeout: int = 10000) -> dict:
    """Attach Playwright to CDP, navigate, run actions, snapshot.

    When a frame target (frame_selector / frame_url / frame_name) is given at the
    top level, the --html / --screenshot snapshot is taken of THAT iframe instead
    of the whole page.
    """
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

        # Enumerate the live frame tree so the caller can discover which iframe to
        # target (url/name) without guessing. Skips the main frame (index 0).
        try:
            frames = []
            for fr in page.frames:
                if fr is page.main_frame:
                    continue
                frames.append({'name': fr.name or None, 'url': fr.url or None})
            result['frames'] = frames
        except Exception:
            pass

        # Run ordered actions (clicks / input — including inside iframes)
        if actions:
            result['actions'] = []
            for step in actions:
                rec = await run_action(page, step, action_timeout)
                result['actions'].append(rec)

        # Snapshot scope: a specific iframe if requested, else the whole page.
        snap_frame = None
        if frame_selector or frame_url or frame_name:
            try:
                snap_frame = resolve_scope(page, frame_selector=frame_selector,
                                           frame_url=frame_url, frame_name=frame_name)
            except Exception as e:
                result['frame_error'] = repr(e)

        if screenshot:
            if snap_frame is not None:
                # Screenshot just the <iframe> element's box.
                target = snap_frame.locator(':root') if hasattr(snap_frame, 'frame_locator') \
                    else page.locator('iframe')
                try:
                    await target.screenshot(path=screenshot)
                except Exception:
                    await page.screenshot(path=screenshot, full_page=full_page)
            else:
                await page.screenshot(path=screenshot, full_page=full_page)
            result['screenshot'] = screenshot
        if html_out:
            if snap_frame is not None:
                # Frame.content() for a Frame; for a FrameLocator grab outerHTML.
                if hasattr(snap_frame, 'content'):
                    html = await snap_frame.content()
                else:
                    html = await snap_frame.locator(':root').evaluate('el => el.outerHTML')
            else:
                html = await page.content()
            pathlib.Path(html_out).write_text(html, encoding='utf-8')
            result['html'] = html_out
            result['html_bytes'] = len(html)
        # Don't close the browser — caller decides via --keep-open
        return result


def parse_frame_selector(raw: str | None):
    """A single CSS selector, or comma-separated CSS selectors for nested iframes."""
    if not raw:
        return None
    parts = [s.strip() for s in raw.split(',') if s.strip()]
    return parts if len(parts) > 1 else parts[0]


def load_actions(args) -> list | None:
    """Build the ordered action list from --actions (JSON) or --actions-file.

    Accepts either a JSON array of step objects, or a single step object.
    """
    raw = None
    if args.actions_file:
        raw = pathlib.Path(args.actions_file).read_text(encoding='utf-8')
    elif args.actions:
        raw = args.actions
    if not raw:
        return None
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        sys.exit('--actions must be a JSON array of step objects (or one object)')
    return data


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
            actions=load_actions(args),
            frame_selector=parse_frame_selector(args.frame_selector),
            frame_url=args.frame_url,
            frame_name=args.frame_name,
            action_timeout=args.action_timeout,
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
    # --- iframe targeting (for --html/--screenshot snapshot + per-step default) ---
    p.add_argument('--frame-selector',
                   help='CSS of the <iframe> element to snapshot. Comma-separate '
                        'for nested iframes, e.g. "#outer,#inner". Most robust.')
    p.add_argument('--frame-url',
                   help='Substring of a frame URL to snapshot (fallback to selector)')
    p.add_argument('--frame-name',
                   help='Exact name of a frame to snapshot')
    # --- ordered actions: clicks / input, incl. inside iframes ---
    p.add_argument('--actions',
                   help='JSON array of ordered action steps (clicks/input). Each '
                        'step may carry frame_selector/frame_url/frame_name to act '
                        'INSIDE an iframe. See SKILL.md for the step schema.')
    p.add_argument('--actions-file',
                   help='Path to a JSON file with the action steps (alt to --actions)')
    p.add_argument('--action-timeout', type=int, default=10000,
                   help='Per-step timeout in ms (default: 10000)')
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
