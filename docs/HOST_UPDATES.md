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
pin and the enrolled official publisher must agree. An absent/null pin disables
checking, staging and application. A moving GitHub branch, HTTPS, an image tag,
repository ownership and checksums do not create a publisher trust root.

The checked-in descriptor has **no production pin**. No production signing key,
signed Host release, signed bootstrap package or product-signed ISO is created
or published by this change. Existing ISO checksums and Ubuntu's signed boot
chain do not authenticate our remastered product media.

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
   OCI/runtime locks. Shared existing image assets remain in the retained
   baseline; they are not recopied or replaced by the Host bundle.
3. Under the Host lifecycle lock, stop/gate the Controller and check root LVM
   identity, free extents and existing snapshot capacity. Take an HAOS cold disk
   and UEFI snapshot when provisioned, returning HA to its original running
   state, then create a tagged root LVM snapshot. Record intent before each
   effect and verify readback. Uncertain snapshots are not repeated. Existing
   snapshots are never deleted or merged.
4. Persist switch intent and atomically replace the active
   `/opt/agentic-home` symlink. The executor lives outside this tree, so restarting
   the broker does not kill recovery or change its imported code.
5. Run the fixed idempotent reconcile hook, restart product services and probe
   the actual Unix broker, its explicit contracts, Controller health and current
   database integrity/schema. A candidate cannot change the recovery executor,
   its units, publisher pin or the first-boot installer.
6. On failure, restore the prior bundle and product-managed file preimages,
   restart and probe again. Boot recovery compensates an interrupted approved
   switch; it never turns a merely staged candidate into a new installation.
   An unconfirmed recovery remains visibly blocked for review.

Reconcile supports a fixed set of product unit files, the product logind drop-in
and a product netplan file. It records before/after bytes and modes and verifies
ownership. It refuses to overwrite unexpected owner edits; rollback also keeps
later owner edits and reports the conflict. It accepts no shell commands or
arbitrary paths. Network configuration requiring activation is reported as
requiring a restart rather than applying a live network change behind the
owner's back. G15/G16 implementations can ship their reviewed declarative files
through this hook; this PR does not replace those concurrent fixes.

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

A maintainer must first produce an authenticated bootstrap package/media using
Ted's approved public pin and externally signed Controller/Host envelopes. The
bootstrap payload is fixed and root-owned; the graphical launcher reviews its
exact versions, fingerprints, candidate hashes and authority. The local OS
administrator authentication belongs to installing/launching that trusted
bootstrap, not a remote Controller root endpoint. The owner then approves the
single bootstrap review. The bootstrap takes restore points first, enrolls only
the exact approved official key, installs the immutable executor, converts the
old product directory into a retained baseline using an atomic directory/link
exchange, installs a compatible signed Controller when needed and applies the
exact signed Host bundle. It preserves household data and the prior tree.

The bootstrap's initial authenticity must already be established by the signed
OS package channel or independently authenticated signed media. A public key
inside an otherwise unauthenticated download cannot authenticate itself. The
current repository cannot supply this missing production trust decision.

After that one-time migration, future signed Host updates and rollbacks use the
existing maintenance page. Ordinary owners do not need SSH, sudo commands,
interface identifiers or a reinstallation. If the old Host is still installed,
the UI says that the one-time trusted bootstrap is required, rather than
pretending a Controller-only update delivered Host capabilities.

## What Ted must approve before delivery

- The production **public** Ed25519 key and independent DER SHA-256 fingerprint,
  official publisher identity, stable release channel and exact image repository.
  A test key is never a production substitute; private keys stay outside the
  repository and build artifacts.
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
already-built public product directory. It creates a bounded tar and an
**unsigned** envelope for the external signer, never a key or a signed release.
The signed wrapper published by the approved pipeline is
`agentic-home-host-update.json` containing `{manifest, signature}`. Its exact
versioned tar is `agentic-home-host-<version>.tar`. Nothing is auto-published or
merged by these tools.
