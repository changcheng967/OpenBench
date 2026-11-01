# Handoff: host OpenBench for Luminex on Oracle Cloud + attach workers

**Audience:** an agent (or a person) with shell access to the Oracle account, the
GitHub account `changcheng967`, and the worker machines. No prior context needed.

**Read this file top to bottom before touching anything.** Sections 1–2 are
blockers: if you skip them, the server will come up, workers will connect, tests
will run, and every result will be wrong with no error message anywhere.

---

## 0. State of play

| item | state | where |
|---|---|---|
| OpenBench fork, server-side patches (DEBUG=0, whitenoise, `SECRET_KEY` from env) | **done** | `OpenSite/settings.py`, `requirements-prod.txt` |
| Client patch: inject net path per workload | **done**, corrected to `NNUEFile` | `Client/worker.py:519-536` |
| Client version bumped 53 → 54 so stale upstream clients are rejected | **done** | `Client/worker.py:64`, `Config/config.json:2` |
| `Config/config.json` → fork URL | **done**, verify the URL | `Config/config.json:3` |
| `deploy/bootstrap.sh` + systemd unit + runbook | **done** | `deploy/` |
| Engine `bench` prints a `nodes` total | **NOT DONE — blocker** | `src/main.cpp:124` |
| `Makefile` at Luminex repo root | **NOT DONE — blocker** | repo root |
| Fork pushed to GitHub | **NOT DONE — blocker** | `github.com/changcheng967/OpenBench` |
| Oracle instance | not created | — |
| Workers attached | none | — |

Working copies on the machine this was written on:

- fork: `C:\Users\chang\Downloads\openbench` (git remote still points at
  `AndyGrant/OpenBench`; the fork's own commits are uncommitted working-tree
  changes plus untracked `deploy/`, `luminex-integration/`, `requirements-prod.txt`)
- engine: `C:\Users\chang\Downloads\Luminex` (v6.3.0, ~3030 blitz)

---

## 0.1 Inputs the owner must hand over

The receiving agent cannot start without these. Everything else is derivable.

1. **Oracle Cloud tenancy**: console login (or an API key + tenancy/user OCID if
   you want the agent to script instance creation), and the home region.
2. **GitHub write access** to `changcheng967/OpenBench` and
   `changcheng967/Luminex` (a PAT with `repo` scope, or you push yourself).
3. **A domain or subdomain** you control for `bench.<domain>`, plus Cloudflare
   account access for the tunnel (the arena already uses one — reuse the account).
4. **Worker SSH access**: the 8-thread box, the DCU pod, the C500 pod, and any
   others. Include the cgroup CPU limit for each containerised one, because the
   client reports host cores, not the quota.
5. **The net file**: `luminex_gen768_i8.nnue` (75,624,236 bytes) and the opening
   book as EPD.
6. **The Luminex commit sha** you want as the current `base` — it must be pushed
   and must already contain the §1a/§1b changes.

---

## 1. Engine changes (3 items, all in the Luminex repo)

> **Constraint:** the Luminex repo is under a read-only policy for design work.
> These three changes are functional, not experimental, and are explicitly
> authorised here. Apply them as one commit, message style
> `Add OpenBench build and bench contract`, and push. Do not touch anything else
> in `src/`.

### 1a. `bench` must print a node count (blocker)

`Client/bench.py` parses the **last** line matching `nodes searched (\d+)` or
`(\d+ nodes)` after stripping non-alphanumerics, and expects it within 60 s.
Luminex's `bench` is a perft run that prints `Perft 4: 197281` — the word
"nodes" never appears, so the client raises
`OpenBenchBadBenchException: Failed to Execute Benchmark` and blacklists the
test on every worker.

Patch `src/main.cpp`, immediately before the `if (p1 == 20 && ...)` block at
line 128:

```cpp
        // OpenBench bench contract: emit a "nodes" total and an NPS figure.
        // The client parses /nodes searched (\d+)/ and /nps (\d+)/ from the
        // LAST matching lines, so these must come after the perft output.
        const uint64_t total_nodes = p1 + p2 + p3 + p4;
        std::cout << "Nodes searched " << total_nodes << std::endl;
        if (ms > 0)
            std::cout << "NPS " << (total_nodes * 1000ULL / (uint64_t)ms) << std::endl;
```

`total_nodes` is **208,603** for the start position. That number goes in
`Engine.bench`.

**Why perft and not a real search bench:** perft is pure integer arithmetic, so
the node count is identical on every worker regardless of ISA. A search bench
would produce different totals on AVX2 vs AVX-512 vs ARM builds, and OpenBench
treats a bench mismatch as a corrupt worker. Keep perft for the determinism
check.

**Consequence you must handle:** OpenBench uses the reported NPS for time-control
scaling. Perft NPS is several million; Luminex search NPS is ~0.6M/thread. So:

- `EngineConfig.nps` = your **measured single-thread search NPS** (≈610,000 at
  the time of writing — re-measure with `bench` on the target hardware, or take
  the number from a `go depth 18` run).
- `Test.scale_method` = `BASE` (a fixed value), never `NPS` from the bench.

A proper search bench (~40 lines: ~20 FENs, fixed depth, sum nodes) is worth
doing before SPSA — SPSA re-scales the time control every iteration — but it is
not needed to start.

### 1b. `Makefile` at the repo root (blocker)

The client always invokes `make`, never `cmake`. Copy
`openbench/luminex-integration/Makefile` to the Luminex repo root, next to
`CMakeLists.txt`, and commit it. It is a shim that calls cmake and copies the
binary to `./$(EXE)`.

It has already been made architecture-aware: `MARCH` is `x86-64-v3` on x86-64 and
`neoverse-n1` on aarch64. This matters because `CMakeLists.txt:70` routes
`-march=` through `try_add_flag()`, which **silently drops** flags the compiler
rejects — so `-march=x86-64-v3` on the ARM server does not fail, it just
produces an unoptimised binary with no warning.

Verify by hand on a worker before going further:

```bash
make -j EXE=luminex-test CXX=g++ && ls -l luminex-test
./luminex-test bench | tail -3        # must print "Nodes searched 208603"
```

Run that bench five times, and at `-j1` and `-j8`: the number must not move.

### 1c. The network (already solved client-side, but verify)

Luminex loads `.nnue` at runtime through the **`NNUEFile`** UCI option
(`src/uci.cpp:218`). The default value is `luminex_sf1_i8.nnue`, which does not
exist in the repo — so if the option is never set, `nnue::load()` fails, returns
false, and the engine evaluates with the HCE fallback. Silent, and every test
result is meaningless.

The fork's client injects it. Two things to check:

1. `Client/worker.py` builds `option.NNUEFile=<abs path into Networks/>`. It must
   say **`NNUEFile`**. An `EvalFile=` there hits the catch-all branch at
   `src/uci.cpp:435` and is ignored without error. (This was the bug in the
   first draft of these notes; it is fixed in the fork as of CLIENT_VERSION 54.)
2. `Test.dev_options` / `base_options` must stay **empty**. An explicit
   `NNUEFile=` in the options string suppresses injection.

Verify manually:

```bash
printf 'uci\nsetoption name NNUEFile value /abs/path/luminex_gen768_i8.nnue\nisready\nposition startpos\neval\nquit\n' | ./luminex-test
```

`eval` must print a real NNUE evaluation, not the HCE number.

**Optional, better long-term (not needed to start):** embed the net at build time
(`EVALFILE` → `objcopy`/`incbin` + a `load_from_memory()` path). That makes the
binary self-contained, removes this whole failure class from the lichess bot and
the arena too, and lets you run stock OpenBench clients instead of the fork.

---

## 2. Fork changes before it can be cloned by workers

Workers clone `client_repo_url` from the server's `Config`, so the fork must be
publicly reachable **before** any worker attaches.

```bash
cd /path/to/openbench
git checkout -b luminex
git add -A
git commit -m "Luminex: NNUEFile injection, prod server, deploy runbook"
git remote add mine https://github.com/changcheng967/OpenBench.git
git push -u mine luminex
```

Then confirm `Config/config.json`:

```json
"client_version"  : 54,
"client_repo_url" : "https://github.com/changcheng967/OpenBench",
"client_repo_ref" : "luminex",
```

`client_repo_ref` must be the branch you actually pushed. `client_version` must
equal `CLIENT_VERSION` in `Client/worker.py:64` — that pair is what stops a
worker running upstream v53 from silently producing net-less tests.

If the repo is private, workers need a token; public is much simpler and there is
nothing secret in it.

---

## 3. Provision the Oracle instance

Free-tier allowance changed in mid-2026: **1,500 OCPU-hours + 9,000 GB-hours per
month**, i.e. **2 OCPU / 12 GB running 24/7**. (It was 3,000/18,000 = 4 OCPU/24 GB
before the halving.) Do not provision 4 OCPU/24 GB on the assumption that it is
free — you will be billed for the excess.

| setting | value |
|---|---|
| shape | `VM.Standard.A1.Flex`, **2 OCPU / 12 GB** |
| image | Ubuntu 24.04 (aarch64) |
| boot volume | 50 GB is plenty; the free block-storage allowance is 200 GB total |
| public IP | **keep it** (you need SSH; the console-only alternative is painful) |
| ingress | open 22 **only from your IP**; leave 80/443 closed if using Cloudflare Tunnel |

Everything the server does is HTTP and SQLite. It never searches a position, so
2 cores are ample — CPU only matters on workers.

**Known Oracle obstacles, in order of likelihood:**

1. **"Out of capacity"** for A1 in your home region. Retry over hours, or create
   the instance in a different region (latency to workers does not affect
   correctness). Scripting retries against the API is the usual workaround.
2. **Idle reclamation** after ~7 days of no activity. A server with workers
   attached is never idle; if you are worried, a cron `curl` once an hour is
   enough.
3. Signup requires a card for identity verification. Always Free resources are
   not charged.

---

## 4. Deploy the server

From the fork root on the VM:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/changcheng967/OpenBench.git && cd OpenBench
git checkout luminex

export OPENBENCH_ADMIN_USER=luminex
export OPENBENCH_ADMIN_PASSWORD='<pick-something-long>'
sudo -E bash deploy/bootstrap.sh
```

The script is idempotent and does, in order: apt packages, `openbench` system
user, rsync into `/opt/openbench`, venv + `requirements-prod.txt`, generate
`.env` with a real `SECRET_KEY`, migrate, `collectstatic`, create the admin **via
`Scripts/create_user.py --enabled`**, install and start the systemd unit, then
health-check `127.0.0.1:8000`.

**Do not use `manage.py createsuperuser`.** OpenBench authenticates workers with
`authenticate(request, requireEnabled=True)`, which does
`Profile.objects.get(user=user)`. `createsuperuser` creates no `Profile`, so
every worker attach fails with `Bad Credentials` and nothing in the log explains
why. If you already made one that way, run `Scripts/create_user.py --enabled`
for a second account and use that.

Check:

```bash
systemctl status openbench
journalctl -u openbench -n 40 --no-pager
curl -fsS -o /dev/null -H 'Host: localhost' http://127.0.0.1:8000/ && echo OK
ss -ltnp | grep 8000     # if this is something else, gunicorn never bound
```

---

## 5. Ingress

Reuse Cloudflare Tunnel — no open ports, free TLS, and it survives the VM's IP
changing.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create openbench
cloudflared tunnel route dns openbench bench.<yourdomain>
cloudflared service install
```

Point the tunnel's ingress at `http://127.0.0.1:8000`, then in
`/opt/openbench/.env`:

```
OPENBENCH_ALLOWED_HOSTS=bench.<yourdomain>
OPENBENCH_CSRF_ORIGINS=https://bench.<yourdomain>
```

```bash
systemctl restart openbench
```

`CSRF_TRUSTED_ORIGINS` is mandatory behind HTTPS: without it the admin login
POST silently 403s and you will think the password is wrong.

---

## 6. Register content in `/admin/`

Log in, then create in this order (each depends on the previous).

### Engine

| field | value |
|---|---|
| name | `Luminex` |
| source | `https://github.com/changcheng967/Luminex/archive/<full-40-char-sha>.zip` |
| sha | the same full sha |
| bench | `208603` |

The URL must be a **GitHub archive zip**, not a git URL — the client unzips it.
The commit must be pushed and must contain both the `bench` patch and the
`Makefile`.

### EngineConfig

| field | value | why |
|---|---|---|
| name | `Luminex` | |
| source | same archive URL | |
| build_path | `.` | where the Makefile lives |
| build_compilers | `g++-13 g++-12 g++ clang++` | client scans in order, first match wins |
| build_cpuflags | `avx2 bmi2` | client checks the machine has them; **leave empty for the ARM worker's config** |
| build_systems | `Linux` | OS names, not ISA |
| nps | measured single-thread search NPS (≈610000) | see §1a |

Make a **second** EngineConfig (`Luminex-ARM`, same source, `build_cpuflags`
empty) if you intend to run the Oracle VM itself as a worker.

### Network

Upload `luminex_gen768_i8.nnue` — **75,624,236 bytes**. The client stores it once
per worker at `Networks/<sha256>` and reuses it. Confirm the sha256 the admin
shows matches:

```bash
sha256sum luminex_gen768_i8.nnue
```

### Book

Your 8-ply opening book as EPD. `book_index` + rounds-per-runner decides which
slice each worker plays, so two workers never play the same line.

### Test defaults

| field | value |
|---|---|
| test_mode | `SPRT` |
| elolower / eloupper | `0.0` / `5.0` |
| alpha / beta | `0.05` / `0.05` |
| scale_method | `BASE`, `scale_nps` = your number |
| win_adj / draw_adj | defaults are fine |
| dev_options / base_options | **empty** |

---

## 7. Attach workers

Workers are stateless: a worker that dies loses only its in-flight games, and the
server reassigns the work unit. Machines can appear and vanish.

```bash
# every worker
sudo apt-get install -y python3 python3-pip git build-essential cmake
git clone https://github.com/changcheng967/OpenBench.git && cd OpenBench
git checkout luminex
pip install -r requirements.txt -r Client/requirements.txt
python3 Client/client.py -U luminex -P '<password>' -S https://bench.<yourdomain> \
        -T <concurrent-games> -N 1
```

`-T` is **concurrent games**, not threads per engine. One game per core, no SMP:
SMP adds overhead and lazy-SMP makes results nondeterministic. `-N 1`.

`client.py` is the entry point, not `worker.py`. It parses `-U/-P/-S`, then
downloads `client_repo_url` at `client_repo_ref` as a zip and **overwrites every
file in the local `Client/` directory except `client.py`**, then hands the
remaining argv to `worker.py`, which is where `-T`/`-N` are parsed. Two
consequences:

- **Any patch you apply locally on a worker is wiped on the next start.** The
  `NNUEFile` fix must be committed and pushed to the branch in `Config`.
- If a worker fails with "Unable to retrieve .zip archive from Github", the
  branch name in `client_repo_ref` does not exist on the fork.

| machine | cores | `-T` | notes |
|---|---|---|---|
| the 8-thread box (2.49M nps peak) | 8 | `8` | |
| DCU pod (`openbenchtest1`) | **14** | `14` | the client reports `physical_cores=32` from `/proc/cpuinfo` and ignores `cpu.max = 1400000 100000`; `-T 32` oversubscribes 2.3× and produces fewer games, not more |
| Oracle VM itself | 2 | `1`–`2` | uses the ARM EngineConfig; build is slower and weaker per node, still free throughput |
| C500 pod | as reported | `nproc` | |

Run it under systemd or `tmux` so it survives logout. Log rotation: the client
prunes `Networks/` weekly on its own.

**Windows workers:** the client needs `make`, `g++` and `cmake`. Use WSL2
(Ubuntu), not native Windows.

**pip mirror trap:** on the Chinese-hosted pods pip defaults to
`pypi.tuna.tsinghua.edu.cn` and can time out. Prefix with
`PIP_INDEX_URL=https://pypi.org/simple`.

---

## 8. Smoke test ladder

Each step fails loudly and independently. Do not create a real test until all
five pass.

1. **Build by hand**, exactly as the client does:
   `make -j EXE=luminex-test CXX=g++ && ls -l luminex-test`
2. **Bench by hand:** `./luminex-test bench | tail -3` → `Nodes searched 208603`,
   stable across 5 runs and across `-j` values.
3. **Net loads:** §1c's `eval` check, with and without `NNUEFile`.
4. **One hand-run match** with fastchess, 20 games, to confirm the
   `option.NNUEFile=...` string form is accepted. This is the step that catches
   option-syntax mistakes and it costs two minutes.
5. **32-game test in the UI**, watch one worker claim it, confirm the games
   appear and the PGN shows a real net-driven game (not HCE noise).

---

## 9. Definition of done

- `https://bench.<yourdomain>/` serves the login page over HTTPS.
- `/admin/` accepts the admin login (not a 403).
- Engine, EngineConfig, Network, Book all registered and enabled.
- At least two workers show as **online** in the UI with matching bench values.
- A 32-game self-test (Luminex vs Luminex, same sha) completes and reports
  ≈50% with no errors.
- A 200-game `Luminex@newsha` vs `Luminex@oldsha` test runs to completion and
  the SPRT LLR moves in the expected direction.
- `journalctl -u openbench` is clean of tracebacks.

---

## 10. Failure modes

| symptom | cause | fix |
|---|---|---|
| worker: `Bad Credentials`, nothing else | user has no `Profile` | `Scripts/create_user.py --enabled`, not `createsuperuser` |
| worker: `OpenBenchBadBenchException` | `bench` prints no `nodes` | §1a patch, push, update `Engine.bench` |
| tests run but every game is nonsense / very fast | `EvalFile=` injected instead of `NNUEFile=` | §1c, check `Client/worker.py:536` |
| a worker's local patch keeps disappearing | `client.py` re-downloads `Client/` from `client_repo_ref` on every start | commit and push the patch; never patch on the worker |
| bench mismatch across workers | `-march=native` or mixed ISAs on a *search* bench | keep perft bench, fixed `MARCH` |
| ARM worker builds but is slow | `-march=x86-64-v3` silently dropped | §1b conditional `MARCH` |
| `Address already in use` on 8000 | something else bound 8000 | `ss -ltnp`, change `--bind` |
| admin login 403 | `OPENBENCH_CSRF_ORIGINS` unset | §5 |
| instance creation fails, "out of capacity" | A1 capacity | retry / other region |
| all workers idle, server up | `Config.client_version` ≠ `CLIENT_VERSION` | make them equal |
| time controls absurdly short | `scale_method=NPS` off a perft figure | `BASE` + manual nps |

---

## 11. Throughput and how it changes the program

At `-T 14`, one 14-core pod is worth roughly **1,750 games/hour**. A 2,000-game
gate becomes ~70 minutes instead of ~2 hours.

That matters because of SPRT: at `elo0=0 elo1=5`, clear results stop in a few
hundred games, so the combination — SPRT for allocation, more cores for
throughput — is what turns a 1.9M-game SPSA campaign from "79 days" into "a few
weeks". Set `priority` high on gate tests and let SPSA runs sit at default.

---

## 12. Do not

- Do not modify Luminex `src/` beyond §1a. Everything else in the engine is under
  a read-only policy for design work.
- Do not set `UCI_LimitStrength` in test options (default false; `UCI_Elo` maxes
  at 3190 and would silently cripple the engine).
- Do not let `Media/` grow unpruned — every test can archive PGNs. Move
  `OPENBENCH_MEDIA_ROOT` to the largest volume and delete old tests.
- Do not run `-T` = logical CPUs on a cgroup-limited pod.
- Do not trust a test whose workers reported different bench values.
