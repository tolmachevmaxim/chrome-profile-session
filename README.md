# chrome-profile-session

A Claude Code / Agent **skill** that runs any web action under your *real*
Chrome login session — without ever decrypting cookies by hand.

It clones your Chrome profile into a temp dir, launches the real `Google
Chrome.app` in remote-debugging (CDP) mode, and attaches
[Playwright](https://playwright.dev/python/) over CDP. Because the actual
Chrome binary starts up, it decrypts cookies via the OS keychain itself — so
every site you're logged into (Google, Gmail, Drive, YouTube, Maps, Calendar,
…) is authenticated automatically. No 2FA dance, no manual cookie extraction.

> **macOS-focused.** Paths/keychain handling target macOS + `Google
> Chrome.app`. Adapt `CHROME_USER_DATA` / `CHROME_BINARY` in
> `scripts/chrome_session.py` for Linux/Windows.

## Why

Chrome encrypts cookies with a per-user key in the OS keychain (Chrome Safe
Storage). Decrypting them yourself is fragile and requires the keychain
password. The robust trick: let *Chrome* do the decryption by pointing it at a
**clone** of your profile with `--remote-debugging-port`, then drive it with
Playwright. The clone means your live, everyday Chrome is never touched.

## Install

```bash
git clone https://github.com/<you>/chrome-profile-session.git
cd chrome-profile-session
pip install playwright
python -m playwright install chromium   # only needed for Playwright's own browser; this skill uses your system Chrome
```

As a Claude Code skill, drop the folder into `~/.claude/skills/` (or your
project's `.claude/skills/`) and invoke it by name.

## Usage

List the Chrome profiles on this machine and their account emails:

```bash
python3 scripts/chrome_session.py list
```

Screenshot an authenticated page in a given account's session:

```bash
python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "https://www.google.com/maps" \
  --screenshot /tmp/maps.png \
  --wait-ms 4000
```

Dump page HTML, keep Chrome open for interactive work, or reuse a clone — see
[`SKILL.md`](SKILL.md) for the full command reference and
[`references/profile-mapping.md`](references/profile-mapping.md) for how the
profile↔email mapping works.

## How it works

1. Read `Local State` JSON to map email → profile directory.
2. Clone the profile's auth-relevant files (`Cookies`, `Login Data`,
   `Local Storage`, `Network/`, …) plus the top-level `Local State` (holds the
   cookie-encryption key) into a temp dir.
3. Launch the real Chrome with `--user-data-dir=<clone>` and
   `--remote-debugging-port=<random>`. Chrome decrypts cookies on startup.
4. Attach Playwright via `chromium.connect_over_cdp()`.
5. Do the work; then close + clean up the clone (or `--keep-open`).

## Security

- Runs entirely locally. No cookies or tokens are sent anywhere.
- The clone is a temp dir, removed on exit (unless you pass `--keep-clone` /
  `--user-data-dir`).
- Cookies in the clone stay Chrome-encrypted; they're only decrypted inside
  the running Chrome process.
- When driving this from a cloud/remote agent, return only the requested
  artifact (screenshot, HTML, JSON) — never raw cookies or session state.

## Requirements

- macOS with Google Chrome installed at `/Applications/Google Chrome.app`
- Python 3.10+
- `playwright` (Python)

## License

MIT — see [LICENSE](LICENSE).
