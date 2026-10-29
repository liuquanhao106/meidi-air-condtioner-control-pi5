#!/usr/bin/env python3
"""美的空调本地 Web 控制台（临时界面）。

启动:  python midea_server.py
访问:  http://127.0.0.1:8765

优化记录:
  P0  1. 进程内复用单条 TCP 连接（常驻设备实例 + 串行锁 + 失败重连一次）
      2. 能力位全部来自设备真实上报 supports_*，前端按能力渲染
      3. 支持自定义风速（1-100 百分比）
  P1  4. UDP 广播自动发现：连接失败时按设备 ID 重新定位并把新 IP 写回配置
      5. 重连 / 单步超时 / 请求去重（同 key 请求合并 + 短时状态缓存）
"""
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from msmart.device.AC.device import AirConditioner
from msmart.discover import Discover
from msmart.lan import AuthenticationError

import midea_config as config
from midea_config import SetupNeeded

BASE = Path(__file__).parent
HTML = BASE / "midea_ui.html"
PORT = 8765
__version__ = "1.1.0"

CONNECT_TIMEOUT = 12.0   # 单次鉴权+刷新上限
IO_TIMEOUT = 8.0         # 单条指令/刷新上限
STATUS_CACHE = 0.5       # 状态接口短缓存，避免连点重复读设备
DISCOVER_TIMEOUT = 4.0   # 广播发现等待时长

MODES = {"auto": "AUTO", "cool": "COOL", "dry": "DRY", "heat": "HEAT", "fan": "FAN_ONLY"}
FANS = {"auto": "AUTO", "silent": "SILENT", "low": "LOW", "medium": "MEDIUM", "high": "HIGH", "max": "MAX"}
ANGLES = {"off": "OFF", "pos1": "POS_1", "pos2": "POS_2", "pos3": "POS_3", "pos4": "POS_4", "pos5": "POS_5"}
# 枚举风速换算成美居展示的百分比，仅用于前端显示
FAN_PCT = {"silent": 20, "low": 40, "medium": 60, "high": 80, "max": 100}


async def _auth(ip: str, cfg: dict) -> AirConditioner:
    dev = AirConditioner(ip, cfg["id"], cfg["port"], version=3,
                         sn=cfg["sn"], name=cfg["name"])
    await dev.authenticate(cfg["token"], cfg["key"])
    await dev.refresh()
    return dev


async def _discover_ip(cfg: dict):
    """UDP 广播发现：按设备 ID 或 SN 匹配，返回当前 IP。

    id 取自广播报文的 [20:26] 小端解析值（msmart 内部已完成），这里再做一次 SN 兜底，
    避免固件把 id 报成 0 时匹配失败；同时打印每台设备的明细，便于排查。
    """
    try:
        found = await Discover.discover(timeout=DISCOVER_TIMEOUT,
                                        discovery_packets=3,
                                        auto_connect=False)
    except Exception as e:
        print(f"discover failed: {e}", file=sys.stderr)
        return None

    for dev in found:
        print(f"  found id={getattr(dev, 'id', None)} sn={getattr(dev, 'sn', '')} "
              f"ip={getattr(dev, 'ip', '')}", file=sys.stderr)

    want_id, want_sn = cfg.get("id"), cfg.get("sn")
    for dev in found:
        if getattr(dev, "id", None) == want_id:
            return getattr(dev, "ip", None)
        if want_sn and getattr(dev, "sn", "") == want_sn:
            return getattr(dev, "ip", None)

    # 原 IP 上出现了别的设备：不是换地址，而是身份对不上，交给设置弹窗
    for dev in found:
        if getattr(dev, "ip", None) == cfg.get("ip") and getattr(dev, "id", None) != want_id:
            raise SetupNeeded("device_mismatch",
                              f"{cfg.get('ip')} 上是 id={getattr(dev, 'id', None)} 的另一台设备")
    return None


class DeviceManager:
    """常驻事件循环 + 单个已鉴权的设备连接，所有请求串行执行。

    去重策略: 相同 key 的并发请求共享同一个 future，只有一个真正打设备。
    """

    def __init__(self, timeout: float = 25.0):
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._dev = None
        self._io_lock = None
        self._lock = threading.Lock()
        self._inflight = {}
        self._cache_ts = 0.0
        self._cache = None

    # ---------- 对外入口 ----------
    def run(self, key: str, action):
        with self._lock:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = asyncio.run_coroutine_threadsafe(self._exec(action), self._loop)
                self._inflight[key] = future
        try:
            return future.result(self._timeout)
        finally:
            if owner:
                with self._lock:
                    if self._inflight.get(key) is future:
                        del self._inflight[key]

    def reset(self):
        with self._lock:
            self._dev = None
            self._cache = None
            self._cache_ts = 0.0

    def cached_status(self):
        if self._cache and time.monotonic() - self._cache_ts < STATUS_CACHE:
            return self._cache
        return None

    def store_status(self, state):
        self._cache, self._cache_ts = state, time.monotonic()

    # ---------- 内部 ----------
    async def _exec(self, action):
        if self._io_lock is None:
            self._io_lock = asyncio.Lock()
        async with self._io_lock:          # 单连接串行，防止并发指令打乱状态
            for attempt in (1, 2):
                try:
                    if self._dev is None:
                        self._dev = await self._connect()
                    return await action(self._dev)
                except (SetupNeeded, ValueError):
                    raise                  # 配置/参数错误：不重连、不吞异常
                except Exception:
                    self._dev = None       # 连接异常：丢弃并重连一次
                    self._cache = None
                    if attempt == 2:
                        raise

    async def _connect(self) -> AirConditioner:
        cfg = config.load()                      # 配置不完整时抛 SetupNeeded
        try:
            return await asyncio.wait_for(_auth(cfg["ip"], cfg), CONNECT_TIMEOUT)
        except AuthenticationError as e:         # 密钥被设备拒绝：交给设置弹窗
            print(f"authenticate {cfg['ip']} failed: {e}", file=sys.stderr)
            raise SetupNeeded("auth_failed") from e
        except Exception as e:
            # 可能是 DHCP 换了地址：广播重新定位后重试
            print(f"connect {cfg['ip']} failed ({e}), try discovery", file=sys.stderr)
            ip = await _discover_ip(cfg)
            if not ip:
                raise
            print(f"discovered device {cfg['id']} at {ip}", file=sys.stderr)
            config.update_ip(ip)
            try:
                return await asyncio.wait_for(_auth(ip, cfg), CONNECT_TIMEOUT)
            except AuthenticationError as e2:
                print(f"authenticate {ip} failed: {e2}", file=sys.stderr)
                raise SetupNeeded("auth_failed") from e2


MANAGER = DeviceManager()


async def _discover_all() -> list[dict]:
    found = await Discover.discover(timeout=DISCOVER_TIMEOUT, discovery_packets=3,
                                    auto_connect=False)
    out = []
    for dev in found:
        out.append({
            "ip": getattr(dev, "ip", ""),
            "port": getattr(dev, "port", 6444),
            "id": int(getattr(dev, "id", 0) or 0),
            "sn": getattr(dev, "sn", ""),
            "name": getattr(dev, "name", ""),
            "model": getattr(dev, "model", ""),
        })
    return out


def list_devices() -> list[dict]:
    """设置弹窗用：广播发现局域网内的空调。"""
    try:
        return asyncio.run(_discover_all())
    except SetupNeeded:
        raise
    except Exception as e:
        raise SetupNeeded("unreachable", f"广播发现失败：{e}")


async def _verify_credentials(cfg: dict) -> None:
    dev = AirConditioner(cfg["ip"], cfg["id"], cfg["port"], version=3,
                         sn=cfg["sn"], name=cfg["name"])
    await asyncio.wait_for(dev.authenticate(cfg["token"], cfg["key"]), CONNECT_TIMEOUT)


def apply_setup(payload: dict) -> dict:
    """先真实鉴权，成功才落盘；避免把无效密钥写进配置。"""
    token, key = config.check_credentials(payload.get("token"), payload.get("key"))
    ip = str(payload.get("ip") or "").strip()
    try:
        dev_id = int(payload.get("id"))
        port = int(payload.get("port") or 6444)
    except (TypeError, ValueError):
        raise SetupNeeded("missing", "设备信息不完整，请重新发现设备")
    if not ip or dev_id <= 0:
        raise SetupNeeded("missing", "设备信息不完整，请重新发现设备")

    cfg = {
        "ip": ip, "port": port, "id": dev_id,
        "sn": payload.get("sn", ""), "name": payload.get("name", ""),
        "model": payload.get("model", ""), "version": 3,
        "token": token, "key": key,
    }
    try:
        asyncio.run(_verify_credentials(cfg))
    except AuthenticationError as e:
        print(f"setup authenticate {ip} failed: {e}", file=sys.stderr)
        raise SetupNeeded("auth_failed")
    except Exception as e:
        raise SetupNeeded("unreachable", f"连不上 {ip}：{e}")

    config.write(cfg)
    MANAGER.reset()
    return get_status()


def _has(dev, name: str) -> bool:
    try:
        return bool(getattr(dev, name))
    except Exception:
        return False


def capabilities(dev: AirConditioner) -> dict:
    caps = {
        "eco": _has(dev, "supports_eco"),
        "turbo": _has(dev, "supports_turbo"),
        "freeze": _has(dev, "supports_freeze_protection"),
        "purifier": _has(dev, "supports_purifier"),
        "display": _has(dev, "supports_display_control"),
        "sound": _has(dev, "supports_sound"),
        "filter": _has(dev, "supports_filter_reminder"),
        "ieco": _has(dev, "supports_ieco"),
        "breeze": _has(dev, "supports_breezeless") or _has(dev, "supports_breeze_away"),
        "angle": _has(dev, "supports_vertical_swing_angle") or _has(dev, "supports_horizontal_swing_angle"),
        "custom_fan": _has(dev, "supports_custom_fan_speed"),
        "aux": len(getattr(dev, "supported_aux_modes", [0])) > 1,
        "humidity": _has(dev, "supports_humidity"),
    }
    return caps


def state_of(dev: AirConditioner) -> dict:
    fan = dev.fan_speed
    if isinstance(fan, AirConditioner.FanSpeed):
        fan_name = fan.name.lower()
        # 支持自定义风速时，设备会把百分比写进 fan_speed；否则按枚举换算
        fan_pct = FAN_PCT.get(fan_name)
    else:
        fan_name, fan_pct = "custom", int(fan)

    swing = dev.swing_mode.name.lower()
    return {
        "power": bool(dev.power_state),
        "mode": dev.operational_mode.name.lower(),
        "target": dev.target_temperature,
        "indoor": dev.indoor_temperature,
        "outdoor": dev.outdoor_temperature,
        "fan": fan_name,
        "fan_pct": fan_pct,
        "swing_v": swing in ("vertical", "both"),
        "swing_h": swing in ("horizontal", "both"),
        "breeze": bool(dev.breezeless),
        "eco": bool(dev.eco),
        "turbo": bool(dev.turbo),
        "freeze": bool(dev.freeze_protection),
        "purifier": bool(dev.purifier),
        "display": bool(dev.display_on),
        "sound": bool(dev.sound),
        "filter_alert": bool(dev.filter_alert),
        "ieco": bool(dev.ieco),
        "aux": getattr(dev.aux_mode, "name", "OFF") != "OFF",
        "angle_v": getattr(dev, "vertical_swing_angle", None).name.lower()
        if getattr(dev, "vertical_swing_angle", None) is not None else None,
        "caps": capabilities(dev),
        "error": dev.error_code,
        "model": _cfg_model(),
        "ip": dev.ip,
    }


def _cfg_model() -> str:
    try:
        return config.read().get("model") or ""
    except SetupNeeded:
        return ""


async def _read_state(dev: AirConditioner) -> dict:
    await asyncio.wait_for(dev.refresh(), IO_TIMEOUT)
    return state_of(dev)


def get_status() -> dict:
    cached = MANAGER.cached_status()
    if cached is not None:
        return cached
    state = MANAGER.run("status", _read_state)
    MANAGER.store_status(state)
    return state


# 指令字段 → 状态字段，用于下发后回读校验
WANT_KEYS = {
    "power": "power", "temp": "target", "mode": "mode", "fan": "fan",
    "fan_pct": "fan_pct", "swing_v": "swing_v", "swing_h": "swing_h",
    "breeze": "breeze", "eco": "eco", "turbo": "turbo", "freeze": "freeze",
    "purifier": "purifier", "sound": "sound", "display": "display",
    "ieco": "ieco", "aux": "aux", "angle_v": "angle_v",
}


def mismatches(payload: dict, s: dict) -> dict:
    """把请求字段与回读状态逐项比对，返回未生效的字段。"""
    bad = {}
    for k, sk in WANT_KEYS.items():
        if k not in payload:
            continue
        want, got = payload[k], s.get(sk)
        if k == "temp":
            if got is None or abs(float(got) - float(want)) > 0.5:
                bad[k] = (want, got)
        elif k == "fan_pct":
            if got is None or abs(int(got) - int(want)) > 6:
                bad[k] = (want, got)
        elif k == "angle_v":
            if str(got).lower() != str(want).lower():
                bad[k] = (want, got)
        elif bool(got) != bool(want):
            bad[k] = (want, got)
    return bad


def apply_control(payload: dict) -> dict:
    async def action(dev):
        if "power" in payload:
            dev.power_state = bool(payload["power"])
        if "temp" in payload:
            dev.target_temperature = float(payload["temp"])
        if "mode" in payload:
            key = str(payload["mode"]).lower()
            if key not in MODES:
                raise ValueError(f"unknown mode: {key}")
            dev.operational_mode = AirConditioner.OperationalMode[MODES[key]]
        if "fan" in payload:
            key = str(payload["fan"]).lower()
            if key not in FANS:
                raise ValueError(f"unknown fan: {key}")
            dev.fan_speed = AirConditioner.FanSpeed[FANS[key]]
        if "fan_pct" in payload:
            if not _has(dev, "supports_custom_fan_speed"):
                raise ValueError("该机型不支持自定义风速")
            dev.fan_speed = max(1, min(100, int(payload["fan_pct"])))

        if "swing_v" in payload or "swing_h" in payload:
            cur = dev.swing_mode.name.lower()
            v = bool(payload.get("swing_v", cur in ("vertical", "both")))
            h = bool(payload.get("swing_h", cur in ("horizontal", "both")))
            target = "BOTH" if v and h else "VERTICAL" if v else "HORIZONTAL" if h else "OFF"
            dev.swing_mode = AirConditioner.SwingMode[target]

        if "breeze" in payload:
            if not _has(dev, "supports_breezeless") and not _has(dev, "supports_breeze_away"):
                raise ValueError("该机型不支持防直吹")
            dev.breezeless = bool(payload["breeze"])
        if "eco" in payload:
            dev.eco = bool(payload["eco"])
        if "turbo" in payload:
            if not _has(dev, "supports_turbo"):
                raise ValueError("该机型不支持强劲模式")
            dev.turbo = bool(payload["turbo"])
        if "freeze" in payload:
            if not _has(dev, "supports_freeze_protection"):
                raise ValueError("该机型不支持防冻")
            dev.freeze_protection = bool(payload["freeze"])
        if "purifier" in payload:
            if not _has(dev, "supports_purifier"):
                raise ValueError("该机型不支持净化")
            dev.purifier = bool(payload["purifier"])
        if "sound" in payload:
            if not _has(dev, "supports_sound"):
                raise ValueError("该机型不支持提示音开关")
            dev.sound = bool(payload["sound"])
        if "display" in payload:
            if not _has(dev, "supports_display_control"):
                raise ValueError("该机型不支持屏显开关")
            if bool(payload["display"]) != bool(dev.display_on):
                await dev.toggle_display()
        if "ieco" in payload:
            if not _has(dev, "supports_ieco"):
                raise ValueError("该机型不支持智控温")
            dev.ieco = bool(payload["ieco"])
        if "aux" in payload:
            if len(getattr(dev, "supported_aux_modes", [0])) <= 1:
                raise ValueError("该机型不支持电辅热")
            dev.aux_mode = (AirConditioner.AuxHeatMode.AUX_HEAT if payload["aux"]
                            else AirConditioner.AuxHeatMode.OFF)
        if "angle_v" in payload:
            key = str(payload["angle_v"]).lower()
            if key not in ANGLES:
                raise ValueError(f"unknown angle: {key}")
            dev.vertical_swing_angle = AirConditioner.SwingAngle[ANGLES[key]]

        await asyncio.wait_for(dev.apply(), IO_TIMEOUT)
        await asyncio.sleep(1.2)
        await asyncio.wait_for(dev.refresh(), IO_TIMEOUT)
        state = state_of(dev)
        bad = mismatches(payload, state)      # 回读校验：设备没接受就明确告诉前端
        state["applied"] = not bad
        if bad:
            state["mismatch"] = {k: {"want": w, "got": g} for k, (w, g) in bad.items()}
        return state

    key = "control:" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    state = MANAGER.run(key, action)
    clean = {k: v for k, v in state.items() if k not in ("applied", "mismatch")}
    MANAGER.store_status(clean)   # 指令后的状态直接进缓存，避免紧接着重复读设备
    return state


def _safe_cfg() -> dict:
    try:
        return config.read()
    except SetupNeeded:
        return {}


def setup_body(exc: SetupNeeded) -> dict:
    """428 响应体：告诉前端为什么需要填密钥，并附上可公开的设备信息。"""
    return {
        "error": "need_credentials",
        "reason": exc.reason,
        "message": str(exc),
        "device": config.public_info(_safe_cfg()),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                self._send(200, dict(get_status(), version=__version__))
            except SetupNeeded as e:
                self._send(428, setup_body(e))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif path == "/api/setup/discover":
            try:
                self._send(200, {"devices": list_devices()})
            except SetupNeeded as e:
                self._send(428, setup_body(e))
            except Exception as e:
                self._send(502, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/control", "/api/setup"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/setup":
                self._send(200, {"ok": True, "state": apply_setup(payload)})
            else:
                self._send(200, apply_control(payload))
        except SetupNeeded as e:
            self._send(428, setup_body(e))
        except Exception as e:
            self._send(400, {"error": str(e)})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"美的空调控制台 v{__version__} 已启动: http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
