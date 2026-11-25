# velocloud-5x0-debian-bsp

Debian 13 ("trixie") board support for the **VeloCloud Edge 520 and 540**
(Intel Atom C2000 "Rangeley"/Avoton boards, board version 2.x): all the copper
ports, the fan, and the kernel-update plumbing needed to keep them working.
The Debian packages are called `velo5x0-bsp` and `velo5x0-bsp-dkms`.

- **GE1 / GE2** (Intel I354 with Marvell 88E1514 PHYs): a patched `igb` driver
  built by DKMS, named `ge1` and `ge2` by PCI path.
- **LAN1 to LAN8** (two Marvell 88E6176 switches): on a stock Debian kernel they
  run as two plain 4-port switches. On a Debian kernel rebuilt with DSA (the
  script is in `kernel-build/`) each jack is its own interface, `lan1` to `lan8`.
- **Fan** (Edge 540 only): a service that keeps the fan off unless the CPU or a
  SATA/mSATA drive needs it, with full-speed fail-safes (service stop, crash,
  sensor error, early boot, shutdown). From 0.5.0 it also takes a drive
  temperature input.
- **Kernel guard**: a kernel update whose board modules did not build never
  becomes the GRUB default, so a box managed over GE1 is not lost.

This is derived from the community work in
[velo540openwrt](https://github.com/gallops-gannets/velo540openwrt) (its kernel
C files are GPL-2.0);
see "Credit" and `PROVENANCE.md`. The project lives in this repository and
is developed in public here.

## Status

Running on one VeloCloud Edge 540 since 2026-11-24 on Debian 13 with kernel
`6.12.118+velo5x0dsa1` (velo5x0-bsp 0.4.x, 0.5.0 since 2026-11-24, 0.5.1 since 2026-11-25). We'll update this section if we hit problems.

Burn-in time is short so far; treat this as early. Reports from other units are welcome.

Versions 0.5.x run on that unit: 0.5.0 (drive-temperature fan input) since 2026-11-24 and
0.5.1 (minimum run time) since 2026-11-25; see the CHANGELOG entries for what was observed.

## Supported hardware

| Unit | Status |
|---|---|
| VeloCloud Edge 540, board version 2.x | Developed and run on one unit |
| VeloCloud Edge 520, board version 2.x | Within the intent (same software buildout), **not checked on a 520**. The 520 has a fan, but fan control here is enabled on the 540 only for now; the 520 fan controller is not yet identified or verified on hardware (contributions welcome) |
| Board version 1.x ("rev A") | Refused: the software touches no hardware and logs why |

The software checks the DMI model and version and the igb NVM signature, and does
nothing on any other board. Debian 13 amd64 with a 6.12-series kernel is the
target; another kernel series needs re-qualification.

## What works, and what needs the DSA kernel

| Feature | Stock Debian 13 kernel | DSA kernel (`+velo5x0dsa`) |
|---|---|---|
| GE1, GE2 at 1000/full, `ge1`/`ge2` names | yes | yes |
| I350 and SFP ports unaffected | yes | yes |
| LAN1 to LAN8 as plain switch ports | yes (two unmanaged 4-port switches) | not used |
| `lan1`..`lan8` as separate interfaces | **no** | yes |
| Fan control and fail-safes (540) | yes | yes |
| Kernel update guard | yes | yes |

Debian's kernel is built without `CONFIG_NET_DSA`, which cannot be added as
modules, so per-jack interfaces need a rebuilt kernel (measured about 42 minutes on four
cores of an Apple M4 in a Debian 13 container). **No kernel binaries are published here**:
build your own with the documented script and review it first.

## Install

```
sudo apt install dkms linux-headers-amd64
# build the packages (below) or take them from a GitHub release
sudo dpkg -i velo5x0-bsp-dkms_<ver>_all.deb velo5x0-bsp_<ver>_all.deb
```

The dkms package builds the modules for every installed kernel with headers and
fails if the running kernel's build fails. Installing the main package also sets
the GRUB default (`GRUB_DEFAULT=saved`, via `/etc/default/grub.d/velo5x0-bsp.cfg`),
runs `update-grub` and pins the default to the newest kernel whose board modules
built; removing the package undoes this. The main package enables the fan units
(they run only on an EDGE540) and rebuilds the initramfs. Nothing is loaded until
the next boot; `docs/TECHNICAL.md` has "Loading without a reboot". After a reboot
run `sudo velo5x0-check` (read-only).

Build the packages (on Debian, needs `dpkg-dev`):

```
sh packaging/build-debs.sh ./build
```

### Building the DSA kernel

```
# on any Docker host; about 31 GB of volume space
kernel-build/velo5x0-kernel-build-docker 6.12.118-1 1 ./out
# or on a Debian 13 machine, as root for the build dependencies:
kernel-build/velo5x0-kernel-build --debian-version 6.12.118-1 --rev 1 \
    --install-deps --workdir /srv/kbuild --outdir ./out
```

It downloads Debian's `linux-source-6.12`, applies one patch (DSA platform-data
tree numbering), enables `NET_DSA`, `NET_DSA_MV88E6XXX` and the DSA taggers as
modules, builds, and writes a manifest with the source and config hashes.
Install the headers and image **in one dpkg call**:

```
sudo dpkg -i linux-headers-<release>_*.deb linux-image-<release>_*.deb
```

Never install the `linux-libc-dev` package it also produces. Details, switching
between DSA and unmanaged modes, and upgrade notes are in `docs/TECHNICAL.md`.

## Drive temperature and disk spin-down

From 0.5.1, each time drive demand turns on (also if the CPU already runs the
fan) the fan holds at least its minimum drive duty for 10 minutes
(`drive_min_run_s` in `/etc/velo5x0-bsp/fan.conf`, 0 to 3600, 0 turns this off).
From 0.5.0 the fan service reads drive temperatures through the kernel
`drivetemp` driver every 10 s. On some spinning disks, reading the temperature
can keep the drive from spinning down (see the kernel's `drivetemp`
documentation); SSDs and mSATA cards are not affected. To opt out, remove
`/etc/modules-load.d/velo5x0-drivetemp.conf` and unload `drivetemp`; the fan then
uses the CPU temperature only (or the `smartctl` fallback, which does not wake a
sleeping drive).

## Safety warnings

This is board-level code. A wrong write can hang a PHY, cut PoE or power-cycle
the machine. The code never does the following, and a change that does is a bug
(`docs/REQUIREMENTS.md`, REQ-7):

- **PCA9557 at I2C 0x18, bit 4 (WAN PHY reset):** never pulse it while `igb` is
  bound to PCI functions 00:14.2 or 00:14.3. It hangs the transmitter. The one
  permitted reset runs once, before igb binds.
- **PCA9557 at I2C 0x1c, bits 0 and 1 (PoE):** do not touch them. Every write to
  that chip is a read-modify-write that preserves them.
- **SoC GPIO SUS2:** must not be driven low (it is the power-cycle line).
- The fan service writes nothing unless the fan chip at 0x2f reads product ID
  0x1d.

Do not run `i2cset` or similar by hand on this board's buses unless you know
which device you are addressing. The software refuses to act on any board other
than a supported 5x0.

## Credit and licence

- Based on [velo540openwrt](https://github.com/gallops-gannets/velo540openwrt),
  the community OpenWrt port for the VeloCloud Edge 540: the `igb` patch 200,
  the DSA platform-data patch 230 and the structure of the board and switch glue
  modules come from there; the register sequences were also informed by
  VeloCloud's GPL source and a FreeBSD port. Thank you to its authors.
- `PROVENANCE.md` lists, file by file, what came from where, under which licence
  and what was changed. The upstream repository has no top-level licence: its
  kernel C files carry per-file GPL-2.0 headers, which is what was reused; its
  shell scripts were **not** copied and are reimplemented.
- The `igb` sources under `dkms/igb-vc/` are Debian's `linux-source-6.12`
  (GPL-2.0), unchanged apart from the patches applied at build time.
- Everything here is GPL-2.0 (`LICENSE`). New files carry
  `SPDX-License-Identifier: GPL-2.0`.

## Documents

- `docs/TECHNICAL.md`: package contents, port map, the DSA kernel, fan details.
- `docs/REQUIREMENTS.md`: what the `VBSP-...` identifiers in the code mean.
- `PROVENANCE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`.

Issues and pull requests are welcome; see `CONTRIBUTING.md`.
