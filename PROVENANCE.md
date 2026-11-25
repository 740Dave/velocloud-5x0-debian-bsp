# velo5x0-bsp provenance (VBSP-REQ-8)

Every file in this package that came from another project is listed here with
its upstream, path, commit or release, licence and what was changed. Files not
listed were written for this package (GPL-2.0, covered by `LICENSE`; the source files carry an SPDX header).

## Lifted from velo540openwrt

- Upstream: https://github.com/gallops-gannets/velo540openwrt
- Commit: `d487e0734e7ad1da70d77ffefb681d0f4e90d317` (2026-11-18)
- Licence: the kernel C files carry per-file `SPDX-License-Identifier: GPL-2.0`
  and say they derive from VeloCloud's GPL code. The repository has no top-level
  licence, so its shell scripts (`files/etc/init.d/velo540-switch`,
  `files/usr/sbin/velo540-fand`, `files/etc/init.d/velo540-fan` and the others)
  are treated as all rights reserved. **No text from them is in this package.**
  The register sequences they carry are reimplemented from the spec's record of
  them (VBSP-HW-SWITCH, VBSP-HW-FAN), which comes from the vendor GPL source and
  FreeBSD.

| Package file | Upstream path | Licence | Changes |
|---|---|---|---|
| `dkms/igb-vc/patches/200-igb-velocloud-edge5x0.patch` | `patches/200-igb-velocloud-edge5x0.patch` | GPL-2.0 | Diff unchanged; a provenance header was prepended (text before the first `diff` line, which `patch` ignores). Upstream sha256 `a3a0af3d0b84477bc99688e2a86eeafb2cd33f26e39a1f7e7492be198ff62b01`. |
| `dkms/vc-edge5x0/vc-edge5x0-board.c` (in part) | `package/velo540-glue/src/vc-edge5x0-mdio.c` | GPL-2.0 | Derived. Kept: the DMI board match, the GPIO base lookup (00:1f.0 config 0x48, mask 0xff80) and the GPIO_USE_SEL write for the bus pins. Replaced: the gpiod lookup tables for mainline `mdio-gpio`, `i2c-gpio` and `leds-pca963x` (none is built in the Debian kernel) with direct bit-banging on the SoC GPIO registers. Added: the PCA9557 reset sequence, the NVM signature check and the rev A refusal (upstream remaps rev A pins instead). |
| `dkms/vc-edge5x0-dsa/vc-edge5x0-dsa.c` | `package/velo540-glue/src/vc-edge5x0-dsa.c` | GPL-2.0 | Derived (0.4.0). Kept: the platform-data recipe (an `mdio_device` at SMI address 0 on each `igb-vc-*` bus, `dsa_mv88e6xxx_pdata` with CPU port 4, conduit = the igb netdev of the same PCI function, compatible `marvell,mv88e6085`), the local copy of `mdio_device_bus_match`, the conduit lookup, the default port-name lists (the jack map). Changed: no platform-driver deferral (a work item polls for igb's buses and netdevs, and re-registers after igb unbinds and returns); the conduit reference is taken per probe attempt from a bus notifier and dropped when a bound switch unbinds, because `mv88e6xxx_probe()` drops it on every failed probe and never on remove (upstream leaks it on unbind and can underflow it on a failed bind); our device is tracked through bus removal (no use after free if igb unbinds first); `dsa_mask` defaults to 3 and is read-only; added the CPU-link bring-up after bind (upstream does it from its unlicensed `velo540-switch` script; reimplemented here from the spec's register sequence, VBSP-HW-SWITCH, with the SerDes reached through the driver's own chip-locked internal MDIO bus) and a read-only `status` file. |
| `kernel-build/patches/230-dsa-pdata-own-tree.patch` | `patches/230-dsa-pdata-own-tree.patch` | GPL-2.0 | Diff unchanged; a provenance header was prepended (text before the `From:` line, which `patch` ignores). Upstream sha256 `a686e23da944ce89954bb8f222aefc1f22646fab0b04125e9e8a53c84b9c7940`. Applied to Debian's `linux-source-6.12` by `velo5x0-kernel-build`; not part of a .deb of this package. |

Not lifted, though the spec's lift list (VBSP-DES-4) names it:
- `patches/210-mdio-gpio-clear-level-before-input.patch`: its fix (drive MDIO low
  before releasing it) is reimplemented in `vc_mdio_drive()` of the board module,
  which does its own bit-banging.

## Lifted from the Linux kernel (Debian)

| Package file | Upstream | Licence | Changes |
|---|---|---|---|
| `dkms/igb-vc/drivers/net/ethernet/intel/igb/*` (21 files) | Debian `linux-source-6.12` 6.12.118-1, `drivers/net/ethernet/intel/igb/` | GPL-2.0 (SPDX header in each file) | None: byte-identical, so they can be checked with `sha256sum -c SOURCE.sha256` run inside Debian's `drivers/net/ethernet/intel/igb/` (`tests/test_static.py` checks this tree against the same list). The per-file sha256 list is `dkms/igb-vc/SOURCE.sha256`. Their headers are the kernel's own; the upstream, release and licence are recorded here and in `dkms/igb-vc/SOURCE` instead of being written into each file, to keep them pristine. |

## Taken from Debian (not copied into this tree)

- The DSA kernel is built from Debian's `linux-source-6.12` and
  `linux-config-6.12` (amd64) of the version given to `velo5x0-kernel-build`
  (first build: 6.12.118-1), downloaded by apt from the Debian archive.  The
  config change is `kernel-build/config/velo5x0-dsa.config` (written for this
  package).  Each build's manifest records the sha256 of both source packages.

## velo5x0-bsp's own change to lifted code

- `dkms/igb-vc/patches/201-velo5x0-bsp-ext-mdio-bus-name.patch` (GPL-2.0): applied
  after patch 200. It changes the default external MDIO bus id from `gpio-0` to
  `vc-edge5x0-wan`. It also makes igb take a module reference on the provider of
  that bus (`try_module_get(bus->owner)`) for as long as it holds the bus, so
  the board module cannot be unloaded under a bound igb. It also:
  - makes the external-MDIO PHY accessors refuse atomic context
    (`WARN_ONCE`, then `-E1000_ERR_PHY`), because the bus is behind a mutex;
  - moves igb's PHY-info refresh from its timer (softirq) to a work item,
    which `igb_down` and `igb_remove` cancel;
  - unregisters the buses after `unregister_netdev`.

## Facts taken from documents (not code)

- EMC2104 register map: SMSC/Microchip EMC2104 datasheet revision 1.78 (02-28-11).
- PCA9557 register map: NXP PCA9557 datasheet (registers 0 input, 1 output,
  2 polarity, 3 configuration).
- Pin roles, bus addresses, reset sequence, jack map, switch register sequences:
  the velo5x0-bsp spec (VBSP-HW-*), which cites the vendor GPL source, FreeBSD
  and velo540openwrt for each.
