from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceRoster:
    left: str
    right: str

    def __post_init__(self):
        if any(not isinstance(name, str) or not name.strip() for name in self.devices):
            raise ValueError('设备号须为非空文本')
        if self.left == self.right:
            raise ValueError('班次须使用两台不同设备')

    @property
    def devices(self) -> tuple[str, str]:
        return self.left, self.right

    def require_device(self, device_id: str) -> str:
        if device_id not in self.devices:
            raise ValueError(f'设备不属于该班清册 {device_id!r}')
        return device_id


def build_roster(devices) -> DeviceRoster:
    if not isinstance(devices, (tuple, list)) or len(devices) != 2:
        raise ValueError('设备清册须按左右顺序提供两台设备')
    return DeviceRoster(*devices)
