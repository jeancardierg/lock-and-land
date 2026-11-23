# Lock & Land desktop app internals

One app per OS: a tray icon (Windows) or menu-bar icon (macOS), the app window, and the background schedule, all in
one process. There is no web server and no network port.

## What runs where
| Piece | How |
|---|---|
| App window | pywebview (WebView2 on Windows, WebKit on macOS). Its pages call the app's routes in-process through a private named pipe (Windows) or Unix socket (macOS), protected by a random key in the data folder. Nothing listens on TCP. |
| Schedule | Built in: fetch every 3 h, letters every 2 min, recruiter mail every 30 min, sign-in check every 6 h. An overdue fetch runs a minute after start. Tray > Pause stops it. |
| Start at login | HKCU Run key (Windows), LaunchAgent `app.jobhunter.tray` (macOS). Tray > Start at login. |
| Sign-ins and settings | The Configuration page. An amber tray icon means something needs you. |
| CLI | `jobhunter.exe doctor`, `jobhunter.exe gmail-auth --force`, `jobhunter.exe import <folder>` ... (same commands as `python -m jobhunter`) |

The only network traffic is outgoing: job sources, Gmail and Telegram if you set them up, and Ollama on your own
computer (`localhost:11434` by default). Gmail OAuth (the advanced option) opens a one-off sign-in page in your
browser, which is gone as soon as Google sends you back.

## Where things live
| What | Where |
|---|---|
| Secrets (Gmail app password or OAuth token, job-source keys, Telegram token, database key) | Windows Credential Manager / macOS Keychain, service `jobhunter` |
| Settings, CVs, facts, letters, logs | The data folder: `%LOCALAPPDATA%\jobhunter` on Windows (movable on Configuration > Data folder), `~/Library/Application Support/jobhunter` on macOS |
| Databases (jobs, vectors, queues, backups) | The data folder, encrypted with SQLCipher 4 (AES-256), key in the keychain |

**Recovery key:** Configuration > Database encryption > Show recovery key. Put it in a password manager. Without it a
lost keychain entry (new PC, OS reinstall) means the database can't be opened.

## Sign-in per source
| Source | Sign-in |
|---|---|
| Gmail | Easiest: an **app password** on Configuration > Gmail (needs 2-Step Verification; no Google Cloud project, no weekly expiry). Advanced: OAuth with your own Desktop app client (read + send); Google ends it every 7 days while the Cloud app is in Testing, and every other Gmail must be added as a test user. |
| Jooble, Careerjet, Findwork | Free API keys on Configuration |
| Adzuna | Developer API keys. Optional and paid after the trial; off by default |
| Other boards | Public, nothing to sign in to |
| LinkedIn | Never signed in to; job alerts are read from Gmail |
| Employer job sites | Not used by the app: you apply and sign in on them in your usual browser |

## Build
- Windows: `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` (Python 3.11 + Inno Setup 6) -> `dist\LockAndLand-setup-<version>.exe`
- macOS: `bash packaging/build_mac.sh` on a Mac -> `dist/LockAndLand-<version>.dmg`. Unsigned: see the README for the first-launch steps.
- Both: `.github/workflows/build.yml` builds them on GitHub runners on every push to `main`, and attaches them to a GitHub Release when one is published with a new `v*` tag.

Ollama is installed separately. Google Chrome is not needed.
