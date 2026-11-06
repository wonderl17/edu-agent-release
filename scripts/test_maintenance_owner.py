"""Exact host death proofs must never turn transport/identity uncertainty into completion."""
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('owner_gate',Path(__file__).resolve().parents[1]/'server/maintenance_gate.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


class OwnerProofTests(unittest.TestCase):
    def owner(self):
        return dict(containerId='a'*12,pid=123,processStartTicks='12345',bootId='9a740f91-6055-4a68-99ec-6ebdcd163537')

    def ops(self):return types.SimpleNamespace(DOCKER=['docker'],fixed_env=lambda:{})

    def container(self,running=True):
        return [dict(Id='a'*64,Config=dict(Labels={'com.docker.compose.project':'teaching-production',
                    'com.docker.compose.service':'api'}),State=dict(Running=running))]

    def test_live_identity_is_not_settled_and_pid_reuse_or_missing_process_is_proven(self):
        owner=self.owner()
        for proof,terminated in ((dict(alive=True,start=owner['processStartTicks'],boot=owner['bootId']),False),
                                 (dict(alive=True,start='99999',boot=owner['bootId']),True),
                                 (dict(alive=False),True)):
            results=[(0,('a'*64+'\n').encode()),(0,json.dumps(self.container()).encode()),(0,json.dumps(proof).encode())]
            with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run',side_effect=results):
                self.assertIs(gate._terminated(owner),terminated)

    def test_failed_inventory_malformed_owner_and_foreign_container_are_uncertain(self):
        owner=self.owner()
        for field,value in (('bootId',None),('processStartTicks',12345),('pid',True),('containerId','not-a-container')):
            with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run') as command, \
                 self.assertRaises(gate.GateRefused):
                gate._terminated(dict(owner,**{field:value}))
            command.assert_not_called()
        with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run',return_value=(1,b'')), \
             self.assertRaisesRegex(gate.GateRefused,'GATE_TERMINATION_PROOF_UNAVAILABLE'):
            gate._terminated(owner)
        container=self.container();container[0]['Config']['Labels']['com.docker.compose.project']='foreign'
        with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run',side_effect=[(0,('a'*64).encode()),(0,json.dumps(container).encode())]), \
             self.assertRaisesRegex(gate.GateRefused,'GATE_TERMINATION_PROOF_INVALID'):
            gate._terminated(owner)

    def test_absence_requires_successful_inventory_and_stopped_exact_container_is_proven(self):
        with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run',return_value=(0,b'')):
            self.assertTrue(gate._terminated(self.owner()))
        with patch.object(gate,'_ops',return_value=self.ops()),patch.object(gate,'_run',side_effect=[(0,('a'*64).encode()),(0,json.dumps(self.container(False)).encode())]):
            self.assertTrue(gate._terminated(self.owner()))


if __name__=='__main__':unittest.main()
