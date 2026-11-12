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

# (Luminex fork) Tests for Gauntlets. Run with: python3 manage.py test OpenBench
#
# Nothing here reaches Github or a Worker. The lookups of branches are faked,
# and the part of a Worker is played through the same endpoints a Client uses.

import hashlib
import io
import json
import os
import random
import re
import tempfile

from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

import OpenBench.gauntlets
import OpenBench.stats

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.gauntlets import expected_score, fit_rating
from OpenBench.models import *

DEV_SHA = 'fb59275a77777f1b8a6da36b503a6f6a207fc50f'

class FakeGithub(object):

    # Stands in for requests.get() in verify_workload: every branch and tag
    # exists, and resolves to a commit whose message carries a bench

    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None):

        self.calls.append(url)
        kind, ref = url.rstrip('/').split('/')[-2:]

        sha    = ref if re.match('^[0-9a-f]{40}$', ref) else hashlib.sha1(url.encode()).hexdigest()
        commit = { 'sha' : sha, 'commit' : { 'message' : 'A commit\n\nBench: 7654321' } }

        # Only "main" is a branch. Anything else has to be found as a tag or commit
        if kind == 'branches':
            commit = { 'name' : ref, 'commit' : commit } if ref == 'main' else { 'message' : 'Branch not found' }

        response = mock.Mock()
        response.json.return_value = commit
        return response

def simulate_pairs(rng, score, pairs):

    # A pentanomial of `pairs` game pairs, whose games score `score` on average.
    # Each opening favours one colour, so the two games of a pair lean opposite
    # ways, and a good share of the games are drawn

    def play(mean):
        draw = 1.2 * min(mean, 1.0 - mean)
        roll = rng.random()
        return 1.0 if roll < mean - draw / 2 else 0.5 if roll < mean + draw / 2 else 0.0

    counts = [0, 0, 0, 0, 0]
    for _ in range(pairs):
        bias = rng.choice([-1, 1]) * min(0.08, score / 2, (1.0 - score) / 2)
        counts[int(2 * (play(score + bias) + play(score - bias)))] += 1

    return counts

class RatingFitTests(TestCase):

    def test_recovers_the_rating_that_made_the_scores(self):

        truth   = 3462.0
        samples = [
            (anchor, 1000, 1000 * expected_score(truth, anchor), 150.0)
            for anchor in (3417, 3477, 3530, 3640, 3720)
        ]

        fit = fit_rating(samples)
        self.assertAlmostEqual(fit['rating'], truth, places=3)
        self.assertEqual(fit['opponents'], 5)
        self.assertEqual(fit['games'], 5000)

        # Every opponent implies the very same rating, so there is no spread
        self.assertAlmostEqual(fit['spread'], 0.0, places=6)
        self.assertAlmostEqual(fit['error'], fit['error_with_spread'], places=6)

    def test_one_opponent_matches_the_plain_elo_difference(self):

        fit = fit_rating([(3000, 400, 240.0, 60.0)])
        self.assertAlmostEqual(fit['rating'], 3000 + OpenBench.stats.logistic_elo(0.6), places=3)

    def test_needs_points_scored_and_points_dropped(self):

        self.assertIsNone(fit_rating([]))
        self.assertIsNone(fit_rating([(3400, 0, 0.0, 0.0)]))
        self.assertIsNone(fit_rating([(3400, 20, 20.0, 0.0)]))
        self.assertIsNone(fit_rating([(3400, 20, 0.0, 0.0), (3500, 10, 0.0, 0.0)]))

        # One half point against a far stronger opponent is already enough
        self.assertIsNotNone(fit_rating([(3400, 20, 20.0, 0.0), (3900, 20, 0.5, 0.2)]))

    def test_error_covers_the_truth_as_often_as_it_claims(self):

        # Many simulated Gauntlets, played from a known rating with draws and
        # paired games. The 95% error should miss the truth about 1 time in 20

        rng     = random.Random(20261112)
        truth   = 3500.0
        anchors = [3417, 3489, 3530, 3600, 3720]
        points  = [0.0, 0.5, 1.0, 1.5, 2.0]
        covered = 0
        runs    = 400

        for _ in range(runs):

            samples = []
            for anchor in anchors:

                test = Test(use_penta=True, use_tri=False)
                test.LL, test.LD, test.DD, test.DW, test.WW = simulate_pairs(
                    rng, expected_score(truth, anchor), 150)
                samples.append((anchor, *OpenBench.gauntlets.score_stats(test)))

            fit = fit_rating(samples)
            covered += abs(fit['rating'] - truth) <= fit['error']

        self.assertGreater(covered / runs, 0.91)
        self.assertLess(covered / runs, 0.985)

    def test_spread_reports_anchors_that_disagree(self):

        # The same play against every opponent, but the list has two of the
        # five opponents rated 40 too high, and two rated 40 too low

        truth   = 3500.0
        shifts  = [+40, -40, 0, +40, -40]
        samples = []

        for anchor, shift in zip([3417, 3489, 3530, 3600, 3720], shifts):
            scored = 2000 * expected_score(truth, anchor)
            samples.append((anchor + shift, 2000, scored, 300.0))

        fit = fit_rating(samples)
        self.assertGreater(fit['spread'], 25.0)
        self.assertLess(fit['spread'], 55.0)
        self.assertGreater(fit['error_with_spread'], 2.0 * fit['error'])

    def test_score_stats_counts_pairs_as_single_observations(self):

        test = Test(use_penta=True, use_tri=False)
        test.LL, test.LD, test.DD, test.DW, test.WW = 10, 20, 40, 20, 10

        games, scored, variance = OpenBench.gauntlets.score_stats(test)
        self.assertEqual(games, 200)
        self.assertAlmostEqual(scored, 100.0)

        # 100 pairs, each varying by ((1)^2 * 20 + (0.5)^2 * 40) / 99 around 1 point
        self.assertAlmostEqual(variance, 100 * 30.0 / 99)

        test = Test(use_penta=False, use_tri=True)
        test.losses, test.draws, test.wins = 25, 50, 25

        games, scored, variance = OpenBench.gauntlets.score_stats(test)
        self.assertEqual(games, 100)
        self.assertAlmostEqual(scored, 50.0)
        self.assertAlmostEqual(variance, 100 * 12.5 / 99)

class GauntletTests(TestCase):

    def setUp(self):

        self.user    = User.objects.create_user('tester', 'tester@example.com', 'secret')
        self.profile = Profile.objects.create(user=self.user, enabled=True, approver=True, engine='Luminex')
        self.client.login(username='tester', password='secret')

        common = { 'build_compilers' : 'g++', 'build_systems' : 'Linux', 'nps' : 1000000 }

        EngineConfig.objects.create(name='Luminex', source='https://github.com/example/Luminex', **common)

        # A set of prebuilt binaries, whose source is the archive itself
        EngineConfig.objects.create(
            name='Stash-37', source='https://github.com/example/stash-engines/releases/download/all/stash-37.zip',
            rated_branch='v37', rated_bench=4120000, ratings={ 'CCRL Blitz' : 3417, 'CCI STC' : 3050 }, **common)

        # Built from a tag, which has to be looked up once
        EngineConfig.objects.create(
            name='Halogen', source='https://github.com/example/Halogen', build_path='src',
            rated_branch='v16.0.0', ratings={ 'CCRL Blitz' : 3720, 'CCI STC' : 3395 }, **common)

        # Built from a commit that is already known, and rated on one list only
        EngineConfig.objects.create(
            name='Sirius', source='https://github.com/example/Sirius', rated_branch='v9.0',
            rated_sha='1' * 40, rated_bench=5550000, ratings={ 'CCRL Blitz' : 3530 }, **common)

        # Neither of these is an opponent: one has no ratings, one is disabled
        EngineConfig.objects.create(name='Unrated', source='https://github.com/example/Unrated', **common)
        EngineConfig.objects.create(
            name='Stash-36', source='https://github.com/example/stash-36.zip', enabled=False,
            ratings={ 'CCRL Blitz' : 3399 }, **common)

        # The migrations already bring a set of Books along
        Book.objects.update_or_create(name='2moves_v1.epd', defaults={
            'source' : 'https://raw.githubusercontent.com/x/y.zip', 'sha' : '0' * 64, 'enabled' : True })
        Network.objects.create(sha256='A9A8380F', name='net.nnue', engine='Luminex', author='tester', default=True)

        self.github = FakeGithub()
        patcher = mock.patch('OpenBench.workloads.verify_workload.requests.get', self.github)
        patcher.start()
        self.addCleanup(patcher.stop)

    def form(self, **changes):

        form = {
            'dev_engine' : 'Luminex', 'dev_repo' : 'https://github.com/example/Luminex',
            'dev_branch' : DEV_SHA, 'dev_bench' : 'Autofill', 'dev_network' : 'A9A8380F',
            'dev_options' : 'Threads=1 Hash=16', 'dev_time_control' : '10+0.1',
            'opponents' : ['Stash-37', 'Halogen', 'Sirius'], 'base_options' : 'Threads=1 Hash=16',
            'games' : '40', 'rating_list' : 'CCRL Blitz', 'info' : 'A first gauntlet',
            'book_name' : '2moves_v1.epd', 'upload_pgns' : 'COMPACT', 'priority' : '0',
            'throughput' : '1000', 'workload_size' : '1', 'syzygy_wdl' : 'DISABLED',
            'syzygy_adj' : 'OPTIONAL', 'win_adj' : 'None', 'draw_adj' : 'None', 'scale_nps' : '1250000',
        }

        form.update(changes)
        return form

    def create(self, **changes):
        response = self.client.post('/gauntlet/new/', self.form(**changes))
        self.assertEqual(response.status_code, 302)
        return response

    def play(self, test, penta):

        # Report one batch of finished game pairs, the way a Worker would

        machine = self.machine()
        result, _ = Result.objects.get_or_create(test=test, machine=machine)

        wins   = penta[3] + 2 * penta[4] + penta[2]
        losses = penta[1] + 2 * penta[0] + penta[2]
        draws  = penta[1] + penta[3]

        # A split pair is counted as a win and a loss here; either reading of
        # the middle bucket leaves the points, which is all that the fit uses
        response = self.client.post('/clientSubmitResults/', {
            'machine_id' : machine.id, 'secret' : machine.secret, 'test_id' : test.id, 'result_id' : result.id,
            'trinomial' : '%d %d %d' % (losses, draws, wins), 'pentanomial' : ' '.join(map(str, penta)),
            'crashes' : 0, 'timelosses' : 0, 'illegals' : 0,
        })

        return response.json()

    def machine(self):

        if (machine := Machine.objects.first()):
            return machine

        info = {
            'client_ver' : OPENBENCH_CONFIG['client_version'], 'mac_address' : 'AA', 'machine_name' : 'worker',
            'compilers' : { name : ['g++', '14.2.0'] for name in EngineConfig.objects.values_list('name', flat=True) },
            'tokens' : {}, 'cpu_flags' : ['AVX2', 'BMI2'], 'os_name' : 'Linux', 'concurrency' : 4,
            'sockets' : 1, 'physical_cores' : 4, 'syzygy_max' : 0,
        }

        response = self.client.post('/clientWorkerInfo/', {
            'username' : 'tester', 'password' : 'secret', 'system_info' : json.dumps(info) })

        return Machine.objects.get(id=response.json()['machine_id'])

    def test_form_offers_the_rated_engines(self):

        response = self.client.get('/gauntlet/new/')
        self.assertEqual(response.status_code, 200)

        # Strongest first, and neither the unrated nor the disabled Engine
        offered = [config.name for config in response.context['opponents']]
        self.assertEqual(offered, ['Halogen', 'Sirius', 'Stash-37'])
        self.assertEqual(response.context['rating_lists'], ['CCRL Blitz', 'CCI STC'])
        self.assertContains(response, 'Play against Halogen')

    def test_creates_one_fixed_games_test_per_opponent(self):

        response = self.create()
        gauntlet = Gauntlet.objects.get()

        self.assertEqual(response.url, '/gauntlet/%d/' % (gauntlet.id))
        self.assertEqual(gauntlet.author, 'tester')
        self.assertEqual(gauntlet.rating_list, 'CCRL Blitz')
        self.assertEqual(gauntlet.info, 'A first gauntlet')

        tests = { test.base_engine : test for test in Test.objects.all() }
        self.assertEqual(sorted(tests), ['Halogen', 'Sirius', 'Stash-37'])

        # One Dev build, shared by all of the Tests
        self.assertEqual(len({ test.dev_id for test in tests.values() }), 1)

        for test in tests.values():
            self.assertEqual(test.test_mode, 'GAMES')
            self.assertEqual(test.max_games, 40)
            self.assertEqual(test.dev.sha, DEV_SHA)
            self.assertEqual(test.dev.bench, 7654321)
            self.assertEqual(test.dev_network, 'A9A8380F')
            self.assertEqual(test.dev_netname, 'net.nnue')
            self.assertEqual(test.base_network, '')
            self.assertEqual(test.dev_time_control, '10.0+0.10')
            self.assertEqual(test.base_time_control, '10.0+0.10')
            self.assertEqual(test.scale_method, 'DEV')
            self.assertEqual(test.scale_nps, 1250000)
            self.assertTrue(test.approved)
            self.assertTrue(test.use_penta)
            self.assertEqual(test.gauntlet_opponent.gauntlet, gauntlet)

        # Prebuilt: the archive is the source, and nothing was looked up for it
        stash = tests['Stash-37']
        self.assertEqual(stash.base.source, 'https://github.com/example/stash-engines/releases/download/all/stash-37.zip')
        self.assertEqual((stash.base.name, stash.base.sha, stash.base.bench), ('v37', 'stash-37', 4120000))
        self.assertEqual(stash.gauntlet_opponent.ratings, { 'CCRL Blitz' : 3417, 'CCI STC' : 3050 })

        # A tag: looked up, kept on the Engine, and fetched as a plain archive
        halogen = EngineConfig.objects.get(name='Halogen')
        self.assertEqual(len(halogen.rated_sha), 40)
        self.assertEqual(halogen.rated_bench, 7654321)
        self.assertEqual(tests['Halogen'].base.sha, halogen.rated_sha)
        self.assertEqual(tests['Halogen'].base.name, 'v16.0.0')
        self.assertEqual(tests['Halogen'].base.bench, 7654321)
        self.assertEqual(tests['Halogen'].base.source,
            'https://github.com/example/Halogen/archive/%s.zip' % (halogen.rated_sha))

        # A known commit: used as it is
        self.assertEqual(tests['Sirius'].base.sha, '1' * 40)
        self.assertEqual(tests['Sirius'].base.bench, 5550000)
        self.assertFalse(any('Sirius' in url or 'stash' in url for url in self.github.calls))

        self.assertEqual(Profile.objects.get(user=self.user).tests, 1)
        self.assertEqual(LogEvent.objects.filter(summary__startswith='CREATE').count(), 3)

    def test_a_second_gauntlet_looks_up_no_opponent_again(self):

        self.create()
        before = len(self.github.calls)

        self.create()
        after = [url for url in self.github.calls[before:] if 'Luminex' not in url]

        self.assertEqual(after, [])
        self.assertEqual(Gauntlet.objects.count(), 2)

    def test_rejects_bad_forms_and_creates_nothing(self):

        bad_forms = [
            ({ 'opponents' : [] }, 'at least one opponent'),
            ({ 'opponents' : ['Unrated'] }, 'not an enabled Engine with ratings'),
            ({ 'opponents' : ['Stash-36'] }, 'not an enabled Engine with ratings'),
            ({ 'opponents' : ['Sirius', 'Sirius'] }, 'only be listed once'),
            ({ 'opponents' : ['Sirius'], 'rating_list' : 'CCI STC' }, 'None of the opponents'),
            ({ 'games' : '0' }, 'not greater than 0'),
            ({ 'dev_time_control' : 'soon' }, 'not parsable'),
            ({ 'dev_network' : 'FFFFFFFF' }, 'Unknown Network'),
            ({ 'base_options' : 'Hash=16' }, 'Threads'),
        ]

        for changes, message in bad_forms:
            response = self.client.post('/gauntlet/new/', self.form(**changes), follow=True)
            self.assertContains(response, message)

        self.assertEqual(Gauntlet.objects.count(), 0)
        self.assertEqual(Test.objects.count(), 0)
        self.assertEqual(Engine.objects.count(), 0)

    def test_only_enabled_users_may_create(self):

        self.profile.enabled = False
        self.profile.save()

        response = self.client.post('/gauntlet/new/', self.form())
        self.assertEqual(response.url, '/login/')
        self.assertEqual(Gauntlet.objects.count(), 0)

    def test_scripts_endpoint_creates_gauntlets(self):

        self.client.logout()

        form = self.form(action='CREATE_GAUNTLET', username='tester', password='secret')
        response = self.client.post('/scripts/', form)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Gauntlet.objects.count(), 1)

    def test_workers_play_it_as_ordinary_tests(self):

        self.create()
        machine = self.machine()

        self.assertEqual(sorted(machine.info['supported']), ['Halogen', 'Luminex', 'Sirius', 'Stash-36', 'Stash-37', 'Unrated'])

        response = self.client.post('/clientGetWorkload/', { 'machine_id' : machine.id, 'secret' : machine.secret })
        workload = response.json()['workload']

        test = Test.objects.get(id=workload['test']['id'])
        self.assertEqual(workload['test']['type'], 'GAMES')
        self.assertEqual(workload['test']['scale_method'], 'DEV')
        self.assertEqual(workload['test']['dev']['engine'], 'Luminex')
        self.assertEqual(workload['test']['dev']['network'], 'A9A8380F')
        self.assertEqual(workload['test']['base']['engine'], test.base_engine)
        self.assertEqual(workload['test']['base']['network'], '')
        self.assertEqual(workload['test']['base']['source'], test.base.source)
        self.assertEqual(workload['test']['base']['build']['path'], EngineConfig.objects.get(name=test.base_engine).build_path)

        # 4 concurrent games, of 1 pair each
        self.assertEqual(workload['distribution']['rounds-per-runner'], 4)

        # Short of the 40 games the Test goes on, and at 40 the Worker is stopped
        self.assertEqual(self.play(test, [1, 2, 4, 2, 1]), {})
        self.assertEqual(self.play(test, [0, 2, 4, 3, 1]), { 'stop' : True })

        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertEqual(test.games, 40)

        # The next Workload is for another opponent
        response = self.client.post('/clientGetWorkload/', { 'machine_id' : machine.id, 'secret' : machine.secret })
        self.assertNotEqual(response.json()['workload']['test']['id'], test.id)

    def test_page_shows_every_opponent_and_the_rating(self):

        self.create()
        tests = { test.base_engine : test for test in Test.objects.all() }

        # 60% against 3417, 50% against 3530, and nothing yet against 3720
        self.play(tests['Stash-37'], [1, 3, 6, 6, 4])
        self.play(tests['Sirius'], [2, 4, 8, 4, 2])

        gauntlet = Gauntlet.objects.get()
        response = self.client.get('/gauntlet/%d/' % (gauntlet.id))
        self.assertEqual(response.status_code, 200)

        rows = { row['test'].base_engine : row for row in response.context['rows'] }
        self.assertEqual([row['test'].base_engine for row in response.context['rows']], ['Halogen', 'Sirius', 'Stash-37'])

        self.assertEqual(rows['Stash-37']['anchor'], 3417)
        self.assertEqual(rows['Stash-37']['games'], 40)
        self.assertAlmostEqual(rows['Stash-37']['score'], 100 * 24.5 / 40)
        self.assertAlmostEqual(rows['Stash-37']['implied'], 3417 + OpenBench.stats.logistic_elo(24.5 / 40))
        self.assertEqual(rows['Sirius']['penta'], (2, 4, 8, 4, 2))
        self.assertAlmostEqual(rows['Sirius']['elo'], 0.0)
        self.assertIsNone(rows['Halogen']['score'])

        # 44.5 points of 80 were scored, and the rating expects exactly as many
        fit = gauntlet_fit = OpenBench.gauntlets.find_summary(gauntlet.id).fit()
        expected = 40 * expected_score(fit['rating'], 3417) + 40 * expected_score(fit['rating'], 3530)
        self.assertAlmostEqual(expected, 44.5, places=6)
        self.assertEqual(fit['opponents'], 2)
        self.assertAlmostEqual(rows['Sirius']['expected'], 100 * expected_score(fit['rating'], 3530))

        self.assertContains(response, 'Rating | %d +- ' % (round(fit['rating'])))
        self.assertContains(response, 'on CCRL Blitz')
        self.assertContains(response, 'Stash-37 v37')
        self.assertContains(response, 'Halogen v16.0.0')

        # The other list rates only Stash and Halogen, and only Stash has games
        fits = { each['list'] : each['fit'] for each in response.context['fits'] }
        self.assertEqual(list(fits), ['CCRL Blitz', 'CCI STC'])
        self.assertAlmostEqual(fits['CCI STC']['rating'], 3050 + OpenBench.stats.logistic_elo(24.5 / 40), places=3)

        response = self.client.get('/gauntlet/%d/?list=CCI STC' % (gauntlet.id))
        self.assertEqual(response.context['rating_list'], 'CCI STC')
        self.assertContains(response, 'on CCI STC')

        # A list that rates none of the opponents falls back to the Gauntlet's own
        response = self.client.get('/gauntlet/%d/?list=Nonsense' % (gauntlet.id))
        self.assertEqual(response.context['rating_list'], 'CCRL Blitz')

        # The page of each Test links back to the Gauntlet
        response = self.client.get('/test/%d/' % (tests['Sirius'].id))
        self.assertContains(response, '/gauntlet/%d/' % (gauntlet.id))

        self.assertEqual(self.client.get('/gauntlet/999/').url, '/index/')

    def test_api_returns_the_whole_table(self):

        self.create()
        tests = { test.base_engine : test for test in Test.objects.all() }
        self.play(tests['Stash-37'], [1, 3, 6, 6, 4])

        gauntlet = Gauntlet.objects.get()
        data = self.client.get('/api/gauntlet/%d/' % (gauntlet.id)).json()['gauntlet']

        self.assertEqual(data['id'], gauntlet.id)
        self.assertEqual(data['rating_list'], 'CCRL Blitz')
        self.assertEqual(data['dev']['sha'], DEV_SHA)
        self.assertEqual(data['games'], 40)
        self.assertTrue(data['active'])
        self.assertFalse(data['finished'])
        self.assertEqual([row['engine'] for row in data['opponents']], ['Halogen', 'Sirius', 'Stash-37'])
        self.assertEqual(data['opponents'][2]['penta'], [1, 3, 6, 6, 4])
        self.assertAlmostEqual(data['ratings']['CCRL Blitz']['rating'], 3417 + OpenBench.stats.logistic_elo(24.5 / 40), places=1)

        self.assertIn('error', self.client.get('/api/gauntlet/999/').json())

    def test_lists_show_one_row_per_gauntlet(self):

        # An ordinary Test on either side of the Gauntlet
        def plain_test(**fields):
            engine = Engine.objects.create(name='main', source='https://api.github.com/repos/e/L/zipball/abc', sha='a' * 40, bench=1)
            return Test.objects.create(
                author='tester', book_name='2moves_v1.epd', dev=engine, base=engine,
                dev_repo='https://github.com/e/L', base_repo='https://github.com/e/L',
                dev_engine='Luminex', base_engine='Luminex', dev_options='Threads=1 Hash=16',
                base_options='Threads=1 Hash=16', dev_time_control='10.0+0.10', base_time_control='10.0+0.10',
                approved=True, **fields)

        older = plain_test(finished=True, passed=True)
        self.create()
        newer = plain_test()

        gauntlet = Gauntlet.objects.get()
        members  = list(Test.objects.filter(gauntlet_opponent__gauntlet=gauntlet).order_by('id'))

        def index():
            context = self.client.get('/index/').context
            return (
                list(context['pending']),
                [test for group in context['active'] for test in group['tests']],
                list(context['completed']),
            )

        def is_gauntlet(test):
            return getattr(test, 'gauntlet_summary', None) != None

        # Active: the plain Test, and one row for the three Tests of the Gauntlet
        pending, active, completed = index()
        self.assertEqual(pending, [])
        self.assertEqual(len(active), 2)
        self.assertEqual([is_gauntlet(test) for test in active].count(True), 1)
        self.assertEqual([test.id for test in completed], [older.id])
        self.assertContains(self.client.get('/index/'), 'Gauntlet: %s' % (DEV_SHA[:16].upper()))
        self.assertContains(self.client.get('/index/'), 'Opponents: 0 of 3 finished')

        # Still Active while one opponent is unfinished, and not yet Finished
        self.play(members[0], [5, 5, 0, 5, 5])
        self.play(members[1], [5, 5, 0, 5, 5])
        pending, active, completed = index()
        self.assertEqual(len(active), 2)
        self.assertEqual([test.id for test in completed], [older.id])

        # Finished: one row, placed by the Test that finished last
        self.play(members[2], [5, 5, 0, 5, 5])
        pending, active, completed = index()
        self.assertEqual([test.id for test in active], [newer.id])
        self.assertEqual([test.id for test in completed], [members[2].id, older.id])
        self.assertTrue(is_gauntlet(completed[0]))
        self.assertFalse(is_gauntlet(completed[1]))
        self.assertContains(self.client.get('/index/'), 'Opponents: 3 of 3 finished')

        # The same holds on the page of the author, and Greens has no opponents
        context = self.client.get('/user/tester/').context
        self.assertEqual([test.id for test in context['completed']], [members[2].id, older.id])
        context = self.client.get('/greens/').context
        self.assertEqual([test.id for test in context['completed']], [older.id])

        # Pending: one row, even though only one of its Tests awaits approval
        Test.objects.filter(id__in=[members[0].id]).update(approved=False, finished=False)
        Test.objects.filter(id__in=[members[1].id]).update(finished=False)
        pending, active, completed = index()
        self.assertEqual([is_gauntlet(test) for test in pending], [True])
        self.assertEqual([test.id for test in active], [newer.id])
        self.assertEqual([test.id for test in completed], [older.id])

        # The history of Gauntlets
        response = self.client.get('/gauntlets/')
        self.assertEqual([summary.id for summary in response.context['gauntlets']], [gauntlet.id])
        self.assertContains(response, '/gauntlet/%d/' % (gauntlet.id))

    def test_finished_rows_page_like_tests(self):

        # 30 Gauntlets of one opponent each, all finished: 25 rows, then 5
        for _ in range(30):
            self.create(opponents=['Sirius'], games='2')

        for test in Test.objects.all():
            self.play(test, [0, 0, 1, 0, 0])

        context = self.client.get('/index/').context
        self.assertEqual(len(context['completed']), 25)
        self.assertTrue(all(hasattr(test, 'gauntlet_summary') for test in context['completed']))

        context = self.client.get('/index/2/').context
        self.assertEqual(len(context['completed']), 5)

    def test_actions_apply_to_every_test(self):

        self.create()
        gauntlet = Gauntlet.objects.get()
        url      = '/gauntlet/%d/' % (gauntlet.id)
        tests    = lambda: list(Test.objects.order_by('id'))

        self.play(tests()[0], [5, 5, 0, 5, 5])

        # Stop only stops what is still being played
        self.assertEqual(self.client.get(url + 'STOP/').url, url)
        self.assertTrue(all(test.finished for test in tests()))

        # Restart only restarts what was stopped, not what was played out
        self.client.get(url + 'RESTART/')
        self.assertEqual([test.finished for test in tests()], [True, False, False])

        self.client.post(url + 'MODIFY/', { 'priority' : '7', 'throughput' : '0', 'workload_size' : '3', 'info' : 'Changed' })
        self.assertEqual([(test.priority, test.throughput, test.workload_size) for test in tests()], [(7, 1, 3)] * 3)
        self.assertEqual(Gauntlet.objects.get().info, 'Changed')
        self.assertTrue(all(test.info == 'Gauntlet %d' % (gauntlet.id) for test in tests()))

        # A deleted Gauntlet leaves every list, and its page still restores it
        self.assertEqual(self.client.get(url + 'DELETE/').url, '/index/')
        self.assertTrue(all(test.deleted for test in tests()))
        self.assertEqual(list(self.client.get('/gauntlets/').context['gauntlets']), [])
        self.assertEqual([t for g in self.client.get('/index/').context['active'] for t in g['tests']], [])
        self.assertContains(self.client.get(url), 'Restore')

        self.client.get(url + 'RESTORE/')
        self.assertFalse(any(test.deleted for test in tests()))

        self.assertEqual(self.client.get(url + 'EXPLODE/').url, '/index/')

        # Results that arrived meanwhile are not undone by an action
        self.assertEqual(tests()[0].games, 40)

    def test_actions_need_the_author_or_an_approver(self):

        self.create()
        gauntlet = Gauntlet.objects.get()
        Test.objects.update(approved=False)

        other = User.objects.create_user('other', 'other@example.com', 'secret')
        Profile.objects.create(user=other, enabled=True)

        self.client.logout()
        self.assertEqual(self.client.get('/gauntlet/%d/STOP/' % (gauntlet.id)).url, '/login/')

        self.client.login(username='other', password='secret')
        self.assertEqual(self.client.get('/gauntlet/%d/STOP/' % (gauntlet.id)).url, '/index/')
        self.assertFalse(Test.objects.filter(finished=True).exists())

        # An author who is not an approver may stop, but not approve
        Gauntlet.objects.update(author='other')
        self.assertEqual(self.client.get('/gauntlet/%d/APPROVE/' % (gauntlet.id)).url, '/index/')
        self.assertFalse(Test.objects.filter(approved=True).exists())

        self.client.get('/gauntlet/%d/STOP/' % (gauntlet.id))
        self.assertFalse(Test.objects.filter(finished=False).exists())

        self.client.login(username='tester', password='secret')
        self.client.get('/gauntlet/%d/APPROVE/' % (gauntlet.id))
        self.assertFalse(Test.objects.filter(approved=False).exists())

    def test_engine_form_keeps_the_rated_build(self):

        self.profile.superuser = True
        self.profile.save()

        config = EngineConfig.objects.get(name='Sirius')
        form   = {
            'source' : config.source, 'nps' : '900000', 'private' : 'FALSE', 'enabled' : 'TRUE',
            'build_path' : '', 'build_compilers' : 'g++', 'build_cpuflags' : '', 'build_systems' : 'Linux',
            'presets' : json.dumps({ 'test_presets' : { 'default' : {} }, 'tune_presets' : { 'default' : {} },
                                     'datagen_presets' : { 'default' : {} } }),
            'rated_branch' : 'v9.0', 'rated_bench' : '5550000', 'ratings' : '{ "CCRL Blitz" : 3531, "CCI LTC" : 3387 }',
        }

        # The ratings change, and the commit stays, as the branch did not move
        self.client.post('/manage/engines/Sirius/edit/', form)
        config.refresh_from_db()
        self.assertEqual(config.ratings, { 'CCRL Blitz' : 3531, 'CCI LTC' : 3387 })
        self.assertEqual(config.rated_sha, '1' * 40)

        # Another branch is another commit, which is looked up again when played
        self.client.post('/manage/engines/Sirius/edit/', { **form, 'rated_branch' : 'v9.1' })
        config.refresh_from_db()
        self.assertEqual((config.rated_branch, config.rated_sha), ('v9.1', ''))

        response = self.client.post('/manage/engines/Sirius/edit/', { **form, 'ratings' : '{ "CCRL Blitz" : "strong" }' }, follow=True)
        self.assertContains(response, 'must be a number')

        response = self.client.post('/manage/engines/Sirius/edit/', { **form, 'ratings' : '[3531]' }, follow=True)
        self.assertContains(response, 'must be a json object')

        # Editing the ratings must not send every Worker off to restart
        checksum = ServerState.checksum()
        self.client.post('/manage/engines/Sirius/edit/', { **form, 'ratings' : '{}', 'rated_bench' : '1' })
        self.assertEqual(ServerState.checksum(), checksum)
        self.assertEqual(EngineConfig.objects.get(name='Sirius').ratings, {})

        self.assertContains(self.client.get('/manage/engines/Halogen/'), 'v16.0.0')
        self.assertContains(self.client.get('/manage/engines/'), 'CCRL Blitz 3720')

    def test_import_engines_reads_the_rated_build(self):

        conf = {
            'private' : False, 'nps' : 800000, 'source' : 'https://github.com/example/Clover',
            'build' : { 'path' : 'src', 'compilers' : ['g++'], 'cpuflags' : ['AVX2'], 'systems' : ['Linux'] },
            'rated' : { 'branch' : 'v9.1', 'sha' : '2' * 40, 'bench' : 1234567, 'ratings' : { 'CCRL Blitz' : 3726 } },
        }

        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, 'Clover.json'), 'w') as fout:
                json.dump(conf, fout)
            call_command('import_engines', folder, stdout=io.StringIO())

        config = EngineConfig.objects.get(name='Clover')
        self.assertEqual((config.rated_branch, config.rated_sha, config.rated_bench), ('v9.1', '2' * 40, 1234567))
        self.assertEqual(config.ratings, { 'CCRL Blitz' : 3726 })
        self.assertEqual(config.build_path, 'src')
        self.assertIn(config, OpenBench.gauntlets.rated_engines())
