# SPDX-License-Identifier: GPL-2.0
"""Static acceptance tests of the package source tree.

TEST-7  the three forbidden hardware writes cannot happen (static part)
TEST-8  provenance complete, nothing unlicensed copied
TEST-11 leak scan finds nothing
and:    code constants match the spec's register sequences; the kernel-update
        guard; the .link files; the fan fail-safe wiring of the units.

Optional environment:
  VELO5X0_UPSTREAM  path to a velo540openwrt clone at the pinned commit (enables the
                    copied-text check; skipped without it)
"""
import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True   # keep .pyc out of the source tree

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, ".."))
BOARD = os.path.join(SRC, "dkms/vc-edge5x0/vc-edge5x0-board.c")
SWITCH = os.path.join(SRC, "dkms/vc-edge5x0/vc-edge5x0-switch.c")
DSA = os.path.join(SRC, "dkms/vc-edge5x0-dsa/vc-edge5x0-dsa.c")
KBUILD = os.path.join(SRC, "kernel-build/velo5x0-kernel-build")
P230 = os.path.join(SRC, "kernel-build/patches/230-dsa-pdata-own-tree.patch")
P200 = os.path.join(SRC, "dkms/igb-vc/patches/200-igb-velocloud-edge5x0.patch")
P201 = os.path.join(SRC, "dkms/igb-vc/patches/201-velo5x0-bsp-ext-mdio-bus-name.patch")
FAN = os.path.join(SRC, "fan/velo5x0-fan")
GUARD = os.path.join(SRC, "kernel/velo5x0-kernel-guard")
REQS = os.path.join(SRC, "docs", "REQUIREMENTS.md")
PINNED = "d487e0734e7ad1da70d77ffefb681d0f4e90d317"


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def c_functions(text):
    """name -> body of each top-level C function (brace matching)."""
    out = {}
    for m in re.finditer(r"^(?:static\s+)?[\w\s\*]+?\b(\w+)\s*\([^;{)]*\)\s*\{", text, re.M):
        name, i, depth = m.group(1), m.end(), 1
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out[name] = text[m.end():i]
    return out


class Test7ForbiddenWrites(unittest.TestCase):
    def setUp(self):
        self.board = read(BOARD)
        self.fns = c_functions(self.board)

    def test_port_io_writes_only_in_vc_sc_update(self):
        for name, body in self.fns.items():
            if re.search(r"\bout[bwl]\s*\(", body):
                self.assertEqual(name, "vc_sc_update", "port I/O write in %s" % name)
        for p in (SWITCH,):
            self.assertNotRegex(read(p), r"\bout[bwl]\s*\(")

    def test_sus_bank_never_addressed(self):
        # every register constant passed to vc_sc_update is an SC-bank offset < 0x80
        regs = dict(re.findall(r"#define\s+(VC_SC_\w+)\s+(0x[0-9a-fA-F]+)", self.board))
        for k, v in regs.items():
            self.assertLess(int(v, 16), 0x80, k)
        self.assertNotRegex(self.board, r"vc_gbase\s*\+\s*(0x8|12[89]|1[3-9]\d|VC_SUS)")
        self.assertNotRegex(self.board, r"\bSUS\w*\s*\(")
        body = self.fns["vc_sc_update"]
        self.assertIn("reg != VC_SC_USE_SEL && reg != VC_SC_IO_SEL", body)
        self.assertIn("mask & ~VC_BUS_PINS", body)
        pins = dict(re.findall(r"#define\s+(VC_PIN_\w+)\s+(\d+)", self.board))
        self.assertEqual(pins, {"VC_PIN_SDA": "11", "VC_PIN_SCL": "12", "VC_PIN_MDIO": "13", "VC_PIN_MDC": "14"})
        # all GPIO register access goes through vc_sc_update / vc_sc_get reads
        for name, body in self.fns.items():
            if name in ("vc_sc_update", "vc_sc_get", "vc_gpio_init"):
                continue
            self.assertNotRegex(body, r"\binl\s*\(", name)

    def test_i2c_only_to_0x18_and_only_in_reset(self):
        self.assertRegex(self.board, r"#define\s+VC_PCA18_ADDR\s+0x18\b")
        xfers = re.findall(r"i2c_smbus_xfer\s*\(\s*[^,]+,\s*(\w+)", self.board)
        self.assertTrue(xfers)
        self.assertEqual(set(xfers), {"VC_PCA18_ADDR"})
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", self.board, flags=re.S)
        self.assertNotRegex(code, r"0x1[cC]\b")
        for name, body in self.fns.items():
            if "vc_pca18_write(" in body and name not in ("vc_pca18_write",):
                self.assertEqual(name, "vc_pca18_reset", "0x18 written from %s" % name)
        calls = [n for n, b in self.fns.items() if re.search(r"\bvc_pca18_reset\s*\(\s*\)\s*;", b)]
        self.assertEqual(calls, ["vc_board_init"])
        for p in (SWITCH,):
            self.assertNotRegex(read(p), r"i2c_\w+\s*\(")

    def test_reset_refuses_when_bound_and_locks_devices(self):
        body = self.fns["vc_pca18_reset"]
        lock = body.index("device_lock(")
        check = body.index("dev.driver")
        first_write = body.index("vc_pca18_write(")
        self.assertLess(lock, first_write)
        self.assertLess(check, first_write)
        self.assertRegex(body, r"if \(bound\) \{[^}]*goto out;")

    def test_igb_glue_touches_no_gpio_or_i2c(self):
        for p in (P200, P201):
            added = "\n".join(l[1:] for l in read(p).splitlines() if l.startswith("+") and not l.startswith("+++"))
            self.assertNotRegex(added, r"\bout[bwl]\s*\(|i2c_smbus|i2c_transfer|gpiod_")

    def test_fan_tool_write_paths(self):
        tree = ast.parse(read(FAN))
        consts = {n.targets[0].id: n.value for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        self.assertEqual(ast.literal_eval(consts["PCA_POE_BITS"]), 0x03)
        self.assertEqual(ast.literal_eval(consts["PCA_FAN_BITS"]), 0xC0)
        self.assertEqual(ast.literal_eval(consts["PCA_RESETS_ADDR"]), 0x18)
        # raw.write only inside GuardedBus.write_emc / GuardedBus.pca1c_rmw
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "write" and isinstance(node.func.value, ast.Attribute)
                            and node.func.value.attr == "raw"):
                        self.assertEqual(cls.name, "GuardedBus")
                        self.assertIn(fn.name, ("write_emc", "pca1c_rmw"))
                        target = ast.unparse(node.args[0])
                        self.assertEqual(target, {"write_emc": "EMC_ADDR", "pca1c_rmw": "PCA_FAN_ADDR"}[fn.name])
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name != "LinuxI2C"]:
            for node in ast.walk(cls):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_xfer":
                    self.fail("_xfer called outside LinuxI2C")
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_xfer":
                    self.fail("_xfer called from module function %s" % fn.name)

    def test_bus_providers_are_pinned(self):
        # igb pins vc-edge5x0-board while it holds the WAN PHY bus,
        # and the switch module pins igb (and checks the bus state) on every use
        p201 = read(P201)
        self.assertIn("+		if (!try_module_get(bus->owner)) {", p201)
        self.assertIn("+		module_put(dev_spec->vc_ext_bus->owner);", p201)
        sw = read(SWITCH)
        fns = c_functions(sw)
        for name, body in fns.items():
            if "mdio_find_bus(" in body:
                self.assertEqual(name, "vc_get_bus")
        self.assertIn("try_module_get(bus->owner)", fns["vc_get_bus"])
        self.assertIn("bus->state != MDIOBUS_REGISTERED", fns["vc_get_bus"])

    def test_nvm_read_under_device_lock(self):
        fns = c_functions(self.board)
        self.assertIn("device_lock_assert(&pdev->dev);", fns["vc_read_nvm_id"])
        self.assertIn("device_lock(&pdev->dev);", fns["vc_read_nvm_id_locked"])
        self.assertIn("vc_read_nvm_id_locked(pdev, nvm)", fns["vc_board_supported"])

    def test_no_shell_hardware_pokes(self):
        for root in ("systemd", "kernel", "initramfs-tools", "packaging"):
            for d, _, files in os.walk(os.path.join(SRC, root)):
                for f in files:
                    t = read(os.path.join(d, f))
                    self.assertNotRegex(t, r"i2cset|i2ctransfer|/dev/port|/sys/class/gpio|setpci|outb", f)


HARNESS = r"""
#include <stdio.h>
#include <stdint.h>
typedef uint32_t u32;
#define BIT(n) (1UL << (n))
#define WARN_ON(x) (x)
#define raw_spin_lock_irqsave(l, f) ((void)(f))
#define raw_spin_unlock_irqrestore(l, f) ((void)(f))
#define VC_SC_USE_SEL 0x00
#define VC_SC_IO_SEL 0x04
#define VC_SC_GP_LVL 0x08
#define VC_PIN_SDA 11
#define VC_PIN_SCL 12
#define VC_PIN_MDIO 13
#define VC_PIN_MDC 14
#define VC_BUS_PINS (BIT(VC_PIN_SDA) | BIT(VC_PIN_SCL) | BIT(VC_PIN_MDIO) | BIT(VC_PIN_MDC))
static int vc_gpio_lock;
static unsigned short vc_gbase = 0x500;
static u32 vc_lvl_shadow;
/* simulated SC bank with the Avoton read-back bug: GP_LVL reads return the
 * pad level for inputs and 0 for outputs; writes set the output latch */
static u32 hw_use = 0x0006f800 | BIT(20), hw_io = 0x00069800 & ~BIT(20), latch, pads = 0x00049800;
static u32 inl(unsigned port) {
	unsigned r = port - 0x500;
	if (r == VC_SC_USE_SEL) return hw_use;
	if (r == VC_SC_IO_SEL) return hw_io;
	return pads & hw_io;            /* outputs read back as 0 */
}
static void outl(u32 v, unsigned port) {
	unsigned r = port - 0x500;
	if (r == VC_SC_USE_SEL) hw_use = v;
	else if (r == VC_SC_IO_SEL) hw_io = v;
	else latch = v;
}
%s
int main(void) {
	int i;
	latch = BIT(20);                /* a foreign GPIO output driven high by the firmware */
	vc_lvl_shadow = latch;          /* the seed: as at load, before the read-back is wrong */
	for (i = 0; i < 1000; i++) {
		vc_sc_update(VC_SC_GP_LVL, BIT(VC_PIN_MDC), (i & 1) ? BIT(VC_PIN_MDC) : 0);
		vc_sc_update(VC_SC_GP_LVL, BIT(VC_PIN_MDIO), (i & 2) ? BIT(VC_PIN_MDIO) : 0);
		vc_sc_update(VC_SC_IO_SEL, BIT(VC_PIN_SDA), (i & 4) ? BIT(VC_PIN_SDA) : 0);
	}
	vc_sc_update(VC_SC_GP_LVL, BIT(20), 0);   /* a foreign pin must be refused */
	printf("%%d %%d\n", (latch >> 20) & 1, (latch & ~VC_BUS_PINS) == BIT(20));
	return 0;
}
"""


class TestGpioShadow(unittest.TestCase):
    """GP_LVL writes never take another pin's level from a read."""

    def test_vc_sc_update_keeps_foreign_output_levels(self):
        import shutil
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            self.skipTest("no C compiler")
        body = c_functions(read(BOARD))["vc_sc_update"]
        fn = "static void vc_sc_update(unsigned int reg, u32 mask, u32 val)\n{" + body
        with tempfile.TemporaryDirectory() as td:
            src, exe = os.path.join(td, "h.c"), os.path.join(td, "h")
            with open(src, "w") as f:
                f.write(HARNESS % fn)
            r = subprocess.run([cc, "-w", "-o", exe, src], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = subprocess.run([exe], capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["1", "1"], "foreign output pin 20 lost its level, or other bits changed")

    def test_gp_lvl_branch_never_reads(self):
        body = c_functions(read(BOARD))["vc_sc_update"]
        gp = body.split("if (reg == VC_SC_GP_LVL) {", 1)[1].split("} else {", 1)[0]
        self.assertNotIn("inl(", gp)
        self.assertIn("outl(vc_lvl_shadow, vc_gbase + VC_SC_GP_LVL);", gp)


class TestIgbPatchedSource(unittest.TestCase):
    """0.3.1: the patched igb never reaches the mutex-protected external MDIO
    bus from atomic context checked on the source that
    dkms builds: pristine Debian igb + patch 200 + patch 201."""

    PHY_CALLS = re.compile(r"\b(igb_read_phy_reg|igb_write_phy_reg|igb_get_phy_info|phy\.ops\.(read|write)_reg)\s*\(")

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.td = tempfile.TemporaryDirectory()
        if not shutil.which("patch"):
            raise unittest.SkipTest("no patch(1)")
        shutil.copytree(os.path.join(SRC, "dkms/igb-vc/drivers"), os.path.join(cls.td.name, "drivers"))
        for pf in (P200, P201):
            with open(pf, "rb") as f:
                r = subprocess.run(["patch", "-s", "-p1", "-d", cls.td.name], stdin=f, capture_output=True)
            assert r.returncode == 0, r.stdout + r.stderr
        d = os.path.join(cls.td.name, "drivers/net/ethernet/intel/igb")
        cls.main = read(os.path.join(d, "igb_main.c"))
        cls.vc = read(os.path.join(d, "igb_vc.c"))
        cls.fmain, cls.fvc = c_functions(cls.main), c_functions(cls.vc)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_guard_is_first_in_both_accessors(self):
        for fn, call in (("igb_vc_ext_read_reg", "mdiobus_read(bus"), ("igb_vc_ext_write_reg", "mdiobus_write(bus")):
            body = self.fvc[fn]
            g = body.index('WARN_ONCE(!in_task() || !preemptible(), "igb-vc: ext MDIO from atomic ctx")')
            self.assertLess(g, body.index(call), fn)
            self.assertIn("return -E1000_ERR_PHY;", body[g:body.index(call)], fn)

    def test_timer_only_schedules_the_work(self):
        t = self.fmain["igb_update_phy_info"]
        self.assertIn("schedule_work(&adapter->phy_info_task);", t)
        self.assertNotRegex(t, self.PHY_CALLS)
        self.assertIn("igb_get_phy_info(&adapter->hw);", self.fmain["igb_phy_info_task"])
        self.assertIn("INIT_WORK(&adapter->phy_info_task, igb_phy_info_task);", self.main)

    def test_work_cancelled_after_timer_in_down_and_remove(self):
        for fn in ("igb_down", "igb_remove"):
            b = self.fmain[fn]
            i = b.index("del_timer_sync(&adapter->phy_info_timer);")
            self.assertIn("cancel_work_sync(&adapter->phy_info_task);", b[i:i + 120], fn)

    def test_buses_unregistered_after_netdev(self):
        b = self.fmain["igb_remove"]
        self.assertLess(b.index("unregister_netdev(netdev);"), b.index("igb_vc_mii_unregister(adapter);"))

    def test_audit_no_phy_access_from_timers(self):
        callbacks = re.findall(r"timer_setup\(&adapter->\w+, (\w+), 0\)", self.main)
        self.assertIn("igb_update_phy_info", callbacks)
        for cb in callbacks:
            self.assertNotRegex(self.fmain[cb], self.PHY_CALLS, cb)

    def test_audit_no_phy_access_under_spinlocks(self):
        for m in re.finditer(r"spin_lock(?:_irqsave|_bh)?\(&(adapter->\w+)", self.main):
            lock = m.group(1)
            end = self.main.index("spin_unlock", m.end())
            region = self.main[m.end():end]
            self.assertNotRegex(region, self.PHY_CALLS, lock)
            for callee in re.findall(r"\b(igb_\w+)\s*\(", region):
                if callee in self.fmain:
                    self.assertNotRegex(self.fmain[callee], self.PHY_CALLS, "%s under %s" % (callee, lock))


class Test8Provenance(unittest.TestCase):
    def test_lifted_file_headers(self):
        h200 = read(P200).split("diff -Naur", 1)[0]
        for needle in ("github.com/gallops-gannets/velo540openwrt", "patches/200-igb-velocloud-edge5x0.patch",
                       PINNED, "GPL-2.0"):
            self.assertIn(needle, h200)
        hb = read(BOARD).split("*/", 1)[0]
        for needle in ("velo540openwrt", "package/velo540-glue/src/vc-edge5x0-mdio.c", PINNED, "GPL-2.0"):
            self.assertIn(needle, hb)

    def test_dsa_glue_and_patch230_headers(self):
        hd = read(DSA).split("*/", 1)[0]
        for needle in ("velo540openwrt", "package/velo540-glue/src/vc-edge5x0-dsa.c", PINNED, "GPL-2.0"):
            self.assertIn(needle, hd)
        h230 = read(P230).split("\nFrom: ", 1)[0]
        for needle in ("github.com/gallops-gannets/velo540openwrt", "patches/230-dsa-pdata-own-tree.patch",
                       PINNED, "GPL-2.0"):
            self.assertIn(needle, h230)

    def test_patch230_diff_is_upstream_byte_identical(self):
        body = "From: " + read(P230).split("\nFrom: ", 1)[1]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(),
                         "a686e23da944ce89954bb8f222aefc1f22646fab0b04125e9e8a53c84b9c7940")

    def test_patch200_diff_is_upstream_byte_identical(self):
        body = "diff -Naur" + read(P200).split("diff -Naur", 1)[1]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(),
                         "a3a0af3d0b84477bc99688e2a86eeafb2cd33f26e39a1f7e7492be198ff62b01")

    def test_provenance_file_lists_everything(self):
        prov = read(os.path.join(SRC, "PROVENANCE.md"))
        for needle in (PINNED, "200-igb-velocloud-edge5x0.patch", "vc-edge5x0-mdio.c",
                       "201-velo5x0-bsp-ext-mdio-bus-name.patch", "linux-source-6.12", "6.12.118-1",
                       "vc-edge5x0-dsa.c", "230-dsa-pdata-own-tree.patch"):
            self.assertIn(needle, prov)

    def test_igb_sources_match_recorded_hashes(self):
        d = os.path.join(SRC, "dkms/igb-vc/drivers/net/ethernet/intel/igb")
        rec = {}
        for line in read(os.path.join(SRC, "dkms/igb-vc/SOURCE.sha256")).splitlines():
            h, f = line.split()
            rec[f] = h
        self.assertEqual(sorted(rec), sorted(os.listdir(d)))
        for f, h in rec.items():
            with open(os.path.join(d, f), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), h, f)

    def test_every_own_source_has_spdx(self):
        for p in (BOARD, SWITCH, DSA, FAN, KBUILD, os.path.join(SRC, "tools/velo5x0-check"),
                  os.path.join(SRC, "tools/velo5x0-leakscan"),
                  os.path.join(SRC, "kernel-build/velo5x0-kernel-build-docker")):
            self.assertIn("SPDX-License-Identifier: GPL-2.0", read(p)[:200], p)

    @unittest.skipUnless(os.environ.get("VELO5X0_UPSTREAM"), "VELO5X0_UPSTREAM not set")
    def test_no_unlicensed_upstream_text(self):
        up = os.environ["VELO5X0_UPSTREAM"]
        lines = set()
        for d, _, files in os.walk(os.path.join(up, "files")):
            for f in files:
                try:
                    for l in read(os.path.join(d, f)).splitlines():
                        s = " ".join(l.split())
                        if len(s) >= 30:
                            lines.add(s)
                except (UnicodeDecodeError, OSError):
                    pass
        hits = []
        for d, _, files in os.walk(SRC):
            if "/tests" in d:
                continue
            for f in files:
                try:
                    for n, l in enumerate(read(os.path.join(d, f)).splitlines(), 1):
                        if " ".join(l.split()) in lines:
                            hits.append("%s:%d" % (os.path.join(d, f), n))
                except (UnicodeDecodeError, OSError):
                    pass
        self.assertEqual(hits, [])


class Test11LeakScan(unittest.TestCase):
    def test_package_tree_has_no_site_inventory(self):
        r = subprocess.run([sys.executable, os.path.join(SRC, "tools/velo5x0-leakscan"), SRC],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_scanner_catches_planted_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "x.conf"), "w") as f:
                ip = ".".join(["10", "1", "2", "3"])
                mac = ":".join(["12", "34", "56", "78", "9a", "bc"])
                host = "nas" + "." + "local"
                f.write("gw %s\nmac %s\nhost %s\nok 192.0.2.7 00:00:5e:00:53:01\n" % (ip, mac, host))
            r = subprocess.run([sys.executable, os.path.join(SRC, "tools/velo5x0-leakscan"), td],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("3 finding(s)", r.stdout)


class TestCodeMatchesRequirements(unittest.TestCase):
    def setUp(self):
        self.spec = re.sub(r"\s+", " ", read(REQS))

    def test_fan_recipe(self):
        m = re.search(r"recipe (0x50=0x00, 0x42=0x10, 0x2b=0x0c, 0x41=0x01)", self.spec)
        self.assertTrue(m)
        spec_seq = [(int(a, 16), int(b, 16)) for a, b in re.findall(r"(0x[0-9a-f]+)=(0x[0-9a-f]+)", m.group(1))]
        tree = ast.parse(read(FAN))
        recipe = [n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "EMC_RECIPE"][0]
        code = [tuple(ast.literal_eval(ast.unparse(e)) if not isinstance(e, ast.Tuple) else
                      [ast.literal_eval(x) if isinstance(x, ast.Constant) else
                       {"EMC_LUT_CONFIG": 0x50, "EMC_FAN_CONFIG1": 0x42, "EMC_PWM_BASE": 0x2B,
                        "EMC_PWM_DIVIDE": 0x41}[x.id] for x in e.elts]) for e in recipe.value.elts]
        self.assertEqual([tuple(c) for c in code], spec_seq)
        self.assertIn("product ID register (0xfd) at 0x2f", self.spec)
        self.assertIn("(product ID register 0xfd reads 0x1d)", self.spec)

    def test_reset_sequence(self):
        self.assertIn("POL=0x00, OUT=0xE3, CFG=0x00, wait 10 ms, OUT=0xFF, wait 50 ms", self.spec)
        board = read(BOARD)
        self.assertRegex(board, r"#define\s+VC_PCA18_OUT_ASSERT\s+0xe3")
        self.assertRegex(board, r"#define\s+VC_PCA18_OUT_RELEASE\s+0xff")
        body = c_functions(board)["vc_pca18_reset"]
        seq = re.findall(r"vc_pca18_write\((PCA9557_REG_\w+), ([\w]+)\)|msleep\((\d+)\)", body)
        flat = [(a, b) if a else ("sleep", c) for a, b, c in seq]
        self.assertEqual(flat[:6], [("PCA9557_REG_POL", "0x00"), ("PCA9557_REG_OUT", "VC_PCA18_OUT_ASSERT"),
                                    ("PCA9557_REG_CFG", "0x00"), ("sleep", "10"),
                                    ("PCA9557_REG_OUT", "VC_PCA18_OUT_RELEASE"), ("sleep", "50")])

    def test_pins_and_jacks(self):
        self.assertIn("(SC11 SDA, SC12 SCL)", self.spec)
        self.assertIn("(SC14 MDC, SC13 MDIO)", self.spec)
        self.assertIn("LAN1=B1, LAN2=B0, LAN3=B3, LAN4=B2, LAN5=A2, LAN6=A0, LAN7=A1, LAN8=A3", self.spec)
        sw = read(SWITCH)
        self.assertIn("switch A: LAN6, LAN7, LAN5, LAN8 on ports 0-3", sw)
        self.assertIn("switch B: LAN2, LAN1, LAN4, LAN3 on ports 0-3", sw)


UUID = "0000aaaa-1111-2222-3333-444455556666"


class TestGuard(unittest.TestCase):
    """VBSP-REQ-5: verify, defer on missing headers, pin GRUB."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = self.td.name
        self.mod, self.boot = os.path.join(t, "modules"), os.path.join(t, "boot")
        self.state = os.path.join(t, "state")
        os.makedirs(self.mod)
        os.makedirs(self.boot)
        self.cfg = os.path.join(t, "grub.cfg")
        self.calls = os.path.join(t, "calls")
        stub = os.path.join(t, "stub")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$(basename $0) $*" >> %s\n' % self.calls)
        os.chmod(stub, 0o755)
        for n in ("setdefault", "initramfs"):
            os.symlink(stub, os.path.join(t, n))
        envl = os.path.join(t, "envlist")
        with open(envl, "w") as f:
            f.write("#!/bin/sh\necho saved_entry=old\n")
        os.chmod(envl, 0o755)
        self.env = dict(os.environ, VELO5X0_MODROOT=self.mod, VELO5X0_BOOT=self.boot,
                        VELO5X0_GRUBCFG=self.cfg, VELO5X0_STATE=self.state,
                        VELO5X0_SETDEFAULT=os.path.join(t, "setdefault"),
                        VELO5X0_INITRAMFS=os.path.join(t, "initramfs"),
                        VELO5X0_GRUBENV_LIST=envl, PATH="/usr/bin:/bin")

    def tearDown(self):
        self.td.cleanup()

    def kernel(self, k, modules=("vc-edge5x0-board", "vc-edge5x0-switch", "igb"), headers=True,
               image=True, initrd=True):
        d = os.path.join(self.mod, k, "updates/dkms")
        os.makedirs(d, exist_ok=True)
        for m in modules:
            open(os.path.join(d, m + ".ko.xz"), "w").close()
        if headers:
            os.makedirs(os.path.join(self.mod, k, "build"), exist_ok=True)
            open(os.path.join(self.mod, k, "build", "Makefile"), "w").close()
        if image:
            open(os.path.join(self.boot, "vmlinuz-" + k), "w").close()
        if initrd:
            open(os.path.join(self.boot, "initrd.img-" + k), "w").close()

    def grubcfg(self, kernels):
        lines = ["submenu 'Advanced options for Debian GNU/Linux' $menuentry_id_option 'gnulinux-advanced-%s' {" % UUID]
        for k in kernels:
            lines.append("\tmenuentry 'Debian GNU/Linux, with Linux %s' --class debian $menuentry_id_option 'gnulinux-%s-advanced-%s' {" % (k, k, UUID))
            lines.append("\tmenuentry 'Debian GNU/Linux, with Linux %s (recovery mode)' --class debian $menuentry_id_option 'gnulinux-%s-recovery-%s' {" % (k, k, UUID))
        lines.append("}")
        with open(self.cfg, "w") as f:
            f.write("\n".join(lines) + "\n")

    def run_guard(self, *args):
        r = subprocess.run(["/bin/sh", GUARD] + list(args), env=self.env, capture_output=True, text=True)
        log = read(os.path.join(self.state, "kernel-guard.log")) if os.path.exists(
            os.path.join(self.state, "kernel-guard.log")) else ""
        calls = read(self.calls) if os.path.exists(self.calls) else ""
        return r.returncode, log, calls

    def entry(self, k):
        return "gnulinux-advanced-%s>gnulinux-%s-advanced-%s" % (UUID, k, UUID)

    def test_image_with_modules_passes(self):
        self.kernel("6.12.200+test-amd64")
        rc, log, _ = self.run_guard("image", "6.12.200+test-amd64")
        self.assertEqual(rc, 0)
        self.assertIn("board modules present", log)

    def test_image_without_headers_defers_and_is_not_pinned(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.200+test-amd64", modules=(), headers=False)
        self.grubcfg(["6.12.200+test-amd64", "6.12.117+test-amd64"])
        rc, log, _ = self.run_guard("image", "6.12.200+test-amd64")
        self.assertEqual(rc, 0)
        self.assertIn("deferred", log)
        rc, log, calls = self.run_guard("pin")
        self.assertIn("setdefault " + self.entry("6.12.117+test-amd64"), calls)
        self.assertNotIn("6.12.200", calls)

    def test_image_build_failure_fails_loudly(self):
        self.kernel("6.12.200+test-amd64", modules=("igb",))
        rc, log, _ = self.run_guard("image", "6.12.200+test-amd64")
        self.assertEqual(rc, 1)
        self.assertIn("DKMS build failed", log)
        self.assertIn("vc-edge5x0-board", log)

    def test_other_series_fails(self):
        self.kernel("6.17.1+test-amd64")
        rc, log, _ = self.run_guard("image", "6.17.1+test-amd64")
        self.assertEqual(rc, 1)
        self.assertIn("outside the 6.12 series", log)

    def test_headers_after_image_rebuilds_initramfs_and_pins(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.200+test-amd64")
        self.grubcfg(["6.12.200+test-amd64", "6.12.117+test-amd64"])
        rc, log, calls = self.run_guard("headers", "6.12.200+test-amd64")
        self.assertEqual(rc, 0)
        self.assertIn("initramfs -u -k 6.12.200+test-amd64", calls)
        self.assertIn("setdefault " + self.entry("6.12.200+test-amd64"), calls)

    def test_headers_build_failure_fails_and_does_not_pin(self):
        self.kernel("6.12.200+test-amd64", modules=("igb",))
        self.grubcfg(["6.12.200+test-amd64"])
        rc, log, calls = self.run_guard("headers", "6.12.200+test-amd64")
        self.assertEqual(rc, 1)
        self.assertNotIn("setdefault", calls)

    def test_headers_before_image_waits(self):
        self.kernel("6.12.200+test-amd64", image=False, initrd=False)
        rc, log, calls = self.run_guard("headers", "6.12.200+test-amd64")
        self.assertEqual(rc, 0)
        self.assertIn("image not installed yet", log)
        self.assertEqual(calls, "")

    def test_pin_never_picks_an_unverified_kernel_even_if_newest(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.118+test-amd64")
        self.kernel("6.12.200+test-amd64", modules=("igb", "vc-edge5x0-board"))  # build failed
        self.kernel("6.12.300+test-amd64", initrd=False)                          # no initramfs
        self.grubcfg(["6.12.300+test-amd64", "6.12.200+test-amd64", "6.12.118+test-amd64", "6.12.117+test-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.118+test-amd64"))
        self.assertIn("pinned to 6.12.118+test-amd64", log)
        with open(os.path.join(self.state, "pinned-kernel")) as f:
            self.assertEqual(f.read().strip(), "6.12.118+test-amd64")

    def test_pin_without_grub_entry_leaves_default(self):
        self.kernel("6.12.118+test-amd64")
        self.grubcfg([])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls, "")
        self.assertIn("no entry", log)

    def test_pin_excludes_rt_and_cloud(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.300+test-rt-amd64")
        self.kernel("6.12.301+test-cloud-amd64")
        self.grubcfg(["6.12.301+test-cloud-amd64", "6.12.300+test-rt-amd64", "6.12.117+test-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.117+test-amd64"))
        rc, log, calls = self.run_guard("image", "6.12.300+test-rt-amd64")
        self.assertEqual(rc, 0)
        self.assertIn("never made the boot default", log)

    DSA_MODS = ("vc-edge5x0-board", "vc-edge5x0-switch", "igb", "vc-edge5x0-dsa")

    def dsa_intree(self, k, mods=("net/dsa/dsa_core", "drivers/net/dsa/mv88e6xxx/mv88e6xxx", "net/dsa/tag_dsa")):
        for m in mods:
            p = os.path.join(self.mod, k, "kernel", m + ".ko.xz")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()

    def test_dsa_kernel_preferred_over_newer_stock(self):
        self.kernel("6.12.118+velo5x0dsa1", modules=self.DSA_MODS)
        self.dsa_intree("6.12.118+velo5x0dsa1")
        self.kernel("6.12.119+deb13-amd64")
        self.grubcfg(["6.12.119+deb13-amd64", "6.12.118+velo5x0dsa1"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.118+velo5x0dsa1"))

    def test_dsa_kernel_needs_dsa_glue_and_intree_modules(self):
        self.kernel("6.12.118+deb13-amd64")
        self.kernel("6.12.118+velo5x0dsa1")                    # no vc-edge5x0-dsa
        self.dsa_intree("6.12.118+velo5x0dsa1")
        self.kernel("6.12.118+velo5x0dsa2", modules=self.DSA_MODS)
        self.dsa_intree("6.12.118+velo5x0dsa2", mods=("net/dsa/dsa_core",))   # no mv88e6xxx, tag_dsa
        self.grubcfg(["6.12.118+velo5x0dsa2", "6.12.118+velo5x0dsa1", "6.12.118+deb13-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.118+deb13-amd64"))
        rc, log, _ = self.run_guard("image", "6.12.118+velo5x0dsa1")
        self.assertEqual(rc, 1)
        self.assertIn("vc-edge5x0-dsa", log)
        rc, log, _ = self.run_guard("image", "6.12.118+velo5x0dsa2")
        self.assertEqual(rc, 1)
        self.assertIn("mv88e6xxx(in-tree)", log)

    def test_newest_dsa_kernel_first(self):
        for k in ("6.12.118+velo5x0dsa1", "6.12.119+velo5x0dsa1"):
            self.kernel(k, modules=self.DSA_MODS)
            self.dsa_intree(k)
        self.grubcfg(["6.12.119+velo5x0dsa1", "6.12.118+velo5x0dsa1"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.119+velo5x0dsa1"))

    def test_removed_pinned_kernel_is_repinned(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.118+test-amd64")
        self.grubcfg(["6.12.118+test-amd64", "6.12.117+test-amd64"])
        self.run_guard("pin")
        # 6.12.118 removed: its files go, update-grub drops its entry, the postrm hook pins
        import shutil
        shutil.rmtree(os.path.join(self.mod, "6.12.118+test-amd64"))
        os.remove(os.path.join(self.boot, "vmlinuz-6.12.118+test-amd64"))
        os.remove(os.path.join(self.boot, "initrd.img-6.12.118+test-amd64"))
        self.grubcfg(["6.12.117+test-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip().splitlines()[-1], "setdefault " + self.entry("6.12.117+test-amd64"))

    def test_newest_verified_without_entry_falls_back_to_older(self):
        self.kernel("6.12.117+test-amd64")
        self.kernel("6.12.118+test-amd64")
        self.grubcfg(["6.12.117+test-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(calls.strip(), "setdefault " + self.entry("6.12.117+test-amd64"))
        self.assertIn("trying the next older one", log)

    def test_no_verified_kernel_leaves_default_and_says_if_stale(self):
        self.kernel("6.12.118+test-amd64", modules=("igb",))
        self.grubcfg(["6.12.118+test-amd64"])
        rc, log, calls = self.run_guard("pin")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, "")
        self.assertIn("saved_entry left untouched", log)
        self.assertIn("names no existing entry", log)     # the stub's saved_entry=old

    def test_hooks_are_inert_after_removal(self):
        for h in ("postinst.d/dkms-velo5x0-guard", "postinst.d/zz-velo5x0-pin",
                  "postrm.d/zz-velo5x0-pin", "header_postinst.d/dkms-velo5x0-guard"):
            t = read(os.path.join(SRC, "kernel", h))
            self.assertIn('[ -x "$G" ] || exit 0', t, h)
            with tempfile.TemporaryDirectory() as td:
                w = os.path.join(td, "hook")
                with open(w, "w") as f:
                    f.write(t.replace("/usr/lib/velo5x0-bsp/velo5x0-kernel-guard", os.path.join(td, "absent")))
                self.assertEqual(subprocess.run(["/bin/sh", w, "6.12.1+x-amd64"]).returncode, 0, h)

    def test_removal_restores_grub_default(self):
        b = read(os.path.join(SRC, "packaging/build-debs.sh"))
        prerm = b.split('cat >"$P/DEBIAN/prerm" <<\'EOF\'', 1)[1].split("EOF", 1)[0]
        for line in ("rm -f /etc/default/grub.d/velo5x0-bsp.cfg", "grub-editenv - unset saved_entry", "update-grub"):
            self.assertIn(line, prerm)
        conff = b.split("DEBIAN/conffiles", 1)[0].rsplit("printf", 1)[1]
        self.assertNotIn("grub.d", conff)

    def test_hook_order(self):
        post = sorted(os.listdir(os.path.join(SRC, "kernel/postinst.d")) + ["dkms", "initramfs-tools", "zz-update-grub"])
        self.assertEqual(post, ["dkms", "dkms-velo5x0-guard", "initramfs-tools", "zz-update-grub", "zz-velo5x0-pin"])
        hdr = sorted(os.listdir(os.path.join(SRC, "kernel/header_postinst.d")) + ["dkms"])
        self.assertEqual(hdr, ["dkms", "dkms-velo5x0-guard"])
        rm = sorted(os.listdir(os.path.join(SRC, "kernel/postrm.d")) + ["initramfs-tools", "zz-update-grub"])
        self.assertEqual(rm[-1], "zz-velo5x0-pin")
        self.assertIn("GRUB_DEFAULT=saved", read(os.path.join(SRC, "grub/velo5x0-bsp.cfg")))

    def test_log_is_not_in_var_log(self):
        t = read(GUARD)
        self.assertIn('LOG=${VELO5X0_LOG:-$STATE/kernel-guard.log}', t)
        self.assertIn('STATE=${VELO5X0_STATE:-/var/lib/velo5x0-bsp}', t)


class TestUnitsAndLinks(unittest.TestCase):
    def test_link_files_match_pci_path_only(self):
        for n, f in ((1, 2), (2, 3)):
            t = read(os.path.join(SRC, "systemd/network/10-velo5x0-ge%d.link" % n))
            self.assertIn("Path=pci-0000:00:14.%d" % f, t)
            self.assertIn("Name=ge%d" % n, t)
            self.assertNotRegex(t, r"(?m)^\s*(Permanent)?MACAddress\s*=")

    def test_fan_failsafe_wiring(self):
        fs = read(os.path.join(SRC, "systemd/system/velo5x0-fan-failsafe.service"))
        fd = read(os.path.join(SRC, "systemd/system/velo5x0-fand.service"))
        for line in ("Type=notify", "WatchdogSec=30", "Restart=on-failure"):
            self.assertIn(line, fd)
        sd = read(os.path.join(SRC, "systemd/system-shutdown/velo5x0-fan-full"))
        self.assertIn("ExecStart=/usr/sbin/velo5x0-fan full", fs)
        self.assertIn("ExecStop=/usr/sbin/velo5x0-fan full", fs)
        self.assertIn("ExecStopPost=/usr/sbin/velo5x0-fan full", fd)
        self.assertIn("velo5x0-fan --kmsg full", sd)
        for t in (fs, fd):
            self.assertIn("ConditionFirmware=smbios-field(board_name = EDGE540)", t)

    def test_softdep_order(self):
        t = read(os.path.join(SRC, "modprobe.d/velo5x0-bsp.conf"))
        self.assertIn("softdep igb pre: vc-edge5x0-board post: vc-edge5x0-switch vc-edge5x0-dsa", t)

    def test_initramfs_carries_dsa_when_built(self):
        t = read(os.path.join(SRC, "initramfs-tools/hooks/velo5x0-bsp"))
        self.assertIn('modinfo -k "${version}" vc-edge5x0-dsa', t)
        self.assertIn("manual_add_modules vc-edge5x0-dsa mv88e6xxx tag_dsa", t)


class TestDsaGlue(unittest.TestCase):
    """VBSP-DES-1C / DES-3 / REQ-3 / REQ-4 / REQ-7, static part."""

    def setUp(self):
        self.t = read(DSA)
        self.code = re.sub(r"/\*.*?\*/", "", self.t, flags=re.S)

    def test_no_gpio_i2c_or_port_io(self):
        for pat in (r"\b(outb|outl|inb|inl|outw|inw)\s*\(", r"\bi2c_", r"\bgpio", r"pci_write_config"):
            self.assertNotRegex(self.code, pat)

    def test_jack_map(self):
        self.assertIn('ports_a = "lan6,lan7,lan5,lan8"', self.t)   # switch A ports 0-3 = LAN6, LAN7, LAN5, LAN8
        self.assertIn('ports_b = "lan2,lan1,lan4,lan3"', self.t)   # switch B ports 0-3 = LAN2, LAN1, LAN4, LAN3
        self.assertIn('{ .bus_id = "igb-vc-0000:00:14.0" }', self.t)
        self.assertIn('{ .bus_id = "igb-vc-0000:00:14.1" }', self.t)

    def test_ports_5_6_not_in_platform_data(self):
        self.assertIn("enabled_ports = GENMASK(VC_CPU_PORT, 0)", self.code)
        self.assertIn("#define VC_CPU_PORT\t\t4", self.t)

    def test_switch_register_writes_only_port4_and_serdes(self):
        writes = re.findall(r"vc_wr\(\s*sw\s*,\s*(\w+)\s*,\s*\w+\s*,\s*([^,]+),", self.code)
        self.assertTrue(writes)
        # the only raw mdiobus_write is inside vc_wr itself
        self.assertEqual(len(re.findall(r"mdiobus_write\(", self.code)), 1)
        for bus, addr in writes:
            if bus == "raw":
                self.assertEqual(addr.strip(), "VC_SW_PORT(VC_CPU_PORT)")
            else:
                self.assertEqual((bus, addr.strip()), ("ib", "VC_SERDES_DEV"))

    def test_cpu_link_runs_after_bind_only(self):
        self.assertRegex(self.code, r"(?s)case BUS_NOTIFY_BOUND_DRIVER:.*?schedule_work\(&sw->link_work\)")
        lw = c_functions(self.t)["vc_link_work"]
        self.assertIn("test_bit(VC_BOUND", lw)

    def test_conduit_reference_per_probe_attempt(self):
        self.assertRegex(self.code, r"case BUS_NOTIFY_BIND_DRIVER:\s*dev_hold")
        self.assertRegex(self.code, r"(?s)case BUS_NOTIFY_UNBOUND_DRIVER:.*?test_and_clear_bit\(VC_HELD.*?dev_put")
        # one hold/put pair per probe attempt, one for the lookup in vc_sw_register
        self.assertEqual(self.code.count("dev_hold("), 2)
        self.assertEqual(self.code.count("dev_put("), 2)
        self.assertRegex(self.code, r"(?s)out_bus:\s*if \(nd\)\s*dev_put\(nd\)")

    def test_accesses_fenced_against_unbind_and_removal(self):
        self.assertRegex(self.code, r"(?s)case BUS_NOTIFY_UNBIND_DRIVER:\s*clear_bit\(VC_BOUND.*?mutex_lock\(&vc_io\)")
        self.assertRegex(self.code, r"(?s)case BUS_NOTIFY_DEL_DEVICE:.*?mutex_lock\(&vc_io\)")
        self.assertEqual(len(re.findall(r"mdiobus_read\(", self.code)), 1)   # only inside vc_rd
        ex = c_functions(self.t)["vc_dsa_exit"]
        self.assertLess(ex.index("bus_unregister_notifier"), ex.rindex("cancel_delayed_work_sync"))

    def test_switch_module_defers_to_dsa_on_dsa_kernels(self):
        sw = read(SWITCH)
        self.assertIn('static char *mode = "auto";', sw)
        self.assertIn('IS_ENABLED(CONFIG_NET_DSA_MV88E6XXX) ? "dsa" : "unmanaged"', sw)

    def test_dkms_build_exclusive(self):
        t = read(os.path.join(SRC, "dkms/vc-edge5x0-dsa/dkms.conf"))
        self.assertIn('BUILD_EXCLUSIVE_CONFIG="CONFIG_NET_DSA_MV88E6XXX"', t)
        b = read(os.path.join(SRC, "packaging/build-debs.sh"))
        self.assertIn("vc-edge5x0 igb-vc vc-edge5x0-dsa", b)
        self.assertIn("\\$rc != 77", b)


class TestKernelBuild(unittest.TestCase):
    def test_fragment_is_dsa_and_signing_only(self):
        lines = [l for l in read(os.path.join(SRC, "kernel-build/config/velo5x0-dsa.config")).splitlines()
                 if l and not l.startswith("#")]
        self.assertEqual(sorted(lines), sorted([
            "CONFIG_NET_DSA=m", "CONFIG_NET_DSA_MV88E6XXX=m", "CONFIG_NET_DSA_TAG_DSA=m",
            "CONFIG_NET_DSA_TAG_EDSA=m", 'CONFIG_SYSTEM_TRUSTED_KEYS=""']))

    def test_script_refuses_other_series_and_never_deletes(self):
        t = read(KBUILD)
        self.assertIn("6.12.*-*) ;;", t)
        self.assertIn('SUFFIX="+velo5x0dsa$REV"', t)
        self.assertNotRegex(t, r"(?m)^[^#]*\brm\s")
        r = subprocess.run(["bash", KBUILD, "--debian-version", "6.13.1-1"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only Debian 6.12-series", r.stderr)

    def test_config_guard_present(self):
        t = read(KBUILD)
        self.assertIn("olddefconfig changed a symbol Debian set", t)
        self.assertIn("new symbol turned on outside the allow list", t)
        self.assertIn("NEW_SYMBOL_ALLOW='^CONFIG_(NET_DSA[A-Z0-9_]*|MODULE_SIG_ALL)$'", t)


class TestPackagedLicence(unittest.TestCase):
    """Both .debs carry the licence text as /usr/share/doc/<package>/copyright."""

    def test_copyright_files_in_both_packages(self):
        b = read(os.path.join(SRC, "packaging/build-debs.sh"))
        self.assertIn('"$SRC/LICENSE" "$P/usr/share/doc/velo5x0-bsp-dkms/copyright"', b)
        self.assertIn('"$P/usr/share/doc/velo5x0-bsp/copyright" "$SRC/LICENSE"', b)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", read(os.path.join(SRC, "LICENSE")))


if __name__ == "__main__":
    unittest.main()
