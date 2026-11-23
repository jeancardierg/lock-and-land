# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include what you found, how to reproduce it, and which
version or commit you used. You should get a first answer within a week.

Never include real secrets in a report: no recovery keys, app passwords, tokens, API keys or facts profiles.

## What the app protects

- **Secrets** (Gmail app password or OAuth token, Telegram bot token, job-source keys, the database key) are stored
  only in Windows Credential Manager or the macOS Keychain, never in files, the database or the logs. Tokens and keys
  are redacted from log lines.
- **The database** is encrypted with SQLCipher 4 (AES-256). The recovery key is shown only after an operating-system
  confirmation.
- **The local page** listens on `127.0.0.1` only, refuses cross-site requests, and every page has a strict Content
  Security Policy. Risky actions (showing the recovery key, sending an email application, moving the data folder) ask
  in a native OS dialog that page script can't click.
- **Untrusted input.** Job ads and recruiter emails are treated as untrusted text in every AI prompt, links are
  limited to http(s), and links to local or home-network addresses are never fetched.
- **Nothing is sent without you.** Email applications need your approval, the exact recipient on screen and a second
  confirmation. The app never fills in or submits web forms.
- **Builds.** GitHub Actions steps are pinned to exact commits, and every build publishes SHA256 checksums.
  The installers are not code-signed, which is why Windows and macOS warn on first launch.

## Supported versions

Only the latest release gets fixes.
