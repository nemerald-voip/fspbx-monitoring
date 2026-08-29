#!/usr/bin/env python3

import importlib.util
import json
import socket
import struct
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("freeswitch_rtcp_to_hep.py")
SPEC = importlib.util.spec_from_file_location("freeswitch_rtcp_to_hep", MODULE_PATH)
assert SPEC and SPEC.loader
AGENT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AGENT
SPEC.loader.exec_module(AGENT)


def decode_chunks(message):
    chunks = {}
    offset = 6
    while offset < len(message):
        vendor, chunk_type, length = struct.unpack("!HHH", message[offset : offset + 6])
        assert vendor == 0
        chunks[chunk_type] = message[offset + 6 : offset + length]
        offset += length
    return chunks


class FakeSocket:
    def __init__(self, pieces):
        self.pieces = iter(pieces)

    def recv(self, _size):
        return next(self.pieces, b"")


class FakeSender:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class AgentTests(unittest.TestCase):
    def test_esl_reader_handles_fragmented_content_length_frame(self):
        reader = AGENT.ESLReader(
            FakeSocket(
                [
                    b"Content-Type: text/event-plain\nContent-Length: 27\n\nEvent-Name: CHANNEL_",
                    b"CREATE\n",
                ]
            )
        )
        headers, body = reader.next_frame()
        self.assertEqual(headers["Content-Type"], "text/event-plain")
        self.assertEqual(body, b"Event-Name: CHANNEL_CREATE\n")

    def test_plain_event_headers_are_url_decoded(self):
        headers = AGENT.parse_headers(
            b"Event-Name: CHANNEL_CREATE\nvariable_sip_call_id: abc%40123\n\n"
        )
        self.assertEqual(headers["variable_sip_call_id"], "abc@123")

    def test_cache_correlates_other_leg_to_sip_call_id(self):
        cache = AGENT.CallCache()
        cache.update(
            {
                "Unique-ID": "a-leg",
                "variable_sip_call_id": "call@example.net",
                "variable_local_media_ip": "192.0.2.10",
                "variable_remote_media_ip": "198.51.100.20",
                "variable_local_media_port": "20000",
                "variable_remote_media_port": "30000",
            }
        )
        cache.update({"Unique-ID": "b-leg", "Other-Leg-Unique-ID": "a-leg"})
        correlation_id, _ = cache.resolve({"Unique-ID": "b-leg"})
        self.assertEqual(correlation_id, "call@example.net")

    def test_rtcp_payload_converts_cumulative_values_to_intervals(self):
        state = AGENT.CallState(recv_packets=90, recv_octets=9000, recv_lost=2)
        event = {
            "Event-Name": "RECV_RTCP_MESSAGE",
            "SSRC": "01020304",
            "Source0-SSRC": "aabbccdd",
            "Source0-Fraction": "3",
            "Source0-Lost": "4",
            "Source0-Highest-Sequence-Number-Received": "1000",
            "Source0-Jitter": "16",
            "Source0-LSR": "20",
            "Source0-DLSR": "30",
            "Sender-Packet-Count": "100",
            "Octect-Packet-Count": "10000",
        }
        payload = AGENT.rtcp_payload(event, state)
        self.assertEqual(payload["sender_information"]["packets"], 10)
        self.assertEqual(payload["sender_information"]["octets"], 1000)
        self.assertEqual(payload["report_blocks"][0]["packets_lost"], 2)
        self.assertEqual(payload["ssrc"], 0x01020304)
        self.assertEqual(payload["report_blocks"][0]["source_ssrc"], 0xAABBCCDD)

    def test_rtcp_payload_rejects_missing_or_invalid_ssrc(self):
        state = AGENT.CallState()
        self.assertIsNone(
            AGENT.rtcp_payload(
                {
                    "Event-Name": "SEND_RTCP_MESSAGE",
                    "SSRC": "not-hex",
                    "Source-SSRC": "11223344",
                },
                state,
            )
        )

    def test_hep3_encoding_contains_homer_rtcp_chunks(self):
        payload = b'{"type":200}'
        message = AGENT.encode_hep3(
            payload,
            "192.0.2.10",
            20001,
            "198.51.100.20",
            30001,
            1234,
            "pbx-1",
            "call@example.net",
            1_700_000_000_123_456,
        )
        self.assertEqual(message[:4], b"HEP3")
        self.assertEqual(struct.unpack("!H", message[4:6])[0], len(message))
        chunks = decode_chunks(message)
        self.assertEqual(chunks[1], struct.pack("!B", socket.AF_INET))
        self.assertEqual(chunks[11], b"\x05")
        self.assertEqual(chunks[12], struct.pack("!I", 1234))
        self.assertEqual(chunks[17], b"call@example.net")
        self.assertEqual(chunks[19], b"pbx-1")
        self.assertEqual(chunks[15], payload)

    def test_handle_event_sends_one_correlated_report(self):
        config = AGENT.Config(
            "127.0.0.1", 8021, "192.0.2.50", 9060, "udp", 1000, "pbx-1"
        )
        cache = AGENT.CallCache()
        sender = FakeSender()
        cache.update(
            {
                "Unique-ID": "leg-1",
                "variable_sip_call_id": "call-1",
                "variable_local_media_ip": "192.0.2.10",
                "variable_remote_media_ip": "198.51.100.20",
                "variable_local_media_port": "20000",
                "variable_remote_media_port": "30000",
            }
        )
        sent = AGENT.handle_event(
            {
                "Event-Name": "SEND_RTCP_MESSAGE",
                "Unique-ID": "leg-1",
                "SSRC": "a1b2c3d4",
                "Source-SSRC": "11223344",
                "Source-Fraction": "0",
                "Source-Lost": "0",
            },
            cache,
            sender,
            config,
        )
        self.assertTrue(sent)
        chunks = decode_chunks(sender.messages[0])
        self.assertEqual(chunks[17], b"call-1")
        payload = json.loads(chunks[15])
        self.assertEqual(payload["ssrc"], 0xA1B2C3D4)
        self.assertEqual(payload["report_blocks"][0]["source_ssrc"], 0x11223344)

    def test_config_rejects_non_loopback_esl(self):
        config = AGENT.Config(
            "192.0.2.10", 8021, "192.0.2.50", 9060, "udp", 1000, "pbx-1"
        )
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            config.validate()


if __name__ == "__main__":
    unittest.main()
