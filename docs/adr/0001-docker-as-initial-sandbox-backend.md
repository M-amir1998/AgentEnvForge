# ADR-0001: Docker as the initial sandbox backend, with archive-based file transfer

- Status: accepted
- Date: 2026-10-04

## Context

Task code and agent actions must be treated as untrusted: a repository can contain a
fork bomb, an agent can try to exfiltrate data or exhaust memory. Every run needs a
disposable environment with enforced CPU, memory, process and network limits, and it must
work on developer laptops (Linux, macOS, Windows) as well as CI.

## Options considered

1. **Plain subprocesses on the host** with `ulimit`/job objects: trivial, but no filesystem
   or network isolation and not portable. Rejected.
2. **Docker** (Engine or Desktop): ubiquitous, cgroup-enforced limits, `network=none`,
   capability dropping, mature Python SDK. Shares the host kernel.
3. **Podman**: daemonless and rootless by default, API-compatible-ish; smaller install
   base on macOS/Windows.
4. **gVisor / Firecracker microVMs**: much stronger isolation, but Linux-only, operationally
   heavier, and a poor first-run experience for contributors.
5. **Kubernetes Jobs**: right for scale, wrong for a local-first v0.1.

## Decision

Use Docker through the official Docker SDK for Python, behind the `SandboxBackend` /
`Sandbox` protocols (`agentenvforge.sandbox.base`), so other backends can be added
without touching the orchestrator.

Files move **only as tar archives through the Docker API** (`put_archive` / `get_archive`);
no host directory is ever bind-mounted. Containers run as a numeric non-root user, with
all capabilities dropped, `no-new-privileges`, `--init`, CPU/memory (swap disabled)/PID
limits, and `network_mode=none` unless a task opts in.

## Consequences

- Works identically with local and remote daemons, and on Docker Desktop for Windows/macOS
  where bind mounts are slow and have permission quirks.
- No host path can leak into a sandbox, and an agent cannot write to the host.
- Copying is O(workspace size) per stage. That is fine for typical repositories but would
  need revisiting for multi-GB workspaces (e.g. volumes).
- Archives coming *out* of a sandbox are untrusted, so extraction uses the `data` filter,
  skips unsafe members and enforces a size limit (`agentenvforge.workspace.archive`).
- Images must provide `sleep` and GNU coreutils `timeout`; this is verified by a probe
  when each sandbox starts.

## Limitations

Containers share the host kernel; a kernel exploit escapes them. Docker is a
defence-in-depth boundary, not a VM boundary. See `docs/architecture/security.md`.
Disk usage is not limited in v0.1 (overlay storage quotas are not portable).
