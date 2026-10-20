# Synthetic DSM contract fixture

Hand-authored synthetic data, never a physical NAS capture. This custom integration
runs only in an isolated test Core container. SSDP injection replaces observation;
Core's `synology_dsm` flow, py-synologydsm-api 2.10.3, platforms, coordinator,
backup agent and registries remain unmodified. It serves HTTPS on container
loopback `127.0.0.1:5001`; HTTP 5000 is unnecessary because SSDP's location is
used only to extract the host. Test TLS key generation uses disk below `/config`.

The scenario is written before startup as `agentic_synology_scenario.json`:
`synthetic: true`, `username`, `password`, `otp_required`, `otp_code`,
`writable_shares: [{name,path}]`, `serial`, `mac`, `model`, `hostname`,
`friendly_name`. All credentials are synthetic. The request journal contains
only `api`, `method`, `outcome`; no URLs, parameters, session IDs or credentials.
Unknown operations, wrong CGI paths/versions/verbs, and a non-writable share
query are rejected. Every qualification checks that zero rejections occurred.
Session IDs exist only in memory. Trusted-device token hashes and the consumed
OTP boolean persist so a full Core restart can exercise the stored device token.

## Source review and request map

Reviewed extracted source in both pinned local images, Core 2026.10.1 and
2026.10.2: all 30 library Python files and all 18 integration Python files are
byte-identical. SHA-256 of `synology_dsm/synology_dsm.py`:
`67df22a60560c502e11ec17c1d8884f4d7d1d924d69d64d843264f37c3daf58c`;
of `homeassistant/components/synology_dsm/config_flow.py`:
`3cb7dfc9b14a9f60ace7ee0779a1d67eb2f835e03138cfd4d22a3eda73eafc82`.
Relevant additional modules: library `api/__init__.py`, `api/core/*.py`,
`api/dsm/*.py`, `api/storage/storage.py`, `api/file_station/{__init__,models}.py`;
Core `common.py`, `coordinator.py`, `backup.py`, and the platform modules.

Every request includes `api`, `method`, `version`; authenticated requests also
include `_sid` (deliberately excluded below). These versions and paths are the
**synthetic advertised API profile**, not claims about every DSM firmware.
The library chooses each advertised `maxVersion`. `query` has no `query=all`
parameter in this library. No `SynoToken` is issued by this fixture.

### Config flow

| HTTP | API | Method | Version | Path | Other parameters without secrets |
| --- | --- | --- | --- | --- | --- |
| GET | SYNO.API.Info | query | 1 | /webapi/query.cgi | none |
| GET | SYNO.API.Auth | login | 7 | /webapi/entry.cgi | enable_device_token=yes; device_name=container hostname; account/passwd and optional otp_code/device_id omitted |
| GET | SYNO.DSM.Info | getinfo | 2 | /webapi/entry.cgi | none |
| GET | SYNO.Core.System.Utilization | get | 1 | /webapi/entry.cgi | none |
| GET | SYNO.Storage.CGI.Storage | load_info | 1 | /webapi/entry.cgi | action=load_info |
| GET | SYNO.DSM.Network | list | 2 | /webapi/entry.cgi | none |
| GET | SYNO.FileStation.List | list_share | 2 | /webapi/entry.cgi | offset=0; limit=100; onlywritable=True; additional=["real_path","owner","time","perm","mount_point_type","sync_share","volume_status"] |

The first login initializes information. Later validation with the same client
repeats login, utilization, storage, network and shares without fetching
information again. Invalid credentials reset the client, so its next login
also repeats API discovery. The config flow does not logout.

`SYNO.API.Auth` codes: 400 = `SynologyDSMLoginInvalidException` (`invalid_auth`);
403 = `SynologyDSMLogin2SARequiredException`; 404 =
`SynologyDSMLogin2SAFailedException` (`otp_failed`). Other mapped codes:
401 disabled account; 402 permission denied; 406 forced 2SA; remaining codes
become `SynologyDSMLoginFailedException`. Those additional cases are source
review only, not exercised by this matrix.

On a successful OTP login the fixture returns `did`; the library keeps it as
its device token. The folder POST reuses the same client and saved input, so
its re-login sends **both the previous OTP and device_id**. The fixture treats
a valid token as authoritative and ignores the already-consumed OTP. This is
an explicit firmware assumption, not a real DSM trusted-device acceptance
result. Entry setup and restart send the saved device token without the OTP.

### Entry setup and initial platforms

A new client repeats API discovery, login (device token when configured),
information (once during login and once explicitly), network and shares.
`SynoApi.async_setup()` additionally reads the following:

| HTTP | API | Method | Version | Path | Other parameters without secrets |
| --- | --- | --- | --- | --- | --- |
| GET | SYNO.Core.Upgrade.Server | check | 4 | /webapi/entry.cgi | none |
| GET | SYNO.Core.Hardware.FanSpeed | get | 1 | /webapi/entry.cgi | none |
| GET | SYNO.Core.SecurityScan.Status | system_get | 1 | /webapi/entry.cgi | none |
| GET | SYNO.Core.System | info | 3 | /webapi/entry.cgi | none |
| POST | SYNO.Core.ExternalDevice.Storage.USB | list | 1 | /webapi/entry.cgi | form: additional=["all"]; mimeType=application/json; api/method/version/_sid are also included in the form |

The initial update also reads information, utilization, storage, upgrade and
hardware. Platform registration subscribes enabled entities without calling
fan control, reboot, shutdown or update installation. The USB POST is a read.
The upgrade check only queries availability; on a real NAS it could cause a
Synology update-server check. Authentication creates sessions/trusted-device
state, but this tested profile makes no DSM configuration/file write calls.

### After-setup polling and backup agent

The test explicitly refreshes the real central coordinator twice, then again
after restart. It does not claim elapsed-interval or long-duration coverage.
`_setup_api_requests()` selects reads according to enabled entity subscribers;
the synthetic profile polls information, utilization, storage, security,
system, hardware and upgrade. USB inventory is fetched on initial setup but
has no enabled USB entity in this empty-USB scenario. Network is not polled.

With a folder selected, the native backup agent constructor performs no I/O.
Listing the empty backup folder uses:

| HTTP | API | Method | Version | Path | Other parameters without secrets |
| --- | --- | --- | --- | --- | --- |
| GET | SYNO.FileStation.List | list | 2 | /webapi/entry.cgi | offset=0; limit=1000; folder_path=/home-backups/ha_backup_synthetic_synology_qualification; additional=["real_path","owner","time","perm","mount_point_type","type","size"] |
| GET | SYNO.API.Auth | logout | 7 | /webapi/entry.cgi | none; session lifecycle only |

Logout is allowed for native entry unload, although Core restart need not
unload this entry. A nonempty backup folder would download `_meta.json` files
via `SYNO.FileStation.Download/download` with `path`, `mode=download`.
That capability is advertised but rejected in this empty-folder profile.
Upload and deletion are never invoked. The explicit read allowlist cannot
perform FileStation upload/create/delete or system reboot/shutdown.

### Conditional upstream branches outside this profile

Surveillance Station is not advertised, so its optional camera/home-mode
coordinators do not run. Source-reviewed conditional GETs are
`SYNO.SurveillanceStation.Camera/List` (max_version=7),
`SYNO.SurveillanceStation.Camera.Event/MotionEnum` (`camId`),
`SYNO.SurveillanceStation.Camera/GetLiveViewPath` (`idList`),
`SYNO.SurveillanceStation.HomeMode/GetInfo`, and
`SYNO.SurveillanceStation.Info/GetInfo`; their CGI paths and other versions
come from the real NAS's API discovery and are not asserted here. Camera
image reads, media browsing/Photos, existing backup downloads and all user
actuation remain untested. This fixture makes no universal no-write claim
about unadvertised optional packages or later user actions.

## Captures and evidence

`AGENTIC_SYNOLOGY_EVIDENCE_DIR` exports one sanitized JSON per successful test
run, constrained below `~/agent-work/tmp`. It includes pinned image ID/digests,
run identity, per-phase journal summaries, fixture boundaries and exclusions.
Every native response is immediately projected to a schema capture with secret
defaults replaced by `<redacted>`; raw results are never persisted. Override
`AGENTIC_SYNOLOGY_FORMS_PATH` to choose its disk location below that same root.
The task default is `~/agent-work/tmp/synology-setup-20261019/captured-forms.json`.

Physical NAS, real DSM firmware, LAN SSDP multicast, Supervisor backup, HAOS,
live household, DSM-side trusted-device state and backup upload are not tested.
The service matrix is deliberately collected on the pre-feature base; it
requires the concurrently implemented SPEC API and is never skipped or weakened
because that API is absent.
