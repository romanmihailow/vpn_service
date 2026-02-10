import subprocess
import os
import tempfile
import fcntl
from ipaddress import ip_network, ip_address
from typing import Tuple, Optional, Iterable
from contextlib import contextmanager

from .config import settings
from . import db

# Путь к основному конфигу WireGuard
WG_CONFIG_PATH = "/etc/wireguard/wg0.conf"
WG_CONFIG_LOCK_PATH = settings.WG_CONFIG_LOCK_PATH

# Расширенная сеть WireGuard (/16 вместо /24)
WG_NETWORK_CIDR = "10.8.0.0/16"

# IP сервера WireGuard, который нельзя выдавать клиентам
WG_SERVER_IP = "10.8.0.1"


@contextmanager
def _wg_config_lock():
    lock_dir = os.path.dirname(WG_CONFIG_LOCK_PATH) or "."
    os.makedirs(lock_dir, exist_ok=True)
    with open(WG_CONFIG_LOCK_PATH, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _write_config_atomic(lines: Iterable[str]) -> None:
    dir_path = os.path.dirname(WG_CONFIG_PATH) or "."
    os.makedirs(dir_path, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_path,
        delete=False,
    ) as tmp:
        tmp.writelines(lines)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_path = tmp.name
    os.replace(temp_path, WG_CONFIG_PATH)


def _read_config_lines() -> list[str]:
    try:
        with open(WG_CONFIG_PATH, "r", encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []



def run_cmd(cmd: list) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()

def ensure_wg_up() -> None:
    """
    Проверяем, что WireGuard-интерфейс поднят.

    Если интерфейс wg0 не существует или не работает, выбрасываем RuntimeError.
    """
    try:
        subprocess.run(
            ["wg", "show", settings.WG_INTERFACE_NAME],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"WireGuard интерфейс {settings.WG_INTERFACE_NAME} не поднят. "
            f"Подними его: systemctl start wg-quick@{settings.WG_INTERFACE_NAME}"
        ) from e


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

    Дополнительно проверяем по БД, что сгенерированный IP не используется
    в активных подписках (чтобы не было конфликтов при повторной выдаче).
    """
    db.acquire_ip_allocation_lock()

    try:
        network = ip_network(WG_NETWORK_CIDR)
        server_ip = ip_address(WG_SERVER_IP)

        for ip in network.hosts():
            if ip == server_ip:
                continue

            candidate_ip = str(ip)

            # Проверяем в БД, что этот IP не используется в активной подписке
            # ОЖИДАЕТСЯ, ЧТО В db.py ЕСТЬ ФУНКЦИЯ is_vpn_ip_used(ip: str) -> bool
            if not db.is_vpn_ip_used(candidate_ip):
                return candidate_ip

        raise RuntimeError(f"No free VPN IPs left in {WG_NETWORK_CIDR}")
    except Exception:
        db.release_ip_allocation_lock()
        raise



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

        with _wg_config_lock():
            lines = _read_config_lines()
            lines.extend(
                [
                    "\n",
                    "\n",
                    comment + "\n",
                    "[Peer]\n",
                    f"PublicKey = {public_key}\n",
                    f"AllowedIPs = {allowed_ip}\n",
                ]
            )
            _write_config_atomic(lines)
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
        with _wg_config_lock():
            lines = _read_config_lines()
            if not lines:
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

            _write_config_atomic(new_lines)
    except Exception:
        # Не роняем сервис, если не получилось перезаписать файл
        pass


def add_peer(public_key: str, allowed_ip: str, telegram_user_id: Optional[int] = None) -> None:
    """
    Добавляем пира в wg0 (в рантайме) + дописываем в wg0.conf.
    """
    # Проверяем, что интерфейс WireGuard поднят
    ensure_wg_up()

    cmd = [
        "wg",
        "set",
        settings.WG_INTERFACE_NAME,
        "peer",
        public_key,
        "allowed-ips",
        allowed_ip,
    ]
    try:
        run_cmd(cmd)
    except Exception:
        db.release_ip_allocation_lock()
        raise

    # Сохраняем peer в конфиге с комментарием user=<telegram_id>
    _append_peer_to_config(public_key, allowed_ip, telegram_user_id)



def remove_peer(public_key: str) -> None:
    """
    Удаляем пира из wg0 (в рантайме) + удаляем из wg0.conf (если он там с пометкой сервиса).
    """
    # Проверяем, что интерфейс WireGuard поднят
    ensure_wg_up()

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
