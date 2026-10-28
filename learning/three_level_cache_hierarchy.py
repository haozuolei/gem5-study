# Copyright (c) 2026 The gem5 study project
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""A classic (non-Ruby) three-level cache hierarchy.

Topology (one core)::

    core
     |-- L1I --+
     |-- L1D --+-- L2 -- L3 -- membus -- DDR4

The standard library ships a two-level classic hierarchy
(``PrivateL1PrivateL2CacheHierarchy``) but no classic three-level one, so this
module builds one out of the regular ``Cache`` SimObjects.
"""

from typing import Optional

from m5.objects import (
    NULL,
    BadAddr,
    BasePrefetcher,
    BaseXBar,
    Cache,
    L2XBar,
    StridePrefetcher,
    SystemXBar,
)
from m5.params import Port

from gem5.components.boards.abstract_board import AbstractBoard
from gem5.components.cachehierarchies.abstract_cache_hierarchy import (
    AbstractCacheHierarchy,
)
from gem5.components.cachehierarchies.classic.abstract_classic_cache_hierarchy import (
    AbstractClassicCacheHierarchy,
)
from gem5.components.cachehierarchies.classic.caches.l1dcache import L1DCache
from gem5.components.cachehierarchies.classic.caches.l1icache import L1ICache
from gem5.components.cachehierarchies.classic.caches.l2cache import L2Cache
from gem5.isas import ISA
from gem5.utils.override import overrides


class L3Cache(Cache):
    """A last-level cache.

    This is a plain ``Cache`` with last-level defaults: higher latency and
    more MSHRs than the L2. It keeps ``clusivity = mostly_incl`` so the L1/L2
    contents are normally a subset of the L3.
    """

    def __init__(
        self,
        size: str,
        assoc: int = 16,
        tag_latency: int = 20,
        data_latency: int = 20,
        response_latency: int = 20,
        mshrs: int = 32,
        tgts_per_mshr: int = 12,
        writeback_clean: bool = False,
        clusivity: str = "mostly_incl",
        PrefetcherCls: type[BasePrefetcher] = StridePrefetcher,
    ):
        super().__init__()
        self.size = size
        self.assoc = assoc
        self.tag_latency = tag_latency
        self.data_latency = data_latency
        self.response_latency = response_latency
        self.mshrs = mshrs
        self.tgts_per_mshr = tgts_per_mshr
        self.writeback_clean = writeback_clean
        self.clusivity = clusivity
        self.prefetcher = PrefetcherCls()


class PrivateL1PrivateL2SharedL3CacheHierarchy(AbstractClassicCacheHierarchy):
    """Private L1I/L1D/L2 per core, one shared L3 in front of memory.

    Three buses are used:

    * ``l2buses[i]`` : L1I/L1D <-> private L2 (and the table walker)
    * ``l3bus``      : all private L2s <-> shared L3
    * ``membus``     : L3 <-> memory controllers

    Every cache level gets a stride prefetcher by default. That is *hardware*
    prefetching, which is complementary to the software cache warm-up done by
    ``hello.c`` before it resets the statistics.
    """

    def _get_default_membus(self) -> SystemXBar:
        membus = SystemXBar(width=64)
        membus.badaddr_responder = BadAddr()
        membus.default = membus.badaddr_responder.pio
        return membus

    def __init__(
        self,
        l1i_size: str = "32KiB",
        l1i_assoc: int = 8,
        l1d_size: str = "48KiB",
        l1d_assoc: int = 12,
        l2_size: str = "512KiB",
        l2_assoc: int = 8,
        l3_size: str = "16MiB",
        l3_assoc: int = 16,
        enable_prefetcher: bool = True,
        membus: BaseXBar | None = None,
    ) -> None:
        """Create the hierarchy.

        :param l1i_size: L1 instruction cache size (e.g. ``"32KiB"``).
        :param l1i_assoc: L1 instruction cache associativity.
        :param l1d_size: L1 data cache size.
        :param l1d_assoc: L1 data cache associativity.
        :param l2_size: Private L2 size.
        :param l2_assoc: Private L2 associativity.
        :param l3_size: Shared L3 size.
        :param l3_assoc: Shared L3 associativity.
        :param enable_prefetcher: Attach a ``StridePrefetcher`` to every level.
        :param membus: Optional bus between L3 and memory.
        """
        AbstractClassicCacheHierarchy.__init__(self)

        self._l1i_size = l1i_size
        self._l1i_assoc = l1i_assoc
        self._l1d_size = l1d_size
        self._l1d_assoc = l1d_assoc
        self._l2_size = l2_size
        self._l2_assoc = l2_assoc
        self._l3_size = l3_size
        self._l3_assoc = l3_assoc
        self._enable_prefetcher = enable_prefetcher

        # Every cache created by this hierarchy, used to disable prefetching.
        self._caches: list[Cache] = []

        self.membus = membus if membus else self._get_default_membus()

    @overrides(AbstractClassicCacheHierarchy)
    def get_mem_side_port(self) -> Port:
        return self.membus.mem_side_ports

    @overrides(AbstractClassicCacheHierarchy)
    def get_cpu_side_port(self) -> Port:
        return self.membus.cpu_side_ports

    @overrides(AbstractCacheHierarchy)
    def incorporate_cache(self, board: AbstractBoard) -> None:
        # The system port lets the simulator peek/poke memory functionally.
        board.connect_system_port(self.membus.cpu_side_ports)

        for _, port in board.get_mem_ports():
            self.membus.mem_side_ports = port

        num_cores = board.get_processor().get_num_cores()

        # Bus between the private L2s and the shared L3.
        self.l3bus = SystemXBar(width=64)

        # The shared L3, wired to memory.
        self._l3_node = self.add_root_child(
            "l3-cache",
            L3Cache(size=self._l3_size, assoc=self._l3_assoc),
        )
        self._l3_node.cache.mem_side = self.membus.cpu_side_ports
        self._l3_node.cache.cpu_side = self.l3bus.mem_side_ports
        self._caches.append(self._l3_node.cache)

        # One L1I/L1D/L2 triple and one L1<->L2 bus per core.
        self.l2buses = [L2XBar() for _ in range(num_cores)]

        for i, cpu in enumerate(board.get_processor().get_cores()):
            l2_node = self._l3_node.add_child(
                f"l2-cache-{i}",
                L2Cache(size=self._l2_size, assoc=self._l2_assoc),
            )
            l1i_node = l2_node.add_child(
                f"l1i-cache-{i}",
                L1ICache(size=self._l1i_size, assoc=self._l1i_assoc),
            )
            l1d_node = l2_node.add_child(
                f"l1d-cache-{i}",
                L1DCache(size=self._l1d_size, assoc=self._l1d_assoc),
            )
            self._caches.extend(
                [l2_node.cache, l1i_node.cache, l1d_node.cache]
            )

            # L2 -> L3 bus, L1s -> L2 bus.
            l2_node.cache.mem_side = self.l3bus.cpu_side_ports
            l2_node.cache.cpu_side = self.l2buses[i].mem_side_ports

            l1i_node.cache.mem_side = self.l2buses[i].cpu_side_ports
            l1d_node.cache.mem_side = self.l2buses[i].cpu_side_ports

            cpu.connect_icache(l1i_node.cache.cpu_side)
            cpu.connect_dcache(l1d_node.cache.cpu_side)

            # The table walker behaves like another L1: it goes through L2.
            self._connect_table_walker(i, cpu)

            if board.get_processor().get_isa() == ISA.X86:
                # x86 needs explicit interrupt lines; other ISAs do not.
                cpu.connect_interrupt(
                    self.membus.mem_side_ports,
                    self.membus.cpu_side_ports,
                )
            else:
                cpu.connect_interrupt()

        if not self._enable_prefetcher:
            for cache in self._caches:
                cache.prefetcher = NULL

        if board.has_coherent_io():
            self._setup_io_cache(board)

    def _connect_table_walker(self, cpu_id: int, cpu) -> None:
        cpu.connect_walker_ports(
            self.l2buses[cpu_id].cpu_side_ports,
            self.l2buses[cpu_id].cpu_side_ports,
        )

    def _setup_io_cache(self, board: AbstractBoard) -> None:
        """Create a cache for coherent I/O connections."""
        self.iocache = Cache(
            assoc=8,
            tag_latency=50,
            data_latency=50,
            response_latency=50,
            mshrs=20,
            size="1KiB",
            tgts_per_mshr=12,
            addr_ranges=board.mem_ranges,
        )
        self.iocache.mem_side = self.membus.cpu_side_ports
        self.iocache.cpu_side = board.get_mem_side_coherent_io_port()
