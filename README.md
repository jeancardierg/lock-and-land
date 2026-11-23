# Lock & Land

A free desktop app that helps you job hunt. It finds jobs that fit your CV, scores them, writes a draft cover letter
for each good match and waits for you. **Nothing is ever sent or submitted unless you approve it.**

- **Private by design.** Everything runs on your own computer. The AI that reads your CV and writes letters is
  [Ollama](https://ollama.com), a free local model runner, so your CV never goes to an AI company.
- **Honest letters.** Every sentence in a letter has to point at a fact in your own checked facts profile. A checker
  blocks invented numbers, tools, titles, certifications, salary figures, colleague names and health details.
- **Free.** No subscription and no card. Every job source it uses is free; a few need a free key.
- **Windows 10/11 and Apple Silicon Macs (M1 and newer).**

```
job sites ──► your criteria filter ──► local AI scores the fit ──► local AI drafts a letter from your facts
          ──► fact checker ──► you review: approve, edit or skip
approve   ──► email jobs: sent from your Gmail after you confirm the recipient
          ──► web jobs: the letter is copied and the ad opens in your browser; you apply there and press "I applied"
```

The app never fills in job-site forms, never signs in to job sites and never touches LinkedIn.

## What you need

| | |
|---|---|
| Computer | Windows 10 or 11, or a Mac with an Apple chip (M1 or newer) |
| Memory | 16 GB is best. 8 GB works, but letters are slow |
| Disk | About 15 GB free for the AI models |
| Your CV | PDF or Word |
| Optional | A personal Gmail account (read job-alert emails, send email applications), a Telegram account (phone alerts) |

## 1. Install

Download the installer for your computer from the [latest release](../../releases/latest):

| Computer | File |
|---|---|
| Windows 10 or 11 | `LockAndLand-setup-<version>.exe` |
| Mac with an Apple chip (M1 or newer) | `LockAndLand-<version>.dmg` |

Each release also has `SHA256SUMS-windows.txt` and `SHA256SUMS-macos.txt` if you want to check the download.

### Windows

1. Double-click `LockAndLand-setup-<version>.exe`.
2. Windows shows **Windows protected your PC**, because the app isn't signed with a paid certificate. Click
   **More info**, then **Run anyway**.
3. Click **Next** until **Install**, then **Finish**. No administrator password is needed (it installs for your user only).
4. Lock & Land opens in its own window. Its orange icon also sits near the clock (it may be under the **^** arrow).

If your antivirus asks about the file, choose **Allow**.

### Mac (Apple Silicon)

1. Double-click `LockAndLand-<version>.dmg` and drag **Lock & Land** onto **Applications**.
2. Open **Applications** and double-click **Lock & Land**. macOS says it can't verify the app, because the app isn't
   registered with Apple. Click **Done** (not Move to Bin).
3. Open **System Settings → Privacy & Security**, scroll down to the message about Lock & Land and click
   **Open Anyway**. Enter your Mac password if asked.
4. Open Lock & Land again and click **Open**. From now on it opens normally. Its icon sits in the menu bar.

If macOS says the app **is damaged**, open **Terminal** (Applications → Utilities), run this line, then try step 2 again:

```bash
xattr -cr "/Applications/Lock & Land.app"
```

## 2. First run

The **Configuration** page has a **Getting started** list that ticks each step off when it's done.

1. **Your CV and job criteria (5 min).** The first page is **Job criteria**. Choose your CV file at the top and the
   app fills in most fields: roles, title words, skills, salary floor, home city, work mode, remote regions and
   languages. Check them, fill in anything marked in orange, tick the box that says your CV is accurate and press
   **Save criteria**.
2. **Check your facts profile.** From your CV the app builds `facts/master_profile.json` in its data folder: one
   short, numbered fact per achievement, each with how much you owned it (supported, executed, led, owned...).
   Letters can only use these facts, so read them once and fix anything that is wrong or overstated. The file also
   holds names never to mention, a fixed line for a career break if you have one, and pre-approved answers for
   common screening questions (work authorisation, sponsorship, languages). Salary and notice period are always left
   for you to answer.
3. **Local AI (20–40 min, mostly waiting).** On Configuration → **Local AI**, press **Download Ollama**, install it
   like any app and come back: the card notices by itself. Then press **Download the models** (several GB). The
   card checks your computer's memory and graphics card and suggests models that fit, for example `qwen2.5:14b` for
   letters, `qwen2.5:7b-instruct` for scoring and checks, and `bge-m3` for matching. You can keep using the app
   while it downloads.
4. **Gmail (optional, 3 min).** Use an **app password**, a 16-letter password Google makes just for this app:
   1. Turn on 2-Step Verification at [myaccount.google.com/signinoptions/twosv](https://myaccount.google.com/signinoptions/twosv).
   2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), type `Lock and Land` and press **Create**.
   3. Paste the 16 letters with your Gmail address on Configuration → **Gmail** and press **Save and test**.

   Work or school accounts often block app passwords; use a personal Gmail or skip this step. With Gmail connected
   the app also reads LinkedIn job-alert emails you set up in LinkedIn yourself.
5. **Phone alerts with Telegram (optional, 3 min).** On Configuration → **Phone alerts**:
   1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts. It gives you a token.
   2. Paste the token in the app and press **Save** (the app checks it with Telegram).
   3. Open your new bot in Telegram and press **Start**, then press **Find my chat** in the app.
   4. Press **Send test message**.

   You get a message when letters are ready, when a job is waiting for you, when a sign-in expires or when the
   database has a problem. Messages carry a short notice and the job title only, never letter text.
6. **Job sources (optional).** These work with no setup: Himalayas, Jobicy, We Work Remotely, Working Nomads,
   Remotive, Arbeitnow, RemoteOK, Hacker News "Who is hiring", The Muse, and LinkedIn job-alert emails (needs Gmail).
   For more local jobs, add a free key on Configuration and switch the source on:
   - **Jooble** (good for Spain and many other countries): request a key at [es.jooble.org/api/about](https://es.jooble.org/api/about) (other countries have their own Jooble site). It arrives by email, usually within a day.
   - **Careerjet**: free publisher key from [careerjet.es/partners](https://www.careerjet.es/partners/). **Findwork**: free key from [findwork.dev](https://findwork.dev).
   - **Company watchlist**: public job boards of companies you pick (Greenhouse, Lever, Ashby, Workable, SmartRecruiters).
   - Adzuna is supported but off by default, because it becomes paid after a trial.
7. **Save your recovery key (1 min).** Your database is encrypted and its key is kept in your OS keychain. After the
   first job search, press **Show recovery key**, put it in your password manager and press **I've saved it**. You
   only need it if you move to a new computer or reinstall the OS.

After that the app searches every few hours by itself. Open it from the icon, read the jobs and letters, and
approve, edit or skip each one.

### A CV tailored to one ad

On any letter card, press **Tailor CV**. You get a one-page CV for that ad, as a PDF and a Word file, in English or
Spanish to match the ad. It is built only from your facts profile: your real job titles, employers and MM/YYYY
dates, your verified skills (the ones the ad names first), your achievements word for word (the ones closest to the
ad first) and a 2 to 3 sentence summary the local AI writes and the fact checker checks. The layout is the one
applicant tracking systems read best: one column, standard headings (Professional Summary, Skills, Work Experience,
Education, Languages), no tables, columns or pictures.

The card shows which of the ad's keywords the CV has (green) and which it doesn't (struck through). Missing ones
are never added: if you do have that experience, add it to your facts profile and press **Build again**. Email
applications attach the tailored PDF (switch back to your standard CV on the card); for web applications use
**Open CV** on the Waiting tab and upload it on the site.

### Adding a job yourself

Some job sites hide ads behind a sign-up wall, and some jobs reach you by message. Open **Add a job**, paste the
whole ad with its title (company, location and link are optional) and press **Add and write the letter**. It is
scored like any other job, a letter is written whatever the score and, with **Tailor my CV too** ticked, a tailored
CV as well. It shows under **To review**.

## Where your data lives

| What | Where |
|---|---|
| Passwords and keys (Gmail app password, Telegram token, source keys, database key) | Windows Credential Manager or macOS Keychain, service `jobhunter` |
| Settings (`config.yaml`), CV, facts profile, letters, tailored CVs (`applications/`), logs | The data folder: `%LOCALAPPDATA%\jobhunter` on Windows (you can move it on Configuration → Data folder), `~/Library/Application Support/jobhunter` on macOS |
| Jobs database | The data folder, encrypted with SQLCipher (AES-256) |

None of this is ever uploaded anywhere. Don't put the data folder in a public or shared sync folder.

## If something goes wrong

| What you see | What to do |
|---|---|
| The icon turns amber | Open Configuration: the Getting started list or the red banner says what needs you |
| Gmail says the password wasn't accepted | Copy all 16 letters again, or delete the app password at Google and make a new one. Your normal Google password won't work |
| "App passwords" is missing at Google | Turn on 2-Step Verification first. Work or school accounts may not allow it |
| The model download stops | Check your internet and free disk space, then press the download button again. It resumes |
| Letters take many minutes | Normal with 8 GB of memory. They are written in the background |
| The window doesn't open | Click the Lock & Land icon near the clock (Windows) or in the menu bar (Mac) |
| No phone alerts | Configuration → Phone alerts: press Start in your bot, then Find my chat, then Send test message |

Bug reports and ideas are welcome in [Issues](../../issues). Please never paste your recovery key, app password,
tokens or your facts profile into an issue.

## For developers

Python 3.11. The app is a tray app (pystray) with a native window (pywebview), background queues (Huey on SQLite)
and an encrypted SQLite database (SQLCipher). There is no web server and no network port: the window's pages call
the app's FastAPI routes in-process through a private named pipe (Windows) or Unix socket (macOS). The only
network connections are to the job sources, Gmail and Telegram if you set them up, and Ollama on your own computer.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python -m pytest -q                     # runs on the fictional sample profile in tests/fixtures
.venv/bin/python -m jobhunter doctor              # checks your setup
.venv/bin/python -m jobhunter run                 # one fetch, score and draft cycle in the terminal (never sends)
```

- Installers: `packaging/build_windows.ps1` (needs Inno Setup 6) and `packaging/build_mac.sh`. GitHub Actions
  (`.github/workflows/build.yml`) builds both on every push to `main`. Publishing a GitHub Release with a new
  `v*` tag (for example `v0.4.1`) makes the same workflow attach both installers and their checksums to it.
- Config reference: [`config.example.yaml`](config.example.yaml). The app writes your own `config.yaml` in the data folder.
- Desktop internals: [DESKTOP.md](DESKTOP.md). History: [CHANGES.md](CHANGES.md).
- Personal files are never committed: `.gitignore` excludes `config.yaml`, `.env`, `cv/`, `facts/`, `data/` and databases.

## Security

See [SECURITY.md](SECURITY.md) for how to report a problem privately.

## License

[MIT](LICENSE)
