# Paused history recovery foundation

This is the forward startup-recovery foundation and bounded offline recovery
candidate for #417. Detected unreadable existing SQLite history no longer produces an empty
healthy replacement. The default is **pause collection until explicit verified
recovery**. `recover_unreadable_database=False` continues to raise the original
SQLite failure without quarantining; it cannot bypass an existing recovery intent.
Unsupported schemas, transient open/lock failures and permission errors are not
classified as corruption. Schema admission's supported set is unchanged.

## State and artifact ownership

All new recovery evidence is in `<database>.recovery-required`, outside the
replaceable main database. The directory is private (0700); `intent.json` is 0600.
The existing lifecycle lock owns reservation, hashing and publication. Status
inspection itself is read-only and does not acquire the database lock.

| State | Authority and permitted behavior |
| --- | --- |
| No reservation | Ordinary schema admission may proceed. |
| Reserved directory, absent/incomplete/invalid intent | Recovery required; preserve source and evidence; no SQLite open, collection, mutation or automatic repair. |
| Valid `phase=intent`, before/during/after quarantine | Recovery required. Source and/or retained names contain evidence. No move retry, replacement creation, automatic finalization or clearing. |
| Intent inaccessible, malformed, unsafe or disappears after observation | Fail closed; observable pause remains. This is not a healthy first installation. |

The intent binds an opaque random ID, version 1, pause policy and a bounded set of
main/WAL/SHM/rollback-journal facts: device/inode, size, modification time and
SHA-256. Role names are fixed; the record contains no arbitrary paths or SQLite
exception text. Records reject duplicate keys, unknown versions/fields, invalid
shapes, unsafe modes/ownership, symlinks, hard-linked records and nonregular files.
Status reads at most 8193 record bytes and never hashes database payloads or opens
SQLite. `required` means an intent or finalization reservation, **not verified recovery
artifacts**. Verification of retained contents belongs to the explicit
recovery action; every status outcome other than `none` refuses normal operation.

## Publication order and interruption

1. Exclusively reserve the private recovery directory; sync the database parent.
2. Open available source roles without following links; reject nonregular or
   multiply linked sources. Retain descriptors, hash with bounded buffers, check
   stable identities and sync source bytes.
3. Exclusively write and sync the complete intent; sync its directory.
4. For each recorded role, check identity, publish a non-overwriting hard link,
   sync the recovery directory, check both identities and unlink the source name;
   sync the database parent before proceeding.
5. Keep intent and retained artifacts indefinitely. Never create an empty main.

The hard-link/unlink sequence preserves the original inode. An interrupted
publication can retain **both** names. Startup therefore observes recovery
intent before the ordinary admission lock's hard-link rejection, then rechecks
under that lock before any normal initialization. No recovery-artifact overwrite,
cleanup or re-publication is attempted on restart. Collisions and failed syncs
retain evidence and the pause. A failed reservation cannot promise durable
metadata on an unwritable filesystem; it retains the corrupt source and pauses
in memory, and the next startup retries detection rather than creating a new DB.

This inherits #416's **cooperative-lock** boundary, not atomic binding of SQLite's
internal path opens, hostile rename/ABA protection, or protection against in-place
writes by nonparticipants. The record is not cryptographically authenticated
against a malicious same-user writer. Successful fsync calls and process-crash
fault tests do not certify hardware/filesystem power-loss behavior.

## Service behavior

- The store can be constructed in a paused state, so the service can bind.
- Collector startup does not schedule collection while paused. Manual collection
  refuses before fetching source data; a running scheduler exits upon observing
  recovery state. Store connection/admission checks and segmented reads refuse
  pending recovery; ordinary store backup/restore entry points are gated too.
- `/livez` remains HTTP 200. `/healthz` returns HTTP 503 with
  `status=recovery_required`, `ready=false`, `recovery_required=true`,
  `collection_paused=true`, and a bounded `recovery_state` enum.
- `/api/history/recovery-status` is a safe HTTP 200 observation endpoint, not a
  readiness verdict or recovery action. It exposes only the three recovery
  fields above, without identifiers, paths, counts or SQLite error text.
- Other history service HTTP routes, including dashboard, reads and refresh,
  return a safe HTTP 503 while paused. They do not synthesize empty healthy
  counts. Existing non-recovery remote-collector error HTTP semantics remain
  unchanged.

## Offline read-only bundle admission (bounded successor)

`scripts/recover_paused_history.py` supplies inspect and dry-run admission.
The experimental apply/resume implementation below publishes selected bytes but
**does not complete recovery**. Separate explicit finalization is described below;
salvage remains unavailable.
A successful plan returns `state=admitted-plan-only` and
`restore_available=false`; it does not authorize a later write.

```text
python scripts/recover_paused_history.py inspect --database /absolute/history.db
python scripts/recover_paused_history.py restore-bundle \
  --database /absolute/history.db --recovery-id <32-lowercase-hex> \
  --intent-sha256 <64-lowercase-hex> \
  --bundle /absolute/selected.zip --bundle-sha256 <64-lowercase-hex> \
  --history-only --dry-run --resolved-topology unsegmented \
  --scratch-parent /absolute/private-scratch
```

Paths above are placeholders, not runtime defaults. The offline controller must
resolve configuration before explicitly asserting unsegmented topology. The
planner never discovers ambient configuration. For this bounded slice it requires
an otherwise dedicated database parent: only the database, its fixed SQLite
sidecars and the recovery-required directory may exist there. Other entries,
including any archive, catalog, segment, migration/activation/rotation marker,
operation record or staging inventory, refuse without cleanup. This conservative
restriction is deliberate, not support for arbitrary production layouts.

Local `inspect` exposes the validated ID and raw intent digest without hashing
payloads or opening SQLite. HTTP status remains unchanged and identifier-free.
The ID and digest select evidence and detect stale selections; they are not
credentials or proof of backup freshness or provenance.

Dry-run acquires the same nonblocking abstract socket and parent-directory flock
as ordinary lifecycle writers. Its private ownership primitive separates locking
from normal main admission; the shared authenticated evidence admission recognizes
exactly authenticated two-name interruptions. Normal callers still reject
hard-linked mains. Pinned intent and artifact descriptors bind device, inode,
size, mtime, SHA-256 and observed current ownership/mode/link count. Every role is
checked together and rechecked before returning. Original corrupt bytes are never
opened with SQLite. Divergent replacements, same-byte new inodes, extra links,
unrecorded sidecars, unknown entries and unsupported records remain untouched.

Only a plaintext ZIP containing one declared unsegmented history payload is
eligible in this slice. The existing archive validator checks physical structure,
paths, duplicates and aggregate bounds; exact bundle/member digests and declared
sizes are required. The private standalone payload must have supported schema,
backfill marker 1, full integrity and foreign-key checks. SQLite validation is
query-only with a bounded progress deadline. Empty/intermediate schemas,
segmented generations, other payload groups and encrypted/other archive formats
are unsupported. No DDL or backfill is run on the payload or retained evidence.

Scratch must be in an effective-owner 0700 directory outside the database tree.
Only the current private scratch workspace is removed on ordinary exit. A process
crash can leave scratch there; subsequent plans neither scan nor adopt it. No
source evidence, target-side receipt or marker is removed or rewritten. Exits
are 0 for a completed inspection/admission, 2 for invalid CLI arguments/tokens,
3 for cooperative lifecycle contention and 4 for admission refusal. No exit
claims recovery completion.

Stop nonparticipating writers for reliable offline observations. Locking does
not establish supervised shutdown, prevent hostile ABA or make third-party
SQLite writers cooperative. Migration/rollback/rotation/recovery publishers and
admin bundle imports use the common pause gate and lifecycle lock. That
coordination does not implement recovery or prove supervised shutdown.

## Experimental journaled apply, still paused

This developer candidate has an offline history-only publisher and explicit
journaled resume. Do not run this unreviewed candidate against live history.
Publication and replay deliberately leave the recovery root present; only the
separate explicit finalization command can complete the archive protocol.

For synthetic execution, replace `--dry-run` above with `--apply --offline
--accept-backup-history --publication-mode 0600 --publication-uid <effective-uid>
--publication-gid <effective-gid>`. Both IDs must match the running process.
There are no publication defaults. The operator must stop every writer and
prevent restart; `--offline` only acknowledges that requirement. All existing
bundle, topology, ownership and evidence admission limits still apply.

Under one continuous exclusive lifecycle lock, apply creates a private target-local
`candidate.sqlite3`, verifies its hash/schema/integrity, and exclusively writes and
syncs immutable `recovery-operation.json`. It retains each original inode with
link, directory sync, authenticated unlink and parent sync. It publishes the
candidate into the absent main name without overwriting, syncs it, removes only
the authenticated staging alias, and independently verifies the active bytes and
schema. An exclusive `completed.json` records `phase=applied-paused` and
`recovery_completed=false`. That file is an apply receipt, not recovery completion.
Original intent and evidence remain under the original recovery root.

Even a verified apply or resume returns exit **5**, `state=applied-paused`,
`recovery_completed=false`, and `resume_available=true`. Errors after target
mutation return exit 5 with `state=refused`; inspect the state, not exit 5 alone.
Every remaining artifact stays in place. There is no automatic rollback,
ordinary-restore shortcut, marker removal or archive move. Fresh stores remain
paused and existing stores stay latched.

### Explicit pending replay and its trust boundary

For synthetic replay of a selected complete v1 operation:

```text
python scripts/recover_paused_history.py resume --database PATH \
  --recovery-id ID --intent-sha256 SHA --offline --apply \
  --resolved-topology unsegmented \
  --operation-sha256 OPERATION_SHA --operation-identity DEVICE:INODE
```

If `completed.json` exists, also supply `--receipt-sha256 RECEIPT_SHA` and
`--receipt-identity DEVICE:INODE` for that exact receipt. Omitting its anchors,
supplying anchors for a missing receipt, or using stale anchors refuses without
mutation. Both identities use literal nonnegative decimal device and inode
numbers separated by one colon, without spaces or leading zeroes. Digests and
IDs retain the existing literal lowercase-hex requirements. No bundle argument
is accepted by resume; it cannot select new history.

Successful apply and resume return local `operation` and `receipt` facts with
`dev`, `ino` and `sha256` for a later invocation. Keep these facts in the offline
controller's trusted operation record. They are not returned through HTTP status.
After an interrupted apply that did not return these facts, trusted offline
assessment must select the complete journal and any existing receipt. This CLI
does not automatically authenticate an unknown journal merely by hashing it.
Do not pipe a fresh hash/stat of an unassessed file into resume and call that
provenance verification. The synthetic tests intentionally construct the trusted
selection from task-owned fixtures before exercising stale-selection refusals.

This extra selection is necessary for accepted v1 records. The earlier immutable
intent does not bind a subsequently created operation's inode or digest. The
operation cannot bind the future receipt's identity either. A self-hash is not an
authentication chain. External anchors supply that missing selection boundary;
they are stale-selection guards, not signatures, credentials, or protection from
an owner who can alter both evidence and the trusted controller record. A
self-contained provenance sealing protocol is still deferred; finalization also
requires trusted external selections for existing later records.

Resume acquires the existing lifecycle socket and parent-directory flock once
and holds both through final readback. It bounds operation and receipt reads to
16384 bytes, rejects unknown/duplicate fields and wrong types, and binds intent
identity/hash, original evidence, observed ownership/mode, source selection,
candidate identity/bytes and fixed names. Original evidence is never opened as
SQLite. Candidate schema/backfill, full integrity and foreign keys are checked
again, without initialization or retained-byte salvage.

A complete prepared journal and authenticated candidate can replay original-only,
retained-only and authentic two-name roles. Candidate publication is non-overwriting;
a journal-authenticated active candidate with one or two names can finish the
staging unlink and active readback. Complete files interrupted before fsync are
re-synced before destructive replay. An existing authenticated apply receipt is
re-synced and reverified without rewriting it. The immutable operation and original
intent stay byte-identical. Replay converges only to `applied-paused`.

Missing/partial operation files, unjournaled staging, partial receipts, unexpected
sidecars, inode replacements, conflicting anchors and unknown topology refuse
with exit 4 before mutation. Partial files are never overwritten or deleted. A
crash before any target staging can still admit a fresh explicit apply; that is
not resume. Use the separate explicit finalization command below after applied-paused.

## Explicit evidence-preserving finalization and archived replay

This command is for offline synthetic validation until the candidate is independently
accepted. Keep all producers and publishers stopped, including automatic restarts.
After selected apply or pending resume reaches `applied-paused`, invoke:

```text
python scripts/recover_paused_history.py finalize --database PATH \
  --recovery-id ID --intent-sha256 SHA --offline --apply \
  --resolved-topology unsegmented \
  --operation-sha256 OPERATION_SHA --operation-identity DEVICE:INODE \
  --receipt-sha256 RECEIPT_SHA --receipt-identity DEVICE:INODE
```

The command holds the same lifecycle socket and parent-directory flock throughout.
It authenticates the selected operation, apply receipt, intent, every retained
original and active candidate; it independently checks supported schema, marker 1,
full integrity and foreign keys without initialization. It creates an exclusive
0600 `<database>.recovery-finalizing` record and syncs it and the parent before
moving the entire original recovery directory, using Linux `renameat2` with
`RENAME_NOREPLACE`, to `<database>.recovery-archive-<ID>`. Any archive collision
refuses. No original, intent, operation or apply receipt is deleted or rewritten.

The separate durable gate remains visible to every normal startup/publisher while
the archive rename is synced and the exact archive and active main are independently
reopened and verified. Only then is exclusive `finalized.json` written, synced and
read back in the archive. It records genuine `recovery_completed=true` evidence
bound to the selected operation, receipt, gate, directory identity and active bytes.
After another complete verification, the gate itself is moved without replacement
into that archive as `finalization-gate.json`. Both directories are synced and
archive/main readback repeats. The archive itself remains a durable refusal gate
after the parent gate moves. Startup never treats absence of that gate as proof
of completion.

Only after all five archive/main readback groups and both gate-move directory
barriers succeed does finalization atomically create a private empty directory
inside the archive named `committed-<completion SHA256>-<device>-<inode>`.
This exclusive `mkdir` is the irreversible terminal decision. Its name stores
the explicitly selected completion anchor without a partially writable JSON
record. Finalization validates the startup predicate, syncs the empty terminal
directory, then syncs the archive before reporting success. It never removes,
replaces or rolls back the terminal decision.

Normal startup independently requires the private empty terminal directory and
validates its selected completion receipt, the receipt's operation/apply/gate
bindings, intent, recovery-directory identity, exact archive inventory and
retained original bytes. Missing, partial, malformed, unsafe or inconsistent
archive state refuses startup before SQLite initialization. Parent enumeration
is bounded to 4096 entries and one recovery archive. The active database is not
hashed against its pre-startup digest here, because ordinary initialization and
later writes legitimately change it. Ordinary schema admission still applies.
This read-only observation does not create a commit or complete an interrupted
protocol. Archives produced by the earlier gate-only finalizer remain paused
until explicit selected finalization creates the terminal decision.

The terminal name is a local current-owner protocol receipt created after
explicit verified recovery, not a signature or a newly discovered external
provenance anchor. The threat model still excludes an owner who fabricates both
the terminal decision and its evidence. Startup does not select records merely
by hashing whatever files happen to be present.

An interrupted finalization is resumed by invoking **finalize again**, not by
ordinary startup or the earlier paused `resume` command. Existing gate records
require `--gate-sha256` and `--gate-identity`; existing `finalized.json` requires
`--completion-sha256` and `--completion-identity`. These externally trusted
selections are mandatory whether the gate is still in the parent or archived.
Use returned facts from successful finalization or trusted offline assessment of
task-owned evidence after a crash. A fresh self-hash does not establish provenance.
Complete selected records are re-synced without rewriting; partial records refuse
and remain intact. Missing selected records, swapped inodes (even identical bytes),
unknown entries, sidecars, both/neither pending and archive, or changed active bytes
refuse. Archived replay is idempotent only while writers have remained stopped.

Exit 0 and `state=completed` report verified finalization; `restart_required=true`
means a fresh process may now perform ordinary admission. Existing stores remain
latched and collectors are never automatically restarted. Before the terminal
attempt, post-mutation failures return exit 5, `state=refused`, and reason
`finalization-incomplete-inspect-and-replay`. These states persistently refuse
startup, including either post-gate-move fsync failure and the fifth schema
readback failure. The caller need not enforce that pause externally.

Once terminal creation is attempted, an error returns exit 5 with
`state=outcome-unknown` and `recovery_completed=null`. The operation may already
have committed. Startup uses the retained terminal predicate, not the exit code.
If the terminal is missing or invalid it refuses; a valid committed archive is
admissible. On replay of an independently validated existing commit, later
failure instead reports `state=completed-replay-failed` and
`recovery_completed=true`, never incomplete. A refused malformed request is
still a request refusal, not proof that a previously committed recovery reverted.

An error from the final persistence barrier can mean either that it persisted or
that it did not. No finite additional acknowledgement file can eliminate that
last-boundary uncertainty. Process death or stdout failure after commit likewise
cannot revoke it. This protocol promises no healthy startup for an incomplete
terminal protocol, not that every nonzero exit implies durable pause. A successful
return follows both terminal durability barriers; power-loss behavior still
depends on the filesystem honoring its durability contract. Explicit replay can
re-establish durability while writers remain stopped. Never recreate markers,
roll back, delete evidence or use ordinary restore to acknowledge the warning.
Preserve the archive indefinitely; retention deletion is outside this authorization.

This remains plaintext, history-only, unsegmented, private current-owner 0600
publication on a dedicated parent. No salvage, signatures, noncooperating-writer
protection, hardware power-loss guarantee or live-controller shutdown proof is
provided. Linux no-replace support is required; there is no replacing fallback.

## Deliberately incomplete acceptance

Remaining #417 work includes corruption first detected during an already-running
connection/collector pass (this slice detects corruption at normal initialization),
main UI/backend and admin readiness/upgrade propagation and recovery UX,
runtime verification of coordinated publishers, bounded legacy `.broken-*` assessment, and isolated container/runtime
contention and throughput acceptance. These are not supplied by the new service
HTTP gate. #416's full interrupted migration/backfill/row/count acceptance is
also still open. No release or complete #416/#417 acceptance is implied.

Synthetic tests extend the already POSIX-classified schema suite. They exercise
startup pause, missing/readable replacement restarts, private bounded metadata,
unsafe records, retained inode/digest facts, non-overwriting collisions,
persistent replacement refusal, collector pause, ASGI liveness/readiness/mutation
boundaries, and two fresh-process starts after each before/after fsync, link and
unlink fault. No real history database is used.
