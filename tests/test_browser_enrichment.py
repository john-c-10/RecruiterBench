from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import patch

from benchmark import browser_enrichment as enrichment
from benchmark.browser_evaluation import read_collection
from benchmark.io import atomic_json, digest, read_json


class BrowserEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.role = {"role_id": "role", "search_body": {"query": "Frozen brief"}}
        self.addCleanup(enrichment.source_unit.cache_clear)

    def url(self, number):
        return f"https://www.linkedin.com/in/person-{number}"

    def cached(self, number, status="found", **extra):
        result = {"enrichment_status": status,
                  "profile": {"location": "US", "experiences": ["Engineer"], "educations": [], "listed_skills": []} if status == "found" else None,
                  "personId": str(number) if status == "found" else None,
                  "linkedinUrlStable": self.url(number) if status == "found" else None, **extra}
        atomic_json(enrichment.cache_path(self.root, self.url(number)),
                    {"url": self.url(number), "result": result, "result_sha256": digest(result)})
        return result

    def capture(self, count):
        # Historical page completion at raw rank200 is intentionally nonfinal.
        rows = [{"id": str(n), "linkedin_url": self.url(n)} for n in range(1, count+1)]
        for index, batch in enumerate([rows[:200], rows[200:]], 1):
            if not batch:
                continue
            atomic_json(self.root / 'browser-links' / 'role' / f'page-{index:03d}.json',
                        {"role_id": "role", "page": index, "query": "Frozen brief", "query_started_at": None,
                         "timing_valid": False, "source_url": "https://app.juicebox.ai/project/test",
                         "collected_at": "2026-10-15T01:00:00Z", "complete": index == 1,
                         "termination_reason": "target_reached", "candidates": batch})
        return read_collection(self.root, self.role, source_directory="browser-links", require_experience=False)

    def test_batch_assignments_survive_new_earlier_candidates(self):
        original = [self.url(n) for n in range(65)]
        before = enrichment.pending_batches(self.root, original)
        after = enrichment.pending_batches(self.root, [self.url(n) for n in range(100, 115)] + original)
        self.assertEqual(sorted(map(tuple, before)), sorted(map(tuple, [batch for batch in after if batch[0] in original])))
        self.assertEqual(sorted(map(len, after)), [15, 15, 50])

    def test_uncertain_request_migrates_without_regrouping(self):
        inputs = {"endpoint": enrichment.ENDPOINT, "linkedinUrls": [self.url(1), self.url(2)]}
        atomic_json(self.root / 'units' / 'enrichment' / 'old.json', {"inputs": inputs, "status": "uncertain"})
        batches = enrichment.pending_batches(self.root, [self.url(3), self.url(1), self.url(2)])
        self.assertIn(inputs['linkedinUrls'], batches)
        self.assertIn([self.url(3)], batches)

    def test_skip_missing_and_wait_for_earlier_pending_before_selecting_200(self):
        collection = self.capture(250)
        self.assertIsNone(collection['browser_collection_ms'])
        self.assertFalse(collection['collection_complete'])
        for number in range(1, 251):
            if number != 51:
                self.cached(number)
        first = enrichment.publish_pages(self.root, self.role, collection)
        self.assertEqual(first['selected_count'], 50)
        self.assertFalse(first['complete'])
        originals = {p: p.read_bytes() for p in (self.root / 'browser' / 'role').glob('page-*.json')}
        self.cached(51, 'not_found')
        second = enrichment.publish_pages(self.root, self.role, collection)
        self.assertEqual((second['selected_count'], second['skipped_not_found_count'], second['complete']), (200, 1, True))
        self.assertTrue(all(path.read_bytes() == content for path, content in originals.items()))
        output = read_collection(self.root, self.role)
        ranks = [row['source_rank'] for row in output['raw_candidates']]
        self.assertEqual(ranks, [*range(1, 51), *range(52, 202)])
        self.assertTrue(output['collection_complete'])

    def test_missing_and_search_urls_skip_without_guessing_identity(self):
        self.assertIsNone(enrichment.canonical_linkedin(None))
        self.assertIsNone(enrichment.canonical_linkedin(''))
        self.assertIsNone(enrichment.canonical_linkedin('https://www.linkedin.com/search/results/people/?keywords=someone'))
        with self.assertRaises(ValueError):
            enrichment.canonical_linkedin('https://not-linkedin.example/in/person')

    def test_inflight_api_result_preserves_frozen_cache_and_is_auditable(self):
        self.cached(1, cache_provenance={"source": "verified_wrangle_benchmark_cache"})
        path = enrichment.cache_path(self.root, self.url(1))
        original = path.read_bytes()
        response = SimpleNamespace(status_code=200, json=lambda: {"results": [{"identifier": self.url(1), "status": "found",
            "profile": {"id": "1", "location": "Canada", "experience": ["Changed professional evidence"]}}]})
        calls = []
        client = SimpleNamespace(post=lambda *args, **kwargs: (calls.append(kwargs) or response))
        with patch.dict(os.environ, {"WRANGLE_API_KEY": "test-fixture"}), patch.object(enrichment, 'ENRICHMENT_PACER', SimpleNamespace(wait=lambda: None)):
            enrichment.enrich_batch(self.root, [self.url(1)], client)
            enrichment.enrich_batch(self.root, [self.url(1)], client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(path.read_bytes(), original)
        unit = read_json(next((self.root / 'units' / 'enrichment').glob('*.json')))
        self.assertEqual(unit['result']['results'][self.url(1)]['profile']['location'], 'Canada')
        self.assertEqual(unit['status'], 'complete')


    def test_settled_unchanged_passes_do_not_replay_profiles_or_manifests(self):
        self.capture(2)
        enrichment.pending_batches(self.root, [self.url(1), self.url(2)])
        self.cached(1)
        self.cached(2)
        passes = enrichment.WatcherPasses(self.root)
        collections, missing = passes.scan([self.role])
        self.assertEqual(len(collections), 1)
        self.assertEqual(missing, [])
        self.assertEqual(passes.batches(missing), [])
        passes.batches_finished()
        with patch.object(enrichment, 'read_collection', side_effect=AssertionError('redundant capture read')), \
             patch.object(enrichment, 'cached_profile', side_effect=AssertionError('redundant profile read')), \
             patch.object(enrichment, 'publish_pages', side_effect=AssertionError('redundant publication')), \
             patch.object(enrichment, 'pending_batches', side_effect=AssertionError('redundant manifest replay')), \
             patch.object(enrichment, 'read_json', side_effect=AssertionError('redundant JSON read')):
            self.assertEqual(passes.scan([self.role]), ([], []))
            self.assertEqual(passes.batches([]), [])
            passes.batches_finished()

    def test_appended_raw_page_updates_provenance_without_changing_selected_200(self):
        self.capture(200)
        for number in range(1, 201):
            self.cached(number)
        passes = enrichment.WatcherPasses(self.root)
        passes.scan([self.role])
        original = {p: p.read_bytes() for p in (self.root / 'browser' / 'role').glob('page-*.json')}
        first = read_json(self.root / 'browser-links' / 'role' / 'page-001.json')
        atomic_json(self.root / 'browser-links' / 'role' / 'page-002.json',
                    {**first, 'page': 2, 'complete': False, 'resume_overlap_changed': True,
                     'candidates': [{'id': str(n), 'linkedin_url': self.url(n)} for n in range(201, 211)]})
        collections, missing = passes.scan([self.role])
        self.assertEqual(len(collections), 1)
        self.assertEqual(missing, [self.url(n) for n in range(201, 211)])
        progress = read_json(self.root / 'enrichment-progress' / 'role.json')
        self.assertEqual((progress['raw_count'], progress['pending_count'], progress['selected_count']), (210, 10, 200))
        self.assertTrue(progress['complete'])
        for number in range(201, 211):
            self.cached(number, 'not_found')
        passes.scan([self.role])
        progress = read_json(self.root / 'enrichment-progress' / 'role.json')
        self.assertEqual((progress['scanned_count'], progress['skipped_not_found_count'], progress['pending_count']), (210, 10, 0))
        self.assertTrue(all(path.read_bytes() == content for path, content in original.items()))
        self.assertEqual([row['source_rank'] for row in read_collection(self.root, self.role)['raw_candidates']], list(range(1, 201)))
        self.assertEqual(passes.scan([self.role]), ([], []))

    def test_unchanged_pending_capture_is_revisited_until_cache_arrives(self):
        self.capture(2)
        self.cached(1)
        passes = enrichment.WatcherPasses(self.root)
        self.assertEqual(passes.scan([self.role])[1], [self.url(2)])
        self.assertEqual(passes.scan([self.role])[1], [self.url(2)])
        self.cached(2)
        self.assertEqual(passes.scan([self.role])[1], [])
        self.assertEqual(read_json(self.root / 'enrichment-progress' / 'role.json')['selected_count'], 2)
        self.assertEqual(passes.scan([self.role]), ([], []))

    def test_retry_eligible_orphan_unit_is_not_skipped_or_rekeyed(self):
        for status in ('failed', 'uncertain'):
            with self.subTest(status=status):
                root = self.root / status
                inputs = {'endpoint': enrichment.ENDPOINT, 'linkedinUrls': [self.url(1)]}
                key = digest(inputs)
                unit_path = root / 'units' / 'enrichment' / f'{digest(key)}.json'
                atomic_json(unit_path, {'inputs': inputs, 'input_sha256': digest(inputs),
                                      'status': status, 'attempt': 1})
                passes = enrichment.WatcherPasses(root)
                self.assertEqual(passes.batches([]), [[self.url(1)]])
                self.assertEqual(passes.batches([]), [[self.url(1)]])
                response = SimpleNamespace(status_code=200, json=lambda: {'results': [{
                    'identifier': self.url(1), 'status': 'found',
                    'profile': {'id': '1', 'location': 'US', 'experience': ['Engineer']}}]})
                calls = []
                client = SimpleNamespace(post=lambda *args, **kwargs: (calls.append(kwargs) or response))
                with self.assertRaises(RuntimeError):
                    enrichment.enrich_batch(root, [self.url(1)], client)
                self.assertEqual(calls, [])
                with patch.dict(os.environ, {'WRANGLE_API_KEY': 'test-fixture'}), \
                     patch.object(enrichment, 'ENRICHMENT_PACER', SimpleNamespace(wait=lambda: None)):
                    enrichment.enrich_batch(root, [self.url(1)], client, **{'retry_' + status: True})
                self.assertEqual(calls[0]['headers']['Idempotency-Key'], 'recruiterbench-linkedin-' + key)
                self.assertEqual(read_json(unit_path)['attempt'], 2)
                passes.batches_finished()
                with patch.object(enrichment, 'pending_batches', side_effect=AssertionError('settled replay')):
                    self.assertEqual(passes.batches([]), [])

    def test_new_manifest_is_detected_after_an_idle_pass(self):
        passes = enrichment.WatcherPasses(self.root)
        self.assertEqual(passes.batches([]), [])
        inputs = {'endpoint': enrichment.ENDPOINT, 'linkedinUrls': [self.url(7)]}
        atomic_json(self.root / 'enrichment-batches' / f'{digest(inputs)}.json',
                    {'inputs': inputs, 'batch_sha256': digest(inputs)})
        self.assertEqual(passes.batches([]), [[self.url(7)]])

    def test_profile_file_change_invalidates_settled_hash_checks(self):
        self.capture(1)
        self.cached(1)
        passes = enrichment.WatcherPasses(self.root)
        passes.scan([self.role])
        path = enrichment.cache_path(self.root, self.url(1))
        record = read_json(path)
        record['result']['profile']['location'] = 'Unverified change'
        atomic_json(path, record)
        with self.assertRaisesRegex(ValueError, 'Enrichment cache changed'):
            passes.scan([self.role])


if __name__ == '__main__':
    unittest.main()
