# Open Source Preparation Checklist

Use this checklist before the first public push.

## Must not be committed

- downloaded media outputs
- browser cookies
- browser profile exports
- local virtual environments
- local logs
- local manifests generated from personal runs
- scratch directories
- personal notes or secrets

## Current local items that should stay ignored

- `platform_downloads/`
- `.venv/`
- `.venv-xhs/`
- `.venv-app/`
- `tmp/`
- `var/`
- `*.log`
- `.gallery-dl-archive.txt`
- any cookie or session export

## Manual review before first public push

1. Run `git status --short`
2. Confirm that no media outputs appear in the index
3. Confirm that no cookie, token, or session files appear in the index
4. Confirm that no personal profile URLs remain in committed docs
5. Confirm that all launch scripts and docs describe the project as self-hosted

## Required public-facing files

- `README.md`
- `LICENSE`
- `.gitignore`
- `config.example.toml`
- local setup instructions
- risk and compliance note

## Recommended next files

- `SECURITY.md`
- `CONTRIBUTING.md`
- issue templates
- pull request template
