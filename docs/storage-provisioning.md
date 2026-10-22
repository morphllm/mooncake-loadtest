# Storage-server provisioning and qualification

Use this checklist for a CPU/RAM/NVMe Mooncake store. A same-host TCP result does
not qualify remote KV performance. Preserve the existing management connection
while bringing up the data fabric.

## Hardware and fabric

Record DMI identity, current drive serials, PCIe topology, memory population,
NUMA nodes, HCA model/firmware, negotiated link width/speed and active port state.
Rebuild the data-drive allowlist after vendor intervention. Never erase a boot,
root or swap ancestor. When destructive qualification is authorized, erase data
namespaces, initialize the measured extent with noncompressible data, and verify
its CRC before reporting read bandwidth. Reads of unwritten LBAs are insufficient.

Require a working high-speed data link and explicit HCA selection. InfiniBand
needs a subnet manager; RoCE needs a deliberately configured Ethernet fabric.
Do not change link mode, firmware, MTU or switch congestion settings by guesswork.
First establish host-memory `ib_write_bw`/`ib_read_bw`, then the Mooncake Transfer
Engine, then real offloaded-object restores. Retain negotiated speed, counters,
errors and payload bytes with each result. GPU clients additionally need their
own verified GPUDirect path; a CPU storage-node test cannot establish that.

The [ConnectX-7 manual](https://networking-docs.nvidia.com/connectx7hw/introduction)
documents PCIe Gen5 and Socket Direct configurations for Gen4 hosts. A card's
400 Gb/s label does not override its host PCIe link: Gen4 x16 has about 31.5 GB/s
of encoded one-way bandwidth before transaction overhead. A 23-drive array at
5.5 GB/s per drive has a 126.5 GB/s arithmetic sum, not a promise that one HCA can
export it. Size and benchmark the complete path.

## Process and memory placement

Prefer one store leg per NUMA node, each with its local data drives and, ideally,
a local HCA. Use a separate filesystem/directory per leg. Pin its CPU and memory
placement and verify `/proc/PID/numa_maps`; record cross-socket paths that remain.
The runner derives TCP lane placement when all backing NVMes share a NUMA node;
mixed backing is not treated as local. The current benchmark uses an independent
master per lane; its aggregate is a local multi-lane ceiling, not evidence that
one shared remote pool or router integration is qualified.

[AMD's EPYC 7003 tuning guidance](https://docs.amd.com/api/khub/documents/jyZAYhV9MdtcpY8ruukQsA/content)
explains that NPS changes locality and channel interleaving. NPS4 is a benchmark
candidate, not a universal requirement. Start with the measured topology and
avoid a BIOS reboot merely to apply an untested recipe. Verify memory-channel
population and compare measured memory bandwidth with the intended workload.

For pinned workloads, test the performance governor and disabled automatic NUMA
balancing. Record their prior values and restore them after the experiment.
Keep thermal/error monitoring active. Do not disable kernel or transport fast
paths to make a failing test pass.

## Hugepages and registered I/O

[Mooncake's deployment guide](https://kvcache-ai.github.io/Mooncake/deployment/mooncake-store-deployment-guide.html)
documents `MC_STORE_USE_HUGEPAGE=1` and `MC_STORE_HUGEPAGE_SIZE=2MB` for explicit
HugeTLB allocation. Reserve pages on the intended NUMA nodes **before** starting
the processes. Verify actual `Private_Hugetlb`/`Shared_Hugetlb` backing, not just
the environment variable. A reservation failure must remain a failed prerequisite.

For this two-leg test, 32 GiB of 2 MiB pages per NUMA node covers two 16 GiB store
segments plus leg/client buffers and headroom. This is a benchmark budget, not
production capacity sizing. For production, budget total resident memory across
segments, staging, clients, master indexes and OS at roughly 80–85% of measured
RAM; do not allocate that percentage independently to every process.

The [SSD offload guide](https://kvcache-ai.github.io/Mooncake/deployment/ssd/ssd-offload.html)
requires sufficient locked memory for registered buffers. Set service/container
`LimitMEMLOCK=infinity` / `--ulimit memlock=-1`, then inspect the live process limit.
Keep `MOONCAKE_OFFLOAD_USE_URING=true`, O_DIRECT, registered slots and buffer reuse;
fail qualification on registration or POSIX-I/O fallback. The current fixed-slot
implementation deliberately keeps SSD staging on ordinary pages. HugeTLB for
store/client memory is a separate setting, not a replacement for that staging fix.

Do not globally enable a large `MC_MMAP_ARENA_POOL_SIZE`: it is **per process** and
can multiply the reservation across clients. Do not enable a private local cache
and count its hits as shared-store or disk restores. Keep local memcpy enabled
where appropriate, but label same-host copies separately from remote RDMA.

## NVMe-oF is a different backend

Mooncake also documents an [SPDK NVMe-oF SSD pool](https://kvcache-ai.github.io/Mooncake/deployment/ssd/nvmf-ssd-deployment-guide.html).
It requires a `USE_NOF=ON` build, SPDK targets, explicit namespace registration,
RDMA and DMA-capable buffers; it is not enabled by tuning the filesystem backend.
Evaluate it against the same initialized data and workload when that backend is
selected. Use an explicit verified data-device PCI allowlist: never let a target
setup script discover and bind every unmounted device. Keep boot and management
access intact. HugeTLB-backed shared buffers need the documented per-process
SPDK registration on both endpoints. A passing filesystem-offload test does not
qualify NoF, and a NoF microbenchmark does not prove end-to-end KV restore speed.

## Evidence required from the tuned run

- Exact launcher/runtime provenance, command, profile and per-process limits.
- NUMA placement, HugeTLB backing, fixed-buffer registration and no fallback.
- Per-drive initialized read speeds, latency, serials, SMART/error deltas and
  coverage; full-capacity acceptance remains separate from a bounded test extent.
- Cold registration/startup cost separately from warm steady-state operations.
- Actual SSD restore objects/bytes and phase-aligned physical reads.
- Explicit transport, HCA and negotiated fabric speed; successful API calls alone
  do not prove RDMA was used.

Use the [system profile helper](../scripts/system_profile.py) to apply reversible
host settings around a test and retain before/applied/after evidence. It does not
provision the network or replace the checks above. Keep it outside active drive
benchmarks so settings do not change midway through a comparison.
