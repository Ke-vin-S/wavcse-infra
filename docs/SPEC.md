# wavcse-infra — Technical Specification

Status: v1 design
Primary workload: wavCSE multi-task speech research
Primary GPU provider: RunPod
Controller provider: AWS EC2

# 1. Problem

wavCSE research requires repeated GPU experiments across multiple tasks, seeds, controls, and configurations.

GPU capacity may come from temporary cloud machines. These machines should be created when compute is needed and destroyed afterward.

The research workflow also requires precomputed embeddings. Generating these embeddings is GPU-expensive, but the resulting data is relatively small enough to store permanently, approximately 20 GiB across three datasets.

The system must therefore separate:

- long-lived research/control state
- source code
- reusable data
- experiment metadata
- expensive disposable GPU compute

# 2. Goals

The infrastructure must make it inexpensive and safe to use ephemeral GPU machines.

Primary goals:

1. Keep OMP and research decision-making on a persistent controller.
2. Allow GPU workers to be disposable.
3. Execute reproducible research from exact Git commits.
4. Generate embeddings once and reuse them.
5. Prevent loss of important results when workers disappear.
6. Make GPU cost visible.
7. Allow multiple workers to execute independent experiments concurrently.
8. Keep the initial implementation small enough to understand and maintain.

# 3. Non-goals

v1 is not intended to be:

- a general-purpose cluster manager
- Kubernetes replacement
- Slurm replacement
- MLflow replacement
- CI platform
- generic MLOps platform
- multi-user service
- web application
- always-running scheduler
- distributed-training framework

v1 primarily orchestrates independent jobs.

# 4. System model

    +--------------------------+
    |        GitHub            |
    |                          |
    | wavCSE                   |
    | wavcse-infra             |
    +------------+-------------+
                 |
                 | push/pull
                 |
    +------------v-------------+
    | AWS EC2 Controller       |
    |                          |
    | OMP / Codex / AGF        |
    | tmux                     |
    | wavCSE                   |
    | wavcse-infra             |
    | infra CLI                |
    +------------+-------------+
                 |
                 | RunPod API + SSH
                 |
        +--------+--------+
        |                 |
    +---v------+      +---v------+
    | GPU      |      | GPU      |
    | Worker A |      | Worker B |
    +---+------+      +---+------+
        |                 |
        +--------+--------+
                 |
       +---------+----------+
       |                    |
    +--v---+          +-----v------+
    | S3   |          | MLflow /   |
    |      |          | DagsHub    |
    +------+          +------------+

# 5. Controller

The controller is a small AWS EC2 instance.

It may be stopped when not required but is not routinely terminated.

The controller is responsible for:

- hosting OMP
- hosting Codex CLI and AGF as additional controller development tools
- hosting the writable wavCSE checkout
- hosting wavcse-infra
- creating/destroying GPU workers
- generating temporary storage access
- dispatching remote execution
- inspecting worker state
- maintaining lightweight operational state

The controller does not require a GPU.

The controller should not store the canonical 20 GiB embedding collection.

# 6. wavCSE repository

wavCSE remains the research source of truth.

OMP operates on the controller copy.

Typical flow:

    OMP
      ->
    modify wavCSE
      ->
    local checks
      ->
    commit
      ->
    push
      ->
    submit commit SHA to worker

Workers execute commits. They do not own research development state.

# 7. Worker model

A worker is a disposable GPU machine.

Initial provider:

    RunPod Pod

Worker states exposed by the application should be normalized into a small internal state model such as:

    REQUESTED
    PROVISIONING
    RUNNING
    BOOTSTRAPPING
    READY
    BUSY
    STOPPED
    FAILED
    DESTROYED
    UNKNOWN

Provider-native state should be retained for diagnostics.

Do not assume local state is always synchronized with RunPod.

Provider lifecycle and controller-observed readiness are separate. RunPod `RUNNING`
only means the Pod resource is running; local `READY` additionally requires a current
SSH endpoint, authenticated command execution, the expected bootstrap version, disk and
tool checks, and a supported healthy GPU. Disk health means usable ephemeral execution
storage when no volume was requested; persistent or network mount health is required only
when that storage was explicitly requested.

# 8. Worker profiles

## training

Default profile.

Contains only what is required to execute research.

OMP: no
Codex: no
Interactive IDE tooling: minimal
GPU stack: yes
wavCSE: yes

## development

Optional profile for GPU-specific debugging.

May contain additional development tools.

It is still not an authoritative source-code location.

Any fix that matters must return to the controller repository and be committed before a recorded experiment.

# 9. Provider architecture

v1 implements RunPod only.

Use a narrow provider interface so lifecycle logic does not directly depend on raw RunPod JSON.

Example conceptual interface:

    create_worker(request) -> Worker
    get_worker(id) -> Worker
    list_workers() -> list[Worker]
    start_worker(id)
    stop_worker(id)
    destroy_worker(id)

Do not build provider implementations for services that are not currently used.

# 10. RunPod requirements

Use RunPod's REST API.

The implementation must support enough configuration to request:

- GPU type
- GPU count
- cloud type
- image/template
- storage
- public IP/SSH requirements
- interruptibility policy

Worker creation must not silently substitute an expensive GPU outside configured limits.

The returned worker model should capture, where available:

- provider worker ID
- requested GPU
- actual GPU
- GPU count
- hourly price
- status
- public IP
- SSH port
- datacenter
- creation/start timestamps

# 11. SSH

SSH is the execution channel.

Primary operations:

    bootstrap
    health check
    git operations
    command execution
    log inspection

Basic RunPod proxied SSH and full public-IP SSH have different capabilities.

The architecture must not depend on SCP.

Data/artifact movement is handled independently through storage.

# 12. Embedding lifecycle

The first major workload is generating reusable embeddings.

Initial datasets:

- speaker identification / VoxCeleb-related embeddings
- keyword spotting embeddings
- emotion recognition embeddings

Current logical layout resembles:

    datasets/
      voxceleb/
        minpooling/
          utterance01/
            utterance01.pt

There are many small PyTorch tensor files.

Expected aggregate output:

    approximately 20 GiB

## Generation

A GPU worker may be created specifically to generate embeddings.

Flow:

    create GPU worker
        ->
    clone exact wavCSE commit
        ->
    obtain raw datasets
        ->
    generate embeddings
        ->
    validate embeddings
        ->
    archive each dataset
        ->
    calculate SHA-256
        ->
    upload archive + manifest to S3
        ->
    verify durable copy
        ->
    destroy worker

The worker must not be destroyed until durable storage is verified.

## Packaging

Use dataset-level TAR archives initially.

Compression is optional and should be benchmark-driven.

Do not redesign the wavCSE embedding format in the infrastructure project.

The extracted directory layout should remain compatible with wavCSE.

# 13. Embedding versioning

Embeddings must be namespaced by a version/identity representing their generation configuration.

Example:

    s3://bucket/wavcse/embeddings/wavcse-base-v1-minpool/
      manifest.json
      voxceleb.tar
      keyword-spotting.tar
      emotion-recognition.tar

The manifest should capture enough information to determine whether an existing embedding set can safely be reused.

Do not overwrite an existing embedding version by default.

# 14. Storage transport

The preferred worker access mechanism is S3 presigned URLs.

Advantages:

- worker receives no permanent AWS identity
- URL is scoped to an operation/object
- URL expires
- controller's IAM policy remains authoritative

Download:

    controller
        ->
    generate presigned GET
        ->
    worker curl/download
        ->
    checksum
        ->
    extract

Upload:

    worker produces artifact
        ->
    controller provides presigned PUT
        ->
    worker uploads
        ->
    controller HEAD/checks object
        ->
    mark durable

Never print the full presigned URL in ordinary logs.

# 15. RunPod network volumes

Network volumes are optional optimization.

They may later cache:

- extracted embeddings
- Python/package caches
- other expensive-to-transfer immutable artifacts

They are NOT the canonical store.

v1 must function without a RunPod network volume.

This keeps the system portable and avoids coupling worker availability to a particular RunPod datacenter.

# 16. Jobs

A job is an explicit request to execute one committed version of wavCSE.

Minimum job identity:

    job ID
    name
    Git repository
    commit SHA
    command argv
    required artifacts
    metadata

Research metadata may include:

    study ID
    seed
    task
    experiment name

The infrastructure layer should pass metadata through but should not interpret research semantics unnecessarily.

# 17. Job submission

Example:

    infra job submit jobs/dg-0004-seed42.yaml

Conceptual behavior:

    validate spec
        ->
    resolve worker
        ->
    ensure READY
        ->
    ensure artifacts
        ->
    ensure repository
        ->
    checkout SHA
        ->
    verify clean state
        ->
    execute command
        ->
    stream/store logs
        ->
    capture exit status

The first version may require the caller to specify a worker explicitly.

Automatic scheduling is not required for initial v1.

# 18. Experiment scheduling

Eventually the controller may distribute independent runs:

    seed 42 -> worker A
    seed 43 -> worker B
    seed 44 -> worker C

Do not implement a complex scheduler initially.

A later simple scheduler may select READY workers based on:

- availability
- required VRAM
- GPU constraints
- hourly-price limit

Distributed multi-GPU training across different machines is out of scope.

# 19. MLflow

wavCSE already owns MLflow integration.

wavcse-infra must not replace it.

Infrastructure should ensure useful runtime metadata can be supplied to wavCSE, such as:

    INFRA_PROVIDER=runpod
    INFRA_WORKER_ID=...
    INFRA_GPU=...
    INFRA_GIT_COMMIT=...
    INFRA_JOB_ID=...

wavCSE may log these into MLflow.

# 20. Operational state

Store lightweight state locally on the controller.

Suggested path:

    ~/.local/state/wavcse-infra/

Possible layout:

    workers.json
    jobs/
      <job-id>.json

Use atomic replacement when writing state.

A corrupted/missing local state directory must not prevent reconciliation with RunPod.

# 21. Configuration

Example user configuration:

    ~/.config/wavcse-infra/config.toml

Example concepts:

    [runpod]
    cloud_type = "SECURE"
    default_gpu_types = [...]
    max_hourly_price = ...
    image = "..."
    container_disk_gb = ...

    [storage]
    bucket = "..."
    prefix = "wavcse"

    [paths]
    wavcse = "/home/.../projects/wavCSE"

    [ssh]
    private_key = "/home/.../.ssh/..."

Do not put secrets in this file.

Secrets come from environment or external credential mechanisms. The RunPod key may be
referenced by a non-secret `runpod.api_key_parameter` and resolved at runtime from an AWS
SSM Parameter Store `SecureString`; a non-empty `RUNPOD_API_KEY` environment variable
takes precedence.

# 22. IAM

The controller uses an EC2 IAM instance profile.

It should receive only required S3 permissions for the wavCSE bucket/prefix.

Expected classes of access:

- list required prefix
- get embedding/artifact objects
- put explicitly supported artifacts
- inspect object metadata

Avoid account-wide S3 permissions.

The RunPod API token is unrelated to AWS IAM and must be stored separately.

When the RunPod token is stored in SSM, the controller instance profile requires only
`ssm:GetParameter` on that parameter. A customer-managed KMS key may additionally
require `kms:Decrypt` on the key.

# 23. Controller reconstruction

A fresh controller should be reconstructable.

High-level recovery:

    create Ubuntu EC2
        ->
    attach controller IAM role
        ->
    supply SSH access
        ->
    cloud-init/bootstrap
        ->
    clone wavcse-infra
        ->
    bootstrap controller
        ->
    authenticate external services
        ->
    clone wavCSE
        ->
    infra doctor

Do not rely on an undocumented manually configured controller.

# 24. Doctor

`infra doctor` is a major operational interface.

It should check, without changing infrastructure:

Controller:

    Git
    Python
    uv
    tmux
    OMP if expected
    Codex CLI
    AGF
    AWS identity availability
    RunPod credential resolution and source (environment or SSM)
    wavCSE path

Connectivity:

    GitHub
    RunPod API
    S3 bucket/prefix
    MLflow endpoint where feasible

Configuration:

    parsed config
    required directories
    SSH key availability

It must redact secrets.

Exit non-zero if required checks fail.

# 25. Cost safety

Worker creation must show the requested resource before or immediately after creation.

Where RunPod returns pricing, record it.

The configuration should support a maximum acceptable hourly price.

If the provider cannot satisfy a requested constraint, fail rather than silently choosing an arbitrary more expensive worker.

Destructive cleanup should be explicit.

# 26. Security boundaries

Trust:

Controller:

    trusted

GitHub:

    trusted code source

S3:

    trusted durable artifact store

MLflow/DagsHub:

    trusted experiment metadata service

GPU worker:

    temporary execution environment;
    do not assume secrets stored there remain private forever

Therefore:

- minimize credentials delivered to workers
- use expiring access
- destroy workers after use
- avoid copying controller private keys to workers
- use a dedicated worker-access SSH key if appropriate
- never send controller IAM credentials manually

# 27. Reliability

Expected failures include:

- no requested GPU capacity
- RunPod API timeout
- worker creation succeeds but client loses response
- SSH unavailable temporarily
- bootstrap failure
- Git clone failure
- S3 transfer interruption
- GPU OOM
- process crash
- worker disappearance
- stale local state

The implementation must make these states diagnosable.

Resource-creation retry logic must be especially conservative to avoid accidentally creating multiple paid workers.

# 28. Observability

Every operation should have:

    timestamp
    operation
    worker ID where applicable
    job ID where applicable
    concise status

Logs must be useful from tmux and non-interactive execution.

Machine-readable JSON logging is optional, not required for v1.

# 29. Testing strategy

Unit tests:

- provider API behavior
- config
- validation
- state
- redaction
- command building
- lifecycle decisions

Mocked integration tests:

- RunPod create -> ready -> destroy lifecycle
- S3 presign flow
- SSH adapter behavior

Live integration tests:

- opt-in only
- explicit cost warning
- never normal CI

# 30. Initial delivery phases

## Phase 0 — foundation

Deliver:

- repository structure
- pyproject
- CLI entrypoint
- lint/test tooling
- GitHub Actions
- configuration loader
- documentation

## Phase 1 — controller

Deliver:

- controller bootstrap
- reproducible controller-only agent/tool installation
- thin cloud-init
- `infra doctor`
- controller recovery documentation

## Phase 2 — RunPod read path

Deliver:

- RunPod client
- worker list
- worker show
- normalized models
- tests with mocked responses

No paid resource creation is necessary to complete this phase.

## Phase 3 — RunPod lifecycle

Deliver:

- worker create
- start
- stop
- destroy
- cost guard
- local state
- readiness polling

Live validation must be opt-in.

## Phase 4 — SSH/bootstrap

Deliver:

- SSH endpoint resolution
- readiness check
- remote command execution
- worker bootstrap
- GPU health check

## Phase 5 — storage

Deliver:

- S3 manifest models
- presigned GET/PUT
- artifact verification
- worker materialization
- embedding archive conventions

## Phase 6 — execution

Deliver:

- job schema
- exact-commit checkout
- job submit
- status
- logs
- exit status
- runtime metadata

At this point the first real wavCSE workload should run.

## Phase 7 — hardening

Only after real usage:

- improved recovery
- cache/network-volume support
- simple multi-worker dispatch
- optional provider expansion

# 31. v1 acceptance scenario

Given:

- configured controller
- RunPod API token
- valid controller IAM role
- private S3 bucket
- accessible wavCSE repository
- an exact wavCSE commit
- a valid job spec

The following workflow must succeed:

    infra doctor

    infra worker create \
      --gpu <type> \
      --max-price <value>

    infra worker show <id>

    infra worker bootstrap <id>

    infra job submit \
      --worker <id> \
      job.yaml

    infra job logs <job-id>

    infra job status <job-id>

    infra worker destroy <id>

The experiment must execute the requested commit.

Durable inputs must remain available after worker destruction.

MLflow results produced by wavCSE must remain available after worker destruction.

Explicitly persisted outputs must remain available after worker destruction.

No long-lived AWS credential may be present on the worker.

# 32. Future possibilities

These are intentionally deferred:

- automatic queue scheduler
- worker autoscaling
- Vast.ai provider
- local workstation provider
- spot/preemptible retry policies
- RunPod volume caching
- artifact dependency graph
- automatic cost optimization
- experiment DAGs
- controller API service
- UI
- Terraform/OpenTofu
- Ansible

Add them only when real usage demonstrates the need.
