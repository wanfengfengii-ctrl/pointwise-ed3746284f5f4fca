from archive.freeze import seal
from archive.readback import retrieve
from quality.grading import grade
from recorder.capture import capture
from recorder.journal import DeviceJournal
from recorder.queue import peek
from shift.opening import load_protocol
from shift.protocol import MICROSTRAIN_UNIT
from tests.fixtures import SHIFT, StationTestCase


class RecordingTests(StationTestCase):
    def test_open_capture_peek(self):
        self.assertEqual(self.protocol.roster.devices, ('L', 'R'))
        self.assertEqual((self.protocol.channel, self.protocol.threshold, self.protocol.unit), ('gauge-1', 8, MICROSTRAIN_UNIT))
        self.assertIsNone(self.site.peek())
        left = self.site.capture('L1', [-8, 8, 3], source='试片原件/左')
        right = self.site.capture('R1', [5, -2], device='R', source='试片原件/右')
        self.assertEqual((left.wave_id.sequence, right.wave_id.sequence), (1, 1))
        self.assertEqual(left.wave_id.shift_id, SHIFT)
        self.assertEqual(left.wave_id.device_id, 'L')
        self.assertEqual((left.samples, left.source), ((-8, 8, 3), '试片原件/左'))
        self.assertEqual((right.samples, right.source), ((5, -2), '试片原件/右'))
        self.assertEqual(self.site.peek(), left)
        later = self.site.capture('L2', [4])
        self.assertEqual(later.wave_id.sequence, 2)
        self.assertEqual(self.site.peek(), left)
        self.assertEqual(self.site.peek('R'), right)
        self.assertEqual(self.site.release_count(), 0)

    def test_conflicting_replay(self):
        original = self.site.capture('L7', [-8, 3], source='left/raw')
        for samples, source in (([8, 3], 'left/raw'), ([3, -8], 'left/raw'), ([-8, 3], 'other')):
            with self.subTest(samples=samples, source=source), self.assertRaises(ValueError):
                self.site.capture('L7', samples, source=source)
        self.assertEqual(self.site.peek(), original)
        self.assertEqual(self.site.peek().samples, (-8, 3))
        self.assertEqual(self.site.capture('L7', (-8, 3), source='left/raw'), original)
        self.assertEqual(self.site.capture('L8', [1]).wave_id.sequence, 2)

    def test_open_replay(self):
        original = self.site.capture('existing', [-8, 3])
        self.reopen()
        self.assertEqual(self.site.open(), self.protocol)
        for changes in ({'threshold': 9}, {'channel': 'new-channel'}, {'devices': ('R', 'L')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.site.open(**changes)
        with self.site.ledger.read_transaction() as connection:
            self.assertEqual(load_protocol(connection, SHIFT), self.protocol)
        self.assertEqual(self.site.peek(), original)

        for changes in ({'threshold': True}, {'threshold': -1}, {'threshold': 1.5},
                        {'devices': ('L', 'L')}, {'devices': ('L', '')}, {'channel': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.site.open('invalid', **changes)
        with self.site.ledger.read_transaction() as connection, self.assertRaises(LookupError):
            load_protocol(connection, 'invalid')

    def test_initialization(self):
        packet = self.site.capture('kept', [-10, 2])
        receipt = self.site.receive('take-kept', packet)
        queued = self.site.capture('queued', [3])
        marks = self.site.stop_both()
        archived = seal(self.site.ledger, self.site.sources, SHIFT, marks)
        self.reopen().initialize()
        self.site.initialize()
        self.assertEqual(self.site.open(), self.protocol)
        self.assertEqual(self.site.stop_both(), marks)
        self.assertEqual(self.site.peek(), queued)
        self.assertEqual(self.site.capture('kept', [-10, 2]), packet)
        self.assertEqual(self.site.receive('take-kept', packet), receipt)
        self.assertEqual(self.site.release_count(), 1)
        self.assertEqual(grade(self.site.ledger, receipt.receipt_id).peak.value, 10)
        self.assertEqual(retrieve(self.site.ledger, archived.report_id).record, archived)
        with self.assertRaises(ValueError):
            DeviceJournal(self.site.sources['L'].path, 'R').initialize()
        self.assertEqual(self.site.peek(), queued)

    def test_independent_sequence(self):
        left = self.site.capture('a', [2])
        added = self.site.capture('b', [4])
        right = self.site.capture('a', [3], device='R')
        self.site.open('evening')
        evening = self.site.capture('a', [7], shift_id='evening')
        self.assertEqual([p.wave_id.sequence for p in (left, added, right, evening)], [1, 2, 1, 1])
        self.assertEqual(self.site.peek(), left)
        self.assertEqual(self.site.peek('L', 'evening'), evening)

    def test_exact_replay_after_stop(self):
        original = self.site.capture('L7', [-8, 3], source='left/raw')
        mark = self.site.stop('L')
        self.assertEqual(mark.last_sequence, 1)
        self.reopen()
        self.assertEqual(self.site.capture('L7', [-8, 3], source='left/raw'), original)
        with self.assertRaises(ValueError):
            self.site.capture('L7', [8, 3], source='left/raw')
        with self.assertRaises(ValueError):
            self.site.capture('L8', [2])
        new_right = self.site.capture('R9', [5, -2], device='R')
        self.assertEqual(new_right.wave_id.sequence, 1)
        self.assertEqual(self.site.stop('L'), mark)
        self.assertEqual(self.site.receive('after-stop', original).wave_id, original.wave_id)
        self.assertIsNone(self.site.peek())

    def test_invalid_capture_and_peek(self):
        for samples in ([], [True], [1.0], '12', b'12'):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                self.site.capture('invalid', samples)
        with self.assertRaises(ValueError):
            self.site.capture('invalid', [1], source='')
        with self.assertRaises(LookupError):
            self.site.capture('invalid', [1], shift_id='unopened')
        with self.assertRaises(LookupError):
            self.site.peek(shift_id='unopened')
        unknown = DeviceJournal(self.directory / 'unknown.sqlite3', 'unlisted')
        with self.assertRaises(ValueError):
            peek(self.site.ledger, unknown, SHIFT)
        with self.assertRaises(ValueError):
            capture(self.site.ledger, unknown, SHIFT, 'x', [0], 'source')
        self.assertFalse(unknown.path.exists())
        self.assertEqual(self.site.capture('valid', [0]).wave_id.sequence, 1)
