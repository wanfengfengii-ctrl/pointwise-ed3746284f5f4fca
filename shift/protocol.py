from dataclasses import dataclass

from shift.roster import DeviceRoster, build_roster

MICROSTRAIN_UNIT = 'μɛ'


@dataclass(frozen=True)
class ShiftProtocol:
    shift_id: str
    roster: DeviceRoster
    channel: str
    threshold: int
    unit: str = MICROSTRAIN_UNIT

    def __post_init__(self):
        if not isinstance(self.shift_id, str) or not self.shift_id.strip():
            raise ValueError('班号须为非空文本')
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError('通道须为非空文本')
        if type(self.threshold) is not int or self.threshold < 0:
            raise ValueError('门槛须为非负整数微应变')
        if not isinstance(self.roster, DeviceRoster) or self.unit != MICROSTRAIN_UNIT:
            raise ValueError('试验规程的设备清册或单位无效')


def build_protocol(shift_id, devices, channel, threshold) -> ShiftProtocol:
    return ShiftProtocol(shift_id, build_roster(devices), channel, threshold)
