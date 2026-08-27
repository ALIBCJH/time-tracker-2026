# Deploying

The stack is Docker Compose on a single instance: Postgres, the web process,
the background worker, and Caddy for TLS. One `t4g.small` runs it comfortably
for three people.

A release is: the Deploy action rsyncs the commit to the instance, rebuilds,
and refuses to finish until the app answers. Migrations run once, in the web
container's entrypoint, before it serves.

**The workflow does not create infrastructure.** The instance, the DNS record
and the bucket are made once, by hand. Everything after that is the action.

---

## 1. What has to exist in AWS

| | | Why |
|---|---|---|
| **EC2 instance** | Ubuntu 24.04, `t4g.small`, 20 GB | Runs the whole stack. Arm is cheaper and the images are multi-arch. |
| **Elastic IP** | attached to it | Without one the address changes on stop/start and the DNS record silently rots. |
| **Security group** | 80 and 443 from anywhere; 22 only from where you deploy | Caddy needs 80 to answer the ACME challenge, not only 443. |
| **DNS A record** | your domain → the Elastic IP | Must resolve *before* the first deploy, or the certificate request fails and the site stays down. |
| **S3 bucket** | private, block all public access | Screen captures. Leave `S3_BUCKET` blank to keep them on the instance disk instead. |
| **IAM user** | `s3:GetObject`, `PutObject`, `DeleteObject` on that bucket only | Its keys go in the instance's `.env`, never in GitHub. |
| **Lifecycle rule** | full frames 30 days, thumbnails 365 | The retention the consent page promises. |

## 2. Prepare the instance

SSH in as `ubuntu` and run:

```bash
git clone git@github.com:BensonMunene/Time-tracker.git /tmp/tt && \
  DEPLOY_PATH=/opt/timetracker /tmp/tt/deploy/bootstrap.sh
```

It installs Docker, creates `/opt/timetracker`, writes a `.env` with a
generated database password and secret key, and schedules the nightly backup.

Then **edit `/opt/timetracker/.env`** — set `DOMAIN`, the SMTP settings and the
S3 bucket and keys.

> That file is the production configuration and is deliberately not in the
> repository. The deploy workflow excludes it from every sync, so your edits
> survive releases and no secret is ever in CI logs.

Log out and back in once, so the `docker` group membership applies.

## 3. Tell GitHub how to reach it

**Settings → Secrets and variables → Actions**

| Secret | What it is |
|---|---|
| `SSH_HOST` | The Elastic IP, or the hostname |
| `SSH_USER` | `ubuntu` |
| `SSH_PRIVATE_KEY` | A private key whose public half is in the instance's `~/.ssh/authorized_keys`. Generate a fresh one for this — do not reuse a personal key. |
| `SSH_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519 <the IP>`, run once from somewhere you trust |
| `DOMAIN` | The domain, for the final end-to-end check |

Optional variable: `DEPLOY_PATH`, if not `/opt/timetracker`.

`SSH_KNOWN_HOSTS` is pinned rather than scanned at deploy time on purpose.
Scanning trusts whatever answers on the day, which is the check being skipped.

## 4. Deploy

**Actions → Deploy → Run workflow.**

It runs the full test suite on that exact commit first, and stops if anything
fails. Then it syncs, rebuilds, waits for `/readyz` inside the container, and
finally checks `https://<domain>/healthz` from outside — DNS, certificate and
app, end to end.

Pushing to `main` deploys automatically. To require a human first, add required
reviewers to the `production` environment in repository settings; the workflow
already targets it.

## 5. Rolling back

Run the Deploy action again and put an earlier commit SHA in **ref**. The
images are rebuilt from that commit, so it is a real rollback of code.

**Migrations are not rolled back.** Alembic has a `downgrade` for every
migration here and each has been round-tripped, but the action will not run one
for you — reversing a schema change is a decision, not a step. If the bad
release included a migration, roll the code back first, then decide about the
schema with the data in front of you.

## 6. When something is wrong

```bash
cd /opt/timetracker
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 web
docker compose -f docker-compose.prod.yml logs --tail=100 worker
```

- **The site does not answer at all** — check the `proxy` container. Caddy
  cannot get a certificate if port 80 is closed or DNS does not yet point here.
- **`/readyz` fails but `/healthz` passes** — the app is running and the
  database is not. Look at `db` and at the migration output in `web`'s logs.
- **No reports arriving** — SMTP is unset or wrong. The worker logs
  `Reports skipped` once per tick rather than retrying.
- **An agent stopped reporting** — `docker compose exec web flask --app app
  check-agents --dry-run` shows what would be alerted on.

## 7. Creating the first accounts

There is no public sign-up, by design.

```bash
cd /opt/timetracker
docker compose -f docker-compose.prod.yml exec web flask --app app create-user
docker compose -f docker-compose.prod.yml exec web flask --app app issue-token
```

`create-user` prompts for email, name, role and password. `issue-token` prints
a device token for one machine — that is what the agent authenticates with, and
it is shown once.
