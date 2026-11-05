# Hosting OpenBench for Luminex

Short answer: **Oracle Cloud "Always Free" Ampere A1 VM.** It is the only free
tier that is simultaneously always-on, has a real disk, and does not meter
egress tightly enough to matter.

---

## What the server actually needs

Almost nothing. It serves a job queue, small JSON API calls, engine source zips
and `.nnue` files. **All chess computation happens on the worker clients**, so
the server never searches a position.

| resource | requirement | why |
|---|---|---|
| RAM | ~200 MB | Django + SQLite |
| CPU | < 5% of one core | a dozen HTTP requests a minute |
| disk | 20–50 GB | code, SQLite, Media/ (nets + PGNs) |
| **uptime** | **24/7** | a worker that cannot reach the server is idle |
| **egress** | **tens of GB/month** | each worker pulls the engine zip + a 72 MB net |

So the selection criteria are **uptime** and **egress**, not compute. That
inverts the usual free-tier ranking.

---

## The options, honestly

| option | free allowance | verdict |
|---|---|---|
| **Oracle Always Free (Ampere A1)** | 1,500 OCPU-hr + 9,000 GB-hr/month ⇒ **~2 OCPU + 12 GB running 24/7**; 200 GB block storage; **10 TB/month egress**; no end date | **recommended** |
| Google Cloud e2-micro | 1 free VM (us-west1 / us-central1 / us-east1), 30 GB disk, free forever | **1 GB/month egress** — one 72 MB net × 27 downloads exhausts it |
| Render free web service | free instance | **spins down after ~15 min idle** — fatal for a job server |
| Fly.io / Railway | trial credit | credit expires; not free at 24/7 |
| Cloudflare Workers | generous free tier | no long-running process; OpenBench is Django + SQLite |
| Hetzner CX22 | €3.79/month | not free, but removes both Oracle caveats — the fallback |

### Oracle caveats you should know before signing up

1. **ARM capacity is often unavailable** in popular home regions ("out of
   capacity" at instance creation). Retry, or pick a less busy region — the
   server's location only affects latency to workers, not correctness.
2. **Idle reclamation.** Oracle may reclaim Always Free compute that looks idle
   for ~7 days. A quiet Django app qualifies. In practice a server with workers
   attached is never idle; if you are worried, a cron that touches the app once
   an hour is enough.
3. Signup requires a card for identity verification. Always Free resources are
   not charged, but a card must be on file.

---

## Deployment

On a fresh Ubuntu 22.04/24.04 instance (ARM or x86 both work):

```bash
# as root, from the root of this repository
export OPENBENCH_ADMIN_USER=you
export OPENBENCH_ADMIN_PASSWORD='something-long'
sudo -E bash deploy/bootstrap.sh
```

That installs packages, creates the `openbench` service user, builds a venv,
generates `.env` with a real `SECRET_KEY`, migrates, collects static, installs
and starts the systemd unit, and health-checks `127.0.0.1:8000`.

Two things it does deliberately:

- **`OPENBENCH_DEBUG=0`.** Upstream ships `DEBUG=True` and serves static files
  through `staticfiles_urlpatterns()`, which returns an empty list the moment
  DEBUG is off. This fork adds `STATIC_ROOT`, `collectstatic`, and whitenoise, so
  a single gunicorn process serves everything and no nginx is needed.
- **`Scripts/create_user.py --enabled`**, not `createsuperuser`. See the trap
  below.

---

## Ingress

The live server sits behind a **named Cloudflare Tunnel**. The tunnel needs no
open ports, Cloudflare supplies TLS, and the hostname never changes.

- `https://bench.luminexchess.com` reaches gunicorn on `http://127.0.0.1:8000`
  through the tunnel `luminex-openbench`.
- DNS is a proxied CNAME `bench` → `<tunnel-id>.cfargotunnel.com`.
  `cloudflared tunnel route dns` creates it.
- The apex `luminexchess.com` is the engine's homepage and is hosted elsewhere;
  this tunnel only answers for `bench`.

Setup on the VM (x86_64 here; use the arm64 build of cloudflared on Ampere):

```bash
cloudflared tunnel login                      # prints a link: authorize the zone in a browser
cloudflared tunnel create luminex-openbench   # writes ~/.cloudflared/<tunnel-id>.json
cloudflared tunnel route dns luminex-openbench bench.luminexchess.com
sudo install -d -m 755 /etc/cloudflared
sudo install -m 600 ~/.cloudflared/<tunnel-id>.json /etc/cloudflared/
sudo install -m 644 deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo install -m 644 deploy/cloudflared-openbench.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared-openbench
```

Edit the tunnel id in `config.yml` if you create a new tunnel. The unit replaces
`cloudflared service install`, which would also turn on cloudflared's
self-updater. Then set, in `/opt/openbench/.env`:

```
OPENBENCH_ALLOWED_HOSTS=bench.luminexchess.com,.trycloudflare.com,localhost,127.0.0.1
OPENBENCH_CSRF_ORIGINS=https://bench.luminexchess.com,<current quick-tunnel URL>
```

`CSRF_TRUSTED_ORIGINS` is required for POST forms (login, test creation) behind
HTTPS; without it the admin login silently 403s.

**The quick-tunnel fallback.** The earlier trycloudflare quick tunnel still runs,
as `openbench-tunnel.service` with `deploy/openbench-tunnel-keeper` installed in
`/usr/local/bin`.
- Its URL changes on every restart. The keeper writes the new URL to
  `/home/ubuntu/current_bench_url`, rewrites the CSRF origins (permanent hostname
  first) and restarts OpenBench.
- Why it stays: some school and corporate networks reset TLS to domains
  registered in the last ~30 days. On 2026-11-05, the day the domain was
  registered, one network reset every connection to `luminexchess.com` while the
  same address worked from the server.
- Once no network you use blocks the domain, run
  `sudo systemctl disable --now openbench-tunnel` and drop the trycloudflare
  entries from `.env`.

If you would rather not use Cloudflare, Oracle gives you a public IP — open
80/443 in the VCN security list and put Caddy in front of gunicorn.

---

## Attaching workers

Workers are **stateless and disposable**. A worker that dies mid-workload loses
only its in-flight games; OpenBench reassigns the work unit. That is exactly the
property you wanted: machines can appear and vanish.

```bash
# on each worker
git clone https://github.com/<you>/OpenBench   # the fork, for the NNUEFile patch
cd OpenBench
pip install -r requirements.txt -r Client/requirements.txt
python3 Client/client.py -U <user> -P <pass> -S https://bench.example.com \
        -T <threads> -N <sockets>
```

`-T` is **concurrent games**, not threads per engine. For maximum throughput run
one thread per game (`-T` = number of cores), because SMP search adds overhead
and lazy-SMP makes results nondeterministic.

### The pod you just tested

`openbenchtest1` reports `physical_cores=32` because the client reads
`/proc/cpuinfo` via `py-cpuinfo` — it does **not** read the cgroup quota. The
real limit there is 14 (`cpu.max = 1400000 100000`). So:

```bash
-T 14 -N 1
```

Running `-T 32` on that pod oversubscribes 2.3× and every game gets slower
without producing more of them.

---

## Capacity math

At `-T 14` and roughly 1,000 games/hour/8 cores on the mscope box, one 14-core
pod is worth about **1,750 games/hour**. A 2,000-game gate is then ~70 minutes
instead of ~2 hours.

That matters because of the SPRT interaction: with SPRT bounds at
`elo0=0 elo1=5`, clear results stop in a few hundred games. The combination —
SPRT for allocation, more cores for throughput — is what turns a 1.9M-game SPSA
campaign from "79 days" into "a few weeks".

---

## Gotchas found the hard way

1. **`createsuperuser` produces a user that cannot attach workers.** OpenBench
   calls `authenticate(request, requireEnabled=True)`, which does
   `Profile.objects.get(user=user)`. `createsuperuser` creates no Profile, so
   every worker attach fails with `Bad Credentials` and no other clue. Always use
   `Scripts/create_user.py --enabled`.
2. **Port 8000 may be taken.** The DCU pod's own sshd listens on `0.0.0.0:8000`.
   Check with `ss -ltnp` before assuming gunicorn failed to start; the symptom is
   an HTTP `000` and `Address already in use` in the log.
3. **pip may be pinned to a regional mirror.** On the pod, pip defaulted to
   `pypi.tuna.tsinghua.edu.cn` and timed out. Force the official index:
   `PIP_INDEX_URL=https://pypi.org/simple`.
4. **The upstream `SECRET_KEY` is public.** It is committed in the repo, so
   anyone can forge session cookies against a deployment that keeps it. This fork
   reads `OPENBENCH_SECRET_KEY` from the environment and warns if it is unset.
5. **`Media/` grows without bound.** Every test can archive PGNs. Put
   `OPENBENCH_MEDIA_ROOT` on the largest volume you have, and prune old tests.
