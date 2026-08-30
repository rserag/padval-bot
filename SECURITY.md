# Security policy

## Reporting a vulnerability

Do not open a public issue containing a vulnerability, bot token, chat ID, SSH
key, hostname, IP address, domain, log excerpt, or configuration from a live
deployment.

Use the repository's private vulnerability-reporting feature when it is
available. Otherwise contact the maintainer privately before sharing details.

## Deployment expectations

- Run the bot as a dedicated unprivileged account.
- Keep tokens, pairing secrets, chat IDs, keys, and live JSON configuration out
  of Git and restrict them with filesystem permissions.
- Use one authorized private Telegram chat.
- Give remote SSH accounts the smallest read-only command surface possible.
- Pin and verify SSH host keys.
- Treat Docker socket access as root-equivalent.
- Review status output before adding checks; summaries can still reveal useful
  infrastructure information to an unauthorized recipient.

The project intentionally does not support tokens passed as command-line
arguments or committed inline configuration values.
