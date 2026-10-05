# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through
[GitHub security advisories](https://github.com/M-amir1998/AgentEnvForge/security/advisories/new),
not as public issues. Include affected versions, a reproduction, and the impact you
observed. We aim to acknowledge reports within 7 days.

In scope: sandbox escapes or limit bypasses attributable to AgentEnvForge's configuration,
host file or secret exposure, archive/path traversal, injection through task or agent
specifications, grader-integrity bypasses beyond the documented open issues, and secret
leakage into stored records.

## Supported versions

AgentEnvForge is pre-1.0; only the latest release receives fixes.

## Isolation guarantees, honestly

AgentEnvForge treats task code and agent actions as untrusted and runs them in hardened
Docker containers: no host mounts, non-root, all capabilities dropped,
`no-new-privileges`, CPU/memory/PID limits, no network by default. **Containers share the
host kernel and are not a VM-grade boundary.** Disk usage is not limited in v0.1.
Do not run untrusted tasks on machines that hold sensitive data or credentials.

The full threat model, including known open issues in grader integrity, is in
[docs/architecture/security.md](docs/architecture/security.md).

## Secrets

Never put credentials in task files, Dockerfiles, images, fixtures or example configs.
Pass them to agents only via the agent spec's `secrets:` list (read from your environment
at run time). Their values are redacted from events, logs and artifacts.
