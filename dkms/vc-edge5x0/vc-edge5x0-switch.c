// SPDX-License-Identifier: GPL-2.0
/*
 * vc-edge5x0-switch: bring up the two Marvell 88E6176 LAN switches of a
 * VeloCloud Edge 520/540 (board version 2.x) behind I354 functions 0 and 1
 * (velo5x0-bsp, VBSP-DES-3).
 *
 * Written for this package from the register sequences recorded in the
 * velo5x0-bsp spec (VBSP-HW-SWITCH), which come from the vendor GPL igb_vc.c
 * and FreeBSD igb_vc.c.  No text of the (unlicensed) velo540openwrt shell
 * scripts is used; see PROVENANCE.md.
 *
 * Each switch is reached through the MDIO master of its I354 function, which
 * the patched igb exports as mii_bus "igb-vc-0000:00:14.{0,1}" (single-chip
 * addressing: ports at SMI 0x10..0x16, Global1 0x1b, Global2 0x1c).
 *
 * Per switch, at probe:
 *   CPU link (always): port 4 SerDes (Global2 indirect device 0xF, page 1)
 *     BMCR |= reset + full duplex, &= ~(autoneg + power-down); clear
 *     PHYDetect (bit 12) in port 4 register 0; write port 4 register 1 =
 *     p4_ctrl (0x000E by default, 0x20FE the FreeBSD value; VBSP-OPEN-2).
 *   mode=unmanaged: internal PHYs 0-3 page 0 BMCR = 0x9140, ports 0-4
 *     Forwarding, ports 5 and 6 (inter-chip links) Disabled.
 *   mode=cpu-link: only the CPU link.
 *   mode=dsa: touch nothing; vc-edge5x0-dsa hands the switches to the
 *     mv88e6xxx DSA driver, which resets them, and brings the CPU links up
 *     after that.
 *   mode=auto (default): dsa when this module was built for a kernel with
 *     the mv88e6xxx DSA driver (the velo5x0-bsp +velo5x0dsa kernel),
 *     otherwise unmanaged (stock Debian kernel, which has no DSA).
 *
 * It never touches I2C, the PCA9557 expanders or SoC GPIO (VBSP-REQ-7).
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/dmi.h>
#include <linux/delay.h>
#include <linux/phy.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>

#define DRV_NAME		"vc-edge5x0-switch"
#define VC_NUM_SW		2
#define VC_SW_PORT(p)		(0x10 + (p))
#define VC_GLOBAL2		0x1c
#define VC_G2_SMI_CMD		0x18
#define VC_G2_SMI_DATA		0x19
#define VC_SMI_BUSY		BIT(15)
#define VC_SMI_C22		BIT(12)
#define VC_SMI_OP_WRITE		(1 << 10)
#define VC_SMI_OP_READ		(2 << 10)
#define VC_SERDES_DEV		0x0f
#define VC_PAGE_REG		22
#define VC_CPU_PORT		4
#define VC_PORT_STATUS		0
#define VC_PORT_PHYS_CTRL	1
#define VC_PORT_ID		3
#define VC_PORT_CTRL		4
#define VC_PORT_STATE_MASK	0x0003
#define VC_PORT_STATE_FWD	0x0003
#define VC_PS_PHY_DETECT	BIT(12)
#define VC_ID_MASK		0xfff0
#define VC_ID_88E6176		0x1760

static const char * const vc_bus_ids[VC_NUM_SW] = {
	"igb-vc-0000:00:14.0",	/* switch A: LAN6, LAN7, LAN5, LAN8 on ports 0-3 */
	"igb-vc-0000:00:14.1",	/* switch B: LAN2, LAN1, LAN4, LAN3 on ports 0-3 */
};

static ushort p4_ctrl = 0x000e;
module_param(p4_ctrl, ushort, 0444);
MODULE_PARM_DESC(p4_ctrl, "value written to switch port 4 register 1 (0x000e default, 0x20fe FreeBSD)");

static char *mode = "auto";
module_param(mode, charp, 0444);
MODULE_PARM_DESC(mode, "auto (default: dsa on a DSA kernel, else unmanaged), unmanaged (CPU link + ports 0-4 forwarding, 5/6 disabled), cpu-link (CPU link only) or dsa (touch nothing; vc-edge5x0-dsa owns the switches)");
static const char *vc_mode;	/* mode, with auto resolved */

static DEFINE_MUTEX(vc_sw_lock);
static char vc_sw_result[VC_NUM_SW][96];

/*
 * Look up an igb-exported switch bus and pin it: a device reference (from
 * mdio_find_bus) plus a module reference on its owner (igb), so igb cannot be
 * unloaded while this module talks to the bus, and a check that the bus is
 * still registered (igb unbound from the function leaves it unregistered).
 */
static struct mii_bus *vc_get_bus(const char *id)
{
	struct mii_bus *bus = mdio_find_bus(id);

	if (!bus)
		return NULL;
	if (!try_module_get(bus->owner)) {
		put_device(&bus->dev);
		return NULL;
	}
	if (bus->state != MDIOBUS_REGISTERED) {
		module_put(bus->owner);
		put_device(&bus->dev);
		return NULL;
	}
	return bus;
}

static void vc_put_bus(struct mii_bus *bus)
{
	module_put(bus->owner);
	put_device(&bus->dev);
}

static int vc_rd(struct mii_bus *bus, int addr, int reg)
{
	return mdiobus_read(bus, addr, reg);
}

static int vc_wr(struct mii_bus *bus, int addr, int reg, u16 val)
{
	return mdiobus_write(bus, addr, reg, val);
}

static int vc_smi_wait(struct mii_bus *bus)
{
	int i, v;

	for (i = 0; i < 100; i++) {
		v = vc_rd(bus, VC_GLOBAL2, VC_G2_SMI_CMD);
		if (v < 0)
			return v;
		if (!(v & VC_SMI_BUSY))
			return 0;
		usleep_range(100, 200);
	}
	return -ETIMEDOUT;
}

/* Global2 indirect access to an internal PHY or the SerDes (clause 22) */
static int vc_smi_write(struct mii_bus *bus, int dev, int reg, u16 val)
{
	int err = vc_smi_wait(bus);

	if (!err)
		err = vc_wr(bus, VC_GLOBAL2, VC_G2_SMI_DATA, val);
	if (!err)
		err = vc_wr(bus, VC_GLOBAL2, VC_G2_SMI_CMD,
			    VC_SMI_BUSY | VC_SMI_C22 | VC_SMI_OP_WRITE | (dev << 5) | reg);
	if (!err)
		err = vc_smi_wait(bus);
	return err;
}

static int vc_smi_read(struct mii_bus *bus, int dev, int reg)
{
	int err = vc_smi_wait(bus);

	if (!err)
		err = vc_wr(bus, VC_GLOBAL2, VC_G2_SMI_CMD,
			    VC_SMI_BUSY | VC_SMI_C22 | VC_SMI_OP_READ | (dev << 5) | reg);
	if (!err)
		err = vc_smi_wait(bus);
	if (err)
		return err;
	return vc_rd(bus, VC_GLOBAL2, VC_G2_SMI_DATA);
}

static int vc_port_state(struct mii_bus *bus, int port, u16 state)
{
	int c = vc_rd(bus, VC_SW_PORT(port), VC_PORT_CTRL);

	if (c < 0)
		return c;
	return vc_wr(bus, VC_SW_PORT(port), VC_PORT_CTRL,
		     (c & ~VC_PORT_STATE_MASK) | state);
}

static int vc_cpu_link_up(struct mii_bus *bus)
{
	int v, err;

	err = vc_smi_write(bus, VC_SERDES_DEV, VC_PAGE_REG, 1);
	if (err)
		return err;
	v = vc_smi_read(bus, VC_SERDES_DEV, MII_BMCR);
	if (v < 0)
		return v;
	err = vc_smi_write(bus, VC_SERDES_DEV, MII_BMCR,
			   (v | BMCR_RESET | BMCR_FULLDPLX) & ~(BMCR_ANENABLE | BMCR_PDOWN));
	if (err)
		return err;
	msleep(1000);		/* SerDes reset settle, as both known-good sequences wait */

	v = vc_rd(bus, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS);
	if (v < 0)
		return v;
	err = vc_wr(bus, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS, v & ~VC_PS_PHY_DETECT);
	if (err)
		return err;
	return vc_wr(bus, VC_SW_PORT(VC_CPU_PORT), VC_PORT_PHYS_CTRL, p4_ctrl);
}

static int vc_unmanaged(struct mii_bus *bus)
{
	int p, err;

	for (p = 0; p < 4; p++) {
		err = vc_smi_write(bus, p, VC_PAGE_REG, 0);
		if (!err)
			err = vc_smi_write(bus, p, MII_BMCR,
					   BMCR_RESET | BMCR_ANENABLE | BMCR_FULLDPLX | BMCR_SPEED1000);
		if (err)
			return err;
	}
	for (p = 0; p <= VC_CPU_PORT; p++) {
		err = vc_port_state(bus, p, VC_PORT_STATE_FWD);
		if (err)
			return err;
	}
	for (p = 5; p <= 6; p++) {
		err = vc_port_state(bus, p, 0);	/* Disabled */
		if (err)
			return err;
	}
	return 0;
}

static int vc_switch_setup(int i, struct mii_bus *bus)
{
	int id, st, err;

	id = vc_rd(bus, VC_SW_PORT(0), VC_PORT_ID);
	if (id < 0 || (id & VC_ID_MASK) != VC_ID_88E6176) {
		scnprintf(vc_sw_result[i], sizeof(vc_sw_result[i]),
			  "no 88E6176 (id %#x); not configured", id);
		return -ENODEV;
	}
	err = vc_cpu_link_up(bus);
	if (!err && !strcmp(vc_mode, "unmanaged"))
		err = vc_unmanaged(bus);
	st = vc_rd(bus, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS);
	scnprintf(vc_sw_result[i], sizeof(vc_sw_result[i]),
		  "%s: id %#06x mode %s p4_ctrl %#06x port4 status %#06x",
		  err ? "error" : "up", id, vc_mode, p4_ctrl, st);
	return err;
}

static int vc_sw_probe(struct platform_device *pdev)
{
	struct mii_bus *bus[VC_NUM_SW] = { };
	int i, err = 0;

	if (!strcmp(vc_mode, "dsa")) {
		for (i = 0; i < VC_NUM_SW; i++)
			scnprintf(vc_sw_result[i], sizeof(vc_sw_result[i]),
				  "dsa: left to vc-edge5x0-dsa (mode %s)", mode);
		dev_info(&pdev->dev, "mode %s: switches left to vc-edge5x0-dsa and mv88e6xxx\n", mode);
		return 0;
	}
	for (i = 0; i < VC_NUM_SW; i++) {
		bus[i] = vc_get_bus(vc_bus_ids[i]);
		if (!bus[i]) {
			dev_info(&pdev->dev, "%s not registered yet (patched igb not bound), deferring\n",
				 vc_bus_ids[i]);
			err = -EPROBE_DEFER;
			goto out;
		}
	}
	mutex_lock(&vc_sw_lock);
	for (i = 0; i < VC_NUM_SW; i++) {
		vc_switch_setup(i, bus[i]);
		dev_info(&pdev->dev, "%s %s\n", vc_bus_ids[i], vc_sw_result[i]);
	}
	mutex_unlock(&vc_sw_lock);
out:
	for (i = 0; i < VC_NUM_SW; i++)
		if (bus[i])
			vc_put_bus(bus[i]);
	return err;
}

/* read-only register dump for tests (VBSP-TEST-4: ports 5/6 disabled) */
static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mii_bus *bus;
	ssize_t n = 0;
	int i, p;

	mutex_lock(&vc_sw_lock);
	for (i = 0; i < VC_NUM_SW; i++) {
		n += sysfs_emit_at(buf, n, "switch %c %s: %s\n", 'A' + i, vc_bus_ids[i],
				   vc_sw_result[i][0] ? vc_sw_result[i] : "not probed");
		bus = vc_get_bus(vc_bus_ids[i]);
		if (!bus) {
			n += sysfs_emit_at(buf, n, "  bus not available (igb unbound or unloading)\n");
			continue;
		}
		for (p = 0; p <= 6; p++)
			n += sysfs_emit_at(buf, n, "  port %d status %#06x physctrl %#06x ctrl %#06x state %s\n",
					   p, vc_rd(bus, VC_SW_PORT(p), VC_PORT_STATUS),
					   vc_rd(bus, VC_SW_PORT(p), VC_PORT_PHYS_CTRL),
					   vc_rd(bus, VC_SW_PORT(p), VC_PORT_CTRL),
					   (vc_rd(bus, VC_SW_PORT(p), VC_PORT_CTRL) & VC_PORT_STATE_MASK) ==
					   0 ? "disabled" : "enabled");
		vc_put_bus(bus);
	}
	mutex_unlock(&vc_sw_lock);
	return n;
}
static DEVICE_ATTR_RO(status);

static struct attribute *vc_sw_attrs[] = { &dev_attr_status.attr, NULL };
ATTRIBUTE_GROUPS(vc_sw);

static struct platform_driver vc_sw_driver = {
	.probe = vc_sw_probe,
	.driver = {
		.name = DRV_NAME,
		.dev_groups = vc_sw_groups,
		.probe_type = PROBE_PREFER_ASYNCHRONOUS,
	},
};

static struct platform_device *vc_sw_pdev;

static int __init vc_sw_init(void)
{
	const char *board = dmi_get_system_info(DMI_BOARD_NAME);
	const char *ver = dmi_get_system_info(DMI_BOARD_VERSION);
	int err;

	if (!strcmp(mode, "auto"))
		vc_mode = IS_ENABLED(CONFIG_NET_DSA_MV88E6XXX) ? "dsa" : "unmanaged";
	else if (!strcmp(mode, "unmanaged") || !strcmp(mode, "cpu-link") || !strcmp(mode, "dsa"))
		vc_mode = mode;
	else {
		pr_err(DRV_NAME ": unknown mode \"%s\"\n", mode);
		return -EINVAL;
	}

	if (!board || (strcmp(board, "EDGE520") && strcmp(board, "EDGE540")) ||
	    !ver || ver[0] != '2' || ver[1] != '.') {
		pr_info(DRV_NAME ": board \"%s\" version \"%s\" not supported (need EDGE520/EDGE540 2.x); touching no hardware\n",
			board ? board : "(none)", ver ? ver : "(none)");
		return -ENODEV;
	}
	err = platform_driver_register(&vc_sw_driver);
	if (err)
		return err;
	vc_sw_pdev = platform_device_register_simple(DRV_NAME, -1, NULL, 0);
	if (IS_ERR(vc_sw_pdev)) {
		platform_driver_unregister(&vc_sw_driver);
		return PTR_ERR(vc_sw_pdev);
	}
	return 0;
}

static void __exit vc_sw_exit(void)
{
	platform_device_unregister(vc_sw_pdev);
	platform_driver_unregister(&vc_sw_driver);
}

module_init(vc_sw_init);
module_exit(vc_sw_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("VeloCloud Edge 5x0 88E6176 CPU link and unmanaged-switch bring-up");
