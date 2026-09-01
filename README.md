# Padval Bot

Padval Bot adds a locked-down `/status` command to a Telegram bot that is
already used by a monitoring system such as Grafana. One command returns one
compact message covering routers, Linux hosts, systemd services, Docker
Compose projects, containers, HTTP endpoints, RAID health, storage usage, and
selected process memory.

It uses only the Python standard library and common Linux tools. Grafana can
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

The JSON file has five main areas:

- `telegram`: token, authorized-chat, pairing-secret, and runtime-state files.
- `network_checks`: ICMP, TCP, or DNS checks.
- `routeros`: optional read-only MikroTik resource and WireGuard summary over
  SSH.
- `hosts`: local or SSH Linux probes with filesystems, services, Docker, RAID,
  and selected systemd memory counters.
- `http_checks`: endpoint-specific healthy HTTP status ranges and timeouts.

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

## License

No license has been selected yet. Public visibility does not grant permission
to copy, modify, or redistribute the code. Add an explicit license before
advertising the project as open source.
