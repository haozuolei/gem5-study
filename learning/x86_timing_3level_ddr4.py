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

"""x86 TimingSimpleCPU + three-level classic cache + DDR4, SE mode.

Run with::

    build/X86/gem5.opt learning/x86_timing_3level_ddr4.py

All parameters are fixed below; edit them in place.
"""

import os

# gem5 puts this script's directory on sys.path before executing it, so the
# sibling helper module can just be imported.
from three_level_cache_hierarchy import (
    PrivateL1PrivateL2SharedL3CacheHierarchy,
)

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.simulator import Simulator

# TimingSimpleCPU models cache and memory latency, unlike AtomicSimpleCPU.
processor = SimpleProcessor(
    cpu_type=CPUTypes.TIMING,
    isa=ISA.X86,
    num_cores=1,
    clk_freq="3GHz",
)

# L1I 32KiB/8-way, L1D 48KiB/12-way, private L2 512KiB/8-way,
# shared L3 16MiB/16-way, stride prefetcher on every level.
cache_hierarchy = PrivateL1PrivateL2SharedL3CacheHierarchy(
    l1i_size="32KiB",
    l1d_size="48KiB",
    l2_size="512KiB",
    l3_size="16MiB",
)

# DDR4-2400, two channels interleaved on 64-byte boundaries.
memory = DualChannelDDR4_2400(size="4GiB")

board = SimpleBoard(
    clk_freq="3GHz",
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)
board.set_se_binary_workload(
    BinaryResource(
        local_path=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hello"
        )
    )
)

Simulator(board=board).run()
