# Contributing

## Scope

- macOS-first, self-hosted only
- keep changes local-first and single-user by default
- do not introduce public SaaS assumptions into the core flow

## Setup

```bash
./scripts/bootstrap_mac.sh
cp config.example.toml config.toml
./scripts/run_local.sh
```

## Before opening a PR

1. Keep generated files out of the diff.
2. Confirm `.gitignore` still covers local media, logs, cookies, and runtime state.
3. Run a local smoke test for the platform you changed.
4. Update docs when behavior or setup changes.

## Pull request notes

- keep PRs narrow
- explain platform-specific tradeoffs
- include any manual verification you ran

## Do not commit

- downloaded media
- browser cookies
- exported browser profiles
- local logs
- local SQLite files
- local config overrides
