import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


BUILDER = Path(__file__).resolve().parents[1] / 'cnb/build.py'
spec = importlib.util.spec_from_file_location('cnb_builder', BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

FAKE_BUILD = '''
import json, os, pathlib, sys, time
kind, metadata, events, delay, outcome = sys.argv[1:]
def event(action):
    with open(events, 'a') as f:
        f.write(json.dumps(dict(kind=kind, action=action, pid=os.getpid(), time=time.monotonic())) + '\\n')
event('start')
time.sleep(float(delay))
if outcome == 'fail': sys.exit(7)
value = {'containerimage.digest': 'sha256:' + 'e'*64, 'containerimage.config.digest': 'sha256:' + 'f'*64}
if outcome == 'invalid': value['containerimage.digest'] = 'not-a-digest'
pathlib.Path(metadata).write_text(json.dumps(value))
event('end')
'''


class BuildTests(unittest.TestCase):
    def test_benchmark_cache_is_scoped_to_existing_private_registry(self):
        self.assertEqual(builder.cache_references('api'), builder.REGISTRY+'/cache-api:main')
        self.assertEqual(builder.cache_references('web','benchmark-abcdef012345'), builder.REGISTRY+'/cache-web:benchmark-abcdef012345')
        for tag in ('latest', 'another/registry', 'main,mode=min', 'benchmark-x', ''):
            with self.assertRaises(ValueError): builder.cache_references('api', tag)
        with self.assertRaises(ValueError): builder.cache_references('../api')

    def jobs(self, root, delays=(0.4, 0.15, 0.15), outcome='ok'):
        return [(kind, [sys.executable, '-c', FAKE_BUILD, kind, str(root/(kind+'.json')),
                        str(root/'events'), str(delay), outcome if kind == 'api' else 'ok'], root/(kind+'.json'))
                for kind, delay in zip(('api', 'worker', 'web'), delays)]

    def events(self, root):
        return [json.loads(line) for line in (root/'events').read_text().splitlines()]

    def assert_stopped(self, events):
        for event in events:
            if event['action'] == 'start':
                with self.assertRaises(ProcessLookupError): os.kill(event['pid'], 0)

    def test_two_builds_overlap_and_third_starts_only_when_a_slot_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, timings = builder.build_images(self.jobs(root), root, os.environ, root)
            events = self.events(root); active = 0; peak = 0
            for event in events:
                active += 1 if event['action'] == 'start' else -1
                peak = max(peak, active)
            self.assertEqual(peak, 2)
            self.assertEqual(active, 0)
            self.assertEqual(set(images), {'api', 'worker', 'web'})
            self.assertEqual(set(timings), set(images))
            self.assert_stopped(events)

    def test_serial_override_keeps_identical_verified_image_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, _ = builder.build_images(self.jobs(root, (0, 0, 0)), root, os.environ, root, 1)
            self.assertEqual([(e['kind'], e['action']) for e in self.events(root)],
                             [(kind, action) for kind in ('api', 'worker', 'web') for action in ('start', 'end')])
            self.assertTrue(all(v['id'] == 'sha256:'+'f'*64 for v in images.values()))

    def test_failed_build_or_bad_digest_stops_sibling_and_never_starts_queued_work(self):
        for outcome in ('fail', 'invalid'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises((subprocess.CalledProcessError, ValueError)):
                    builder.build_images(self.jobs(root, (0.2, 30, 0), outcome), root, os.environ, root)
                events = self.events(root)
                self.assertNotIn('web', [e['kind'] for e in events])
                self.assert_stopped(events)

    def test_timeout_stops_all_active_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(TimeoutError):
                builder.build_images(self.jobs(root, (30, 30, 0)), root, os.environ, root, timeout=0.2)
            self.assert_stopped(self.events(root))
            self.assertNotIn('web', [e['kind'] for e in self.events(root)])

    def test_sigterm_cancels_independent_child_process_groups_before_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = '''
import importlib.util, json, os, pathlib, signal, sys
sys.path.insert(0, sys.argv[1])
s=importlib.util.spec_from_file_location('builder',sys.argv[2]); b=importlib.util.module_from_spec(s);s.loader.exec_module(b)
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
r=pathlib.Path(sys.argv[3]);jobs=json.loads((r/'jobs.json').read_text())
b.build_images([(k,c,pathlib.Path(m)) for k,c,m in jobs],r,os.environ,r)
'''
            jobs = [(k, command, str(metadata)) for k, command, metadata in self.jobs(root, (30, 30, 0))]
            (root/'jobs.json').write_text(json.dumps(jobs))
            process = subprocess.Popen([sys.executable, '-c', script, str(BUILDER.parents[1]/'scripts'), str(BUILDER), directory])
            try:
                deadline = time.monotonic()+5
                while not (root/'events').exists() or len(self.events(root)) < 2:
                    if time.monotonic() > deadline: self.fail('children did not start')
                    time.sleep(0.02)
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=8), 143)
                self.assert_stopped(self.events(root))
                self.assertNotIn('web', [e['kind'] for e in self.events(root)])
            finally:
                if process.poll() is None: process.kill(); process.wait()

    def test_invalid_concurrency_starts_nothing(self):
        for concurrency in (0, 3, True, '2'):
            with self.assertRaises(ValueError): builder.build_images([], None, {}, None, concurrency)


if __name__ == '__main__': unittest.main()
