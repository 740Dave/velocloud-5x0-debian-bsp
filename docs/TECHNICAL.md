# velo5x0-bsp: VeloCloud Edge 5x0 board support for Debian 13

Board support that makes the copper ports of a VeloCloud Edge 540 (board
version 2.x) work on Debian 13 with a kernel of the 6.12 series, keeps
them working across reboots and kernel updates by DKMS, and keeps the 540 fan
off unless the CPU needs it.  On a stock Debian kernel the eight LAN jacks
run as two unmanaged 4-port switches; on the velo5x0-bsp kernel (Debian's
own kernel rebuilt with DSA, `+velo5x0dsa`, see "The DSA kernel") each jack
is its own interface, `lan1`..`lan8`. The requirement ids used below and in the code (R1, R2, ... and `VBSP-...`) are explained in
`docs/REQUIREMENTS.md`.

The 520 is within the intent but has not been checked on a 520.

## What is in the package

| Part | Package | What it does | Spec |
|---|---|---|---|
| `vc-edge5x0-board` (DKMS) | velo5x0-bsp-dkms | On a supported board only: switches SoC GPIO SC11-SC14 to GPIO, registers a bit-banged I2C bus (SC11/SC12) and a bit-banged MDIO bus `vc-edge5x0-wan` (SC14/SC13), and releases the switch and WAN PHY resets (PCA9557 at 0x18) before igb binds | D1a, R6, R7, R12 |
| `igb` (DKMS, `igb-vc`) | velo5x0-bsp-dkms | Debian's 6.12 igb with velo540openwrt patch 200: I354 functions 0/1 get a forced 1000/full SerDes link and export their MDIO as `igb-vc-0000:00:14.{0,1}`; functions 2/3 reach their PHY over `vc-edge5x0-wan`. Every other igb device (the I350 at 04:00.x) behaves as stock | D1b, R1, R2, R13 |
| `vc-edge5x0-switch` (DKMS) | velo5x0-bsp-dkms | On a stock kernel (`mode=auto` resolves to `unmanaged`): brings up both 88E6176 CPU links (SerDes, PHYDetect, port 4 forcing) and runs each switch as a plain 4-port switch: ports 0-4 forwarding, inter-chip ports 5/6 disabled. On the DSA kernel (`auto` resolves to `dsa`) it touches nothing | D3, R4 (ports 5/6) |
| `vc-edge5x0-dsa` (DKMS, DSA kernel only) | velo5x0-bsp-dkms | Hands each 88E6176 to the mainline `mv88e6xxx` DSA driver by platform data (no device tree): user ports `lan1`..`lan8` per the jack map, CPU port 4, ports 5/6 left out (DSA disables them); after the driver has bound and reset a chip it brings the CPU link up again. `dkms.conf` has `BUILD_EXCLUSIVE_CONFIG=CONFIG_NET_DSA_MV88E6XXX`, so DKMS skips it on stock kernels | D1c, D3, R3, R4 |
| `10-velo5x0-ge{1,2}.link` | velo5x0-bsp | Names 00:14.2 `ge1` and 00:14.3 `ge2`, matched by PCI path | R2 |
| `modprobe.d/velo5x0-bsp.conf` | velo5x0-bsp | `softdep igb pre: vc-edge5x0-board post: vc-edge5x0-switch vc-edge5x0-dsa` (a soft dependency that does not exist for the running kernel is ignored by kmod) | R6 |
| initramfs hook | velo5x0-bsp | Same modules and ordering inside the initramfs, plus `vc-edge5x0-dsa`, `mv88e6xxx` and `tag_dsa` on a kernel that has them | R6 |
| `kernel-build/` (source only, not in a .deb yet) | — | `velo5x0-kernel-build`: Debian kernel version in, `+velo5x0dsa` kernel .debs out | D1d, R5 |
| `velo5x0-kernel-guard` and its hooks, `GRUB_DEFAULT=saved` | velo5x0-bsp | Never lets a kernel whose board modules are not installed be the GRUB default; fails a real build failure loudly | R5 |
| `velo5x0-fan`, `velo5x0-fand.service`, `velo5x0-fan-failsafe.service`, shutdown hook | velo5x0-bsp | EMC2104 fan control: off when cool, never between off and the stall-free duty, full duty on stop, crash, kill, sensor error, at early boot and at shutdown | R10, D5 |
| `velo5x0-check`, `velo5x0-evidence`, `velo5x0-leakscan` | velo5x0-bsp / source | Read-only acceptance checks; evidence capture; site-inventory scan | R1, R2, R13, R11 |

## Port map

| Interface | Port | Through |
|---|---|---|
| `ge1` | GE1 | I354 function 2, 88E1514 PHY 0 |
| `ge2` | GE2 | I354 function 3, 88E1514 PHY 1 |
| `lan1`..`lan8` (DSA kernel) | LAN1-LAN8 | `lanN` is front jack LANN |
| conduit of 00:14.0 (`enp0s20f0` by default) | LAN5-LAN8 | switch A (stock kernel: the four jacks behind it; DSA kernel: carries the DSA-tagged traffic of lan5-lan8, not used directly) |
| conduit of 00:14.1 (`enp0s20f1` by default) | LAN1-LAN4 | switch B (likewise for lan1-lan4) |

Jack map: LAN1=B1, LAN2=B0, LAN3=B3, LAN4=B2, LAN5=A2, LAN6=A0, LAN7=A1, LAN8=A3
(switch A = 00:14.0, switch B = 00:14.1; the letter is the switch, the digit
its port).

## The DSA kernel (per-jack `lan1`..`lan8`)

The Debian 13 amd64 kernel is built with `CONFIG_NET_DSA` unset, and with it
unset `struct net_device` has no `dsa_ptr` member and `eth_type_trans()`
never hands frames to a DSA tagger, so DSA cannot be added to that kernel as
modules.  The project chose (VBSP-OPEN-28, option 1; see docs/REQUIREMENTS.md) to rebuild Debian's
own kernel with DSA and ship it as a package.  `kernel-build/` does that:

- **Source:** Debian's `linux-source-6.12` of the given version (apt-verified),
  plus `kernel-build/patches/230-dsa-pdata-own-tree.patch` (velo540openwrt):
  DSA puts every switch described by platform data into tree 0, so the second
  88E6176 fails to register; the patch gives each such switch the lowest free
  tree.  This is the only source change.
- **Config:** Debian's `config.amd64_none_amd64` from `linux-config-6.12` of
  the same version, plus `kernel-build/config/velo5x0-dsa.config`:
  `NET_DSA`, `NET_DSA_MV88E6XXX`, `NET_DSA_TAG_DSA`, `NET_DSA_TAG_EDSA` as
  modules, and `SYSTEM_TRUSTED_KEYS=""` (Debian's key bundle is not in
  linux-source; the build signs its modules with a key generated for that
  build).  `olddefconfig` then adds the dependent DSA symbols (all off
  except `NET_DSA_TAG_DSA_COMMON=m`) and `MODULE_SIG_ALL=y`.  The script
  fails if any symbol Debian had set changes, or any new symbol outside
  `NET_DSA*`/`MODULE_SIG_ALL` turns on, and writes the full delta to the
  build manifest.  Compiler: `x86_64-linux-gnu-gcc-14`, as Debian.  Debug
  info and BTF are kept as Debian has them.
- **Names:** kernel release `<upstream>+velo5x0dsa<N>` (for example
  `6.12.118+velo5x0dsa1`), packages `linux-image-<release>`,
  `linux-headers-<release>` and `linux-image-<release>-dbg`, version
  `<debian-version>+velo5x0dsa<N>`.  No name collides with a Debian package.
  The build also makes a `linux-libc-dev` .deb: never install it (it would
  replace Debian's).
- **Not signed by Debian:** fine on the 5x0, which boots by BIOS without
  Secure Boot.

### Why a DSA-core patch and not board glue alone (VBSP-OPEN-28)

Registering the two chips as two separate trees from the board glue, without
touching the DSA core, was checked against the 6.12.118 source and is not
possible:

- Platform data (`struct dsa_chip_data`) has no tree or switch index.
  `dsa_switch_parse()` hard-codes `ds->index = 0` and `dsa_tree_touch(0)`,
  and `dsa_tree_add_switch()` refuses a second index 0 in tree 0 (-EEXIST).
- The only interface that does carry a tree index is the device-tree property
  `dsa,member`, read with `of_*` calls.  Debian amd64 has `CONFIG_OF` unset,
  and building the switch, its ports and the conduit link as runtime OF nodes
  would need more config changes than DSA itself.
- Renumbering a tree from the glue after the first switch is set up
  (`dsa_switch_find()` is exported) would give both switches the same devlink
  switch id (taken from the tree index at setup), so a bridge would treat
  ports on the two chips as one hardware domain and stop forwarding between
  them in software.

So patch 230 (six changed lines in `net/dsa/dsa.c`) is carried in the
kernel build; it applies to Debian 6.12.118-1 without offset or fuzz.

### Building a kernel for a new Debian 6.12 update

When Debian publishes a new 6.12 kernel (for example `6.12.119-1`):

```
# on any Docker host (arm64 cross-compiles); about 31 GB in the volume
# velo5x0-kbuild (the tree 30 GB, the -dbg .deb 1 GB); measured 42 min for
# compile and packaging on 4 cores of an Apple M4 (6.12.118-1, cross)
kernel-build/velo5x0-kernel-build-docker 6.12.119-1 1 ./out
# or on a Debian 13 machine, as root for the build dependencies:
kernel-build/velo5x0-kernel-build --debian-version 6.12.119-1 --rev 1 \
    --install-deps --workdir /srv/kbuild --outdir ./out
```

Raise `--rev` (and so the `+velo5x0dsa<N>` suffix) only to rebuild the same
Debian version with a different patch or config.  A version that has left the
Debian archive can still be built with `--snapshot <timestamp>`
(snapshot.debian.org, signature-checked).  A patch that no longer applies
stops the build with a message; nothing is ever deleted by the script (it
refuses to reuse an existing tree).

After an interrupted or failed run, `--resume` reuses the tree (re-checks
its config, skips the compile if the .debs are there).  On a non-amd64 build
host the headers package from `make bindeb-pkg` holds host-architecture
`objtool` and `resolve_btfids`, which DKMS would then fail to run on the box;
the script rebuilds both for x86-64 and repacks the headers .deb (this needs
the host to run amd64 binaries, which Docker Desktop and qemu-user-static
provide).  It also adds `.config` to the headers package, which bindeb-pkg
leaves out and DKMS reads for `BUILD_EXCLUSIVE_CONFIG`.

Output: the .debs and `BUILD-MANIFEST-<release>.txt` (source and config
package sha256, patch sha256, the config delta, the .deb sha256s).  The script
checks that the image carries `dsa_core`, `mv88e6xxx` and `tag_dsa`, that the
headers package has `CONFIG_NET_DSA=m` and that every binary in it is x86-64
(the box runs them when DKMS builds).

### Installing a DSA kernel

Install the headers and the image **in one dpkg call** (the headers package
that `make bindeb-pkg` makes has no maintainer scripts, so
`/etc/kernel/header_postinst.d` does not run for it; DKMS builds from the
image's postinst, which needs the headers already unpacked):

```
sudo dpkg -i linux-headers-<release>_*.deb linux-image-<release>_*.deb
```

The image postinst runs DKMS (igb, board, switch and the DSA glue), the
guard (which checks every module), the initramfs and update-grub; the guard
then makes it the GRUB default.  If headers and image were installed
separately, run `sudo dkms autoinstall -k <release>` and
`sudo /usr/lib/velo5x0-bsp/velo5x0-kernel-guard pin`.

### Switching modes

- DSA kernel, default: `lan1`..`lan8`.  `vc-edge5x0-switch` does nothing
  (`mode=auto` resolves to `dsa`).
- DSA kernel, unmanaged instead: `options vc-edge5x0-dsa dsa_mask=0` and
  `options vc-edge5x0-switch mode=unmanaged` in a modprobe.d file, then
  `update-initramfs -u` and reboot.
- Stock kernel: unmanaged only.

`lanN` interfaces come up administratively down and nothing here gives them
addresses.  A bridge of several `lanN` is offloaded to the switch when all
its ports are on the same chip (LAN1-4, or LAN5-8); across the two chips the
CPU forwards.  Never bridge a `lanN` with GE1/GE2 or a link that also reaches
the same LAN, or you make a loop.

## Install

```
sudo apt install dkms linux-headers-amd64
sudo dpkg -i velo5x0-bsp-dkms_<ver>_all.deb velo5x0-bsp_<ver>_all.deb
# then, for lan1..lan8, a DSA kernel (see "Installing a DSA kernel")
```

The dkms package builds the modules for every installed kernel that has
headers, and fails if the running kernel's build fails. The main package
enables the fan units (they run only on an EDGE540) and rebuilds the
initramfs. Nothing is loaded until the next boot.

### Loading without a reboot

The stock igb has already failed on 00:14.x, so it can be swapped live. This
takes the I350 interfaces (04:00.x) down for a moment:

```
sudo modprobe vc-edge5x0-board      # buses + reset release (check dmesg)
sudo rmmod igb && sudo modprobe igb # loads the DKMS igb; softdep loads vc-edge5x0-switch
                                    # (and vc-edge5x0-dsa on the DSA kernel) after it
sudo velo5x0-check
```

The board module refuses to pulse the resets while any driver is bound to
00:14.x, so loading it after the DKMS igb is safe but leaves the resets as
they are.

New interfaces come up administratively down and nothing in this package
configures addresses. Before bringing one up on a network with router
advertisements, set `net.ipv6.conf.<if>.accept_ra=0` if no IPv6 route should
appear.

## Fan

`/etc/velo5x0-bsp/fan.conf` holds the thresholds. Temperatures are the hottest
coretemp reading (the Atom C2000 has per-core sensors, no package sensor).

- At or below `off_c` (50 C) the fan is off; from off, it starts at `on_c`
  (60 C) with a short kick, then runs at least `stall_free_duty`; at `full_c`
  (80 C) and above it runs at full duty. The thresholds are the project's
  proposed defaults and are not yet validated on every fan.
- `stall_free_duty` 24: measured on one 540 with its stock fan, which turns at
  duty 12 (about 3250 rpm) and stops at 8. Re-measure with
  `sudo velo5x0-fan sweep` for another fan.
- Fail-safes: full duty at early boot (`velo5x0-fan-failsafe.service`), when
  the service stops, crashes, is killed or hangs (`ExecStopPost`; the service
  is `Type=notify` with `WatchdogSec=30`), on a temperature read error, when
  the fail-safe unit stops at shutdown, and at the very end of shutdown
  (`/usr/lib/systemd/system-shutdown/velo5x0-fan-full`).
- `velo5x0-fan full` reads no configuration. It identifies the chip, writes
  0xff, waits out the spin-up time (register 0x46, 500 ms by default) and
  checks by read-back that 0x40 reads 0xff (retrying 3 times). It then sets
  the 0x1c fan pins (bit 6 low, bit 7 high) by read-modify-write.
- Register 0x40 shows the live drive, not the value written (datasheet rev
  1.78, section 6.20). After a write from 00h the Spin Up Routine runs
  (sections 5.7 and 6.25): 0xff for a quarter of the spin-up time, then the spin
  level 0x99 (60 %), then the setting. Measured on a 540: 0xff at 5 ms, 0x99
  from about 126 ms, 0xff from about 497 ms. No read-back is judged inside
  that window.
- A fan that still reads stopped after 3 kicks raises the alert once and is
  latched at full duty until the service restarts, whatever the temperature.
  Starting below full again needs a person to look at the fan.
- A full-duty write that cannot be verified raises the alert once and is
  retried on every poll until it verifies.
- At start the product-ID read is retried (5 tries over about 8 s) before the
  service gives up with exit 2. The fail-safe tries 3 times.
- Nothing is written unless the chip at 0x2f returns product ID 0x1d.
- Alert (the channel is not decided yet): a CRIT journal line, the file
  `/run/velo5x0-fan/ALERT`, and a failed unit.
- Drive temperature input (v0.5.0, VBSP-REQ-14): the duty written is the
  greater of the CPU demand and the drive demand. Hottest reporting drive at or
  below `drive_off_c` (55 C): no drive demand; from `drive_on_c` (60 C): duty
  `drive_min_duty` (69 of 255, 27 %); 65 C to 70 C linear up to full; 70 C and
  above full. Once on it stays at least 69 until the drive is at or below 55 C.
  The temperatures come from the kernel `drivetemp` driver (loaded at boot by
  `/etc/modules-load.d/velo5x0-drivetemp.conf`, installed by the package only
  on a kernel that has the module); `smartctl` is the fallback only when
  drivetemp exposes no drive (at most once a minute, so a drive found only by
  the fallback shows up within about a minute, not within one poll). Drives are
  read on a separate thread every 10 s, which never delays the CPU control
  loop; a drive whose read hangs for 30 s keeps its last drive demand until a
  fresh reading arrives or the device disappears. Drives are
  rediscovered every poll. A drive that reports no temperature (the onboard
  flash) is ignored; with no reporting drive the fan acts exactly as before and
  logs "no drive temperature input" once. A drive read failure or a vanished
  drive is logged and dropped, and never forces full duty. The state file
  carries `drive_temp_c`, `drive_name`, `drive_demand_active` and `drives`.
  On a 520 the fan service is not enabled for now (the 520 has a fan, but its
  controller has not been identified or verified on hardware; contributions
  welcome), so this does nothing.
- `sudo velo5x0-fan probe` is a read-only dump; the service's state is the
  file `/run/velo5x0-fan/state.json`.
- Upgrading from 0.4 with an edited `/etc/velo5x0-bsp/fan.conf`: the shipped
  file gained the `drive_*` keys, so dpkg asks about the conffile. Keep your
  file (`--force-confold` for unattended upgrades); the daemon fills in the
  missing keys, and a `stall_free_duty` above 68 simply raises the drive
  minimum to just above it (a warning is logged).

## igb and the external MDIO bus

The GE1/GE2 PHYs sit on the bit-banged bus `vc-edge5x0-wan`, and `mdiobus_read`
and `mdiobus_write` take a mutex, so a PHY access must never come from atomic
context. Patch 201 makes sure of it:

- igb's PHY-info refresh, which stock igb runs from a timer (softirq), runs
  from a work item; `igb_down` and `igb_remove` cancel it.
- The external-MDIO accessors refuse atomic callers with
  `WARN_ONCE(..., "igb-vc: ext MDIO from atomic ctx")` and an error, rather
  than sleep.
- An audit of every other PHY access in igb (watchdog task, reset, ethtool,
  SIOCGMIIREG, IRQ handlers, the stats and other spinlock regions) found no
  other atomic caller. `tests/test_static.py` re-checks this on the patched
  source.

## Hard rules the code keeps (R7)

- SoC GPIO SUS2 is never driven: the board module writes only the SC bank,
  and only the four bus pins (`vc_sc_update()` refuses anything else).
- PCA9557 at 0x1c bits 0 and 1 (PoE) are never changed: the fan tool's only
  write path to 0x1c is a read-modify-write of bits 6/7 of the output and
  configuration registers; polarity is never written.
- PCA9557 at 0x18 bit 4 is never pulsed under a bound igb: the only write path
  is the one reset sequence at board-module load, under the device locks of
  00:14.0-3, and it refuses if any of them has a driver.

`tests/test_static.py` checks these statically; `tests/test_fan.py` checks the
fan logic against a fake I2C bus.

## Kernel updates (R5)

A kernel whose board modules are not installed is never the boot default.
This is enforced by pinning rather than by stopping update-grub:

- `/etc/default/grub.d/velo5x0-bsp.cfg` sets `GRUB_DEFAULT=saved`.
- `velo5x0-kernel-guard pin` sets grubenv's `saved_entry` to the newest
  verified velo5x0-bsp DSA kernel (`+velo5x0dsa<N>`) with a GRUB entry, or,
  if there is none, the newest verified 6.12 kernel of the generic `-amd64`
  flavour (never `-rt` or `-cloud`) with a GRUB entry. Verified means the
  board modules (igb, vc-edge5x0-board, vc-edge5x0-switch, and on a DSA
  kernel vc-edge5x0-dsa) are in `updates/dkms`, a DSA kernel also has its
  in-tree `dsa_core`, `mv88e6xxx` and `tag_dsa`, and its vmlinuz and initrd
  are in /boot. When
  the pinned kernel is removed, the postrm hook re-pins to the next one. If no
  verified kernel exists, it logs at crit and leaves `saved_entry` alone, saying
  whether that entry is stale (if stale, GRUB falls back to its first entry).
- Removing the package deletes the grub.d file, clears `saved_entry` and runs
  `update-grub`. The kernel hooks it leaves behind do nothing once the guard
  is gone. It runs after every
  `update-grub` of a kernel install or removal (`zz-velo5x0-pin`), after a
  headers install, and from the package postinst.
- An unverified kernel may appear in grub.cfg, but it is not the default.
  One-shot `grub-reboot` still works.

| Case | What happens |
|---|---|
| Kernel and headers install together, modules build | dkms builds, the guard sees them, `zz-velo5x0-pin` makes the new kernel the default |
| `linux-image-X` configures before `linux-headers-X` | The image hook defers (exit 0) and X is not the default. The headers hook (after dkms builds) rebuilds X's initramfs and pins. No manual step. (Debian's packages only; a `+velo5x0dsa` headers package has no hooks: install it with its image, see "Installing a DSA kernel".) |
| A Debian kernel update arrives while a DSA kernel is installed | The new Debian kernel gets its modules and a GRUB entry but is not the default: a verified `+velo5x0dsa` kernel always wins over a stock one, newer or not, because only it has `lan1`..`lan8`. Build and install the matching DSA kernel to move on (see above). |
| Headers present but the build failed | The image hook exits 1: the kernel install fails loudly, and X is not the default. Fix it, then `dpkg --configure -a`. |
| A kernel outside 6.12 | Refused the same way until re-qualified |

The guard logs to `/var/lib/velo5x0-bsp/kernel-guard.log` (on the root file
system: on some installs /var/log is a tmpfs), and failures also go to the
journal at daemon.crit. `velo5x0-kernel-guard status` shows the current choice.

## Evidence

`velo5x0-evidence NAME COMMAND...` runs a command and keeps its command line,
output, exit code, boot id and times in `/var/lib/velo5x0-bsp/evidence/`, so
a reviewer can check test results after reboots.

## Unrequested extras kept, and why

None of these is asked for by a requirement. Each stays because it serves an
R10 fail-safe or the diagnostics that prove a requirement.

| Extra | Why it stays |
|---|---|
| Stall detection, kick and latch (`check_stall`) | R10 requires a running fan never to sit below its stall-free speed. A fan that stays stopped after 3 kicks raises the alert and is latched at full duty until the service restarts. |
| Duty re-assert (`check_duty`) | If the chip resets or someone writes the duty by hand, the policy duty comes back. It acts only after a mismatch has outlasted the spin-up time. |
| `velo5x0-fan sweep` | Measures the stall-free duty (VBSP-OPEN-11) for a new fan. |
| `velo5x0-fan probe` | Read-only diagnostic dump of the chip and the 0x1c expander. |
| State file `/run/velo5x0-fan/state.json` | Read by the watchdog and R10 tests and by `velo5x0-check`. |
| `--kmsg` | The only witness of the final shutdown write when the journal is not persistent: the line reaches the serial console. |
| `velo5x0-check`, `velo5x0-evidence` | Acceptance checks, and evidence files a reviewer can verify after reboots. |
| `velo5x0-kernel-guard status` | Shows which kernel would be pinned and whether `saved_entry` is stale. |

Removed: the `status` subcommand of `velo5x0-fan` (the state file is enough).

## Tests

```
python3 -B -m unittest tests.test_fan tests.test_static   # off-box (-B: no .pyc in the tree)
VELO5X0_UPSTREAM=<velo540openwrt clone> python3 -B -m unittest tests.test_static   # + copied-text check
tools/velo5x0-leakscan .                                   # R11
sudo velo5x0-check                                         # on the box
```

## Provenance

See PROVENANCE.md. Kernel C from velo540openwrt is lifted with its upstream
path, commit and licence; its shell scripts are unlicensed and are not used.
