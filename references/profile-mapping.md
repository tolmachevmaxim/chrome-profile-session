# Profile mapping and verified usage

Chrome stores the profile ↔ account mapping in its top-level `Local State`
JSON file (`~/Library/Application Support/Google/Chrome/Local State`), under
`profile.info_cache`. Each entry maps a profile directory (`Default`,
`Profile 1`, `Profile 2`, …) to a display `name` and the signed-in account
`user_name` (email).

Run `chrome_session.py list` to print your own mapping, e.g.:

```
Default     name='Personal'  email='you@example.com'
Profile 2   name='Work'      email='second-account@example.com'
```

Verified run notes:

- Launching Google Maps via the cloned `Default` profile returns
  `signed_in_google=true` and a non-zero `cookie_count` (~18 Google cookies).
- Google Maps opens centered on the signed-in account's home location.
- Clicking **Saved** opens the saved-lists panel and shows default lists plus
  custom/shared lists.

Recommended invocation pattern:

```bash
HOME=$HOME python3 scripts/chrome_session.py run \
  --email you@example.com \
  --url "https://www.google.com/maps" \
  --screenshot /tmp/maps.png \
  --wait-ms 4000
```

Notes:
- Prefer `--email` over `--profile` when possible.
- Use the clone/CDP workflow for UI-driven personal Google data.
- Do not extract or print raw cookies or other session secrets.
