# SPDX-License-Identifier: GPL-2.0
"""Off-box tests of velo5x0-fan against a fake I2C backend (VBSP-REQ-10, VBSP-REQ-7B)."""
import importlib.machinery
import importlib.util
import os
import sys
import random
import tempfile
import unittest

sys.dont_write_bytecode = True   # keep .pyc out of the source tree

HERE = os.path.dirname(os.path.abspath(__file__))
FAN = os.path.join(HERE, "..", "fan", "velo5x0-fan")


def load_fan():
    loader = importlib.machinery.SourceFileLoader("velo5x0_fan", FAN)
    spec = importlib.util.spec_from_loader("velo5x0_fan", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fan = load_fan()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


class FakeI2C:
    """Registers per address; records every access; can fail on demand.
    Emulates the EMC2104 Spin Up Routine (datasheet 5.7, 6.20): a 0x40 write
    that changes the setting from 00h reads back 0xff for a quarter of the
    spin-up time, then 0x99 (60 %), then the written value."""

    def __init__(self, product_id=0x1D, pca1c_out=0xFF, pca1c_cfg=0xFF, pca1c_pol=0x00,
                 chip_present=True, clock=None, spin_s=0.5):
        self.clock = clock or FakeClock()
        self.spin_s = spin_s
        self.spin_until = None
        self.ignore_duty_writes = False
        self.regs = {
            fan.EMC_ADDR: {fan.EMC_PRODUCT_ID_REG: product_id, fan.EMC_FAN_SETTING: 0xFF,
                           fan.EMC_SPINUP_CFG: 0x19,
                           fan.EMC_TACH_HI: 0x0F, fan.EMC_TACH_LO: 0x00},
            fan.PCA_FAN_ADDR: {fan.PCA_IN: 0x1F, fan.PCA_OUT: pca1c_out,
                               fan.PCA_POL: pca1c_pol, fan.PCA_CFG: pca1c_cfg},
        }
        self.chip_present = chip_present
        self.reads, self.writes = [], []
        self.fail_writes = False
        self.fail_reads = False

    def read(self, addr, reg):
        self.reads.append((addr, reg))
        if self.fail_reads or (addr == fan.EMC_ADDR and not self.chip_present):
            raise fan.I2CError(6, "nak")
        if addr == fan.EMC_ADDR and reg == fan.EMC_FAN_SETTING and self.spin_until:
            t = self.clock()
            if t < self.spin_until - 0.75 * self.spin_s:
                return 0xFF
            if t < self.spin_until:
                return 0x99
        return self.regs.get(addr, {}).get(reg, 0)

    def write(self, addr, reg, val):
        self.writes.append((addr, reg, val))
        if self.fail_writes:
            raise fan.I2CError(5, "io error")
        if addr == fan.EMC_ADDR and reg == fan.EMC_FAN_SETTING:
            if self.ignore_duty_writes:
                return
            if self.regs[addr].get(reg, 0) == 0 and val != 0:
                self.spin_until = self.clock() + self.spin_s
            elif val == 0:
                self.spin_until = None
        self.regs.setdefault(addr, {})[reg] = val

    def duty_writes(self):
        return [v for a, r, v in self.writes if a == fan.EMC_ADDR and r == fan.EMC_FAN_SETTING]

    def set_tach_stopped(self, stopped):
        self.regs[fan.EMC_ADDR][fan.EMC_TACH_HI] = 0xFF if stopped else 0x0F
        self.regs[fan.EMC_ADDR][fan.EMC_TACH_LO] = 0xF8 if stopped else 0x00


class Logs(list):
    def __call__(self, level, msg):
        self.append((level, msg))

    def has(self, level, text):
        return any(l == level and text in m for l, m in self)


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def make(raw=None, temps=None, cfg=None, alert_file=None):
    raw = raw or FakeI2C()
    log = Logs()
    seq = list(temps or [40.0])
    state = {"i": 0}

    def temp_fn():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v
    c = fan.Controller(fan.GuardedBus(raw, log), temp_fn, dict(cfg or fan.DEFAULTS), log,
                       sleep=raw.clock.sleep, clock=raw.clock, alert_file=alert_file)
    return c, raw, log


class TestPrecondition(unittest.TestCase):
    def test_wrong_product_id_writes_nothing(self):
        c, raw, log = make(FakeI2C(product_id=0x24))
        self.assertFalse(c.identify())
        self.assertFalse(c.full("test"))
        self.assertEqual(raw.writes, [])
        self.assertTrue(log.has("err", "product ID 0x24"))

    def test_absent_chip_writes_nothing(self):
        c, raw, log = make(FakeI2C(chip_present=False))
        self.assertFalse(c.identify())
        self.assertFalse(c.full("test"))
        self.assertEqual(raw.writes, [])
        self.assertTrue(log.has("err", "does not answer"))

    def test_identified_full_writes_ff(self):
        c, raw, _ = make()
        self.assertTrue(c.identify())
        self.assertTrue(c.full("test"))
        # 0xff once, then the 0x1c fan pins (bit 6 low; bit 7 already high here)
        self.assertEqual(raw.writes[0], (fan.EMC_ADDR, fan.EMC_FAN_SETTING, 0xFF))
        self.assertEqual(raw.duty_writes(), [0xFF])
        self.assertEqual(raw.regs[fan.PCA_FAN_ADDR][fan.PCA_OUT] & 0xC0, 0x80)
        self.assertEqual(raw.regs[fan.PCA_FAN_ADDR][fan.PCA_CFG] & 0xC0, 0x00)


class TestFailsafeFull(unittest.TestCase):
    """Findings 1, 4, 5: the fail-safe verifies by read-back after spin-up,
    retries, alerts, sets the 0x1c pins, and reads no configuration."""

    def run_full(self, raw, alert_file=None):
        log = Logs()
        ok = fan.failsafe_full(fan.GuardedBus(raw, log), log, "test", alert_file,
                               raw.clock.sleep, raw.clock)
        return ok, log

    def test_spinup_transient_is_not_a_failure(self):
        raw = FakeI2C()
        raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING] = 0x00      # fan stopped
        t0 = raw.clock()
        ok, log = self.run_full(raw)
        self.assertTrue(ok)
        self.assertEqual(raw.duty_writes(), [0xFF])              # one write, no retry
        self.assertGreaterEqual(raw.clock() - t0, 0.5)           # waited out the spin-up
        self.assertTrue(log.has("warning", "verified"))
        self.assertFalse(any(l == "crit" for l, _ in log))

    def test_write_not_taking_retries_then_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            raw = FakeI2C()
            raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING] = 0x00
            raw.ignore_duty_writes = True
            ok, log = self.run_full(raw, alert)
            self.assertFalse(ok)
            self.assertEqual(len(raw.duty_writes()), fan.FULL_WRITE_TRIES)
            self.assertTrue(log.has("crit", "NOT verified"))
            self.assertTrue(os.path.exists(alert))

    def test_sets_fan_pins_preserving_poe_bits(self):
        for out, cfg in ((0x00, 0xBF), (0x03, 0xFF), (0x41, 0xC2)):
            raw = FakeI2C(pca1c_out=out, pca1c_cfg=cfg)
            ok, _ = self.run_full(raw)
            self.assertTrue(ok)
            r = raw.regs[fan.PCA_FAN_ADDR]
            self.assertEqual((r[fan.PCA_OUT] & 0xC0, r[fan.PCA_CFG] & 0xC0), (0x80, 0x00))
            self.assertEqual((r[fan.PCA_OUT] & 0x03, r[fan.PCA_CFG] & 0x03), (out & 0x03, cfg & 0x03))
            self.assertFalse(any(a == fan.PCA_FAN_ADDR and reg == fan.PCA_POL for a, reg, v in raw.writes))

    def test_full_subcommand_reads_no_configuration(self):
        import ast
        with open(FAN) as f:
            tree = ast.parse(f.read())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("cmd_full", "failsafe_full", "identify", "spinup_time", "wait_for_duty"):
            called = {getattr(n.func, "id", getattr(n.func, "attr", None))
                      for n in ast.walk(fns[name]) if isinstance(n, ast.Call)}
            self.assertNotIn("load_config", called, name)
            self.assertNotIn("Controller", called, name)

    def test_broken_config_does_not_stop_full(self):
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "fan.conf")
            put(bad, "[fan]\noff_c = not-a-number\n")
            with self.assertRaises(ValueError):
                fan.load_config(bad)
            raw = FakeI2C()
            ok, _ = self.run_full(raw)          # the fail-safe never parses it
            self.assertTrue(ok)


class TestNotify(unittest.TestCase):
    def test_sd_notify_datagram(self):
        import socket
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "notify")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.bind(path)
            try:
                self.assertTrue(fan.sd_notify("WATCHDOG=1", {"NOTIFY_SOCKET": path}))
                self.assertEqual(s.recv(64), b"WATCHDOG=1")
            finally:
                s.close()
        self.assertFalse(fan.sd_notify("READY=1", {}))


class TestHardwareRules(unittest.TestCase):
    def test_pca1c_rmw_preserves_poe_bits_for_every_start_value(self):
        for out in range(256):
            for cfg in (0x00, 0x03, 0xFC, 0xFF, random.randrange(256)):
                c, raw, _ = make(FakeI2C(pca1c_out=out, pca1c_cfg=cfg))
                c.identify()
                before, after = c.configure()
                r = raw.regs[fan.PCA_FAN_ADDR]
                self.assertEqual(r[fan.PCA_OUT] & 0x03, out & 0x03)
                self.assertEqual(r[fan.PCA_CFG] & 0x03, cfg & 0x03)
                self.assertEqual(r[fan.PCA_OUT] & 0xC0, 0x80)    # pin 6 low, pin 7 high
                self.assertEqual(r[fan.PCA_CFG] & 0xC0, 0x00)    # pins 6, 7 outputs
                self.assertEqual(r[fan.PCA_OUT] & 0x3C, out & 0x3C)
                self.assertEqual(r[fan.PCA_CFG] & 0x3C, cfg & 0x3C)
                pca_writes = [(reg, v) for a, reg, v in raw.writes if a == fan.PCA_FAN_ADDR]
                self.assertTrue(all(reg in (fan.PCA_OUT, fan.PCA_CFG) for reg, _ in pca_writes))

    def test_polarity_never_written_and_0x18_never_addressed(self):
        c, raw, _ = make(temps=[40, 70, 90, 45, None, 40])
        c.identify()
        c.configure()
        for _ in range(6):
            c.step()
        c.full("end")
        self.assertFalse(any(a == fan.PCA_FAN_ADDR and r == fan.PCA_POL for a, r, v in raw.writes))
        self.assertFalse(any(a == fan.PCA_RESETS_ADDR for a, r, v in raw.writes))
        self.assertFalse(any(a == fan.PCA_RESETS_ADDR for a, r in raw.reads))

    def test_guard_refuses_forbidden_accesses(self):
        g = fan.GuardedBus(FakeI2C(), Logs())
        with self.assertRaises(PermissionError):
            g.read(0x18, 0)
        with self.assertRaises(PermissionError):
            g.pca1c_rmw(fan.PCA_POL, 0x80, 0)
        with self.assertRaises(PermissionError):
            g.pca1c_rmw(fan.PCA_OUT, 0x01, 0)
        with self.assertRaises(PermissionError):
            g.pca1c_rmw(fan.PCA_CFG, 0, 0x02)

    def test_recipe_order_and_values(self):
        c, raw, _ = make()
        c.identify()
        c.configure()
        emc = [(r, v) for a, r, v in raw.writes if a == fan.EMC_ADDR]
        self.assertEqual(emc, [(0x50, 0x00), (0x42, 0x10), (0x2B, 0x0C), (0x41, 0x01)])

    def test_configure_logs_start_duty(self):
        c, raw, log = make()
        raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING] = 0xFF
        c.identify()
        c.configure()
        self.assertTrue(log.has("info", "duty read at start: 0xff"))


class TestPolicy(unittest.TestCase):
    def test_idle_is_off(self):
        c, raw, _ = make(temps=[35.0, 40.0, 50.0])
        c.identify()
        c.configure()
        for _ in range(3):
            c.step()
        self.assertEqual(c.state, c.OFF)
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0)

    def test_hysteresis(self):
        temps = [45, 55, 59.9, 60, 58, 52, 50.1, 50, 55, 59]
        c, raw, _ = make(temps=temps)
        c.identify()
        c.configure()
        states = []
        for _ in temps:
            c.step()
            states.append((c.state, c.duty))
        sf = fan.DEFAULTS["stall_free_duty"]
        self.assertEqual([s for s, _ in states],
                         ["off", "off", "off", "run", "run", "run", "run", "off", "off", "off"])
        for s, d in states:
            if s == "run":
                self.assertGreaterEqual(d, sf)
            else:
                self.assertEqual(d, 0)

    def test_start_kicks_then_runs(self):
        c, raw, _ = make(temps=[40, 65])
        c.identify()
        c.configure()
        c.step()
        c.step()
        d = raw.duty_writes()
        self.assertEqual(d[-2], fan.DEFAULTS["kick_duty"])
        self.assertGreaterEqual(d[-1], fan.DEFAULTS["stall_free_duty"])

    def test_full_above_full_threshold(self):
        c, raw, _ = make(temps=[85])
        c.identify()
        c.configure()
        c.step()
        self.assertEqual(c.duty, 0xFF)

    def test_never_between_off_and_stall_free(self):
        rnd = random.Random(1)
        t = 45.0
        temps = []
        for _ in range(5000):
            t = max(20.0, min(100.0, t + rnd.uniform(-3, 3)))
            temps.append(t)
        c, raw, _ = make(temps=temps)
        c.identify()
        c.configure()
        for _ in temps:
            c.step()
        sf = fan.DEFAULTS["stall_free_duty"]
        for d in raw.duty_writes():
            self.assertTrue(d == 0 or d >= sf, d)

    def test_target_duty_monotonic(self):
        cfg = fan.DEFAULTS
        last = 0
        for t10 in range(500, 1000):
            d = fan.target_duty(t10 / 10.0, cfg)
            self.assertGreaterEqual(d, last)
            last = d
        self.assertEqual(fan.target_duty(80.0, cfg), 0xFF)

    def test_bad_config_rejected(self):
        cfg = dict(fan.DEFAULTS, off_c=60, on_c=55)
        with self.assertRaises(ValueError):
            make(cfg=cfg)
        cfg = dict(fan.DEFAULTS, stall_free_duty=0)
        with self.assertRaises(ValueError):
            make(cfg=cfg)

    def test_stall_kick(self):
        c, raw, log = make(temps=[65] * 10)
        c.identify()
        c.configure()
        c.step()
        raw.set_tach_stopped(True)
        for _ in range(3):
            c.step()
        self.assertTrue(log.has("warning", "stalled"))


class TestReassert(unittest.TestCase):
    def test_foreign_duty_write_is_corrected_once_persistent(self):
        c, raw, log = make(temps=[40] * 6)
        c.identify()
        c.configure()
        c.step()
        self.assertEqual(c.duty, 0)
        raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING] = 0xFF    # e.g. the shutdown hook run by hand
        raw.clock.sleep(5)
        c.step()                                               # first sighting: wait
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0xFF)
        raw.clock.sleep(2)
        c.step()                                               # persisted > spin-up: rewrite
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0)
        self.assertTrue(log.has("warning", "rewriting"))

    def test_spinup_transient_is_not_rewritten(self):
        temps = [40, 65] + [65] * 4
        c, raw, log = make(temps=temps)
        c.identify()
        c.configure()
        c.step()                       # off: duty 0
        raw.clock.sleep(5)
        c.step()                       # on: kick then running duty (spin-up from 0)
        n = len(raw.duty_writes())
        for _ in range(4):
            c.step()
            raw.clock.sleep(2)
        self.assertEqual(len(raw.duty_writes()), n)
        self.assertFalse(log.has("warning", "rewriting"))


class TestFailSafe(unittest.TestCase):
    def test_sensor_error_goes_full_then_recovers(self):
        c, raw, log = make(temps=[40, None, None, 40])
        c.identify()
        c.configure()
        c.step()
        self.assertEqual(c.duty, 0)
        c.step()
        self.assertEqual(c.duty, 0xFF)
        self.assertTrue(log.has("warning", "sensor read error"))
        n = len(raw.duty_writes())
        c.step()
        self.assertEqual(len(raw.duty_writes()), n)       # no repeated writes while in error
        c.step()
        self.assertEqual(c.duty, 0)

    def test_write_failure_raises_alert(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            c, raw, log = make(alert_file=alert)
            c.identify()
            raw.fail_writes = True
            self.assertFalse(c.full("injected"))
            self.assertTrue(log.has("crit", "ALERT"))
            self.assertTrue(os.path.exists(alert))

    def test_permanent_stall_raises_alert(self):
        with tempfile.TemporaryDirectory() as td:
            alert = os.path.join(td, "ALERT")
            c, raw, log = make(temps=[65] * 40, alert_file=alert)
            c.identify()
            c.configure()
            c.step()
            raw.set_tach_stopped(True)
            for _ in range(30):
                c.step()
                raw.clock.sleep(2)
            self.assertTrue(log.has("crit", "fan stalled"))
            self.assertTrue(os.path.exists(alert))
            self.assertEqual(sum(1 for l, m in log if l == "crit" and "fan stalled" in m), 1)

    def test_run_loop_failure_attempts_full(self):
        c, raw, log = make(temps=[40, 70, 70])
        c.identify()
        c.configure()
        c.step()
        c.step()
        raw.fail_reads = True
        with self.assertRaises(OSError):
            c.step()     # tach read fails inside a running step
        raw.fail_reads = False
        self.assertTrue(c.full("control failure"))
        self.assertEqual(raw.duty_writes()[-1], 0xFF)


class TestStallLatch(unittest.TestCase):
    def test_stall_latch_holds_full_until_restart(self):
        # After the stall alert, full duty is held whatever the
        # temperature does, and the alert is raised once
        temps = [65] * 25 + [40] * 10 + [70] * 5
        c, raw, log = make(temps=temps)
        c.identify()
        c.configure()
        c.step()
        raw.set_tach_stopped(True)
        latched_at = None
        for i in range(len(temps) - 1):
            c.step()
            raw.clock.sleep(2)
            if c.stall_latched and latched_at is None:
                latched_at = len(raw.duty_writes())
        self.assertTrue(c.stall_latched)
        after = raw.duty_writes()[latched_at:]
        self.assertTrue(all(d == 0xFF for d in after), after)
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0xFF)
        self.assertEqual(c.state, c.RUN)
        self.assertEqual(sum(1 for l, m in log if l == "crit" and "fan stalled" in m), 1)

    def test_latch_survives_fan_restarting(self):
        c, raw, log = make(temps=[65] * 40)
        c.identify()
        c.configure()
        c.step()
        raw.set_tach_stopped(True)
        while not c.stall_latched:
            c.step()
            raw.clock.sleep(2)
        raw.set_tach_stopped(False)          # spins again at full
        for _ in range(5):
            c.step()
            raw.clock.sleep(2)
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0xFF)

    def test_failed_full_on_sensor_error_is_retried(self):
        # LOW: a failed 0xff write is retried on every poll, alert raised once
        c, raw, log = make(temps=[40, None, None, None, None])
        c.identify()
        c.configure()
        c.step()
        raw.fail_writes = True
        c.step()
        c.step()
        self.assertEqual(sum(1 for l, m in log if l == "crit" and "NOT verified" in m), 1)
        self.assertTrue(log.has("err", "retrying every poll"))
        raw.fail_writes = False
        c.step()
        self.assertEqual(c.duty, 0xFF)
        self.assertEqual(raw.regs[fan.EMC_ADDR][fan.EMC_FAN_SETTING], 0xFF)
        self.assertFalse(c.full_failed)

    def test_identify_retries_a_nack_but_not_a_wrong_id(self):
        raw = FakeI2C()
        fails = {"n": 2}
        orig = raw.read

        def flaky(addr, reg):
            if addr == fan.EMC_ADDR and reg == fan.EMC_PRODUCT_ID_REG and fails["n"]:
                fails["n"] -= 1
                raise fan.I2CError(6, "nak")
            return orig(addr, reg)
        raw.read = flaky
        log = Logs()
        self.assertTrue(fan.identify(fan.GuardedBus(raw, log), log, tries=5, sleep=raw.clock.sleep))
        self.assertTrue(log.has("warning", "retry 1/4"))
        raw2 = FakeI2C(product_id=0x24)
        log2 = Logs()
        self.assertFalse(fan.identify(fan.GuardedBus(raw2, log2), log2, tries=5, sleep=raw2.clock.sleep))
        self.assertEqual(sum(1 for a, r in raw2.reads if r == fan.EMC_PRODUCT_ID_REG), 1)
        raw3 = FakeI2C(chip_present=False)
        log3 = Logs()
        self.assertFalse(fan.identify(fan.GuardedBus(raw3, log3), log3, tries=3, sleep=raw3.clock.sleep))
        self.assertTrue(log3.has("err", "after 3 tries"))
        self.assertEqual(raw3.writes, [])


class TestSysfsDiscovery(unittest.TestCase):
    def test_find_adapter_by_module_param_and_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fan.find_adapter(td))
            d = os.path.join(td, "bus/i2c/devices/i2c-7")
            os.makedirs(d)
            put(os.path.join(d, "name"), fan.ADAPTER_NAME + "\n")
            self.assertEqual(fan.find_adapter(td), "/dev/i2c-7")
            p = os.path.join(td, "module/vc_edge5x0_board/parameters")
            os.makedirs(p)
            put(os.path.join(p, "i2c_adapter"), "i2c-9\n")
            self.assertEqual(fan.find_adapter(td), "/dev/i2c-9")

    def test_coretemp_max(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(fan.read_coretemp(td))
            h = os.path.join(td, "class/hwmon/hwmon3")
            os.makedirs(h)
            put(os.path.join(h, "name"), "coretemp\n")
            for i, v in enumerate((41000, 47000, 44000), start=2):
                put(os.path.join(h, "temp%d_input" % i), "%d\n" % v)
            o = os.path.join(td, "class/hwmon/hwmon0")
            os.makedirs(o)
            put(os.path.join(o, "name"), "acpitz\n")
            put(os.path.join(o, "temp1_input"), "99000\n")
            self.assertEqual(fan.read_coretemp(td), 47.0)


if __name__ == "__main__":
    unittest.main()
