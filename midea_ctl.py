#!/usr/bin/env python3
"""美的空调局域网控制（V3 加密协议，完全离线，不经过云端）。

依赖: pip install msmart-ng

用法:
    python midea_ctl.py status
    python midea_ctl.py on
    python midea_ctl.py off
    python midea_ctl.py temp 26
    python midea_ctl.py mode cool|heat|dry|fan|auto
    python midea_ctl.py fan auto|silent|low|medium|high|max
    python midea_ctl.py swing off|vertical|horizontal|both
"""
import asyncio
import json
import sys
from pathlib import Path

from msmart.device.AC.device import AirConditioner

CONFIG = Path(__file__).with_name("midea_config.json")

MODES = {
    "auto": "AUTO",
    "cool": "COOL",
    "dry": "DRY",
    "heat": "HEAT",
    "fan": "FAN_ONLY",
}
FANS = {
    "auto": "AUTO",
    "silent": "SILENT",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "max": "MAX",
}
SWINGS = {
    "off": "OFF",
    "vertical": "VERTICAL",
    "horizontal": "HORIZONTAL",
    "both": "BOTH",
}


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"缺少配置文件 {CONFIG}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


async def connect() -> AirConditioner:
    cfg = load_config()
    dev = AirConditioner(cfg["ip"], cfg["id"], cfg["port"], version=3, sn=cfg["sn"], name=cfg["name"])
    await dev.authenticate(cfg["token"], cfg["key"])
    await dev.refresh()
    return dev


def show(dev: AirConditioner) -> None:
    print(f"设备      {dev.name}  {load_config().get('model') or ''}  @ {dev.ip}:{dev.port}")
    print(f"开关机    {'开' if dev.power_state else '关'}")
    print(f"运行模式  {dev.operational_mode.name}")
    print(f"目标温度  {dev.target_temperature} ℃")
    print(f"室内温度  {dev.indoor_temperature} ℃")
    print(f"风速      {dev.fan_speed.name if hasattr(dev.fan_speed, 'name') else dev.fan_speed}")
    print(f"摆风      {dev.swing_mode.name}")
    print(f"故障码    {dev.error_code}")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    arg = sys.argv[2].lower() if len(sys.argv) > 2 else None

    dev = await connect()

    if cmd == "status":
        show(dev)
        return

    if cmd == "on":
        dev.power_state = True
    elif cmd == "off":
        dev.power_state = False
    elif cmd == "temp":
        if arg is None:
            sys.exit("用法: temp <温度>")
        dev.target_temperature = float(arg)
    elif cmd == "mode":
        if arg not in MODES:
            sys.exit(f"模式可选: {', '.join(MODES)}")
        dev.operational_mode = AirConditioner.OperationalMode[MODES[arg]]
    elif cmd == "fan":
        if arg not in FANS:
            sys.exit(f"风速可选: {', '.join(FANS)}")
        dev.fan_speed = AirConditioner.FanSpeed[FANS[arg]]
    elif cmd == "swing":
        if arg not in SWINGS:
            sys.exit(f"摆风可选: {', '.join(SWINGS)}")
        dev.swing_mode = AirConditioner.SwingMode[SWINGS[arg]]
    else:
        sys.exit(__doc__)

    await dev.apply()
    await asyncio.sleep(2)
    await dev.refresh()
    print("指令已下发\n")
    show(dev)


if __name__ == "__main__":
    asyncio.run(main())
