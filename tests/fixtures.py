import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from intake.accept import take_one
from intake.ledger import StationLedger
from intake.validation import TakeOneRequest
from recorder.capture import capture
from recorder.endmark import stop
from recorder.journal import DeviceJournal
from recorder.queue import peek
from shift.opening import open_shift

SHIFT = 'morning-01'


class InjectedFailure(RuntimeError):
    pass


def fail_at_checkpoint(_):
    raise InjectedFailure('simulated checkpoint failure')


class Checkpoint:
    def __init__(self):
        self.reached = Event()
        self.resume = Event()
        self.value = None

    def __call__(self, value):
        self.value = value
        self.reached.set()
        if not self.resume.wait(10):
            raise TimeoutError('checkpoint was not resumed')


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.ledger = StationLedger(root / 'station.sqlite3')
        self.sources = {device: DeviceJournal(root / f'{device}.sqlite3', device) for device in ('L', 'R')}

    def initialize(self):
        self.ledger.initialize()
        for journal in self.sources.values():
            journal.initialize()

    def open(self, shift_id=SHIFT, *, devices=('L', 'R'), channel='gauge-1', threshold=8):
        return open_shift(self.ledger, shift_id, devices, channel, threshold)

    def capture(self, action_id, samples, *, device='L', shift_id=SHIFT, source=None):
        if source is None:
            source = f'bench/{device}/{action_id}'
        return capture(self.ledger, self.sources[device], shift_id, action_id, samples, source)

    def peek(self, device='L', shift_id=SHIFT):
        return peek(self.ledger, self.sources[device], shift_id)

    def receive(self, request_id, packet, **checkpoints):
        identity = packet.wave_id
        request = TakeOneRequest(request_id, identity.shift_id, identity.device_id, identity)
        return take_one(self.ledger, self.sources[identity.device_id], request, **checkpoints)

    def stop(self, device, shift_id=SHIFT):
        return stop(self.ledger, self.sources[device], shift_id)

    def stop_both(self, shift_id=SHIFT):
        return self.stop('L', shift_id), self.stop('R', shift_id)

    def release_count(self, device='L', shift_id=SHIFT):
        with self.sources[device].transaction(write=False) as connection:
            return connection.execute('SELECT COUNT(*) FROM releases WHERE shift_id = ?', (shift_id,)).fetchone()[0]

    def disconnect_sources(self):
        for journal in self.sources.values():
            journal.path.rename(journal.path.with_suffix('.sqlite3.offline'))


class StationTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory(prefix='strain-test-')))
        self.site = Workspace(self.directory)
        self.site.initialize()
        self.protocol = self.site.open()

    def reopen(self):
        self.site = Workspace(self.directory)
        return self.site
