import json

from archive.custody import issue_ticket, read_ticket
from archive.freeze import seal
from archive.readback import retrieve
from quality.grading import grade
from recorder.queue import peek
from recorder.retention import ReclaimResult, configure_capacity, reclaim, usage
from tests.fixtures import SHIFT, InjectedFailure, StationTestCase, fail_at_checkpoint


class CapacityTests(StationTestCase):
    def test_configure_usage_and_unlimited_legacy(self):
        self.site.capture('L1', [1, 2])
        self.site.capture('L2', [3, 4, 5, 6])
        self.assertEqual(usage(self.site.sources['L']), 6)
        configure_capacity(self.site.sources['L'], 6)
        # Repeating the same value is fine.
        configure_capacity(self.site.sources['L'], 6)
        with self.assertRaises(ValueError):
            configure_capacity(self.site.sources['L'], 5)
        for bad in (0, -1, 1.5, True, '6', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                configure_capacity(self.site.sources['L'], bad)
        # Right device was never registered: it stays unlimited.
        right = self.site.capture('R1', [1], device='R')
        self.assertEqual(usage(self.site.sources['R']), 1)
        self.assertEqual(right.samples, (1,))

    def test_capacity_enforced_before_commit_and_no_sequence_burned(self):
        journal = self.site.sources['L']
        configure_capacity(journal, 3)
        self.site.capture('L1', [1, 2])
        with self.assertRaises(ValueError):
            self.site.capture('L2', [3, 4])
        with journal.transaction(write=False) as connection:
            self.assertEqual(journal.last_sequence(connection, SHIFT), 1)
        # A smaller wave that fits still gets the next sequence.
        accepted = self.site.capture('L2', [3])
        self.assertEqual(accepted.wave_id.sequence, 2)
        self.assertEqual(usage(journal), 3)

    def test_replay_does_not_consume_capacity_or_queue(self):
        journal = self.site.sources['L']
        configure_capacity(journal, 4)
        original = self.site.capture('L7', [-8, 3, 1, 2])
        self.assertEqual(usage(journal), 4)
        replay = self.site.capture('L7', [-8, 3, 1, 2], source='bench/L/L7')
        self.assertEqual(replay, original)
        self.assertEqual(usage(journal), 4)
        with journal.transaction(write=False) as connection:
            self.assertEqual(journal.last_sequence(connection, SHIFT), 1)


class _CustodyFixture(StationTestCase):
    def _build_released_left(self, *, l1_samples=(-8, 8), l2_samples=(-9, 3, 1, 2),
                             extra_held=False, capacity=6):
        left = self.site.sources['L']
        if extra_held:
            capacity = max(capacity, 7)
        configure_capacity(left, capacity)
        l1 = self.site.capture('L1', list(l1_samples), source='bench/L/L1')
        l2 = self.site.capture('L2', list(l2_samples), source='bench/L/L2')
        held = self.site.capture('L3', [7], source='bench/L/L3') if extra_held else None
        receipt_one = self.site.receive('take-L1', l1)
        receipt_two = self.site.receive('take-L2', l2)
        self.assertEqual(usage(left), 6 if not extra_held else 7)
        marks = self.site.stop_both()
        archive = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        return left, l1, l2, receipt_one, receipt_two, archive, held

    def _tickets(self, archive, receipt_one, receipt_two):
        ticket_one = issue_ticket(
            self.site.ledger, 'T1', archive.report_id, 'L', [receipt_one.receipt_id])
        ticket_two = issue_ticket(
            self.site.ledger, 'T2', archive.report_id, 'L',
            [receipt_one.receipt_id, receipt_two.receipt_id])
        return ticket_one, ticket_two


class TicketIssueTests(_CustodyFixture):
    def test_ticket_shape_and_persistence(self):
        left, l1, l2, r1, r2, archive, _ = self._build_released_left()
        ticket = issue_ticket(self.site.ledger, 'T1', archive.report_id, 'L', [r1.receipt_id])
        self.assertEqual(set(ticket), {'ticket_id', 'report_id', 'shift_id', 'device_id', 'members'})
        self.assertEqual((ticket['ticket_id'], ticket['report_id'],
                          ticket['shift_id'], ticket['device_id']),
                         ('T1', archive.report_id, SHIFT, 'L'))
        member, = ticket['members']
        self.assertEqual(member['wave_id'], {
            'shift_id': SHIFT, 'device_id': 'L', 'sequence': l1.wave_id.sequence})
        self.assertEqual(member['sample_count'], 2)
        self.assertEqual(len(member['body_digest']), 64)
        # JSON-safe across two SQLite databases.
        json.loads(json.dumps(ticket))
        self.assertEqual(read_ticket(self.site.ledger, 'T1'), ticket)
        self.assertIsNone(read_ticket(self.site.ledger, 'unknown'))

    def test_idempotent_issue_rejects_changed_params(self):
        _, _, _, r1, r2, archive, _ = self._build_released_left()
        ticket = issue_ticket(self.site.ledger, 'T1', archive.report_id, 'L', [r1.receipt_id])
        self.assertEqual(
            issue_ticket(self.site.ledger, 'T1', archive.report_id, 'L', [r1.receipt_id]),
            ticket)
        with self.assertRaises(ValueError):
            issue_ticket(self.site.ledger, 'T1', archive.report_id, 'L',
                         [r1.receipt_id, r2.receipt_id])

    def test_only_receipts_inside_fixed_report(self):
        _, l1, _, r1, _, archive, held = self._build_released_left(extra_held=True)
        # A third wave still sits on the device but was never received, so it cannot be signed.
        self.assertIsNotNone(held)
        with self.assertRaises(ValueError):
            issue_ticket(self.site.ledger, 'T9', archive.report_id, 'L', [99])
        # Right device has no entries in this report.
        with self.assertRaises(ValueError):
            issue_ticket(self.site.ledger, 'TR', archive.report_id, 'R', [r1.receipt_id])
        with self.assertRaises(ValueError):
            issue_ticket(self.site.ledger, 'TB', 412, 'L', [r1.receipt_id])
        for bad_ids in ([], [r1.receipt_id, r1.receipt_id], [r1.receipt_id - 1, r1.receipt_id]
                        if r1.receipt_id > 1 else [r1.receipt_id + 1, r1.receipt_id]):
            with self.subTest(bad_ids=bad_ids), self.assertRaises(ValueError):
                issue_ticket(self.site.ledger, 'TBAD', archive.report_id, 'L', bad_ids)

    def test_late_supplement_cannot_join_sealed_report(self):
        _, _, _, r1, _, archive, held = self._build_released_left(extra_held=True)
        # Supplemental intake after sealing is graded but stays out of the fixed report.
        late = self.site.receive('late-L3', held)
        self.assertEqual(grade(self.site.ledger, late.receipt_id).receipt, late)
        with self.assertRaises(ValueError):
            issue_ticket(self.site.ledger, 'TLATE', archive.report_id, 'L',
                         [r1.receipt_id, late.receipt_id])


class ReclaimTests(_CustodyFixture):
    def test_hand_calc_t1_then_t2(self):
        left, l1, l2, r1, r2, archive, _ = self._build_released_left()
        ticket_one, ticket_two = self._tickets(archive, r1, r2)

        first = reclaim(left, ticket_one)
        self.assertEqual(first, {'reclaimed_samples': 2, 'receipt_ids': [r1.receipt_id]})
        self.assertEqual(usage(left), 4)
        # Re-sending the same ticket replays the original result.
        self.assertEqual(reclaim(left, ticket_one), first)
        self.assertEqual(usage(left), 4)

        second = reclaim(left, ticket_two)
        self.assertEqual(second, {'reclaimed_samples': 4,
                                  'receipt_ids': [r1.receipt_id, r2.receipt_id]})
        self.assertEqual(usage(left), 0)
        # Replays keep reporting the first-time reclaimed counts even after overlap.
        self.assertEqual(reclaim(left, ticket_one), first)
        self.assertEqual(reclaim(left, ticket_two), second)
        self.assertEqual(usage(left), 0)

    def test_hand_calc_reverse_order(self):
        left, _, _, r1, r2, archive, _ = self._build_released_left()
        ticket_one, ticket_two = self._tickets(archive, r1, r2)
        self.assertEqual(reclaim(left, ticket_two)['reclaimed_samples'], 6)
        self.assertEqual(usage(left), 0)
        zero = reclaim(left, ticket_one)
        self.assertEqual(zero, {'reclaimed_samples': 0, 'receipt_ids': [r1.receipt_id]})
        self.assertEqual(reclaim(left, ticket_two)['reclaimed_samples'], 6)
        self.assertEqual(reclaim(left, ticket_one), zero)

    def test_reclaimed_action_replays_original_identity(self):
        left, l1, l2, r1, r2, archive, _ = self._build_released_left()
        ticket_one, ticket_two = self._tickets(archive, r1, r2)
        reclaim(left, ticket_one)
        reclaim(left, ticket_two)
        # Exact replay restores the equivalent packet without capacity or queue space.
        replayed = self.site.capture('L1', [-8, 8], source='bench/L/L1')
        self.assertEqual(replayed, l1)
        self.assertEqual(usage(left), 0)
        self.assertIsNone(peek(self.site.ledger, left, SHIFT))
        with left.transaction(write=False) as connection:
            self.assertEqual(left.last_sequence(connection, SHIFT), 2)
        # Changed body or source is still rejected.
        with self.assertRaises(ValueError):
            self.site.capture('L1', [-8, 9], source='bench/L/L1')
        with self.assertRaises(ValueError):
            self.site.capture('L1', [-8, 8], source='bench/L/other')

    def test_station_readback_without_device_and_new_collection(self):
        left, l1, l2, r1, r2, archive, _ = self._build_released_left()
        ticket_one, ticket_two = self._tickets(archive, r1, r2)
        reclaim(left, ticket_one)
        reclaim(left, ticket_two)
        self.site.disconnect_sources()
        reopened = self.reopen()
        result = retrieve(reopened.ledger, archive.report_id)
        self.assertEqual(result.record, archive)
        packets = {trace.packet.action_id: trace.packet for trace in result.traces}
        self.assertEqual(packets['L1'].samples, (-8, 8))
        self.assertEqual(packets['L2'].samples, (-9, 3, 1, 2))
        self.assertEqual(packets['L1'].source, 'bench/L/L1')
        # Old report and receipts are unchanged.
        self.assertEqual([trace.receipt.receipt_id for trace in result.traces],
                         [r1.receipt_id, r2.receipt_id])

        # Device comes back; new work fits in the reclaimed space.
        for journal in self.site.sources.values():
            journal.path.with_suffix('.sqlite3.offline').rename(journal.path)
        next_shift = 'morning-02'
        self.site.open(next_shift)
        new_packet = self.site.capture(
            'N1', [3, -3], source='bench/L/new', shift_id=next_shift)
        self.assertEqual(new_packet.wave_id.sequence, 1)
        self.assertEqual(usage(self.site.sources['L']), 2)
        marks = self.site.stop_both(next_shift)
        new_archive = seal(self.site.ledger, self.site.sources, next_shift, marks)
        self.assertNotEqual(new_archive.report_id, archive.report_id)
        self.assertEqual(retrieve(self.site.ledger, archive.report_id).record, archive)

    def test_reclaim_validation_and_rollback(self):
        left, l1, l2, r1, r2, archive, _ = self._build_released_left()
        ticket_one, ticket_two = self._tickets(archive, r1, r2)

        wrong_device = dict(ticket_one, device_id='R')
        with self.assertRaises(ValueError):
            reclaim(left, wrong_device)
        tampered_digest = json.loads(json.dumps(ticket_one))
        tampered_digest['members'][0]['body_digest'] = 'f' * 64
        with self.assertRaises(ValueError):
            reclaim(left, tampered_digest)
        tampered_count = json.loads(json.dumps(ticket_one))
        tampered_count['members'][0]['sample_count'] = 3
        with self.assertRaises(ValueError):
            reclaim(left, tampered_count)
        duplicate = json.loads(json.dumps(ticket_two))
        duplicate['members'][1] = dict(duplicate['members'][0])
        with self.assertRaises(ValueError):
            reclaim(left, duplicate)
        with self.assertRaises(ValueError):
            reclaim(left, {'ticket_id': 'X', 'report_id': 1, 'shift_id': SHIFT,
                           'device_id': 'L', 'members': []})

        # Pre-commit failure rolls the whole operation back.
        seen = []
        def fail(result):
            self.assertIsInstance(result, ReclaimResult)
            seen.append(result)
            fail_at_checkpoint(result)
        with self.assertRaises(InjectedFailure):
            reclaim(left, ticket_two, before_commit=fail)
        self.reopen()
        self.assertEqual(usage(self.site.sources['L']), 6)
        with self.site.sources['L'].transaction(write=False) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM reclaims').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM captures WHERE body_cleared = 1").fetchone()[0], 0)
        # The untouched ticket can now be processed normally.
        outcome = reclaim(self.site.sources['L'], ticket_two)
        self.assertEqual(outcome['reclaimed_samples'], 6)

    def test_unreleased_member_rejected_before_any_clear(self):
        left = self.site.sources['L']
        configure_capacity(left, 6)
        l1 = self.site.capture('L1', [-8, 8], source='bench/L/L1')
        l2 = self.site.capture('L2', [-9, 3, 1, 2], source='bench/L/L2')
        receipt_one = self.site.receive('take-L1', l1)
        marks = self.site.stop_both()
        archive = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        valid = issue_ticket(self.site.ledger, 'T1', archive.report_id, 'L',
                             [receipt_one.receipt_id])
        # Forge a ticket that names the unreleased L2 identity.
        forged = json.loads(json.dumps(valid))
        forged['ticket_id'] = 'FORGE'
        forged['members'] = [{
            'receipt_id': 77,
            'wave_id': {'shift_id': SHIFT, 'device_id': 'L', 'sequence': l2.wave_id.sequence},
            'sample_count': 4,
            'body_digest': valid['members'][0]['body_digest'],
        }]
        with self.assertRaises(ValueError):
            reclaim(left, forged)
        self.assertEqual(usage(left), 6)
        with left.transaction(write=False) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM captures WHERE body_cleared = 1").fetchone()[0], 0)
        # The valid ticket still processes after the rejected attempt.
        self.assertEqual(reclaim(left, valid)['reclaimed_samples'], 2)
        self.assertEqual(usage(left), 4)
