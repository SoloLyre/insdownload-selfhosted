# insdownload self-hosted

macOS-first local web UI for Instagram, Douyin, and Xiaohongshu downloads.

## How to use

1. Clone the repo.
2. Run:

```bash
./scripts/bootstrap_mac.sh
cp config.example.toml config.toml
./scripts/run_local.sh
```

3. Open [http://127.0.0.1:8123](http://127.0.0.1:8123).
4. In local Chrome, sign in to the platform you want to download from.
5. In the web UI, choose:
   - `Platform`
   - `Profile` or `Single Post`
   - paste a URL or share text
6. Start the task and wait for the local download to finish.

## Important notes

- Sign in first. For higher-quality media and more complete results, use a logged-in local browser session.
- `Profile` mode downloads the whole account currently exposed to that logged-in browser session, including images and videos.
- `Single Post` mode downloads one post only.
- Files stay on the local machine running the app.
- This project is macOS-first and depends on local browser state.

## What it is

- self-hosted only
- runs on the user's own Mac
- single-user, local-only
- uses the user's own browser login state
- saves files to the user's own disk
- serial task execution
- supports profile and single-post targets

The first open-source release is intentionally narrow. No public SaaS, no multi-user mode, no cloud storage.

## Current MVP

The local web app wraps these existing download engines:

- `download_instagram.py`
- `download_douyin_profile.py`
- `download_xiaohongshu.py`

The app adds:

- local task queue
- local SQLite task history
- local log files
- local result pages
- simple settings management
- explicit `Profile / Single Post` task mode

## macOS-first constraint

This repo currently depends on local macOS browser state:

- Douyin uses a local Chromium profile and CDP
- Xiaohongshu reads Chrome cookies and macOS Keychain
- Instagram can use local browser cookies or an Instaloader session

Because of that, `v0` targets local macOS usage first.

## Requirements

- macOS
- Python 3.11+
- Google Chrome installed
- the relevant platform already logged in locally when high-quality or complete results matter

`bootstrap_mac.sh` creates `.venv-app` and installs the local web app plus downloader runtime packages.

## Configuration

Start from [`config.example.toml`](./config.example.toml).

Important keys:

- `app.download_root`: local output root
- `app.data_dir`: SQLite location
- `app.log_dir`: task log directory
- `browser.default_browser`: default browser for Douyin/Instagram
- `browser.default_profile`: default Chrome profile name

## Repository layout

```text
app/
  server.py
  config.py
  db.py
  jobs.py
  models.py
  platforms/
  static/
  templates/
scripts/
download_instagram.py
download_douyin_profile.py
download_xiaohongshu.py
output_layout.py
```

## Opening the repo safely

Before the first public push, review [`docs/OPEN_SOURCE_PREP.md`](./docs/OPEN_SOURCE_PREP.md).

Do not commit:

- downloaded media
- browser cookies or exported profiles
- local manifests from personal runs
- local logs
- local virtual environments
- scratch directories

## Compliance

This project is for authorized, lawful use only. Users are responsible for:

- platform terms compliance
- copyright and content rights
- local legal compliance
- their own browser session and account state

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Security

See [`SECURITY.md`](./SECURITY.md).

## License

This project is released under the [`MIT License`](./LICENSE).
