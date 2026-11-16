"""Reproduce, then check the fix for, the stale-data flicker across 4 workers.

For each cache mode it starts gunicorn with 4 workers, warms every worker's
cache, adds a mock row, then reads the count many times. A response that does not
match the true count is stale. Expected:
  old (per-process LocMemCache): some stale responses  -> the flicker is reproduced
  new (DummyCache):              zero stale responses  -> the fix works

Run from the repo root, with the same environment variables as the test suite
(DEBUG=1, SAP_URL, ... and the fake SAP server running):
    python flicker_test/run_flicker_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8765
BASE = f'http://127.0.0.1:{PORT}'
ROUNDS = 3
READS = 60


def call(path, method='GET'):
	request = urllib.request.Request(
		BASE + path, method=method,
		data=b'' if method == 'POST' else None,
		headers={'Connection': 'close'},  # a new connection each time, so workers vary
	)
	with urllib.request.urlopen(request, timeout=30) as response:
		return json.load(response)


def start_server(mode):
	log = tempfile.NamedTemporaryFile(prefix=f'flicker_{mode}_', suffix='.log', delete=False)
	env = dict(os.environ, DJANGO_SETTINGS_MODULE='flicker_test.flicker_settings', FLICKER_CACHE_MODE=mode)
	process = subprocess.Popen(
		[sys.executable, '-m', 'gunicorn', '--bind', f'127.0.0.1:{PORT}', '--workers', '4', 'vimp.wsgi:application'],
		env=env, stdout=log, stderr=log,
	)
	for _ in range(60):
		if process.poll() is not None:
			break
		try:
			call('/count/')
			return process, log.name
		except Exception:
			time.sleep(1)
	process.terminate()
	raise SystemExit(f'Server did not start in mode "{mode}". See {log.name}')


def run_mode(mode):
	process, log_path = start_server(mode)
	try:
		call('/cleanup/', 'POST')
		pids = {call('/count/')['pid'] for _ in range(READS)}  # warm every worker
		expected = 0
		stale_orm = stale_view = total = 0
		for _ in range(ROUNDS):
			call('/add/', 'POST')
			expected += 1
			for _ in range(READS):
				data = call('/count/')
				pids.add(data['pid'])
				total += 1
				stale_orm += data['orm'] != expected
				stale_view += data['view'] != expected
		call('/cleanup/', 'POST')
		return {'mode': mode, 'workers_seen': len(pids), 'reads': total,
				'stale_orm': stale_orm, 'stale_view': stale_view, 'log': log_path}
	finally:
		process.terminate()
		process.wait(timeout=15)


def main():
	results = [run_mode('old'), run_mode('new')]
	print()
	for r in results:
		print(f"[{r['mode']:>3}] workers seen: {r['workers_seen']}  reads: {r['reads']}  "
			  f"stale (query cache): {r['stale_orm']}  stale (view cache): {r['stale_view']}")
	old, new = results
	print()
	if old['workers_seen'] < 2:
		print('INCONCLUSIVE: only one worker answered, so the flicker could not show.')
		return 2
	if old['stale_orm'] + old['stale_view'] == 0:
		print('NOTE: the old settings produced no stale reads here, so the flicker was not reproduced.')
	if new['stale_orm'] + new['stale_view'] == 0:
		print('PASS: no stale reads with caching off.')
		return 0
	print('FAIL: stale reads still happen with caching off.')
	return 1


if __name__ == '__main__':
	sys.exit(main())
