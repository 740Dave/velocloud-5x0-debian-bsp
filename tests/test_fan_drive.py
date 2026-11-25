# SPDX-License-Identifier: GPL-2.0
"""Off-box tests of the drive-temperature fan input (VBSP-REQ-14, 14A-14E, VBSP-DES-9;
VBSP-TEST-14, and the build parts of VBSP-TEST-17) against a fake sysfs and fake I2C."""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import importlib.machinery
import importlib.util

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_fan as tf      # noqa: E402  (loads the daemon once, reuses its fake bus)

fan = tf.fan
SRC = os.path.abspath(os.path.join(HERE, ".."))


def read(p):
    with open(p) as f:
        return f.read()


class FakeSys:
    """A throwaway /sys with hwmon directories."""

    def __init__(self, td):
        self.root = td
        os.makedirs(os.path.join(td, "class/hwmon"), exist_ok=True)

    def add(self, hw, name, temp_c=None, block=None, raw=None):
        d = os.path.join(self.root, "class/hwmon", hw)
        os.makedirs(d, exist_ok=True)
        tf.put(os.path.join(d, "name"), name + "\n")
        if block:
            os.makedirs(os.path.join(d, "device/block", block), exist_ok=True)
        if raw is not None:
            tf.put(os.path.join(d, "temp1_input"), raw)
        elif temp_c is not None:
            tf.put(os.path.join(d, "temp1_input"), "%d\n" % round(temp_c * 1000))

    def set(self, hw, temp_c):
        tf.put(os.path.join(self.root, "class/hwmon", hw, "temp1_input"), "%d\n" % round(temp_c * 1000))

    def remove(self, hw):
        d = os.path.join(self.root, "class/hwmon", hw)
        for dp, dns, fns in os.walk(d, topdown=False):
            for f in fns:
                os.remove(os.path.join(dp, f))
            for x in dns:
                os.rmdir(os.path.join(dp, x))
        os.rmdir(d)


def make_drive(sysfs=None, temps=None, cfg=None, drive_fn="sys", state_file=None, alert_file=None):
    raw = tf.FakeI2C()
    log = tf.Logs()
    cfg_in = cfg
    seq = list(temps or [40.0])
    st = {"i": 0}

    def temp_fn():
        v = seq[min(st["i"], len(seq) - 1)]
        st["i"] += 1
        return v
    fn = fan.DriveSource(sysfs=sysfs.root).poll if drive_fn == "sys" else drive_fn
    cfg = dict(cfg or fan.DEFAULTS)
    if "drive_min_run_s" not in (cfg_in or {}):
        cfg["drive_min_run_s"] = 0      # the pre-0.5.1 tests measure the bands, not the minimum run
    c = fan.Controller(fan.GuardedBus(raw, log), temp_fn, cfg, log, sleep=raw.clock.sleep,
                       clock=raw.clock, alert_file=alert_file, state_file=state_file, drive_fn=fn)
    c.identify()
    c.configure()
    return c, raw, log, st, seq


def cpu(c, st, seq, t):
    """Set the CPU temperature the next step reads."""
    seq[:] = [t]
    st["i"] = 0


class TestDriveDemandFunction(unittest.TestCase):
    def test_bands(self):
        cfg = fan.DEFAULTS
        # (T, on-before) -> (duty, on-after)
        cases = [(None, True, 0, False), (50, False, 0, False), (55.0, False, 0, False), (55.0, True, 0, False),
                 (57, False, 0, False), (57, True, 69, True), (59.9, False, 0, False), (60, False, 69, True),
                 (62.5, False, 69, True), (65, False, 69, True), (66, False, 106, True), (67.5, False, 162, True),
                 (69.9, False, 251, True), (70, False, 255, True), (75, False, 255, True), (75, True, 255, True)]
        for t, on, duty, on2 in cases:
            self.assertEqual(fan.drive_demand(t, on, cfg), (duty, on2), (t, on))

    def test_shipped_minimum_is_above_stall_free_and_matches_spec(self):
        d = fan.DEFAULTS
        self.assertEqual((d["drive_off_c"], d["drive_on_c"], d["drive_ramp_c"], d["drive_full_c"], d["drive_min_duty"]),
                         (55, 60, 65, 70, 69))
        self.assertGreater(d["drive_min_duty"], d["stall_free_duty"])
        # and never between 0 and stall-free, nor above full, over the whole range
        for t10 in range(300, 900):
            for on in (False, True):
                du, _ = fan.drive_demand(t10 / 10.0, on, d)
                self.assertTrue(du == 0 or d["stall_free_duty"] < du <= 255)

    def test_monotonic_when_on(self):
        last = 0
        for t10 in range(560, 900):
            du, _ = fan.drive_demand(t10 / 10.0, True, fan.DEFAULTS)
            self.assertGreaterEqual(du, last)
            last = du

    def test_config_validation(self):
        # a drive minimum that is not above stall-free is clamped, not refused (v0.4 configs stay valid)
        for bad in (24, 25, 300):
            cfg = dict(fan.DEFAULTS, drive_min_duty=bad)
            c = tf.make(cfg=cfg)[0]
            self.assertEqual(c.cfg["drive_min_duty"], int(fan.DEFAULTS["stall_free_duty"]) + 1, bad)
        with self.assertRaises(ValueError):
            tf.make(cfg=dict(fan.DEFAULTS, drive_off_c=61))
        with self.assertRaises(ValueError):
            tf.make(cfg=dict(fan.DEFAULTS, drive_full_c=65))

    def test_v04_config_files_still_valid(self):
        """A v0.4 fan.conf (no drive keys), with and without a raised stall_free_duty."""
        old = os.path.join(SRC, "tests/fixtures/fan.conf-v0.4")
        for extra, want_min in (("", 69), ("stall_free_duty = 80\n", 81), ("stall_free_duty = 69\n", 70),
                                ("stall_free_duty = 255\n", 255)):
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "fan.conf")
                body = read(old)
                if extra:
                    k = extra.split("=")[0].strip()
                    body = "".join(l for l in body.splitlines(True) if not l.strip().startswith(k)) + extra
                tf.put(p, body)
                cfg = fan.load_config(p)
                c, raw, log = tf.make(cfg=cfg)
                self.assertEqual(c.cfg["drive_min_duty"], want_min, extra)
                if want_min != 69:
                    self.assertTrue(log.has("warning", "drive_min_duty"))

    def test_load_config_reads_drive_keys_and_shipped_conf_matches_defaults(self):
        shipped = fan.load_config(os.path.join(SRC, "fan/fan.conf"))
        for k in ("drive_off_c", "drive_on_c", "drive_ramp_c", "drive_full_c", "drive_min_duty"):
            self.assertEqual(shipped[k], fan.DEFAULTS[k], k)
        self.assertIsInstance(shipped["drive_min_duty"], int)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "fan.conf")
            tf.put(p, "[fan]\ndrive_min_duty = 80\ndrive_on_c = 58\n")
            c = fan.load_config(p)
            self.assertEqual((c["drive_min_duty"], c["drive_on_c"]), (80, 58.0))
            self.assertEqual(c["drive_full_c"], 70.0)


class TestDriveSource(unittest.TestCase):
    def test_discovery_filters_and_names(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon0", "coretemp", 45)
            s.add("hwmon1", "drivetemp", 58.5, block="sda")
            s.add("hwmon2", "nvme", 40)
            s.add("hwmon3", "drivetemp", raw="garbage\n", block="sdc")
            s.add("hwmon4", "drivetemp", 41.0)             # no block dir: hwmon name
            src, d = fan.DriveSource(sysfs=td).poll()
            self.assertEqual(src, "drivetemp")
            self.assertEqual(d, {"sda": 58.5, "sdc": None, "hwmon4": 41.0})

    def test_nothing_without_drivetemp_and_no_smartctl(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSys(td).add("hwmon0", "coretemp", 45)
            self.assertEqual(fan.DriveSource(sysfs=td, which=lambda n: None).poll(), (None, {}))

    def test_smartctl_fallback_only_without_drivetemp_and_once_a_minute(self):
        calls = []

        def run(args, timeout):
            calls.append((list(args), timeout))
            if args[0] == "--scan":
                return json.dumps({"devices": [{"name": "/dev/sda", "type": "sat"}, {"name": "/dev/sdb", "type": "scsi"}]})
            if args[-1] == "/dev/sda":
                return json.dumps({"temperature": {"current": 63}})
            return json.dumps({"smartctl": {"exit_status": 4}})      # onboard flash: no temperature

        clock = tf.FakeClock()
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            src = fan.DriveSource(sysfs=td, which=lambda n: "/usr/sbin/smartctl", run=run, clock=clock)
            self.assertEqual(src.poll(), ("smartctl", {"sda": 63.0}))
            n = len(calls)
            self.assertEqual(n, 3)                                    # scan + two devices
            for _, to in calls:
                self.assertTrue(0 < to <= fan.SMARTCTL_CMD_TIMEOUT_S)
            clock.t += 30
            self.assertEqual(src.poll(), ("smartctl", {"sda": 63.0}))
            self.assertEqual(len(calls), n)                           # cached, not re-run
            clock.t += 31
            src.poll()
            self.assertEqual(len(calls), 2 * n)                       # a minute on: one more pass
            s.add("hwmon1", "drivetemp", 50.0, block="sda")           # drivetemp appears: wins, no more smartctl
            clock.t += 120
            self.assertEqual(src.poll(), ("drivetemp", {"sda": 50.0}))
            self.assertEqual(len(calls), 2 * n)

    def test_smartctl_timeout_and_garbage_do_not_raise(self):
        def run(args, timeout):
            if args[0] == "--scan":
                return json.dumps({"devices": [{"name": "/dev/sda"}, {"name": "/dev/sdb"}, {"name": "x"}]})
            if args[-1] == "/dev/sda":
                raise subprocess.TimeoutExpired("smartctl", timeout)
            return "not json"
        with tempfile.TemporaryDirectory() as td:
            src = fan.DriveSource(sysfs=td, which=lambda n: "x", run=run, clock=tf.FakeClock())
            self.assertEqual(src.poll(), ("smartctl", {"sda": None}))
        def boom(args, timeout):
            raise OSError("no smartctl")
        with tempfile.TemporaryDirectory() as td:
            src = fan.DriveSource(sysfs=td, which=lambda n: "x", run=boom, clock=tf.FakeClock())
            self.assertEqual(src.poll(), (None, {}))


class TestDriveBands(unittest.TestCase):
    def run_drive_temps(self, temps, cpu_t=40.0):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", temps[0], block="sda")
            c, raw, log, st, seq = make_drive(s, temps=[cpu_t])
            out = []
            for t in temps:
                s.set("hwmon1", t)
                cpu(c, st, seq, cpu_t)
                c.step()
                raw.clock.sleep(2)
                out.append(c.duty)
            return out, raw, log

    def test_every_band_rising(self):
        out, raw, log = self.run_drive_temps([50, 55, 57, 60, 62.5, 65, 66, 67.5, 70, 75])
        self.assertEqual(out, [0, 0, 0, 69, 69, 69, 106, 162, 255, 255])
        self.assertFalse(log.has("crit", ""))
        self.assertFalse(log.has("warning", ""))

    def test_hysteresis_falling(self):
        out, raw, log = self.run_drive_temps([62, 61, 59, 57, 55.5, 55.0, 57, 59])
        self.assertEqual(out, [69, 69, 69, 69, 69, 0, 0, 0])
        for d in raw.duty_writes():
            self.assertTrue(d == 0 or d >= 69 or d == 255)

    def test_falling_from_full_stays_at_least_min(self):
        out, _, _ = self.run_drive_temps([72, 68, 64, 58, 56, 54])
        self.assertEqual(out[:5], [255, 69 + round(186 * 3 / 5), 69, 69, 69])
        self.assertEqual(out[5], 0)

    def test_max_with_cpu_demand(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 50, block="sda")
            c, raw, log, st, seq = make_drive(s)
            for cpu_t, drv, expect in ((85, 50, 255), (40, 67.5, 162), (40, 50, 0)):
                s.set("hwmon1", drv)
                cpu(c, st, seq, cpu_t)
                c.step()
                self.assertEqual(c.duty, expect, (cpu_t, drv))
            # CPU in its band, drive lower: CPU wins; drive higher: drive wins
            cpu_d = fan.target_duty(65, fan.DEFAULTS)
            s.set("hwmon1", 62)
            cpu(c, st, seq, 65)
            c.step()
            self.assertEqual(c.duty, max(cpu_d, 69))
            self.assertEqual(c.duty, cpu_d)
            s.set("hwmon1", 69.5)
            c.step()
            self.assertEqual(c.duty, fan.drive_demand(69.5, True, fan.DEFAULTS)[0])

    def test_random_walk_never_below_either_demand(self):
        rnd = random.Random(7)
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 40, block="sda")
            c, raw, log, st, seq = make_drive(s)
            ct, dt, on = 45.0, 50.0, False
            for _ in range(3000):
                ct = max(20.0, min(95.0, ct + rnd.uniform(-3, 3)))
                dt = round(max(30.0, min(80.0, dt + rnd.uniform(-2, 2))), 3)
                s.set("hwmon1", dt)
                cpu(c, st, seq, ct)
                c.step()
                dd, on = fan.drive_demand(dt, on, fan.DEFAULTS)
                self.assertGreaterEqual(c.duty, dd)
                if c.state == c.RUN:
                    self.assertGreaterEqual(c.duty, fan.target_duty(ct, fan.DEFAULTS))
                self.assertTrue(c.duty == 0 or c.duty >= fan.DEFAULTS["stall_free_duty"])
            self.assertTrue(all(d == 0 or d >= 24 for d in raw.duty_writes()))

    def test_fan_kicked_when_drive_starts_a_stopped_fan(self):
        out, raw, log = self.run_drive_temps([50, 61])
        w = raw.duty_writes()
        self.assertEqual(w[-2:], [255, 69])          # kick, then the minimum drive duty


class TestNoDriveIsV04(unittest.TestCase):
    def sequence(self):
        rnd = random.Random(11)
        t, out = 45.0, []
        for _ in range(2500):
            t = max(25.0, min(95.0, t + rnd.uniform(-3, 3)))
            out.append(t)
        return out

    def run_cpu(self, temps, drive_fn, sysfs=None):
        raw = tf.FakeI2C()
        log = tf.Logs()
        seq = iter(temps)
        c = fan.Controller(fan.GuardedBus(raw, log), lambda: next(seq), dict(fan.DEFAULTS), log, sleep=raw.clock.sleep,
                           clock=raw.clock, drive_fn=drive_fn)
        c.identify()
        c.configure()
        duties, states = [], []
        for _ in temps:
            c.step()
            raw.clock.sleep(2)
            duties.append(c.duty)
            states.append(c.state)
        return duties, states, raw.writes, log

    def test_no_drivetemp_no_smartctl_equals_cpu_only(self):
        temps = self.sequence()
        base = self.run_cpu(temps, None)
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon0", "coretemp", 50)
            s.add("hwmon1", "nvme", 30)
            got = self.run_cpu(temps, fan.DriveSource(sysfs=td, which=lambda n: None).poll)
        self.assertEqual(got[0], base[0])
        self.assertEqual(got[1], base[1])
        self.assertEqual(got[2], base[2])          # identical bus write sequence, incl. kicks
        extra = [m for m in got[3] if m not in base[3]]
        self.assertEqual(extra, [("info", "no drive temperature input")])
        self.assertEqual([m for m in base[3] if m not in got[3]], [])
        self.assertEqual(sum(1 for l, m in got[3] if m == "no drive temperature input"), 1)

    def test_no_drive_log_line_once_and_again_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            c, raw, log, st, seq = make_drive(s)
            for _ in range(5):
                c.step()
            self.assertEqual(sum(1 for l, m in log if m == "no drive temperature input"), 1)
            s.add("hwmon1", "drivetemp", 40, block="sda")
            for _ in range(3):
                c.step()
            self.assertTrue(log.has("info", "drive sda: temperature input present"))
            self.assertEqual(sum(1 for l, m in log if m == "no drive temperature input"), 1)
            s.remove("hwmon1")
            for _ in range(3):
                c.step()
            self.assertEqual(sum(1 for l, m in log if m == "no drive temperature input"), 2)
            self.assertFalse(log.has("crit", ""))
            self.assertFalse(log.has("err", ""))

    def test_only_a_drive_that_reports_nothing(self):
        # onboard flash: no drivetemp hwmon at all (sdb), same as no drive
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon0", "coretemp", 50)
            c, raw, log, st, seq = make_drive(s, temps=[40.0])
            c.step()
            self.assertEqual(c.duty, 0)
            self.assertIsNone(c.drive_name)
            self.assertFalse(c.drive_on)


class TestDriveFailureAndChange(unittest.TestCase):
    def test_read_error_is_not_a_failsafe_and_clears_demand(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            sd = os.path.join(td, "sys")
            os.makedirs(sd)
            s = FakeSys(sd)
            s.add("hwmon1", "drivetemp", 66, block="sda")
            c, raw, log, st, seq = make_drive(s, alert_file=alert)
            c.step()
            self.assertGreater(c.duty, 69)
            tf.put(os.path.join(sd, "class/hwmon/hwmon1/temp1_input"), "oops\n")
            for _ in range(5):
                c.step()
            self.assertEqual(c.duty, 0)                    # CPU cool, drive dropped
            self.assertTrue(c.drive_on is False)
            self.assertNotIn(255, raw.duty_writes()[1:])
            self.assertFalse(os.path.exists(alert))
            self.assertFalse(log.has("crit", ""))
            self.assertFalse(c.full_failed)
            self.assertEqual(sum(1 for l, m in log if "temperature read failed" in m), 1)   # once per change
            self.assertFalse(c.drives["sda"]["reporting"])
            # recovers: the demand returns, logged once
            s.set("hwmon1", 62)
            c.step()
            self.assertEqual(c.duty, 69)
            self.assertTrue(log.has("info", "drive sda: temperature input present"))

    def test_read_error_never_writes_full_duty(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            c, raw, log, st, seq = make_drive(s)
            c.step()
            raw.writes.clear()
            tf.put(os.path.join(td, "class/hwmon/hwmon1/temp1_input"), "")
            for _ in range(4):
                c.step()
            self.assertNotIn(255, raw.duty_writes())
            self.assertNotIn("0xff", " ".join(m for _, m in log if "fail-safe" in m or "full duty" in m))

    def test_disappearance_while_hot_and_while_cool(self):
        for temp, expect_after in ((68.0, 0), (45.0, 0)):
            with tempfile.TemporaryDirectory() as td:
                s = FakeSys(td)
                s.add("hwmon1", "drivetemp", temp, block="sda")
                c, raw, log, st, seq = make_drive(s)
                c.step()
                s.remove("hwmon1")
                raw.writes.clear()
                for _ in range(3):
                    c.step()
                self.assertEqual(c.duty, expect_after)
                self.assertNotIn(255, raw.duty_writes())
                self.assertEqual(sum(1 for l, m in log if "gone" in m), 1)
                self.assertFalse(c.drive_on)
                self.assertFalse(c.drives["sda"]["reporting"])
                self.assertFalse(log.has("crit", ""))

    def test_drive_appears_mid_run_and_second_drive_hottest_wins(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            c, raw, log, st, seq = make_drive(s)
            c.step()
            self.assertEqual(c.duty, 0)
            s.add("hwmon1", "drivetemp", 62, block="sda")        # no restart
            c.step()
            self.assertEqual((c.duty, c.drive_name), (69, "sda"))
            s.add("hwmon2", "drivetemp", 67.5, block="sdb")
            c.step()
            self.assertEqual((c.duty, c.drive_name, c.drive_temp), (162, "sdb", 67.5))
            s.remove("hwmon2")
            c.step()
            self.assertEqual((c.duty, c.drive_name), (69, "sda"))     # one cycle later
            s.remove("hwmon1")
            c.step()
            self.assertEqual((c.duty, c.drive_name, c.drive_on), (0, None, False))

    def test_one_of_two_fails_other_still_counts(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 68, block="sda")
            s.add("hwmon2", "drivetemp", 58, block="sdb")
            c, raw, log, st, seq = make_drive(s)
            c.step()
            self.assertEqual(c.drive_name, "sda")
            tf.put(os.path.join(td, "class/hwmon/hwmon1/temp1_input"), "x\n")
            c.step()
            self.assertEqual(c.drive_name, "sdb")
            self.assertEqual(c.duty, 69)    # sdb is 58 C and demand was on at 68: hysteresis holds the minimum

    def test_broken_source_is_not_a_failsafe(self):
        def bad():
            raise RuntimeError("boom")
        c, raw, log, st, seq = make_drive(drive_fn=bad)
        c.step()
        self.assertEqual(c.duty, 0)
        self.assertTrue(log.has("warning", "drive temperature source failed"))
        self.assertNotIn(255, raw.duty_writes())

    def test_cpu_sensor_failure_still_writes_full_with_hot_drive(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            c, raw, log, st, seq = make_drive(s)
            c.step()
            self.assertEqual(c.duty, 69)
            cpu(c, st, seq, None)
            c.step()
            self.assertEqual(c.duty, 255)
            self.assertTrue(log.has("err", "CPU temperature read failed"))
            self.assertTrue(c.sensor_error)
        # and with a cool drive, as in v0.4
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 40, block="sda")
            c, raw, log, st, seq = make_drive(s, temps=[None])
            c.step()
            self.assertEqual(c.duty, 255)


class TestMinimumRun(unittest.TestCase):
    """VBSP-REQ-14 amendment (0.5.1): a drive-demand start holds drive_min_duty for drive_min_run_s."""
    def setup(self, td, run_s=None, temp=61.0, cpu_t=40.0, alert=None):
        s = FakeSys(td)
        s.add("hwmon1", "drivetemp", temp, block="sda")
        cfg = dict(fan.DEFAULTS)
        if run_s is not None:
            cfg["drive_min_run_s"] = run_s
        c, raw, log, st, seq = make_drive(s, temps=[cpu_t], cfg=cfg, alert_file=alert)
        return s, c, raw, log, st, seq

    def run_for(self, c, raw, s_total, st=None, step=2):
        out = []
        for _ in range(int(s_total / step)):
            c.step()
            out.append((raw.clock(), c.duty))
            raw.clock.sleep(step)
        return out

    def test_default_is_600(self):
        self.assertEqual(fan.DEFAULTS["drive_min_run_s"], 600)

    def test_hold_until_600_then_off(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            t0 = raw.clock()
            c.step()
            self.assertEqual(c.duty, 69)
            raw.clock.sleep(30)
            s.set("hwmon1", 54.0)
            while raw.clock() - t0 < 600:
                cpu(c, st, seq, 40.0)
                c.step()
                self.assertEqual(c.duty, 69, raw.clock() - t0)
                raw.clock.sleep(2)
            self.assertEqual(raw.clock() - t0, 600)
            cpu(c, st, seq, 40.0)
            c.step()                            # exactly 600 s after the start: released
            self.assertEqual(c.duty, 0)
            self.assertFalse(c.drive_on)

    def test_zero_disables(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td, run_s=0)
            c.step()
            self.assertEqual(c.duty, 69)
            raw.clock.sleep(30)
            s.set("hwmon1", 54.0)
            cpu(c, st, seq, 40.0)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_custom_value_and_each_start_gets_its_own_hold(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td, run_s=60)
            c.step()
            s.set("hwmon1", 54.0)
            raw.clock.sleep(30)
            c.step()
            self.assertEqual(c.duty, 69)
            raw.clock.sleep(40)
            c.step()
            self.assertEqual(c.duty, 0)
            raw.clock.sleep(1000)
            s.set("hwmon1", 61.0)       # a second start: a new 60 s hold
            c.step()
            s.set("hwmon1", 54.0)
            raw.clock.sleep(30)
            c.step()
            self.assertEqual(c.duty, 69)
            raw.clock.sleep(40)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_hold_does_not_lower_higher_demand_and_ramps_normally(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td, temp=72.0)
            c.step()
            self.assertEqual(c.duty, 255)
            s.set("hwmon1", 67.5)
            raw.clock.sleep(10)
            c.step()
            self.assertEqual(c.duty, 162)

    def test_cpu_demand_during_hold_wins_and_outlasts(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            c.step()
            s.set("hwmon1", 54.0)
            cpu(c, st, seq, 85.0)
            raw.clock.sleep(10)
            c.step()
            self.assertEqual(c.duty, 255)

    def test_cpu_failsafe_during_hold_gives_0xff(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            s, c, raw, log, st, seq = self.setup(td, alert=alert)
            c.step()
            self.assertEqual(c.duty, 69)
            s.set("hwmon1", 54.0)
            raw.clock.sleep(10)
            c.temp_fn = lambda: None            # CPU sensor read error
            c.step()
            self.assertEqual(c.duty, 255)
            self.assertEqual(raw.duty_writes()[-1], 255)

    def test_stall_latch_unaffected(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            s, c, raw, log, st, seq = self.setup(td, alert=alert)
            c.step()
            raw.set_tach_stopped(True)
            for _ in range(40):
                c.step()
                raw.clock.sleep(2)
            self.assertTrue(c.stall_latched)
            self.assertEqual(c.duty, 255)
            s.set("hwmon1", 40)
            raw.clock.sleep(700)
            c.step()
            self.assertEqual(c.duty, 255)

    def test_vanished_drive_clears_hold(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            c.step()
            self.assertEqual(c.duty, 69)
            s.remove("hwmon1")
            raw.clock.sleep(10)
            c.step()
            self.assertEqual(c.duty, 0)
            self.assertFalse(log.has("crit", ""))

    def test_hold_logged_and_published(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "state.json")
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 61.0, block="sda")
            c, raw, log, st, seq = make_drive(s, cfg=dict(fan.DEFAULTS), state_file=sf)
            c.cfg["drive_min_run_s"] = 600
            c.step()
            d = json.loads(read(sf))
            self.assertTrue(d["drive_hold_active"])
            self.assertAlmostEqual(d["drive_hold_remaining_s"], 600, delta=1)
            self.assertGreater(d["drive_hold_until"], time.time())
            self.assertTrue(log.has("info", "minimum run holds duty 69 for 600 s"))
            s.set("hwmon1", 54.0)
            raw.clock.sleep(30)
            c.step()
            self.assertTrue(log.has("info", "54.0 C: minimum run holds duty 69"))
            raw.clock.sleep(570)
            c.step()
            d = json.loads(read(sf))
            self.assertFalse(d["drive_hold_active"])
            self.assertIsNone(d["drive_hold_until"])
            self.assertTrue(log.has("info", "minimum run ended"))

    def test_hold_runs_from_the_start_not_from_last_hot_reading(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            t0 = raw.clock()
            for _ in range(150):                # 300 s at 61 C
                c.step()
                raw.clock.sleep(2)
            s.set("hwmon1", 54.0)
            c.step()
            self.assertEqual(c.duty, 69)
            while raw.clock() - t0 < 600:
                raw.clock.sleep(2)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_arms_while_cpu_already_runs_the_fan(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td, temp=50.0, cpu_t=70.0)
            c.step()
            self.assertGreater(c.duty, 69)
            s.set("hwmon1", 61.0)
            c.step()
            s.set("hwmon1", 54.0)
            cpu(c, st, seq, 45.0)
            raw.clock.sleep(30)
            c.step()
            self.assertEqual(c.duty, 69)
            raw.clock.sleep(600)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_hold_ends_only_when_no_drive_reports(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            s.add("hwmon2", "drivetemp", 40.0, block="sdc")
            c.step()
            s.remove("hwmon1")                  # sda gone, sdc still reports 40 C
            raw.clock.sleep(10)
            c.step()
            self.assertEqual(c.duty, 69)
            s.remove("hwmon2")                  # none left
            raw.clock.sleep(10)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_drive_warming_into_band_during_hold_keeps_on_after_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td)
            c.step()
            s.set("hwmon1", 54.0)
            raw.clock.sleep(10)
            c.step()
            s.set("hwmon1", 57.0)
            raw.clock.sleep(10)
            c.step()
            raw.clock.sleep(1000)
            c.step()
            self.assertEqual(c.duty, 69)        # hysteresis: still above 55 C
            s.set("hwmon1", 55.0)
            c.step()
            self.assertEqual(c.duty, 0)

    def test_cpu_demand_continues_after_hold_ends(self):
        with tempfile.TemporaryDirectory() as td:
            s, c, raw, log, st, seq = self.setup(td, cpu_t=70.0)
            c.step()
            s.set("hwmon1", 54.0)
            raw.clock.sleep(700)
            c.step()
            self.assertEqual(c.duty, fan.target_duty(70.0, fan.DEFAULTS))

    def test_validation(self):
        for bad in (-1, -600, -0.5, 3601, float("nan"), float("inf")):
            cfg = dict(fan.DEFAULTS)
            cfg["drive_min_run_s"] = bad
            with self.assertRaises(fan.ConfigError):
                fan.Controller(fan.GuardedBus(tf.FakeI2C(), tf.Logs()), lambda: 40.0, cfg, tf.Logs())
        for good in (0, 1, 3600):
            cfg = dict(fan.DEFAULTS)
            cfg["drive_min_run_s"] = good
            fan.Controller(fan.GuardedBus(tf.FakeI2C(), tf.Logs()), lambda: 40.0, cfg, tf.Logs())

    def test_load_config_refuses_bad_values(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "fan.conf")
            for bad in ("-1", "-0.5", "3601", "abc", "nan"):
                tf.put(p, "[fan]\ndrive_min_run_s = %s\n" % bad)
                with self.assertRaises(fan.ConfigError, msg=bad):
                    fan.load_config(p)

    def test_config_error_in_run_gives_exit_3_alert_and_full_duty(self):
        self.assertEqual(fan.EXIT_CONFIG, 3)
        unit = read(os.path.join(SRC, "systemd/system/velo5x0-fand.service"))
        self.assertIn("RestartPreventExitStatus=2 3", unit)
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            conf = os.path.join(td, "fan.conf")
            tf.put(conf, "[fan]\ndrive_min_run_s = -5\n")
            log = tf.Logs()
            calls = []
            saved = (fan.open_bus, fan.identify, fan.load_config, fan.failsafe_full, fan.ALERT_FILE)
            orig_load = fan.load_config
            try:
                fan.open_bus = lambda lg: object()
                fan.identify = lambda *a, **k: True
                fan.load_config = lambda: orig_load(conf)
                fan.failsafe_full = lambda bus, lg, why, **k: calls.append(why) or True
                fan.ALERT_FILE = alert
                rc = fan.cmd_run(None, log)
            finally:
                fan.open_bus, fan.identify, fan.load_config, fan.failsafe_full, fan.ALERT_FILE = saved
            self.assertEqual(rc, 3)
            self.assertEqual(calls, ["configuration error"])      # full duty is set, exactly once
            self.assertTrue(log.has("crit", "configuration error"))
            self.assertIn("drive_min_run_s", read(alert))

    def test_load_config_reads_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "fan.conf")
            tf.put(p, "[fan]\ndrive_min_run_s = 120\n")
            self.assertEqual(fan.load_config(p)["drive_min_run_s"], 120)
            self.assertEqual(fan.load_config(os.path.join(td, "none"))["drive_min_run_s"], 600)


class TestStallInterplay(unittest.TestCase):
    def test_healthy_fan_at_minimum_drive_duty_never_latches_for_30_min(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            c, raw, log, st, seq = make_drive(s, alert_file=alert)
            for _ in range(900):                            # 30 min at the 2 s poll
                c.step()
                raw.clock.sleep(2)
            self.assertEqual(c.duty, 69)
            self.assertFalse(c.stall_latched)
            self.assertFalse(os.path.exists(alert))
            self.assertFalse(log.has("warning", "stalled"))
            self.assertEqual(raw.duty_writes().count(69), 1)    # written once, never re-kicked

    def test_stalled_fan_under_drive_demand_latches_full(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            c, raw, log, st, seq = make_drive(s, alert_file=alert)
            c.step()
            raw.set_tach_stopped(True)
            for _ in range(40):
                c.step()
                raw.clock.sleep(2)
            self.assertTrue(c.stall_latched)
            self.assertEqual(c.duty, 255)
            # latched: a cool drive and a cool CPU do not release it
            s.set("hwmon1", 40)
            for _ in range(3):
                c.step()
            self.assertEqual(c.duty, 255)

    def test_set_duty_guard_accepts_drive_minimum(self):
        c, raw, log, st, seq = make_drive(drive_fn=lambda: ("drivetemp", {"sda": 60.0}))
        c.step()
        self.assertEqual(c.duty, 69)


class TestStatePublished(unittest.TestCase):
    def state(self, c):
        with open(c.state_file) as f:
            return json.load(f)

    def test_fields_in_every_case(self):
        with tempfile.TemporaryDirectory() as td:
            sd = os.path.join(td, "sys")
            os.makedirs(sd)
            s = FakeSys(sd)
            sf = os.path.join(td, "run", "state.json")
            c, raw, log, st, seq = make_drive(s, state_file=sf)
            keys = {"drive_temp_c", "drive_name", "drive_demand_active", "drive_demand_on", "drive_duty", "drives",
                    "drive_source"}
            c.step()                                             # no drive
            j = self.state(c)
            self.assertTrue(keys <= set(j))
            self.assertEqual((j["drive_temp_c"], j["drive_name"], j["drive_demand_active"], j["drives"]),
                             (None, None, False, {}))
            s.add("hwmon1", "drivetemp", 62.5, block="sda")
            c.step()
            j = self.state(c)
            self.assertEqual((j["drive_temp_c"], j["drive_name"], j["drive_demand_active"], j["drive_duty"],
                              j["drive_source"]), (62.5, "sda", True, 69, "drivetemp"))
            self.assertEqual(j["drives"], {"sda": {"reporting": True, "temp_c": 62.5}})
            tf.put(os.path.join(sd, "class/hwmon/hwmon1/temp1_input"), "bad\n")
            c.step()
            j = self.state(c)
            self.assertEqual((j["drive_temp_c"], j["drive_demand_active"]), (None, False))
            self.assertEqual(j["drives"], {"sda": {"reporting": False, "temp_c": None}})   # reported before, not now
            s.set("hwmon1", 51)
            c.step()
            j = self.state(c)
            self.assertEqual(j["drives"]["sda"], {"reporting": True, "temp_c": 51.0})
            self.assertFalse(j["drive_demand_active"])
            self.assertTrue(keys <= set(j))

    def test_cpu_fields_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "state.json")
            c, raw, log, st, seq = make_drive(drive_fn=None, state_file=sf)
            c.step()
            j = self.state(c)
            for k in ("time", "temp_c", "max_temp_c", "state", "duty", "rpm_approx", "tach_stopped",
                      "sensor_error", "stall_latched", "full_failed"):
                self.assertIn(k, j)


def load_v04():
    """The frozen v0.4.0 daemon (tests/fixtures), the reference for "no drive == v0.4"."""
    path = os.path.join(HERE, "fixtures", "velo5x0-fan-v0.4")
    loader = importlib.machinery.SourceFileLoader("velo5x0_fan_v04", path)
    spec = importlib.util.spec_from_loader("velo5x0_fan_v04", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.I2CError = fan.I2CError          # the shared fake bus raises the new module's class
    return mod


class TestIdenticalToV04(unittest.TestCase):
    """The new controller with no drive input against the frozen 0.4 controller."""

    def run_one(self, mod, seed, drive_fn):
        rnd = random.Random(seed)
        raw = tf.FakeI2C()
        log = tf.Logs()
        temps = []
        t = 45.0
        for _ in range(600):
            t = max(20.0, min(95.0, t + rnd.uniform(-4, 4)))
            temps.append(None if rnd.random() < 0.01 else t)
        stalls = [rnd.random() < 0.04 for _ in temps]
        seq = iter(temps)
        kw = {} if drive_fn is False else {"drive_fn": drive_fn}
        c = mod.Controller(mod.GuardedBus(raw, log), lambda: next(seq), dict(mod.DEFAULTS), log,
                           sleep=raw.clock.sleep, clock=raw.clock, **kw)
        c.identify()
        c.configure()
        duties = []
        for i in range(len(temps)):
            raw.regs[fan.EMC_ADDR][fan.EMC_TACH_HI] = 0xFF if stalls[i] else 0x0F
            raw.regs[fan.EMC_ADDR][fan.EMC_TACH_LO] = 0xF8 if stalls[i] else 0x00
            c.step()
            raw.clock.sleep(2)
            duties.append((c.duty, c.state))
        return duties, raw.writes, log

    def test_random_walks_with_sensor_errors_and_stalls(self):
        v04 = load_v04()
        for seed in range(12):
            old = self.run_one(v04, seed, False)
            for drive_fn in (None, lambda: (None, {})):
                new = self.run_one(fan, seed, drive_fn)
                self.assertEqual(new[0], old[0], seed)
                self.assertEqual(new[1], old[1], seed)          # identical bus write sequence
                extra = [m for m in new[2] if m not in old[2]]
                self.assertLessEqual(len(extra), 1, extra)
                for lv, m in extra:
                    self.assertEqual(m, "no drive temperature input")


class TestDriveReaderThread(unittest.TestCase):
    def test_hung_read_does_not_block_step_and_hot_drive_keeps_demand(self):
        release = threading.Event()
        present = {"sda"}

        class Hung:
            n = 0

            def poll(self):
                Hung.n += 1
                if Hung.n == 1:
                    return "drivetemp", {"sda": 67.0}
                release.wait(30)            # a drive that never answers
                return "drivetemp", {"sda": 30.0}

            def present(self):
                return set(present)
        clk = tf.FakeClock()
        log0 = tf.Logs()
        r = fan.DriveReader(Hung(), interval_s=0.01, stale_s=30.0, clock=clk, log=log0)
        r.start()
        try:
            for _ in range(200):
                if Hung.n >= 2:
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(Hung.n, 2)              # the thread is now stuck in the second read
            c, raw, log, st, seq = make_drive(drive_fn=r.poll)
            t0 = time.monotonic()
            c.step()
            self.assertLess(time.monotonic() - t0, 1.0)     # the loop never waited on the drive
            self.assertEqual((c.drive_name, c.duty), ("sda", 143))
            clk.t += 31                                      # snapshot stale: the read hung, the drive is still there
            for _ in range(3):
                c.step()
            self.assertEqual((c.drive_name, c.duty), ("sda", 143))   # last demand held
            self.assertTrue(log0.has("warning", "holding the last drive demand"))
            self.assertEqual(sum(1 for l, m in log0 if "holding" in m), 1)
            self.assertNotIn(255, raw.duty_writes())
            present.clear()                                  # the device disappears: now it is dropped
            c.step()
            self.assertEqual(c.duty, 0)
            self.assertTrue(log.has("warning", "drive sda: gone"))
            self.assertNotIn(255, raw.duty_writes())
        finally:
            release.set()
            r.stop()

    def test_no_snapshot_yet_and_failing_source(self):
        class Boom:
            def poll(self):
                raise OSError("x")
        r = fan.DriveReader(Boom(), clock=tf.FakeClock())
        self.assertEqual(r.poll(), (None, {}))
        r.refresh()
        self.assertEqual(r.poll(), (None, {}))

    def test_start_publishes_first_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            r = fan.DriveReader(fan.DriveSource(sysfs=td), interval_s=0.01)
            r.start()
            try:
                self.assertEqual(r.poll(), ("drivetemp", {"sda": 62.0}))
            finally:
                r.stop()


class TestSmartctlRunner(unittest.TestCase):
    def test_timeout_kills_and_never_waits_unbounded(self):
        killed = []

        class P:
            def __init__(self, *a, **k):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                raise subprocess.TimeoutExpired("smartctl", timeout)

            def kill(self):
                killed.append(1)
        orig = fan.subprocess.Popen
        fan.subprocess.Popen = P
        try:
            t0 = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                fan._run_smartctl(["-A"], 0.1)
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            fan.subprocess.Popen = orig
        self.assertEqual(killed, [1])


class TestDriveEdgeCases(unittest.TestCase):
    def test_drive_fields_published_during_cpu_sensor_error(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            sf = os.path.join(td, "state.json")
            c, raw, log, st, seq = make_drive(s, state_file=sf)
            cpu(c, st, seq, None)
            c.step()
            j = json.loads(read(sf))
            self.assertTrue(j["sensor_error"])
            self.assertEqual(j["drive_name"], "sda")
            self.assertEqual(j["duty"], 255)

    def test_fail_safe_written_before_any_drive_io(self):
        order = []

        def drive_fn():
            order.append(("drive", len(raw.duty_writes())))
            return "drivetemp", {"sda": 62.0}
        c, raw, log, st, seq = make_drive(drive_fn=drive_fn, temps=[60.0])
        c.step()
        self.assertEqual(c.duty, 69)
        del order[:]
        cpu(c, st, seq, None)
        base = len(raw.duty_writes())
        c.step()
        self.assertEqual(c.duty, 255)
        self.assertEqual(order[0][1], base + 1)         # the 0xff write is already done

    def test_fan_off_log_line_tells_the_truth(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", 62, block="sda")
            c, raw, log, st, seq = make_drive(s, temps=[60.0])
            c.step()
            cpu(c, st, seq, 40.0)
            c.step()
            self.assertEqual(c.duty, 69)
            self.assertTrue(log.has("info", "CPU demand off; drive demand holds duty 69"))
            self.assertFalse(log.has("info", "fan off"))

    def test_smartctl_drive_in_standby_is_not_a_warning(self):
        state = {"on": True}

        def fn():
            return ("smartctl", {"sda": 40.0}) if state["on"] else ("smartctl", {})
        c, raw, log, st, seq = make_drive(drive_fn=fn)
        c.step()
        state["on"] = False
        c.step()
        self.assertFalse(any(l == "warning" for l, m in log))
        self.assertTrue(log.has("info", "drive sda: gone"))

    def test_never_reported_drive_is_info_not_warning(self):
        with tempfile.TemporaryDirectory() as td:
            s = FakeSys(td)
            s.add("hwmon1", "drivetemp", raw="garbage\n", block="sda")
            c, raw, log, st, seq = make_drive(s)
            for _ in range(3):
                c.step()
            self.assertFalse(any(l == "warning" for l, m in log))
            self.assertTrue(log.has("info", "never reported"))


class TestPackageBuild(unittest.TestCase):
    """The build parts of VBSP-TEST-17: installed by the package, enabled by default, no manual step."""

    def test_modules_load_entry_and_install(self):
        conf = read(os.path.join(SRC, "modules-load.d/velo5x0-drivetemp.conf"))
        lines = [l.strip() for l in conf.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, ["drivetemp"])
        b = read(os.path.join(SRC, "packaging/build-debs.sh"))
        self.assertIn('inst 0644 "$P/usr/share/velo5x0-bsp/drivetemp-modules-load.conf" "$SRC/modules-load.d/velo5x0-drivetemp.conf"', b)
        post = b.split('cat >"$P/DEBIAN/postinst" <<\'EOF\'', 1)[1].split("\nEOF", 1)[0]
        self.assertIn("modprobe drivetemp", post)
        self.assertIn("|| echo", post)                 # a kernel without drivetemp must not fail the install
        self.assertIn("if modinfo drivetemp", post)    # ...nor leave systemd-modules-load failed at boot
        self.assertIn("systemctl enable velo5x0-fan-failsafe.service velo5x0-fand.service", post)
        self.assertNotIn("conffiles\"\n/etc/modules-load.d", b)
        self.assertNotIn("/etc/velo5x0-bsp/fan.conf /etc/modules-load.d/velo5x0-drivetemp.conf", b)
        self.assertIn("Suggests: smartmontools", b)
        self.assertNotIn("Depends: velo5x0-bsp-dkms (= $DEBVER), python3, systemd (>= 254), initramfs-tools, kmod, grub2-common, smartmontools", b)
        self.assertIn("CHANGELOG.md", b)

    def test_no_per_machine_configuration_needed(self):
        shipped = fan.load_config(os.path.join(SRC, "fan/fan.conf"))
        self.assertEqual({k: shipped[k] for k in fan.DEFAULTS}, fan.DEFAULTS)    # file == built-in defaults
        conf = read(os.path.join(SRC, "fan/fan.conf"))
        self.assertNotIn("drives =", conf)                 # no list of drives
        svc = read(os.path.join(SRC, "systemd/system/velo5x0-fand.service"))
        self.assertIn("ExecStart=/usr/sbin/velo5x0-fan run", svc)   # one service, no second unit to enable

    def test_run_wires_drive_source_and_version_bumped(self):
        t = read(os.path.join(SRC, "fan/velo5x0-fan"))
        self.assertIn("DriveReader(DriveSource(), log=log)", t)
        self.assertIn("drive_fn=reader.poll", t)
        self.assertEqual(read(os.path.join(SRC, "VERSION")).strip(), "0.5.1")
        self.assertIn("## 0.5.1", read(os.path.join(SRC, "CHANGELOG.md")))

    def test_shell_script_syntax(self):
        r = subprocess.run(["sh", "-n", os.path.join(SRC, "packaging/build-debs.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
