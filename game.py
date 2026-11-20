from __future__ import annotations

import json
import socket
import struct
import time
from typing import Any


def _read_varint_from_socket(sock: socket.socket) -> int:
    value = 0
    shift = 0
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("connection closed while reading VarInt")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
        shift += 7
        if shift > 35:
            raise ValueError("invalid VarInt")


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        current = value & 0x7F
        value >>= 7
        if value:
            output.append(current | 0x80)
        else:
            output.append(current)
            return bytes(output)


def _mc_query(host: str, port: int, timeout: float = 6.0) -> dict[str, Any]:
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        host_encoded = host.encode("idna")
        handshake = (
            _varint(0)
            + _varint(767)
            + _varint(len(host_encoded))
            + host_encoded
            + struct.pack(">H", port)
            + _varint(1)
        )
        sock.sendall(_varint(len(handshake)) + handshake)
        sock.sendall(b"\x01\x00")
        packet_length = _read_varint_from_socket(sock)
        packet = b""
        while len(packet) < packet_length:
            chunk = sock.recv(packet_length - len(packet))
            if not chunk:
                raise ConnectionError("Minecraft status response ended early")
            packet += chunk
    # Packet ID first VarInt, then a VarInt-prefixed JSON string.
    offset = 0
    while packet[offset] & 0x80:
        offset += 1
    offset += 1
    string_length = 0
    shift = 0
    while True:
        byte = packet[offset]
        offset += 1
        string_length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    status = json.loads(packet[offset : offset + string_length].decode("utf-8"))
    version = status.get("version", {})
    players = status.get("players", {})
    return {
        "online": True,
        "name": status.get("description")
        if isinstance(status.get("description"), str)
        else json.dumps(status.get("description", ""), ensure_ascii=False),
        "version": version.get("name", ""),
        "protocol": version.get("protocol"),
        "players": players.get("online", 0),
        "max_players": players.get("max", 0),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "raw": status,
    }


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", "replace"), end + 1


def _a2s_query(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    started = time.perf_counter()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        request = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
        sock.sendto(request, (host, port))
        data, _ = sock.recvfrom(65535)
        if data[:4] == b"\xFF\xFF\xFF\xFF" and data[4:5] == b"A":
            sock.sendto(request + data[5:9], (host, port))
            data, _ = sock.recvfrom(65535)
    if data[:4] != b"\xFF\xFF\xFF\xFF" or data[4:5] != b"I":
        raise ValueError("Unsupported A2S response")
    offset = 6
    name, offset = _read_cstring(data, offset)
    map_name, offset = _read_cstring(data, offset)
    folder, offset = _read_cstring(data, offset)
    game, offset = _read_cstring(data, offset)
    if offset + 10 > len(data):
        raise ValueError("Truncated A2S response")
    app_id = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    players, max_players, bots = struct.unpack_from("<BBB", data, offset)
    offset += 3
    server_type, environment, visibility = struct.unpack_from("<BBB", data, offset)
    offset += 3
    vac = bool(data[offset])
    offset += 1
    version, _ = _read_cstring(data, offset)
    return {
        "online": True,
        "name": name,
        "map": map_name,
        "folder": folder,
        "game": game,
        "app_id": app_id,
        "players": players,
        "max_players": max_players,
        "bots": bots,
        "server_type": chr(server_type),
        "environment": chr(environment),
        "visibility": visibility,
        "vac": vac,
        "version": version,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def query_game_server(game_type: str, host: str, port: int) -> dict[str, Any]:
    try:
        status = (
            _mc_query(host, port)
            if game_type == "minecraft"
            else _a2s_query(host, port)
        )
        return {**status, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    except Exception as exc:
        return {
            "online": False,
            "error": str(exc),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        data += chunk
    return data


def _minecraft_rcon(
    host: str, port: int, password: str, command: str, timeout: float = 6.0
) -> str:
    def packet(request_id: int, packet_type: int, body: str) -> bytes:
        payload = struct.pack("<ii", request_id, packet_type)
        payload += body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(packet(1, 3, password))
        size = struct.unpack("<i", _recv_exact(sock, 4))[0]
        auth = _recv_exact(sock, size)
        request_id = struct.unpack_from("<i", auth, 0)[0]
        if request_id == -1:
            raise PermissionError("Minecraft RCON authentication failed")
        sock.sendall(packet(2, 2, command))
        size = struct.unpack("<i", _recv_exact(sock, 4))[0]
        response = _recv_exact(sock, size)
        return response[8:-2].decode("utf-8", "replace")


def _source_rcon(
    host: str, port: int, password: str, command: str, timeout: float = 6.0
) -> str:
    def packet(request_id: int, packet_type: int, body: str) -> bytes:
        payload = (
            struct.pack("<ii", request_id, packet_type)
            + body.encode("utf-8")
            + b"\x00\x00"
        )
        return struct.pack("<i", len(payload)) + payload

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(packet(1, 3, password))
        size = struct.unpack("<i", _recv_exact(sock, 4))[0]
        auth = _recv_exact(sock, size)
        request_id = struct.unpack_from("<i", auth, 0)[0]
        if request_id == -1:
            raise PermissionError("GMod RCON authentication failed")
        sock.sendall(packet(2, 2, command))
        size = struct.unpack("<i", _recv_exact(sock, 4))[0]
        response = _recv_exact(sock, size)
        return response[8:-2].decode("utf-8", "replace")


def run_rcon_command(
    game_type: str,
    host: str,
    port: int,
    password: str,
    command: str,
) -> str:
    if not password:
        raise ValueError("RCON 密码为空")
    if not command.strip():
        raise ValueError("命令不能为空")
    if game_type == "minecraft":
        return _minecraft_rcon(host, port, password, command)
    return _source_rcon(host, port, password, command)
