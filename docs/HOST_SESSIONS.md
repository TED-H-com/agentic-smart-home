# Host browser session revocation

The outer Linux Controller uses a separate browser login from Home Assistant.
The S1 fix gives each access-key or desktop-ticket login a random session cookie
and a server-side record. Logging out deletes that session before waiting for
Web work recovery, and commits the revocation to disk before returning success.
Replaying the saved cookie then fails, including after a Controller restart.
Other browser sessions retain their login; the existing shared Web epoch still
cancels authority for pending Web work when any browser logs out.

CSRF tokens bind the authenticated principal, this specific session and expiry.
A token from another login cannot authorize a request. Host/Origin checks,
SameSite=strict, HttpOnly, the local listener and the separate HA Ingress bridge
authority remain in force. Session lifetime remains an absolute 12 hours; this
change does not introduce an idle timeout or a “log out all devices” interface.

The Controller observes the access-key file when issuing or checking a session.
An observed key change removes every Host session and rotates Web authority.
Restoring an old key after its replacement was observed does not restore those
sessions. A missing/unreadable key denies access. A failed database journal
prevents new sessions and successful logout; already authenticated read-only
rescue remains available from the current in-memory state, but a key change
cannot authorize access while persistence is unavailable.
If a logout commit fails, the response is an error, not a successful logout.
The cookie is rejected by that running process, but restarting from the last
committed database can recover the earlier session; the failed logout must be
retried once storage works. The restart guarantee applies to successful logout.
Changing and restoring the key entirely between Controller observations cannot
be detected; this is not an operating-system file watcher.

## Migration and rollback

Previously issued two-part, stateless cookies are intentionally rejected on
upgrade. Existing users must log in once with the access key or reopen the
installed desktop shortcut. Silently accepting or converting the old cookie
would preserve the replay vulnerability the upgrade is intended to remove.

Storage uses additive private `host_session` version 1 records within database
schema 3: only the SHA-256 cookie digest, expiry and keyed access-key fingerprint
are persisted. Neither the cookie nor the access key is stored in these
records. Expired records are removed when another session is issued. New valid
sessions survive a normal Controller restart; successful logout survives it too.

Schema 3 alone does **not** make a downgrade safe. Older Controller binaries do
not enforce these session records and can accept previously issued stateless
cookies. Security downgrade to those binaries is unsupported; the current
updater does not enforce a Host-session capability gate. Do not present restoring
an earlier binary or database snapshot as preserving this revocation guarantee.
Full installation, hardware and deployed-upgrade acceptance remain separate.

## Verification scope

`tests/test_host_sessions.py` covers saved-cookie replay, independent sessions,
cross-session and legacy CSRF rejection, real on-disk persistence and restart,
logout while recovery waits, issuance/revocation durability barriers, key
replacement, legacy-cookie migration, malformed/expired/unrecorded cookies,
missing keys, journal failure and one-use desktop-ticket login. Existing Host,
HA OAuth, bridge and Store durability regressions exercise the adjacent trust
boundaries. These are local automated tests, not Dell deployment acceptance.
