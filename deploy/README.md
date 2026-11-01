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

You already run a Cloudflare Tunnel for the arena, so reuse it — it needs no open
ports, no public IP, and gives you TLS for free.

```bash
# on the VM
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create openbench
cloudflared tunnel route dns openbench bench.example.com
cloudflared service install
```

Then point the tunnel at `http://127.0.0.1:8000` and set:

```
OPENBENCH_ALLOWED_HOSTS=bench.example.com
OPENBENCH_CSRF_ORIGINS=https://bench.example.com
```

`CSRF_TRUSTED_ORIGINS` is required for POST forms (login, test creation) behind
HTTPS; without it the admin login silently 403s.

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
