# Gauntlets

A gauntlet plays one build of an engine against a list of other engines whose
ratings are published, and fits one rating to all of the games. SPRTs and
fixed-games tests say how one build compares with another build of the same
engine. A gauntlet says where the build stands on a rating list.

Everything here is specific to this fork. The code is in
`OpenBench/gauntlets.py`, with its tests in `OpenBench/tests.py`.

## How it runs

A gauntlet is a group of ordinary fixed-games tests, one per opponent, tied
together by a `Gauntlet` row and one `GauntletOpponent` row per test. Workers
are handed those tests, build both engines, play and report, exactly as for
any cross-engine test. Nothing in the client knows about gauntlets, so a
gauntlet needs no client version.

Three things differ from a hand-made set of tests:

- Every test scales its time control by the dev engine (`scale_method` DEV).
  Scaling by the base engine would give every opponent its own time control,
  since each one has its own bench speed.
- The lists of tests (index, user page) show one row per gauntlet. The row
  is Pending while any of its tests awaits approval, Active while any is being
  played, and Finished after that. Greens leaves the opponents out.
- Stop, Restart, Delete, Restore, Approve and Modify on the gauntlet's page
  apply to every test of the gauntlet. Each test still has its own page, with
  its PGNs, its errors and its results per machine.

## Creating one

On the site: Create Gauntlet in the sidebar (`/gauntlet/new/`). The form
takes the dev engine, its branch or commit, bench and network, one time
control, the games per opponent, and the opponents to play (all of them are
ticked). It starts from the settings of the engine's latest test. "Repeat For
Another Build" on a gauntlet's page opens the form with that gauntlet's
settings and opponents.

From a script, the same fields go to `/scripts/` with `action=CREATE_GAUNTLET`,
`username` and `password`. `opponents` is repeated once per opponent:

    dev_engine=Luminex  dev_repo=https://github.com/changcheng967/Luminex
    dev_branch=<commit> dev_bench=206603  dev_network=<sha of the network>
    dev_options="Threads=1 Hash=16"  dev_time_control=10+0.1
    opponents=Stash  opponents=Midnight  ...
    base_options="Threads=1 Hash=16"  games=400  rating_list="CCRL Blitz"
    book_name=2moves_v1.epd  upload_pgns=COMPACT  priority=0  throughput=1000
    workload_size=8  syzygy_wdl=OPTIONAL  syzygy_adj=DISABLED
    win_adj=None  draw_adj=None  scale_nps=1250000  info="..."

From the Django shell, `OpenBench.gauntlets.create_new_gauntlet(request)`
takes the same fields in a `RequestFactory` POST and returns
`(gauntlet, errors)`.

A test stops when its games reach the number asked for, so a few more than
that are played: the pairs that finished before the workers heard of it.

## Opponents

An engine is an opponent once it has ratings. `EngineConfig` carries, next to
the build settings:

| field | meaning |
| --- | --- |
| `rated_branch` | the tag, branch or commit that the ratings belong to |
| `rated_sha` | the commit it resolved to; looked up on Github when first played, then kept |
| `rated_bench` | the bench of that build; read from the commit message if left at 0 |
| `ratings` | `{ "CCRL Blitz" : 3417, "CCRL 40/15" : 3373 }`, one rating per list |

They can be edited under Manage, Engines, and none of them is part of the
build settings that workers cache, so editing them restarts no worker. Adding
or deleting an engine does change those settings: every worker then restarts
itself and drops the workload it was on. Do that between tests.

An opponent has to be buildable by the client as it is:

- `make -j EXE=<name> CXX=<compiler>` (or `CC=` for a C engine), run in the
  build path, must build the engine as its default target and leave the binary
  there under that name. The workers have gcc and g++ 14 and make, nothing
  else: no clang, no Rust, no Go.
- The network must come with the build: committed in the repository, or
  downloaded by the Makefile. The source is Github's archive of a commit, so
  git submodules and LFS files are not in it, and `git` commands fail.
- `./engine bench` must print a node count and an nps figure, the same count
  on every run, within 60 seconds while every core runs a copy.

A source that is itself a `.zip`, such as a set of prebuilt binaries with a
Makefile that copies one, is used as it is, with no lookup on Github.

The opponents are kept as files in `luminex-integration/opponents/`, one
`<Name>.json` each, in the format of `import_engines` with a `rated` object:

    python3 manage.py import_engines luminex-integration/opponents/

### The opponents in this repository

Thirteen engines, each at the exact version the lists rate. Every one was built
and benched on a Xeon 8269CY with gcc 16.2 through the client's own build and
bench functions, and its bench equals the one its author gives for the tag.
The workers have gcc 14.2, which was not available for the check.

| engine | tag | bench | CCRL Blitz | CCRL 40/15 | CCI STC / LTC / VLTC |
| --- | --- | ---: | ---: | ---: | --- |
| Stash | v37.0 | 4646465 | 3417 | 3373 | |
| Smallbrain | 7.0.0 | 3734449 | 3454 | 3382 | |
| Midnight | v9 | 7758763 | 3477 | 3392 | |
| Rice | 8.0.0 | 6151992 | 3530 | 3432 | |
| Elixir | v3.0 | 3614368 | 3567 | 3496 | |
| Altair | v7.0.0 | 8687679 | 3579 | 3474 | 3170 / 3432 / 3478 |
| Renegade | v1.3.0 | 5112160 | 3694 | | |
| Devre | v7.0 | 1209674 | 3704 | 3583 | |
| Quanticade | Cronus-3.0 | 3187919 | 3716 | 3588 | 3332 / 3645 / 3695 |
| Clover | v9.1 | 5182021 | 3726 | 3585 | 3453 / 3663 / 3706 |
| Berserk | 14 | 2811728 | 3737 | 3601 | 3465 / 3688 / 3721 |
| Obsidian | v16.0 | 3125283 | 3746 | 3605 | 3492 / 3707 / 3737 |
| PlentyChess | b-v8.0.0 | 3939470 | 3773 | 3617 | 3509 / 3726 / 3746 |

The ratings are the single-CPU entries of the lists as computed on 2026-11-07
(CCRL) and generated on 2026-11-12 (CCI, hardware class E1). The front pages of
CCRL show the 4-CPU or 8-CPU entry of many engines, which is not the one to
anchor a one-thread game on.

Also built and benched in the same way, and usable by adding a file: Willow 4.0
(3531; its bench takes 20 seconds), Patricia 5 (3539; plays for attacks by
design, so its results depend on the opponent more than most), Astra 7.0
(3730), Caissa 2.0 (3748), Integral v8 (3751).

Tried, and not usable as they are:

| engine | why |
| --- | --- |
| Sirius 9.0, Stormphrax 8.0.0 | need clang and lld |
| Koivisto 9.0 | its network is a git submodule, which a source archive lacks |
| Seer 2.8.0, RubiChess 20240817 | the network has to be handed to the build as EVALFILE |
| Ethereal 14.25 | no tag, and its network is not public |
| Tucano 12.00 | the network is a separate file read at run time; no usable `bench` |
| Halogen 16 | uses AVX-512 VBMI2, which the test machine lacks; untested, may build on the workers |
| BlackCore 6.0, Raphael 4.2.0 | do not compile with gcc 16 (a standard header is not included) |
| Molybdenum 4.1 | its statically linked binary aborted in `bench` on the test machine |

Between 3580 and 3690 on CCRL Blitz nothing usable was found, as the engines
there (Koivisto, Seer, RubiChess, Ethereal) all need their network handed in.
The client can do that for an engine whose network is registered on the server,
which would be the way to add them.

## The rating

Each opponent has a published rating, the anchor. The fit takes a game against
an opponent to score, on average,

    E = 1 / (1 + 10 ^ ((anchor - R) / (400 * scale)))

with a draw counted as half a win and half a loss, and finds the two numbers
that make the games most likely.

- **Rating.** R sits where the points expected against all of the opponents
  equal the points scored.
- **Scale.** How many points of the list one Elo point of ours is worth. It is
  not 1. A list is measured at its own time control and hardware, where the
  gaps between engines are narrower or wider than in our games (CCRL Blitz
  spreads the same engines 1.3 to 1.5 times as far apart as CCRL 40/15 does),
  and some lists stretch their ratings: CCI multiplies its BayesElo ratings
  by 1.35. The games show the scale as the slope of our scores against the
  anchors. With one opponent, or opponents of one rating, they cannot, and a
  weak prior then holds the scale at the list's nominal one, which is 1 unless
  `rating_list_scales` in `Config/config.json` says otherwise. The fit is then
  the plain one: the anchor plus the Elo difference.
- **Error.** The 95% errors come from the measured variance of the points,
  taking a pair of games on one opening as a single observation, so draws and
  the pairing of games both narrow them. They only cover the luck of the games.
  The error of the rating grows by itself when the rating lies outside of the
  opponents' ratings, as it then leans on the scale.
- **Spread.** The anchors are estimates themselves. So the opponents may
  disagree about R by more than their games explain. The spread is that extra
  disagreement: how far off each anchor has to be, as a standard deviation in
  rating points, to account for the points that the fit misses. "Error with
  spread" includes it, and is the one to quote when the spread is not small.
- **Per opponent.** Implied is the anchor plus the Elo difference of the games
  against that opponent alone, times the scale. Expected is the score that the
  fit predicts against it. An opponent whose score is far from the expected
  one is one whose anchor does not hold at our conditions.
- **Lists.** The page gives a rating on every list that rates at least one
  opponent, and the table follows the list that is clicked. The lists are on
  different scales, and the gap between them grows with strength (CCRL Blitz
  minus CCRL 40/15 is about +45 at 3400 and +160 at 3800), so a rating on one
  list cannot be shifted onto another.

Simulated gauntlets, played from a known rating with draws and paired games,
check the fit in `OpenBench/tests.py`. With the build rated among its five
opponents, the 95% error covered the true rating in 377 of 400 runs. With the
build weaker than all five, on a list 1.3 times as wide as our games, it did in
368 of 400, with the error widening from 14 to 34 points by itself. Held at a
scale of 1, the fit puts that second build 41 points too high, with an error
of 17.

Read the result as "this build, at this time control, performs like an engine
rated R on that list", not as the rating the list would give it. The closer
the time control is to the list's own, the less the two differ: CCI's STC is
8+0.08 and its LTC 60+0.6, CCRL Blitz is 2'+1" and CCRL 40/15 is far longer.

## Pages

- `/gauntlets/` lists every gauntlet, newest first, with its rating.
- `/gauntlet/<id>/` has the settings, the rating on each list, and one row per
  opponent: W, L, D, the pentanomial, the score, Elo, Implied, Expected, and
  the games lost on time or by crashing. `?list=<name>` picks the list.
- `/api/gauntlet/<id>/` returns all of that as json.
- `/test/<id>/` of each opponent's test links back to the gauntlet.

## Deploying

The feature adds migration `0018_gauntlet` (two tables, and four columns on
`EngineConfig`; the tests table is not altered) and bumps the static version.
On the server: back up `db.sqlite3`, install the committed files, then
`manage.py migrate`, `manage.py collectstatic --noinput`, and restart.

The server keeps its own `Config/config.json`, which a deploy does not replace.
Add `"rating_list_scales" : { "CCI STC" : 1.35, "CCI LTC" : 1.35, "CCI VLTC" : 1.35 }`
to it by hand, or the CCI lists start from a scale of 1.
