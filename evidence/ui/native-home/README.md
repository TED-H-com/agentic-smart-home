# Native Home entry UI evidence

These are real Chromium renders of the compiled product UI at 1440, 768 and
390 px. Controller API responses and the destination Home Assistant page are
explicit test fixtures. These screenshots do not establish a real owner login,
cloud account, physical household or native HA deployment acceptance.

- `before-assistant-*`: copied prior release screenshots used for comparison.
- `ready-*`: explicit outer return page after native navigation and browser Back.
- `unavailable-*`: failed native entry check with independent rescue actions.
- `bootstrap-complete-*`: completed setup makes native Home the primary action.
- `assistant-welcome-*`: minimal secondary assistant without the previous hero.
- Other screenshots retain existing setup, account, channel, worker-result and
  maintenance regression coverage under the updated navigation.

`tests/test_bootstrap_ui.py` verifies actual cross-origin browser navigation to
the native page fixture, Back/reload behavior, explicit deep links and pending
approval preservation, unavailable/malformed entry contracts, desktop handoff
consumption, session expiry, delayed responses and in-memory chat draft retention.
