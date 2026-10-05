# Security model

**Assume task repositories, agent actions and solution code are untrusted.** Task *files*
(YAML) and the grader configuration are semi-trusted: they are data, but a benchmark
author chooses the image and the commands that run inside it.

## Threats and v0.1 mitigations

| Threat | Mitigation | Residual risk |
|---|---|---|
| Malicious repository / agent code reading host files | no host mounts; archive copy only; host paths in tasks must stay inside the task directory | kernel exploits (shared kernel) |
| Container escape via privileges | non-root uid, all capabilities dropped, `no-new-privileges`, never privileged, no Docker socket | kernel/runtime vulnerabilities |
| Fork bomb | `pids_limit` (default 512) | none known within the limit |
| Memory exhaustion | memory limit with swap disabled → OOM kill inside the container | none known |
| CPU exhaustion / infinite loop | CPU quota; per-command and total time limits; watchdog kills the container | an abandoned in-process adapter thread on the host (ADR-0003) |
| Disk exhaustion | **not limited in v0.1**; archive copies out are capped at 2 GiB | can fill the Docker host's disk |
| Network exfiltration | `network_mode=none` by default; enabling it is explicit and warned about | when enabled, unrestricted egress |
| Secret extraction | no host environment is passed; secrets only via explicit `secrets:` to the agent process; redacted from all records | the agent itself can use or send the secret when network is enabled |
| Malicious archive from a sandbox (path traversal, symlinks to `/etc`) | `tarfile` `data` filter; unsafe members skipped and reported; size limit | none known |
| Malicious git URL (`ext::` command transport, hooks) | `protocol.ext.allow=never`, `core.hooksPath=/dev/null`, no credential prompts | git client vulnerabilities |
| Malicious YAML | `SafeLoader` (no object construction), size limit, duplicate keys rejected | none known |
| Huge outputs exhausting host memory | 1 MiB per stream retained; 20 MiB JUnit limit | none known |

## Grader integrity (reward hacking)

| Attack | Status |
|---|---|
| Agent edits the visible tests | **mitigated**: `protected_paths` restored before grading (tested e2e) |
| Agent reads hidden tests | **mitigated**: hidden assets exist only in the grading sandbox; validation rejects assets inside the source tree |
| Agent leaves a background process / modifies the environment outside the workspace | **mitigated**: grading happens in a fresh container |
| Agent writes a fake success file or fake JUnit report into the workspace | **mitigated**: reports are written to `/tmp/agentenv-grader/` inside the grading sandbox, outside the workspace |
| Agent kills the validation process | **mitigated**: the agent has no access to the grading sandbox |
| Solution code tampers with the test runner at import time (e.g. patching pytest) | **open**: the code under test runs inside the grading process |
| Agent edits configuration that grading depends on (e.g. `pyproject.toml`) | **partially mitigated**: list such files in `protected_paths` |

Planned hardening: running hidden tests as a separate user with read-only assets, gVisor
(`runsc`) runtime support, disk quotas where the storage driver supports them, and
egress allow-lists instead of all-or-nothing networking.

## What Docker does not guarantee

Containers share the host kernel. A kernel or container-runtime vulnerability can lead to
host compromise. For hostile code at scale, run AgentEnvForge inside a dedicated VM or
use a stronger runtime once supported. Do not run it on machines holding sensitive
credentials if tasks come from untrusted sources.

To report a vulnerability, see [SECURITY.md](../../SECURITY.md).
