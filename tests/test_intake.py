import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from intake.accept import take_one
from intake.originals import load_original
from intake.receipts import find_request_receipt, find_wave_receipt, list_shift_receipts
from intake.validation import HeadChanged, TakeOneRequest
from quality.grading import grade
from quality.limits import GradeDecision
from recorder.release import ReleaseEvidence, release_head
from tests.fixtures import SHIFT, Checkpoint, InjectedFailure, StationTestCase, fail_at_checkpoint


class IntakeTests(StationTestCase):
    def test_accept_and_grade(self):
        packet = self.site.capture('L1', [-8, 8, 3], source='bench/left/first')
        receipt = self.site.receive('take-L1', packet)
        self.assertEqual(receipt.wave_id, packet.wave_id)
        self.assertEqual(receipt.request_id, 'take-L1')
        self.assertIsNone(self.site.peek())
        with self.site.ledger.read_transaction() as connection:
            self.assertEqual(load_original(connection, packet.wave_id), packet)
            self.assertEqual(find_wave_receipt(connection, packet.wave_id), receipt)
        result = grade(self.site.ledger, receipt.receipt_id)
        self.assertEqual(result.receipt, receipt)
        self.assertEqual(result.protocol, self.protocol)
        self.assertEqual((result.peak.value, result.peak.index, result.decision), (8, 0, GradeDecision.PASS))

    def test_over_limit(self):
        big = 2 ** 80
        inputs = [([-8, 8, 3], 8, 0, GradeDecision.PASS),
                  ([-9, 3], 9, 0, GradeDecision.RECHECK),
                  ([0, 0], 0, 0, GradeDecision.PASS),
                  ([1, -big, big], big, 1, GradeDecision.RECHECK)]
        for index, (samples, peak, position, decision) in enumerate(inputs):
            with self.subTest(index=index):
                packet = self.site.capture(f'packet-{index}', samples)
                receipt = self.site.receive(f'receipt-{index}', packet)
                result = grade(self.site.ledger, receipt.receipt_id)
                self.assertEqual((result.peak.value, result.peak.index, result.decision), (peak, position, decision))
                with self.site.ledger.read_transaction() as connection:
                    self.assertEqual(load_original(connection, receipt.wave_id).samples, tuple(samples))

    def test_missing_original(self):
        with self.assertRaises(LookupError):
            grade(self.site.ledger, 412)
        packet = self.site.capture('L1', [2])
        receipt = self.site.receive('take-L1', packet)
        with closing(sqlite3.connect(self.site.ledger.path)) as connection, connection:
            connection.execute('PRAGMA foreign_keys = OFF')
            connection.execute('DELETE FROM originals WHERE shift_id = ?', (SHIFT,))
        with self.assertRaisesRegex(RuntimeError, '原件缺失'):
            grade(self.site.ledger, receipt.receipt_id)

    def test_wrong_head(self):
        first = self.site.capture('L7', [-8, 3])
        successor = self.site.capture('L8', [2])
        with self.assertRaises(HeadChanged):
            self.site.receive('skip', successor)
        with self.site.ledger.read_transaction() as connection:
            self.assertIsNone(find_request_receipt(connection, 'skip'))
            self.assertIsNone(load_original(connection, successor.wave_id))
        self.assertEqual(self.site.peek(), first)
        self.assertEqual(self.site.receive('correct', first).wave_id, first.wave_id)
        self.assertEqual(self.site.peek(), successor)

    def test_wrong_binding(self):
        left = self.site.capture('L7', [-8, 3])
        successor = self.site.capture('L8', [1])
        right = self.site.capture('R9', [5, -2], device='R')
        self.site.open('another')
        attempts = (
            TakeOneRequest('bad-device', SHIFT, 'L', right.wave_id),
            TakeOneRequest('bad-shift', 'another', 'L', left.wave_id),
            TakeOneRequest('bad-port', SHIFT, 'R', right.wave_id),
        )
        for request in attempts:
            with self.subTest(request=request.request_id), self.assertRaises(ValueError):
                take_one(self.site.ledger, self.site.sources['L'], request)
        with self.site.ledger.read_transaction() as connection:
            self.assertEqual(list_shift_receipts(connection, SHIFT), ())
        self.assertEqual(self.site.peek('R'), right)
        receipt = self.site.receive('original-request', left)
        with self.assertRaises(ValueError):
            self.site.receive('original-request', successor)
        for evidence in (ReleaseEvidence(receipt.receipt_id, receipt.request_id, successor.wave_id),
                         ReleaseEvidence(receipt.receipt_id, 'other-request', left.wave_id),
                         ReleaseEvidence(999, 'missing', successor.wave_id)):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                release_head(self.site.ledger, self.site.sources['L'], evidence)
        self.assertEqual(self.site.peek(), successor)
        self.assertEqual(self.site.release_count(), 1)

    def test_commit_failure(self):
        packet = self.site.capture('L7', [-8, 3])
        next_packet = self.site.capture('L8', [2])
        with self.assertRaises(InjectedFailure):
            self.site.receive('first', packet, before_commit=fail_at_checkpoint)
        self.reopen()
        with self.site.ledger.read_transaction() as connection:
            self.assertIsNone(find_request_receipt(connection, 'first'))
            self.assertIsNone(load_original(connection, packet.wave_id))
            self.assertEqual(list_shift_receipts(connection, SHIFT), ())
        self.assertEqual(self.site.release_count(), 0)
        self.assertEqual(self.site.peek(), packet)
        receipt = self.site.receive('first', packet)
        self.assertEqual(self.site.peek(), next_packet)
        with self.site.ledger.read_transaction() as connection:
            self.assertEqual(list_shift_receipts(connection, SHIFT), (receipt,))
            self.assertEqual(load_original(connection, packet.wave_id), packet)

    def test_lost_release_reply(self):
        packet = self.site.capture('L7', [-8, 3])
        next_packet = self.site.capture('L8', [2])
        seen = []

        def lose_reply(receipt):
            seen.append(receipt)
            fail_at_checkpoint(receipt)

        with self.assertRaises(InjectedFailure):
            self.site.receive('first', packet, after_release=lose_reply)
        self.reopen()
        self.assertEqual(self.site.peek(), next_packet)
        original, = seen
        self.assertEqual(self.site.receive('first', packet), original)
        self.assertEqual(self.site.receive('first', packet), original)
        old_evidence = ReleaseEvidence(original.receipt_id, original.request_id, original.wave_id)
        self.assertEqual(release_head(self.site.ledger, self.site.sources['L'], old_evidence), old_evidence)
        self.assertEqual(self.site.peek(), next_packet)
        self.assertEqual(self.site.release_count(), 1)
        self.assertEqual(self.site.receive('second', next_packet).wave_id, next_packet.wave_id)
        self.assertEqual(release_head(self.site.ledger, self.site.sources['L'], old_evidence), old_evidence)
        self.assertEqual(self.site.release_count(), 2)
        self.assertIsNone(self.site.peek())

    def test_pending_release(self):
        first = self.site.capture('L7', [-8, 3])
        successor = self.site.capture('L8', [2])
        with self.assertRaises(InjectedFailure):
            self.site.receive('first', first, before_release=fail_at_checkpoint)
        self.reopen()
        self.assertEqual(self.site.peek(), first)
        self.assertEqual(self.site.release_count(), 0)
        with self.site.ledger.read_transaction() as connection:
            original = find_request_receipt(connection, 'first')
            self.assertEqual(load_original(connection, first.wave_id), first)
        self.assertEqual(grade(self.site.ledger, original.receipt_id).peak.value, 8)
        with self.assertRaises(ValueError):
            self.site.receive('competing', first)
        self.assertEqual(self.site.receive('first', first), original)
        self.assertEqual(self.site.peek(), successor)
        self.assertEqual(self.site.release_count(), 1)

    def test_preview_does_not_reserve(self):
        left = self.site.capture('L7', [-8, 3])
        right = self.site.capture('R9', [5, -2], device='R')
        successor = self.site.capture('R10', [1], device='R')
        saved_left, saved_right = self.site.peek(), self.site.peek('R')
        self.assertEqual((saved_left, saved_right), (left, right))
        winner = self.site.receive('right-duty', right)
        with self.assertRaises(HeadChanged):
            self.site.receive('stale-right', saved_right)
        with self.site.ledger.read_transaction() as connection:
            self.assertEqual(list_shift_receipts(connection, SHIFT), (winner,))
        self.assertEqual(self.site.peek('R'), successor)
        self.assertEqual(self.site.receive('left-duty', saved_left).wave_id, left.wave_id)

    def test_append_during_station_commit_checkpoint(self):
        first = self.site.capture('L7', [-8, 3])
        checkpoint = Checkpoint()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.site.receive, 'first', first, before_commit=checkpoint)
            try:
                self.assertTrue(checkpoint.reached.wait(5))
                with self.site.ledger.read_transaction() as connection:
                    self.assertIsNone(find_request_receipt(connection, 'first'))
                added = self.site.capture('L8', [2])
                right = self.site.capture('R9', [5, -2], device='R')
                self.assertEqual(self.site.peek(), first)
                self.assertEqual(self.site.peek('R'), right)
            finally:
                checkpoint.resume.set()
            self.assertEqual(future.result(timeout=5), checkpoint.value)
        self.assertEqual(self.site.peek(), added)
