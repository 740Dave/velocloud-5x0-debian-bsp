# SPDX-License-Identifier: GPL-2.0
# velo5x0-bsp: VeloCloud Edge 5x0 board and switch modules (out of tree).
obj-m := vc-edge5x0-board.o vc-edge5x0-switch.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
