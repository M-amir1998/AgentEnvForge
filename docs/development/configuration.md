# Configuration

Settings are resolved once, at startup, in this order (later wins):

```text
built-in defaults  <  $AGENTENV_HOME/config.toml  <  AGENTENV_* environment variables  <  CLI flags
```

| Setting | Default | Environment variable | CLI flag |
|---|---|---|---|
| `home` | `~/.agentenvforge` | `AGENTENV_HOME` | `--home` |
| `log_level` | `WARNING` | `AGENTENV_LOG_LEVEL` | `--log-level` |
| `log_format` | `text` (`json` available) | `AGENTENV_LOG_FORMAT` | `--log-format` |
| `docker_host` | Docker's own resolution (`DOCKER_HOST`, context) | `AGENTENV_DOCKER_HOST` | - |
| `sandbox_start_timeout_seconds` | `120` | `AGENTENV_SANDBOX_START_TIMEOUT_SECONDS` | - |
| `cleanup_timeout_seconds` | `60` | `AGENTENV_CLEANUP_TIMEOUT_SECONDS` | - |
| `keep_workspace` | `false` | `AGENTENV_KEEP_WORKSPACE` | `agentenv run --keep-workspace` |

`home` decides where the config file is, so it cannot be set inside the config file.
Unknown keys and invalid values are errors (exit code 2), never silently ignored.

Example `config.toml`:

```toml
log_level = "INFO"
cleanup_timeout_seconds = 30
```

These are *infrastructure* settings. Anything that affects a task's semantics (limits,
timeouts, network) lives in the task file, so it is versioned with the task and recorded
in the manifest.
