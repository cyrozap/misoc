from operator import itemgetter

from migen import *

from misoc.cores import lm32, mor1kx, identifier, timer, uart
from misoc.interconnect import wishbone, csr_bus, wishbone2csr
from misoc.integration.config import Config


__all__ = ["mem_decoder", "SoCCore", "soc_core_args", "soc_core_argdict"]


def mem_decoder(address, start=26, end=29):
    return lambda a: a[start:end] == ((address >> (start+2)) & (2**(end-start))-1)


class SoCCore(Module):
    csr_map = {
        "crg":            0,  # user
        "uart_phy":       1,  # provided by default (optional)
        "uart":           2,  # provided by default (optional)
        "identifier_mem": 3,  # provided by default (optional)
        "timer0":         4,  # provided by default (optional)
        "buttons":        5,  # user
        "leds":           6,  # user
    }
    interrupt_map = {
        "uart":   0,
        "timer0": 1,
    }
    mem_map = {
        "rom":      0x00000000,  # (default shadow @0x80000000)
        "sram":     0x10000000,  # (default shadow @0x90000000)
        "main_ram": 0x40000000,  # (default shadow @0xc0000000)
        "csr":      0x60000000,  # (default shadow @0xe0000000)
    }
    def __init__(self, platform, clk_freq,
                cpu_type="lm32", cpu_reset_address=0x00000000,
                integrated_rom_size=0,
                integrated_sram_size=4096,
                integrated_main_ram_size=16*1024,
                shadow_base=0x80000000,
                csr_data_width=8, csr_address_width=14,
                with_uart=True, uart_baudrate=115200,
                uart_type="real",
                ident="",
                with_timer=True):
        self.platform = platform
        self.clk_freq = clk_freq

        self.cpu_type = cpu_type
        if integrated_rom_size:
            cpu_reset_address = 0
        self.cpu_reset_address = cpu_reset_address

        self.integrated_rom_size = integrated_rom_size
        self.integrated_sram_size = integrated_sram_size
        self.integrated_main_ram_size = integrated_main_ram_size

        self.with_uart = with_uart
        self.uart_baudrate = uart_baudrate

        self.shadow_base = shadow_base

        self.csr_data_width = csr_data_width
        self.csr_address_width = csr_address_width

        self._memory_regions = []  # list of (name, origin, length)
        self._csr_regions = []  # list of (name, origin, busword, csr_list/Memory)
        self._constants = []  # list of (name, value)

        self._wb_masters = []
        self._wb_slaves = []

        self.config = Config()

        if cpu_type == "lm32":
            self.submodules.cpu = lm32.LM32(platform, self.cpu_reset_address)
        elif cpu_type == "or1k":
            self.submodules.cpu = mor1kx.MOR1KX(platform, self.cpu_reset_address)
        else:
            raise ValueError("Unsupported CPU type: {}".format(cpu_type))
        self.add_wb_master(self.cpu.ibus)
        self.add_wb_master(self.cpu.dbus)

        if integrated_rom_size:
            self.submodules.rom = wishbone.SRAM(integrated_rom_size, read_only=True)
            self.register_rom(self.rom.bus, integrated_rom_size)

        if integrated_sram_size:
            self.submodules.sram = wishbone.SRAM(integrated_sram_size)
            self.register_mem("sram", self.mem_map["sram"], self.sram.bus, integrated_sram_size)

        # Note: Main Ram can be used when no external SDRAM is available and use SDRAM mapping.
        if integrated_main_ram_size:
            self.submodules.main_ram = wishbone.SRAM(integrated_main_ram_size)
            self.register_mem("main_ram", self.mem_map["main_ram"], self.main_ram.bus, integrated_main_ram_size)

        self.submodules.wishbone2csr = wishbone2csr.WB2CSR(
            bus_csr=csr_bus.Interface(csr_data_width, csr_address_width))
        self.register_mem("csr", self.mem_map["csr"], self.wishbone2csr.wishbone)

        if with_uart:
            if uart_type == "real":
                self.submodules.uart_phy = uart.RS232PHY(platform.request("serial"), clk_freq, uart_baudrate)
                self.submodules.uart = uart.UART(self.uart_phy)
            elif uart_type == "virtual":
                self.submodules.uart_phy = uart.S6VPHY()
                self.submodules.uart = uart.UART(self.uart_phy, phy_cd="jtag")
            else:
                raise ValueError("Unsupported UART type: {}".format(uart_type))

        if ident:
            self.submodules.identifier = identifier.Identifier(ident)
        self.config["CLOCK_FREQUENCY"] = int(clk_freq)

        if with_timer:
            self.submodules.timer0 = timer.Timer()

    def initialize_rom(self, data):
        self.rom.mem.init = data

    def add_wb_master(self, wbm):
        if self.finalized:
            raise FinalizeError
        self._wb_masters.append(wbm)

    def add_wb_slave(self, address_decoder, interface):
        if self.finalized:
            raise FinalizeError
        self._wb_slaves.append((address_decoder, interface))

    def add_memory_region(self, name, origin, length):
        def in_this_region(addr):
            return addr >= origin and addr < origin + length
        for n, o, l in self._memory_regions:
            if n == name or in_this_region(o) or in_this_region(o+l-1):
                raise ValueError("Memory region conflict between {} and {}".format(n, name))

        self._memory_regions.append((name, origin, length))

    def register_mem(self, name, address, interface, size=None):
        self.add_wb_slave(mem_decoder(address), interface)
        if size is not None:
            self.add_memory_region(name, address, size)

    def register_rom(self, interface, rom_size=0xa000):
        self.add_wb_slave(mem_decoder(self.mem_map["rom"]), interface)
        self.add_memory_region("rom", self.cpu_reset_address, rom_size)

    def get_memory_regions(self):
        return self._memory_regions

    def check_csr_region(self, name, origin):
        for n, o, l, obj in self._csr_regions:
            if n == name or o == origin:
                raise ValueError("CSR region conflict between {} and {}".format(n, name))

    def add_csr_region(self, name, origin, busword, obj):
        self.check_csr_region(name, origin)
        self._csr_regions.append((name, origin, busword, obj))

    def get_csr_regions(self):
        return self._csr_regions

    def get_constants(self):
        r = []
        for name, interrupt in sorted(self.interrupt_map.items(), key=itemgetter(1)):
            r.append((name.upper() + "_INTERRUPT", interrupt))
        r += self._constants
        return r

    def do_finalize(self):
        registered_mems = {regions[0] for regions in self._memory_regions}
        for mem in "rom", "sram":
            if mem not in registered_mems:
                raise FinalizeError("CPU needs a {} to be registered with register_mem()".format(mem))

        # Wishbone
        self.submodules.wishbonecon = wishbone.InterconnectShared(self._wb_masters,
            self._wb_slaves, register=True)

        # CSR
        self.submodules.csrbankarray = csr_bus.CSRBankArray(self,
            lambda name, memory: self.csr_map[name if memory is None else name + "_" + memory.name_override],
            data_width=self.csr_data_width, address_width=self.csr_address_width)
        self.submodules.csrcon = csr_bus.Interconnect(
            self.wishbone2csr.csr, self.csrbankarray.get_buses())
        for name, csrs, mapaddr, rmap in self.csrbankarray.banks:
            self.add_csr_region(name, (self.mem_map["csr"] + 0x800*mapaddr) | self.shadow_base, self.csr_data_width, csrs)
        for name, memory, mapaddr, mmap in self.csrbankarray.srams:
            self.add_csr_region(name + "_" + memory.name_override, (self.mem_map["csr"] + 0x800*mapaddr) | self.shadow_base, self.csr_data_width, memory)
        for name, constant in self.csrbankarray.constants:
            self._constants.append(((name + "_" + constant.name).upper(), constant.value.value))

        # Interrupts
        for k, v in sorted(self.interrupt_map.items(), key=itemgetter(1)):
            if hasattr(self, k):
                self.comb += self.cpu.interrupt[v].eq(getattr(self, k).ev.irq)

    def build(self, *args, **kwargs):
        self.platform.build(self, *args, **kwargs)


def soc_core_args(parser):
    parser.add_argument("--cpu-type", default=None,
                        help="select CPU: lm32, or1k")
    parser.add_argument("--integrated-rom-size", default=None, type=int,
                        help="size/enable the integrated (BIOS) ROM")
    parser.add_argument("--integrated-main-ram-size", default=None, type=int,
                        help="size/enable the integrated main RAM")


def soc_core_argdict(args):
    r = dict()
    for a in "cpu_type", "integrated_rom_size", "integrated_main_ram_size":
        arg = getattr(args, a)
        if arg is not None:
            r[a] = arg
    return r
