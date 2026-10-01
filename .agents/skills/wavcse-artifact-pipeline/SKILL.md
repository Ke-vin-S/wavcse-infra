---
name: wavcse-artifact-pipeline
description: Move, verify, cache, package, and publish wavCSE research artifacts across S3, network-volume cache, and scratch; use when acquiring, transferring, manifesting, uploading, or cleaning up artifacts.
---

# wavCSE Artifact Pipeline

Use this skill whenever artifact bytes move between source acquisition, a controller, a
worker's scratch disk, a mounted network volume, packaging, and canonical storage.

The governing rule is that location is not identity: an artifact is trusted because its
identity and invariants were verified, not because a file with the expected name exists.

## Three-layer storage model

    S3 (canonical)     authoritative durable bytes; the only permitted home of the only
                       copy of a canonical research artifact
    network volume     verified rebuildable warm cache attached to a Pod; losing the
                       volume must lose nothing canonical
    container disk     ephemeral scratch; rebuilt from canonical storage on demand

No non-canonical layer may ever hold the only copy. A checkpoint, embedding archive, or
dataset that matters is published to canonical storage before it is treated as durable,
and a warm cache copy is a convenience, never a substitute. The cache never receives a
presigned URL, so the mounted volume cannot become a credential store.

## Artifact identity

An artifact's identity is the SHA-256 of its bytes, recorded as 64 lowercase hexadecimal
characters.

These are not identity:

- an S3 ETag (multipart uploads make it depend on part boundaries; it is never compared
  against a recorded digest);
- a filename or object key;
- a byte size alone;
- a presigned URL, its lifetime, or its presence.

Object keys are relative, normalized, and unambiguous beneath the configured namespace
prefix. Absolute keys, buckets, URLs, `..`, `//`, whitespace, `?`, and `#` are rejected
rather than rewritten. A dataset archive's sidecar manifest key is its `.tar` key with
`.manifest.json` appended.

## Cache-hit criteria the code enforces

A network-volume entry is used only when the requested digest, the recorded metadata, and
the bytes on disk all agree. All of the following must hold:

- `<cache-root>/artifacts/sha256/<first-two-digest-hex>/<digest>/` is a real directory,
  not a symlink;
- `metadata.json` parses as a JSON object with `schema_version` 1, `sha256` equal to the
  requested digest, and a non-negative integer `size_bytes`;
- `content` opens without following a symlink and is a regular file;
- the actual byte size and actual SHA-256 of `content` equal the requested identity, and
  the expected size when one was supplied;
- any recorded size matches the actual size.

Anything else is a miss or an integrity failure. An entry that contradicts its recorded
identity is moved aside under `staging/quarantine-<digest>-<random>`, reported, and
treated as a miss so the next canonical download rebuilds it.

The cache is consulted only when the declared input has a SHA-256. An input identified
only by size is downloaded from canonical storage, because a content-addressed cache
cannot answer a question about an unidentified artifact.

Bytes enter the cache only from a file the canonical download already verified, and no
presigned URL is ever generated for a cache operation. A miss, a quarantined entry, an
unusable cache root, an unsafe path, a destination already owned by another writer, or an
interrupted lookup all degrade to a warning and fall through to canonical storage. Only
the canonical path's own outcome may fail a job: a warm cache can only make a job faster,
never make it fail. Inspect recorded contents with the `infra volume cache stats`
command.

## Source verification before use

Treat a downloaded dataset or archive as untrusted input.

- When the source publishes a checksum, verify against it before use.
- A mismatch against a published checksum is a hard stop for that dataset: do not
  extract, do not compute on it, do not continue. It is never a warning.
- When no authoritative checksum exists, record that fact as provenance and verify size,
  structure, and membership instead. Never claim upstream byte-identity you cannot show.
- Inspect archive metadata and expected structure before extraction; reject unsafe
  members (absolute paths, traversal, escaping links, devices/FIFOs) and extract into a
  fresh bounded directory.
- Never execute files from a dataset archive, and never `curl | bash` for research data.

## Transfer semantics

Transfers use time-limited, object- and action-scoped presigned URLs, signed
only after worker readiness. RunPod carries the URL on direct SSH stdin.
Colab uploads a 0600 local, short-lived secret-bearing envelope through the
CLI file API; a non-secret launcher consumes and deletes the remote file.
Neither transport puts bearer URLs in normal `exec` source, arguments, ordinary
logs, durable job state or Git. The trusted controller accepts only the
pinned CLI's restricted local history exception (see `docs/COLAB.md`).

- One PUT cannot exceed the single-PUT ceiling of 5 GB. A larger artifact must be sharded
  or repackaged smaller; multipart upload is not implemented.
- Downloads stage beside the destination under a destination-wide lock. Nothing reaches
  the destination path until the assembled file matches the expected size and, when
  supplied, its digest.
- A resumable download requires an expected SHA-256. Its resume record is bound to that
  digest, names the size, granularity, and completed inclusive ranges, and holds no
  bearer material; a record lacking the digest is discarded, never trusted.
- A ranged response must carry HTTP 206 with a valid, consistent `Content-Range`. An
  endpoint that ignores `Range` falls back to a single connection; a full `200` body is
  never accepted as a requested partial range.
- Placement links the destination from the verified open inode and then proves the
  created entry is that inode. It never moves a pathname, so a replaced staging path
  cannot redirect what lands.
- The digest is computed while streaming: incrementally on download, and over exactly the
  bytes handed to HTTP on upload. An upload whose streamed size differs from the source
  size fails.
- A default PUT is signed with `If-None-Match: *`, so an existing persisted object is not
  silently replaced; `--overwrite` removes that condition deliberately.

Trust is divided. The worker holds one time-limited URL and no durable credentials, no
AWS credentials, no controller SSH key, and no RunPod key. The controller owns identity:
it decides the expected digest, records what canonical storage actually returned, and
performs the verification that admits an artifact.

## Manifest schema (version 1)

A manifest is the reviewable record of how one reusable object was produced. Required
fields:

- `schema_version` — exactly `1`;
- `artifact_name`, `artifact_type` — non-empty labels;
- `object_key` — the key relative to the configured namespace prefix;
- `size_bytes` — non-negative integer;
- `sha256` — 64 lowercase hexadecimal characters;
- `created_at` — timezone-aware, stored in UTC.

Optional fields, present only when actually known:

- `dataset`;
- `generator_git_commit` — a full 40- or 64-character commit ID, never a branch name;
- `extracted_destination`;
- `notes`;
- `metadata` — a flat map of non-empty string keys to non-empty string values.

Unknown values stay absent instead of being guessed. Never record a generator commit, a
dataset name, or an extracted destination you did not observe. The schema forbids extra
keys, is immutable, serializes deterministically (sorted keys, fixed indent, trailing
newline, absent optionals omitted), and refuses presigned-URL or credential material in
any text field.

## Deterministic packaging

A re-run over the same inputs with the same code must reproduce the same digest, so a
digest can be compared across runs and machines.

- Normalize archive member metadata: fixed modification time, owner/group and names, and
  mode; never embed host paths or a wall-clock creation time.
- Order members and choose shard boundaries explicitly from a sorted member list, never
  from filesystem iteration order.
- Bound each shard below the single-PUT ceiling with headroom for the transport.
- Keep the sidecar manifest with the archive so the package is independently inspectable.
- Verify byte integrity and semantic membership. A tar digest alone does not prove the
  package holds the correct samples.

## Publication is not complete at upload acknowledgement

An HTTP PUT success, a HEAD size check, or a plausible key is not durable publication.

    download material from canonical storage
      -> re-verify it (size, digest, structure)
      -> record the digest and size
      -> upload
      -> read the canonical object back and hash it
      -> compare against the recorded identity
      -> mark canonical

The only acceptable evidence that canonical bytes are correct is an independent read-back
that streams the stored object through SHA-256 and matches the recorded size and digest.
A declared job output is held to exactly that rule; when the bucket reports a version id,
the read-back is bound to the version the metadata read observed. `infra storage verify`
alone proves existence, size, and manifest consistency, and explicitly does not prove
content — never present it as content verification.

## Producer/verifier common-mode failure

A validation that shares the producer's assumption can pass while the artifact is wrong:
hashing the same file the producer wrote, reusing the producer's member list, or trusting
a self-reported count proves only self-consistency. Require an oracle derived
independently of the producer — expected membership from the source checkout, expected
IDs/labels from the dataset's own metadata, or a loader that reconstructs the split — and
check semantics, not only bytes.

## Cleanup rules

May be deleted, once it is provably rebuildable or already canonical elsewhere:

- container scratch, including a worker's own temporary files;
- cache staging and quarantine entries under `<cache-root>/staging/`;
- a verified cache entry, because canonical storage can rebuild it;
- a transport archive, after its extracted form has been verified against the manifest.

Never delete:

- a canonical object;
- the only copy of anything, including a cache entry that is still the sole verified
  materialization before canonical publication;
- the destination, staging state, or lock of an in-flight transfer.

Volume cleanup is operator-managed, for example
`infra worker exec <worker-id> -- rm -rf <path>`. Destroying a volume or Pod is not a
cleanup step for canonical data and never removes an S3 object.

## Readiness states to report

Report an artifact's state honestly and never a stronger one than the evidence supports:

- source verified — the source's own checksum or contract was checked;
- generated — the producer wrote output, which is not yet correctness;
- validated — an independent oracle confirmed structure and membership;
- canonical — the bytes are stored at the object key;
- independently verified — the canonical object was read back and hashed to the recorded
  identity;
- warm in cache — a verified entry exists on a mounted network volume.
