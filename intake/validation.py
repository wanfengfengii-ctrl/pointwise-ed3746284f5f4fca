from dataclasses import dataclass

from intake.receipts import Receipt
from recorder.packet import WaveId, WavePacket
from shift.protocol import ShiftProtocol


class HeadChanged(ValueError):
    pass


@dataclass(frozen=True)
class TakeOneRequest:
    request_id: str
    shift_id: str
    device_id: str
    wave_id: WaveId


def validate_binding(protocol: ShiftProtocol, source_device: str,
                     request: TakeOneRequest, previous: Receipt | None = None) -> None:
    if not isinstance(request.request_id, str) or not request.request_id.strip():
        raise ValueError('取件请求号须为非空文本')
    protocol.roster.require_device(request.device_id)
    if protocol.shift_id != request.shift_id or source_device != request.device_id:
        raise ValueError('取件请求的班次或设备绑定不符')
    identity = request.wave_id
    if not isinstance(identity, WaveId) or (identity.shift_id, identity.device_id) != (request.shift_id, request.device_id):
        raise ValueError('取件请求与指定原件的班次或设备不符')
    if previous is not None and (previous.request_id, previous.wave_id) != (request.request_id, identity):
        raise ValueError('取件请求号已绑定另一原件')


def require_requested_head(request: TakeOneRequest, current: WavePacket | None) -> WavePacket:
    if current is None or current.wave_id != request.wave_id:
        found = current.wave_id if current is not None else None
        raise HeadChanged(f'队首已变。指定 {request.wave_id!r}，实际 {found!r}')
    return current
