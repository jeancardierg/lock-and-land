# Automated applications removed: every web job is a hand-off (23 Nov 2026)

- Bot protection (Cloudflare, DataDome, PerimeterX and similar) blocks automated form filling too often, so the app no
  longer drives a browser at all. Gone: ATS form filling, account sign-up and sign-in, verification-email links,
  bot-check waiting, the Lock & Land Chrome/Edge profile, the sign-in browser, the "Browser to use" setting, the
  browser queue and the `fill` / `try-form` commands. Playwright is no longer bundled, so the installer is smaller
  and Google Chrome is not needed.
- "Approve & fire" is now **Approve & apply** (or **Approve & send** for jobs with an email address). For a web job
  it copies the letter plus any answered screening questions, opens the ad in your usual browser and puts the job on
  the Waiting tab. Apply there, then press **I applied**. The Waiting tab also has **Copy letter** to copy it again.
- Email applications through Gmail are unchanged (recipient shown, two confirmations, CV attached).
- Passwords that the previous build saved for job-site accounts stay in the keychain. The Waiting tab lists them as
  "Saved job site passwords" with Copy (after the OS pop-up) and Forget.
- Drafting, fact checks, scoring, sources and tracking are unchanged. The old `browser:` section in config.yaml is
  ignored, and the old `data/browser` profile folder can be deleted by hand.

# Security checks after Approve & fire, browser choice, "Configuration" (23 Nov 2026)

- Approve & fire on a Himalayas job stuck on Cloudflare's "Performing security verification" page. The app loaded the
  page, didn't recognise that wording, filled nothing and gave no message, so Chrome just sat there.
- Full-page bot checks (Cloudflare "Just a moment...", "Performing security verification", DataDome, PerimeterX
  "Press & hold") are now recognised. The app never clicks or answers them: it brings the window to the front, sends a
  notification ("please complete the check in the Lock & Land window"), shows the same on the Waiting tab, and
  carries on filling by itself once the real page appears. The same happens if the employer's own site shows a check.
- If the check isn't finished within 5 minutes (`browser.challenge_wait_s`), the job stops cleanly: nothing filled,
  moved to "Needs you" with a plain reason, tab left open so you can apply there and press "I applied".
- Job boards (Himalayas, RemoteOK, WWR, Working Nomads, Jobicy, Remotive, The Muse, Arbeitnow, Jooble, Careerjet,
  Findwork): the app follows the ad's Apply link to the employer's real form, in the same tab, instead of filling the
  board's own search or newsletter boxes. Never into LinkedIn, Indeed, InfoJobs or Glassdoor.
- No more stray about:blank tab: the empty tab Chrome opens with is reused for the first job.
- The "Chrome is being controlled by automated test software" bar is gone (as in the sign-in window). Nothing else is
  hidden from sites: no stealth, no fingerprint changes, captchas are always yours.
- Connections is now called **Configuration** (old /connections links still work). It has a "Browser to use" choice:
  Google Chrome or Microsoft Edge, each with its own Lock & Land profile. Your everyday browser profile can't be
  used: Chrome (since version 136) and Edge refuse to let an app drive it. Pass a site's check once in the sign-in
  browser and the profile remembers it.
- The Waiting card's "Open the ad" is now "Open in my usual browser", the hand-off for sites that keep blocking.

# Fix: "database is locked" when saving an edited letter (23 Nov 2026)

- Saving a hand-edited letter (Save and recheck) or approving could wait 30 s and fail with "database is locked".
  Cause: the recruiter mail poll re-sees messages it already stored; the duplicate INSERT failed and left its
  transaction open, so that background thread kept the database's write lock until its next write. The same could
  happen when a job board re-listed a job under a new title.
- Every database write now commits or rolls back on its own, a leftover open transaction is closed (and logged) the
  next time that thread uses the database, and embeddings are saved per batch instead of holding vectors.db locked
  while the local AI works.
- If the database is ever busy, the window now says plainly that nothing was saved and to try again.
- The background second-opinion check no longer overwrites the result of a letter you edited while it was running.

# Accounts on job sites (23 Nov 2026)

- When a form needs an account (Workday, SuccessFactors, Taleo, iCIMS, or any page with a password box), the
  "Waiting for you" card offers: Create account for me, Sign in for me (once saved), Show me the steps,
  Find the verification email, and Account made: fill the application.
- Create account for me asks in an operating-system pop-up first. After Yes, the app's Chrome fills the sign-up form
  with your email, name, phone and country from the checked facts profile and a generated 16-character password.
  It never ticks the site's terms, never solves a captcha and never presses Create account or Sign in: those are
  outlined for you. The only click it makes is on a plain "Create account" link while no sign-up form is showing.
- Passwords are saved only in Windows Credential Manager or the macOS Keychain (one entry per site), never in the
  database, config, logs or screenshots. "Job site accounts" on the Waiting tab lists the sites; copying or forgetting
  a password asks in the OS pop-up.
- If you say No (or do it yourself), the card shows step-by-step instructions with Copy buttons and a generated
  password. The verification-email check reads your mailbox for 15 minutes and only offers links to the same site;
  opening one asks first.

# Security hardening (23 Nov 2026)

- The riskiest actions now ask in an operating-system dialog that page script can't click: showing or restoring the
  recovery key, sending an application by email, and moving the data folder. Outside the desktop app
  (`jobhunter review`, tests) nothing changes.
- Every page in the app window carries a Content Security Policy: no remote scripts, frames, plugins, form posts or
  `<base>` tricks. `JOBHUNTER_NO_CSP=1` turns it off if a WebView version ever objects.
- Earlier the same day (PR #5): job links are safe in the review page, `javascript:` links don't run, the dev server
  refuses cross-site requests, API keys are kept out of the log (which now rotates), ad links to local or home-network
  addresses aren't fetched, and CI is pinned with SHA256 checksums for each build.

# Local AI suggestions per device (23 Nov 2026)

- Connections -> Local AI now detects this computer (OS, CPU, RAM, NVIDIA/AMD/Intel GPUs and VRAM via nvidia-smi or
  the Windows display-adapter registry, Apple Silicon unified memory and the GPU share macOS allows) and Ollama
  (installed, running, version, installed models, and how much of each loaded model really sits on the GPU).
- It suggests a model, quantisation and context per task (letters, scoring, fact check, embeddings), explains why,
  warns on weak hardware with a fallback (3B models, or Ollama on a stronger computer), and offers a bigger letter
  model when it would run partly on the CPU. "Use these settings" writes `llm.models` + `llm.num_ctx` (backup kept)
  and downloads what's missing with progress. "Start Ollama" button when it's installed but not running.
- New `llm.num_ctx.<task>` (per-task context); `python -m jobhunter doctor` reports whether the models suit the machine.
- First run: "choose the local AI models" appears in Setup needed until a suggestion has been applied.

# v0.3 (21 Nov 2026): grounded letters, review gateway, form pre-fill

Phase 1: fact grounding (all local, no paid API)
- `facts/master_profile.json`: ~43 atomic facts with IDs, ownership level (supported → designed) and never-say rules,
  verified skills, fixed career-break line, names never to mention, and a Q&A bank (work authorisation, sponsorship...).
- Letters are generated as JSON constrained by Ollama `format`: every claim sentence must cite fact IDs from an enum of
  the retrieved facts. A deterministic validator blocks numbers, tools or stronger verbs not in the cited facts,
  certifications, salary figures, false titles, colleague names and health details; a 7B second opinion lowers the score.
  Up to 3 rewrites with the validator's feedback. Fixes v0.2's false positives (e.g. "Led delivery" of Intune).
- Retrieval with bge-m3 embeddings (numpy, cached in SQLite). Must-haves with no matching fact show as gaps.
- Full job ads from public ATS endpoints (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Personio, JSON-LD).
- Removed the Anthropic provider and key. LinkedIn alert parser no longer stores the title as the company.
- Optional IMAP/SMTP mail backend with a Gmail app password (no 7-day OAuth expiry).

Phase 2: review gateway
- Job state machine (queued → extracted → scored → generated → needs_review → approved → submitting → awaiting_click →
  submitted, plus filtered / relocation / needs_human / rejected / failed). Every change is a guarded update plus a
  `job_events` row in one transaction: a double click can't approve or send twice.
- `migrate` backs up the database first. v0.2 drafts go back to `scored` for grounded rewriting.
- Local queues (Huey on SQLite): main (ingest every 3 h, recruiter mail, notifications), gpu (one model at a time),
  browser (one Chrome window). Windows toast notifications; optional Telegram (title + link only).
- Review page on :8502 with A/E/R keys, fact tooltips, checks, editable answers saved to the Q&A bank.

Phase 3: form pre-fill that stops at Submit
- Playwright drives your installed Chrome with a separate profile. Adapters for Lever, Ashby, Workable, Greenhouse,
  Workday (first page only) and a generic label matcher (EN/ES). Values come only from config, the master profile,
  the Q&A bank and the answers you reviewed. CV and letter PDF uploaded.
- Never clicks Submit, never solves or hides captchas, no stealth plugins, never opens LinkedIn/Indeed/InfoJobs/Glassdoor.
  Voluntary equal-opportunity questions get "prefer not to say" where offered; consent boxes are left for you.
- Confirmation page detection moves the job to `submitted`; closed tabs and blockers move it to "needs you".
- Daily cap (`limits.submissions_per_day`, default 15).

# v0.2 (20 Nov 2026): review fixes + CV integration
- Attachments point at your final CV PDFs; sends are blocked while a PDF still has contact placeholders.

Safety
- Removed Playwright and all LinkedIn browser automation (login, pre-fill). LinkedIn's terms ban automated activity, and
  pre-filling through a driven browser was still automation. Now: copy the draft, open the link in your own browser.
- Gmail scope narrowed from `gmail.modify` to `gmail.readonly` + `gmail.send`.
- Background runs never start the OAuth browser flow (the old code could hang the scheduled task for an hour); expired
  or revoked tokens fail fast with "run gmail-auth".
- Email sends: exact recipient shown, "I've reviewed" tick required, single valid recipient enforced (no comma lists from
  job ads or Reply-To), CR/LF stripped from subjects, double-click can't send twice (atomic claim), Reply-To re-checked at send.
- Applications are no longer sent silently without the CV when the PDF is missing: the send is blocked.
- Job/recruiter text is marked untrusted in prompts (prompt-injection hygiene). Dashboard bound to 127.0.0.1, only http(s) links rendered.

CV integration
- `cv/` holds up to three CV variants (main, lead, Spanish); the app picks the variant per job and attaches the matching PDF.
- Profile, targets, markets (Barcelona hybrid / remote EU), minimum salary, keywords and drafting rules wired into config.
- Phrasing rules for tools you only know indirectly (for example "Jamf-comparable").

Other fixes
- Off-target sources/queries dropped (Sweden JobTech off, Adzuna Spain only, Remotive customer-support category removed,
  Arbeitnow remote-only). Removed IT support / service desk queries; first-line roles excluded.
- Keyword bonus (+2 per target keyword, max +10) on top of the Ollama score, shown in the dashboard.
- Scoring uses the CV variant that fits the job. SQLite busy timeout raised for scheduler + dashboard running together.
- Logging works under pythonw (no stderr). `gmail-auth` warns if profile.email doesn't match the authorised account.
- Tests: 8 → 16.

Later on 20 Nov
- Drafting provider switch: `llm.drafting.provider: ollama` (default, local qwen2.5:14b, free) or `claude`.
- `scripts\gmail_setup.ps1`: guided Gmail OAuth setup.
- Remotive categories devops + information-technology (verified live). Ollama model qwen2.5:14b.
- Scheduled task time limit 2 h; max 8 drafts per run.
- Fact check v2: letter only, real quotes only, deterministic number and banned-phrase checks, one rewrite retry on banned phrases.
- Forwarded LinkedIn mail recognised; never replies to your own other mailboxes (`profile.other_emails`).
- New dashboard: job table with filters and a detail pane, inline editing, status sidebar (runs, schedule, Gmail
  sign-in expiry with re-sign-in, source health), background Run now, tracker stages, help tab.
- Desktop/Start-menu shortcut (`scripts\install_shortcut.ps1`); `dashboard.cmd` reuses a running dashboard.
- Run lock so a manual run and the scheduled task never overlap; per-source health saved with each run.
- Rules: jobs requiring a language other than English/Spanish are excluded; on-site/hybrid jobs outside
  Barcelona and its ~1 h commute towns go to a "Relocation needed" tab. Search widened to all of Spain.
  Existing jobs are re-sorted automatically on the next run.
