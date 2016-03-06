from migen import *
from migen.genlib.record import Record
from migen.genlib.resetsync import AsyncResetSynchronizer
from migen.genlib.cdc import MultiReg

from misoc.interconnect.csr import *
from misoc.interconnect.csr_eventmanager import *
from misoc.interconnect.stream import Source, Sink, SyncFIFO, AsyncFIFO


class RS232PHYRX(Module):
    def __init__(self, pads, tuning_word):
        self.source = Source([("data", 8)])

        # # #

        uart_clk_rxen = Signal()
        phase_accumulator_rx = Signal(32)

        rx = Signal()
        self.specials += MultiReg(pads.rx, rx)
        rx_r = Signal()
        rx_reg = Signal(8)
        rx_bitcount = Signal(4)
        rx_busy = Signal()
        rx_done = self.source.stb
        rx_data = self.source.data
        self.sync += [
            rx_done.eq(0),
            rx_r.eq(rx),
            If(~rx_busy,
                If(~rx & rx_r,  # look for start bit
                    rx_busy.eq(1),
                    rx_bitcount.eq(0),
                )
            ).Else(
                If(uart_clk_rxen,
                    rx_bitcount.eq(rx_bitcount + 1),
                    If(rx_bitcount == 0,
                        If(rx,  # verify start bit
                            rx_busy.eq(0)
                        )
                    ).Elif(rx_bitcount == 9,
                        rx_busy.eq(0),
                        If(rx,  # verify stop bit
                            rx_data.eq(rx_reg),
                            rx_done.eq(1)
                        )
                    ).Else(
                        rx_reg.eq(Cat(rx_reg[1:], rx))
                    )
                )
            )
        ]
        self.sync += \
                If(rx_busy,
                    Cat(phase_accumulator_rx, uart_clk_rxen).eq(phase_accumulator_rx + tuning_word)
                ).Else(
                    Cat(phase_accumulator_rx, uart_clk_rxen).eq(2**31)
                )


class RS232PHYTX(Module):
    def __init__(self, pads, tuning_word):
        self.sink = Sink([("data", 8)])

        # # #

        uart_clk_txen = Signal()
        phase_accumulator_tx = Signal(32)

        pads.tx.reset = 1

        tx_reg = Signal(8)
        tx_bitcount = Signal(4)
        tx_busy = Signal()
        self.sync += [
            self.sink.ack.eq(0),
            If(self.sink.stb & ~tx_busy & ~self.sink.ack,
                tx_reg.eq(self.sink.data),
                tx_bitcount.eq(0),
                tx_busy.eq(1),
                pads.tx.eq(0)
            ).Elif(uart_clk_txen & tx_busy,
                tx_bitcount.eq(tx_bitcount + 1),
                If(tx_bitcount == 8,
                    pads.tx.eq(1)
                ).Elif(tx_bitcount == 9,
                    pads.tx.eq(1),
                    tx_busy.eq(0),
                    self.sink.ack.eq(1),
                ).Else(
                    pads.tx.eq(tx_reg[0]),
                    tx_reg.eq(Cat(tx_reg[1:], 0))
                )
            )
        ]
        self.sync += [
                If(tx_busy,
                    Cat(phase_accumulator_tx, uart_clk_txen).eq(phase_accumulator_tx + tuning_word)
                ).Else(
                    Cat(phase_accumulator_tx, uart_clk_txen).eq(0)
                )
        ]


class RS232PHY(Module, AutoCSR):
    def __init__(self, pads, clk_freq, baudrate=115200):
        self._tuning_word = CSRStorage(32, reset=int((baudrate/clk_freq)*2**32))
        self.submodules.tx = RS232PHYTX(pads, self._tuning_word.storage)
        self.submodules.rx = RS232PHYRX(pads, self._tuning_word.storage)
        self.sink, self.source = self.tx.sink, self.rx.source


class S6VPHY(Module, AutoCSR):
    def __init__(self):
        self.clock_domains.cd_jtag = ClockDomain()
        self.source = Source([("data", 8)])
        self.sink = Sink([("data", 8)])

        rx_done = self.source.stb
        rx_data = self.source.data

        tx_available = self.sink.stb
        tx_read = self.sink.ack
        tx_data = self.sink.data

        capture = Signal()
        tck = Signal()
        reset = Signal()
        sel = Signal()
        shift = Signal()
        tdi = Signal()
        update = Signal()
        tdo = Signal()

        jtag_bitcount = Signal(4)
        jtag_register_length = 10
        jtag_register = Signal(jtag_register_length)

        self.comb += self.cd_jtag.clk.eq(tck)
        self.specials += AsyncResetSynchronizer(self.cd_jtag, reset)
        self.specials += Instance("BSCAN_SPARTAN6", p_JTAG_CHAIN=2,
                                  o_CAPTURE=capture, o_DRCK=tck,  o_RESET=reset,
                                  o_RUNTEST=None, o_SEL=sel, o_SHIFT=shift,
                                  o_TCK=None, o_TDI=tdi, o_TMS=None,
                                  o_UPDATE=update, i_TDO=tdo)
        self.comb += tdo.eq(jtag_register[0])
        self.sync.jtag += [
            tx_read.eq(0),
            If(sel,
                If(update,
                    If(jtag_register[0] & (jtag_bitcount == jtag_register_length),
                        rx_data.eq(jtag_register[1:9]),
                        rx_done.eq(1)
                    )
                ).Elif(shift,
                    jtag_register.eq(Cat(jtag_register[1:], tdi)),
                    jtag_bitcount.eq(jtag_bitcount + 1),
                    If(tx_available & tdi & (jtag_bitcount == jtag_register_length-1),
                        tx_read.eq(1)
                    )
                ).Elif(capture,
                    jtag_bitcount.eq(0),
                    If(tx_available,
                        jtag_register.eq(Cat(1, tx_data))
                    ).Else(
                        jtag_register.eq(0)
                    )
                )
            )
        ]


def _get_uart_fifo(depth, sink_cd="sys", source_cd="sys"):
    if sink_cd != source_cd:
        fifo = AsyncFIFO([("data", 8)], depth)
        return ClockDomainsRenamer({"write": sink_cd, "read": source_cd})(fifo)
    else:
        return SyncFIFO([("data", 8)], depth)


class UART(Module, AutoCSR):
    def __init__(self, phy,
                 tx_fifo_depth=16,
                 rx_fifo_depth=16,
                 phy_cd="sys"):
        self._rxtx = CSR(8)
        self._txfull = CSRStatus()
        self._rxempty = CSRStatus()

        self.submodules.ev = EventManager()
        self.ev.tx = EventSourceProcess()
        self.ev.rx = EventSourceProcess()
        self.ev.finalize()

        # # #

        # TX
        tx_fifo = _get_uart_fifo(tx_fifo_depth, source_cd=phy_cd)
        self.submodules += tx_fifo

        self.comb += [
            tx_fifo.sink.stb.eq(self._rxtx.re),
            tx_fifo.sink.data.eq(self._rxtx.r),
            self._txfull.status.eq(~tx_fifo.sink.ack),
            Record.connect(tx_fifo.source, phy.sink),
            # Generate TX IRQ when tx_fifo becomes non-full
            self.ev.tx.trigger.eq(~tx_fifo.sink.ack)
        ]

        # RX
        rx_fifo = _get_uart_fifo(rx_fifo_depth, sink_cd=phy_cd)
        self.submodules += rx_fifo

        self.comb += [
            Record.connect(phy.source, rx_fifo.sink),
            self._rxempty.status.eq(~rx_fifo.source.stb),
            self._rxtx.w.eq(rx_fifo.source.data),
            rx_fifo.source.ack.eq(self.ev.rx.clear),
            # Generate RX IRQ when tx_fifo becomes non-empty
            self.ev.rx.trigger.eq(~rx_fifo.source.stb)
        ]
