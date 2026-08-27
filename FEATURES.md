# TimeTracker Cloud — feature picklist

Rebuild of `~/code/active/timetracker` as a hosted, multi-user app.
Target: 2 tracked users + 1 admin, on AWS.

Source of truth for "what the app already does" is the local app. Nothing here
is ported blindly — each item is re-implemented against the multi-user schema.

Status: `[ ]` todo · `[~]` in progress · `[x]` done

---

## A. Capture agent — runs on each user's machine

- [x] A1  Active window + app detection (X11 `xdotool`; from `activity.py`)
- [x] A2  Idle detection via X11 screensaver counter, 15-min pause threshold
- [x] A3  Suspend/freeze detection (loop-gap heuristic)
- [x] A4  App-usage logging — titles normalised at capture so spans do not fragment
- [x] A5  Idle-period logging — actually written now; the local app never did
- [x] A6  Screenshot capture, gated on "session active AND not idle AND enabled"
- [x] A7  Thumbnail generation
- [x] A8  Capture sound notification
- [x] A9  **NEW** Local spool + batched upload, survives a dropped connection
- [x] A10 **NEW** Per-device agent token, rotatable
- [x] A11 **NEW** WebP encoding before upload (~5× smaller than PNG)

## B. Sessions & core data

- [x] B1  Start/stop session with project + task
- [x] B2  Active-session state
- [x] B3  Orphaned-session recovery, capped at the last heartbeat
- [x] B4  Auto-resume after idle/suspend, without crediting the gap
- [x] B5  Race-safe start — now a partial unique index, not a Python dance
- [x] B6  Logical day/week boundaries (`WEEK_CUTOFF_HOUR`)
- [x] B7  Session history

## C. Dashboard (web)

- [x] C1  Today summary — total, status, active session
- [x] C2  Live status polling
- [x] C3  Weekly summary view
- [x] C4  Screenshot gallery by date
- [x] C5  Session history page
- [x] C6  Start/stop controls (browser and widget, reconciled)

## D. Daily activity log

- [x] D1  Auto-draft from window titles (`summarize.py` — no model, no network)
- [x] D2  Categorisation: Coding / Web / App / Search / Email / Research / Shell
- [x] D3  Evening prompt window (21:00–24:00), gated on actual presence
- [x] D4  Pending queue — a missed day stays queued indefinitely
- [x] D5  Answers: confirm / skip / leave-as-is
- [x] D6  Top-up when an answered day gains more undescribed time
- [x] D7  Activity-log history

## E. Reporting & email

- [x] E1  Weekly report, Mon→Sun, sent Monday 17:00
- [x] E2  Week-over-week absolute delta
- [x] E3  Daily bar chart (PNG, inline `cid:`)
- [x] E4  Category donut
- [x] E5  Activity list with private/research label filtering
- [x] E6  Project rows
- [x] E7  Monthly report, last day of month 21:00
- [x] E8  Month-over-month **percentage** change
- [x] E9  Work-stream donut (ordered patterns + catch-all)
- [x] E10 Week-by-week bars
- [x] E11 Coverage caption — described time vs tracked time
- [x] E12 Days-described line
- [x] E13 Email previews for both reports
- [x] E14 Manual send endpoints (recipient override for samples)
- [x] E15 Send-once guards — per user, a DB constraint, not a JSON file
- [x] E16 One-off catch-up window for schedule changes
- [x] E17 **NEW** "No history is not a missed send" guard on every trigger

## F. Desktop widget

- [x] F1  Ring gauge, day + week goals
- [x] F2  Overdrive state past 100%
- [x] F3  Docked top-centre positioning
- [x] F4  Greeting toast on session start
- [x] F5  Focus cheers every 25 min of non-idle tracking
- [x] F6  Tray menu — show/hide, dock, pause, dashboard, quit
- [x] F7  Daily-prompt card
- [x] F8  Offline detection
- [x] F9  **NEW** Points at a configurable server URL, carries the agent token

## G. Multi-user platform — all NEW

- [x] G1  User accounts + login
- [x] G2  Password reset
- [x] G3  Roles: worker vs admin
- [x] G4  Per-device agent tokens
- [x] G5  `user_id` on every table; every query filtered
- [x] G6  Admin: team overview — each person shown in their own timezone
- [x] G7  Admin: per-person drill-down
- [x] G8  Per-user settings — timezone, goals, idle threshold, prompt hours,
          streams, private labels, send times
- [x] G9  Per-user email delivery (no shared Gmail app password)
- [x] G10 Consent record + a pause control the tracked user holds

## H. Infrastructure — all NEW

- [x] H1  Postgres + Alembic migrations
- [x] H2  Gunicorn + Caddy (automatic TLS) + domain
- [x] H3  Background worker OUT of the web process (4 workers = 4 emails)
- [x] H4  Timezone-aware datetimes throughout
- [x] H5  S3 screenshot storage, private bucket, presigned URLs
- [x] H6  S3 lifecycle — full-res 30 days, thumbnails 1 year
- [x] H7  Secrets via environment, never on disk
- [x] H8  Docker Compose deploy
- [x] H9  Backups + a *tested* restore
- [x] H10 Health checks (liveness + readiness) and structured logs
- [x] H11 Rate limiting on login and ingest

## I. Knowing the tracking itself broke — all NEW

- [x] I1  `orphaned_at` — an end time the server inferred is marked as inferred
- [x] I2  Alert when a running session had to be capped, with the time lost
- [x] I3  Alert when a device stops reporting for days
- [x] I4  Send-once guard — a DB constraint, per session and per silence episode
- [x] I5  Dormancy re-arms when the agent reports again, with no flag to reset
- [x] I6  Silent while tracking is paused — a pause is not a fault
- [x] I7  Opt-out separate from the reports opt-out
- [x] I8  Agent staleness on the admin team page — shown, never mailed
- [x] I9  `check-agents [--dry-run]` for inspecting the condition by hand

## J. Idle pauses a session instead of closing it — all NEW

- [x] J1  15 minutes of no input pauses; it used to close at 10
- [x] J2  The same session resumes — project, task and history intact
- [x] J3  `sessions.idle_since` — the idle stretch in progress, which has no row yet
- [x] J4  Every total subtracts idle, merged so no minute is deducted twice
- [x] J5  Idle is spooled when it STARTS, so a crash cannot hide it
- [x] J6  Idle in progress is held back from upload until it ends
- [x] J7  The session is heartbeated while paused, so it is not capped as dead
- [x] J8  A pause waits indefinitely — no maximum, no guessing
- [x] J9  A suspend of any length pauses; there is one rule, not a threshold
- [x] J10 A session paused when it was capped raises no I2 alert
- [x] J11 A pause survives the agent restarting — the mark is settled at boot
- [x] J12 The dashboard shows paused as its own state, not as tracking
- [x] J13 The session figure on screen is tracked time, so it agrees with the day
- [x] J14 A session paused since before today began is closed where input stopped

## K. Activity, so that time credited is time worked — all NEW

- [x] K1  Per-minute activity from the idle counter already polled — no new access
- [x] K2  A minute counts if ANY input landed in it, so reading is not absence
- [x] K3  Ten-minute windows, clock-aligned, so a capture matches one by time
- [x] K4  Active AND tracked minutes stored — a half-covered window says so
- [x] K5  A percentage under every screenshot, and for the day
- [x] K6  Activity on the weekly report
- [x] K7  Server refuses a window claiming more active minutes than tracked
- [x] K8  Never subtracted from hours — reading and calls are work at 0%
- [x] K9  "Not measured" renders as a dash, never as 0%
- [x] K10 Screenshot timing jittered ±35%, so the schedule cannot be worked around
- [x] K11 Consent version bumped — everyone is asked again

## L. The year, month by month — all NEW

- [x] L1  A person's whole year in the admin panel, twelve months at a glance
- [x] L2  Hours, days worked, average working day and activity per month
- [x] L3  Built on `daily_totals`, so breaks and timezones behave as everywhere
- [x] L4  Months cut at the worker's own midnight, not the server's
- [x] L5  Future months shown and empty — a year stopping in August reads wrong
- [x] L6  Anyone sees their own; an admin sees anybody's, via the same rule
- [x] L7  Year bounded, so a crafted query cannot ask for a million days

---

All 119 done. What is deliberately NOT here, and why:

- **No self-service password reset form.** An unauthenticated way to make the
  domain send mail to any address someone types buys nothing behind a private,
  three-person deployment. An administrator issues links.
- **No public sign-up.** Open registration on a server holding screen captures
  is a liability nobody asked for.
- **No admin control over anyone's pause.** A switch someone else can flip is
  not a control.
- **No alert when an agent goes quiet during the day.** Telling "the agent
  crashed at 11am" apart from "she finished at 4pm and shut the laptop" needs
  working hours nobody is asked for, and the false alarm would train everyone
  to ignore the channel — taking the real alerts with it. The dormancy alert
  catches the same fault a few days later, when it is no longer a guess.
- **No alert to an administrator about somebody else's agent.** They have the
  team page, which shows the same staleness by looking. Being notified about a
  colleague's laptop is a different thing from being able to check on it.
- **No maximum pause, and no "breaks".** A pause stops the clock and waits, for
  as long as it takes. Deciding on somebody's behalf that they have been away
  long enough to have gone home needs working hours nobody is asked for, and it
  was a second concept doing a job the pause control (G10) already does. One
  rule — idle pauses, you resume, or you pause on purpose — is the whole of it.
