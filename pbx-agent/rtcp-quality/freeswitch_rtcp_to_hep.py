#!/usr/bin/env python3
"""Forward FreeSWITCH RTCP events from local ESL to HOMER over HEPv3."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import unquote


LOG = logging.getLogger("freeswitch-rtcp-to-hep")
STOP = False
CHANNEL_EVENTS = {
    "CHANNEL_CREATE",
    "CHANNEL_PROGRESS_MEDIA",
    "CHANNEL_ANSWER",
    "CHANNEL_BRIDGE",
}
RTCP_EVENTS = {"RECV_RTCP_MESSAGE", "SEND_RTCP_MESSAGE"}
SUBSCRIPTION = (
    "event plain HEARTBEAT CHANNEL_CREATE CHANNEL_PROGRESS_MEDIA CHANNEL_ANSWER "
    "CHANNEL_BRIDGE CHANNEL_DESTROY RECV_RTCP_MESSAGE SEND_RTCP_MESSAGE\n\n"
)


def as_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value or "", 10)
    except (TypeError, ValueError):
        return default


def as_float(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value or "")
    except (TypeError, ValueError):
        return default


def as_port(value: Optional[str], default: int = 0) -> int:
    port = as_int(value, default)
    return port if 0 <= port <= 65535 else 0


def as_signed_loss(value: Optional[str]) -> int:
    loss = as_int(value)
    return loss - 0x100000000 if loss > 0x7FFFFFFF else loss


def as_ssrc(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        ssrc = int(value, 16)
    except ValueError:
        return None
    return ssrc if 0 <= ssrc <= 0xFFFFFFFF else None


def parse_headers(data: bytes) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for raw_line in data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
        if not raw_line:
            break
        name, separator, value = raw_line.partition(":")
        if separator:
            headers[name.strip()] = unquote(value.lstrip())
    return headers


class ESLReader:
    """Read Content-Length framed messages from an ESL socket."""

    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.buffer = b""

    def next_frame(self) -> Tuple[Dict[str, str], bytes]:
        while True:
            marker = self.buffer.find(b"\n\n")
            marker_size = 2
            if marker < 0:
                marker = self.buffer.find(b"\r\n\r\n")
                marker_size = 4
            if marker >= 0:
                header_data = self.buffer[:marker]
                headers = parse_headers(header_data)
                content_length = as_int(headers.get("Content-Length"))
                frame_length = marker + marker_size + content_length
                if len(self.buffer) >= frame_length:
                    body_start = marker + marker_size
                    body = self.buffer[body_start:frame_length]
                    self.buffer = self.buffer[frame_length:]
                    return headers, body
            chunk = self.connection.recv(65536)
            if not chunk:
                raise ConnectionError("ESL closed the connection")
            self.buffer += chunk
            if len(self.buffer) > 4 * 1024 * 1024:
                raise ValueError("ESL frame exceeds the 4 MiB safety limit")


@dataclass(frozen=True)
class Config:
    esl_host: str
    esl_port: int
    hep_host: str
    hep_port: int
    hep_transport: str
    capture_id: int
    node_name: str

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
        config = cls(
            esl_host=str(raw.get("esl_host", "127.0.0.1")),
            esl_port=as_int(str(raw.get("esl_port", 8021))),
            hep_host=str(raw["hep_host"]),
            hep_port=as_int(str(raw.get("hep_port", 9060))),
            hep_transport=str(raw.get("hep_transport", "udp")).lower(),
            capture_id=as_int(str(raw["capture_id"])),
            node_name=str(raw["node_name"]),
        )
        config.validate()
        return config

    def validate(self) -> None:
        try:
            esl_address = ipaddress.ip_address(self.esl_host)
        except ValueError as error:
            raise ValueError("esl_host must be a loopback IP address") from error
        if not esl_address.is_loopback:
            raise ValueError("esl_host must remain loopback-only")
        if not 1 <= self.esl_port <= 65535 or not 1 <= self.hep_port <= 65535:
            raise ValueError("ports must be between 1 and 65535")
        if self.hep_transport not in {"udp", "tcp"}:
            raise ValueError("hep_transport must be udp or tcp")
        if not 1 <= self.capture_id <= 0xFFFFFFFF:
            raise ValueError("capture_id must be between 1 and 4294967295")
        if not self.hep_host or not self.node_name:
            raise ValueError("hep_host and node_name must not be empty")


@dataclass
class CallState:
    call_id: str = ""
    call_uuid: str = ""
    direction: str = ""
    other_leg: str = ""
    local_ip: str = "127.0.0.1"
    remote_ip: str = "127.0.0.1"
    local_port: int = 0
    remote_port: int = 0
    recv_packets: int = 0
    recv_octets: int = 0
    recv_lost: int = 0
    send_packets: int = 0
    send_octets: int = 0
    send_lost: int = 0
    updated_at: float = 0.0


@dataclass
class CallGroup:
    correlation_id: str = ""
    priority: int = 0
    updated_at: float = 0.0


class CallCache:
    def __init__(self, ttl_seconds: int = 86400) -> None:
        self.states: Dict[str, CallState] = {}
        self.groups: Dict[str, CallGroup] = {}
        self.ttl_seconds = ttl_seconds

    def update_group(self, state: CallState, now: float) -> None:
        if not state.call_uuid or not state.call_id:
            return
        group = self.groups.setdefault(state.call_uuid, CallGroup())
        # B2BUA legs have distinct SIP Call-IDs. Prefer the original inbound
        # leg, but let its ID supersede a provisional outbound ID seen first.
        priority = 2 if state.direction == "inbound" else 1
        if not group.correlation_id or priority > group.priority:
            group.correlation_id = state.call_id
            group.priority = priority
        group.updated_at = now

    def update(self, event: Dict[str, str], now: Optional[float] = None) -> None:
        unique_id = event.get("Unique-ID", "")
        if not unique_id:
            return
        state = self.states.setdefault(unique_id, CallState())
        state.call_id = event.get("variable_sip_call_id") or state.call_id
        state.call_uuid = (
            event.get("Channel-Call-UUID")
            or event.get("variable_call_uuid")
            or state.call_uuid
        )
        state.direction = event.get("Call-Direction") or state.direction
        state.other_leg = event.get("Other-Leg-Unique-ID") or state.other_leg
        state.local_ip = event.get("variable_local_media_ip") or state.local_ip
        state.remote_ip = event.get("variable_remote_media_ip") or state.remote_ip
        state.local_port = as_port(
            event.get("variable_local_media_port"), state.local_port
        )
        state.remote_port = as_port(
            event.get("variable_remote_media_port"), state.remote_port
        )
        state.updated_at = time.monotonic() if now is None else now
        self.update_group(state, state.updated_at)

    def resolve(self, event: Dict[str, str]) -> Tuple[str, CallState]:
        unique_id = event.get("Unique-ID", "")
        self.update(event)
        state = self.states[unique_id]
        other_leg = event.get("Other-Leg-Unique-ID") or state.other_leg
        other_state = self.states.get(other_leg)
        group = self.groups.get(state.call_uuid)
        if group and group.correlation_id:
            call_id = group.correlation_id
        elif state.direction == "inbound" and state.call_id:
            call_id = state.call_id
        elif other_state and other_state.direction == "inbound" and other_state.call_id:
            call_id = other_state.call_id
        else:
            call_id = state.call_id or (other_state.call_id if other_state else "")
            call_id = call_id or event.get("variable_sip_call_id", "") or unique_id
        return call_id, state

    def remove(self, event: Dict[str, str]) -> None:
        self.states.pop(event.get("Unique-ID", ""), None)

    def expire(self, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            key
            for key, state in self.states.items()
            if current - state.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self.states.pop(key, None)
        expired_groups = [
            key
            for key, group in self.groups.items()
            if current - group.updated_at > self.ttl_seconds
        ]
        for key in expired_groups:
            self.groups.pop(key, None)


def counter_delta(current: int, previous: int) -> int:
    if current < previous:
        return current
    return current - previous


def rtcp_payload(event: Dict[str, str], state: CallState) -> Optional[dict]:
    received = event.get("Event-Name") == "RECV_RTCP_MESSAGE"
    prefix = "Source0-" if received else "Source-"
    sender_ssrc = as_ssrc(event.get("SSRC"))
    source_ssrc = as_ssrc(event.get(prefix + "SSRC"))
    if sender_ssrc is None or source_ssrc is None:
        return None

    packets_now = as_int(event.get("Sender-Packet-Count"))
    octets_now = as_int(event.get("Octect-Packet-Count"))
    lost_now = as_signed_loss(event.get(prefix + "Lost"))
    if received:
        packets = counter_delta(packets_now, state.recv_packets)
        octets = counter_delta(octets_now, state.recv_octets)
        lost = counter_delta(lost_now, state.recv_lost)
        state.recv_packets, state.recv_octets, state.recv_lost = (
            packets_now,
            octets_now,
            lost_now,
        )
    else:
        packets = counter_delta(packets_now, state.send_packets)
        octets = counter_delta(octets_now, state.send_octets)
        lost = counter_delta(lost_now, state.send_lost)
        state.send_packets, state.send_octets, state.send_lost = (
            packets_now,
            octets_now,
            lost_now,
        )

    return {
        "type": 200,
        "ssrc": sender_ssrc,
        "report_count": 1,
        "report_blocks": [
            {
                "source_ssrc": source_ssrc,
                "fraction_lost": as_int(event.get(prefix + "Fraction")),
                "packets_lost": lost,
                "highest_seq_no": as_int(
                    event.get(prefix + "Highest-Sequence-Number-Received")
                ),
                "lsr": as_int(event.get(prefix + "LSR")),
                "ia_jitter": as_float(event.get(prefix + "Jitter")),
                "dlsr": as_int(event.get(prefix + "DLSR")),
            }
        ],
        "sender_information": {
            "packets": packets,
            "ntp_timestamp_sec": as_int(event.get("NTP-Most-Significant-Word")),
            "ntp_timestamp_usec": as_int(event.get("NTP-Least-Significant-Word")),
            "rtp_timestamp": as_int(event.get("RTP-Timestamp")),
            "octets": octets,
        },
    }


def media_direction(event_name: str, state: CallState) -> Tuple[str, int, str, int]:
    # A received report describes PBX-to-peer RTP. A sent report describes the
    # peer-to-PBX RTP that FreeSWITCH received and measured.
    if event_name == "RECV_RTCP_MESSAGE":
        return state.local_ip, state.local_port, state.remote_ip, state.remote_port
    return state.remote_ip, state.remote_port, state.local_ip, state.local_port


def hep_chunk(chunk_type: int, payload: bytes) -> bytes:
    return struct.pack("!HHH", 0, chunk_type, len(payload) + 6) + payload


def normalized_addresses(source: str, destination: str) -> Tuple[int, bytes, bytes]:
    try:
        source_ip = ipaddress.ip_address(source)
        destination_ip = ipaddress.ip_address(destination)
        if source_ip.version != destination_ip.version:
            raise ValueError("address families differ")
    except ValueError:
        source_ip = ipaddress.ip_address("127.0.0.1")
        destination_ip = ipaddress.ip_address("127.0.0.1")
    family = socket.AF_INET if source_ip.version == 4 else socket.AF_INET6
    return family, source_ip.packed, destination_ip.packed


def encode_hep3(
    payload: bytes,
    source_ip: str,
    source_port: int,
    destination_ip: str,
    destination_port: int,
    capture_id: int,
    node_name: str,
    correlation_id: str,
    timestamp_us: Optional[int] = None,
) -> bytes:
    family, source_bytes, destination_bytes = normalized_addresses(
        source_ip, destination_ip
    )
    ip_source_type, ip_destination_type = (3, 4) if family == socket.AF_INET else (5, 6)
    event_time = timestamp_us or time.time_ns() // 1000
    chunks: Iterable[bytes] = (
        hep_chunk(1, struct.pack("!B", family)),
        hep_chunk(2, struct.pack("!B", socket.IPPROTO_UDP)),
        hep_chunk(ip_source_type, source_bytes),
        hep_chunk(ip_destination_type, destination_bytes),
        hep_chunk(7, struct.pack("!H", source_port)),
        hep_chunk(8, struct.pack("!H", destination_port)),
        hep_chunk(9, struct.pack("!I", event_time // 1_000_000)),
        hep_chunk(10, struct.pack("!I", event_time % 1_000_000)),
        hep_chunk(11, struct.pack("!B", 5)),
        hep_chunk(12, struct.pack("!I", capture_id)),
        hep_chunk(19, node_name.encode("utf-8")),
        hep_chunk(17, correlation_id.encode("utf-8")),
        hep_chunk(15, payload),
    )
    message = b"HEP3\x00\x00" + b"".join(chunks)
    if len(message) > 65535:
        raise ValueError("HEP message exceeds 65535 bytes")
    return message[:4] + struct.pack("!H", len(message)) + message[6:]


class HEPSender:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.connection: Optional[socket.socket] = None
        self.destination: Optional[tuple] = None

    def close(self) -> None:
        if self.connection:
            self.connection.close()
        self.connection = None
        self.destination = None

    def _connect(self) -> None:
        socket_type = socket.SOCK_DGRAM if self.config.hep_transport == "udp" else socket.SOCK_STREAM
        family, socket_type, protocol, _, destination = socket.getaddrinfo(
            self.config.hep_host,
            self.config.hep_port,
            0,
            socket_type,
        )[0]
        self.connection = socket.socket(family, socket_type, protocol)
        self.destination = destination
        if self.config.hep_transport == "tcp":
            self.connection.settimeout(5)
            self.connection.connect(destination)

    def send(self, message: bytes) -> None:
        try:
            if not self.connection:
                self._connect()
            assert self.connection is not None
            if self.config.hep_transport == "udp":
                assert self.destination is not None
                self.connection.sendto(message, self.destination)
            else:
                self.connection.sendall(message)
        except OSError:
            self.close()
            raise


def event_timestamp(event: Dict[str, str]) -> int:
    timestamp = as_int(event.get("Event-Date-Timestamp"))
    return timestamp if timestamp > 0 else time.time_ns() // 1000


def handle_event(
    event: Dict[str, str], cache: CallCache, sender: HEPSender, config: Config
) -> bool:
    event_name = event.get("Event-Name", "")
    if event_name in CHANNEL_EVENTS:
        cache.update(event)
        return False
    if event_name == "CHANNEL_DESTROY":
        cache.remove(event)
        return False
    if event_name not in RTCP_EVENTS:
        return False
    if not event.get("Unique-ID"):
        return False

    correlation_id, state = cache.resolve(event)
    payload = rtcp_payload(event, state)
    if payload is None:
        return False
    source_ip, source_port, destination_ip, destination_port = media_direction(
        event_name, state
    )
    message = encode_hep3(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        source_ip,
        source_port,
        destination_ip,
        destination_port,
        config.capture_id,
        config.node_name,
        correlation_id,
        event_timestamp(event),
    )
    sender.send(message)
    return True


def read_password() -> str:
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not credentials_directory:
        raise ValueError("CREDENTIALS_DIRECTORY is not set; use the systemd service")
    password_path = Path(credentials_directory) / "esl_password"
    password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
    if not password or "\n" in password or "\r" in password:
        raise ValueError("ESL credential must contain one non-empty line")
    return password


def expect_reply(reader: ESLReader, expected: str) -> None:
    headers, _ = reader.next_frame()
    if expected not in headers.get("Reply-Text", ""):
        raise ConnectionError(f"ESL command failed: {headers.get('Reply-Text', 'no reply')}")


def consume_esl(config: Config, password: str, sender: HEPSender) -> None:
    cache = CallCache()
    with socket.create_connection((config.esl_host, config.esl_port), timeout=10) as connection:
        connection.settimeout(90)
        reader = ESLReader(connection)
        headers, _ = reader.next_frame()
        if headers.get("Content-Type") != "auth/request":
            raise ConnectionError("ESL did not request authentication")
        connection.sendall(f"auth {password}\n\n".encode("utf-8"))
        expect_reply(reader, "+OK accepted")
        connection.sendall(SUBSCRIPTION.encode("ascii"))
        expect_reply(reader, "+OK")
        connection.settimeout(5)
        LOG.info("connected to local ESL and subscribed to RTCP events")
        sent = 0
        while not STOP:
            try:
                headers, body = reader.next_frame()
            except socket.timeout:
                continue
            if headers.get("Content-Type") != "text/event-plain":
                continue
            event = parse_headers(body)
            try:
                if handle_event(event, cache, sender, config):
                    sent += 1
                    if sent == 1:
                        LOG.info("forwarded first correlated RTCP report to HOMER")
                    elif sent % 100 == 0:
                        LOG.info("forwarded %d RTCP reports", sent)
            except OSError as error:
                LOG.warning("HEP send failed; report dropped: %s", error)
            if sent % 100 == 0:
                cache.expire()


def stop_handler(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to JSON configuration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        config = Config.load(args.config)
        password = read_password()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOG.error("configuration error: %s", error)
        return 2

    sender = HEPSender(config)
    backoff = 1
    while not STOP:
        try:
            consume_esl(config, password, sender)
            backoff = 1
        except (ConnectionError, OSError, ValueError) as error:
            if STOP:
                break
            LOG.warning("ESL connection unavailable: %s; retrying in %ds", error, backoff)
            sender.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    sender.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
