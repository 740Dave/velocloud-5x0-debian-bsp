// SPDX-License-Identifier: GPL-2.0
/*
 * vc-edge5x0-board: VeloCloud Edge 520/540 (board version 2.x) board support
 * for stock Debian 13 kernels of the 6.12 series (velo5x0-bsp).
 *
 * Provenance (see PROVENANCE.md):
 *   Derived in part from velo540openwrt package/velo540-glue/src/vc-edge5x0-mdio.c
 *   (https://github.com/gallops-gannets/velo540openwrt, commit
 *   d487e0734e7ad1da70d77ffefb681d0f4e90d317, SPDX GPL-2.0): the DMI board
 *   match, the LPC GPIO base lookup (00:1f.0 config 0x48) and the GPIO_USE_SEL
 *   switch of the bus pins.  Everything else in this file is new: the stock
 *   Debian kernel builds neither gpio-ich, mdio-gpio, mdio-bitbang nor
 *   i2c-gpio, so the buses are bit-banged here directly on the SoC GPIO
 *   registers, and the PCA9557 reset sequence (FreeBSD vc_board.c, as
 *   recorded in the velo5x0-bsp spec VBSP-HW-PCA18) runs here before igb binds.
 *
 * What it does, only on a supported board (VBSP-REQ-12), in this order
 * (VBSP-DES-1A, VBSP-REQ-6):
 *   1. switch SoC core-well GPIO SC11..SC14 to GPIO (inputs first);
 *   2. register a bit-banged I2C adapter on SC11 (SDA) / SC12 (SCL);
 *   3. run the PCA9557 @0x18 reset sequence (POL=0, OUT=0xE3, CFG=0, 10 ms,
 *      OUT=0xFF, 50 ms), only while no driver is bound to 00:14.0..00:14.3,
 *      holding their device locks so none can bind meanwhile (VBSP-REQ-7C);
 *   4. register a bit-banged Clause-22 MDIO bus on SC14 (MDC) / SC13 (MDIO)
 *      named "vc-edge5x0-wan", carrying the 88E1514 WAN PHYs at 0 and 1.
 *      Registering it last lets the patched igb's deferred probe of
 *      00:14.2/00:14.3 retry only after the PHYs are out of reset.
 *
 * Hard rules this file keeps (VBSP-REQ-7):
 *   - it never writes the SUS GPIO bank (GPIO base + 0x80): SUS2 is the
 *     power-cycle line (VBSP-REQ-7A).  Every GPIO write goes through
 *     vc_sc_update(), which only accepts SC-bank registers and only the four
 *     bus pins;
 *   - it never addresses the PCA9557 at 0x1c (VBSP-REQ-7B);
 *   - it writes the PCA9557 at 0x18 only in vc_pca18_reset(), which runs once
 *     per module load and refuses when igb (or anything) is bound to
 *     00:14.x (VBSP-REQ-7C).
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/dmi.h>
#include <linux/pci.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/delay.h>
#include <linux/spinlock.h>
#include <linux/i2c.h>
#include <linux/i2c-algo-bit.h>
#include <linux/phy.h>
#include <linux/platform_device.h>

#define DRV_NAME		"vc-edge5x0-board"
#define VC_MDIO_BUS_ID		"vc-edge5x0-wan"

/* Atom C2000 LPC bridge (00:1f.0): GPIO base address in config space 0x48 */
#define VC_LPC_DEVFN		PCI_DEVFN(0x1f, 0)
#define VC_LPC_DEVICE		0x1f38
#define VC_LPC_GBASE		0x48
#define VC_GBASE_MASK		0xff80

/* SoC core-well (SC) GPIO bank, offsets from the GPIO base.  The SUS bank
 * (base + 0x80) is deliberately absent: nothing here may address it.
 */
#define VC_SC_USE_SEL		0x00
#define VC_SC_IO_SEL		0x04	/* 1 = input */
#define VC_SC_GP_LVL		0x08
#define VC_SC_REGION_LEN	0x0c

/* rev B (board version 2.x) pins, as SC-bank bit numbers */
#define VC_PIN_SDA		11
#define VC_PIN_SCL		12
#define VC_PIN_MDIO		13
#define VC_PIN_MDC		14
#define VC_BUS_PINS		(BIT(VC_PIN_SDA) | BIT(VC_PIN_SCL) | \
				 BIT(VC_PIN_MDIO) | BIT(VC_PIN_MDC))

/* I354 SGMII functions 00:14.0 .. 00:14.3 */
#define VC_I354_SLOT		0x14
#define VC_I354_DEVICE		0x1f41
#define VC_I354_NFUNC		4
#define VC_E1000_EERD		0x14
#define VC_EERD_START		BIT(0)
#define VC_EERD_DONE		BIT(1)
#define VC_EERD_ADDR_SHIFT	2
#define VC_EERD_DATA_SHIFT	16
#define VC_NVM_WORD_ID		0x06
#define VC_NVM_ID_VC		0x5663	/* "Vc" */
#define VC_NVM_ID_5X		0x3558	/* "5X" */

/* PCA9557 #1 at 0x18: bit 2 switch A reset, bit 3 switch B reset,
 * bit 4 both WAN PHY resets (all active low).
 */
#define VC_PCA18_ADDR		0x18
#define PCA9557_REG_IN		0
#define PCA9557_REG_OUT		1
#define PCA9557_REG_POL		2
#define PCA9557_REG_CFG		3
#define VC_PCA18_OUT_ASSERT	0xe3
#define VC_PCA18_OUT_RELEASE	0xff

static u16 vc_gbase;
static u32 vc_lvl_shadow;		/* GP_LVL is not readable for outputs */
static DEFINE_RAW_SPINLOCK(vc_gpio_lock);
static bool vc_region_held;

static struct platform_device *vc_pdev;
static struct i2c_adapter vc_i2c_adap;
static bool vc_i2c_added;
static struct mii_bus *vc_mii;

static char vc_reset_result[96] = "not run";
module_param_string(reset_result, vc_reset_result, sizeof(vc_reset_result), 0444);
MODULE_PARM_DESC(reset_result, "outcome of the PCA9557@0x18 reset sequence at load (read-only)");

static char vc_i2c_name[48] = "";
module_param_string(i2c_adapter, vc_i2c_name, sizeof(vc_i2c_name), 0444);
MODULE_PARM_DESC(i2c_adapter, "name of the board I2C adapter this module registered (read-only)");

/* ---------------------------------------------------------------- GPIO -- */

/*
 * The only GPIO write path.  @reg must be an SC-bank register and @mask may
 * only name the four bus pins; anything else is refused (VBSP-REQ-7A).
 */
static void vc_sc_update(unsigned int reg, u32 mask, u32 val)
{
	unsigned long flags;
	u32 v;

	if (WARN_ON(reg != VC_SC_USE_SEL && reg != VC_SC_IO_SEL &&
		    reg != VC_SC_GP_LVL))
		return;
	if (WARN_ON(mask & ~VC_BUS_PINS))
		return;

	raw_spin_lock_irqsave(&vc_gpio_lock, flags);
	if (reg == VC_SC_GP_LVL) {
		/* GP_LVL is written from a shadow only, never from a read: on this
		 * SoC an output level cannot be read back (VBSP-HW-GPIO
		 * x-readback-bug), so a read-modify-write could flip another SC
		 * output.  The shadow is seeded once at load (vc_gpio_init) and
		 * only our four bits ever change in it, so every other bit is
		 * written back with the value it had when this module loaded.
		 * Assumption: nothing else drives SC-bank GPIO outputs after that
		 * (the Debian kernel has no gpio-ich); any other GPIO-mode SC
		 * output present at load is logged as a warning.
		 */
		vc_lvl_shadow = (vc_lvl_shadow & ~mask) | (val & mask);
		outl(vc_lvl_shadow, vc_gbase + VC_SC_GP_LVL);
	} else {
		v = inl(vc_gbase + reg);
		v = (v & ~mask) | (val & mask);
		outl(v, vc_gbase + reg);
	}
	raw_spin_unlock_irqrestore(&vc_gpio_lock, flags);
}

static int vc_sc_get(unsigned int pin)
{
	return !!(inl(vc_gbase + VC_SC_GP_LVL) & BIT(pin));
}

/* drive a pin low (output, level 0) or release it (input) */
static void vc_pin_od(unsigned int pin, int high)
{
	if (high) {
		vc_sc_update(VC_SC_IO_SEL, BIT(pin), BIT(pin));
	} else {
		vc_sc_update(VC_SC_GP_LVL, BIT(pin), 0);
		vc_sc_update(VC_SC_IO_SEL, BIT(pin), 0);
	}
}

static int vc_gpio_init(void)
{
	struct pci_dev *lpc;
	u32 gbase = 0, use, io;

	lpc = pci_get_domain_bus_and_slot(0, 0, VC_LPC_DEVFN);
	if (!lpc)
		return -ENODEV;
	pci_read_config_dword(lpc, VC_LPC_GBASE, &gbase);
	pci_dev_put(lpc);
	gbase &= VC_GBASE_MASK;
	if (!gbase) {
		pr_err(DRV_NAME ": GPIO base not set in 00:1f.0 config 0x48\n");
		return -ENODEV;
	}
	vc_gbase = gbase;

	if (!request_region(vc_gbase, VC_SC_REGION_LEN, DRV_NAME)) {
		pr_err(DRV_NAME ": GPIO SC bank I/O %#x..%#x is claimed by another driver; touching no hardware\n",
		       vc_gbase, vc_gbase + VC_SC_REGION_LEN - 1);
		return -EBUSY;
	}
	vc_region_held = true;

	use = inl(vc_gbase + VC_SC_USE_SEL);
	io = inl(vc_gbase + VC_SC_IO_SEL);
	vc_lvl_shadow = inl(vc_gbase + VC_SC_GP_LVL);	/* the one read that seeds the shadow */
	if (use & ~io & ~VC_BUS_PINS)
		pr_warn(DRV_NAME ": other GPIO-mode SC outputs %#010x: their GP_LVL bits are written back as read now (%#010x); their levels cannot be re-read later\n",
			(u32)(use & ~io & ~VC_BUS_PINS), (u32)(vc_lvl_shadow & use & ~io & ~VC_BUS_PINS));
	pr_info(DRV_NAME ": GPIO base %#x: SC USE_SEL %#010x IO_SEL %#010x GP_LVL %#010x; other GPIO-mode SC outputs %#010x\n",
		vc_gbase, use, io, vc_lvl_shadow, (u32)(use & ~io & ~VC_BUS_PINS));

	/* bus pins: inputs (released) first, then GPIO mode, then latch 0 */
	vc_sc_update(VC_SC_IO_SEL, VC_BUS_PINS, VC_BUS_PINS);
	vc_sc_update(VC_SC_USE_SEL, VC_BUS_PINS, VC_BUS_PINS);
	vc_sc_update(VC_SC_GP_LVL, VC_BUS_PINS, 0);
	/* MDC is a push-pull output, idle low */
	vc_sc_update(VC_SC_IO_SEL, BIT(VC_PIN_MDC), 0);

	pr_info(DRV_NAME ": SC USE_SEL %#010x -> %#010x\n", use,
		inl(vc_gbase + VC_SC_USE_SEL));
	return 0;
}

/* ----------------------------------------------------------------- I2C -- */

static void vc_i2c_setsda(void *data, int state) { vc_pin_od(VC_PIN_SDA, state); }
static void vc_i2c_setscl(void *data, int state) { vc_pin_od(VC_PIN_SCL, state); }
static int vc_i2c_getsda(void *data) { return vc_sc_get(VC_PIN_SDA); }
static int vc_i2c_getscl(void *data) { return vc_sc_get(VC_PIN_SCL); }

static struct i2c_algo_bit_data vc_i2c_bit = {
	.setsda = vc_i2c_setsda,
	.setscl = vc_i2c_setscl,
	.getsda = vc_i2c_getsda,
	.getscl = vc_i2c_getscl,
	.udelay = 5,			/* ~100 kHz */
	.timeout = HZ / 10,
};

static int vc_i2c_init(struct device *parent)
{
	int err;

	/* bus idle: both lines released, then wait 100 us (FreeBSD sequence) */
	vc_pin_od(VC_PIN_SDA, 1);
	vc_pin_od(VC_PIN_SCL, 1);
	udelay(100);

	vc_i2c_adap.owner = THIS_MODULE;
	vc_i2c_adap.class = 0;
	vc_i2c_adap.algo_data = &vc_i2c_bit;
	vc_i2c_adap.dev.parent = parent;
	vc_i2c_adap.nr = -1;
	strscpy(vc_i2c_adap.name, "vc-edge5x0 board i2c", sizeof(vc_i2c_adap.name));
	err = i2c_bit_add_numbered_bus(&vc_i2c_adap);
	if (err)
		return err;
	vc_i2c_added = true;
	scnprintf(vc_i2c_name, sizeof(vc_i2c_name), "i2c-%d", vc_i2c_adap.nr);
	pr_info(DRV_NAME ": board I2C bus registered as %s (SDA SC%d, SCL SC%d)\n",
		vc_i2c_name, VC_PIN_SDA, VC_PIN_SCL);
	return 0;
}

static int vc_pca18_read(u8 reg)
{
	union i2c_smbus_data d;
	int err;

	err = i2c_smbus_xfer(&vc_i2c_adap, VC_PCA18_ADDR, 0, I2C_SMBUS_READ,
			     reg, I2C_SMBUS_BYTE_DATA, &d);
	return err ? err : d.byte;
}

static int vc_pca18_write(u8 reg, u8 val)
{
	union i2c_smbus_data d = { .byte = val };

	return i2c_smbus_xfer(&vc_i2c_adap, VC_PCA18_ADDR, 0, I2C_SMBUS_WRITE,
			      reg, I2C_SMBUS_BYTE_DATA, &d);
}

/*
 * The PCA9557 @0x18 reset sequence (VBSP-DES-1A).  The only code that writes
 * 0x18.  It holds the device lock of every I354 function so that no driver
 * can bind while the resets are asserted, and refuses if any is already bound
 * (VBSP-REQ-7C: never pulse bit 4 while igb is bound to 00:14.2/00:14.3).
 */
static void vc_pca18_reset(void)
{
	struct pci_dev *fn[VC_I354_NFUNC] = { };
	int i, nlocked = 0, in, out, pol, cfg, err = 0;
	const char *bound = NULL;

	for (i = 0; i < VC_I354_NFUNC; i++) {
		fn[i] = pci_get_domain_bus_and_slot(0, 0, PCI_DEVFN(VC_I354_SLOT, i));
		if (!fn[i]) {
			scnprintf(vc_reset_result, sizeof(vc_reset_result),
				  "skipped: 00:14.%d absent", i);
			goto out;
		}
	}
	for (i = 0; i < VC_I354_NFUNC; i++) {
		device_lock(&fn[i]->dev);
		nlocked++;
		if (fn[i]->dev.driver && !bound)
			bound = pci_name(fn[i]);
	}
	if (bound) {
		scnprintf(vc_reset_result, sizeof(vc_reset_result),
			  "skipped: %s already bound to a driver", bound);
		goto out;
	}

	in = vc_pca18_read(PCA9557_REG_IN);
	out = vc_pca18_read(PCA9557_REG_OUT);
	pol = vc_pca18_read(PCA9557_REG_POL);
	cfg = vc_pca18_read(PCA9557_REG_CFG);
	pr_info(DRV_NAME ": PCA9557@0x18 before reset: IN=%#04x OUT=%#04x POL=%#04x CFG=%#04x\n",
		in, out, pol, cfg);
	if (in < 0) {
		scnprintf(vc_reset_result, sizeof(vc_reset_result),
			  "failed: PCA9557@0x18 does not answer (%d)", in);
		goto out;
	}

	err = vc_pca18_write(PCA9557_REG_POL, 0x00);
	if (!err)
		err = vc_pca18_write(PCA9557_REG_OUT, VC_PCA18_OUT_ASSERT);
	if (!err)
		err = vc_pca18_write(PCA9557_REG_CFG, 0x00);
	if (!err) {
		msleep(10);
		err = vc_pca18_write(PCA9557_REG_OUT, VC_PCA18_OUT_RELEASE);
		msleep(50);
	}
	if (err) {
		/* last resort: try to leave every reset released */
		vc_pca18_write(PCA9557_REG_OUT, VC_PCA18_OUT_RELEASE);
		scnprintf(vc_reset_result, sizeof(vc_reset_result),
			  "failed: I2C write error %d", err);
		goto out;
	}
	in = vc_pca18_read(PCA9557_REG_IN);
	out = vc_pca18_read(PCA9557_REG_OUT);
	scnprintf(vc_reset_result, sizeof(vc_reset_result),
		  "done: OUT=%#04x IN=%#04x", out, in);
out:
	for (i = nlocked - 1; i >= 0; i--)
		device_unlock(&fn[i]->dev);
	for (i = 0; i < VC_I354_NFUNC; i++)
		pci_dev_put(fn[i]);
	pr_info(DRV_NAME ": switch/WAN PHY reset sequence %s\n", vc_reset_result);
}

/* ---------------------------------------------------------------- MDIO -- */
/*
 * IEEE 802.3 clause 22 framing, bit-banged.  MDC idles low; the PHY samples
 * MDIO on the rising edge and drives read data after it.
 */
#define VC_MDIO_HALF_NS		400

static void vc_mdc(int v)
{
	vc_sc_update(VC_SC_GP_LVL, BIT(VC_PIN_MDC), v ? BIT(VC_PIN_MDC) : 0);
}

static void vc_mdio_drive(bool out)
{
	if (out) {
		vc_sc_update(VC_SC_IO_SEL, BIT(VC_PIN_MDIO), 0);
	} else {
		/* Avoton read-back workaround (VBSP-HW-GPIO): drive the line low
		 * before releasing it, so no stale output latch of 1 can be read
		 * back as data (the fix velo540openwrt patches/210 makes in
		 * mdio-gpio).
		 */
		vc_sc_update(VC_SC_GP_LVL, BIT(VC_PIN_MDIO), 0);
		vc_sc_update(VC_SC_IO_SEL, BIT(VC_PIN_MDIO), BIT(VC_PIN_MDIO));
	}
}

static void vc_mdio_send_bit(int v)
{
	vc_sc_update(VC_SC_GP_LVL, BIT(VC_PIN_MDIO), v ? BIT(VC_PIN_MDIO) : 0);
	ndelay(VC_MDIO_HALF_NS);
	vc_mdc(1);
	ndelay(VC_MDIO_HALF_NS);
	vc_mdc(0);
}

static int vc_mdio_get_bit(void)
{
	ndelay(VC_MDIO_HALF_NS);
	vc_mdc(1);
	ndelay(VC_MDIO_HALF_NS);
	vc_mdc(0);
	return vc_sc_get(VC_PIN_MDIO);
}

static void vc_mdio_send_num(u16 v, int bits)
{
	int i;

	for (i = bits - 1; i >= 0; i--)
		vc_mdio_send_bit((v >> i) & 1);
}

static u16 vc_mdio_get_num(int bits)
{
	u16 v = 0;
	int i;

	for (i = 0; i < bits; i++)
		v = (v << 1) | vc_mdio_get_bit();
	return v;
}

static void vc_mdio_cmd(int op, int phy, int reg)
{
	int i;

	vc_mdio_drive(true);
	for (i = 0; i < 32; i++)	/* preamble */
		vc_mdio_send_bit(1);
	vc_mdio_send_bit(0);		/* ST = 01 */
	vc_mdio_send_bit(1);
	vc_mdio_send_num(op, 2);	/* 10 = read, 01 = write */
	vc_mdio_send_num(phy, 5);
	vc_mdio_send_num(reg, 5);
}

static int vc_mdio_read(struct mii_bus *bus, int phy, int reg)
{
	int i, v;

	vc_mdio_cmd(2, phy, reg);
	vc_mdio_drive(false);
	if (vc_mdio_get_bit() != 0) {
		/* nobody drove turnaround low: flush and report no device */
		for (i = 0; i < 32; i++)
			vc_mdio_get_bit();
		return 0xffff;
	}
	v = vc_mdio_get_num(16);
	vc_mdio_get_bit();		/* idle */
	return v;
}

static int vc_mdio_write(struct mii_bus *bus, int phy, int reg, u16 val)
{
	vc_mdio_cmd(1, phy, reg);
	vc_mdio_send_bit(1);		/* TA = 10 */
	vc_mdio_send_bit(0);
	vc_mdio_send_num(val, 16);
	vc_mdio_drive(false);
	vc_mdio_get_bit();		/* idle */
	return 0;
}

static int vc_mdio_init(struct device *parent)
{
	int err, a;

	vc_mii = mdiobus_alloc();
	if (!vc_mii)
		return -ENOMEM;
	vc_mii->name = "vc-edge5x0 WAN PHY MDIO (bit-bang)";
	strscpy(vc_mii->id, VC_MDIO_BUS_ID, MII_BUS_ID_SIZE);
	vc_mii->read = vc_mdio_read;
	vc_mii->write = vc_mdio_write;
	vc_mii->parent = parent;
	/* igb drives these PHYs through mdiobus_read/write; phylib must not
	 * scan the bus or attach its own driver to them.
	 */
	vc_mii->phy_mask = ~0U;
	err = mdiobus_register(vc_mii);
	if (err) {
		mdiobus_free(vc_mii);
		vc_mii = NULL;
		return err;
	}
	for (a = 0; a < 2; a++)
		pr_info(DRV_NAME ": %s PHY %d ID %04x:%04x\n", VC_MDIO_BUS_ID, a,
			mdiobus_read(vc_mii, a, MII_PHYSID1),
			mdiobus_read(vc_mii, a, MII_PHYSID2));
	return 0;
}

/* --------------------------------------------------------- board match -- */

/*
 * Read I354 NVM words 0x06/0x07 through EERD.  Called with the device lock
 * held (see vc_read_nvm_id_locked), so igb cannot bind and start using the
 * same registers between the driver check and the EERD access.
 */
static int vc_read_nvm_id(struct pci_dev *pdev, u16 *w)
{
	void __iomem *regs;
	u16 cmd;
	u32 v = 0;
	int i, n;

	device_lock_assert(&pdev->dev);
	if (pdev->dev.driver)
		return -EBUSY;
	pci_read_config_word(pdev, PCI_COMMAND, &cmd);
	if (!(cmd & PCI_COMMAND_MEMORY) || !pci_resource_len(pdev, 0) ||
	    !(pci_resource_flags(pdev, 0) & IORESOURCE_MEM))
		return -ENODEV;
	regs = ioremap(pci_resource_start(pdev, 0), 0x20);
	if (!regs)
		return -ENOMEM;
	for (n = 0; n < 2; n++) {
		writel(((VC_NVM_WORD_ID + n) << VC_EERD_ADDR_SHIFT) | VC_EERD_START,
		       regs + VC_E1000_EERD);
		for (i = 0; i < 1000; i++) {
			v = readl(regs + VC_E1000_EERD);
			if (v & VC_EERD_DONE)
				break;
			udelay(5);
		}
		if (!(v & VC_EERD_DONE)) {
			iounmap(regs);
			return -ETIMEDOUT;
		}
		w[n] = v >> VC_EERD_DATA_SHIFT;
	}
	iounmap(regs);
	return 0;
}

static int vc_read_nvm_id_locked(struct pci_dev *pdev, u16 *w)
{
	int err;

	device_lock(&pdev->dev);
	err = vc_read_nvm_id(pdev, w);
	device_unlock(&pdev->dev);
	return err;
}

/* VBSP-REQ-12: identify by DMI and by the I354 NVM signature; refuse otherwise */
static int vc_board_supported(void)
{
	const char *board = dmi_get_system_info(DMI_BOARD_NAME);
	const char *ver = dmi_get_system_info(DMI_BOARD_VERSION);
	struct pci_dev *pdev;
	u16 nvm[2] = { 0, 0 };
	int i, err;

	if (!board || (strcmp(board, "EDGE520") && strcmp(board, "EDGE540"))) {
		pr_info(DRV_NAME ": board \"%s\" is not a VeloCloud EDGE520/EDGE540; touching no hardware\n",
			board ? board : "(none)");
		return -ENODEV;
	}
	if (!ver || ver[0] != '2' || ver[1] != '.') {
		pr_info(DRV_NAME ": %s board version \"%s\" is not 2.x (rev A boards use another pin map and are not supported); touching no hardware\n",
			board, ver ? ver : "(none)");
		return -ENODEV;
	}
	pdev = pci_get_domain_bus_and_slot(0, 0, VC_LPC_DEVFN);
	if (!pdev || pdev->vendor != PCI_VENDOR_ID_INTEL || pdev->device != VC_LPC_DEVICE) {
		pr_info(DRV_NAME ": 00:1f.0 is not the Atom C2000 LPC bridge 8086:%04x; touching no hardware\n",
			VC_LPC_DEVICE);
		pci_dev_put(pdev);
		return -ENODEV;
	}
	pci_dev_put(pdev);
	for (i = 0; i < VC_I354_NFUNC; i++) {
		pdev = pci_get_domain_bus_and_slot(0, 0, PCI_DEVFN(VC_I354_SLOT, i));
		if (!pdev || pdev->vendor != PCI_VENDOR_ID_INTEL ||
		    pdev->device != VC_I354_DEVICE) {
			pr_info(DRV_NAME ": 00:14.%d is not an I354 SGMII function 8086:%04x; touching no hardware\n",
				i, VC_I354_DEVICE);
			pci_dev_put(pdev);
			return -ENODEV;
		}
		if (i == 0) {
			err = vc_read_nvm_id_locked(pdev, nvm);
			if (!err && (nvm[0] != VC_NVM_ID_VC || nvm[1] != VC_NVM_ID_5X)) {
				pr_info(DRV_NAME ": I354 NVM words 0x06/0x07 are %04x/%04x, not Vc5X (%04x/%04x); touching no hardware\n",
					nvm[0], nvm[1], VC_NVM_ID_VC, VC_NVM_ID_5X);
				pci_dev_put(pdev);
				return -ENODEV;
			}
			if (err)
				pr_warn(DRV_NAME ": I354 NVM signature not readable (%d); relying on DMI and PCI IDs, igb checks it again at probe\n",
					err);
			else
				pr_info(DRV_NAME ": I354 NVM signature Vc5X (%04x/%04x)\n",
					nvm[0], nvm[1]);
		}
		pci_dev_put(pdev);
	}
	pr_info(DRV_NAME ": %s board version %s supported\n", board, ver);
	return 0;
}

/* ---------------------------------------------------------- init/exit -- */

static void vc_teardown(void)
{
	if (vc_mii) {
		mdiobus_unregister(vc_mii);
		mdiobus_free(vc_mii);
		vc_mii = NULL;
	}
	if (vc_i2c_added) {
		i2c_del_adapter(&vc_i2c_adap);
		vc_i2c_added = false;
	}
	if (vc_pdev) {
		platform_device_unregister(vc_pdev);
		vc_pdev = NULL;
	}
	if (vc_region_held) {
		/* leave the bus pins released (inputs); MDC too */
		vc_sc_update(VC_SC_IO_SEL, VC_BUS_PINS, VC_BUS_PINS);
		release_region(vc_gbase, VC_SC_REGION_LEN);
		vc_region_held = false;
	}
}

static int __init vc_board_init(void)
{
	int err;

	err = vc_board_supported();
	if (err)
		return err;

	err = vc_gpio_init();
	if (err)
		return err;

	vc_pdev = platform_device_register_simple(DRV_NAME, -1, NULL, 0);
	if (IS_ERR(vc_pdev)) {
		err = PTR_ERR(vc_pdev);
		vc_pdev = NULL;
		goto fail;
	}

	err = vc_i2c_init(&vc_pdev->dev);
	if (err) {
		pr_err(DRV_NAME ": I2C adapter registration failed: %d\n", err);
		goto fail;
	}

	vc_pca18_reset();

	err = vc_mdio_init(&vc_pdev->dev);
	if (err) {
		pr_err(DRV_NAME ": MDIO bus registration failed: %d\n", err);
		goto fail;
	}
	pr_info(DRV_NAME ": ready: %s, MDIO bus %s\n", vc_i2c_name, VC_MDIO_BUS_ID);
	return 0;
fail:
	vc_teardown();
	return err;
}

static void __exit vc_board_exit(void)
{
	vc_teardown();
}

module_init(vc_board_init);
module_exit(vc_board_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("VeloCloud Edge 5x0 board buses (bit-banged I2C and WAN PHY MDIO) and reset release");
