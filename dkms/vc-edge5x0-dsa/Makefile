# SPDX-License-Identifier: GPL-2.0
# velo5x0-bsp: VeloCloud Edge 5x0 switches as DSA (out of tree).  Needs a
# kernel with CONFIG_NET_DSA_MV88E6XXX (the velo5x0-bsp +velo5x0dsa kernel).
obj-m := vc-edge5x0-dsa.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
