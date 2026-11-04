import json
import unittest

from piston_device.sendspin import crypto, messages as sm

KEY = crypto.x25519_public(bytes(range(32)))
ID = sm.client_id_for(KEY)


def jf(message_type, payload):  # a server-side encrypted JSON plaintext
    return sm.json_frame(message_type, payload)


class IdTests(unittest.TestCase):
    def test_a_client_id_is_the_43_character_base64url_key(self):
        self.assertEqual(len(ID), 43)
        self.assertNotIn("=", ID)
        self.assertEqual(sm.public_key_from_id(ID), KEY)

    def test_bad_ids_are_refused(self):
        for bad in (None, "", "short", ID + "A", ID[:-1] + "=", "!" * 43, 5):
            with self.assertRaises(sm.SendspinProtocolError, msg=repr(bad)):
                sm.public_key_from_id(bad)
        with self.assertRaises(sm.SendspinProtocolError):  # the last character of 43 must have its two low bits clear
            sm.public_key_from_id(ID[:-1] + "B")


class CleartextTests(unittest.TestCase):
    def test_client_init_has_the_envelope_and_fixed_fields(self):
        text = sm.build_client_init(ID)
        self.assertEqual(json.loads(text), {"type": "client/init", "payload": {"client_id": ID, "version": 1, "suite": "25519_ChaChaPoly_SHA256"}})
        self.assertEqual(text, sm.build_client_init(ID))  # the prologue needs the exact same bytes every time

    def test_server_init_round_trip_and_prologue_uses_the_exact_text(self):
        server_text = '{"type":"server/init","payload":{"server_id":"%s","version":1}}' % ID
        self.assertEqual(sm.parse_server_init(server_text), ID)
        client_text = sm.build_client_init(ID)
        self.assertEqual(sm.prologue(client_text, server_text), (client_text + server_text).encode())

    def test_server_init_rejections(self):
        bad = ['{"type":"server/init","payload":{"server_id":"%s","version":2}}' % ID,
               '{"type":"server/init","payload":{"server_id":"x","version":1}}',
               '{"type":"server/init","payload":{"version":1}}', '{"type":"other","payload":{}}', "[]", "not json",
               '{"type":"server/init","payload":[]}']
        for text in bad:
            with self.assertRaises(sm.SendspinProtocolError, msg=text):
                sm.parse_server_init(text)

    def test_a_server_error_names_its_reason(self):
        with self.assertRaises(sm.SendspinProtocolError) as cm:
            sm.parse_server_init('{"type":"server/error","payload":{"reason":"unsupported_suite"}}')
        self.assertIn("unsupported_suite", str(cm.exception))

    def test_noise_handshake_round_trip_unpadded(self):
        for size in (1, 2, 3, 32, 80):
            data = bytes(range(size))
            text = sm.build_noise_handshake(data)
            self.assertNotIn("=", text)
            self.assertEqual(sm.parse_noise_handshake(text), data)

    def test_noise_handshake_rejections(self):
        for text in ('{"type":"noise/handshake","payload":{}}', '{"type":"noise/handshake","payload":{"data":5}}',
                     '{"type":"noise/handshake","payload":{"data":"AAA="}}', '{"type":"noise/handshake","payload":{"data":""}}',
                     '{"type":"x","payload":{"data":"AAAA"}}'):
            with self.assertRaises(sm.SendspinProtocolError, msg=text):
                sm.parse_noise_handshake(text)


class EncryptedFrameTests(unittest.TestCase):
    def test_json_frames_carry_type_byte_zero_and_the_envelope(self):
        frame = sm.json_frame("client/time", {"client_transmitted": 5})
        self.assertEqual(frame[0], 0)
        self.assertEqual(json.loads(frame[1:]), {"type": "client/time", "payload": {"client_transmitted": 5}})
        self.assertEqual(sm.parse_server_message(frame), ("client/time", {"client_transmitted": 5}))

    def test_audio_chunk_round_trip_including_negative_timestamps(self):
        for ts in (0, 1, 1_234_567_890_123, -5):
            chunk = sm.parse_audio_chunk(sm.build_audio_chunk(ts, b"\x01\x02\x03\x04", send_ahead_us=250_000))
            self.assertEqual(chunk, (ts, 250_000, b"\x01\x02\x03\x04"))
        kind, chunk = sm.parse_server_message(sm.build_audio_chunk(7, bytes(3840)))
        self.assertEqual((kind, chunk.timestamp_us, len(chunk.pcm)), ("audio", 7, 3840))

    def test_short_or_wrong_audio_chunks_fail(self):
        for bad in (b"", bytes([4]), bytes([4]) + bytes(11), bytes([5]) + bytes(20)):
            with self.assertRaises(sm.SendspinProtocolError, msg=bad):
                sm.parse_audio_chunk(bad)

    def test_a_header_only_chunk_is_valid_and_empty(self):
        self.assertEqual(sm.parse_audio_chunk(sm.build_audio_chunk(1, b"")).pcm, b"")

    def test_oversized_json_and_deep_json_fail(self):
        with self.assertRaises(sm.SendspinProtocolError):
            sm.parse_server_message(bytes([0]) + json.dumps({"type": "x", "payload": {"a": "b" * 17000}}).encode())
        deep = {"type": "x", "payload": {}}
        node = deep["payload"]
        for _ in range(12):
            node["n"] = {}
            node = node["n"]
        with self.assertRaises(sm.SendspinProtocolError):
            sm.parse_server_message(bytes([0]) + json.dumps(deep).encode())

    def test_malformed_json_frames_fail(self):
        for bad in (bytes([0]) + b"not json", bytes([0]) + b"[]", bytes([0]) + b'{"payload":{}}', bytes([0]) + b'{"type":"x","payload":[]}',
                    bytes([0]) + b"\xff\xfe", b""):
            with self.assertRaises(sm.SendspinProtocolError, msg=bad):
                sm.parse_server_message(bad)

    def test_unused_binary_types_are_passed_through_and_unknown_json_is_unvalidated(self):
        self.assertEqual(sm.parse_server_message(bytes([8, 1, 2])), ("binary", (8, b"\x01\x02")))
        self.assertEqual(sm.parse_server_message(jf("server/future", {"a": 1})), ("server/future", {"a": 1}))
        with self.assertRaises(sm.SendspinProtocolError):
            sm.parse_server_message(bytes([1, 3, 4]))  # an unassembled fragment


class FragmentTests(unittest.TestCase):
    def test_fragments_are_reassembled(self):
        d = sm.Defragmenter()
        body = jf("x/y", {"k": "v" * 100})
        first = bytes([1, 2, body[0]]) + body[1:50]
        middle = bytes([1, 0]) + body[50:80]
        last = bytes([1, 1]) + body[80:]
        self.assertIsNone(d.feed(first))
        self.assertIsNone(d.feed(middle))
        self.assertEqual(d.feed(last), body)
        self.assertEqual(d.feed(body), body)  # unfragmented messages pass straight through afterwards

    def test_a_single_fragment_marked_first_and_last(self):
        d = sm.Defragmenter()
        self.assertEqual(d.feed(bytes([1, 3, 0]) + b"abc"), b"\x00abc")

    def test_bad_fragment_sequences_fail(self):
        cases = [[bytes([1, 0]) + b"x"], [bytes([1, 2, 1]) + b"x"], [bytes([1])], [bytes([1, 2, 0]) + b"a", bytes([1, 2, 0]) + b"b"],
                 [bytes([1, 2, 0]) + b"a", bytes([4]) + bytes(20)]]
        for frames in cases:
            d = sm.Defragmenter()
            with self.assertRaises(sm.SendspinProtocolError, msg=frames):
                for f in frames:
                    d.feed(f)

    def test_reassembly_is_bounded(self):
        d = sm.Defragmenter()
        d.feed(bytes([1, 2, 0]) + bytes(60000))
        with self.assertRaises(sm.SendspinProtocolError):
            for _ in range(20):
                d.feed(bytes([1, 0]) + bytes(60000))


class ServerMessageTests(unittest.TestCase):
    def ok(self, message_type, payload):
        self.assertEqual(sm.parse_server_message(jf(message_type, payload)), (message_type, payload))

    def bad(self, message_type, payload):
        with self.assertRaises(sm.SendspinProtocolError, msg=(message_type, payload)):
            sm.parse_server_message(jf(message_type, payload))

    def test_server_hello_and_activate(self):
        self.ok("server/hello", {"name": "Music Assistant", "languages": ["en"]})
        self.bad("server/hello", {"name": "x" * 65})
        self.bad("server/hello", {})
        self.ok("server/activate", {"activities": ["playback"], "active_roles": ["player@v1"]})
        self.ok("server/activate", {"activities": []})
        self.bad("server/activate", {"activities": ["dancing"]})
        self.bad("server/activate", {"activities": "playback"})

    def test_server_state_metadata_and_controller_from_the_capture(self):
        meta = {"timestamp": 5, "title": "404", "artist": "Knife Party", "album_artist": "Knife Party", "album": "Abandon Ship",
                "artwork_url": "http://host/x?size=512", "year": 2014, "track": 5,
                "progress": {"track_progress": 39, "track_duration": 297000, "playback_speed": 1000}}
        self.ok("server/state", {"metadata": meta})
        self.ok("server/state", {"metadata": {"timestamp": 5}})
        self.ok("server/state", {"metadata": {"timestamp": 5, "title": None, "progress": None, "extra": [1]}})
        control = {"supported_commands": ["play", "pause", "next"], "volume": 20, "muted": False, "repeat": "off", "shuffle": False,
                   "seek_max_ms": 1000}
        self.ok("server/state", {"controller": control})
        self.ok("server/state", {"other": 1})

    def test_server_state_rejects_wrong_types_and_oversized_text(self):
        for meta in ({"title": 5}, {"title": "é" * 600}, {"year": "2014"}, {"track": True}, {"progress": 3},
                     {"progress": {"track_progress": -1}}, {"progress": {"track_duration": "x"}}, {"timestamp": 1.5}, "x"):
            self.bad("server/state", {"metadata": meta})
        for ctl in ({"volume": 101}, {"volume": "5"}, {"muted": 1}, {"supported_commands": "play"}, {"seek_max_ms": -3}, []):
            self.bad("server/state", {"controller": ctl})

    def test_unknown_controller_commands_are_tolerated_and_filtered(self):
        _, payload = sm.parse_server_message(jf("server/state", {"controller": {"supported_commands": ["play", "teleport", 5, "next"]}}))
        self.assertEqual(payload["controller"]["supported_commands"], ["play", "next"])

    def test_client_command_accepts_only_the_four_card_commands(self):
        for command in ("previous", "play", "pause", "next"):
            frame = json.loads(sm.build_client_command(command)[1:])
            self.assertEqual(frame, {"type": "client/command", "payload": {"controller": {"command": command}}})
        for command in ("stop", "volume", "shuffle", "", None, 5):
            with self.assertRaises(sm.SendspinProtocolError):
                sm.build_client_command(command)

    def test_the_artwork_channel_is_in_client_state_and_never_in_the_hello(self):
        hello = json.loads(sm.build_client_hello("Box", 1000)[1:])["payload"]
        self.assertNotIn("artwork@v1_support", hello)
        self.assertNotIn("artwork", hello)
        state = json.loads(sm.build_client_state(True, 20, False, 0)[1:])["payload"]
        self.assertEqual(state["artwork"], {"channels": [{"source": "album", "format": "jpeg", "width": 512, "height": 512}]})

    def test_stream_start_accepts_only_our_formats(self):
        fmt = {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}
        self.ok("stream/start", {"server_transmitted": 5, "player": fmt})
        self.ok("stream/start", {"server_transmitted": 5, "player": {**fmt, "sample_rate": 44100}})
        for change in ({"codec": "flac"}, {"sample_rate": 96000}, {"channels": 1}, {"bit_depth": 24}):
            self.bad("stream/start", {"server_transmitted": 5, "player": {**fmt, **change}})
        self.bad("stream/start", {"player": fmt})
        self.bad("stream/start", {"server_transmitted": 5, "player": "pcm"})

    def test_stream_clear_and_end(self):
        self.ok("stream/clear", {"server_transmitted": 1, "from_timestamp": 10, "roles": ["player"]})
        self.ok("stream/clear", {"server_transmitted": 1})
        self.bad("stream/clear", {"server_transmitted": 1, "roles": []})
        self.bad("stream/clear", {"server_transmitted": 1, "from_timestamp": "x"})
        self.ok("stream/end", {})
        self.ok("stream/end", {"roles": ["player", "artwork"]})
        self.bad("stream/end", {"roles": ["player", "nope"]})

    def test_server_time(self):
        self.ok("server/time", {"client_transmitted": 1, "server_received": 2, "server_transmitted": 3})
        self.bad("server/time", {"client_transmitted": 1, "server_received": 2})

    def test_player_commands(self):
        self.ok("server/command", {"player": {"command": "volume", "volume": 37}})
        self.ok("server/command", {"player": {"command": "mute", "mute": True}})
        self.ok("server/command", {"player": {"command": "set_output_delay", "output_delay_ms": 250}})
        self.ok("server/command", {"controller": {"command": "x"}})  # for a role we do not have: left to the caller
        for player in ({"command": "volume", "volume": 101}, {"command": "volume", "volume": -1}, {"command": "volume", "volume": True},
                       {"command": "volume"}, {"command": "mute", "mute": 1}, {"command": "set_output_delay", "output_delay_ms": 5001},
                       {"command": "explode"}, {}, "volume"):
            self.bad("server/command", {"player": player})

    def test_group_update(self):
        self.ok("group/update", {"playback_state": "playing", "group_id": "g", "group_name": "Kitchen"})
        self.bad("group/update", {"playback_state": "paused"})


# The codec_header Music Assistant really sent for a 24-bit, 44.1 kHz track (captured 2026-11-04)
REAL_FLAC_HEADER = "ZkxhQ4AAACISABIAAAAAAG5aCsRDcAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def flac_header(rate=44100, channels=2, depth=24, last=True, length=34, marker=b"fLaC"):
    """A fLaC marker + STREAMINFO block with the given values, Base64 encoded."""
    info = (rate << 44) | ((channels - 1) << 41) | ((depth - 1) << 36)
    block = bytes([(0x80 if last else 0) | 0]) + length.to_bytes(3, "big") + bytes(10) + info.to_bytes(8, "big") + bytes(16)
    return __import__("base64").b64encode(marker + block[:4] + block[4:][:34].ljust(34, b"\0")).decode()


class FlacHeaderTests(unittest.TestCase):
    def test_the_header_music_assistant_really_sent_parses(self):
        info = sm.parse_flac_header(REAL_FLAC_HEADER)
        self.assertEqual((info["sample_rate"], info["channels"], info["bit_depth"]), (44100, 2, 24))
        self.assertEqual(len(info["bytes"]), 42)
        self.assertEqual(info["bytes"][:5], b"fLaC\x80")

    def test_generated_headers_round_trip_the_values(self):
        for rate, depth in ((48000, 24), (44100, 16), (48000, 16), (44100, 24)):
            info = sm.parse_flac_header(flac_header(rate=rate, depth=depth))
            self.assertEqual((info["sample_rate"], info["bit_depth"]), (rate, depth))

    def test_the_last_block_flag_is_set_when_the_server_left_it_out(self):
        info = sm.parse_flac_header(flac_header(last=False))
        self.assertEqual(info["bytes"][4] & 0x80, 0x80)

    def test_bad_headers_are_rejected(self):
        bad = [None, 5, "", "not base64!!", REAL_FLAC_HEADER[:-4], flac_header(marker=b"fLaX"),
               flac_header(length=35), "A" * 9000, __import__("base64").b64encode(b"fLaC" + bytes(100)).decode(),
               __import__("base64").b64encode(b"fLaC\x01" + bytes([0, 0, 34]) + bytes(34)).decode()]   # a non-STREAMINFO block type
        for header in bad:
            with self.assertRaises(sm.SendspinProtocolError, msg=repr(header)[:40]):
                sm.parse_flac_header(header)


class FormatTableTests(unittest.TestCase):
    def names(self, formats):
        return [(f["codec"], f["sample_rate"], f["bit_depth"]) for f in formats]

    def test_pcm_only_by_default(self):
        self.assertEqual(self.names(sm.offered_formats()), [("pcm", 48000, 16), ("pcm", 44100, 16)])

    def test_the_full_table_in_the_preferred_order(self):
        self.assertEqual(self.names(sm.offered_formats({"flac", "opus"})), [
            ("flac", 48000, 24), ("flac", 44100, 24), ("flac", 48000, 16), ("flac", 44100, 16),
            ("pcm", 48000, 16), ("pcm", 44100, 16), ("opus", 48000, 16)])

    def test_a_codec_that_did_not_load_is_left_out_and_pcm_stays(self):
        self.assertEqual([f["codec"] for f in sm.offered_formats({"opus"})], ["pcm", "pcm", "opus"])
        self.assertEqual([f["codec"] for f in sm.offered_formats({"flac"})], ["flac"] * 4 + ["pcm", "pcm"])
        self.assertEqual({f["channels"] for f in sm.offered_formats({"flac", "opus"})}, {2})

    def test_the_hello_lists_exactly_the_offered_formats(self):
        import json
        hello = json.loads(sm.build_client_hello("Box", 1_000_000, available={"flac", "opus"})[1:])
        self.assertEqual(hello["payload"]["player@v1_support"]["supported_formats"], sm.offered_formats({"flac", "opus"}))

    def validate(self, **change):
        fmt = {"codec": "flac", "sample_rate": 44100, "channels": 2, "bit_depth": 24, "codec_header": REAL_FLAC_HEADER}
        return sm.validate_stream_format({**fmt, **change})

    def test_valid_streams_for_each_codec(self):
        flac = self.validate()
        self.assertEqual((flac["codec"], flac["sample_rate"], flac["bit_depth"]), ("flac", 44100, 24))
        self.assertEqual(flac["header"][:5], b"fLaC\x80")
        self.assertEqual(self.validate(codec_header=flac_header(48000, 2, 16), sample_rate=48000, bit_depth=16)["bit_depth"], 16)
        self.assertNotIn("header", sm.validate_stream_format({"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}))
        self.assertEqual(sm.validate_stream_format({"codec": "opus", "sample_rate": 48000, "channels": 2, "bit_depth": 16})["codec"], "opus")
        self.assertEqual(sm.validate_stream_format({"codec": "opus", "sample_rate": 48000, "channels": 2})["codec"], "opus")  # depth ignored

    def test_flac_streams_that_disagree_with_their_header_or_the_table_are_rejected(self):
        for change in ({"codec_header": None}, {"codec_header": "AAAA"}, {"sample_rate": 48000}, {"bit_depth": 16}, {"channels": 1},
                       {"sample_rate": 96000, "codec_header": flac_header(96000, 2, 24)}, {"bit_depth": 32, "codec_header": flac_header(44100, 2, 32)},
                       {"codec_header": flac_header(44100, 1, 24)}):
            with self.assertRaises(sm.SendspinProtocolError, msg=change):
                self.validate(**change)

    def test_other_codecs_and_formats_are_rejected(self):
        for fmt in ({"codec": "mp3", "sample_rate": 44100, "channels": 2, "bit_depth": 16},
                    {"codec": "opus", "sample_rate": 44100, "channels": 2, "bit_depth": 16},
                    {"codec": "opus", "sample_rate": 48000, "channels": 1, "bit_depth": 16},
                    {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 24},
                    {"codec": "pcm", "sample_rate": 96000, "channels": 2, "bit_depth": 16},
                    {"codec": "pcm", "sample_rate": "48000", "channels": 2, "bit_depth": 16}, None, "pcm"):
            with self.assertRaises(sm.SendspinProtocolError, msg=fmt):
                sm.validate_stream_format(fmt)

    def test_stream_start_validation_covers_flac_and_the_client_state_never_leaks_the_header_bytes(self):
        payload = {"server_transmitted": 1, "player": {"codec": "flac", "sample_rate": 44100, "channels": 2, "bit_depth": 24,
                                                       "codec_header": REAL_FLAC_HEADER}}
        self.assertEqual(sm.parse_server_message(sm.json_frame("stream/start", payload))[0], "stream/start")
        import json
        state = json.loads(sm.build_client_state(True, 20, False, 0, fmt=payload["player"])[1:])
        self.assertEqual(state["payload"]["player"]["format"], {"codec": "flac", "sample_rate": 44100, "channels": 2, "bit_depth": 24})


class BuilderTests(unittest.TestCase):
    def parsed(self, frame):
        return json.loads(frame[1:])

    def test_client_hello_announces_only_the_player_role_with_pcm(self):
        hello = self.parsed(sm.build_client_hello("Box", 1_000_000, device_info={"product_name": "Piston"}))
        p = hello["payload"]
        self.assertEqual(hello["type"], "client/hello")
        self.assertEqual((p["name"], p["supported_roles"], p["unpaired_access"]), ("Box", ["player@v1", "metadata@v1", "artwork@v1", "controller@v1"], {"enabled": True}))
        support = p["player@v1_support"]
        self.assertEqual(support["buffer_capacity"], 1_000_000)
        self.assertEqual([(f["codec"], f["channels"], f["sample_rate"], f["bit_depth"]) for f in support["supported_formats"]],
                         [("pcm", 2, 48000, 16), ("pcm", 2, 44100, 16)])
        self.assertIn("pairing_psk", p["supported_pair_methods"])
        self.assertEqual(p["device_info"], {"product_name": "Piston"})
        self.assertFalse(self.parsed(sm.build_client_hello("Box", 1, unpaired_access=False))["payload"]["unpaired_access"]["enabled"])

    def test_client_hello_validation(self):
        for call in (lambda: sm.build_client_hello("x" * 65, 1), lambda: sm.build_client_hello("é" * 33, 1), lambda: sm.build_client_hello("Box", 0),
                     lambda: sm.build_client_hello("Box", 1, device_info={"manufacturer": "m" * 65})):
            with self.assertRaises(sm.SendspinProtocolError):
                call()
        sm.build_client_hello("é" * 32, 1)  # 64 bytes is allowed

    def test_client_state_shape(self):
        state = self.parsed(sm.build_client_state(True, 20, False, 0))
        player = state["payload"]["player"]
        self.assertEqual(state["type"], "client/state")
        self.assertIs(state["payload"]["available"], True)
        self.assertEqual((player["volume"], player["muted"], player["output_delay_ms"]), (20, False, 0))
        self.assertEqual(player["supported_commands"], ["volume", "mute", "set_output_delay"])
        self.assertNotIn("format", player)
        self.assertEqual(self.parsed(sm.build_client_state(False, 0, True, 5000, fmt={"codec": "pcm", "sample_rate": 44100, "channels": 2, "bit_depth": 16}))
                         ["payload"]["player"]["format"]["sample_rate"], 44100)
        for call in (lambda: sm.build_client_state(True, 101, False, 0), lambda: sm.build_client_state(True, 0, False, 5001),
                     lambda: sm.build_client_state(True, 0, False, 0, fmt={"codec": "flac"})):
            with self.assertRaises(sm.SendspinProtocolError):
                call()

    def test_client_time_and_goodbye(self):
        self.assertEqual(self.parsed(sm.build_client_time(123))["payload"], {"client_transmitted": 123})
        self.assertEqual(self.parsed(sm.build_client_goodbye("shutdown")), {"type": "client/goodbye", "payload": {"reason": "shutdown"}})
        with self.assertRaises(sm.SendspinProtocolError):
            sm.build_client_goodbye("because")


if __name__ == "__main__":
    unittest.main()
