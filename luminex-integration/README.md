# Getting Luminex onto OpenBench

Everything OpenBench needs from an engine, what Luminex is missing, and the exact
patches. Two of the three gaps are in the engine repo; one is already fixed in
this fork's client.

---

## The contract OpenBench enforces

Reading `Client/utils.py` and `Client/bench.py`, a client does exactly this:

1. **Fetch** `Engine.source` as a **zip** and unzip it. That means the URL must be
   a GitHub archive link, not a git URL:
   `https://github.com/changcheng967/Luminex/archive/<sha>.zip`
2. **Build** by running, with cwd = `<repo>/<build_path>`:
   ```
   make -j EXE=<binary-name> [CC=<cc> | CXX=<cxx>] [EVALFILE=<abs net path>]
   ```
   and then expects the binary at `<repo>/<build_path>/<binary-name>`.
3. **Bench** by running `./<binary-name> bench` and parsing stdout+stderr for:
   - nodes: `(\d+\s+nodes) | (nodes\s+\d+) | (nodes searched\s+\d+)`
   - nps:   `(\d+\s+nps)   | (nps\s+\d+)   | (nodes second\s+\d+)`
   after stripping every non-alphanumeric character from each line.
   It scans lines **in reverse**, so the last matching line wins.
   Requirements: deterministic across threads, matches `Engine.bench`, finishes
   within **60 seconds**.

Note what `makefile_command()` does **not** pass: no target name, no CPU flags,
no OS name. The ISA decision is entirely inside your build script.

---

## Gap 1 — `bench` prints the wrong thing (blocker)

`src/main.cpp:104` implements `bench` as a **perft** run and prints:

```
Perft 4: 197281 (expected 197281)
Time: 52ms
ALL PERFT TESTS PASSED
```

After the parser's non-alphanumeric strip that becomes `Perft 4 197281 expected
197281` — **the word "nodes" never appears**, so `bench` parses as `None` and the
client raises `OpenBenchBadBenchException: Failed to Execute Benchmark`. The
worker blacklists the test and moves on.

### Minimum patch (2 lines, keeps perft)

In `src/main.cpp`, immediately before the `if (p1 == 20 && ...)` block:

```cpp
        // OpenBench bench contract: emit a "nodes" total and an NPS figure.
        // The client parses /nodes searched (\d+)/ and /nps (\d+)/ from the
        // LAST matching lines, so these must come after the perft output.
        const uint64_t total_nodes = p1 + p2 + p3 + p4;
        std::cout << "Nodes searched " << total_nodes << std::endl;
        if (ms > 0)
            std::cout << "NPS " << (total_nodes * 1000ULL / (uint64_t)ms) << std::endl;
```

`total_nodes` = 208,603 for the start position. That is the value to put in
`Engine.bench`.

### Caveat you must not ignore

Perft NPS is **not** search NPS. OpenBench uses the reported NPS for time-control
scaling (`scale_method`, `scale_nps`) and for throughput balancing. Perft runs at
several million nodes/sec; Luminex search runs at ~0.6M/thread. If you let
OpenBench scale on perft-NPS, every test's time control will be off by roughly
the ratio, and fixed-node tests will be mis-scaled.

Until you write a real search bench, **set `EngineConfig.nps` manually** to your
measured single-thread search NPS and use `scale_method = BASE` with that fixed
value. The perft bench is then used only for what it is actually good at:
proving every worker built an identical binary.

### Proper fix (later, ~40 lines)

A real bench: iterate a fixed list of ~20 FENs, run the search to a fixed depth,
sum `nodes`, print the total. That gives a search-NPS figure and doubles as a
regression test. Worth doing before you start SPSA, because SPSA scales the time
control by NPS on every iteration.

---

## Gap 2 — no `Makefile` (blocker)

Luminex builds with CMake only, and OpenBench always invokes `make`. Copy the
`Makefile` from this directory to the **root of the Luminex repo** (next to
`CMakeLists.txt`), then set `build_path = .`.

The shim accepts `EXE`, `CC`, `CXX`, and ignores `EVALFILE`. It pins
`LUMINEX_MARCH=x86-64-v3` rather than `native`, because `native` lets different
workers build different ISA levels and the bench determinism check can then fail
on heterogeneous hardware.

---

## Gap 3 — the network (fixed in this fork)

Upstream assumes every engine bakes its net into the binary at build time via
`EVALFILE=`, so the net never appears in the UCI option string. Luminex loads
`.nnue` at runtime through the `NNUEFile` option (`src/uci.cpp:218`), so a stock
client would start the engine with **no net loaded** and every evaluation would
be garbage.

Fixed in `Client/worker.py`, inside `engine_settings()`: when the workload
declares a network and the options string does not already contain `NNUEFile=`,
the client appends an absolute path to the downloaded net.

Consequence: **every worker must run this fork's client, not upstream's.** Point
`Config/config.json` at your fork:

```json
"client_repo_url" : "https://github.com/<you>/OpenBench",
"client_repo_ref" : "master"
```

The alternative — and the better long-term answer — is to teach Luminex to embed
the net at build time (an `EVALFILE` → `incbin`/`objcopy` step plus a
`nnue::load_from_memory()`), which makes the binary self-contained and lets you
run stock OpenBench. Worth doing when you have a spare afternoon, because a
self-contained binary also removes the "net file not found" failure class from
the lichess bot and the arena.

---

## Registration values

Fill these into the Django admin at `/admin/`.

### Engine

| field | value |
|---|---|
| name | `Luminex` |
| source | `https://github.com/changcheng967/Luminex/archive/<full-sha>.zip` |
| sha | the same commit sha |
| bench | `208603` (or whatever your patched `bench` prints) |

### EngineConfig

| field | value | why |
|---|---|---|
| name | `Luminex` | |
| source | same archive URL | |
| build_path | `.` | where the Makefile lives |
| build_compilers | `g++-13 g++-12 g++ clang++` | the client scans for these, first match wins |
| build_cpuflags | `avx2 bmi2` | the client checks the machine has them |
| build_systems | `Linux` | **OS names, not ISA** — `views.py:791` compares `machine.info['os_name']` |

### Network

Upload the `.nnue` (72 MB — 75,624,236 bytes). The client downloads it once per worker into
`Networks/<sha256>` and reuses it.

### Book

Your 8-ply opening book as EPD. OpenBench handles `EPD` and `PGN`; `book_index`
plus `rounds-per-runner` decides which slice each worker gets, so two workers
never play the same opening.

### Test options

Leave `dev_options` / `base_options` **empty** — the client injects `NNUEFile`
itself. If you ever need to override it, an explicit `NNUEFile=` in the options
string suppresses the injection.

---

## Smoke test, in order

Run these before creating a real test; each one fails loudly and independently.

1. **Build by hand on a worker**, exactly as the client would:
   ```
   make -j EXE=luminex-test CXX=g++ && ls -l luminex-test
   ```
2. **Bench by hand:**
   ```
   ./luminex-test bench | tail -3
   ```
   Confirm you see `Nodes searched <N>` and that `<N>` is stable across 5 runs
   and across `-j` values.
3. **Net loads:**
   ```
   printf 'uci\nisready\nposition startpos\neval\nquit\n' | ./luminex-test
   ```
   With no `NNUEFile` set you should see it fall back; with
   `setoption name NNUEFile value /abs/path.nnue` you should see a real eval.
4. **One hand-run match** with fastchess (what OpenBench actually drives), 20
   games, to confirm the `option.NNUEFile=...` string form is accepted.
5. **Then** create a 32-game test in the UI and watch one worker claim it.

Step 4 is the one that catches option-syntax mistakes, and it costs two minutes.
