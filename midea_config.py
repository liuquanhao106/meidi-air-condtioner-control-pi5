"""配置读写与凭据校验。

token 为 128 位十六进制（64 字节），key 为 64 位十六进制（32 字节）。
任何一处不合法都抛 SetupNeeded，由入口分别转成「一行中文提示」或 HTTP 428。
"""
import json
import re
from pathlib import Path

CONFIG = Path(__file__).with_name("midea_config.json")

HEX_TOKEN = re.compile(r"^[0-9a-fA-F]{128}$")
HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")

REASON_TEXT = {
    "missing": "缺少 token / key，请先填写设备密钥",
    "invalid_format": "密钥格式不对：token 应为 128 位十六进制，key 应为 64 位十六进制",
    "auth_failed": "密钥无效或已失效：设备拒绝了本次鉴权",
    "unreachable": "连不上设备：请确认空调已通电并与电脑在同一局域网",
    "device_mismatch": "配置中的设备 ID 与局域网发现的设备不一致",
}


class SetupNeeded(Exception):
    """需要用户补全 token/key，或设备身份不匹配。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        msg = REASON_TEXT.get(reason, reason)
        super().__init__(f"{msg}（{detail}）" if detail else msg)


def read() -> dict:
    """读取原始配置；文件不存在返回空 dict。"""
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SetupNeeded("invalid_format", f"配置文件不是合法 JSON：{e.msg}")


def check_credentials(token, key) -> tuple[str, str]:
    """只校验密钥格式，返回小写规范值；设置弹窗与配置加载共用。"""
    token = str(token or "").strip()
    key = str(key or "").strip()
    if not token or not key:
        raise SetupNeeded("missing")
    if not HEX_TOKEN.match(token):
        raise SetupNeeded("invalid_format", f"token 长度为 {len(token)}，应为 128")
    if not HEX_KEY.match(key):
        raise SetupNeeded("invalid_format", f"key 长度为 {len(key)}，应为 64")
    return token.lower(), key.lower()


def validate(cfg: dict) -> dict:
    """校验并返回规范化后的副本；不合法抛 SetupNeeded。"""
    if not cfg:
        raise SetupNeeded("missing", "未找到配置文件")
    token, key = check_credentials(cfg.get("token"), cfg.get("key"))
    if not cfg.get("ip"):
        raise SetupNeeded("missing", "缺少设备 IP")
    try:
        dev_id = int(cfg.get("id"))
        port = int(cfg.get("port", 6444))
    except (TypeError, ValueError):
        raise SetupNeeded("missing", "设备 id 或 port 不合法")
    if dev_id <= 0:
        raise SetupNeeded("missing", "设备 id 必须为正整数")
    if not 0 < port < 65536:
        raise SetupNeeded("missing", "端口号不合法")

    cfg = dict(cfg)
    cfg["token"], cfg["key"] = token.lower(), key.lower()
    cfg["id"], cfg["port"] = dev_id, port
    return cfg


def load() -> dict:
    """读取 + 校验，供连接前调用。"""
    return validate(read())


def write(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def update_ip(ip: str) -> None:
    """设备换 IP 后写回配置；token/key 与 IP 无关，可继续使用。"""
    cfg = read()
    if cfg.get("ip") == ip:
        return
    cfg["ip"] = ip
    write(cfg)


def public_info(cfg: dict) -> dict:
    """给设置弹窗用的公开字段，绝不包含 token/key。"""
    return {k: cfg.get(k) for k in ("ip", "port", "id", "sn", "name", "model", "version")}
