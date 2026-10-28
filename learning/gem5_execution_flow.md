# gem5 仿真执行流详解

本文以本目录的 `x86_timing_3level_ddr4.py` 为例，讲清楚一件事：

> Python 里那句 `Simulator(board=board).run()`，究竟是怎样一步步进入 C++ 仿真内核，
> 把第一个事件放进事件队列，并最终执行到二进制第一条指令的。

所有 `文件:行号` 都对应本仓库当前版本（gem5 26.0）的源码。

---

## 0. 一句话版本

```
你的配置脚本
  → gem5 用 exec() 执行它（构建 Python 侧的 SimObject 树）
  → Simulator.run() 触发 instantiate：C++ 对象创建 + initState
      └─ Process::initState() 里 tc->activate()，把「取指事件」排在 tick 0
  → m5.simulate() → C++ simulate() → doSimLoop() → EventQueue::serviceOne()
  → 取到 tick 0 的 fetchEvent → TimingSimpleCPU::fetch() → 取指
  → 译码 → 执行 → advancePC → 取指下一条 …
```

---

## 1. 三个世界

| 层 | 代码位置 | 干什么 |
|---|---|---|
| ① 配置层 | `learning/x86_timing_3level_ddr4.py`、`learning/three_level_cache_hierarchy.py` | 用 Python 描述硬件结构（CPU、cache、DRAM、端口连接） |
| ② 控制层 | `src/python/gem5/simulate/`、`src/python/m5/simulate.py` | 实例化、启动、进入/退出仿真、处理退出事件、写统计 |
| ③ 内核层 | `src/sim/`、`src/cpu/`、`src/mem/` | 事件队列、CPU 模型、cache/DRAM 模型，全部是 C++ |

gem5 的仿真内核里**没有"主循环遍历所有对象"这种写法**，它只做一件事：不断从事件队列里取出
最早的事件并执行它。所谓"仿真"，就是事件按 tick 顺序被消费的过程。

---

## 2. 阶段 A：脚本是怎么被 gem5 执行的

`build/X86/gem5.opt learning/x86_timing_3level_ddr4.py` 启动后：

1. `src/python/m5/main.py:646` 附近把脚本所在目录插到 `sys.path`
   （正因如此我们才可以在脚本里直接 `from three_level_cache_hierarchy import ...`）。
2. `src/python/m5/main.py:662` 读文件、`compile()`，然后在 `main.py:687` 用
   `exec(filecode, scope)` 执行，其中：
   - `__name__ = "__m5_main__"`
   - `__file__ = 脚本路径`

所以脚本里 `if __name__ == "__m5_main__":` 这样的判断是 gem5 的约定；
普通 `python xxx.py` 是跑不起来的（没有 `m5` 模块）。

脚本顶层代码（我们写的是模块级顺序执行）此时**只是在造 Python 对象**，没有任何仿真发生：

```python
processor = SimpleProcessor(...)                 # 造 BaseCPU 的 Python 壳
cache_hierarchy = PrivateL1PrivateL2SharedL3CacheHierarchy(...)
memory = DualChannelDDR4_2400(size="4GiB")
board = SimpleBoard(...)
board.set_se_binary_workload(...)                # 造 Process 对象
Simulator(board=board).run()                     # 从这里才开始真正的工作
```

---

## 3. 阶段 B：SimObject 树长什么样

`set_se_binary_workload()`（`src/python/gem5/components/boards/se_binary_workload.py`）做了两件关键的事：

- 创建 `Process` SimObject（内含可执行文件、命令行、环境变量）；
- `core.workload = process`（`base_cpu_core.py:140`），于是 Process 变成 CPU 的子节点。

跑起来之后，SimObject 树大致是：

```
Root
└── board: SimpleBoard
    ├── processor
    │   └── cores.core: TimingSimpleCPU
    │       ├── icache_port / dcache_port（端口，不是子节点）
    │       └── workload: Process          ← initState 时激活线程的就是它
    ├── cache_hierarchy
    │   ├── l3-cache          : L3Cache
    │   ├── l2-cache-0        : L2Cache
    │   ├── l1i-cache-0       : L1ICache
    │   ├── l1d-cache-0       : L1DCache
    │   ├── l2buses[0]        : L2XBar
    │   ├── l3bus             : SystemXBar
    │   └── membus            : SystemXBar
    └── memory: ChanneledMemory
        ├── mem_ctrl[0]（每个通道一个 MemCtrl）
        └── mem_ctrl[1]
```

端口连接是在 `PrivateL1PrivateL2SharedL3CacheHierarchy.incorporate_cache()`
（`learning/three_level_cache_hierarchy.py`）里写的：

```
core.icache_port → l1i.cpu_side
core.dcache_port → l1d.cpu_side
l1i.mem_side / l1d.mem_side → l2buses[0].cpu_side_ports
l2.cpu_side → l2buses[0].mem_side_ports
l2.mem_side → l3bus.cpu_side_ports
l3.cpu_side → l3bus.mem_side_ports
l3.mem_side → membus.cpu_side_ports
membus.mem_side_ports → mem_ctrl[*].port
```

这些赋值只是把 Python 侧的端口对象连起来，真正的 C++ 端口绑定发生在实例化阶段。

---

## 4. 阶段 C：实例化（`Simulator.run()` 的前半段）

`Simulator.run()` 在 `src/python/gem5/simulate/simulator.py:610`，第一步是
`self._instantiate()`（同文件 `:562`）。

### 4.1 `board._pre_instantiate()`

`src/python/gem5/components/boards/abstract_board.py:484`：

1. `_connect_things()` → 依次调用
   `memory.incorporate_memory()`、`cache_hierarchy.incorporate_cache()`、
   `processor.incorporate_processor()`，并设置内存地址范围；
2. 创建 `Root` 对象（SimObject 树的根）；
3. 调用各组件的 `_pre_instantiate(root)`。

### 4.2 `_fix_all_objects()` + `_dump_configs()`

`src/python/m5/simulate.py:78` 修正父子关系、给参数填默认值；
`:96` 把 `m5out/config.ini`、`config.json`、`config.dot` 写出来
（**这就是实例化前就能看到完整配置的原因**）。

### 4.3 `_create_cpp_objects()` —— 关键的五遍

`src/python/m5/simulate.py:208`，注释里写得很清楚：

| 遍 | 代码 | 作用 |
|---|---|---|
| 1 | `obj.createCCObject()`（`:224`） | 为每个 SimObject 调用 C++ 构造函数，`TimingSimpleCPU`、`Cache`、`MemCtrl` 等 C++ 对象在此诞生 |
| 2 | `_bind_all_ports(root)`（`:232`） | 把 Python 侧连好的端口真正 bind 到 C++ 端口 |
| 3 | `_m5_core.initAll(...)`（`:241`） | 逐个调用 `SimObject::init()`，例如 `BaseCPU::init()`（`src/cpu/base.cc:337`）、`Process::init()` |
| 4 | `root.regStats()` / `regProbePointsAll()` / `regProbeListenersAll()` | 统计项、探针注册 |
| 5 | `_m5_core.initStateAll(...)`（`:262`） | 逐个调用 `SimObject::initState()`，**激活线程就发生在这里** |

### 4.4 线程是在哪里被"点火"的

这是整个流程里最关键、也最容易忽略的一步：

```
_m5_core.initStateAll()                       src/python/m5/simulate.py:262
└─ Process::initState()                      src/sim/process.cc:290
   └─ tc->activate()                          src/sim/process.cc:299  ← 注释写着 "mark this context as active so it will start ticking"
      └─ SimpleThread::activate()             src/cpu/simple_thread.cc:131
         └─ TimingSimpleCPU::activateContext() src/cpu/simple/timing.cc:208
            └─ schedule(fetchEvent, clockEdge(Cycles(0)))   src/cpu/simple/timing.cc:220
               ⇒ 一个 tick = 0 的事件被放进事件队列
```

注意这里的顺序无关紧要：`activate()` 只是**把事件排进队列**，
此刻 `curTick()` 还是 0，事件循环尚未开始。所以 x86 的
`X86_64Process::initState()` 之后才执行的 `argsInit()`
（`src/arch/x86/process.cc:159`，其中 `:1043` 用
`tc->pcState(getStartPC())` 设置入口 PC、设置 RSP）仍然能赶在
事件被消费之前生效。

顺带一提，`fetchEvent` 的定义在 `src/cpu/simple/timing.cc:79`：

```cpp
fetchEvent([this]{ fetch(); }, name())
```

它是个 `EventFunctionWrapper`，`process()` 就是执行这个 lambda。

### 4.5 `_post_instantiate()`

`abstract_board.py:462`，给各组件一个"实例化完成"的钩子。

到这一步为止：C++ 对象都有了，统计项都注册了，线程已激活，tick 0 的取指事件已入队。
**但一条指令都还没执行。**

---

## 5. 阶段 D：进入 C++ 事件循环

`Simulator.run()` 随后进入 `while True` 循环（`simulator.py:638`），每次调用
`m5.simulate(self.get_max_ticks())`（`:644`）。

### 5.1 Python 侧 `m5.simulate()`

`src/python/m5/simulate.py:315`，第一次调用会做三件事：

```python
if need_startup:                       # :322
    for obj in root.descendants():
        obj.startup()                  # :325  BaseCPU::startup()、设备 startup()
    atexit.register(stats.dump)        # :330  退出时写 stats.txt
    atexit.register(_m5_core.doExitCleanup)
    stats.reset()                      # :336  统计归零

sim_out = _m5_event.simulate(*args)    # :345  真正进入 C++
```

`BaseCPU::startup()`（`src/cpu/base.cc:376`）只做电源状态、进度事件之类的准备工作，
**不再负责启动线程**——启动线程是 4.4 节那次 `activate()` 的事。

### 5.2 Python → C++ 的桥

`_m5_event` 是 pybind11 模块，绑定点在
`src/python/pybind11/event.cc:110`：

```cpp
m.def("simulate", &simulate, py::arg("ticks") = MaxTick);
```

`Simulator` 默认 `max_ticks = m5.MaxTick`（`simulator.py:105`），
所以传进去的是一个极大值，即"一直跑到有退出事件为止"。

### 5.3 C++ `simulate()`

`src/sim/simulate.cc:193`：

1. `initSigInt()` / `initSigCont()`：装 Ctrl-C 处理；
2. `set_max_tick(MaxTick)`：创建并调度 **`GlobalSimLoopExitEvent`**。
   这是队列里第二个事件，排在 `MaxTick`（实际不会触发），
   作用是**保证事件队列永远非空**，循环里的 `assert(!eventq->empty())` 才成立；
3. `num_cycles != -1` 时按需改 max tick；
4. 多事件队列（并行仿真）时还要排一个 `quantum_event`；
5. `doSimLoop(mainEventQueue[0])`。

### 5.4 `doSimLoop()`

`src/sim/simulate.cc:296`：

```cpp
curEventQueue(eventq);
eventq->handleAsyncInsertions();
while (1) {
    assert(!eventq->empty());
    ...处理 async 事件（Ctrl-C、stats dump、IO）...
    Event *exit_event = eventq->serviceOne();   // :342
    if (exit_event != NULL)
        return exit_event;
}
```

`EventQueue::serviceOne()`（`src/sim/eventq.cc:222`）是所有仿真的核心：

```cpp
Event *event = head;              // 取最早的事件
...
head = next;                      // 出队
if (!event->squashed()) {
    setCurTick(event->when());    // :244  ★ 时间推进
    event->process();             // :247  ★ 虚函数分发
    if (event->isExitEvent()) return event;
}
event->release();
```

`setCurTick()` 之后，`curTick()` 就是这条事件的时间戳——gem5 的"时间"
就是这样一格一格被事件推进的，不存在"每个 tick 都跑一遍"。

---

## 6. 阶段 E：第一个事件

事件队列里此刻（简化后）有两个事件：

| when | 事件 | 说明 |
|---|---|---|
| 0 | `TimingSimpleCPU::fetchEvent` | `EventFunctionWrapper`，回调 `fetch()` |
| `MaxTick` | `GlobalSimLoopExitEvent` | 保险丝，保证队列不空 |

所以 `serviceOne()` 弹出的第一个事件就是 **tick 0 的 `fetchEvent`**：

```
EventQueue::serviceOne()            src/sim/eventq.cc:222
└─ setCurTick(0)                    :244
└─ event->process()                 :247
   └─ EventFunctionWrapper::process()
      └─ TimingSimpleCPU::fetch()   src/cpu/simple/timing.cc:679
```

在 `--debug-flags=Event` 下，`eventq.cc:246` 会给这条事件打一行 `executed` 的 trace。

---

## 7. 阶段 F：从 fetch 到第一条指令执行

### 7.1 `TimingSimpleCPU::fetch()`（`timing.cc:679`）

```
swapActiveThread()                        // 多线程时切换
checkForInterrupts() / checkPcEventQueue()
needToFetch = true
setupFetchRequest(ifetch_req)             // 见下
thread->mmu->translateTiming(req, tc, &fetchTranslation, BaseMMU::Execute)
```

`setupFetchRequest()`（`src/cpu/simple/base.cc:323`）决定"一次取多少字节"：

```cpp
Addr fetchPC = (instAddr & decoder->pcMask()) + t_info.fetchOffset;
req->setVirt(fetchPC, decoder->moreBytesSize(), Request::INST_FETCH, ...);
```

x86 的 `Decoder::MachInst` 是 `uint64_t`（`src/arch/x86/decoder.hh:78`），
所以 `moreBytesSize() == 8`、`pcMask()` 掩掉低 3 位：
**每次取指是 8 字节对齐的一块**，指令可以跨块，不够时再取下一块
（`preExecute()` 里 `decoder->moreBytes()` 会用到 `t_info.fetchOffset`）。

### 7.2 地址翻译与发送请求

`translateTiming()` 走 ITLB（SE 模式下是 `SEWorklist`/`EmulationPageTable`，
命中就直接给出物理地址，未命中触发缺页处理），完成后回调
`TimingSimpleCPU::sendFetch()`（`timing.cc:721`）：

```cpp
ifetch_pkt = new Packet(req, MemCmd::ReadReq);
ifetch_pkt->dataStatic(decoder->moreBytesPtr());
if (!icachePort.sendTimingReq(ifetch_pkt)) { _status = IcacheRetry; }
else                                       { _status = IcacheWaitResponse; }
```

`icachePort` 是 `TimingCPUPort`，它的 `sendTimingReq` 一路往下走到 L1I：

```
IcachePort → Cache::recvTimingReq()        src/mem/cache/cache.cc:419
           → BaseCache::recvTimingReq()    src/mem/cache/base.cc:454
              命中：立刻安排响应
              缺失：memSidePort → CoherentXBar::recvTimingReq()  src/mem/coherent_xbar.cc:149
                    → L2 → L3 → membus → MemCtrl → DRAM
```

这条链路上每个组件都是"收到请求 → 查表 → 要么响应要么往下转发"，
每一跳都会产生新的、带时间戳的事件，所以延迟是**真实建模**出来的。

### 7.3 响应回到 CPU

```cpp
IcachePort::recvTimingResp(pkt)                 timing.cc:911
└─ tickEvent.schedule(pkt, cpu->clockEdge())    timing.cc:927   // 延到下一个 CPU 时钟沿
   └─ ITickEvent::process()                     timing.cc:905
      └─ TimingSimpleCPU::completeIfetch(pkt)   timing.cc:821
```

`_status` 状态机就是在这里流转的（`src/cpu/simple/base.hh:108`）：
`Idle / Running / Faulting / ITBWaitResponse / IcacheRetry / IcacheWaitResponse /
DTBWaitResponse / DcacheRetry / DcacheWaitResponse / ...`

### 7.4 译码与执行（`completeIfetch`）

```cpp
preExecute();                                  // 译码
if (curStaticInst->isMemRef()) {
    fault = curStaticInst->initiateAcc(&t_info, traceData);  // load/store：发 dcache 请求
} else {
    fault = curStaticInst->execute(&t_info, traceData);      // 普通指令：直接执行
}
advanceInst(fault);
```

`preExecute()`（`src/cpu/simple/base.cc:347`）里做了三件事：

1. `decoder->moreBytes(pc, fetchPC)`：把刚取到的 8 字节喂给解码器；
2. `decoder->decode(pc_state)`：得到 `StaticInstPtr`；
3. 如果指令是 macroop（x86 常见），取出第一个 microop 放到 `curStaticInst`。

`curStaticInst->execute()` 是虚函数，最终落到 ISA 生成代码里对应指令的
`execute()`（例如 `Endbr64`、`Xor`、`Mov` 等类），指令语义就在那里执行。

`advanceInst()`（`timing.cc:755`）最后：

```cpp
if (!t_info.stayAtPC) advancePC(fault);   // 更新 PC
...
if (_status == BaseSimpleCPU::Running)
    fetch();                              // 直接取下一条
```

> 小结论：`fetchEvent` 只负责"点火"和重试/异常场合；
> 稳态下 CPU 是被 **cache 响应事件**驱动的，`advanceInst()` 会直接调用下一次 `fetch()`。
> 这就是 TimingSimpleCPU 名字里 "timing" 的含义：功能上顺序执行，
> 但每次访存都要等内存系统的时序响应。

### 7.5 一条指令的完整时间线（示意）

```
tick 0        fetchEvent.process() → fetch()
tick 0        ITLB 翻译（同步/近乎瞬时）→ sendTimingReq → L1I
tick N        L1I 命中 → recvTimingResp → tickEvent @ clockEdge()
tick N+k      ITickEvent.process() → completeIfetch()
              preExecute() 译码 → execute()/initiateAcc()
              advancePC() → fetch() 下一条
```

---

## 8. 第一条指令到底是什么

入口 PC 来自 `X86_64Process::argsInit()` 的 `tc->pcState(getStartPC())`
（`src/arch/x86/process.cc:1043`），即 ELF 的 `e_entry`。本目录的 `hello`：

```console
$ readelf -h learning/hello | grep Entry
  Entry point address:               0x4017c0

$ objdump -d --start-address=0x4017c0 learning/hello | head
00000000004017c0 <_start>:
  4017c0:  f3 0f 1e fa      endbr64
  4017c4:  31 ed            xor    %ebp,%ebp
  4017c6:  49 89 d1         mov    %rdx,%r9
  ...
```

所以：

- 第一条 **取指请求**：虚拟地址 `0x4017c0`，长度 8 字节（低 3 位被 `pcMask` 清零）；
- 第一条 **执行的指令**：`endbr64`（4 字节，`f3 0f 1e fa`），x86 解码器把它译成
  `HINT_NOP`（`hint`）微操作；
- 之后是 `xor %ebp,%ebp` … 一路走到 `__libc_start_main`，最终调用到我们的 `main()`。

用 `--debug-flags=Exec` 可以直接看到（真实输出）：

```console
$ build/X86/gem5.opt --debug-flags=Exec --debug-start=0 -d /tmp/m5-exec \
    learning/x86_timing_3level_ddr4.py 2>&1 | grep -m 3 0x4017c
  77589: board.processor.cores.core: T0 : 0x4017c0 @_start    : hint
  77589: board.processor.cores.core: T0 : 0x4017c0 @_start. 0 :   HINT_NOP : fault   NoFault : No_OpClass :
  78588: board.processor.cores.core: T0 : 0x4017c4 @_start+4    : xor	ebp
```

两个细节很能说明问题：

- 取指事件在 **tick 0** 触发，但第一条指令直到 **tick 77589** 才执行
  （ticks 是皮秒，77589 ps ≈ 77.6 ns）。这段差值就是"冷启动"的代价：
  ITLB 页表遍历 + 指令第一次必须从 DRAM 取回来。
- 第二条指令只晚 999 ps（3 个 3 GHz 周期），因为指令块已经在 L1I 里了。

---

## 9. 阶段 G：退出

程序执行到 `exit_group` 系统调用时：

```
syscall 处理                        src/sim/syscall_emul.cc:259
└─ exitSimulationLoopClassic("exiting with last active thread context", code)
   └─ 调度 GlobalSimLoopExitEvent（when = curTick）
      └─ EventQueue::serviceOne() 返回它（isExitEvent() == true）
         └─ doSimLoop() return
            └─ C++ simulate() 返回该事件给 Python
               └─ Simulator.run() 的 exit handler 分派
                  → run() 返回
                     → Python 解释器退出
                        → atexit: stats.dump 写出 m5out/stats.txt
                        → atexit: doExitCleanup
```

`Simulator.run()` 里的分派逻辑在 `simulator.py:644-676`：
根据 `ExitEvent` 对应的 hypercall ID 找到处理器（默认 `ClassicGeneratorExitHandler`），
由它决定"继续跑"还是"结束"。

---

## 10. 关键代码索引表

| 阶段 | 函数 | 位置 |
|---|---|---|
| 执行配置脚本 | `main()` | `src/python/m5/main.py:646,662,687` |
| 连接组件 + 建 Root | `AbstractBoard::_pre_instantiate` | `src/python/gem5/components/boards/abstract_board.py:484` |
| 创建 C++ 对象 | `_create_cpp_objects` | `src/python/m5/simulate.py:208` |
| C++ 构造 | `createCCObject()` | `src/python/m5/simulate.py:224` |
| `init()` | `initAll` | `src/python/m5/simulate.py:241` |
| `initState()` | `initStateAll` | `src/python/m5/simulate.py:262` |
| 激活线程 | `Process::initState` | `src/sim/process.cc:290` |
| → | `SimpleThread::activate` | `src/cpu/simple_thread.cc:131` |
| → | `TimingSimpleCPU::activateContext` | `src/cpu/simple/timing.cc:208` |
| 排入取指事件 | `schedule(fetchEvent, ...)` | `src/cpu/simple/timing.cc:220` |
| 进入仿真 | `m5.simulate` | `src/python/m5/simulate.py:315` |
| pybind 桥 | `m.def("simulate", ...)` | `src/python/pybind11/event.cc:110` |
| C++ 仿真入口 | `simulate(Tick)` | `src/sim/simulate.cc:193` |
| 幂等保险丝 | `set_max_tick` | `src/sim/simulate.cc:259` |
| 事件循环 | `doSimLoop` | `src/sim/simulate.cc:296` |
| 取一个事件 | `EventQueue::serviceOne` | `src/sim/eventq.cc:222` |
| 时间推进 | `setCurTick` | `src/sim/eventq.cc:244` |
| 事件分发 | `event->process()` | `src/sim/eventq.cc:247` |
| 第一次取指 | `TimingSimpleCPU::fetch` | `src/cpu/simple/timing.cc:679` |
| 构造取指请求 | `BaseSimpleCPU::setupFetchRequest` | `src/cpu/simple/base.cc:323` |
| L1I 入口 | `Cache::recvTimingReq` | `src/mem/cache/cache.cc:419` |
| 取指响应 | `IcachePort::recvTimingResp` | `src/cpu/simple/timing.cc:911` |
| 译码 | `BaseSimpleCPU::preExecute` | `src/cpu/simple/base.cc:347` |
| 更新 PC | `BaseSimpleCPU::advancePC` | `src/cpu/simple/base.cc:525` |
| 设置入口 PC | `X86Process::argsInit` | `src/arch/x86/process.cc:1043` |

---

## 11. 自己动手观察

```sh
# 1) 看事件流（第一条事件、时间推进）
build/X86/gem5.opt --debug-flags=Event --debug-file=/tmp/event.log \
    -d learning/m5out learning/x86_timing_3level_ddr4.py

# 2) 看取指/CPU 状态机
build/X86/gem5.opt --debug-flags=SimpleCPU,Fetch -d /tmp/m5-fetch \
    learning/x86_timing_3level_ddr4.py

# 3) 看真正执行的指令（第一条就是 _start 的 endbr64）
build/X86/gem5.opt --debug-flags=Exec --debug-start=0 -d /tmp/m5-exec \
    learning/x86_timing_3level_ddr4.py 2>&1 | head

# 4) 看实例化后生成的完整配置
less learning/m5out/config.ini
```

可用的 flag 名在 `build/X86/debug/` 目录下（每个 `.hh` 就是一个 flag）。

---

## 12. 几个常见的误解

**Q：为什么 `Process::initState()` 里先 `activate()`，后面才设置 PC？会不会取到 PC=0？**

不会。`activate()` 只是把事件排到 tick 0，事件循环要等所有 `initState()` 跑完、
`doSimLoop()` 真正开始后才消费事件。此时 `argsInit()` 早已把 PC 设成 `0x4017c0`。

**Q：是不是每个 tick 都会"过一遍"所有 SimObject？**

不是。只有被调度的事件才会执行，`setCurTick()` 直接把时间跳到事件的时间戳。
没有事件的 tick 是不存在的。

**Q：`AtomicSimpleCPU` 和 `TimingSimpleCPU` 在流程上差在哪？**

流程骨架完全一样（同样的 `activateContext` → 事件 → `fetch`）。
区别在于 `AtomicSimpleCPU::fetch()` 用 `sendAtomic()` 同步拿到数据，
在一次事件里把整条指令做完；而 `TimingSimpleCPU` 发 `sendTimingReq()` 后返回，
靠后续的 cache 响应事件推进，因此能真实体现 cache/DRAM 的延迟。

**Q：为什么事件队列里必须永远有个事件？**

`doSimLoop()` 里有 `assert(!eventq->empty())`。`GlobalSimLoopExitEvent`（at
`MaxTick`）就是这个"兜底"事件，同时也承担"跑到上限就退出"的职责。
