"""Index verified, previously returned Wrangle profile evidence by LinkedIn URL."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.io import atomic_json, digest, read_json

root = Path('data/runs/juicebox-computer-use-hard-v1')
index, units, profiles = {}, 0, 0
for run in sorted(root.parent.iterdir()):
    path = run / 'plan.json'
    if run == root or not path.exists():
        continue
    envelope = read_json(path)
    if envelope['plan'].get('provider', 'wrangle') != 'wrangle':
        continue
    if envelope['plan_sha256'] != digest(envelope['plan']):
        raise ValueError('Source plan changed')
    for path in sorted((run / 'units' / 'search').glob('*.json')):
        unit = read_json(path)
        if unit['status'] != 'complete':
            continue
        if unit['inputs']['plan_sha256'] != envelope['plan_sha256'] or unit['result_sha256'] != digest(unit['result']):
            raise ValueError('Source search unit changed')
        units += 1
        for candidate in unit['result']['candidates']:
            if not candidate['profile'].get('experiences'):
                continue
            profiles += 1
            for identity in candidate['identity_keys']:
                if not identity.startswith('linkedin:/in/'):
                    continue
                url = 'https://www.linkedin.com' + identity.removeprefix('linkedin:')
                entry = {'source_unit': str(path.resolve()), 'rank': candidate['rank'],
                         'source_run': run.name, 'source_ended_at': unit['ended_at'],
                         'result_sha256': unit['result_sha256'],
                         'profile_sha256': candidate['profile_sha256']}
                if url not in index or entry['source_ended_at'] > index[url]['source_ended_at']:
                    index[url] = entry
atomic_json(root / 'source-cache-index.json', {'schema_version':'1.0', 'urls': index,
    'source_units':units, 'source_profiles':profiles, 'unique_linkedin_urls':len(index)})
print({'source_units':units, 'source_profiles':profiles, 'unique_linkedin_urls':len(index)})
