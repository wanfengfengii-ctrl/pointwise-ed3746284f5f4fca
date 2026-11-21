import json
import sqlite3
from contextlib import closing
from dataclasses import replace

from archive.catalog import find_shift_report
from archive.freeze import seal
from archive.readback import retrieve
from intake.receipts import find_request_receipt
from quality.grading import grade
from quality.limits import GradeDecision
from quality.reconcile import MissingRange
from recorder.endmark import load_endmark
from shift.closing import ShiftPhase, derive_shift_phase
from tests.fixtures import SHIFT, InjectedFailure, StationTestCase, fail_at_checkpoint


class HandoverTests(StationTestCase):
    def test_offline_readback(self):
        first = self.site.capture('L1', [0, 0], source='bench/zero')
        self.site.capture('L2', [-9, 3])
        self.site.capture('L3', [3])
        receipt = self.site.receive('take-zero', first)
        marks = self.site.stop_both()
        archived = seal(self.site.ledger, self.site.sources, SHIFT, reversed(marks))
        self.site.disconnect_sources()
        self.reopen()
        self.assertFalse(any(journal.path.exists() for journal in self.site.sources.values()))
        result = retrieve(self.site.ledger, archived.report_id)
        self.assertEqual(result.record, archived)
        left, right = result.record.report.devices
        self.assertEqual((left.endmark.last_sequence, right.endmark.last_sequence), (3, 0))
        self.assertEqual(left.missing, (MissingRange(2, 3),))
        self.assertEqual((right.entries, right.missing, right.rechecks), ((), (), ()))
        entry, = left.entries
        self.assertEqual((entry.peak.value, entry.peak.index, entry.decision), (0, 0, GradeDecision.PASS))
        self.assertEqual(left.rechecks, ())
        trace, = result.traces
        self.assertEqual((trace.receipt, trace.packet), (receipt, first))
        self.assertEqual((trace.packet.samples, trace.packet.source), ((0, 0), 'bench/zero'))

    def test_gap_ranges(self):
        left_accepted = self.site.capture('L1', [-9, 3])
        self.site.capture('L2', [2])
        self.site.capture('L3', [3])
        self.site.capture('R1', [0], device='R')
        self.site.capture('R2', [1], device='R')
        receipt = self.site.receive('take-L1', left_accepted)
        record = seal(self.site.ledger, self.site.sources, SHIFT, self.site.stop_both())
        left, right = record.report.devices
        self.assertEqual(tuple(entry.wave_id.sequence for entry in left.entries), (1,))
        self.assertEqual(left.missing, (MissingRange(2, 3),))
        self.assertEqual(right.missing, (MissingRange(1, 2),))
        self.assertEqual(right.entries, ())
        recheck, = left.rechecks
        self.assertEqual((recheck.receipt_id, recheck.peak.value, recheck.peak.index), (receipt.receipt_id, 9, 0))

    def test_fixed_endmark(self):
        self.assertEqual(derive_shift_phase(self.protocol, ()), ShiftPhase.RECORDING)
        packet = self.site.capture('L1', [1])
        mark = self.site.stop('L')
        self.assertEqual(derive_shift_phase(self.protocol, (mark,)), ShiftPhase.ONE_STOPPED)
        self.assertEqual(load_endmark(self.site.sources['L'], SHIFT), mark)
        self.assertIsNone(load_endmark(self.site.sources['R'], SHIFT))
        with self.assertRaises(ValueError):
            seal(self.site.ledger, self.site.sources, SHIFT, (mark,))
        with self.assertRaises(ValueError):
            self.site.capture('L2', [2])
        self.site.receive('L1-after-stop', packet)
        self.site.capture('R1', [3], device='R')
        right_mark = self.site.stop('R')
        self.assertEqual((mark.last_sequence, right_mark.last_sequence), (1, 1))
        self.assertEqual(derive_shift_phase(self.protocol, (right_mark, mark)), ShiftPhase.STOPPED)
        self.reopen()
        self.assertEqual(self.site.stop_both(), (mark, right_mark))
        with self.assertRaises(LookupError):
            self.site.stop('L', 'unopened')

    def test_empty_shift(self):
        marks = self.site.stop_both()
        self.assertEqual([mark.last_sequence for mark in marks], [0, 0])
        record = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        self.site.disconnect_sources()
        result = retrieve(self.reopen().ledger, record.report_id)
        self.assertEqual(result.traces, ())
        self.assertEqual([device.endmark.device_id for device in result.record.report.devices], ['L', 'R'])
        for device in result.record.report.devices:
            self.assertEqual((device.entries, device.rechecks, device.missing), ((), (), ()))

    def test_publish_failure(self):
        packet = self.site.capture('L1', [-9, 3])
        receipt = self.site.receive('take-L1', packet)
        marks = self.site.stop_both()
        captured_ids = []

        def fail(record):
            captured_ids.append(record.report_id)
            fail_at_checkpoint(record)

        with self.assertRaises(InjectedFailure):
            seal(self.site.ledger, self.site.sources, SHIFT, marks, before_commit=fail)
        self.reopen()
        candidate, = captured_ids
        self.assertIsNone(retrieve(self.site.ledger, candidate))
        with self.site.ledger.read_transaction() as connection:
            self.assertIsNone(find_shift_report(connection, SHIFT))
            self.assertEqual(find_request_receipt(connection, 'take-L1'), receipt)
        record = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        self.assertEqual(seal(self.site.ledger, self.site.sources, SHIFT, reversed(marks)), record)
        trace, = retrieve(self.site.ledger, record.report_id).traces
        self.assertEqual((trace.receipt, trace.packet), (receipt, packet))

    def test_snapshot_stability(self):
        first = self.site.capture('L1', [-9, 3], source='bench/before')
        tail = self.site.capture('L2', [4], source='bench/supplement')
        receipt = self.site.receive('take-L1', first)
        marks = self.site.stop_both()
        original = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        before = retrieve(self.site.ledger, original.report_id)
        supplement = self.site.receive('take-L2', tail)
        self.assertEqual(grade(self.site.ledger, supplement.receipt_id).peak.value, 4)
        self.assertEqual(seal(self.site.ledger, self.site.sources, SHIFT, marks), original)
        self.site.disconnect_sources()
        after = retrieve(self.reopen().ledger, original.report_id)
        self.assertEqual(after, before)
        trace, = after.traces
        self.assertEqual((trace.receipt, trace.packet), (receipt, first))
        self.assertEqual(after.record.report.devices[0].missing, (MissingRange(2, 2),))

    def test_endmark_binding(self):
        old_packet = self.site.capture('L1', [1])
        old_receipt = self.site.receive('old-first', old_packet)
        old_marks = self.site.stop_both()
        old = seal(self.site.ledger, self.site.sources, SHIFT, old_marks)
        next_shift = 'morning-02'
        self.site.open(next_shift)
        self.site.capture('L1', [2], shift_id=next_shift)
        new_left, new_right = self.site.stop_both(next_shift)
        self.assertEqual(old_marks[0].last_sequence, new_left.last_sequence)
        invalid = ((old_marks[0], new_right), (new_left, new_left), (new_right,),
                   (replace(new_left, last_sequence=0), new_right))
        for marks in invalid:
            with self.subTest(marks=marks), self.assertRaises(ValueError):
                seal(self.site.ledger, self.site.sources, next_shift, marks)
        bad_sources = {'L': self.site.sources['R'], 'R': self.site.sources['L']}
        with self.assertRaises(ValueError):
            seal(self.site.ledger, bad_sources, next_shift, (new_left, new_right))
        with self.site.ledger.read_transaction() as connection:
            self.assertIsNone(find_shift_report(connection, next_shift))
        with self.assertRaises(ValueError):
            seal(self.site.ledger, self.site.sources, SHIFT, (new_left, old_marks[1]))
        self.site.disconnect_sources()
        result = retrieve(self.reopen().ledger, old.report_id)
        self.assertEqual(result.record, old)
        self.assertEqual(result.traces[0].receipt, old_receipt)
        self.assertEqual(result.traces[0].packet, old_packet)

    def test_readback_errors(self):
        left = self.site.receive('take-L1', self.site.capture('L1', [1]))
        right = self.site.receive('take-R1', self.site.capture('R1', [2], device='R'))
        report = seal(self.site.ledger, self.site.sources, SHIFT, self.site.stop_both())
        self.assertIsNone(retrieve(self.site.ledger, 999))
        self.assertEqual([trace.receipt for trace in retrieve(self.site.ledger, report.report_id).traces], [left, right])
        with closing(sqlite3.connect(self.site.ledger.path)) as connection:
            connection.execute('PRAGMA foreign_keys = OFF')
            with connection:
                stored, = connection.execute('SELECT snapshot FROM archive_reports WHERE report_id = ?', (report.report_id,)).fetchone()
                corrupted = json.loads(stored)
                corrupted['devices'][0]['entries'][0]['receipt_id'] = right.receipt_id
                connection.execute('UPDATE archive_reports SET snapshot = ? WHERE report_id = ?',
                                   (json.dumps(corrupted), report.report_id))
            with self.assertRaisesRegex(RuntimeError, '身份不符'):
                retrieve(self.site.ledger, report.report_id)
            with connection:
                connection.execute('UPDATE archive_reports SET snapshot = ? WHERE report_id = ?', (stored, report.report_id))
                connection.execute('DELETE FROM originals WHERE device_id = ?', ('L',))
            with self.assertRaisesRegex(RuntimeError, '原件缺失'):
                retrieve(self.site.ledger, report.report_id)
            with connection:
                connection.execute('DELETE FROM receipts WHERE receipt_id = ?', (left.receipt_id,))
            with self.assertRaisesRegex(RuntimeError, '首执.*缺失'):
                retrieve(self.site.ledger, report.report_id)

    def test_formal_intake_counts_before_source_release(self):
        packet = self.site.capture('L1', [-9, 3])
        with self.assertRaises(InjectedFailure):
            self.site.receive('take-L1', packet, before_release=fail_at_checkpoint)
        self.assertEqual(self.site.peek(), packet)
        self.assertEqual(self.site.release_count(), 0)
        report = seal(self.site.ledger, self.site.sources, SHIFT, self.site.stop_both())
        self.assertEqual(report.report.devices[0].missing, ())
        self.assertEqual(len(report.report.devices[0].rechecks), 1)
        self.site.disconnect_sources()
        trace, = retrieve(self.reopen().ledger, report.report_id).traces
        self.assertEqual(trace.packet, packet)
