# Signed Host bundle updates

The Controller image updater cannot deliver fixes to the root Host broker,
`/opt/agentic-home`, systemd units or product-managed Linux configuration. The
Host bundle updater is a separate, owner-approved path for those files. It does
not reinstall Linux, upgrade HAOS, pull new outer runtime images, restore an old
Controller database, or automatically install a release.

## Trust and approval

The authority is the **same official Ed25519 public key** designated by
`host/official-update-channel.json` and the official publisher flow in PR #121.
The fixed external executor keeps its own authenticated, root-owned copy at
`/usr/lib/agentic-home-update/current/official-update-channel.json`. Both that
pin and the enrolled official publisher must agree. Valid engineering metadata
with explicit `pin: null` and `image_repository: null` disables checking, staging
and application. Missing or invalid metadata fails validation; it does not
silently defer executor installation. A moving GitHub branch, HTTPS, an image tag,
repository ownership and checksums do not create a publisher trust root.

Ted approved the production **public** Ed25519 key on **2026-10-19**. The
checked-in descriptor pins that key, its DER SHA-256 fingerprint
`18dd73f8b96d115d076be6b362cd62ed011c4be2b6dfdaf808e9b3e5a6a53d39`, and the exact
Controller image repository `ghcr.io/ted-h-com/agentic-smart-home-controller`.
The private key remains outside this repository on the release side. See
[the public key and approval flow](UPDATES.md#官方更新來源與-owner-批准).
No production-signed Controller image, Host bundle, bootstrap package or
product-signed media has been published yet. Existing ISO checksums and Ubuntu's
signed boot chain do not authenticate our remastered product media.

Publisher enrollment in #121 grants Controller-image trust. It does not by
itself approve a root Host update. The maintenance page offers **檢查主機更新**,
which verifies and stages an official candidate without executing its code.
One exact **核准主機更新** action authorizes that candidate's root product code,
automatic restore points, temporary service/HA interruptions and the reviewed
reconcile changes. No second approval is needed to compensate a failed approved
update. Manual **回復先前主機版本** requires its own expiring exact review.
Approval is bound to the current owner, Host session, authorization epoch,
candidate, baseline and a one-use Host token. Model, bridge and root Runner
roles cannot grant it. Responses lost across restarts are read back from the
persistent operation record, not treated as permission to install twice.

## Activation and recovery

1. Verify a canonical signed envelope, its bounded lifetime/test-evidence hashes,
   exact official release URL, archive size and SHA-256. No manifest-defined
   command, installer arguments or arbitrary download target is accepted.
2. Extract only bounded regular files into
   `/opt/agentic-home-bundles/<candidate digest>`. Reject traversal, links,
   special files, duplicate entries and mismatched manifests. Validate the
   explicit Host/Controller/reconcile contracts, database schema and unchanged
   OCI/runtime locks **and the baseline compose file**. Shared existing image
   assets remain in the retained baseline; they are not recopied or replaced
   by the Host bundle. A separately approved compatible Controller image may
   already be active through its existing runtime override; Host staging does
   not replace the other installed runtimes or change their boot references.
3. Under the Host lifecycle lock, stop/gate the Controller and check root LVM
   identity, free extents and existing snapshot capacity. Take an HAOS cold disk
   and UEFI snapshot when provisioned, returning HA to its original running
   state, then create a tagged root LVM snapshot. Record intent before each
   effect and verify readback. Uncertain snapshots are not repeated. Existing
   snapshots are never deleted or merged.
4. Persist switch intent and atomically replace the active
   `/opt/agentic-home` symlink. The executor lives outside this tree, so restarting
   the broker does not kill recovery or change its imported code.
5. Keep the fixed UID1000-only update API running from the immutable engine on
   `/run/agentic-home/host-updates.sock`, separately from the selected Host broker.
   It accepts only the bounded Host-update routes, with the same owner Controller
   checks, process locks and one-use approvals. Legacy rollback therefore cannot
   remove the maintenance page's update/status path. Run the fixed idempotent
   reconcile hook, restart product services and probe
   the actual Unix broker, its explicit contracts, Controller health and current
   database integrity/schema. A candidate cannot change the recovery executor,
   its API/recovery units, publisher pin or the first-boot installer.
6. On failure, restore the prior bundle and product-managed file preimages,
   restart and probe again. Boot recovery compensates an interrupted approved
   switch; it never turns a merely staged candidate into a new installation.
   An unconfirmed recovery remains visibly blocked for review.

Reconcile supports fixed product units, the product logind/netplan files and a
versioned `appliance_policy: 1` adapter for G15/G16. The builder adds that signed
opt-in when the candidate contains the appliance implementation. The immutable
engine derives policy from the installed profile and bounded, root-owned inputs;
it does not run a candidate hook. It journals before/after bytes, modes and the
five exact sleep-target masks, preserves unexpected or later owner edits, and
rebuilds dconf from the remaining inputs on rollback. It accepts no arbitrary
commands or paths. Power changes use logind HUP and verified readback. Netplan
and persistent WoL policy are generated and checked, then explicitly reported as
requiring reboot: no live DHCP edit, connection activation, reconnect, package
installation or runtime ethtool write occurs during a Host update.

Updater-managed installs retain a root-private marker suppressing the broker's
autonomous appliance writer under the shared lifecycle lock, including after
rollback. Candidates and compatible appliance-writing baselines must declare
`appliance_guard: 1` in the exact Host contract. The released 0.11.0 baseline,
which has no autonomous appliance module, is supported. The intermediate #122
engineering baseline without that guard is rejected before copying or stopping
services; it cannot safely be a rollback target. It needs a separately reviewed
migration, rather than a silent baseline patch.

The Controller durably fences ordinary product mutations before dispatching an
approved Host operation, including Controller-image updates. It retains that
fence across a lost reply until typed terminal readback or explicit retirement
of the exact unused offer. The owner can choose **確認未開始並重新檢查** for an
uncertain, expired offer: the fixed engine retires that exact capability under
its process lock before confirming cancellation. An already accepted operation
is read back instead; it is never cancelled or installed again by that action.
The healthy update path still requires one approval.

A retained legacy broker keeps its original code. The new Controller gate and
immutable engine process/lifecycle locks protect the product workflow; they do
not retrofit interception into a separate privileged caller using the old
broker directly. Concurrent direct legacy administrative operations are outside
this migration's concurrency guarantee. A lifecycle operation winning the lock
before any update effect causes a clear failed attempt and a fresh review,
rather than a permanently queued installation.

If #119's `root_cow` interface is present, capacity checks use it. The standalone
fallback validates exact LV/VG identity, checks existing snapshot headroom and
only extends the identified snapshot using free VG extents with readback. Unsafe
capacity blocks new copies/updates. It never powers HA off merely because an
existing restore point is critical or invalid. HA is briefly shut down only to
create the restore point explicitly authorized by the update.

The automatic rollback guarantee covers **bundle selection and product-managed
files**. Root LVM and HAOS snapshots are retained recovery material, not silently
merged/restored on a running household. Current Controller data stays in place;
only schema 3 and protocol 1 are supported. Arbitrary OS package migrations,
firmware, kernel/bootloader updates, arbitrary system-file writes and database
schema migrations need a future separately reviewed contract. Root LVM and
sufficient snapshot space are required; unsupported hosts stop before activation.

## Existing 0.11.0 household: first-time bootstrap

An installed 0.11.0 Host has no root bundle endpoint or immutable executor.
Updating its Controller cannot install either one. Rerunning `install.py --apply`
over the household, granting a worker root shell, or downloading and running an
unsigned script is **not** the migration.

The fixed API service is installed and started as part of the authenticated
bootstrap before either candidate is activated. It remains available when the
selected broker is the original 0.11.0 version. Controller transport falls back
to the old broker only if the fixed socket cannot be connected before a request
is sent; lost replies, timeouts and HTTP errors never cause a POST retry.

A maintainer must first produce authenticated bootstrap media using Ted's
approved public pin and externally signed Controller/Host envelopes. The small
bootstrap package carries the verifier, pin, signed envelopes and expected
payload hashes. Large image/archive payloads remain on the official USB volume
(label `AH-UPDATE`), or in the fixed root-owned local support staging directory
`/var/lib/agentic-home/update-inbox/agentic-home-update`, until the approved
capacity checks and restore point are complete. Every local staging parent,
directory and archive is checked for root ownership, non-writable permissions
and link safety; an unsafe existing inbox fails closed instead of using USB.
The graphical launcher finds either source without extra owner steps, and the
root-private copy is rehashed before it is parsed.
The fixed root-owned bootstrap metadata binds the removable payloads; trusting
the removable filesystem's ownership is not a substitute for authentication.
The graphical launcher reviews its
exact versions, fingerprints, candidate hashes and authority. The local OS
administrator authentication belongs to installing/launching that trusted
bootstrap, not a remote Controller root endpoint. The owner then approves the
single bootstrap review. Interrupted Controller and broker stop intents are
recorded before each stop and compensated only against the exact recorded
baseline. This also covers the period before the permanent executor exists.
The bootstrap takes restore points first, enrolls only
the exact approved official key, installs the immutable executor, converts the
old product directory into a retained baseline using an atomic directory/link
exchange, installs a compatible signed Controller when needed and applies the
exact signed Host bundle. It preserves household data and the prior tree.

The bootstrap OS package declares `python3-yaml`, `ethtool`, `netplan.io` and
`dconf-cli` prerequisites for the fixed appliance adapter. Their installation by
the trusted OS package channel happens outside Host bundle rollback. The package's
read-only 80 MiB pre-install capacity guard bounds its own at-most-16 MiB payload;
it does not bound package-manager dependency downloads or writes. Ted must verify
those prerequisites and adequate root/CoW headroom in the initial signed OS
package/media delivery. Do not claim whole-OS rollback for dependency changes.

The bootstrap's initial authenticity must already be established by the signed
OS package channel or independently authenticated signed media. A public key
inside an otherwise unauthenticated download cannot authenticate itself. The
approved repository pin does not authenticate the bootstrap's distribution path;
that delivery remains outstanding.

New installs from independently authenticated media carrying the approved pin
install an active immutable executor. The media's pin and default official
publisher enrollment must agree; an existing trust choice is not overwritten.
Valid engineering fixtures with explicit `pin: null` and
`image_repository: null` defer installing an active executor. If an earlier
engineering build already installed a null-pin executor, the local bootstrap
review must bind that exact verified inactive engine digest before its pointer
can move to a real pinned engine. The old directory is retained. This cannot
replace an already active signing key or silently rotate a trusted engine.

For the existing installation's owner, the one-time actions are:

1. Obtain the bootstrap through the authenticated official package channel or
   independently authenticated official USB that Ted has approved. Use the
   graphical package installer and authenticate with the local administrator
   account. Do not use an unsigned download merely because it includes a key.
2. Insert the prepared official update USB labelled **AH-UPDATE**, unless support
   has already prepared the protected local update inbox. Open
   **Agentic Home 主機更新** from the desktop applications menu; the launcher
   locates the files automatically. No terminal, SSH or manual mount is needed.
3. Review the shown public-key fingerprint, Controller/Host versions and
   interruption/restore-point scope against the authenticated release identity.
   Approve that exact bootstrap once. This also approves enrollment of that
   exact official key when it is absent from the current trust store, including
   after revocation. It cannot silently replace a different existing official
   key. This explicit owner enrollment differs from fresh-install defaults,
   which preserve an existing empty trust store.
4. Leave the machine and USB connected while the tool runs. On completion, use
   the maintenance page to read the installed versions. An interrupted or
   uncertain operation is recovered/read back; repeating the launcher does not
   silently authorize another installation.

After that one-time migration, future signed Host updates and rollbacks use the
existing maintenance page. Ordinary owners do not need SSH, sudo commands,
interface identifiers or a reinstallation. If the old Host is still installed,
the UI says that the one-time trusted bootstrap is required, rather than
pretending a Controller-only update delivered Host capabilities.

## Remaining delivery approvals and evidence

- The production public key and DER SHA-256 fingerprint were approved by Ted on
  2026-10-19 and are already pinned with the official publisher identity, stable
  release channel and exact image repository. Delivery must preserve that exact
  approved identity. A test key is never a production substitute; private keys
  stay outside the repository and build artifacts on the release side.
- An authenticated initial bootstrap/media distribution path, package signing
  and owner-visible identity verification. The bootstrap verifier itself must
  arrive through that authenticated path.
- A protected external signing pipeline binding canonical envelopes to exact
  source commits, supported protocols/schema, tar hashes, passing test evidence,
  issuance/expiry and immutable release assets. Host signing grants root product
  code authority and deserves stricter review than a Controller image update.
- The exact first household bootstrap candidate and compatible Controller/Host
  pair. CI/container evidence is engineering evidence; no physical Dell, real
  household or power-loss acceptance is claimed here.

Maintainer artifact preparation uses `scripts/build_host_update.py` against an
already-built public product directory plus `--baseline-product` pointing to
the independently authenticated complete installed release. It copies the
baseline compose file and image/provider/base/network locks into the new Host
archive, leaving the new build's runtime image IDs and tags out of this update.
This is required even when the new build also supplies a compatible Controller
prerequisite. It creates a bounded tar and an
**unsigned** envelope for the external signer, never a key or a signed release.
The signed wrapper published by the approved pipeline is
`agentic-home-host-update.json` containing `{manifest, signature}`. Its exact
versioned tar is `agentic-home-host-<version>.tar`. Nothing is auto-published or
merged by these tools.
