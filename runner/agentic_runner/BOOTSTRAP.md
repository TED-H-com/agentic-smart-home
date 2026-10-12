# Host CLI bootstrap assistance

`POST /bootstrap/jobs` is private to the paired Controller and enabled only in
Host Runner mode. It uses the same durable job database and single execution
semaphore as existing jobs. The body is strictly typed:

```json
{
  "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "provider": "codex",
  "stage": "hermes_setup",
  "model": "gpt-6-astra",
  "expires": 2000000000.0,
  "observations": {
    "network_ready": true,
    "hermes_installed": false,
    "hermes_auth_state": "needs_auth",
    "channel": "line",
    "ha_state": "not_installed"
  }
}
```

The example expiry must be replaced with an actual deadline within six minutes.
Providers are the four already packaged CLIs; each retains its existing supported
login methods. No subscription token is copied to Hermes. Supported channel
choices are LINE, Telegram, Discord and Web.

The Runner verifies local login status and invokes the actual selected CLI with
the reviewed prompt in `bootstrap.py`, existing disabled tools, bounded output,
isolated provider profile and a five-minute timeout. Local `connected` auth alone
does not prove inference. Only a successfully parsed CLI result adds
`evidence.inference_completed=true`, with the CLI version and
`observed_from=host_runner`. `live_mutations` remains false.

`GET /bootstrap/jobs/{id}` resumes observation after a page closes. A retry must
reuse the exact ID, deadline and fields; conflicting content returns 409.
`POST /bootstrap/jobs/{id}/cancel` uses the existing job cancellation path.
Restarted unfinished jobs fail without automatic replay.

Model output permits only `decision`, `next_step` and fixed `reason_codes`.
Commands, paths, URLs, credentials, prose and additional fields are rejected.
The result is advice: Controller and the fixed Host installer independently
decide the next authorized step and verify installation and authentication.
No generated value approves deployment, updates a source pin or controls an
arbitrary shell. The household `home_operator` command restrictions are unchanged.

Tests include real packaged Codex inference against an in-container Responses
fixture with external networking disabled and an empty exposed tool catalog.
This establishes CLI integration, not a real cloud account or laptop acceptance.
