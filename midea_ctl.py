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
import sys

from msmart.device.AC.device import AirConditioner
from msmart.lan import AuthenticationError

import midea_config as config
from midea_config import SetupNeeded

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


def load_cfg() -> dict:
    """读取并校验配置；不合法抛 SetupNeeded（由入口转成一行提示）。"""
    return config.load()


async def connect() -> tuple[AirConditioner, dict]:
    cfg = load_cfg()
    dev = AirConditioner(cfg["ip"], cfg["id"], cfg["port"], version=3, sn=cfg["sn"], name=cfg["name"])
    try:
        await dev.authenticate(cfg["token"], cfg["key"])
    except AuthenticationError as e:       # 英文原文只进这句，不对外输出
        raise SetupNeeded("auth_failed") from e
    await dev.refresh()
    return dev, cfg


def show(dev: AirConditioner, cfg: dict) -> None:
    print(f"设备      {dev.name}  {cfg.get('model') or ''}  @ {dev.ip}:{dev.port}")
    print(f"开关机    {'开' if dev.power_state else '关'}")
    print(f"运行模式  {dev.operational_mode.name}")
    print(f"目标温度  {dev.target_temperature} ℃")
    print(f"室内温度  {dev.indoor_temperature} ℃")
    print(f"风速      {dev.fan_speed.name if hasattr(dev.fan_speed, 'name') else dev.fan_speed}")
    print(f"摆风      {dev.swing_mode.name}")
    print(f"故障码    {dev.error_code}")


def read_back(dev: AirConditioner) -> dict:
    fan = dev.fan_speed
    swing = dev.swing_mode.name.lower()
    return {
        "power": bool(dev.power_state),
        "target": None if dev.target_temperature is None else round(float(dev.target_temperature), 1),
        "mode": dev.operational_mode.name.lower(),
        "fan": fan.name.lower() if hasattr(fan, "name") else str(fan),
        "swing_v": swing in ("vertical", "both"),
        "swing_h": swing in ("horizontal", "both"),
    }


def verify(dev: AirConditioner, want: dict) -> dict:
    """逐字段比对期望值与回读值，返回不一致项。"""
    got = read_back(dev)
    bad = {}
    for k, v in want.items():
        g = got.get(k)
        if k == "target":
            if g is None or abs(g - float(v)) > 0.5:
                bad[k] = (v, g)
        elif g != v:
            bad[k] = (v, g)
    return bad


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    arg = sys.argv[2].lower() if len(sys.argv) > 2 else None

    dev, cfg = await connect()

    if cmd == "status":
        show(dev, cfg)
        return

    want = {}
    if cmd == "on":
        dev.power_state = True
        want["power"] = True
    elif cmd == "off":
        dev.power_state = False
        want["power"] = False
    elif cmd == "temp":
        if arg is None:
            sys.exit("用法: temp <温度>")
        dev.target_temperature = float(arg)
        want["target"] = float(arg)
    elif cmd == "mode":
        if arg not in MODES:
            sys.exit(f"模式可选: {', '.join(MODES)}")
        dev.operational_mode = AirConditioner.OperationalMode[MODES[arg]]
        want["mode"] = arg
    elif cmd == "fan":
        if arg not in FANS:
            sys.exit(f"风速可选: {', '.join(FANS)}")
        dev.fan_speed = AirConditioner.FanSpeed[FANS[arg]]
        want["fan"] = arg
    elif cmd == "swing":
        if arg not in SWINGS:
            sys.exit(f"摆风可选: {', '.join(SWINGS)}")
        dev.swing_mode = AirConditioner.SwingMode[SWINGS[arg]]
        want["swing_v"] = arg in ("vertical", "both")
        want["swing_h"] = arg in ("horizontal", "both")
    else:
        sys.exit(__doc__)

    await dev.apply()
    await asyncio.sleep(2)
    await dev.refresh()

    bad = verify(dev, want)
    if getattr(dev, "online", True) is False:      # 底层超时会被吞成空响应，这里兜住
        print("⚠ 设备无响应：指令可能未送达")
        show(dev, cfg)
        sys.exit(2)
    if bad:
        print("✗ 未生效：")
        for k, (w, g) in bad.items():
            print(f"   {k}: 期望 {w}，实际 {g}")
        print()
        show(dev, cfg)
        sys.exit(2)

    print("✓ 已生效\n")
    show(dev, cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SetupNeeded as e:
        sys.exit(f"无法连接：{e}\n"
                 f"请在 {config.CONFIG.name} 中填入 token / key，"
                 f"或运行 midea_server.py 在网页上完成设置。")
