# velo5x0-bsp changelog

## 0.4.1 (2026-11-24)

First public release. 0.4.0 was not published; this is 0.4.0 (which ran on one
VeloCloud Edge 540 with the DSA kernel `6.12.118+velo5x0dsa1`) plus:

- `velo5x0-check`: the DSA CPU-link check is judged by the port 4 status bit 11
  while the conduit is up, and reported NOT RUN otherwise.
- `tests/onbox/dsa-onbox-test.sh`: on-box DSA port tests (network and bridge
  phases only; no I2C, no fan).
- Public repository layout: `LICENSE`, `docs/REQUIREMENTS.md` (what the
  `VBSP-...` ids in the code mean), `docs/TECHNICAL.md`, CI workflow.

Contents: patched igb and board/switch/DSA modules by DKMS, `ge1`/`ge2` names,
load-order and initramfs plumbing, the Edge 540 fan service with its
fail-safes (CPU temperature only), the kernel-update guard, and the DSA kernel
build script.
