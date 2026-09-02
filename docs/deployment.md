# Deployment

This guide keeps every credential and live topology value outside the Git
checkout.

## 1. Create an unprivileged account

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin padval-bot
sudo install -d -o root -g padval-bot -m 0750 /etc/padval-bot/ssh
```

If local Docker inspection is enabled, the service account needs access to the
Docker API. Membership in the conventional `docker` group is effectively root
access. Prefer a narrowly filtered socket proxy or rootless Docker. If you
accept the risk, add `SupplementaryGroups=docker` to the systemd unit locally;
it is deliberately absent from the public example.

## 2. Install the package

```bash
sudo python3 -m venv /opt/padval-bot/venv
sudo /opt/padval-bot/venv/bin/pip install /path/to/padval-bot
```

Copy `config.example.json` to `/etc/padval-bot/config.json`, replace every
documentation value, and restrict it:

```bash
sudo install -o root -g padval-bot -m 0640 config.json /etc/padval-bot/config.json
```

## 3. Store the Telegram token and pairing secret

Create a bot with BotFather or reuse a dedicated monitoring bot. Store the raw
token as a single line in `/etc/padval-bot/telegram-token`, never in JSON, Git,
shell history, or a systemd `Environment=` line.

Generate a one-time pairing secret without printing it into logs:

```bash
sudo install -o root -g padval-bot -m 0640 /path/to/telegram-token /etc/padval-bot/telegram-token
openssl rand -hex 24 | sudo tee /etc/padval-bot/pairing-secret >/dev/null
sudo chown root:padval-bot /etc/padval-bot/pairing-secret
sudo chmod 0640 /etc/padval-bot/pairing-secret
```

Read the pairing secret locally, send `/status SECRET` in a private chat, then
remove the pairing file after `/var/lib/padval-bot/allowed_chat_id` appears.
The numeric chat ID remains mode `0600` in the state directory.

## 4. Store the Jellyfin API key

If automatic media refresh is enabled, create a dedicated Jellyfin API key for
Padval Bot in the Jellyfin administrator dashboard. Store the raw key as one
line in the file referenced by `jellyfin.api_key_file`, owned by root and the
service group with mode `0640`. Do not put the key in JSON, YAML, a URL, Git,
shell history, or a systemd environment variable.

Jellyfin API keys are not narrowly permission-scoped. Treat this file as an
administrator credential and use the private Jellyfin address rather than the
public reverse proxy. Validate access without starting a scan:

```bash
sudo -u padval-bot /opt/padval-bot/venv/bin/padval-bot \
  --config /etc/padval-bot/config.json --check-jellyfin
```

## 5. Configure read-only SSH

Create separate keys for Linux and RouterOS monitoring. Do not reuse a personal
administrative key. On Linux, restrict the remote account to the commands and
files needed by the probe. On RouterOS, use a dedicated address-restricted
account with only SSH/read permissions.

Populate `/etc/padval-bot/ssh/known_hosts` out of band and verify every host-key
fingerprint before enabling the service. Padval Bot will not accept changed or
unknown host keys automatically.

## 6. Test and enable

Run one collection without Telegram:

```bash
sudo -u padval-bot /opt/padval-bot/venv/bin/padval-bot \
  --config /etc/padval-bot/config.json --once
```

Install and start the unit:

```bash
sudo install -o root -g root -m 0644 deploy/padval-bot.service \
  /etc/systemd/system/padval-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now padval-bot.service
systemctl status padval-bot.service
```

The service deliberately emits no token, chat contents, SSH output, or live
configuration values to its journal.

## Updating

Build and test a new checkout first, then reinstall it into the existing
virtual environment and restart only `padval-bot.service`. Keep the private
configuration and state directory in place. Roll back by reinstalling the
previous known-good Git revision.

For automatic production releases, install the root-owned controller and
forced-command entry point from `deploy/`, create a dedicated
`padvalbot-deploy` account with no Docker access, and store only its restricted
SSH private key as the `PADVAL_DEPLOY_SSH_KEY` production environment secret.
The deploy workflow sends the exact tested revision, performs live destination
preflight checks, waits for a fresh Telegram polling heartbeat, and restores
the prior release if the candidate does not become healthy.
