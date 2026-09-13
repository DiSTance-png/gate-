"""Outbound endpoint validation for credential-bearing backup and notification calls."""
from __future__ import annotations
import ipaddress
import socket
import urllib.parse


def validate_outbound_url(url: str, *, allow_private: bool = False, allowed_hosts: set[str] | None = None) -> str:
    parsed = urllib.parse.urlparse(str(url).strip())
    if parsed.scheme not in ({"https", "http"} if allow_private else {"https"}):
        raise ValueError("Endpoint 必须使用 HTTPS；仅显式启用私有存储时允许 HTTP")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or parsed.username or parsed.password:
        raise ValueError("Endpoint 主机无效，URL 中不得嵌入账号密码")
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError("Endpoint 不在官方域名白名单")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f"Endpoint 域名无法解析：{host}") from exc
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("Endpoint 指向受保护的本机、链路本地或保留地址")
        if ip.is_private and not allow_private:
            raise ValueError("Endpoint 指向私有网络；如为自建 MinIO/NAS，请显式启用私有存储")
    return str(url).rstrip("/")


def validate_wechat_base_url(url: str) -> str:
    return validate_outbound_url(url, allowed_hosts={"ilinkai.weixin.qq.com"})
