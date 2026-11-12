# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# (Luminex fork) Gauntlets: one Dev build against a list of rated opponents,
# to place the Dev build on a rating list instead of measuring it against
# another build of itself.
#
# A Gauntlet is a group of ordinary fixed-games Tests, one per opponent, tied
# together by GauntletOpponent rows. Workers are handed those Tests, play them
# and report them like any others. This module adds what surrounds them:
#
# >>> create_gauntlet(request)              The form, and its submission
# >>> view_gauntlet(request, pk)            Results of every opponent, and the rating
# >>> modify_gauntlet(request, pk, action)  One action, applied to every Test
# >>> collapse_test_lists(...)              One row per Gauntlet in the lists of Tests
# >>> fit_rating(samples)                   The rating that explains all the games

import math
import re

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery, Sum

import OpenBench.model_utils
import OpenBench.stats
import OpenBench.utils
import OpenBench.views

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import *
from OpenBench.workloads import verify_workload as verify

ELO_PER_LOGIT = 400.0 / math.log(10.0)

## The rating

def expected_score(rating, anchor):
    return 1.0 / (1.0 + 10.0 ** ((anchor - rating) / 400.0))

def score_stats(test):

    # The games played, the points scored, and the variance of those points.
    # A pair of games on one opening is a single observation, as its two games
    # are far from independent. Draws and that pairing both shrink the variance,
    # which is measured here rather than assumed.

    if test.use_penta:
        counts, points, games_each = test.as_penta(), [0.0, 0.5, 1.0, 1.5, 2.0], 2
    else:
        counts, points, games_each = test.as_tri(), [0.0, 0.5, 1.0], 1

    observed = sum(counts)
    total    = sum(n * x for n, x in zip(counts, points))

    # A lone observation says nothing of the spread. Assume the widest one that
    # independent games allow, so that the error is overstated, not understated
    if observed < 2:
        return games_each * observed, total, observed * 0.25 * games_each

    mean     = total / observed
    variance = sum(n * (x - mean) ** 2 for n, x in zip(counts, points)) / (observed - 1)

    return games_each * observed, total, observed * variance

def fit_rating(samples):

    # The maximum likelihood rating given [(anchor, games, points, variance), ...]:
    # one tuple per opponent, with the opponent's published rating as the anchor.
    #
    # Each game is taken to score, on average, E = 1 / (1 + 10^((anchor - R) / 400)).
    # Counting a draw as half a win and half a loss, the log-likelihood of R is
    #     sum of  points * ln(E) + (games - points) * ln(1 - E)
    # and it peaks where the points expected of R equal the points scored:
    #     sum of  games * E(R)  =  sum of  points
    # The left side rises with R, so bisection finds the one solution.
    #
    # The error follows from that same equation. The points scored vary by the
    # measured variances, and a rating point is worth sum(games * E * (1 - E))
    # / ELO_PER_LOGIT points, so the 95% error of R is 1.96 * sqrt(variance) / worth.
    #
    # That error only counts the luck of the games. The anchors are themselves
    # estimates, and are measured under other conditions than ours, so opponents
    # may disagree about R by more than their games explain. The spread is that
    # extra disagreement: the standard deviation, between opponents, of the
    # rating each one implies (DerSimonian-Laird). error_with_spread adds it.

    samples = [sample for sample in samples if sample[1] > 0]
    games   = sum(sample[1] for sample in samples)
    points  = sum(sample[2] for sample in samples)

    # Without both a point scored and a point dropped, the rating has no bound
    if not samples or points <= 0 or points >= games:
        return None

    def surplus(rating):
        return sum(n * expected_score(rating, anchor) for anchor, n, _, _ in samples) - points

    low  = min(sample[0] for sample in samples) - 4000.0
    high = max(sample[0] for sample in samples) + 4000.0

    for _ in range(64):
        rating = (low + high) / 2.0
        if surplus(rating) < 0: low = rating
        else: high = rating

    # How many points one rating point is worth, against each opponent
    worth = [
        n * expected_score(rating, anchor) * (1.0 - expected_score(rating, anchor)) / ELO_PER_LOGIT
        for anchor, n, _, _ in samples
    ]

    deviation = math.sqrt(sum(sample[3] for sample in samples)) / sum(worth)

    # The rating that each opponent implies on its own, and its deviation
    implied = []
    for anchor, n, scored, variance in samples:
        if 0 < scored < n and variance > 0:
            mean  = scored / n
            slope = mean * (1.0 - mean) / ELO_PER_LOGIT
            implied.append((anchor + OpenBench.stats.logistic_elo(mean), math.sqrt(variance) / n / slope))

    spread = 0.0
    if len(implied) >= 3:
        weights  = [1.0 / dev ** 2 for _, dev in implied]
        centre   = sum(w * r for w, (r, _) in zip(weights, implied)) / sum(weights)
        excess   = sum(w * (r - centre) ** 2 for w, (r, _) in zip(weights, implied)) - (len(implied) - 1)
        scale    = sum(weights) - sum(w ** 2 for w in weights) / sum(weights)
        spread   = math.sqrt(max(0.0, excess / scale)) if scale > 0 else 0.0

    # An anchor that is off by x moves the rating by x times its share of the worth
    shares = sum((w / sum(worth)) ** 2 for w in worth)

    return {
        'rating'            : rating,
        'error'             : 1.96 * deviation,
        'spread'            : spread,
        'error_with_spread' : 1.96 * math.sqrt(deviation ** 2 + spread ** 2 * shares),
        'opponents'         : len(samples),
        'games'             : games,
    }

## Opponents

def rated_engines():

    # Engines that have a rating on at least one list. Strongest first, going
    # by each one's best rating, as the lists are on different scales

    configs = [config for config in EngineConfig.objects.filter(enabled=True) if config.ratings]
    return sorted(configs, key=lambda config: (-max(config.ratings.values()), config.name))

def rating_lists(ratings):

    # Names of the lists across many { list : rating }, the most used ones first

    counts = {}
    for rated in ratings:
        for name in rated:
            counts[name] = counts.get(name, 0) + 1

    return sorted(counts, key=lambda name: (-counts[name], name))

def verify_ratings(ratings):

    # Returns an error message, or None, for an EngineConfig.ratings value

    if type(ratings) != dict:
        return 'Ratings must be a json object, such as { "CCRL Blitz" : 3417 }'

    for name, rating in ratings.items():

        if not re.match(r'^[a-zA-Z0-9 /_.+-]{1,32}$', name):
            return 'Rating lists are named with 1-32 of [a-zA-Z0-9 /_.+-]'

        if type(rating) not in (int, float) or not 0 < rating < 10000:
            return 'The rating on %s must be a number' % (name)

    return None

class FormData(object):

    # The functions of verify_workload read their input from a request's POST.
    # This carries the same fields for an opponent, which has no form of its own

    def __init__(self, **fields):
        self.POST = fields

def opponent_build(errors, config):

    # Returns (source, name, sha, bench) for the rated build of an Engine

    name = config.rated_branch

    # A source that is itself an archive, such as a set of prebuilt binaries,
    # is used as it is. There is no branch to look up, and no commit to name
    if config.source.endswith('.zip'):
        stem = config.source.split('/')[-1].removesuffix('.zip')
        return (config.source, name or stem, stem, config.rated_bench)

    if not name:
        errors.append('%s has no rated branch to play against' % (config.name))
        return None

    sha, bench = config.rated_sha, config.rated_bench

    # Ask Github once, and keep the answer, as tags do not move. Without a
    # bench, the same request reads one out of the commit message
    if not sha or not bench:

        fields = FormData(
            base_engine = config.name,
            base_repo   = config.source,
            base_branch = name,
            base_bench  = str(bench) if bench else '',
        )

        if not (found := verify.collect_github_info(errors, fields, 'base')):
            return None

        sha, bench = found[2], found[3]
        EngineConfig.objects.filter(pk=config.pk).update(rated_sha=sha, rated_bench=bench)

    # Plain archive links, unlike those of the API, are not rate limited, and
    # are the ones that the Client retries through its proxy
    source = OpenBench.utils.path_join(config.source, 'archive', '%s.zip' % (sha))
    return (source, name, sha, bench)

## Creation

def form_defaults(engines, like=None):

    # Settings to fill the form with, for each Engine: those of its latest Test.
    # They are what the Engine is being tested with, whether or not it has presets.
    # When repeating a Gauntlet, its Engine takes everything from that Gauntlet

    fields = [
        'dev_options', 'dev_time_control', 'book_name', 'upload_pgns',
        'syzygy_wdl', 'syzygy_adj', 'win_adj', 'draw_adj', 'scale_nps',
    ]

    defaults = {}
    for engine in engines:

        latest = Test.objects.filter(dev_engine=engine.name, deleted=False)
        latest = latest.filter(test_mode__in=['SPRT', 'GAMES']).order_by('-id').first()

        defaults[engine.name] = { 'scale_nps' : engine.nps }
        if latest:
            defaults[engine.name] = { field : getattr(latest, field) for field in fields }

    if like:
        test, repeated = like.first, fields + ['dev_network', 'base_options', 'workload_size', 'priority', 'throughput']
        defaults[test.dev_engine] = { field : getattr(test, field) for field in repeated }
        defaults[test.dev_engine]['games'] = test.max_games

    return defaults

def create_gauntlet(request):

    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if not Profile.objects.get(user=request.user).enabled:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if request.method == 'GET':

        engines   = EngineConfig.objects.filter(enabled=True).order_by('name')
        opponents = rated_engines()

        # ?like=<id> repeats a Gauntlet for another build: same settings, same
        # opponents. Otherwise every opponent is ticked, on the latest list used
        like = request.GET.get('like', '')
        like = find_summary(like) if like.isdigit() else None
        last = like.gauntlet if like else Gauntlet.objects.order_by('-id').first()

        data = {
            'networks'     : list(Network.objects.all().values()),
            'books'        : Book.objects.filter(enabled=True).order_by('name'),
            'engines'      : engines,
            'opponents'    : opponents,
            'ticked'       : [test.base_engine for test in like.tests] if like else [config.name for config in opponents],
            'rating_lists' : rating_lists([config.ratings for config in opponents]),
            'rating_list'  : last.rating_list if last else None,
            'dev_engine'   : like.first.dev_engine if like else Profile.objects.get(user=request.user).engine,
            'defaults'     : form_defaults(engines, like),

            # The source and the networks are applied by create_workload.js
            'engine_configs' : {
                engine.name : OpenBench.model_utils.engine_config_to_dict(engine)
                for engine in engines
            },
        }

        return OpenBench.views.render(request, 'create_gauntlet.html', data)

    gauntlet, errors = create_new_gauntlet(request)

    if errors:
        return OpenBench.views.redirect(request, '/gauntlet/new/', error='\n'.join(errors))

    return OpenBench.views.redirect(request, '/gauntlet/%d/' % (gauntlet.id), status='Gauntlet was Created!')

def verify_gauntlet_creation(errors, request):

    verifications = [

        # Verify everything about the Dev Engine
        (verify.verify_engine         , 'dev_engine', 'Dev Engine'),
        (verify.verify_github_repo    , 'dev_repo'),
        (verify.verify_network        , 'dev_network', 'Dev Network', 'dev_engine'),
        (verify.verify_options        , 'dev_options', 'Threads', 'Dev Options'),
        (verify.verify_options        , 'dev_options', 'Hash', 'Dev Options'),
        (verify.verify_time_control   , 'dev_time_control', 'Time Control'),

        # Verify what is shared by all of the opponents
        (verify.verify_options        , 'base_options', 'Threads', 'Opponent Options'),
        (verify.verify_options        , 'base_options', 'Hash', 'Opponent Options'),
        (verify.verify_integer        , 'games', 'Games'),
        (verify.verify_greater_than   , 'games', 'Games', 0),

        # Verify everything about the General Settings
        (verify.verify_book           , 'book_name', 'Book'),
        (verify.verify_upload_pgns    , 'upload_pgns', 'Upload PGNs'),
        (verify.verify_integer        , 'priority', 'Priority'),
        (verify.verify_greater_than   , 'throughput', 'Throughput', 0),
        (verify.verify_syzygy_field   , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify everything about the Workload Settings
        (verify.verify_integer        , 'workload_size', 'Workload Size'),
        (verify.verify_greater_than   , 'workload_size', 'Workload Size', 0),

        # Verify the Scaling Mechanisms
        (verify.verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify.verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify.verify_syzygy_field   , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify.verify_win_adj        , 'win_adj'),
        (verify.verify_draw_adj       , 'draw_adj'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])

def verify_opponents(errors, request):

    # Returns the EngineConfig of each requested opponent, in the order given

    names   = [name for name in request.POST.getlist('opponents') if name]
    configs = { config.name : config for config in rated_engines() }

    if not names:
        errors.append('A Gauntlet needs at least one opponent')

    for name in names:
        if name not in configs:
            errors.append('%s is not an enabled Engine with ratings' % (name))

    if len(set(names)) != len(names):
        errors.append('An opponent may only be listed once')

    opponents = [configs[name] for name in names if name in configs]

    if opponents and not any(request.POST.get('rating_list') in config.ratings for config in opponents):
        errors.append('None of the opponents has a rating on the chosen Rating List')

    return opponents

def create_new_gauntlet(request):

    # Returns (Gauntlet, None), or (None, errors) with nothing having been created

    errors = []
    verify_gauntlet_creation(errors, request)
    opponents = verify_opponents(errors, request)

    if errors:
        return None, errors

    # Collects all data from the Github API. The Dev build is looked up once,
    # and each opponent only for as long as its commit is not yet known
    dev_info = verify.collect_github_info(errors, request, 'dev')
    builds   = [opponent_build(errors, config) for config in opponents]

    if errors:
        return None, errors

    with transaction.atomic():

        gauntlet = Gauntlet.objects.create(
            author      = request.user.username,
            info        = verify.determine_info(request, dev_info[4])[:1024],
            rating_list = request.POST['rating_list'],
        )

        # One Engine for the Dev build, shared by all of the Tests
        dev = Engine.objects.create(name=dev_info[1], source=dev_info[0], sha=dev_info[2], bench=dev_info[3])

        for config, build in zip(opponents, builds):

            test                   = Test()
            test.author            = request.user.username
            test.book_name         = request.POST['book_name']
            test.upload_pgns       = request.POST['upload_pgns']
            test.info              = 'Gauntlet %d' % (gauntlet.id)

            test.dev               = dev
            test.dev_repo          = request.POST['dev_repo']
            test.dev_engine        = request.POST['dev_engine']
            test.dev_options       = request.POST['dev_options']
            test.dev_network       = request.POST['dev_network']
            test.dev_time_control  = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])

            # Opponents come with their own networks, and play at our time control
            test.base              = Engine.objects.create(name=build[1], source=build[0], sha=build[2], bench=build[3])
            test.base_repo         = config.source
            test.base_engine       = config.name
            test.base_options      = request.POST['base_options']
            test.base_network      = ''
            test.base_time_control = test.dev_time_control

            test.workload_size     = int(request.POST['workload_size'])
            test.priority          = int(request.POST['priority'])
            test.throughput        = int(request.POST['throughput'])

            test.syzygy_wdl        = request.POST['syzygy_wdl']
            test.syzygy_adj        = request.POST['syzygy_adj']
            test.win_adj           = request.POST['win_adj']
            test.draw_adj          = request.POST['draw_adj']

            # Scaling by the opponent would give every opponent its own time
            # control, as each one has its own bench speed. Only the Dev build
            # is the same in all of the Tests
            test.scale_method      = Test.ScaleMethod.DEV
            test.scale_nps         = int(request.POST['scale_nps'])

            test.test_mode         = 'GAMES'
            test.max_games         = int(request.POST['games'])

            if test.dev_network:
                test.dev_netname = Network.objects.get(engine=test.dev_engine, sha256=test.dev_network).name

            test.save()

            GauntletOpponent.objects.create(gauntlet=gauntlet, test=test, ratings=dict(config.ratings))

            summary = 'CREATE P=%d TP=%d' % (test.priority, test.throughput)
            LogEvent.objects.create(author=test.author, summary=summary, log_file='', test_id=test.id)

        profile = Profile.objects.get(user=request.user)
        profile.tests += 1
        profile.save()

        if not OPENBENCH_CONFIG['use_cross_approval'] and profile.approver:
            Test.objects.filter(gauntlet_opponent__gauntlet=gauntlet).update(approved=True)

    return gauntlet, None

## Summaries

class Summary(object):

    # A Gauntlet together with its Tests, and everything that is derived from
    # them. Templates use this in the place of a Test, for a row or for a page

    def __init__(self, gauntlet, entries):

        self.gauntlet = gauntlet
        self.id       = gauntlet.id
        self.author   = gauntlet.author
        self.info     = gauntlet.info
        self.creation = gauntlet.creation

        # A Gauntlet is deleted by deleting its Tests. Deleted Tests are left
        # out, unless nothing else is left, so that a restored opponent returns
        self.deleted  = all(entry.test.deleted for entry in entries)
        self.entries  = [entry for entry in entries if self.deleted or not entry.test.deleted]
        self.tests    = [entry.test for entry in self.entries]

        # The settings are shared, so any Test is able to speak for all of them
        self.first    = self.tests[0] if self.tests else None

        waiting       = [test for test in self.tests if not test.finished and not test.deleted]
        self.pending  = any(not test.approved for test in waiting)
        self.active   = bool(waiting) and not self.pending
        self.finished = not waiting
        self.stopped  = [test for test in self.tests if test.finished and not test.passed and not test.failed]
        self.error    = any(test.error for test in self.tests)
        self.priority = max([test.priority for test in self.tests], default=0)

        self.games    = sum(test.games  for test in self.tests)
        self.wins     = sum(test.wins   for test in self.tests)
        self.losses   = sum(test.losses for test in self.tests)
        self.draws    = sum(test.draws  for test in self.tests)
        self.planned  = sum(test.max_games for test in self.tests)

    def lists(self):
        return rating_lists([entry.ratings for entry in self.entries])

    def fit(self, rating_list=None):

        rating_list = rating_list or self.gauntlet.rating_list

        return fit_rating([
            (entry.ratings[rating_list], *score_stats(entry.test))
            for entry in self.entries if rating_list in entry.ratings
        ])

    def fits(self):
        return [{ 'list' : name, 'fit' : self.fit(name) } for name in self.lists()]

    def rows(self, rating_list=None):

        # One row per opponent, with the strongest first. An opponent without
        # a rating on this list is still shown, with the results of its games

        rating_list = rating_list or self.gauntlet.rating_list
        fit         = self.fit(rating_list)
        rows        = []

        # Games lost on time or to a crash, by either side. An opponent that
        # does not cope with our time control is no measure of anything
        faults = Result.objects.filter(test_id__in=[test.id for test in self.tests]).values('test_id')
        faults = { row['test_id'] : row for row in faults.annotate(timeloss=Sum('timeloss'), crashes=Sum('crashes')) }

        for entry in self.entries:

            test                = entry.test
            games, points, _    = score_stats(test)
            lower, elo, upper   = OpenBench.stats.Elo(test.results())
            anchor              = entry.ratings.get(rating_list)

            row = {
                'test'     : test,
                'name'     : '%s %s' % (test.base_engine, test.base.name),
                'anchor'   : anchor,
                'ratings'  : entry.ratings,
                'games'    : test.games,
                'penta'    : test.as_penta() if test.use_penta else None,
                'score'    : 100.0 * points / games if games else None,
                'elo'      : elo + 0.0 if games else None,
                'error'    : max(upper - elo, elo - lower) if games else None,
                'implied'  : anchor + elo if games and anchor != None else None,
                'expected' : 100.0 * expected_score(fit['rating'], anchor) if fit and anchor != None else None,
                'timeloss' : faults.get(test.id, {}).get('timeloss') or 0,
                'crashes'  : faults.get(test.id, {}).get('crashes') or 0,
            }

            rows.append(row)

        return sorted(rows, key=lambda row: (row['anchor'] == None, -(row['anchor'] or 0), row['name']))

    def stat_lines(self, rating_list=None):

        rating_list = rating_list or self.gauntlet.rating_list
        done        = len([test for test in self.tests if test.finished])

        if (fit := self.fit(rating_list)):
            rating_line = 'Rating: %d +- %d (%s)' % (round(fit['rating']), round(fit['error']), rating_list)
        else:
            rating_line = 'Rating: Pending (%s)' % (rating_list)

        return [
            rating_line,
            'Games: %d W: %d L: %d D: %d' % (self.games, self.wins, self.losses, self.draws),
            'Opponents: %d of %d finished' % (done, len(self.tests)),
        ]

    def long_stat_lines(self, rating_list=None):

        rating_list = rating_list or self.gauntlet.rating_list
        test        = self.first
        threads     = int(OpenBench.utils.extract_option(test.dev_options, 'Threads'))
        hashmb      = int(OpenBench.utils.extract_option(test.dev_options, 'Hash'))
        timecontrol = test.dev_time_control + ['s', '']['=' in test.dev_time_control]

        lines = []

        if (fit := self.fit(rating_list)):
            lines.append('Rating | %d +- %d (95%%) on %s' % (round(fit['rating']), round(fit['error']), rating_list))
            if fit['spread'] >= 0.5:
                lines.append('Spread | %d between opponents, +- %d with it' % (
                    round(fit['spread']), round(fit['error_with_spread'])))
        else:
            lines.append('Rating | Pending on %s' % (rating_list))

        lines.append('Conf   | %s Threads=%d Hash=%dMB' % (timecontrol, threads, hashmb))
        lines.append('Games  | N: %d W: %d L: %d D: %d' % (self.games, self.wins, self.losses, self.draws))
        lines.append('Versus | %d opponent%s, %d games each' % (
            len(self.tests), '' if len(self.tests) == 1 else 's', test.max_games))

        return lines

def summarize(gauntlet_ids):

    # Returns { id : Summary }, loading every Test in a single query

    entries = GauntletOpponent.objects.filter(gauntlet_id__in=list(gauntlet_ids))
    entries = entries.select_related('gauntlet', 'test', 'test__dev', 'test__base').order_by('id')

    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.gauntlet_id, []).append(entry)

    return { pk : Summary(group[0].gauntlet, group) for pk, group in grouped.items() }

def attach_summaries(tests):

    # Tests that stand in for a Gauntlet in a list are given its Summary,
    # as test.gauntlet_summary, which is what the Templates look for

    tests   = list(tests)
    members = { test.id : test for test in tests }

    links = GauntletOpponent.objects.filter(test_id__in=list(members)).values_list('test_id', 'gauntlet_id')
    links = dict(links)

    summaries = summarize(set(links.values()))
    for test_id, gauntlet_id in links.items():
        members[test_id].gauntlet_summary = summaries[gauntlet_id]

    return tests

def without_gauntlets(tests):
    return tests.filter(gauntlet_opponent__isnull=True)

def collapse_test_lists(pending, active, completed):

    # A Gauntlet takes one row in the lists of Tests, in the place of one row
    # per opponent. It is Pending while any Test awaits approval, Active while
    # any Test is being played, and Finished after that.
    #
    # Returns the Pending and Active rows as lists, and the Finished rows as a
    # QuerySet, as there are pages of those. Pass a page of it on to
    # attach_summaries(). The row that remains for a Gauntlet is one of its Tests.

    pending = attach_summaries(pending)
    active  = attach_summaries(active)

    def one_row_each(tests, hidden):
        rows, seen = [], set(hidden)
        for test in tests:
            if (summary := getattr(test, 'gauntlet_summary', None)):
                if summary.id in seen: continue
                seen.add(summary.id)
            rows.append(test)
        return rows

    pending    = one_row_each(pending, [])
    is_pending = [test.gauntlet_summary.id for test in pending if hasattr(test, 'gauntlet_summary')]
    active     = one_row_each(active, is_pending)

    # For the Finished rows the same is done by the database, so that counting
    # and slicing pages still works. The one Test that is kept is the one
    # updated last, which places the Gauntlet at the time that it finished.

    members    = GauntletOpponent.objects.filter(gauntlet=OuterRef('gauntlet_opponent__gauntlet'))
    unfinished = members.filter(test__finished=False, test__deleted=False)
    newest     = members.filter(test__deleted=False).order_by('-test__updated', '-test__id')

    completed = completed.annotate(
        gauntlet_unfinished = Exists(unfinished),
        gauntlet_newest     = Subquery(newest.values('test__id')[:1]),
    )

    completed = completed.filter(
        Q(gauntlet_opponent__isnull=True) |
        Q(gauntlet_unfinished=False, id=F('gauntlet_newest'))
    )

    return pending, active, completed

## Viewing

def find_summary(pk):
    return summarize([int(pk)]).get(int(pk))

def view_gauntlet(request, pk):

    if not (summary := find_summary(pk)):
        return OpenBench.views.redirect(request, '/index/', error='No such Gauntlet exists')

    # Any list that rates one of the opponents may be looked at
    rating_list = request.GET.get('list')
    if rating_list not in summary.lists():
        rating_list = summary.gauntlet.rating_list

    data = {
        'gauntlet'    : summary,
        'workload'    : summary.first,
        'rating_list' : rating_list,
        'rows'        : summary.rows(rating_list),
        'fits'        : summary.fits(),
        'statblock'   : '\n'.join(summary.long_stat_lines(rating_list)),
    }

    return OpenBench.views.render(request, 'gauntlet.html', data)

def gauntlet_to_dict(summary):

    # Everything on the page of a Gauntlet, for scripts

    def rounded(value):
        return None if value == None else round(value, 2)

    def fit_to_dict(fit):
        return None if fit == None else { key : rounded(value) for key, value in fit.items() }

    test = summary.first

    return {
        'id'           : summary.id,
        'author'       : summary.author,
        'info'         : summary.info,
        'creation'     : str(summary.creation),
        'rating_list'  : summary.gauntlet.rating_list,
        'pending'      : summary.pending,
        'active'       : summary.active,
        'finished'     : summary.finished,
        'deleted'      : summary.deleted,
        'dev'          : {
            'engine'       : test.dev_engine,
            'name'         : test.dev.name,
            'sha'          : test.dev.sha,
            'network'      : test.dev_network,
            'netname'      : test.dev_netname,
            'options'      : test.dev_options,
            'time_control' : test.dev_time_control,
        },
        'games'        : summary.games,
        'wins'         : summary.wins,
        'losses'       : summary.losses,
        'draws'        : summary.draws,
        'ratings'      : { each['list'] : fit_to_dict(each['fit']) for each in summary.fits() },
        'opponents'    : [{
            'test'      : row['test'].id,
            'engine'    : row['test'].base_engine,
            'name'      : row['test'].base.name,
            'ratings'   : row['ratings'],
            'max_games' : row['test'].max_games,
            'finished'  : row['test'].finished,
            'games'     : row['games'],
            'wins'      : row['test'].wins,
            'losses'    : row['test'].losses,
            'draws'     : row['test'].draws,
            'penta'     : row['penta'],
            'score'     : rounded(row['score']),
            'elo'       : rounded(row['elo']),
            'error'     : rounded(row['error']),
            'implied'   : rounded(row['implied']),
            'expected'  : rounded(row['expected']),
            'timeloss'  : row['timeloss'],
            'crashes'   : row['crashes'],
        } for row in summary.rows()],
    }

def list_gauntlets(request, page=1):

    # Every Gauntlet, newest first, as a history of the ratings

    gauntlets = Gauntlet.objects.filter(opponents__test__deleted=False).distinct().order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(gauntlets, int(page), 'gauntlets')

    wanted    = list(gauntlets[start:end].values_list('id', flat=True))
    summaries = summarize(wanted)

    data = { 'gauntlets' : [summaries[pk] for pk in wanted if pk in summaries], 'paging' : paging }
    return OpenBench.views.render(request, 'gauntlets.html', data)

## Modification

def modify_gauntlet(request, pk, action):

    actions = [ 'APPROVE', 'RESTART', 'STOP', 'DELETE', 'RESTORE', 'MODIFY' ]

    if action not in actions:
        return OpenBench.views.redirect(request, '/index/', error='Unknown Gauntlet action')

    if not (summary := find_summary(pk)):
        return OpenBench.views.redirect(request, '/index/', error='No such Gauntlet exists')

    # Must be logged in to interact with Gauntlets
    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only users may modify Gauntlets')

    # Must be an approver, or interacting with their own Gauntlet
    profile = Profile.objects.get(user=request.user)
    if not profile.approver and summary.author != request.user.username:
        return OpenBench.views.redirect(request, '/index/', error='You cannot interact with another user\'s Gauntlet')

    if action == 'APPROVE' and not profile.approver:
        return OpenBench.views.redirect(request, '/index/', error='You cannot approve this Gauntlet')

    # Every action is that of a Test, applied to each Test that it applies to

    def approve(test): test.approved = True
    def restart(test): test.finished = False
    def stop(test):    test.finished = True
    def delete(test):  test.deleted  = True
    def restore(test): test.deleted  = False

    def modify(test):

        try: test.priority = int(request.POST['priority'])
        except: pass

        try: test.throughput = max(1, int(request.POST['throughput']))
        except: pass

        try: test.workload_size = max(1, int(request.POST['workload_size']))
        except: pass

    applies = {
        'APPROVE' : (approve, lambda test: not test.approved),
        'RESTART' : (restart, lambda test: test in summary.stopped),
        'STOP'    : (stop   , lambda test: not test.finished),
        'DELETE'  : (delete , lambda test: not test.deleted),
        'RESTORE' : (restore, lambda test: test.deleted),
        'MODIFY'  : (modify , lambda test: True),
    }

    change, applicable = applies[action]

    with transaction.atomic():

        # Only the fields that an action may touch are written, as the Workers
        # are adding results to these same rows while we are at it
        fields = ['approved', 'finished', 'deleted', 'priority', 'throughput', 'workload_size', 'updated']

        for test in filter(applicable, summary.tests):
            change(test)
            test.save(update_fields=fields)
            LogEvent.objects.create(author=request.user.username, summary=action, log_file='', test_id=test.id)

        if action == 'MODIFY' and 'info' in request.POST:
            summary.gauntlet.info = request.POST['info'][:1024]
            summary.gauntlet.save()

    messages = {
        'APPROVE' : 'Gauntlet was Approved!', 'RESTART' : 'Gauntlet was Restarted!',
        'STOP'    : 'Gauntlet was Stopped!',  'DELETE'  : 'Gauntlet was Deleted!',
        'RESTORE' : 'Gauntlet was Restored!', 'MODIFY'  : 'Gauntlet was Modified!',
    }

    # A deleted Gauntlet is on no list, so there is only the index to return to
    destination = '/index/' if action == 'DELETE' else '/gauntlet/%d/' % (summary.id)
    return OpenBench.views.redirect(request, destination, status=messages[action])
