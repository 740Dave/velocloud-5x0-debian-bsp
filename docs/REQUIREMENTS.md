# Requirements this project is built to

Comments in the code, the tests and the other documents refer to numbered
items such as `VBSP-REQ-10` (VBSP = velo5x0 board support package). This file
states, in plain English, what each referenced item requires or records. The
words MUST and MUST NOT carry their usual meaning: a build or change that
breaks one is a bug.

Items marked (0.5.0) are not in 0.4.1.

Scope: a VeloCloud Edge 540, board version 2.x, running Debian 13 ("trixie")
with a 6.12-series kernel. The Edge 520 is within the intent but has not been
checked on a 520.

## Requirements (VBSP-REQ-*)

**REQ-1. The four I354 functions bind igb with no probe error.** After boot, PCI
functions 00:14.0, 00:14.1, 00:14.2 and 00:14.3 MUST each be bound to the
board's igb driver, and the kernel log MUST contain no igb probe failure for
any of them.

**REQ-2. GE1 and GE2 link at 1000 Mb/s full duplex and pass traffic.** They MUST
be named `ge1` (PCI function 0000:00:14.2) and `ge2` (0000:00:14.3) by systemd
`.link` files that match on the PCI path, not on the MAC address, so the
names hold on any unit.

**REQ-3. `lanN` is front jack LANN, for N from 1 to 8.** Interfaces `lan1` to
`lan8` MUST exist, `lanN` MUST be the interface of front jack LANN according to
the jack map below, and each jack MUST link at 1000 Mb/s full duplex and pass
traffic. This needs the DSA kernel (see `docs/TECHNICAL.md`).

**REQ-4. Each `lanN` is usable independently.** Two jacks placed in different
bridges or VLANs MUST be isolated from each other. The inter-chip switch ports
5 and 6 MUST be disabled.

**REQ-5. Requirements 1 to 4 survive reboots and kernel updates.** They MUST hold
after three consecutive reboots, and again after a newer Debian kernel package
within the 6.12 series is installed and the box rebooted onto it, with no manual
step in between. If the DKMS build fails for a newly installed kernel, the box
MUST keep booting a kernel whose modules built (the failing kernel MUST NOT
become the GRUB default, or the package install MUST fail loudly before reboot)
and the failure MUST be logged. A new kernel series is out of scope and needs
re-qualification.

**REQ-6. Module and service load order is deterministic.** The resets MUST be
released, and the GPIO, I2C and MDIO buses MUST be present, before igb binds.
The switch CPU-link setup MUST be run again after the DSA driver resets the
switch chips.

**REQ-7. The software never does any of the three forbidden hardware writes.**
Every shipped file, in every runtime state, MUST satisfy 7A, 7B and 7C.

- **REQ-7A.** The software MUST NOT drive SoC GPIO pin SUS2 (the power-cycle
  line) low, by any write to the SUS bank GP_LVL or IO_SEL register or any other
  path.
- **REQ-7B.** Any write to the PCA9557 at I2C address 0x1c MUST be a
  read-modify-write that preserves bits 0 (PoE shutdown) and 1 (PoE reset)
  exactly as read. The software MUST NOT change bits 0 and 1 of the output,
  polarity or configuration register.
- **REQ-7C.** The software MUST NOT pulse bit 4 (the WAN PHY reset) of the
  PCA9557 at I2C address 0x18 at any time when igb is bound to PCI function
  00:14.2 or 00:14.3. The only permitted pulse is the one in the reset sequence
  of DES-1A, which completes before igb binds (pulsing it under a bound igb
  hangs the transmitter).

**REQ-8. Lifted files carry their provenance.** Every file taken from another
project MUST name its upstream source path, upstream commit and licence in its
header or in a `SOURCE` file beside it (pristine kernel files keep their own
headers), and in `PROVENANCE.md`, and no file whose licence is absent or not
GPL-compatible may be copied.

**REQ-10. The fan is off unless temperature requires it, never runs below the
stall-free duty, and goes to full speed on any control failure.** On the 540
only, a fan-control service MUST read the CPU temperature from coretemp and set
the EMC2104 fan controller (I2C 0x2f) duty with hysteresis.
Before any write the service MUST read the chip product ID register (0xfd) at
0x2f on the board I2C bus and find 0x1d; if the chip is absent or the ID
differs it MUST NOT write and MUST log the reason. At or below a low
temperature threshold the fan MUST be off (duty 0). When running it MUST NOT
run below the stall-free minimum duty: it is either off or at or above that
duty. After the precondition has passed, on service stop, on a crash and on a
CPU temperature sensor read error the service MUST attempt full duty (write
0xff); if that write fails, or the chip stops answering, it MUST log the failure
and raise an alert. Across shutdown, reboot and early boot, fail-safe steps
write full duty as well (see DES-5). Default thresholds (proposed, not yet
validated on every fan): on at 60 C, off at 50 C or below, full above 80 C,
hottest core temperature (the SoC has no package sensor).

**REQ-11. The published package contains no site inventory.** No IP addresses, no
MAC addresses other than ones documented as examples, no hostnames and no unit
serial numbers. `tools/velo5x0-leakscan` checks this.

**REQ-12. The software acts only on a supported board.** It MUST identify the
board by DMI (EDGE520 or EDGE540, version 2.x) and by igb NVM words 0x06 and
0x07 equal to `Vc5X`; on any other board, including a rev A board (version 1.x),
it MUST touch no hardware and MUST write one clear log line saying why.

**REQ-13. The replacement igb does not regress the I350 and its SFP ports.**
With the board igb installed, the I350 functions at PCI 04:00.x MUST still bind
to igb, each with a network interface, exactly as with the stock igb. If an SFP
module is fitted, its link MUST come up and pass traffic as on stock Debian.

**REQ-14 (0.5.0). Fan duty is the greater of CPU demand and drive demand.** On the
540 the service MUST write max(CPU demand, drive demand). CPU demand is the
duty REQ-10 gives (0 for off). Drive demand is a function of T, the hottest
temperature among the drives that report one, and one state bit, drive-demand-on:

| T (hottest reporting drive) | Drive demand |
|---|---|
| at or below 55 C | none (duty 0); drive-demand-on is cleared |
| above 55 C and below 60 C | duty 69 (27 %) if drive-demand-on is set, otherwise none |
| 60 C or more | drive-demand-on is set; duty 69 (69 of 255) |
| 65 C to 70 C | rises linearly from 69 at 65 C to 255 at 70 C, rounded to the nearest integer |
| 70 C or more | duty 255 (full) |

Hysteresis: once drive-demand-on is set, drive demand MUST NOT fall below 69
until T is at or below 55 C (or no drive reports a temperature). Drive demand is
evaluated on every poll of the CPU loop. The thresholds and the 69 minimum are
configurable in `/etc/velo5x0-bsp/fan.conf`.

- **REQ-14A (0.5.0).** A drive that reports no temperature (for example onboard flash)
  MUST be ignored. A 540 with no drive that reports a temperature MUST behave
  exactly as before REQ-14 (CPU demand only, the same writes and fail-safes); the
  only addition is one log line at start, and again on change, saying that no
  drive input is present (not an error, warning or alert). A failure to read a
  drive temperature, or a drive that disappears, MUST NOT trigger the full-duty
  fail-safe: that drive is removed from the choice (clearing drive-demand-on if
  no drive then reports), logged once per change, and published. The CPU sensor
  error and every other fail-safe are unchanged. The implementation reads drives
  on a separate thread so that a hung drive cannot delay the control loop. A
  drive whose read hangs (no fresh reading for 30 s, device still present) keeps
  its last drive demand until a fresh reading arrives or the device disappears,
  so a hot drive keeps being cooled; only a read that returns an error, or a
  device that disappears, drops its demand.
- **REQ-14B (0.5.0).** The minimum non-zero drive duty (69 of 255) MUST be greater than
  the stall-free duty (24), and the stall detection MUST NOT trip on a healthy
  fan at that duty. If a configuration sets a stall-free duty at or above the
  drive minimum, the drive minimum is raised to just above it, with a warning.
- **REQ-14C (0.5.0).** On every poll the service MUST publish, in a machine-readable
  state file under `/run/velo5x0-fan/` (`state.json`): the temperature of the
  hottest drive used as the input (or none), which drive it is, whether drive
  demand is active, and for each drive that reported before, whether it
  currently reports. Publishing to MQTT or a home-automation system is outside
  this package.
- **REQ-14D (0.5.0).** The drive input MUST ship in the package, enabled by default,
  with no per-machine configuration: no file edit, no unit enabled by hand, no
  list of drives. On a 520 (no fan) the fan service is not enabled, so it has
  nothing to act on.
- **REQ-14E (0.5.0).** Drives MUST be discovered at run time, not from a list fixed at
  install or start: a drive added later is picked up with no reinstall,
  configuration change or manual restart. (The kernel `drivetemp` source is
  rediscovered every poll; the `smartctl` fallback, used only when drivetemp
  exposes no drive, runs at most once a minute.)

## Design and hardware notes (VBSP-DES-*, VBSP-HW-*)

**DES-1.** The board support ships as Debian DKMS packages so it is rebuilt for
every kernel update: the board module (1A), a patched igb (1B) and the switch
registration module `vc-edge5x0-dsa` (1C), with a DSA-enabled rebuild of
Debian's kernel for per-jack interfaces.

- **DES-1A.** A DMI-matched board module that, only on a supported board:
  switches SoC GPIO SC11 to SC14 to GPIO; registers a bit-banged I2C bus (SC11
  SDA, SC12 SCL) and a bit-banged MDIO bus (SC14 MDC, SC13 MDIO); then runs the
  reset sequence on the PCA9557 at 0x18 (POL=0x00, OUT=0xE3, CFG=0x00, wait 10
  ms, OUT=0xFF, wait 50 ms), all before igb binds.
- **DES-1B.** The velo540openwrt igb patch 200 is carried as a DKMS igb replacing
  the in-tree one; it routes function 2 and 3 PHY access to the bit-bang bus and
  publishes function 0 and 1's own MDIO for the switch drivers. The same igb also
  drives the I350, so it MUST NOT regress it (REQ-13).
- **DES-1C.** `vc-edge5x0-dsa` creates a device at MDIO address 0 on each igb
  MDIO bus and hands both Marvell 88E6176 switches to the mainline `mv88e6xxx`
  driver without a device tree, with user ports named `lan1` to `lan8` per the
  jack map.

**DES-3.** The switch CPU-link bring-up (SerDes page 1 reset, PHYDET clear, port
4 forcing) runs after the DSA driver binds, because the mainline driver resets
the chip and does not set up the port 4 SerDes.

**DES-4.** Kernel C files from velo540openwrt (GPL-2.0 per-file headers, derived
from VeloCloud's GPL code) are lifted with provenance. Its shell scripts carry no
licence and are not copied: they are reimplemented from the documented register
sequences. Its patch 210 is not lifted; its fix (drive MDIO low before releasing
it) is reimplemented in the board module.

**DES-5.** The fan is a userspace systemd service with open-loop PWM. It reads
the chip product ID first and writes nothing unless it reads 0x1d; sets open-loop
PWM by the recipe 0x50=0x00, 0x42=0x10, 0x2b=0x0c, 0x41=0x01 (duty in register
0x40); sets PCA9557 0x1c pin 6 low and pin 7 high by read-modify-write preserving
bits 0 and 1; holds the fan off at or below the off threshold; never sets a
non-zero duty below the stall-free duty (about 2700 RPM); and attempts 0xff on
exit by an `ExecStopPost` step so it also runs after a crash or kill. Fail-safe
order: the chip has no running watchdog (only a power-up one), so full duty is
written by a shutdown hook, by a unit at early boot before the fan service, and
on every control failure. The service is `Type=notify` with `WatchdogSec=30`.
From 0.5.0 the duty written is the greater of the CPU policy and DES-9.

**DES-9 (0.5.0).** On each poll the fan service reads every drive exposed by the kernel
`drivetemp` hwmon driver (loaded at boot), runs `smartctl` only when drivetemp
exposes no drive at all, takes the highest temperature, and computes drive demand
by the bands of REQ-14. It writes max(CPU demand, drive demand) through the
existing duty path, so the product-ID precondition, stall detection, duty
re-assert and every fail-safe of DES-5 apply unchanged. A drive that reports
nothing is not an input; a read error or vanished drive drops that drive and
logs. Consequence: on a drive that settles at 58 to 59 C under the minimum duty,
drive demand can stay on, so the fan may run continuously at about 27 % instead
of being off at idle.

**HW-FAN.** The 540 fan is an EMC2104 at I2C 0x2f (product ID register 0xfd reads
0x1d); the 520 has no fan. Recipe as in DES-5; duty is register 0x40 and 0xff is
full. PCA9557 at 0x1c pin 6 low and pin 7 high is the only combination where the
fan follows PWM. The stock fan stalls below about 2700 RPM. Mainline `emc2103`
binds only product IDs 0x24 and 0x26, so control is in userspace. The chip has a
power-up watchdog only: after power-up, if 4 s pass without the host programming
it, the fan goes to full until the Fan Setting register is written.

**HW-GPIO.** The SoC GPIO base address is in PCI config register 0x48 of
00:1f.0. Two banks: SC (31 pins, offset +0x00) and SUS (28 pins, +0x80); each has
USE_SEL (+0x00), IO_SEL (+0x04, 1 = input) and GP_LVL (+0x08). Output levels
cannot be read back, so a driver keeps a software copy; Linux `gpio-ich` ORs
that copy into reads even after a pin becomes an input, which corrupts
bit-banged MDIO reads, so the line is driven low before it is released.

**HW-JACKS.** Jack map: LAN1=B1, LAN2=B0, LAN3=B3, LAN4=B2, LAN5=A2, LAN6=A0,
LAN7=A1, LAN8=A3, where A is the switch on 00:14.0, B the switch on 00:14.1 and
the digit is the switch port 0 to 3. Switch port 4 is the CPU port on each
switch; ports 5 and 6 are unused cross-links and stay disabled.

**HW-PCA18.** The PCA9557 at 0x18 holds active-low reset lines: bit 0 PCIe reset
A, bit 1 PCIe reset B, bit 2 switch A reset, bit 3 switch B reset, bit 4 reset of
both 88E1514 WAN PHYs, bits 5 and 6 RF kill, bit 7 USB power. Pulsing bit 4 while
igb is bound hangs the transmitter (REQ-7C).

**HW-SWITCH.** Two Marvell 88E6176 switches (ID 0x1761), chip A on function 0's
MDIO and chip B on function 1's, single-chip addressing. The mainline
`mv88e6xxx` driver resets the chip and does not set up the port 4 SerDes, so
the CPU-link sequence is run again after it binds. Port 4 register 1 takes 0x000E
(the default here) or 0x20FE; both were observed to work.

## Open items referenced in the code

- **OPEN-2.** Which value goes in switch port 4 register 1: both 0x000E and
  0x20FE work; 0x000E is the default.
- **OPEN-11.** Fan numbers not fixed by any source: the stall-free duty (24 on the
  reference unit, whose fan turns at duty 12 and stops at duty 8, re-measure with
  `velo5x0-fan sweep` for another fan), the duty curve between 60 C and 80 C, the
  hysteresis detail and the polling interval.
- **OPEN-28.** How per-jack interfaces are provided when Debian's kernel has no
  DSA (`CONFIG_NET_DSA` unset). Chosen: rebuild Debian's own kernel with DSA
  (`kernel-build/`); the alternatives were per-port VLANs on the stock kernel and
  an out-of-tree tagger.

## Tests referenced in the code

- **TEST-4.** Two jacks in different bridges are isolated: no packet arrives at
  the other peer, and ports 5 and 6 of both switches are disabled (needs cabling;
  `tests/onbox/`).
- **TEST-14 (0.5.0).** The drive input in every band, with hysteresis, with no drive, and
  with a drive read failure (`tests/test_fan_drive.py`). Stepping the drive
  through 50, 55, 57 (from off), 60, 62.5, 65, 66, 67.5, 70 and 75 C gives duties
  0, 0, 0, 69, 69, 69, 106, 162, 255 and 255; 69 is held while falling from 62 C to
  57 C; the duty is 0 at 55 C with the CPU cool; a drive read failure writes no
  0xff and raises no alert; a CPU sensor failure still writes 0xff.
- **TEST-17 (0.5.0).** The drive input is installed and active by default and picks up a
  drive added later (the package carries the module-load entry; the second half
  needs a drive to be fitted and is not run automatically).
