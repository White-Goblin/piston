import asyncio
import json
import unittest

from piston_device.sendspin import sink as sk
from piston_device.sendspin.sink import FakeSink, PipeWireSink, SinkError, pulse_percent


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.data, self.closed = proc, bytearray(), False

    def write(self, data):
        if self.proc.broken:
            raise BrokenPipeError()
        self.data += data

    async def drain(self):
        if self.proc.broken:
            raise BrokenPipeError()

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


class FakeStderr:
    def __init__(self, text):
        self.text = text

    async def read(self, n):
        return self.text[:n]


class FakeProc:
    def __init__(self, args, stderr=b""):
        self.args = args
        self.returncode = None
        self.broken = False
        self.stdin = FakeStdin(self)
        self.stderr = FakeStderr(stderr)
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def die(self, code=1):
        self.returncode = code
        self.broken = True


class Env:
    """The world around the sink: spawned processes, a fake pactl that knows the running streams, a fake clock."""

    def __init__(self, appear=True, pactl_fails=False):
        self.procs, self.calls, self.now = [], [], 100.0
        self.appear, self.pactl_fails = appear, pactl_fails
        self.index = 40
        self.stderr = b""

    async def spawn(self, *args, **kwargs):
        proc = FakeProc(args, self.stderr)
        proc.node = next(a for a in args if "node.name=" in a).split("node.name=")[1].split()[0]
        proc.kwargs = kwargs
        self.procs.append(proc)
        return proc

    async def pactl(self, *args):
        self.calls.append(args)
        if self.pactl_fails:
            raise SinkError("pactl failed")
        if args[:2] == ("-f", "json"):
            if not self.appear:
                return "[]"
            live = [p for p in self.procs if p.returncode is None]
            return json.dumps([{"index": self.index + i, "properties": {"node.name": p.node}} for i, p in enumerate(live)])
        return ""

    async def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    def sink(self, **kw):
        return PipeWireSink(spawn=self.spawn, pactl=self.pactl, clock=self.clock, sleep=self.sleep, **kw)

    def sets(self):
        return [c for c in self.calls if c[0].startswith("set-sink-input")]


class VolumeCurveTests(unittest.TestCase):
    def test_the_percentage_follows_the_spec_amplitude_curve(self):
        self.assertEqual([pulse_percent(v) for v in (0, 1, 25, 50, 100)], [0, 10, 50, 71, 100])
        for v in range(0, 101, 5):
            amplitude = (pulse_percent(v) / 100) ** 3          # PulseAudio: amplitude = percentage cubed
            self.assertAlmostEqual(amplitude, (v / 100) ** 1.5, delta=0.02)

    def test_out_of_range_volumes_raise(self):
        for v in (-1, 101):
            with self.assertRaises(ValueError):
                pulse_percent(v)


class PipeWireSinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_starts_the_documented_command_primes_the_stream_and_applies_volume_before_returning(self):
        env = Env()
        s = env.sink(volume=30, muted=True)
        await s.open(48000)
        proc = env.procs[0]
        self.assertEqual(proc.args[:3], ("pw-cat", "--playback", "--raw"))
        for flag, value in (("--format", "s16"), ("--rate", "48000"), ("--channels", "2"), ("--latency", "50ms")):
            self.assertEqual(proc.args[proc.args.index(flag) + 1], value)
        self.assertEqual(proc.args[-1], "-")
        self.assertIn("application.name=Piston", " ".join(proc.args))
        self.assertEqual(len(proc.stdin.data), 960 * 4)   # 20 ms of silence to make the stream appear
        self.assertEqual(set(proc.stdin.data), {0})
        self.assertEqual(proc.kwargs["env"]["XDG_RUNTIME_DIR"], sk.RUNTIME_DIR)
        self.assertEqual(env.sets(), [("set-sink-input-volume", "40", "55%"), ("set-sink-input-mute", "40", "1")])
        self.assertTrue(s.alive)
        self.assertIsNone(s.error)

    async def test_volume_and_mute_changes_reach_the_running_stream(self):
        env = Env()
        s = env.sink()
        await s.open(44100)
        env.calls.clear()
        self.assertTrue(await s.set_volume(100))
        self.assertTrue(await s.set_mute(True))
        self.assertTrue(await s.set_mute(False))
        self.assertEqual(env.sets(), [("set-sink-input-volume", "40", "100%"), ("set-sink-input-mute", "40", "1"), ("set-sink-input-mute", "40", "0")])

    async def test_a_level_set_before_the_stream_exists_is_applied_when_it_starts(self):
        env = Env()
        s = env.sink()
        self.assertTrue(await s.set_volume(25))
        self.assertTrue(await s.set_mute(True))
        self.assertEqual(env.calls, [])
        await s.open(48000)
        self.assertEqual(env.sets(), [("set-sink-input-volume", "40", "50%"), ("set-sink-input-mute", "40", "1")])

    async def test_an_invalid_volume_is_refused(self):
        s = Env().sink()
        with self.assertRaises(ValueError):
            await s.set_volume(101)

    async def test_the_stream_is_found_by_its_own_unique_node_name(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        await s.open(44100)                                 # a new stream: the old one is closed, the new one has its own name
        self.assertNotEqual(env.procs[0].node, env.procs[1].node)
        self.assertTrue(env.procs[0].stdin.closed)
        self.assertEqual(env.sets()[-2][1], str(env.index))

    async def test_a_stream_that_never_appears_fails_the_open_and_kills_the_process(self):
        env = Env(appear=False)
        s = env.sink()
        with self.assertRaises(SinkError):
            await s.open(48000)
        self.assertFalse(s.alive)
        self.assertIn("did not appear", s.error)
        self.assertTrue(env.procs[0].stdin.closed)

    async def test_a_process_that_dies_at_start_reports_why(self):
        env = Env()
        env.stderr = b"error: open failed"

        async def spawn(*a, **k):
            p = await Env.spawn(env, *a, **k)
            p.die(1)
            return p
        s = PipeWireSink(spawn=spawn, pactl=env.pactl, clock=env.clock, sleep=env.sleep)
        with self.assertRaises(SinkError) as cm:
            await s.open(48000)
        self.assertIn("open failed", str(cm.exception))

    async def test_a_missing_pw_cat_is_reported(self):
        async def spawn(*a, **k):
            raise FileNotFoundError("pw-cat")
        s = PipeWireSink(spawn=spawn)
        with self.assertRaises(SinkError):
            await s.open(48000)
        self.assertIn("pw-cat", s.error)

    async def test_pactl_failing_at_open_fails_it(self):
        env = Env(pactl_fails=True)
        with self.assertRaises(SinkError):
            await env.sink().open(48000)

    async def test_unsupported_formats_are_refused(self):
        s = Env().sink()
        for args in ((48000, 2, 24), (48000, 6, 16)):
            with self.assertRaises(SinkError):
                await s.open(*args)

    async def test_write_appends_audio_and_write_silence_is_chunked(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        proc = env.procs[0]
        proc.stdin.data.clear()
        self.assertTrue(await s.write(b"\x01\x02\x03\x04"))
        self.assertEqual(bytes(proc.stdin.data), b"\x01\x02\x03\x04")
        proc.stdin.data.clear()
        self.assertTrue(await s.write_silence(2500))        # 960 + 960 + 580 frames
        self.assertEqual(len(proc.stdin.data), 2500 * 4)

    async def test_writes_before_open_and_after_close_are_dropped(self):
        env = Env()
        s = env.sink()
        self.assertFalse(await s.write(b"x" * 4))
        await s.open(48000)
        await s.close()
        await s.close()                                     # idempotent
        self.assertFalse(await s.write(b"x" * 4))
        self.assertFalse(s.alive)

    async def test_a_broken_pipe_drops_the_write_and_reports_it(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        env.procs[0].broken = True
        self.assertFalse(await s.write(b"x" * 4))
        self.assertEqual(s.error, "audio output closed")

    async def test_a_dead_process_is_restarted_with_backoff_and_audio_is_dropped_meanwhile(self):
        env = Env()
        s = env.sink(volume=40)
        await s.open(48000)
        env.procs[0].die(1)
        self.assertTrue(await s.write(b"x" * 4))            # first restart is allowed at once
        self.assertEqual(len(env.procs), 2)
        self.assertTrue(s.alive)
        self.assertIn(("set-sink-input-volume", str(env.index), "63%"), env.sets())  # the level survives the restart
        env.procs[1].die(1)
        self.assertFalse(await s.write(b"x" * 4))            # inside the backoff window: dropped, not restarted
        self.assertEqual(len(env.procs), 2)
        env.now += 0.6
        self.assertTrue(await s.write(b"x" * 4))
        self.assertEqual(len(env.procs), 3)
        env.procs[2].die(1)
        env.now += 0.6
        self.assertFalse(await s.write(b"x" * 4))            # the backoff grew to 1 s
        env.now += 1.5
        self.assertTrue(await s.write(b"x" * 4))
        self.assertEqual(len(env.procs), 4)

    async def test_a_stream_that_keeps_running_resets_the_backoff(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        for _ in range(2):
            env.procs[-1].die(1)
            env.now += 20
            self.assertTrue(await s.write(b"x" * 4))
        env.now += 30
        self.assertTrue(await s.write(b"x" * 4))             # alive for a long time: failures forgotten
        env.procs[-1].die(1)
        self.assertTrue(await s.write(b"x" * 4))             # restarts at once again

    async def test_flush_starts_a_fresh_stream_with_the_same_format_and_level(self):
        env = Env()
        s = env.sink(volume=20)
        await s.open(44100)
        await s.flush()
        self.assertEqual(len(env.procs), 2)
        self.assertTrue(env.procs[0].stdin.closed)
        self.assertIn("44100", env.procs[1].args)
        self.assertEqual(env.sets()[-2][2], "45%")

    async def test_a_changed_stream_index_is_found_again(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        env.index = 77                                       # PipeWire re-created the stream under another index
        failed = {"n": 0}
        real = env.pactl

        async def pactl(*args):
            if args[0] == "set-sink-input-volume" and args[1] == "40" and not failed["n"]:
                failed["n"] = 1
                raise SinkError("no such sink input")
            return await real(*args)
        s._pactl = pactl
        self.assertTrue(await s.set_volume(10))
        self.assertIn(("set-sink-input-volume", "77", "32%"), env.sets())

    async def test_a_level_that_cannot_be_applied_is_reported_not_raised(self):
        env = Env()
        s = env.sink()
        await s.open(48000)
        env.pactl_fails = True
        self.assertFalse(await s.set_volume(10))
        self.assertTrue(s.error)


class FakeSinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_calls_in_order(self):
        f = FakeSink()
        await f.open(48000)
        await f.set_volume(10)
        await f.write(b"abcd")
        await f.write_silence(2)
        await f.flush()
        await f.close()
        self.assertEqual([e[0] for e in f.events], ["open", "volume", "write", "write", "flush", "close"])
        self.assertEqual(f.written, b"abcd" + bytes(8))

    async def test_can_simulate_failures(self):
        f = FakeSink()
        f.fail_open = True
        with self.assertRaises(SinkError):
            await f.open(48000)
        f.fail_open = False
        await f.open(48000)
        f.accept_writes = False
        self.assertFalse(await f.write(b"x"))
        self.assertFalse(f.alive)


if __name__ == "__main__":
    unittest.main()
