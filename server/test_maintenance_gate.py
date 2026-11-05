import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import types

spec = importlib.util.spec_from_file_location('maintenance_gate', Path(__file__).with_name('maintenance_gate.py'))
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
OWNER = {'operationId': 'operation', 'generation': '2', 'ownerToken': 'x' * 40}

class Contract(unittest.TestCase):
    def test_legacy_is_detected_without_acquiring_or_changing_state(self):
        def bridge(_): raise gate.GateRefused('GATE_NOT_INSTALLED')
        result = gate.dispatch({'action': 'status'}, bridge=bridge)
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['capabilities']['protocolVersion'], 1)

    def test_invalid_response_cannot_enable_legacy_fallback(self):
        def bridge(_): raise gate.GateRefused("GATE_APP_PROTOCOL_INVALID")
        with self.assertRaisesRegex(gate.GateRefused, "GATE_APP_PROTOCOL_INVALID"):
            gate.dispatch({"action": "status"}, bridge=bridge)

    def test_acquire_journals_owner_before_mutation_and_keeps_uncertain_owner(self):
        events = []
        def bridge(request):
            events.append(('bridge', request.copy()))
            raise KeyboardInterrupt()
        def journal(value): events.append(('journal', value.copy()))
        with self.assertRaises(KeyboardInterrupt):
            gate.dispatch({'action': 'acquire', 'reason': 'release', 'owner': 'test'}, bridge=bridge, journal_writer=journal)
        self.assertEqual([event[0] for event in events], ['journal', 'bridge'])
        self.assertEqual(events[0][1]['ownerToken'], events[1][1]['ownerToken'])
        self.assertEqual(events[0][1]['acquisitionStatus'], 'pending')

    def test_only_proven_dead_owner_is_reconciled(self):
        seen = []
        owner = {'id': 'old-owner'}
        def bridge(request):
            seen.append(request)
            return {'runtimeOwners': [owner], 'quiescent': False}
        request = {**OWNER, 'action': 'reconcile_owner', 'terminatedInstanceId': 'old-owner'}
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_OWNER_STILL_ALIVE'):
            gate.dispatch(request, bridge=bridge, termination_check=lambda _: False)
        self.assertEqual([item['action'] for item in seen], ['status'])
        seen.clear()
        gate.dispatch(request, bridge=bridge, termination_check=lambda _: True)
        self.assertEqual([item['action'] for item in seen], ['status', 'reconcile_owner'])
        self.assertTrue(seen[1]['evidence']['verifiedTerminated'])

    def test_recover_acquire_restores_same_committed_owner(self):
        pending = {**OWNER, 'reason': 'release', 'owner': 'test'}
        events = []
        def bridge(request):
            events.append(request)
            if request['action'] == 'status': return {'state': 'quiescing', 'operationId': OWNER['operationId']}
            return {**OWNER, 'state': 'quiescing'}
        journals = []
        result = gate.dispatch({**OWNER, 'action': 'recover_pending'}, bridge=bridge, journal_reader=lambda: pending, journal_writer=journals.append)
        self.assertEqual(result['ownerToken'], OWNER['ownerToken'])
        self.assertEqual([item['action'] for item in events], ['status', 'acquire'])
        self.assertEqual(journals[0]['phase'], 'quiescing')

    def test_recover_uncommitted_acquire_requires_pg_unowned_and_real_health(self):
        journals = []
        request = {**OWNER, 'action': 'recover_pending'}
        gate.dispatch(request, bridge=lambda _: {'state': 'accepting', 'operationId': None}, journal_reader=lambda: OWNER, journal_writer=journals.append, health_check=lambda: True)
        self.assertEqual(journals[0]['phase'], 'released')
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_PENDING_OWNER_CONFLICT'):
            gate.dispatch(request, bridge=lambda _: {'state': 'critical', 'operationId': 'another'}, journal_reader=lambda: OWNER, journal_writer=lambda _: self.fail('cleared conflict'), health_check=lambda: True)
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_RUNTIME_UNHEALTHY'):
            gate.dispatch(request, bridge=lambda _: {'state': 'accepting', 'operationId': None}, journal_reader=lambda: OWNER, journal_writer=lambda _: self.fail('cleared unhealthy'), health_check=lambda: False)

    def test_mixed_runtime_flags_fail_closed_before_cli_or_mutation(self):
        rows = {name: {'Id': name, 'Config': {'Env': ['DEPLOYMENT_ADMISSION_ENABLED=' + flag]}} for name, flag in [('api', 'true'), ('worker-general', 'false'), ('worker-ppt', 'true')]}
        with patch.object(gate, '_run', side_effect=AssertionError('should not execute')):
            with self.assertRaisesRegex(gate.GateRefused, 'GATE_RUNTIME_CAPABILITY_MISMATCH'):
                gate._runtime_coherence(types.SimpleNamespace(), rows)
        for row in rows.values(): row['Config']['Env'] = []
        self.assertFalse(gate._runtime_coherence(types.SimpleNamespace(), rows))

    def test_enabled_runtime_requires_verified_bundle_hashes_for_each_role(self):
        rows = {name: {'Id': name, 'Config': {'Env': ['DEPLOYMENT_ADMISSION_ENABLED=true']}} for name in ['api', 'worker-general', 'worker-ppt']}
        ops = types.SimpleNamespace(DOCKER=['docker'], fixed_env=lambda: {})
        def run(args, **_): return 0, json.dumps({'supported': True, 'protocolVersion': 2, 'role': args[-1]}).encode()
        with patch.object(gate, '_run', side_effect=run) as calls:
            self.assertTrue(gate._runtime_coherence(ops, rows)); self.assertEqual(calls.call_count, 3)
        with patch.object(gate, '_run', return_value=(2, b'')):
            with self.assertRaisesRegex(gate.GateRefused, 'GATE_RUNTIME_CAPABILITY_MISMATCH'):
                gate._runtime_coherence(ops, rows)

    def test_runtime_owner_is_bound_to_the_inspected_api_container(self):
        caps = {'protocolVersion': 2, 'transactionalTaskAdmission': True, 'reversibleHttpAdmission': True, 'durableLineage': True}
        ops = types.SimpleNamespace(DOCKER=['docker'], fixed_env=lambda: {}, inspect=lambda: {'api': {'Id': 'a'*64, 'State': {'StartedAt': 'now'}}})
        response = {'status': 'ok', 'capabilities': caps, 'runtimeOwner': {'containerId': 'b'*12}}
        with patch.object(gate, '_ops', return_value=ops), patch.object(gate, '_runtime_coherence', return_value=True), patch.object(gate, '_run', side_effect=[(0, b'{"installed":true}'), (0, json.dumps(response).encode())]):
            with self.assertRaisesRegex(gate.GateRefused, 'GATE_RUNTIME_CAPABILITY_MISMATCH'):
                gate._bridge({'action': 'status'})

    def test_timeout_never_releases_or_reopens(self):
        seen = []
        def bridge(value): seen.append(value['action']); return {'quiescent': False}
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_QUIESCE_TIMEOUT'):
            gate.dispatch({**OWNER, 'action': 'await_quiescent', 'timeoutSeconds': 0}, bridge=bridge)
        self.assertEqual(seen, ['await_quiescent'])

    def test_storage_barrier_fails_before_critical_mutation(self):
        seen = []
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_STORAGE_BARRIER_UNAVAILABLE'):
            gate.dispatch({**OWNER, 'action': 'enter_critical', 'requireStorageWriteBarrier': True}, bridge=lambda r: seen.append(r))
        self.assertEqual(seen, [])

    def test_health_is_observed_not_trusted_from_input(self):
        seen = []
        request = {**OWNER, 'action': 'release_after_healthy', 'evidence': {'healthy': True, 'servicesRestored': True, 'migrationReviewRequired': False}}
        with self.assertRaisesRegex(gate.GateRefused, 'GATE_RUNTIME_UNHEALTHY'):
            gate.dispatch(request, bridge=lambda r: seen.append(r), health_check=lambda: False)
        self.assertEqual(seen, [])
        gate.dispatch(request, bridge=lambda r: seen.append(r) or {"state": "accepting", "operationId": None}, health_check=lambda: True)
        self.assertTrue(seen[0]['evidence']['verifiedByHostHelper'])

    def test_owner_required_for_all_mutating_followups(self):
        for action in ['begin_quiesce', 'await_quiescent', 'enter_critical', 'release_after_healthy']:
            with self.subTest(action=action), self.assertRaisesRegex(gate.GateRefused, 'GATE_OWNER_REQUIRED'):
                gate.dispatch({'action': action}, bridge=lambda _: self.fail('bridge invoked'))

if __name__ == '__main__': unittest.main()
