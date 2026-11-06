import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tencent_production as controller


class TimingTests(unittest.TestCase):
    def value(self):
        return dict(schemaVersion=1,phases=[dict(name='worker-drain',durationMs=123)],
                    totalDurationMs=200,maintenanceDurationMs=150)

    def test_public_summary_rejects_private_strings_unknown_fields_and_unbounded_values(self):
        good=self.value()
        with patch.object(controller,'status') as output:
            controller.report_timing_summary(good)
        self.assertEqual(len(output.call_args_list),2)
        for change in (lambda x:x.update(secret='SENTINEL'),lambda x:x['phases'][0].update(name='SENTINEL'),
                       lambda x:x['phases'][0].update(name=['private']),
                       lambda x:x['phases'][0].update(durationMs=True),lambda x:x.update(totalDurationMs=10800001),
                       lambda x:x.update(maintenanceDurationMs=float('nan'))):
            value=copy.deepcopy(good);change(value)
            with patch.object(controller,'status') as output:
                controller.report_timing_summary(value)
                output.assert_not_called()

    def test_phase_timing_does_not_consume_control_flow_deadline_clock(self):
        spec=importlib.util.spec_from_file_location('timing_receiver',Path(__file__).resolve().parents[1]/'server/tencent_receiver.py')
        receiver=importlib.util.module_from_spec(spec);spec.loader.exec_module(receiver)
        with patch.object(receiver.time,'monotonic',side_effect=AssertionError('control clock consumed')), \
             patch.object(receiver.time,'perf_counter',side_effect=[0,1,2,3]), \
             patch.object(receiver,'atomic',side_effect=OSError('disk write failed')):
            receiver.run_dir=Path('/private/receipt')
            receiver.timing_start();receiver.release_phase('drain');receiver.release_phase('worker-drain')
            result=receiver.attach_timings({'status':'deployed'})
        self.assertEqual(result['status'],'deployed')
        self.assertEqual([x['name'] for x in result['timingSummary']['phases']],['drain','worker-drain'])
        self.assertEqual(result['timingSummary']['totalDurationMs'],3000)


if __name__=='__main__':unittest.main()
