# Plan — one declared application registry, asserted by `make check`

Status: proposal, not implemented. Every claim below cites current code. Recorded
2026-10-11 while answering "there are now many apps with versions and configs — is this
the time to use Ansible?"

## 1. Verdict

- **Do not adopt Ansible** (section 3). One persistent host plus disposable workers is
  the inverse of the topology Ansible is for, the parts worth having are already here and
  already tested, and it would add a second mutation path beside hard invariant 9.
- **Do the cheap half — and only when the next app actually lands**: declare the
  application set once in `apps/registry` and add a read-only `check` that fails when the
  scripts, the docs, or `tool-links.sh` disagree with it (section 5, Phase 1). No runtime
  behavior changes, no new dependency, one new file and one new `make` target.

The growth pain is in *applications*, not hosts. Today one new pinned application can
touch six files in three languages plus two documentation tables, and nothing in
`make check` compares them.

## 2. The duplication, measured

| Artifact | Count | Sites that restate it |
| --- | --- | --- |
| apt package sets | 2 disjoint lists (only `tmux` is in both) | `controller/bootstrap.sh:110-118`, `worker/bootstrap.sh:28-39` |
| Version pins | 8 constants, 7 tools | `controller/bootstrap.sh:4,11,21` (uv `0.12.19`, lazygit `0.66.0`, Colab CLI `0.7.4`), `worker/bootstrap.sh:5` (uv `0.10.9`), `controller/install-agents.sh:4,7,8,11` (OMP `v18.8.0`, Codex `0.157.1`, AGF `v0.15.1`, OpenCode `1.18.35`) |
| Release digests | 9 SHA-256 constants, 4 archives | `controller/bootstrap.sh:12-13`, `controller/install-agents.sh:5,6,9,10,12,13,14` |
| Mirrored config files | 4 entries, 3 apps | `apps/manifest` (tmux, opencode, lazygit) — the only file that is data |
| System-wide tool links | 8 command names | `controller/tool-links.sh:22` |
| Doctor expectations | 2 environment variables | `docs/OPERATIONS.md:71-72`, `src/wavcse_infra/doctor.py:161,330` |
| Docs restating pins and the tool set | 2 pin tables + 1 checklist | `docs/OPERATIONS.md:276-282`, `docs/OPERATIONS.md:383-386`, `docs/SPEC.md:700-711` |
| Version-override contract | 18 environment variables | `docs/OPERATIONS.md:77-96` |

Two of the eight "duplications" are *not* duplication and must stay distinct: the
controller and worker package sets are different sets for different accounts, and the
controller and worker `uv` pins are deliberately different versions. The registry records
both under an explicit target rather than merging them.

Nothing enforces agreement. `make check` (`Makefile:64-70`) runs `agents-check` but no
application-consistency check, so a version bumped in `install-agents.sh` and not in
`docs/OPERATIONS.md` is invisible; so is a tool added to `MANAGED_TOOLS` that no installer
creates, or an entry added to `apps/manifest` with no directory under `apps/`.

## 3. Why not Ansible

### 3.1 The topology is the opposite of Ansible's

Ansible converges a fleet of long-lived hosts toward desired state. This repository has
**one** persistent host (the controller, `docs/ARCHITECTURE.md:12-16`) and N disposable
workers whose drift is corrected by destroying and rebuilding them, not by converging
them: workers are provider-owned and recreated (`worker/bootstrap.sh`, streamed by
`src/wavcse_infra/workers/bootstrap.py:79-80`), and local state is explicitly "supplemental,
never the sole source of truth" (`AGENTS.md`, invariant 8). Managing workers as an
inventory inverts invariants 1 and 8, and a worker that has drifted is not a state to
converge — it is a container to replace.

The worker side is also where Ansible costs most: `ansible-pull`/push means installing
Ansible and cloning this repository *inside every GPU pod*, replacing a contract that is
currently a reviewed constant with a version-checked completion marker
(`src/wavcse_infra/workers/bootstrap.py:172,188-192`). That is strictly weaker for a
disposable container, and ADR-031 already rejected putting a payload into that streamed
script for the same reason (`docs/DECISIONS.md:1590-1592`).

### 3.2 What Ansible would genuinely offer is already implemented here

| Ansible feature | Equivalent in this repository |
| --- | --- |
| `--check`/`--diff` dry run | `controller/app-config.sh --check` (`:320`), `controller/tool-links.sh --check`, `make app-config-check`, `make tool-links-check`, `make doctor` |
| Idempotent package tasks | both bootstraps already converge on a version marker (`worker/bootstrap.sh:6-7`) and are documented as safe to rerun |
| `get_url checksum:` release pinning | per-architecture SHA-256 verification before extraction (`controller/bootstrap.sh:214-227`, `controller/install-agents.sh:275,359,437`), including "a pin without a digest is not a pin" |
| Templating configuration | `apps/manifest` + digest-verified atomic applier (`src/wavcse_infra/app_config.py:217,366,416`) |
| `ansible-vault` | nothing to vault: worker secrets travel as short-lived presigned URLs on stdin, never in `argv` (invariant 4/5) |
| Playbook linting | ruff + shellcheck + pytest + cloud-init schema in `make check` |

The residual gain is a uniform `--check --diff` over everything at once, paid for with a
new runtime on the controller and workers, application logic moved out of `pytest` and
`shellcheck` coverage, and a second way to mutate a worker beside `infra` (invariant 9).
That trade is not worth it at this scale.

### 3.3 Revisit triggers — so the decision is re-decidable, not permanent

Reconsider a configuration-management framework if **any** of these becomes true:

- Two or more *persistent, non-disposable* hosts you must converge, especially with
  different distributions or package-manager constraints.
- Package **version** pinning or repository management beyond `apt-get install` (apt
  holds, custom repos, build-from-source packages).
- A second human operator with different access rights, or a machine-readable compliance
  report as a deliverable.
- Long-lived stateful services on the persistent hosts — currently excluded by policy
  (no daemon, no database, no message broker).

Registering the university host (`docs/PLAN-STATIC-SSH-PROVIDER.md`) is **not** a trigger:
it is one host, it is explicitly non-canonical, and it receives the same streamed
bootstrap. Note also that `AGENTS.md` lists Ansible under "do not add without an explicit
request" — this plan does not request it.

## 4. Design

### 4.1 Keep `apps/manifest` as it is; add `apps/registry`

ADR-031 chose a line-oriented registry readable by both Bash and Python and rejected a
TOML/JSON registry (`docs/DECISIONS.md:1553-1602`). Widening the *manifest* field layout
would churn both parsers (`controller/app-config.sh:133`, `src/wavcse_infra/app_config.py:366`) and
`tests/unit/test_app_config.py` for no gain. Application records are therefore additive:
`apps/manifest` stays byte-identical and keeps meaning "mirrored files"; `apps/registry`
carries packages, pins, digests, links, and doctor expectations.

### 4.2 Record shape

Tab-separated, like `apps/manifest`, one record per `<app>` and `<kind>`:

```text
# <app>\t<kind>\t<fields...>
#   package\t<target>\t<apt-package>
#   release\t<target>\t<version>\t<mechanism>\t<install-dir>\t<override-env-prefix>
#   digest\t<arch>\t<sha256>          (attaches to the preceding release record)
#   link\t<command-name>
#   expect\t<environment-variable>
#   manual\t<note>                    (a mechanism the schema cannot express)
```

Example for the review UI:

```text
lazygit	release	controller	0.66.0	github-release	~/.local/bin	WAVCSE_INFRA_LAZYGIT
lazygit	digest	x86_64	5b45541155d20bd32bf2cc5ab5b7e3d91c2eebf0fb1242281350edc27d59d2b7
lazygit	digest	aarch64	9a4fc4656897ac9f7877b835473ce1a75620cc267f554c57fc4ff266407f3257
lazygit	link	lazygit
tmux	package	both	tmux
tmux	manual	configuration only; no pinned release, package from the distribution
```

Validation mirrors what already exists: unknown kind or target, an absolute or escaping
path, a digest that is not 64 lowercase hex (`src/wavcse_infra/app_config.py:29`), a
`digest` with no preceding `release`, or a `release` missing a digest for an architecture
it claims to support are all errors naming the offending line — the same fail-closed
stance as `_reject_unsafe` (`src/wavcse_infra/app_config.py:416`) and `manifest_entries`
(`controller/app-config.sh:133`).

### 4.3 Every field must have a consumer

A registry field that nothing reads is fiction, so the checker asserts each of these and
fails on any disagreement:

| Registry field | Asserted against |
| --- | --- |
| `package target=controller` | the `apt-get install` list in `controller/bootstrap.sh:110-118` |
| `package target=worker` | `required_packages` in `worker/bootstrap.sh:28-39` |
| `release version` | `DEFAULT_*_VERSION` (`controller/bootstrap.sh:4,11,21`, `controller/install-agents.sh:4,7,8,11`) |
| `release override-env-prefix` | the override table in `docs/OPERATIONS.md:77-96` |
| `digest` | `DEFAULT_*_SHA256` constants (`controller/bootstrap.sh:12-13`, `controller/install-agents.sh:5,6,9,10,12,13,14`) |
| `link` | `MANAGED_TOOLS` (`controller/tool-links.sh:22`) |
| `expect` | `WAVCSE_INFRA_EXPECT_*` (`docs/OPERATIONS.md:71-72`, `src/wavcse_infra/doctor.py:161,330`) |
| `version` | the pin tables `docs/OPERATIONS.md:276-282` and `docs/OPERATIONS.md:383-386` |
| an app with a `release` or a `link` | the `infra doctor` checklist at `docs/SPEC.md:700-711` and the first column of both pin tables |

The checker also asserts that every directory under `apps/` is declared by at least one
record, and that every declared app either owns a mirrored file or says why it has none.

### 4.4 The worker bootstrap stays a literal

`worker/bootstrap.sh` is streamed as a reviewed constant and verified by digest and
completion-marker version (`src/wavcse_infra/workers/bootstrap.py:79-80,172,188-192`); it
cannot read this repository. Its package list therefore stays inline and is **asserted**
against the registry, never fetched or generated. This is the deliberate exception that
keeps the reviewed-constant property (ADR-031) intact, and it is why Phase 1 asserts
rather than generates.

## 5. Phases

### Phase 1 — registry and checker, no behavior change

Goal: one declared application set, and a gate that fails when anything disagrees with it.

- `apps/registry` (new) — the records of 4.2, populated from the current values, with no
  change to any installer.
- `scripts/apps/app_registry.py` (new) — `check` subcommand, following the existing
  `scripts/agents/agent_assets.py` precedent, reading the registry through
  `resources.files("wavcse_infra")` (`src/wavcse_infra/app_config.py:428-434`) so it works from the
  checkout and from an installed wheel (`apps/` is force-included by `pyproject.toml`).
- `Makefile` — `apps-check` target added to `check` beside `agents-check`
  (`Makefile:64-70`), plus an `apps-check` `.PHONY` entry.
- `tests/unit/test_app_registry.py` (new) — the checker's own tests: every kind parses,
  each malformed record above is rejected with the line number, and a mutated constant in
  a remembered-literal sample fails the comparison.

Acceptance: `make apps-check` passes on an unchanged tree; deliberately editing one
version constant in `controller/install-agents.sh` makes it exit 1 naming the file, field,
and both values; no script's runtime behavior changes; `make check` stays green.

### Phase 2 — let the controller scripts read the registry (optional, deliberate)

Only for scripts that already run inside the checkout, and only to delete a literal:
`install_os_packages` (`controller/bootstrap.sh:102`), the `DEFAULT_*` pins
(`controller/install-agents.sh:4-19`), and `MANAGED_TOOLS`
(`controller/tool-links.sh:22`). The `WAVCSE_INFRA_*_VERSION` override contract stays: the
registry supplies the default, the environment still wins, and overriding a version
without supplying a digest stays fatal (`controller/bootstrap.sh:214-227`). A registry
that fails to parse is fatal with an actionable message before any package or binary is
touched — the same stance as an ambiguous manifest.

Worker scripts are excluded by 4.4.

Acceptance: `controller/bootstrap.sh --help`-visible behavior unchanged; a fresh
bootstrap on a real controller installs the identical package set and tool versions as
before the change (compare `install_os_packages` output and the version checks);
`WAVCSE_INFRA_LAZYGIT_VERSION` override with and without a digest behaves exactly as today.

### Phase 3 — check the documents

Extend the checker to assert that the pin tables at `docs/OPERATIONS.md:276-282` and
`docs/OPERATIONS.md:383-386` still contain each registry version token, and that the
`infra doctor` checklist at `docs/SPEC.md:700-711` names every app that declares a release
or a link. The anchors are explicit, so a missing row is an error rather than a silent
pass. Prose stays hand-written; only the tokens and the names are checked.

Acceptance: removing a row from either pin table, or a line from the doctor checklist,
fails `make apps-check`; changing a version in the registry alone fails it until both the
scripts and the documents agree.

### Phase 4 — record the decision

- New ADR after ADR-036 (`docs/DECISIONS.md:1867`): declare the application set in one
  registry; assert it in `make check`; reject a configuration-management framework with
  the trigger conditions of 3.3 recorded, so the rejection is re-examinable.
- Update the section that currently describes the manifest as covering files only —
  `docs/OPERATIONS.md:228`, where a pointer to this plan already sits — plus the tool
  inventory in `docs/ARCHITECTURE.md:229-231`.
- `make check` and `make agents-check` must pass; `make check` is the required gate.

## 6. Invariants that stay intact

- **One format, two parsers** (ADR-031): tab- and line-oriented, no TOML/JSON, and the new
  registry is read by the same two languages.
- **Worker bootstrap stays a reviewed constant** streamed and digest/version-verified
  (`src/wavcse_infra/workers/bootstrap.py:79-80,172,188-192`).
- **No new mutation path** (invariant 9): `apps-check` is read-only, like
  `app-config.sh --check` and `tool-links.sh --check`.
- **A pin without a digest is not a pin**: unchanged, and now also a registry validation rule.
- **The registry carries no secrets**: versions, digests, paths, and variable names only —
  never an auth store (`docs/OPERATIONS.md:257-259`).
- **Nothing new is installed anywhere**: no Ansible, no collections, no agent runtime.

## 7. Residual risks

| Risk | Mitigation |
| --- | --- |
| The registry becomes a second, unchecked source of truth | Every field must have a consumer in the 4.3 table; a field with no consumer is a check failure |
| Doc assertions are brittle regexes that silently stop matching | Phase 3 anchors are explicit and fail closed; a missing anchor is an error, never a skip |
| A future app needs a mechanism the schema cannot express (npm, cargo, pipx) | `manual` with a required note, reported by the checker so an unmanaged mechanism is visible rather than silent; a new kind is added only together with its consumer |
| Two registries (`manifest`, `registry`) drift apart about which apps exist | The checker asserts every `apps/<app>/` directory is declared, and every declared app has files or a stated reason |
| Phase 2 makes bootstrap depend on parsing a repository file | Parse failure is fatal with an actionable message before any install, mirroring the ambiguous-manifest stance (`controller/app-config.sh:133`) |
| The check is over-fitted to today's three apps | Schema holds only the kinds with a real consumer; over-fitting is bounded by the 4.3 table being the contract |

## 8. Rejected designs

- **Ansible, Chef, Puppet, Salt** — section 3: fleet-shaped tool for a one-persistent-host
  topology, a second mutation path, logic outside the test gate, and explicitly excluded
  by `AGENTS.md` absent an explicit request.
- **Migrating the registry to TOML or JSON** — already rejected in ADR-031: two parsers and
  more bytes for no gain, given a format Bash can read.
- **Generating `worker/bootstrap.sh` from the registry** — breaks the reviewed-constant
  digest contract for every future edit (4.4).
- **Putting workers in an inventory and converging them with a playbook** — contradicts
  invariants 1 and 8; a drifted worker is replaced, not converged.
- **A Python-only registry that the Bash appliers cannot read** — would make the Bash side
  fetch or duplicate values again, which is the problem being solved.
