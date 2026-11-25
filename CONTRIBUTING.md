# Contributing

Issues and pull requests are welcome, especially reports from other VeloCloud
Edge 5x0 units (520 or 540, any board version) and other Debian 6.12 kernels.

- Open an issue first for anything bigger than a small fix.
- Run the tests before sending a change: `python3 -B -m pytest -q tests`
  (and `sh -n` on any shell script you touch). To also check the copied
  upstream text, clone velo540openwrt at the commit in `PROVENANCE.md` and set
  `VELO5X0_UPSTREAM=<clone>`.
- Keep the safety rules in the README: no change may pulse bit 4 of the PCA9557
  at 0x18 while igb is bound, touch bits 0/1 of the PCA9557 at 0x1c, or drive
  SUS2 low (`docs/REQUIREMENTS.md`, REQ-7). The fan service must keep every
  full-duty fail-safe.
- Do not put site details in a change or an issue attachment: no IP or MAC
  addresses, host names, serial numbers or keys (`tools/velo5x0-leakscan` checks
  a tree for them).
- Files taken from other projects must be recorded in `PROVENANCE.md` with
  upstream path, commit and licence. Everything here is GPL-2.0.
- Hardware reports: say what unit and board version, the kernel
  (`uname -r`), and attach `sudo velo5x0-check` output.

Development is public: from 0.4.1 on, changes are made in the open on `main`, and issues and pull requests are the way to take part.
