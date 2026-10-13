# Home verifier UI fixture

These are real Chromium screenshots of the compiled Web UI with synthetic API
receipts. They do not prove Home Worker execution, cloud authentication or E2
acceptance.

- `before-1440.png` / `before-390.png`: the existing UI omitted an
  `independent_home_verifier` receipt.
- `after-1440.png` / `after-768.png` / `after-390.png`: the 0.8.0 compiled UI shows
  the Home verification status and source, using existing components and styles.
  All three widths were visually inspected and checked for horizontal overflow.
- The browser regression also preserves `independent_host_verifier`, exposes the
  original verification evidence on expansion, and hides unknown verifier sources.

The worker UI suite passed 5 tests; see [JUnit](../../home-verifier-ui.xml).
The [initial fixture failure](../../home-verifier-ui-initial-fixture-failure.xml)
records an incorrect test expectation for a translated status label. The existing
Status component renders `passed`; the corrected assertion checks that value.

Compiled assets: `index-CYBEQgqz.js`, `index-BlZhfU3M.css`. The thin App receives
the same compiled assets via `package_bridge`.
