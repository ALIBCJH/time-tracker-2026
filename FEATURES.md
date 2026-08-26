# TimeTracker Cloud — feature picklist

Rebuild of `~/code/active/timetracker` as a hosted, multi-user app.
Target: 2 tracked users + 1 admin, on AWS.

Source of truth for "what the app already does" is the local app. Nothing here
is ported blindly — each item is re-implemented against the multi-user schema.

Status: `[ ]` todo · `[~]` in progress · `[x]` done

---

## A. Capture agent — runs on each user's machine

- [ ] A1  Active window + app detection (X11 `xdotool`; from `activity.py`)
- [ ] A2  Idle detection via X11 screensaver counter, 10-min session cutoff
- [ ] A3  Suspend/freeze detection (loop-gap heuristic)
- [ ] A4  App-usage logging (`app_usage` rows — feeds the whole activity log)
- [ ] A5  Idle-period logging
- [ ] A6  Screenshot capture, gated on "session active AND not idle AND enabled"
- [ ] A7  Thumbnail generation
- [ ] A8  Capture sound notification
- [ ] A9  **NEW** Local spool + batched upload, survives a dropped connection
- [ ] A10 **NEW** Per-device agent token, rotatable
- [ ] A11 **NEW** WebP encoding before upload (~5× smaller than PNG)

## B. Sessions & core data

- [ ] B1  Start/stop session with project + task
- [ ] B2  Active-session state
- [ ] B3  Orphaned-session recovery, capped at last-known activity
- [ ] B4  Short-gap auto-resume (≤5 min downtime = crash, not absence)
- [ ] B5  Race-safe start (`BEGIN IMMEDIATE` + close-all-active)
- [ ] B6  Logical day/week boundaries (`WEEK_CUTOFF_HOUR`)
- [ ] B7  Session history

## C. Dashboard (web)

- [ ] C1  Today summary — total, status, active session
- [ ] C2  Live status polling
- [ ] C3  Weekly summary view
- [ ] C4  Screenshot gallery by date
- [ ] C5  Session history page
- [ ] C6  Start/stop controls

## D. Daily activity log

- [ ] D1  Auto-draft from window titles (`summarize.py` — no model, no network)
- [ ] D2  Categorisation: Coding / Web / App / Search / Email / Research / Shell
- [ ] D3  Evening prompt window (21:00–24:00), gated on actual presence
- [ ] D4  Pending queue — a missed day stays queued indefinitely
- [ ] D5  Answers: confirm / skip / leave-as-is
- [ ] D6  Top-up when an answered day gains more undescribed time
- [ ] D7  Activity-log history

## E. Reporting & email

- [ ] E1  Weekly report, Mon→Sun, sent Monday 17:00
- [ ] E2  Week-over-week absolute delta
- [ ] E3  Daily bar chart (PNG, inline `cid:`)
- [ ] E4  Category donut
- [ ] E5  Activity list with private/research label filtering
- [ ] E6  Project rows
- [ ] E7  Monthly report, last day of month 21:00
- [ ] E8  Month-over-month **percentage** change
- [ ] E9  Work-stream donut (ordered patterns + catch-all)
- [ ] E10 Week-by-week bars
- [ ] E11 Coverage caption — described time vs tracked time
- [ ] E12 Days-described line
- [ ] E13 Email previews for both reports
- [ ] E14 Manual send endpoints (recipient override for samples)
- [ ] E15 Send-once guards — **per user, in the DB, not a JSON file**
- [ ] E16 One-off catch-up window for schedule changes
- [ ] E17 **NEW** "No history is not a missed send" guard on every trigger

## F. Desktop widget

- [ ] F1  Ring gauge, day + week goals
- [ ] F2  Overdrive state past 100%
- [ ] F3  Docked top-centre positioning
- [ ] F4  Greeting toast on session start
- [ ] F5  Focus cheers every 25 min of non-idle tracking
- [ ] F6  Tray menu — show/hide, dock, dashboard, previews, quit
- [ ] F7  Daily-prompt card
- [ ] F8  Offline detection
- [ ] F9  **NEW** Points at a configurable server URL, carries the agent token

## G. Multi-user platform — all NEW

- [ ] G1  User accounts + login
- [ ] G2  Password reset
- [ ] G3  Roles: worker vs admin
- [ ] G4  Per-device agent tokens
- [ ] G5  `user_id` on every table; every query filtered
- [ ] G6  Admin: team overview
- [ ] G7  Admin: per-person drill-down
- [ ] G8  Per-user settings — timezone, goals, idle threshold, prompt hours,
          streams, private labels, send times
- [ ] G9  Per-user email delivery (no shared Gmail app password)
- [ ] G10 Consent record + a pause control the tracked user holds

## H. Infrastructure — all NEW

- [ ] H1  Postgres + Alembic migrations
- [ ] H2  Gunicorn + Nginx + TLS + domain
- [ ] H3  Background worker OUT of the web process (4 workers = 4 emails)
- [ ] H4  Timezone-aware datetimes throughout
- [ ] H5  S3 screenshot storage, private bucket, presigned URLs
- [ ] H6  S3 lifecycle — full-res 30 days, thumbnails 1 year
- [ ] H7  Secrets via environment, never on disk
- [ ] H8  Docker Compose deploy
- [ ] H9  Backups + a *tested* restore
- [ ] H10 Health checks + log aggregation
- [ ] H11 Rate limiting on login and ingest
