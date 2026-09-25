# Builds `meshtastic.mpy`: the Rust codec plus a MicroPython shim. This takes
# the library's own name rather than an underscored one, because the migration
# is moving the Python layers into it a subsystem at a time -- when the last of
# them lands, this file *is* the library, and renaming it then would break every
# import that had already been written against it.
#
# Structurally identical to ../../lr1121/natmod/Makefile; see the comments there
# for why each piece is the way it is.

MPY_DIR ?= ../../../circuitpython

SRC = meshtastic.c

# Two boards, two instruction sets, one module. The default is the Thumb build
# the nRF52840 board needs; `make TARGET=xtensa` builds the same codec for the
# ESP32-S3 on the Meshnology W12. A natmod carries machine code, so neither
# .mpy will load on the other's board -- they are separate artifacts, and each
# gets its own MOD name and BUILD directory so that building one does not
# silently replace the other, and so neither needs a clean in between.
#
# Only the name differs: copy whichever one matches the board to lib/ as
# `meshtastic.mpy`, because the import name comes from the filename on the
# drive rather than from anything inside the file.
TARGET ?= thumb

ifeq ($(TARGET),xtensa)
MOD = meshtastic-xtensa
BUILD = build-xtensa
ARCH = xtensawin
# dynruntime.mk hardcodes the original ESP32 prefix and resolves libgcc/libm at
# include time, so this has to win before then. The generic `xtensa-esp-elf-`
# driver must not be used: with no chip dynconfig it builds big-endian Xtensa.
override CROSS = xtensa-esp32s3-elf-
RUST_TARGET ?= xtensa-esp32s3-none-elf
# The `esp` fork, not plain nightly: upstream rustc has no Xtensa backend. It
# is an older cargo, so nothing here may use core features newer than it.
RUST_TOOLCHAIN ?= esp
else
MOD = meshtastic
ARCH = armv7emsp
override CROSS = arm-none-eabi-
RUST_TARGET ?= thumbv7em-none-eabihf
RUST_TOOLCHAIN ?= nightly
endif

#: Which archive members carry the crate. Anything else in libmeshtastic.a --
#: core and compiler_builtins -- is left behind: each is one code generation
#: unit and arrives whole, so taking compiler_builtins for the sake of memcpy
#: alone costs 130 kB. See the memcpy note in meshtastic.c for what happens
#: instead.
RUST_MEMBERS ?= ^meshtastic

LINK_RUNTIME = 1

RUST_CRATE_DIR = ..
RUST_LIB = $(RUST_CRATE_DIR)/../target/$(RUST_TARGET)/release/libmeshtastic.a
RUST_OBJ = $(BUILD)/rust_meshtastic.o

SRC_O += $(RUST_OBJ)

include $(MPY_DIR)/py/dynruntime.mk

# The panic shim is localized because -Zbuild-std gives the synthetic `__rustc`
# crate the same disambiguator in every build, so two crates built this way
# define the same symbol. Harmless while each is its own .mpy; a multiple
# definition once both are linked into one firmware image.
# Combined with `ld -r` rather than moved: SRC_O wants one object, and on some
# targets more than one member is taken.
$(RUST_OBJ): $(RUST_LIB)
	$(ECHO) "AR $@"
	$(Q)rm -rf $(BUILD)/rust-obj && mkdir -p $(BUILD)/rust-obj
	$(Q)cd $(BUILD)/rust-obj && $(CROSS)ar x $(abspath $(RUST_LIB)) \
		$$($(CROSS)ar t $(abspath $(RUST_LIB)) | grep -E '$(RUST_MEMBERS)')
	$(Q)$(CROSS)ld -r -o $@ $(BUILD)/rust-obj/*.o
	$(Q)$(CROSS)objcopy --wildcard --localize-symbol='*rust_begin_unwind*' $@

$(RUST_LIB): $(wildcard $(RUST_CRATE_DIR)/src/*.rs) $(RUST_CRATE_DIR)/Cargo.toml
	$(ECHO) "CARGO $(RUST_TARGET)"
	$(Q)cd $(RUST_CRATE_DIR) && cargo +$(RUST_TOOLCHAIN) build --release \
		-Zunstable-options \
		-Zbuild-std=core,compiler_builtins \
		-Zbuild-std-features=compiler-builtins-mem \
		--target $(RUST_TARGET)

.PHONY: rust-tests
rust-tests:
	$(Q)cd $(RUST_CRATE_DIR) && cargo +$(RUST_TOOLCHAIN) test
