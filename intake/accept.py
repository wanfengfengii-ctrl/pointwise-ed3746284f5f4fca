from collections.abc import Callable

from intake.ledger import StationLedger
from intake.originals import store_original
from intake.receipts import Receipt, find_request_receipt, find_wave_receipt, issue_receipt
from intake.validation import TakeOneRequest, require_requested_head, validate_binding
from recorder.journal import DeviceJournal
from recorder.queue import read_current_head
from recorder.release import ReleaseEvidence, release_head
from shift.opening import load_protocol


def take_one(ledger: StationLedger, journal: DeviceJournal, request: TakeOneRequest, *,
             before_commit: Callable[[Receipt], None] | None = None,
             before_release: Callable[[Receipt], None] | None = None,
             after_release: Callable[[Receipt], None] | None = None) -> Receipt:
    """Commit station custody before delivering evidence to one recorder."""
    with ledger.write_transaction() as station:
        protocol = load_protocol(station, request.shift_id)
        validate_binding(protocol, journal.device_id, request)
        receipt = find_request_receipt(station, request.request_id)
        if receipt is not None:
            validate_binding(protocol, journal.device_id, request, receipt)
        else:
            with journal.transaction(write=False) as device:
                current = read_current_head(journal, device, request.shift_id)
                packet = require_requested_head(request, current)
            if find_wave_receipt(station, packet.wave_id) is not None:
                raise ValueError('原件已由另一请求正式实收')
            store_original(station, packet)
            receipt = issue_receipt(station, request.request_id, packet.wave_id)
            if before_commit is not None:
                before_commit(receipt)

    if before_release is not None:
        before_release(receipt)
    evidence = ReleaseEvidence(receipt.receipt_id, receipt.request_id, receipt.wave_id)
    release_head(ledger, journal, evidence)
    if after_release is not None:
        after_release(receipt)
    return receipt
