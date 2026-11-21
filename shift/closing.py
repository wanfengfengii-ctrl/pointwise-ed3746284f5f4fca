from enum import StrEnum

from recorder.endmark import EndMark
from shift.protocol import ShiftProtocol


class ShiftPhase(StrEnum):
    RECORDING = 'recording'
    ONE_STOPPED = 'one_stopped'
    STOPPED = 'stopped'


def _index_endmarks(protocol: ShiftProtocol, endmarks) -> dict[str, EndMark]:
    by_device = {}
    for mark in endmarks:
        if not isinstance(mark, EndMark) or mark.shift_id != protocol.shift_id:
            raise ValueError('末序须属于本班')
        protocol.roster.require_device(mark.device_id)
        if mark.device_id in by_device:
            raise ValueError(f'重复设备末序 {mark.device_id!r}')
        by_device[mark.device_id] = mark
    return by_device


def derive_shift_phase(protocol: ShiftProtocol, endmarks) -> ShiftPhase:
    count = len(_index_endmarks(protocol, endmarks))
    return (ShiftPhase.RECORDING, ShiftPhase.ONE_STOPPED, ShiftPhase.STOPPED)[count]


def require_stopped(protocol: ShiftProtocol, endmarks) -> tuple[EndMark, EndMark]:
    by_device = _index_endmarks(protocol, endmarks)
    left, right = protocol.roster.devices
    if left not in by_device or right not in by_device:
        raise ValueError('封报需要本班两台设备的末序')
    return by_device[left], by_device[right]
