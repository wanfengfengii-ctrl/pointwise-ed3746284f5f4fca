import json
from collections.abc import Sequence
from dataclasses import dataclass

MAX_SOURCE_SEQUENCE = (1 << 63) - 1


def _require_name(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}须为非空文本')


@dataclass(frozen=True)
class WaveId:
    shift_id: str
    device_id: str
    sequence: int

    def __post_init__(self):
        _require_name(self.shift_id, '班号')
        _require_name(self.device_id, '设备号')
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SOURCE_SEQUENCE:
            raise ValueError('原件源序无效')


def validate_capture_payload(action_id, samples, source) -> tuple[int, ...]:
    _require_name(action_id, '采集动作号')
    _require_name(source, '采集出处')
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes, bytearray)):
        raise ValueError('波形须为非空整数序列')
    values = tuple(samples)
    if not values or any(type(sample) is not int for sample in values):
        raise ValueError('波形须为非空整数序列')
    return values


@dataclass(frozen=True)
class WavePacket:
    wave_id: WaveId
    action_id: str
    samples: tuple[int, ...]
    source: str

    def __post_init__(self):
        if not isinstance(self.wave_id, WaveId):
            raise ValueError('原包须具有确切原件身份')
        values = validate_capture_payload(self.action_id, self.samples, self.source)
        object.__setattr__(self, 'samples', values)


def make_packet(shift_id, device_id, sequence, action_id, samples, source) -> WavePacket:
    return WavePacket(WaveId(shift_id, device_id, sequence), action_id, samples, source)


def capture_payload_matches(packet: WavePacket, action_id, samples, source) -> bool:
    values = validate_capture_payload(action_id, samples, source)
    return (packet.action_id, packet.samples, packet.source) == (action_id, values, source)


def encode_packet(packet: WavePacket) -> str:
    identity = packet.wave_id
    return json.dumps({
        'shift_id': identity.shift_id,
        'device_id': identity.device_id,
        'sequence': identity.sequence,
        'action_id': packet.action_id,
        'signed_samples': [hex(sample) for sample in packet.samples],
        'source': packet.source,
    }, separators=(',', ':'))


def decode_packet(payload: str) -> WavePacket:
    value = json.loads(payload)
    return make_packet(
        value['shift_id'], value['device_id'], value['sequence'], value['action_id'],
        tuple(int(sample, 16) for sample in value['signed_samples']), value['source'],
    )
