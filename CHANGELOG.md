# velo5x0-bsp changelog

## 0.5.1 (2026-11-25)

Not yet run on hardware. Tested off the box only (fake sensors and a fake fan
chip); the 30-minute check on a 540 is still to be done.

- Fan: a minimum run time. Each time drive demand starts the fan, the fan holds
  at least `drive_min_duty` (69/255) for `drive_min_run_s` seconds (new key in
  `/etc/velo5x0-bsp/fan.conf`, default 600) before drive demand may release it,
  even if the hottest drive falls to the off threshold (55 C) sooner. This
  answers the 0.5.0 observation of 30-second bursts. `drive_min_run_s = 0`
  turns it off; a negative value is refused at start. CPU demand, the CPU
  fail-safes (a fail-safe always wins) and the stall latch are unaffected; a
  drive that vanishes or stops reporting clears the hold. The timer is not
  kept over a daemon restart.
- Upgrading with an edited `fan.conf`: add `drive_min_run_s` if you want a value
  other than 600; a file without it uses 600.

## 0.5.0 (2026-11-24)

Tested on hardware: one Edge 540 (Debian 13, kernel 6.12.118+velo5x0dsa1),
2026-11-24, 30-minute run. The mSATA drive temperature was read via drivetemp;
the fan switched on at 60 C at duty 69/255 (about 3900 rpm) and off at 55 C; the
drive peaked at 60.0 C; no stall latch; the CPU fail-safes were unaffected.
Observed: the fan runs in short bursts (about 30 s on, several minutes off)
because the drive warms slowly; a minimum on-time may come in a later release.

Spec v0.5 (VBSP-REQ-14, 14A to 14E, VBSP-DES-9).

- Fan: the duty written is max(CPU demand, drive demand). Drive demand from the
  hottest drive that reports a temperature: at or below 55 C none; from 60 C
  duty 69/255 (27 %); 65 C to 70 C linear 69 to 255; 70 C and above full. Once
  on, it stays at least 69 until the hottest drive is at or below 55 C.
  Thresholds are `drive_*` keys in `/etc/velo5x0-bsp/fan.conf` (defaults as
  above; `drive_min_duty` must exceed `stall_free_duty`).
- Drive temperatures come from the kernel `drivetemp` hwmon driver; the package
  ships a module-load entry (installed to `/etc/modules-load.d/` by postinst) and loads it in postinst.
  `smartctl` is used only when drivetemp exposes no drive and smartmontools is
  installed (bounded timeouts, at most once a minute). Sensors are rediscovered
  every poll, so a drive added later is used with no restart.
- No temperature-reporting drive: the fan behaves as in 0.4.0, with one info
  line "no drive temperature input". A drive read failure or a drive that
  disappears drops that drive, is logged once, and is not a fail-safe trigger.
  CPU sensor error and every other fail-safe are unchanged.
- State file `/run/velo5x0-fan/state.json` gains `drive_temp_c`, `drive_name`,
  `drive_source`, `drive_demand_active`, `drive_demand_on`, `drive_duty`, `drives`.
  `velo5x0-fan probe` shows `drive_source` and `drives_c`.
- Suggests smartmontools.
- Drive reads run on a separate thread (every 10 s), so a slow or hung drive
  never delays the control loop. A drive whose read hangs (no fresh reading for
  30 s, device still present) keeps its last drive demand until a fresh reading
  arrives or the device disappears (logged once); only an error return or the
  device disappearing drops its demand. smartctl is run with a hard bound on
  our wait and the child is killed on timeout. The drive input is evaluated after the CPU sensor-error branch,
  so the full-duty fail-safe write never waits on it.
- A drive that vanishes from the smartctl fallback (for example in standby) is
  logged at info level, not as a warning.
- A `drive_min_duty` not above `stall_free_duty` is raised to just above it with
  a warning, instead of refusing a valid 0.4 configuration.
- `drivetemp` is loaded at boot only on a kernel that has it (the package
  installs `/etc/modules-load.d/velo5x0-drivetemp.conf` in postinst after
  `modinfo drivetemp` succeeds).
- Log lines: "CPU demand off; drive demand holds duty N" when the CPU drops
  to off while a drive holds the fan; a drive that never reported is logged at
  info level; drive fields stay published in the state file during a CPU
  sensor error.

## 0.4.1

First public release (the former 0.4.0 plus a `velo5x0-check` DSA CPU-link
fix and `tests/onbox/`). See the 0.4.1 tag.

## 0.4.0

0.4.0 was not published; 0.4.1 is the first public release.
