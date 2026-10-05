# Sandboxing

Implementation: `sandbox/docker_backend.py`. Decision record: ADR-0001.

## What every sandbox gets

| Property | Mechanism | Verified by |
|---|---|---|
| No host filesystem access | no bind mounts or volumes; files move via `put_archive`/`get_archive` | `test_container_is_hardened_as_documented` (`Mounts` empty) |
| Non-root | `user=<uid>:<gid>` from `environment.user` (default `1000:1000`) | `test_commands_run_as_unprivileged_user...` |
| No privileges | `cap_drop=[ALL]`, `no-new-privileges`, `privileged=False` | hardening test |
| CPU | `nano_cpus = resources.cpus × 10⁹` | hardening test |
| Memory | `mem_limit = memswap_limit = resources.memory_mb` (no swap) | `test_memory_limit_kills_without_reporting_timeout` |
| Processes | `pids_limit = resources.pids_limit` | `test_pid_limit_contains_fork_bombs` |
| Network | `network_mode=none` unless `network.enabled: true` | `test_network_is_disabled` |
| Zombie reaping | `init=True` | hardening test |
| Command time limit | GNU `timeout --kill-after=5s` + host watchdog | `test_command_timeout_is_enforced` |
| Bounded output | at most 1 MiB per stream kept, truncation recorded | `test_output_is_bounded` |
| Controlled environment | only task `environment.variables`, `HOME=/tmp`, and per-command env | user/env test |
| Findability | labels `io.agentenvforge.{managed,run-id,task-id,role}` | orphan test |

**Not enforced in v0.1:** disk usage (overlay quotas are not portable across Docker
storage drivers and Docker Desktop), GPU allocation, I/O bandwidth. A disk-filling agent
is bounded only by the Docker host's free space.

## Image requirements

Images must contain `sleep` (keeps the container alive) and GNU coreutils `timeout`. A
probe command runs when each sandbox starts and fails it with a clear message otherwise.
Debian/Ubuntu-based images, including the official `python` images, qualify; BusyBox
`timeout` has different flags. Images run as root by default. The run user is chosen by
the task, so images need no user account.

## Image caching

For Dockerfile tasks the image is tagged `agentenvforge/<task>:<hash>`. The hash covers
the build context's content and the Dockerfile path, so an unchanged context reuses the
image (9 s build → 0.03 s on the example). Prebuilt images (`environment.image`) are used
when present locally and pulled otherwise. Pin them by digest; validation warns if they aren't.

## Adding a backend

Implement `SandboxBackend` and `Sandbox` from `sandbox/base.py` (six methods in total),
add a branch in `cli/wiring.create_backend`, and make the Docker integration tests
pass against it. A backend must enforce every `ResourceSpec` field it accepts, or refuse
the task; claiming a limit that is not enforced is a bug.
