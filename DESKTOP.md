# jobhunter desktop app (v0.4)

One app per OS: a tray icon (Windows) or menu-bar icon (macOS) that runs the review page, the background schedule
and the sign-in page. Replaces the venv + PowerShell scheduled tasks.

## What runs where
| Piece | How |
|---|---|
| Review page | http://127.0.0.1:8502 inside the app (tray > Open jobhunter) |
| Schedule | built in: fetch every 3 h, letters every 2 min, recruiter mail every 30 min, sign-in check every 6 h. An overdue fetch runs a minute after start. Tray > Pause stops it. |
| Start at login | HKCU Run key (Windows), LaunchAgent `app.jobhunter.tray` (macOS). Tray > Start at login. |
| Sign-ins | Connections page (/connections). Amber tray icon = something needs you. |
| CLI | `jobhunter.exe doctor`, `jobhunter.exe gmail-auth --force`, `jobhunter.exe import <folder>` ... (same commands as `python -m jobhunter`) |

## Where things live
| What | Where |
|---|---|
| Secrets (Gmail token + OAuth client, Adzuna keys, IMAP app password, Telegram token, database key) | Windows Credential Manager / macOS Keychain, service `jobhunter` |
| Settings, CVs, facts, letters, logs | data folder chosen in the installer (pointer in `%APPDATA%\jobhunter\home.txt`); macOS: `~/Library/Application Support/jobhunter` |
| Databases (jobs, vectors, queues, backups) | data folder, encrypted with SQLCipher 4 (AES-256), key in the keychain |

**Recovery key:** Connections > Database encryption > Show recovery key. Put it in a password manager. Without it a
lost keychain entry (new PC, OS reinstall) means the database can't be opened.

## Sign-in per source
| Source | Sign-in |
|---|---|
| Gmail | Easiest (and what testers use): **app password** on Connections > Email (needs 2-Step Verification; no Google Cloud project, no weekly expiry). Advanced: OAuth with your own Desktop app client (read + send); Google ends it every 7 days while the Cloud app is in Testing, and every other Gmail must be added as a test user. |
| Adzuna | developer API keys, no user login. Optional and paid after the trial; off by default |
| Remotive, Arbeitnow, JobTech | public, nothing to sign in to |
| LinkedIn | never logged in to; job alerts are read from Gmail |
| ATS sites (Greenhouse, Lever, Ashby, Workday) | not used by the app: you apply and sign in on them in your usual browser |

## Build
- Windows: `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` (Python 3.11 + Inno Setup 6) -> `dist\jobhunter-setup-0.4.0.exe`
- macOS: `bash packaging/build_mac.sh` on a Mac -> `dist/jobhunter-0.4.0.dmg`. Unsigned: right-click > Open the first time.
- Both: `.github/workflows/build.yml` builds them on GitHub runners if the code is in a GitHub repo.

Ollama is installed separately. Google Chrome is no longer needed.
