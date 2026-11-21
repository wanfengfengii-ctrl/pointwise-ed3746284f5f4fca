import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from station_demo import run_day


class DaybookTests(unittest.TestCase):
    def test_complete_sample_day(self):
        expected_grades = [
            ['L', 1, 8, 0, 'pass'],
            ['L', 2, 9, 0, 'recheck'],
            ['L', 3, 0, 0, 'pass'],
            ['R', 1, 5, 0, 'pass'],
        ]
        expected_handover = {
            'received': {'L': [1, 2, 3], 'R': [1]},
            'endmarks': {'L': 4, 'R': 1},
            'missing': {'L': [[4, 4]], 'R': []},
            'rechecks': {'L': [2], 'R': []},
        }
        expected_originals = [
            ['L1', [-8, 8, 3], '试片A/左规/首载'],
            ['L2', [-9, 3], '试片A/左规/加荷'],
            ['L3', [0, 0], '试片A/左规/卸荷零点'],
            ['R1', [5, -2], '试片A/右规/首载'],
        ]
        with TemporaryDirectory(prefix='strain-daybook-') as directory:
            workspace = Path(directory)
            result = run_day(workspace)
            self.assertFalse((workspace / 'left.sqlite3').exists())
            self.assertFalse((workspace / 'right.sqlite3').exists())
            self.assertTrue((workspace / 'left.sqlite3.offline').is_file())
            self.assertTrue((workspace / 'right.sqlite3.offline').is_file())

        actual_grades = [
            [item.receipt.wave_id.device_id, item.receipt.wave_id.sequence,
             item.peak.value, item.peak.index, item.decision.value]
            for item in result.grades
        ]
        report = result.handover.record.report
        self.assertEqual((report.shift_id, report.channel, report.threshold, report.unit), ('strain-day-01', 'gauge-A', 8, 'μɛ'))
        self.assertEqual([item.endmark.device_id for item in report.devices], ['L', 'R'])
        devices = {item.endmark.device_id: item for item in report.devices}
        actual_handover = {
            'received': {name: [entry.wave_id.sequence for entry in item.entries] for name, item in devices.items()},
            'endmarks': {name: item.endmark.last_sequence for name, item in devices.items()},
            'missing': {name: [[gap.first, gap.last] for gap in item.missing] for name, item in devices.items()},
            'rechecks': {name: [entry.wave_id.sequence for entry in item.rechecks] for name, item in devices.items()},
        }
        actual_originals = [[trace.packet.action_id, list(trace.packet.samples), trace.packet.source]
                            for trace in result.handover.traces]
        self.assertEqual(actual_grades, expected_grades)
        self.assertEqual(actual_handover, expected_handover)
        self.assertEqual(actual_originals, expected_originals)
        self.assertEqual([trace.receipt for trace in result.handover.traces], [item.receipt for item in result.grades])
        scenario = {
            'case_id': 'v7',
            'input': {'samples': {row[0]: row[1] for row in actual_originals},
                      'left_held': {'action': 'L4', 'samples': [2]}, 'threshold': report.threshold},
            'expected': expected_grades,
            'actual': actual_grades,
            'downstream_expected': {'handover': expected_handover, 'originals': expected_originals},
            'downstream_actual': {'handover': actual_handover, 'originals': actual_originals},
            'code_paths': ['quality/peaks.py', 'quality/limits.py', 'quality/reconcile.py',
                           'archive/freeze.py', 'archive/readback.py', 'archive/trace.py'],
            'oracle': '按样例手算各件绝对峰值与首峰位置，比较门槛 8，再从末序区间扣除实际交原位置并核对夜班原件。',
        }
        print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False, separators=(',', ':')))
