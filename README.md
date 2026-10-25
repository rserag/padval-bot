# Padval Bot

Padval Bot adds locked-down `/status` and optional `/torrent` commands to a
Telegram bot that is already used by a monitoring system such as Grafana.
Status returns one compact infrastructure summary. Torrent submission accepts
one magnet link, asks for a configured destination, and submits it to
qBittorrent without exposing the magnet in replies or logs.

It uses Python, PyYAML, and common Linux tools. Grafana can
continue sending alerts with the same Telegram bot token; Padval Bot only
consumes incoming commands.

## Example report

```text
🟢 HOMELAB STATUS
All monitored systems healthy
30 Aug 2026 · 17:30 UTC

NETWORK
✅ gateway · ✅ router SSH · ✅ DNS

APP-VM  198.51.100.10
⏱ 12d 4h · Load 0.18
💾 RAM 31% · root 27%
✅ Core services  2/2
✅ Apps  2/2 active
✅ Containers  3/3 healthy

STORAGE-VM  203.0.113.20
⏱ 12d 3h · Load 0.08
💾 RAM 42% · root 22% · data 61%
✅ Core services  4/4
✅ RAID md0 · [UU] · spare yes · mismatch 0

HTTP
✅ Endpoints  1/1 responding
```

The addresses above are reserved documentation ranges, not a real deployment.

## Security properties

- One private Telegram chat is authorized; every other chat is ignored.
- First-time pairing requires a random secret stored outside Git.
- Bot tokens, chat IDs, SSH keys, and live configuration are excluded from the
  repository and should be readable only by the service account.
- SSH uses a dedicated key, batch mode, strict host-key checking, and a
  configured `known_hosts` file.
- Remote probes are read-only and return only health/resource summaries.
- The example systemd service is unprivileged and hardened.
- Reports are capped below Telegram's single-message limit.
- Torrent destinations come from reviewed YAML. Custom paths are contained
  below explicit application roots and verified on the storage host.

Read [SECURITY.md](SECURITY.md) and [the deployment guide](docs/deployment.md)
before connecting it to real infrastructure.

## Requirements

- Python 3.11 or newer
- Linux with `systemd`
- `curl`, `ping`, and OpenSSH client
- Docker CLI and Compose plugin only when Docker collection is enabled
- Python 3 on every SSH-probed Linux host

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install .
cp config.example.json config.json
.venv/bin/padval-bot --config ./config.json --once
```

`config.json` is intentionally ignored by Git. Edit it with your actual hosts,
services, and private file paths. The example uses addresses from RFC 5737.

For a persistent installation, follow [docs/deployment.md](docs/deployment.md).

## Configuration

The JSON file has seven main areas:

- `telegram`: token, authorized-chat, pairing-secret, and runtime-state files.
- `network_checks`: ICMP, TCP, or DNS checks.
- `routeros`: optional read-only MikroTik resource and WireGuard summary over
  SSH.
- `hosts`: local or SSH Linux probes with filesystems, services, Docker, RAID,
  and selected systemd memory counters.
- `http_checks`: endpoint-specific healthy HTTP status ranges and timeouts.
- `torrent`: optional private qBittorrent endpoint and SSH path-check settings.
  Public destination choices live in `config/torrent-locations.yaml`.
- `jellyfin`: optional private Jellyfin endpoint and API-key file. The key is
  read from a protected file and is never placed in Git or a URL.

Start from [config.example.json](config.example.json). No live configuration is
loaded from environment variables or command-line secrets, which keeps tokens
out of process listings and service definitions.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

CI runs those checks on all supported Python versions. Contributions are
welcome, but do not include real infrastructure details or credentials in
issues, fixtures, logs, screenshots, or pull requests.

## Torrent workflow

When enabled in the private runtime configuration, send `/torrent <magnet>` in
the paired private chat. The bot presents the destinations from
`config/torrent-locations.yaml`, plus Custom and Cancel controls. A custom path
must already exist, resolve below one of the configured roots, and be writable
by the qBittorrent service account. Pending magnets remain only in process
memory and expire automatically. Common copied magnets with literal spaces in
their display name are normalized automatically; matching single or double
quotes around the complete magnet are also accepted.

A save location with `kind: tv` opens a show and season picker instead of
submitting immediately. Choose an existing show folder or **New show**, then
choose an existing season, enter a season number (0 for specials), or choose
**All seasons / Complete show** for a torrent containing multiple seasons. Lists
are paginated, and Back and Cancel controls are available. New names are
entered by replying to the bot's prompt; `/cancel` also works from a prompt.
The two shows most recently used for successful TV submissions appear first,
followed by the other shows in their usual order. These shortcuts survive bot
restarts; removed show folders are omitted. Browsing or cancelling does not
change the recent list. Only the two folder names are saved in the private
runtime file `tv-recent-shows.json` (mode `0600`), without magnets or tracker URLs.
After a successful episode submission, **Add another episode** asks for a new magnet
and sends it directly to the same show and season, with no repeat selection or
confirmation. The prompt displays the destination, validates the new magnet,
and rechecks folder safety before each submission. Each successful episode
offers the button again. Reply to the prompt within ten minutes, or use
`/cancel`; starting a new `/torrent` request also cancels that prompt. Only the
latest successful TV submission's shortcut is active, and shortcuts expire
when the bot restarts. No previous magnet is retained for this feature.
The bot shows the full destination and waits for **Download** before creating
any missing folders or submitting the magnet. An existing season folder is
reused when its number matches, including older names such as
`Season 2 (BluRay)`; ambiguous matches require an explicit folder selection.

Complete-show downloads target the selected show folder directly, without an
artificial season directory. Confirmation shows **All seasons / Complete show**
and the show destination. Only the show folder is created if missing. These
downloads still support progress, completion notifications, recent-show
shortcuts, and Jellyfin refreshes, but do not offer **Add another episode**
because no individual season was selected.

TV submissions use qBittorrent's `NoSubfolder` content layout to remove only
the torrent's outer release folder. Folders inside that root, including all
season folders, retain their hierarchy and names. The bot does not create a
season structure for torrents that do not contain one, or select individual
files from a pack. Existing release folders are listed as they exist; the bot
does not reorganize or rename existing media.

Only one location may use `kind: tv`. Its path must remain under the configured
allowed roots. The storage host lists real directories only and opens show and
season directories without following symlinks. Folder creation uses the same
unprivileged SSH account as the destination checks; that account must be the
qBittorrent service account and have permission to create directories beneath
the TV root. Movies and Custom retain their existing behavior.

When tracking is enabled in the same YAML file, new bot-added torrents are
watched automatically. `/downloads` shows progress, speed, ETA, state, and
destination. Each download has a notification toggle, and the bot sends one
durable completion notification even when completion happens across a bot
restart. Tracking state contains no magnet or tracker URL.

When `tracking.media_refresh` is enabled and private Jellyfin access is
configured, each newly completed download queues a full library scan.
Completions within the configured debounce window are coalesced into one
request. Pending refreshes and failed attempts survive restarts; failures retry
with bounded exponential backoff. Jellyfin credentials remain outside the
repository.

The same authorized private chat can send `/scan` to request an immediate
Jellyfin library scan. One Telegram message follows the scheduled task with a
progress bar when Jellyfin reports a percentage, elapsed time, and a manual
Refresh status button. It is edited automatically every ten seconds and turns
into a completion or failure notification when the scan finishes. `/scanstatus`
checks the current task and starts following it if another Jellyfin client
started it. Active scan-message state is restart-safe and contains no API key
or media details; the key remains confined to the protected credential file.

## License

No license has been selected yet. Public visibility does not grant permission
to copy, modify, or redistribute the code. Add an explicit license before
advertising the project as open source.
