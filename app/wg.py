import subprocess
from typing import Tuple, Optional

from .config import settings
from . import db

# Путь к основному конфигу WireGuard
WG_CONFIG_PATH = "/etc/wireguard/wg0.conf"


def run_cmd(cmd: list) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def generate_keypair() -> Tuple[str, str]:
    """
    Генерируем приватный и публичный ключ для клиента.
    """
    # приватный ключ
    priv_proc = subprocess.run(
        ["wg", "genkey"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    private_key = priv_proc.stdout.strip()

    # публичный ключ из приватного
    pub_proc = subprocess.run(
        ["wg", "pubkey"],
        check=True,
        input=(private_key + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    public_key = pub_proc.stdout.decode("utf-8").strip()

    return private_key, public_key


def generate_client_ip() -> str:
    """
    Берём максимум по последнему октету из БД и выдаём следующий.
    Стартовое значение берём из WG_CLIENT_IP_START (например, 10 → 10.8.0.10).
    """
    last_from_db = db.get_max_client_ip_last_octet()
    base = max(last_from_db, settings.WG_CLIENT_IP_START - 1)
    next_octet = base + 1
    ip = f"{settings.WG_CLIENT_NETWORK_PREFIX}{next_octet}"
    return ip


def _append_peer_to_config(public_key: str, allowed_ip: str, telegram_user_id: Optional[int] = None) -> None:
    """
    Дописываем peer в /etc/wireguard/wg0.conf.

    Формат:

    # auto-added by vpn_service user=123456789
    [Peer]
    PublicKey = ...
    AllowedIPs = ...
    """
    try:
        comment = "# auto-added by vpn_service"
        if telegram_user_id is not None:
            comment += f" user={telegram_user_id}"

        with open(WG_CONFIG_PATH, "a", encoding="utf-8") as f:
            f.write("\n\n")
            f.write(comment + "\n")
            f.write("[Peer]\n")
            f.write(f"PublicKey = {public_key}\n")
            f.write(f"AllowedIPs = {allowed_ip}\n")
    except Exception:
        # Если что-то не так с файлом конфига — не роняем сервис
        pass


def _remove_peer_from_config(public_key: str) -> None:
    """
    Удаляем peer из /etc/wireguard/wg0.conf, но только тот, который
    был добавлен нашим сервисом в формате:

    # auto-added by vpn_service user=...
    [Peer]
    PublicKey = <public_key>
    AllowedIPs = ...

    Логика:
    - ищем строку с комментарием "# auto-added by vpn_service"
    - проверяем, что после неё идёт [Peer] и PublicKey = <наш ключ>
    - если совпадает — вырезаем этот блок до следующей пустой строки
    """
    try:
        with open(WG_CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    except Exception:
        return

    new_lines = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Ищем наш блок
        if line.startswith("# auto-added by vpn_service") and i + 2 < n:
            line_peer = lines[i + 1].strip()
            line_pub = lines[i + 2].strip()

            target_pub_line = f"PublicKey = {public_key}"

            if line_peer == "[Peer]" and line_pub == target_pub_line:
                # Пропускаем этот блок до первой пустой строки (или до конца файла)
                i += 3
                while i < n and lines[i].strip() != "":
                    i += 1
                # Пропускаем возможную одну пустую строку после блока
                if i < n and lines[i].strip() == "":
                    i += 1
                continue

        # Если не наш блок — просто копируем строку
        new_lines.append(line)
        i += 1

    try:
        with open(WG_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception:
        # Не роняем сервис, если не получилось перезаписать файл
        pass


def add_peer(public_key: str, allowed_ip: str, telegram_user_id: Optional[int] = None) -> None:
    """
    Добавляем пира в wg0 (в рантайме) + дописываем в wg0.conf.
    """
    cmd = [
        "wg",
        "set",
        settings.WG_INTERFACE_NAME,
        "peer",
        public_key,
        "allowed-ips",
        allowed_ip,
    ]
    run_cmd(cmd)

    # Сохраняем peer в конфиге с комментарием user=<telegram_id>
    _append_peer_to_config(public_key, allowed_ip, telegram_user_id)


def remove_peer(public_key: str) -> None:
    """
    Удаляем пира из wg0 (в рантайме) + удаляем из wg0.conf (если он там с пометкой сервиса).
    """
    cmd = [
        "wg",
        "set",
        settings.WG_INTERFACE_NAME,
        "peer",
        public_key,
        "remove",
    ]
    run_cmd(cmd)

    _remove_peer_from_config(public_key)


def build_client_config(
    client_private_key: str,
    client_ip: str,
) -> str:
    """
    Генерируем текст конфигурации для клиента (для телефона / ПК).
    """
    config_text = f"""[Interface]
PrivateKey = {client_private_key}
Address = {client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}
DNS = 1.1.1.1

[Peer]
PublicKey = {settings.WG_SERVER_PUBLIC_KEY}
Endpoint = {settings.WG_SERVER_ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
    return config_text
