// SPDX-License-Identifier: GPL-2.0
/*
 * vc-edge5x0-dsa: hand the two Marvell 88E6176 LAN switches of a VeloCloud
 * Edge 520/540 (board version 2.x) to the mainline DSA driver (mv88e6xxx)
 * without a device tree, one network interface per front jack (lan1..lan8),
 * and bring the switch CPU links up again after that driver has reset the
 * chips (velo5x0-bsp, VBSP-DES-1C, VBSP-DES-3, VBSP-REQ-3, VBSP-REQ-4).
 *
 * Derived from velo540openwrt package/velo540-glue/src/vc-edge5x0-dsa.c,
 * https://github.com/gallops-gannets/velo540openwrt commit
 * d487e0734e7ad1da70d77ffefb681d0f4e90d317, SPDX GPL-2.0 (itself derived from
 * VeloCloud's GPL code).  Kept from upstream: the platform-data recipe (an
 * mdio_device at SMI address 0 on each igb-vc bus, a dsa_mv88e6xxx_pdata with
 * the CPU on port 4 and the igb netdev of the same PCI function as conduit,
 * compatible "marvell,mv88e6085"), the local bus_match, the conduit lookup
 * and the default port-name lists.  Changed for velo5x0-bsp: see PROVENANCE.md
 * (no platform-driver deferral, reference counting of the conduit per probe
 * attempt, bus-removal handling, the CPU-link bring-up after bind, which
 * upstream does from an unlicensed shell script and which is reimplemented
 * here from the register sequence in the velo5x0-bsp spec, VBSP-HW-SWITCH).
 *
 *   00:14.0 -> switch A: port 0 = LAN6, 1 = LAN7, 2 = LAN5, 3 = LAN8
 *   00:14.1 -> switch B: port 0 = LAN2, 1 = LAN1, 2 = LAN4, 3 = LAN3
 *   port 4 = CPU (SerDes to the I354 function); ports 5 and 6 (inter-chip
 *   links) are left out of the platform data, so DSA treats them as unused
 *   and disables them.
 *
 * Two switches each described by platform data need the DSA-core patch that
 * gives every such switch its own tree (velo5x0-bsp kernel-build
 * patches/230-*); without it the second one fails to register.  The
 * velo5x0-bsp kernel (+velo5x0dsa) carries it.
 *
 * It never touches I2C, the PCA9557 expanders or SoC GPIO (VBSP-REQ-7).
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/mdio.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/phy.h>
#include <linux/platform_device.h>
#include <linux/rtnetlink.h>
#include <linux/workqueue.h>
#include <linux/platform_data/mv88e6xxx.h>
#include <net/net_namespace.h>

#define DRV_NAME		"vc-edge5x0-dsa"
#define VC_NUM_SW		2
#define VC_LAN_PORTS		4
#define VC_CPU_PORT		4
#define VC_SW_PORT(p)		(0x10 + (p))	/* single-chip SMI address of port p */
#define VC_PORT_STATUS		0
#define VC_PORT_PHYS_CTRL	1
#define VC_PORT_CTRL		4
#define VC_PORT_VLAN_MAP	6	/* Port Based VLAN Map: ports this port may forward to */
#define VC_PS_LINK		BIT(11)
#define VC_PS_PHY_DETECT	BIT(12)
#define VC_PORT_STATE_MASK	0x0003
#define VC_SERDES_DEV		0x0f	/* SerDes, behind the Global2 SMI */
#define VC_PAGE_REG		22
#define VC_SYNC_TRIES		120	/* x 1 s waiting for igb's buses and netdevs */

static unsigned int dsa_mask = 3;
module_param(dsa_mask, uint, 0444);
MODULE_PARM_DESC(dsa_mask, "switches handed to DSA: 1 = 00:14.0 (LAN5-8), 2 = 00:14.1 (LAN1-4), 3 = both (default), 0 = none");

static ushort p4_ctrl = 0x000e;
module_param(p4_ctrl, ushort, 0444);
MODULE_PARM_DESC(p4_ctrl, "value written to switch port 4 register 1 after DSA binds (0x000e default, 0x20fe the FreeBSD value)");

static char *ports_a = "lan6,lan7,lan5,lan8";
static char *ports_b = "lan2,lan1,lan4,lan3";
module_param(ports_a, charp, 0444);
module_param(ports_b, charp, 0444);
MODULE_PARM_DESC(ports_a, "interface names for switch A (00:14.0) ports 0-3");
MODULE_PARM_DESC(ports_b, "interface names for switch B (00:14.1) ports 0-3");

/* sw->flags bits, set and cleared by the bus notifier (which takes no mutex) */
enum { VC_REGISTERED, VC_BOUND, VC_HELD };

struct vc_sw {
	const char *bus_id;
	const char *names_param;
	struct dsa_mv88e6xxx_pdata pdata;
	char names[VC_LAN_PORTS][IFNAMSIZ];
	struct mdio_device *md;		/* ours; we keep one extra device reference */
	unsigned long flags;
	struct work_struct link_work;
	char result[160];
};

static struct vc_sw vc_sw[VC_NUM_SW] = {
	{ .bus_id = "igb-vc-0000:00:14.0" },
	{ .bus_id = "igb-vc-0000:00:14.1" },
};

static DEFINE_MUTEX(vc_lock);		/* vc_sw[].md, result; registration */
/*
 * vc_io is a leaf lock held around each single MDIO access of this module
 * (never across a sleep, never while taking another lock).  The notifier
 * clears VC_BOUND (before mv88e6xxx's remove) or VC_REGISTERED (before the
 * device is deleted, e.g. under igb's mdiobus_unregister) and then takes and
 * drops vc_io: after that no access of ours can still touch a bus that is
 * going away, and every later one sees the cleared flag and is skipped.
 */
static DEFINE_MUTEX(vc_io);
static struct delayed_work vc_sync_work;
static int vc_sync_tries;
static bool vc_exiting;
static struct platform_device *vc_pdev;

/* mdio_device_bus_match() is not exported to modules: the same, locally */
static int vc_mdio_bus_match(struct device *dev, const struct device_driver *drv)
{
	struct mdio_device *mdiodev = to_mdio_device(dev);
	const struct mdio_driver *mdiodrv = to_mdio_driver(drv);

	if (mdiodrv->mdiodrv.flags & MDIO_DEVICE_IS_PHY)
		return 0;
	return strcmp(mdiodev->modalias, drv->name) == 0;
}

static struct vc_sw *vc_sw_of(struct device *dev)
{
	int i;

	for (i = 0; i < VC_NUM_SW; i++)
		if (vc_sw[i].md && dev == &vc_sw[i].md->dev)
			return &vc_sw[i];
	return NULL;
}

/* the igb netdev of the PCI function that owns this MDIO bus, with a reference */
static struct net_device *vc_conduit_for(struct device *parent)
{
	struct net_device *nd, *found = NULL;

	rtnl_lock();
	for_each_netdev(&init_net, nd) {
		if (nd->dev.parent == parent) {
			dev_hold(nd);
			found = nd;
			break;
		}
	}
	rtnl_unlock();
	return found;
}

/*
 * Conduit references.  mv88e6xxx_probe() dev_put()s pdata->netdev on every
 * failed probe and never on remove, and DSA takes its own reference once the
 * switch is set up.  So one reference is taken per probe attempt (BIND), and
 * it is dropped here when a bound switch is unbound; on a failed attempt the
 * driver has already dropped it.
 */
static int vc_bus_notify(struct notifier_block *nb, unsigned long action, void *data)
{
	struct device *dev = data;
	struct vc_sw *sw = vc_sw_of(dev);

	if (!sw)
		return NOTIFY_DONE;
	switch (action) {
	case BUS_NOTIFY_BIND_DRIVER:
		dev_hold(sw->pdata.netdev);
		set_bit(VC_HELD, &sw->flags);
		break;
	case BUS_NOTIFY_DRIVER_NOT_BOUND:
		clear_bit(VC_HELD, &sw->flags);	/* the driver dropped it */
		break;
	case BUS_NOTIFY_BOUND_DRIVER:
		set_bit(VC_BOUND, &sw->flags);
		if (!READ_ONCE(vc_exiting))
			schedule_work(&sw->link_work);
		break;
	case BUS_NOTIFY_UNBIND_DRIVER:
		/* before mv88e6xxx_remove() frees the chip and its internal bus */
		clear_bit(VC_BOUND, &sw->flags);
		mutex_lock(&vc_io);
		mutex_unlock(&vc_io);
		break;
	case BUS_NOTIFY_UNBOUND_DRIVER:
		clear_bit(VC_BOUND, &sw->flags);
		if (test_and_clear_bit(VC_HELD, &sw->flags))
			dev_put(sw->pdata.netdev);
		break;
	case BUS_NOTIFY_DEL_DEVICE:
		/* removed by us, or by igb unregistering the bus under it */
		if (test_and_clear_bit(VC_REGISTERED, &sw->flags)) {
			mutex_lock(&vc_io);
			mutex_unlock(&vc_io);
			if (!READ_ONCE(vc_exiting)) {
				vc_sync_tries = 0;
				schedule_delayed_work(&vc_sync_work, HZ);
			}
		}
		break;
	}
	return NOTIFY_OK;
}

static struct notifier_block vc_bus_nb = { .notifier_call = vc_bus_notify };

/* caller holds vc_lock */
static int vc_sw_register(struct vc_sw *sw)
{
	struct mdio_device *md;
	struct net_device *nd;
	struct mii_bus *bus;
	char buf[64], *p, *tok;
	int i, err;

	bus = mdio_find_bus(sw->bus_id);
	if (!bus)
		return -EPROBE_DEFER;
	nd = NULL;
	/* not a bus that igb is tearing down (or has not finished setting up) */
	if (bus->state != MDIOBUS_REGISTERED || !bus->parent || !device_is_bound(bus->parent)) {
		err = -EPROBE_DEFER;
		goto out_bus;
	}
	nd = vc_conduit_for(bus->parent);
	if (!nd) {
		err = -EPROBE_DEFER;
		goto out_bus;
	}

	memset(&sw->pdata, 0, sizeof(sw->pdata));
	strscpy(buf, sw->names_param, sizeof(buf));
	p = buf;
	for (i = 0; i < VC_LAN_PORTS; i++) {
		tok = strsep(&p, ",");
		if (!tok || !*tok || strlen(tok) >= IFNAMSIZ) {
			pr_err(DRV_NAME ": bad port name list \"%s\"\n", sw->names_param);
			err = -EINVAL;
			goto out_bus;
		}
		strscpy(sw->names[i], tok, IFNAMSIZ);
		sw->pdata.cd.port_names[i] = sw->names[i];
	}
	sw->pdata.cd.port_names[VC_CPU_PORT] = (char *)"cpu";
	sw->pdata.compatible = "marvell,mv88e6085";	/* 88E6352 family incl. 88E6176 */
	sw->pdata.enabled_ports = GENMASK(VC_CPU_PORT, 0);	/* 5, 6: unused */
	sw->pdata.netdev = nd;
	sw->pdata.eeprom_len = 0;
	sw->pdata.irq = 0;		/* INTn not wired: the driver polls */

	md = mdio_device_create(bus, 0);
	if (IS_ERR(md)) {
		err = PTR_ERR(md);
		goto out_bus;
	}
	strscpy(md->modalias, "mv88e6085", sizeof(md->modalias));
	md->bus_match = vc_mdio_bus_match;
	md->dev.platform_data = &sw->pdata;
	get_device(&md->dev);		/* ours, until vc_sw_reap/unregister */
	sw->result[0] = 0;
	sw->md = md;			/* before register: the notifier matches on it */
	set_bit(VC_REGISTERED, &sw->flags);
	err = mdio_device_register(md);	/* binds mv88e6xxx now if it is loaded */
	if (err) {
		clear_bit(VC_REGISTERED, &sw->flags);
		sw->md = NULL;
		put_device(&md->dev);
		mdio_device_free(md);
		goto out_bus;
	}
	pr_info(DRV_NAME ": %s -> DSA (conduit %s, ports %s)%s\n", sw->bus_id,
		netdev_name(nd), sw->names_param,
		test_bit(VC_BOUND, &sw->flags) ? "" : "; waiting for mv88e6xxx to bind");
	err = 0;
out_bus:
	if (nd)
		dev_put(nd);	/* the lookup's; BIND takes the probe's own */
	put_device(&bus->dev);
	return err;
}

/*
 * caller holds vc_lock: drop a device that the bus removed under us, once it
 * is fully unbound (so its late notifications still find it and its conduit
 * reference is returned)
 */
static void vc_sw_reap(struct vc_sw *sw)
{
	if (sw->md && !test_bit(VC_REGISTERED, &sw->flags) &&
	    !test_bit(VC_HELD, &sw->flags) && !test_bit(VC_BOUND, &sw->flags) &&
	    !READ_ONCE(sw->md->dev.driver)) {
		pr_info(DRV_NAME ": %s went away (igb unbound); will re-register when it returns\n",
			sw->bus_id);
		put_device(&sw->md->dev);
		sw->md = NULL;
	}
}

/* caller holds vc_lock */
static void vc_sw_unregister(struct vc_sw *sw)
{
	struct mdio_device *md = sw->md;

	if (!md)
		return;
	if (test_and_clear_bit(VC_REGISTERED, &sw->flags)) {
		mdio_device_remove(md);		/* unbinds mv88e6xxx: lanN go away */
		mdio_device_free(md);
	}
	sw->md = NULL;
	put_device(&md->dev);
}

static void vc_sync(struct work_struct *work)
{
	int i, err, waiting = 0;

	mutex_lock(&vc_lock);
	if (vc_exiting)
		goto out;
	for (i = 0; i < VC_NUM_SW; i++) {
		vc_sw_reap(&vc_sw[i]);
		if (!(dsa_mask & BIT(i)))
			continue;
		if (vc_sw[i].md) {
			if (!test_bit(VC_REGISTERED, &vc_sw[i].flags))
				waiting++;	/* still unbinding: reap it next pass */
			continue;
		}
		err = vc_sw_register(&vc_sw[i]);
		if (err == -EPROBE_DEFER)
			waiting++;
		else if (err)
			pr_err(DRV_NAME ": %s: registration failed (%d)\n", vc_sw[i].bus_id, err);
	}
	if (waiting && ++vc_sync_tries <= VC_SYNC_TRIES)
		schedule_delayed_work(&vc_sync_work, HZ);
	else if (waiting)
		pr_err(DRV_NAME ": gave up waiting for the igb-vc buses and netdevs (is the velo5x0-bsp igb bound to 00:14.0/1?); reload " DRV_NAME " to retry\n");
out:
	mutex_unlock(&vc_lock);
}

static int vc_is_mv_bus(struct device *dev, void *data)
{
	return dev->class && !strcmp(dev->class->name, "mdio_bus") &&
	       !strncmp(dev_name(dev), "mv88e6xxx-", 10);
}

/* the mv88e6xxx internal MDIO bus (Global2 SMI) of switch device dev, with a reference */
static struct mii_bus *vc_internal_bus(struct device *dev)
{
	struct device *d = device_find_child(dev, NULL, vc_is_mv_bus);

	return d ? to_mii_bus(d) : NULL;
}

/*
 * One MDIO access under vc_io, only while the device is still registered
 * (raw: the igb-vc bus) or still bound (internal: mv88e6xxx's bus).
 */
static int vc_rd(struct vc_sw *sw, struct mii_bus *bus, int need, int addr, int reg)
{
	int v = -ENODEV;

	mutex_lock(&vc_io);
	if (test_bit(VC_REGISTERED, &sw->flags) && test_bit(need, &sw->flags))
		v = mdiobus_read(bus, addr, reg);
	mutex_unlock(&vc_io);
	return v;
}

static int vc_wr(struct vc_sw *sw, struct mii_bus *bus, int need, int addr, int reg, u16 val)
{
	int err = -ENODEV;

	mutex_lock(&vc_io);
	if (test_bit(VC_REGISTERED, &sw->flags) && test_bit(need, &sw->flags))
		err = mdiobus_write(bus, addr, reg, val);
	mutex_unlock(&vc_io);
	return err;
}

/*
 * The CPU link (VBSP-DES-3), after mv88e6xxx has reset the chip.  The driver
 * leaves port 4 unforced and, without a device tree, never configures its
 * SerDes.  If port 4 has no link: SerDes page 1 BMCR |= reset + full duplex,
 * &= ~(autoneg + power-down), wait 1 s, page 0 again.  Then clear PHYDetect
 * in port 4 status and write port 4 register 1 = p4_ctrl.  SerDes registers
 * go through the driver's own internal MDIO bus, so each access takes the
 * driver's chip lock (the page-select/BMCR sequence is not atomic against the
 * driver, which with no switch IRQ does not touch the SerDes after setup);
 * the two port 4 writes are single SMI transactions on the igb-vc bus.
 * Caller holds vc_lock.
 */
static int vc_cpu_link(struct vc_sw *sw)
{
	struct mii_bus *raw = sw->md->bus, *ib;
	int st, v = 0, err;
	bool serdes = false;

	st = vc_rd(sw, raw, VC_BOUND, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS);
	if (st < 0)
		return st;
	if (!(st & VC_PS_LINK)) {
		ib = vc_internal_bus(&sw->md->dev);
		if (!ib)
			return -ENODEV;
		serdes = true;
		err = vc_wr(sw, ib, VC_BOUND, VC_SERDES_DEV, VC_PAGE_REG, 1);
		if (!err) {
			v = vc_rd(sw, ib, VC_BOUND, VC_SERDES_DEV, MII_BMCR);
			err = v < 0 ? v : 0;
		}
		if (!err)
			err = vc_wr(sw, ib, VC_BOUND, VC_SERDES_DEV, MII_BMCR,
				    (v | BMCR_RESET | BMCR_FULLDPLX) &
				    ~(BMCR_ANENABLE | BMCR_PDOWN));
		if (!err) {
			msleep(1000);	/* SerDes reset settle, as both known-good sequences wait */
			vc_wr(sw, ib, VC_BOUND, VC_SERDES_DEV, VC_PAGE_REG, 0);
		}
		put_device(&ib->dev);
		if (err)
			return err;
		st = vc_rd(sw, raw, VC_BOUND, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS);
		if (st < 0)
			return st;
	}
	err = vc_wr(sw, raw, VC_BOUND, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS, st & ~VC_PS_PHY_DETECT);
	if (!err)
		err = vc_wr(sw, raw, VC_BOUND, VC_SW_PORT(VC_CPU_PORT), VC_PORT_PHYS_CTRL, p4_ctrl);
	v = vc_rd(sw, raw, VC_BOUND, VC_SW_PORT(VC_CPU_PORT), VC_PORT_STATUS);
	scnprintf(sw->result, sizeof(sw->result),
		  "%s: SerDes %s, PHYDetect cleared, p4_ctrl %#06x, port4 status %#06x (link %s)",
		  err ? "error" : "up", serdes ? "reset" : "not reset (port 4 already had link)",
		  p4_ctrl, v, v >= 0 && (v & VC_PS_LINK) ? "up" : "down");
	return err;
}

static void vc_link_work(struct work_struct *work)
{
	struct vc_sw *sw = container_of(work, struct vc_sw, link_work);
	int err;

	mutex_lock(&vc_lock);
	if (vc_exiting || !sw->md || !test_bit(VC_REGISTERED, &sw->flags) ||
	    !test_bit(VC_BOUND, &sw->flags))
		goto out;
	err = vc_cpu_link(sw);
	if (err)
		scnprintf(sw->result, sizeof(sw->result), "error %d bringing up the CPU link", err);
	pr_info(DRV_NAME ": %s CPU link %s\n", sw->bus_id, sw->result);
out:
	mutex_unlock(&vc_lock);
}

/* read-only status for velo5x0-check (VBSP-TEST-4: ports 5/6 disabled) */
static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mii_bus *raw;
	ssize_t n = 0;
	int i, p, c;

	mutex_lock(&vc_lock);
	for (i = 0; i < VC_NUM_SW; i++) {
		struct vc_sw *sw = &vc_sw[i];

		n += sysfs_emit_at(buf, n, "switch %c %s: %s%s%s cpu-link %s\n", 'A' + i, sw->bus_id,
				   !(dsa_mask & BIT(i)) ? "not handed to DSA" :
				   test_bit(VC_REGISTERED, &sw->flags) ? "registered" : "not registered",
				   test_bit(VC_BOUND, &sw->flags) ? " bound" : "",
				   sw->md && test_bit(VC_REGISTERED, &sw->flags) ?
				   (sw->md->dev.driver ? " driver mv88e6085" : " no driver") : "",
				   sw->result[0] ? sw->result : "not run");
		if (!sw->md || !test_bit(VC_REGISTERED, &sw->flags))
			continue;
		raw = sw->md->bus;
		for (p = 0; p <= 6; p++) {
			c = vc_rd(sw, raw, VC_REGISTERED, VC_SW_PORT(p), VC_PORT_CTRL);
			n += sysfs_emit_at(buf, n, "  port %d status %#06x physctrl %#06x ctrl %#06x state %s vlanmap %#06x\n",
					   p, vc_rd(sw, raw, VC_REGISTERED, VC_SW_PORT(p), VC_PORT_STATUS),
					   vc_rd(sw, raw, VC_REGISTERED, VC_SW_PORT(p), VC_PORT_PHYS_CTRL), c,
					   c < 0 ? "unknown" : (c & VC_PORT_STATE_MASK) == 0 ? "disabled" : "enabled",
					   vc_rd(sw, raw, VC_REGISTERED, VC_SW_PORT(p), VC_PORT_VLAN_MAP));
		}
	}
	mutex_unlock(&vc_lock);
	return n;
}
static DEVICE_ATTR_RO(status);
static struct attribute *vc_attrs[] = { &dev_attr_status.attr, NULL };
ATTRIBUTE_GROUPS(vc);

static int __init vc_dsa_init(void)
{
	const char *board = dmi_get_system_info(DMI_BOARD_NAME);
	const char *ver = dmi_get_system_info(DMI_BOARD_VERSION);
	int i, err;

	if (!board || (strcmp(board, "EDGE520") && strcmp(board, "EDGE540")) ||
	    !ver || ver[0] != '2' || ver[1] != '.') {
		pr_info(DRV_NAME ": board \"%s\" version \"%s\" not supported (need EDGE520/EDGE540 2.x); touching no hardware\n",
			board ? board : "(none)", ver ? ver : "(none)");
		return -ENODEV;
	}
	if (dsa_mask & ~GENMASK(VC_NUM_SW - 1, 0))
		return -EINVAL;
	vc_sw[0].names_param = ports_a;
	vc_sw[1].names_param = ports_b;
	for (i = 0; i < VC_NUM_SW; i++)
		INIT_WORK(&vc_sw[i].link_work, vc_link_work);
	INIT_DELAYED_WORK(&vc_sync_work, vc_sync);

	vc_pdev = platform_device_alloc(DRV_NAME, PLATFORM_DEVID_NONE);
	if (!vc_pdev)
		return -ENOMEM;
	vc_pdev->dev.groups = vc_groups;	/* status file exists before the uevent */
	err = platform_device_add(vc_pdev);
	if (err) {
		platform_device_put(vc_pdev);
		return err;
	}
	err = bus_register_notifier(&mdio_bus_type, &vc_bus_nb);
	if (err)
		goto out_pdev;
	if (!dsa_mask)
		pr_info(DRV_NAME ": dsa_mask=0: switches left alone\n");
	schedule_delayed_work(&vc_sync_work, 0);
	return 0;

out_pdev:
	platform_device_unregister(vc_pdev);
	return err;
}

static void __exit vc_dsa_exit(void)
{
	int i;

	mutex_lock(&vc_lock);
	WRITE_ONCE(vc_exiting, true);
	mutex_unlock(&vc_lock);
	cancel_delayed_work_sync(&vc_sync_work);
	mutex_lock(&vc_lock);
	for (i = VC_NUM_SW - 1; i >= 0; i--)
		vc_sw_unregister(&vc_sw[i]);
	mutex_unlock(&vc_lock);
	bus_unregister_notifier(&mdio_bus_type, &vc_bus_nb);
	/* after the notifier is gone, so nothing can queue them again */
	cancel_delayed_work_sync(&vc_sync_work);
	for (i = 0; i < VC_NUM_SW; i++)
		cancel_work_sync(&vc_sw[i].link_work);
	platform_device_unregister(vc_pdev);
}

module_init(vc_dsa_init);
module_exit(vc_dsa_exit);
MODULE_SOFTDEP("pre: mv88e6xxx tag_dsa");
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("VeloCloud Edge 5x0 88E6176 switches as DSA (platform data), one interface per jack");
