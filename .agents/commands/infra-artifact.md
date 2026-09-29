---
description: Inventory, materialize, publish, and independently verify canonical S3 artifacts.
---

# Canonical artifact lifecycle

S3 is canonical for embeddings, checkpoints, and explicitly persisted large outputs;
RunPod local disks and network volumes are rebuildable caches. One presigned URL is
issued per transfer, and workers never receive long-lived AWS credentials.

Skills: wavcse-artifact-pipeline
Boundaries: no-commit

## Sequence

1. Inventory — `infra storage list [--prefix <relative-key-prefix>] [--limit <n>]
   [--json]`. Keys are relative to `storage.prefix`. An absolute key, `..`, an empty
   segment, a trailing separator on an object key, whitespace, `?`, `#`, a URL, or a key
   that repeats the configured prefix is rejected rather than rewritten.

2. Metadata check — `infra storage verify <artifact> [--expected-size <bytes>]
   [--manifest <key> | --manifest-file <path>] [--json]`. This confirms existence, size,
   metadata, and manifest consistency without downloading the object, so it makes no
   cryptographic claim about content: a recorded digest is not confirmed by a
   metadata-only check, and an S3 ETag is not a SHA-256 digest. `--manifest` and
   `--manifest-file` are mutually exclusive.

3. Materialize — `infra storage download <artifact> <absolute-worker-path>
   --worker <exact-worker-id> [--expected-size <bytes>] [--expected-sha256 <hex>]
   [--concurrency <n>] [--overwrite]`. Content is proven here: pass `--expected-sha256`
   when the bytes are persisted data, not scratch. A size or digest mismatch materializes
   nothing and removes the temporary file; an existing destination is never replaced
   without `--overwrite`. This needs a `READY` worker with the direct SSH endpoint.

4. Publish — `infra storage upload <artifact> <absolute-worker-path>
   --worker <exact-worker-id> [--overwrite]`. An upload is a claim until re-verified: the
   command reports the worker-computed SHA-256 and then the controller's own check of the
   stored object, which confirms existence and size and explicitly reports that the object
   body was not downloaded. Re-verify the canonical bytes independently before treating
   the artifact as published, and never report a stored digest as proven from the upload
   output alone.

5. Manual presign — `infra storage presign-download <artifact> [--expires-in <seconds>]`
   and `infra storage presign-upload <artifact> [--expires-in <seconds>] [--overwrite]`.
   A URL is a bearer secret, printed to standard output because that is the requested
   product. Keep it out of tickets, documentation, MLflow parameters, and shell history;
   it expires. The default PUT URL signs an `If-None-Match: *` header the caller must
   send; `--overwrite` deliberately omits that guard.

6. Manifests — keep one version 1 manifest beside each dataset archive, carrying the
   digest a reading consumer actually proved. Recorded digests, sizes, and keys are
   evidence only for the object they name.

## Constraints

- The network-volume cache is rebuildable and never canonical: losing it must never lose
  the only copy of an artifact. Inspect it with `infra volume cache stats
  --worker <worker-id>`, never as a source of truth.
- Single presigned PUT uploads are limited to 5 GB; larger outputs need a separate
  multipart transfer workflow.
- `verify`, `list`, and `presign-*` do not modify an object; `upload` and `--overwrite`
  are deliberate, so state the intent before using them.
- This command lifecycle changes no repository state and creates no commits.

For pipeline stages, manifest conventions, and extraction layout, use the
`wavcse-artifact-pipeline` skill.
