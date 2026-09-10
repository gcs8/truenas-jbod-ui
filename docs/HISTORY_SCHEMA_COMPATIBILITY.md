# History schema admission

## Scope

`history_service.schema_compatibility.classify_schema(connection)` is a read-only
classifier. HistoryStore now calls it before configuring or initializing an
existing database, including lazy `initialize=False` connections and reconnects.
This is the startup-admission slice of #416, not issue closure or the durable
paused-recovery implementation in #417.

The function requires an already validated connection with `query_only=ON`.
It never opens a source pathname, sets pragmas, executes source schema SQL,
reads application rows, initializes a database, or quarantines files. Reference
DDL runs only in private in-memory SQLite connections.

The store supplies lifecycle locking, WAL-aware opening, retained source identity
checks and pending-marker checks. The classifier itself remains independent of
that admission layer. `query_only` alone does not establish that opening SQLite
made no filesystem changes; the sidecar limitations below are part of the
admission contract.

## Supported source contracts

`schema_contracts.json` freezes the public DDL, optional ALTER ordering and
identity-index declarations from these tags. Every record contains its full
source commit. `scripts/freeze_history_schema_contracts.py` regenerates the
contracts from named Git objects without importing the service or reading a
history database. Tests also contain frozen initialized release SQL, independent
of the current `store.SCHEMA` value.

| Representative | Released initialized family |
| --- | --- |
| v0.9.0 | v0.9.0 through v0.10.0; inspected v0.8.0 tag shares this shape |
| v0.11.0 | v0.11.0 through v0.21.2; disk identity additions |
| v0.22.0 | v0.22.0 through v0.22.1; rollups, counts, triggers |
| v0.22.2 | v0.22.2 through v0.23.0 and the candidate base; retention maintenance state |

The classifier enumerates initialized shapes and each actual CREATE/ALTER/index
prefix from a fresh database and from each initialized earlier representative.
It does not infer compatibility from an arbitrary subset of familiar tables.
It does not yet enumerate every possible upgrade from an interrupted older
initializer into a newer initializer.

Comparison uses sorted semantic column metadata and tokenized object definitions.
Column order and SQL whitespace outside literals do not affect comparison.
Primary-key column ordering, nullability, types, defaults, CHECK expressions,
AUTOINCREMENT, index definitions and trigger bodies remain significant. Unknown
objects and changed definitions fail closed. This is a conservative comparison
of released declarations, not a general SQL equivalence solver. Alternate
comments or spellings not found in those declarations can be refused.

## Results and fault boundaries

- `supported`: a released initialized shape, including ALTER-upgraded order.
- `intermediate`: an enumerated released initialization prefix.
- `empty`: an existing connection with no schema objects. This is **not** proof
  of first install, and never overrides external recovery evidence.
- `SchemaIncompatibility`: a bounded `backfill_marker`, `object_definition`, or
  `schema_bounds` reason. The exception is a ValueError, not a SQLite error,
  and includes no source paths, SQL, object names or application rows.
- SQLite errors propagate separately. Classification is not an integrity check
  and does not map unreadable data, permission failures or contention to empty.

The observed SQLite `user_version` values are 0 and 1. Value 1 records completion
of the identity backfill, **not a comprehensive schema version**. Other values
are refused even for an empty or recognizable schema. A familiar marker does
not authorize foreign objects. The classifier never stamps `application_id`.
Bundle versions, segmented catalog versions, migration journal versions and
application release numbers remain separate namespaces.

## Startup and connection ownership

Admission runs under the existing Linux lifecycle lock, after pending-marker
refusal and before existing parent/file permission normalization, WAL setup,
DDL, backfill or count synchronization. Missing files are exclusively created
with the configured shared mode; existing zero-byte files remain an accepted
interrupted-create state. Neither case bypasses pending lifecycle markers.
`initialize=False` defers schema initialization, not connection admission.

Each open retains an `O_NOFOLLOW` main-file descriptor and checks device/inode,
link count and parent identity before and after classification and SQLite open.
Main-file symlinks, hard-link aliases, file mounts and unsafe sidecar links are
refused. Existing canonical-parent locking unifies directory aliases.

A normal `mode=ro` handle first reads the committed WAL-visible schema. The
actual writable handle is then put in `query_only` mode and classified again;
it is this same handle, not a later pathname reopen, that receives WAL setup
and application operations. On admission failure the writable handle closes
before the read guard, avoiding last-writer checkpoint-on-close. No
`immutable=1`, application-ID stamp or new version number is used.

The private connection adapter retains its acquired lifecycle lock until close.
Its context manager commits/rolls back and **also closes**, unlike a raw SQLite
connection context manager. Callers supplying `migration_lock_held=True` retain
responsibility for the enclosing lock through the entire use/close interval.
Connection operations and cursor execution/fetch/iteration check main, parent,
WAL/SHM identity and pending markers; reconnects reclassify instead of trusting
the journal-mode cache. Rollback journals are allowed their normal per-commit
creation/deletion, but may not be symlinks or hard-link aliases.

These are cooperative lifecycle guarantees with detection of persistent
out-of-band replacements. They do not claim protection from a hostile actor
performing rename/ABA races or in-place writes while ignoring the lock, nor
atomic kernel binding between Python pathname checks and SQLite's internal
opens. Use the supported lifecycle operations rather than replacing files under
live handles. The stronger lock lifetime also serializes store reads against
lifecycle writers; runtime throughput and contention acceptance remain separate
from synthetic source tests.

## Refusal byte-preservation boundary

Synthetic non-WAL unsupported-marker, foreign-object and conflicting-definition
fixtures retain every database-file byte, file mode, parent mode and directory
entry on refusal. Schema failures are typed `ValueError`s and cannot enter
SQLite corruption quarantine. Read-only faults cannot bypass classification to
trigger permission repair of an unknown source.

WAL fixtures cover both a live writer and an abruptly exited synthetic writer
with committed uncheckpointed schema. Refusal preserves main and existing WAL
bytes. SQLite may create/rebuild SHM, update its read marks, and create an empty
WAL sidecar when opening a checkpointed WAL-mode database. **SHM byte neutrality
and an unchanged sidecar inventory are not promised.** No live database was used
to establish these results. The lifecycle lock uses an abstract Unix socket and
a directory flock, not a persistent `.migration.lock` file.

## Remaining acceptance and recovery work

Released-family rows survive repeated admitted startup. The classifier's
CREATE/ALTER prefix matrix is not a complete process-crash startup matrix:
interrupted-older-to-newer upgrades, every backfill/count commit boundary and
full event/rollup/identity preservation still need dedicated acceptance. No
PR483 committed-batch implementation or acceptance is implied. Backup/import
publication policy is not redesigned here; startup refusal after publication
must not be described as a pre-publication restore gate.

The approved #417 policy is to pause collection and persist recovery-required
status until explicit verified recovery. It is not implemented by this module.
The next persistence milestone must journal intent before any rename, retain
opaque source/sidecar evidence across restarts, fail closed on malformed records,
and keep safe liveness/status available without an empty healthy fallback.
No clearing endpoint or broad restore UI is part of this candidate. Existing
automatic quarantine behavior remains unchanged until that milestone is wired
and verified as a whole.
