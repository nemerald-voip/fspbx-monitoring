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
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "inbound",
                "variable_sip_call_id": "call@example.net",
                "variable_local_media_ip": "192.0.2.10",
                "variable_remote_media_ip": "198.51.100.20",
                "variable_local_media_port": "20000",
                "variable_remote_media_port": "30000",
            }
        )
        cache.update(
            {
                "Unique-ID": "b-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "outbound",
                "Other-Leg-Unique-ID": "a-leg",
            }
        )
        correlation_id, _, fallback = cache.resolve({"Unique-ID": "b-leg"})
        self.assertEqual(correlation_id, "call@example.net")
        self.assertFalse(fallback)

    def test_cache_uses_one_canonical_call_id_for_both_sip_legs(self):
        cache = AGENT.CallCache()
        cache.update(
            {
                "Unique-ID": "a-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "inbound",
                "Other-Leg-Unique-ID": "b-leg",
                "variable_sip_call_id": "phone-call-id",
            }
        )
        cache.update(
            {
                "Unique-ID": "b-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "outbound",
                "Other-Leg-Unique-ID": "a-leg",
                "variable_sip_call_id": "carrier-call-id",
            }
        )

        a_call_id, _, _ = cache.resolve({"Unique-ID": "a-leg"})
        b_call_id, _, _ = cache.resolve({"Unique-ID": "b-leg"})

        self.assertEqual(a_call_id, "phone-call-id")
        self.assertEqual(b_call_id, "phone-call-id")

    def test_cache_promotes_late_inbound_call_id_for_call_group(self):
        cache = AGENT.CallCache()
        cache.update(
            {
                "Unique-ID": "b-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "outbound",
                "variable_sip_call_id": "carrier-call-id",
            }
        )
        cache.update(
            {
                "Unique-ID": "a-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "inbound",
                "variable_sip_call_id": "phone-call-id",
            }
        )

        correlation_id, _, _ = cache.resolve({"Unique-ID": "b-leg"})

        self.assertEqual(correlation_id, "phone-call-id")

    def test_cache_uses_own_call_id_for_unbridged_channel(self):
        cache = AGENT.CallCache()
        cache.update(
            {
                "Unique-ID": "single-leg",
                "Channel-Call-UUID": "single-leg",
                "Call-Direction": "outbound",
                "variable_sip_call_id": "single-call-id",
            }
        )

        correlation_id, _, fallback = cache.resolve({"Unique-ID": "single-leg"})

        self.assertEqual(correlation_id, "single-call-id")
        self.assertFalse(fallback)

    def test_cache_marks_uuid_only_correlation_as_fallback(self):
        correlation_id, _, fallback = AGENT.CallCache().resolve(
            {"Unique-ID": "uuid-only"}
        )
        self.assertEqual(correlation_id, "uuid-only")
        self.assertTrue(fallback)

    def test_rtcp_payload_preserves_cumulative_values(self):
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
        diagnostics = AGENT.Diagnostics()
        payload = AGENT.rtcp_payload(event, diagnostics)
        self.assertEqual(payload["sender_information"]["packets"], 100)
        self.assertEqual(payload["sender_information"]["octets"], 10000)
        self.assertEqual(payload["report_blocks"][0]["packets_lost"], 4)
        self.assertEqual(payload["ssrc"], 0x01020304)
        self.assertEqual(payload["report_blocks"][0]["source_ssrc"], 0xAABBCCDD)
        self.assertEqual(payload["type"], 200)
        self.assertEqual(payload["type_source"], "compatibility_fallback")
        self.assertEqual(diagnostics.received_type_fallbacks, 1)

    def test_rtcp_payload_preserves_all_blocks_and_exposes_rtt(self):
        diagnostics = AGENT.Diagnostics()
        payload = AGENT.rtcp_payload(
            {
                "Event-Name": "RECV_RTCP_MESSAGE",
                "SSRC": "01020304",
                "Source0-SSRC": "11111111",
                "Source0-Lost": "1",
                "Rtt0-Avg": "0.012500",
                "Source1-SSRC": "22222222",
                "Source1-Lost": "4294967295",
                "Rtt1-Avg": "0.025",
            },
            diagnostics,
        )
        self.assertEqual(payload["report_count"], 2)
        self.assertEqual(
            [block["source_ssrc"] for block in payload["report_blocks"]],
            [0x11111111, 0x22222222],
        )
        self.assertEqual(payload["report_blocks"][0]["rtt_avg_seconds"], 0.0125)
        self.assertEqual(payload["report_blocks"][0]["rtt_avg_ms"], 12.5)
        self.assertEqual(payload["report_blocks"][1]["packets_lost"], -1)
        self.assertEqual(diagnostics.rtt_blocks, 2)

    def test_rtcp_payload_uses_explicit_receiver_report_type(self):
        payload = AGENT.rtcp_payload(
            {
                "Event-Name": "RECV_RTCP_MESSAGE",
                "RTCP-Packet-Type": "RR",
                "SSRC": "01020304",
                "Source0-SSRC": "11111111",
                "Sender-Packet-Count": "999",
                "Octect-Packet-Count": "9999",
            }
        )
        self.assertEqual(payload["type"], 201)
        self.assertEqual(payload["type_source"], "freeswitch_event")
        self.assertNotIn("sender_information", payload)

    def test_rtcp_payload_rejects_missing_or_invalid_ssrc(self):
        diagnostics = AGENT.Diagnostics()
        self.assertIsNone(
            AGENT.rtcp_payload(
                {
                    "Event-Name": "SEND_RTCP_MESSAGE",
                    "SSRC": "not-hex",
                    "Source-SSRC": "11223344",
                },
                diagnostics,
            )
        )
        self.assertEqual(diagnostics.invalid_sender_ssrc, 1)

    def test_rtcp_payload_skips_invalid_blocks_but_keeps_valid_ones(self):
        diagnostics = AGENT.Diagnostics()
        payload = AGENT.rtcp_payload(
            {
                "Event-Name": "RECV_RTCP_MESSAGE",
                "SSRC": "01020304",
                "Source0-SSRC": "not-hex",
                "Source1-SSRC": "22222222",
            },
            diagnostics,
        )
        self.assertEqual(payload["report_count"], 1)
        self.assertEqual(payload["report_blocks"][0]["source_ssrc"], 0x22222222)
        self.assertEqual(diagnostics.invalid_report_blocks, 1)

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
        diagnostics = AGENT.Diagnostics()
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
            diagnostics,
        )
        self.assertTrue(sent)
        chunks = decode_chunks(sender.messages[0])
        self.assertEqual(chunks[17], b"call-1")
        payload = json.loads(chunks[15])
        self.assertEqual(payload["ssrc"], 0xA1B2C3D4)
        self.assertEqual(payload["report_blocks"][0]["source_ssrc"], 0x11223344)
        self.assertEqual(payload["type_source"], "freeswitch_send_event")
        self.assertEqual(diagnostics.rtcp_events, 1)
        self.assertEqual(diagnostics.send_events, 1)
        self.assertEqual(diagnostics.hep_sent, 1)
        self.assertEqual(diagnostics.peer_to_pbx_sent, 1)
        self.assertEqual(diagnostics.report_blocks_sent, 1)

    def test_handle_event_sends_both_legs_under_inbound_call_id(self):
        config = AGENT.Config(
            "127.0.0.1", 8021, "192.0.2.50", 9060, "udp", 1000, "pbx-1"
        )
        cache = AGENT.CallCache()
        sender = FakeSender()
        diagnostics = AGENT.Diagnostics()
        cache.update(
            {
                "Unique-ID": "a-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "inbound",
                "variable_sip_call_id": "phone-call-id",
            }
        )
        cache.update(
            {
                "Unique-ID": "b-leg",
                "Channel-Call-UUID": "call-group",
                "Call-Direction": "outbound",
                "variable_sip_call_id": "carrier-call-id",
            }
        )
        for unique_id in ("a-leg", "b-leg"):
            sent = AGENT.handle_event(
                {
                    "Event-Name": "SEND_RTCP_MESSAGE",
                    "Unique-ID": unique_id,
                    "SSRC": "a1b2c3d4",
                    "Source-SSRC": "11223344",
                },
                cache,
                sender,
                config,
                diagnostics,
            )
            self.assertTrue(sent)

        correlation_ids = [decode_chunks(message)[17] for message in sender.messages]
        self.assertEqual(correlation_ids, [b"phone-call-id", b"phone-call-id"])
        self.assertEqual(diagnostics.hep_sent, 2)

    def test_handle_event_counts_uuid_correlation_fallback(self):
        config = AGENT.Config(
            "127.0.0.1", 8021, "192.0.2.50", 9060, "udp", 1000, "pbx-1"
        )
        diagnostics = AGENT.Diagnostics()
        sent = AGENT.handle_event(
            {
                "Event-Name": "SEND_RTCP_MESSAGE",
                "Unique-ID": "uuid-only",
                "SSRC": "a1b2c3d4",
                "Source-SSRC": "11223344",
            },
            AGENT.CallCache(),
            FakeSender(),
            config,
            diagnostics,
        )
        self.assertTrue(sent)
        self.assertEqual(diagnostics.correlation_fallbacks, 1)

    def test_config_rejects_non_loopback_esl(self):
        config = AGENT.Config(
            "192.0.2.10", 8021, "192.0.2.50", 9060, "udp", 1000, "pbx-1"
        )
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            config.validate()


if __name__ == "__main__":
    unittest.main()
