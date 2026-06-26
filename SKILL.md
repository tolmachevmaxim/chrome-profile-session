---
name: chrome-profile-session
description: Run any web action under your real Chrome login session (Google Maps, Gmail, YouTube, etc.) by cloning a Chrome profile and attaching Playwright over CDP. Use when a task needs authenticated access to a Google or other site where you are logged in on Chrome locally.
---

# chrome-profile-session

Launch the **real Chrome.app** with a cloned user profile in CDP debug mode,
then attach Playwright. Chrome decrypts cookies via the OS keychain itself, so all
login sessions (Google, YouTube, Gmail, Drive, Maps, Calendar) are available.

## Profiles

Run the helper to see available Chrome profiles + their account emails:

```bash
python3 scripts/chrome_session.py list
```

Example output:

```
Default         name='Personal'   email='you@example.com'
Profile 2       name='Work'       email='second-account@example.com'
```

Pick by `--email <addr>` (recommended) or `--profile <dir>`.

## Typical use

### Screenshot of an authenticated page
```bash
python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "https://www.google.com/maps" \
  --screenshot /tmp/maps.png \
  --wait-ms 4000
```

Output is JSON on stdout with: `signed_in_google`, `cookie_count`, `url`, `title`,
plus paths to screenshot/html if requested. Stderr has progress logs.

### Dump page HTML (e.g. scrape your data)
```bash
python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "https://myaccount.google.com" \
  --html /tmp/account.html \
  --wait-ms 3000
```

### Keep Chrome open for interactive debugging
```bash
python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "about:blank" \
  --keep-open
```
Prints the CDP WebSocket URL so any Playwright/puppeteer script can attach
to the same browser.

### Reuse the clone (no re-copy each run)
```bash
python3 scripts/chrome_session.py run \
  --user-data-dir /tmp/chrome-clone-default \
  --email you@example.com \
  --url ...
```
First run creates the clone; subsequent runs are fast. Cookies stay fresh from
the original profile.

### Clicks & input — including INSIDE iframes
Pass an ordered list of action steps via `--actions` (inline JSON) or
`--actions-file <path>`. Each step optionally targets an iframe, so you can
type/click inside embedded widgets (payment forms, auth iframes, embedded apps)
that the main document can't reach.

```bash
python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "https://example.com/checkout" \
  --actions '[
    {"action":"fill","selector":"#card","value":"4242424242424242","frame_selector":"iframe[name=card]"},
    {"action":"fill","selector":"#cvc","value":"123","frame_selector":"iframe[name=card]"},
    {"action":"click","selector":"button[type=submit]"},
    {"action":"wait_for","selector":"#success","frame_url":"checkout","state":"visible"}
  ]' \
  --screenshot /tmp/after.png --wait-ms 2000
```

**Discovering frames:** every `run` returns a `frames` array in its JSON output
(`name` + `url` of each iframe). Use it to pick the right `frame_*` target.

**Per-step frame targeting** (omit all three → acts on the main document):
- `frame_selector` — CSS of the `<iframe>` element (most robust; works
  cross-origin via Playwright FrameLocator). Comma-separate for **nested**
  iframes: `"#outer,#inner"`.
- `frame_url` — substring match against a frame's URL.
- `frame_name` — exact frame `name`.

**Actions:** `click`, `fill` (clears+sets), `type` (keystroke-by-keystroke),
`press` (`{key}`, with or without `selector`), `select` (`{value}`), `check` /
`uncheck`, `hover`, `wait_for` (`{state}`, default `visible`), `wait` (`{ms}`).
Per-step `timeout` (ms) overrides the global `--action-timeout` (default 10000).
Results report `ok`/`error` per step; one failing step does not abort the run.

The same `--frame-selector` / `--frame-url` / `--frame-name` flags also scope a
`--html` / `--screenshot` snapshot to a single iframe instead of the whole page.

## How it works

1. **Find profile** matching the requested email via `Local State` JSON.
2. **Clone** profile's auth-relevant files (`Cookies`, `Login Data`,
   `Preferences`, `Local Storage`, `IndexedDB`, `Network/`, `Web Data`, …)
   into a temp dir, alongside the top-level `Local State` (which holds the
   encrypted key for cookies).
3. **Launch** `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   with `--user-data-dir=<clone>`, `--profile-directory=<dir>`,
   `--remote-debugging-port=<random>`, `--no-first-run`,
   `--remote-allow-origins=*`. Chrome decrypts cookies through the keychain on
   startup — no manual decryption needed.
4. **Attach** Playwright via `chromium.connect_over_cdp()`.
5. **Do work**, then either keep Chrome open (`--keep-open`) or terminate +
   clean up the clone.

## When NOT to use

- For static, unauthenticated pages — use a lighter plain Playwright script.
- For sites where you already have preset cookies (e.g. a dedicated
  browser-session helper with `li_at` / `c_user+xs`) — those are more focused.
- For API-integrated services (Gmail, Calendar, Drive, Sheets) — prefer the
  official APIs. APIs are cleaner than scraping authenticated browser pages.

## Notes

- Chrome must be installed at `/Applications/Google Chrome.app`. (Edit
  `CHROME_BINARY` in `chrome_session.py` if you use a non-standard location.)
- When running under headless/automation contexts, set `HOME=$HOME` explicitly
  so the helper can find the real Chrome profile and `Local State` file for
  profile discovery.
- The script runs a **second** Chrome instance alongside any already-running
  Chrome — it does NOT touch the live profile. Safe.
- Clones are created in `$TMPDIR/chrome-clone-*` and removed on exit unless
  `--keep-clone` / `--user-data-dir` is given.
- Cookies stay valid for the clone's lifetime but **session cookies may
  expire** if the original profile signs out. Reclone after sign-out.
- Doesn't bypass 2FA — if Chrome is logged in, the clone is logged in.
- Use `--keep-open` + the CDP URL to share a single Chrome instance across
  multiple Playwright scripts.

## Verified working

- ✅ Google Maps (`https://www.google.com/maps`) — auth detected, map centered
  on the signed-in account's home location, ~18 Google cookies including
  `SID`, `HSID`, `__Secure-1PSID`.
- Should work for: Gmail, Drive, YouTube, Calendar, Photos, MyAccount,
  Google Sheets web, Google Docs web, anything under `*.google.com`.

## Security

- The skill runs locally on your machine. No cookies/tokens are sent to
  any LLM or remote service.
- Clone is local-only, temp-dir, removed on exit.
- Cookies in the clone are still Chrome-encrypted (decrypted only inside
  the running Chrome process). Reading the clone files manually won't
  reveal raw secrets.
- When invoking from a cloud/remote agent, return only the requested artifact
  (screenshot, HTML, JSON) — never raw cookies or session state.

## Support Files

- `references/profile-mapping.md` — how the profile ↔ email mapping works and verified Google Maps behavior.
