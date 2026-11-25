#!/bin/sh
# velo5x0-bsp: build the two Debian packages from this source tree.
#   velo5x0-bsp-dkms  DKMS sources: vc-edge5x0 (board + switch), igb-vc and
#                     vc-edge5x0-dsa (built only for a kernel with DSA)
#   velo5x0-bsp       fan tool and units, .link files, modprobe/modules-load
#                     config, initramfs hook, kernel-update guard, docs
# Usage: packaging/build-debs.sh [outdir]      (run on Debian: needs dpkg-deb)
set -eu
umask 022

SRC=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$SRC/../build}
VER=$(cat "$SRC/VERSION")
DEBVER="$VER-1"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/velo5x0-bsp-stage.XXXXXX")
mkdir -p "$OUT"

inst() { # mode src dst
	mkdir -p "$(dirname "$2")"
	install -m "$1" "$3" "$2"
}

# ---------------------------------------------------------------- -dkms
P=$STAGE/dkms
mkdir -p "$P/DEBIAN" "$P/usr/src" "$P/usr/share/doc/velo5x0-bsp-dkms"
cp -R "$SRC/dkms/vc-edge5x0" "$P/usr/src/vc-edge5x0-$VER"
cp -R "$SRC/dkms/igb-vc" "$P/usr/src/igb-vc-$VER"
cp -R "$SRC/dkms/vc-edge5x0-dsa" "$P/usr/src/vc-edge5x0-dsa-$VER"
sed -i "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=\"$VER\"/" \
	"$P/usr/src/vc-edge5x0-$VER/dkms.conf" "$P/usr/src/igb-vc-$VER/dkms.conf" \
	"$P/usr/src/vc-edge5x0-dsa-$VER/dkms.conf"
cp "$SRC/PROVENANCE.md" "$SRC/README.md" "$SRC/docs/TECHNICAL.md" "$SRC/docs/REQUIREMENTS.md" "$P/usr/share/doc/velo5x0-bsp-dkms/"
cp "$SRC/LICENSE" "$P/usr/share/doc/velo5x0-bsp-dkms/copyright"
cp "$SRC/LICENSE" "$P/usr/share/doc/velo5x0-bsp-dkms/copyright"
cat >"$P/DEBIAN/control" <<EOF
Package: velo5x0-bsp-dkms
Version: $DEBVER
Architecture: all
Maintainer: velo5x0-bsp maintainers <noreply@example.invalid>
Depends: dkms (>= 3), linux-headers-amd64, make, gcc
Section: kernel
Priority: optional
Description: VeloCloud Edge 5x0 board support kernel modules (DKMS)
 Board module (bit-banged I2C and WAN PHY MDIO buses, reset release), switch
 CPU-link module, igb with the Edge 5x0 glue, and (for the velo5x0-bsp DSA
 kernel only) the DSA glue that gives one interface per LAN jack, for Debian
 13 kernels of the 6.12 series.
EOF
cat >"$P/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
VER=$VER
case "\$1" in
configure)
	for m in vc-edge5x0 igb-vc vc-edge5x0-dsa; do
		dkms status -m \$m -v \$VER | grep -q . || dkms add -m \$m -v \$VER
	done
	# build and install for every installed kernel that has headers; the
	# running kernel must succeed or the install fails loudly.  Exit 77 is
	# dkms's "skipped by BUILD_EXCLUSIVE" (vc-edge5x0-dsa on a kernel
	# without DSA), not a failure.
	for k in /lib/modules/*; do
		k=\${k##*/}
		[ -e /lib/modules/\$k/build ] || continue
		for m in vc-edge5x0 igb-vc vc-edge5x0-dsa; do
			rc=0
			dkms install -m \$m -v \$VER -k \$k --force || rc=\$?
			[ \$rc = 77 ] && echo "velo5x0-bsp-dkms: \$m not built for \$k (kernel without DSA)" >&2
			if [ \$rc != 0 ] && [ \$rc != 77 ]; then
				echo "velo5x0-bsp-dkms: build of \$m for \$k FAILED (\$rc)" >&2
				[ "\$k" = "\$(uname -r)" ] && exit 1
			fi
		done
	done
	;;
esac
exit 0
EOF
cat >"$P/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
case "\$1" in
remove|upgrade|deconfigure)
	for m in vc-edge5x0 igb-vc vc-edge5x0-dsa; do
		dkms status -m \$m -v $VER | grep -q . && dkms remove -m \$m -v $VER --all || true
	done
	;;
esac
exit 0
EOF
chmod 0755 "$P/DEBIAN/postinst" "$P/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$P" "$OUT/velo5x0-bsp-dkms_${DEBVER}_all.deb"

# --------------------------------------------------------------- main
P=$STAGE/main
mkdir -p "$P/DEBIAN"
inst 0755 "$P/usr/sbin/velo5x0-fan" "$SRC/fan/velo5x0-fan"
inst 0755 "$P/usr/sbin/velo5x0-check" "$SRC/tools/velo5x0-check"
inst 0644 "$P/etc/velo5x0-bsp/fan.conf" "$SRC/fan/fan.conf"
inst 0644 "$P/usr/lib/systemd/system/velo5x0-fan-failsafe.service" "$SRC/systemd/system/velo5x0-fan-failsafe.service"
inst 0644 "$P/usr/lib/systemd/system/velo5x0-fand.service" "$SRC/systemd/system/velo5x0-fand.service"
inst 0755 "$P/usr/lib/systemd/system-shutdown/velo5x0-fan-full" "$SRC/systemd/system-shutdown/velo5x0-fan-full"
inst 0644 "$P/usr/lib/systemd/network/10-velo5x0-ge1.link" "$SRC/systemd/network/10-velo5x0-ge1.link"
inst 0644 "$P/usr/lib/systemd/network/10-velo5x0-ge2.link" "$SRC/systemd/network/10-velo5x0-ge2.link"
inst 0644 "$P/usr/lib/modprobe.d/velo5x0-bsp.conf" "$SRC/modprobe.d/velo5x0-bsp.conf"
inst 0644 "$P/usr/lib/modules-load.d/velo5x0-bsp.conf" "$SRC/modules-load.d/velo5x0-bsp.conf"
inst 0644 "$P/usr/share/velo5x0-bsp/drivetemp-modules-load.conf" "$SRC/modules-load.d/velo5x0-drivetemp.conf"
inst 0755 "$P/usr/share/initramfs-tools/hooks/velo5x0-bsp" "$SRC/initramfs-tools/hooks/velo5x0-bsp"
inst 0755 "$P/usr/lib/velo5x0-bsp/velo5x0-kernel-guard" "$SRC/kernel/velo5x0-kernel-guard"
inst 0755 "$P/etc/kernel/postinst.d/dkms-velo5x0-guard" "$SRC/kernel/postinst.d/dkms-velo5x0-guard"
inst 0755 "$P/etc/kernel/postinst.d/zz-velo5x0-pin" "$SRC/kernel/postinst.d/zz-velo5x0-pin"
inst 0755 "$P/etc/kernel/header_postinst.d/dkms-velo5x0-guard" "$SRC/kernel/header_postinst.d/dkms-velo5x0-guard"
inst 0755 "$P/etc/kernel/postrm.d/zz-velo5x0-pin" "$SRC/kernel/postrm.d/zz-velo5x0-pin"
inst 0644 "$P/usr/share/velo5x0-bsp/grub-default.cfg" "$SRC/grub/velo5x0-bsp.cfg"
inst 0755 "$P/usr/sbin/velo5x0-evidence" "$SRC/tools/velo5x0-evidence"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/README.md" "$SRC/README.md"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/CHANGELOG.md" "$SRC/CHANGELOG.md"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/PROVENANCE.md" "$SRC/PROVENANCE.md"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/TECHNICAL.md" "$SRC/docs/TECHNICAL.md"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/REQUIREMENTS.md" "$SRC/docs/REQUIREMENTS.md"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/copyright" "$SRC/LICENSE"
inst 0644 "$P/usr/share/doc/velo5x0-bsp/copyright" "$SRC/LICENSE"
printf '%s\n' /etc/velo5x0-bsp/fan.conf \
	/etc/kernel/postinst.d/dkms-velo5x0-guard /etc/kernel/postinst.d/zz-velo5x0-pin \
	/etc/kernel/header_postinst.d/dkms-velo5x0-guard /etc/kernel/postrm.d/zz-velo5x0-pin >"$P/DEBIAN/conffiles"
cat >"$P/DEBIAN/control" <<EOF
Package: velo5x0-bsp
Version: $DEBVER
Architecture: all
Maintainer: velo5x0-bsp maintainers <noreply@example.invalid>
Depends: velo5x0-bsp-dkms (= $DEBVER), python3, systemd (>= 254), initramfs-tools, kmod, grub2-common
Suggests: smartmontools
Section: kernel
Priority: optional
Description: VeloCloud Edge 5x0 board support for Debian 13
 GE1/GE2 naming, load ordering, the switch bring-up, the Edge 540 fan
 service with its fail-safes and drive-temperature input (drivetemp,
 smartctl only as a fallback), and a guard that stops a kernel update whose
 board modules did not build.
EOF
cat >"$P/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
case "$1" in
configure)
	depmod -a "$(uname -r)" || true
	systemctl daemon-reload || true
	# the drive-temperature driver: loaded now and at every boot, but only on a
	# kernel that has it (an unknown module in modules-load.d would leave
	# systemd-modules-load failed); a kernel without it only loses the drive input
	if modinfo drivetemp >/dev/null 2>&1; then
		install -m 0644 /usr/share/velo5x0-bsp/drivetemp-modules-load.conf /etc/modules-load.d/velo5x0-drivetemp.conf
		modprobe drivetemp 2>/dev/null || echo "velo5x0-bsp: drivetemp not loadable; no drive temperature input" >&2
	else
		rm -f /etc/modules-load.d/velo5x0-drivetemp.conf
		echo "velo5x0-bsp: this kernel has no drivetemp module; no drive temperature input" >&2
	fi
	# enabled, not started: the services only run on an EDGE540
	# (ConditionFirmware) and take effect from the next boot
	systemctl enable velo5x0-fan-failsafe.service velo5x0-fand.service || true
	mkdir -p /var/lib/velo5x0-bsp/evidence
	update-initramfs -u -k "$(uname -r)"
	# GRUB_DEFAULT=saved (/etc/default/grub.d/velo5x0-bsp.cfg, installed here
	# rather than as a conffile so that removal takes it away), then pin the
	# default to the newest kernel whose board modules are verified
	mkdir -p /etc/default/grub.d
	install -m 0644 /usr/share/velo5x0-bsp/grub-default.cfg /etc/default/grub.d/velo5x0-bsp.cfg
	update-grub
	/usr/lib/velo5x0-bsp/velo5x0-kernel-guard pin
	echo "velo5x0-bsp: installed. Reboot, or see docs/TECHNICAL.md 'Loading without a reboot'." >&2
	;;
esac
exit 0
EOF
cat >"$P/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
remove)
	systemctl disable --now velo5x0-fand.service velo5x0-fan-failsafe.service || true
	rm -f /etc/modules-load.d/velo5x0-drivetemp.conf
	# give GRUB its own default back: no GRUB_DEFAULT=saved, no saved_entry
	rm -f /etc/default/grub.d/velo5x0-bsp.cfg
	grub-editenv - unset saved_entry || true
	update-grub || true
	;;
esac
exit 0
EOF
chmod 0755 "$P/DEBIAN/postinst" "$P/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$P" "$OUT/velo5x0-bsp_${DEBVER}_all.deb"

echo "staging left in $STAGE (not deleted)"
ls -l "$OUT"
