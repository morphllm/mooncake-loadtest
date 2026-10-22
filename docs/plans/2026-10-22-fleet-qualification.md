# Resumable storage-server qualification

The next hour: consolidate the verified drive and pool operations, add bounded parallel SSH orchestration, validate failure/resume behavior, and publish the tested change. Today: keep both existing hardware runs intact, collect their evidence, and distinguish tested-region results from full media acceptance. No production serving changes.

## Before

```text
┌─ Operator laptop ──────────────────────────────────────────────────────────┐
│ ┌─────────────────────┐     SSH:22 · ad hoc commands · management Ethernet │
│ │ SSH + temporary     │┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐ │
│ │ sequencing scripts  │                                                   ┆ │
│ └─────────────────────┘                                                   ┆ │
└───────────────────────────────────────────────────────────────────────────┆─┘
┌─ Physical storage server ──────────────────────────────────────────────────┆─┐
│ ┌─────────────────────┐  syscall:— · test plan · local CPU                 ▼ │
│ │ Individual helpers  │┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐  ┌──────────────────┐ │
│ └─────────────────────┘                          ┆  │ Mooncake runner  │ │
│ ┌─────────────────────┐  PCIe:— · blocks · NVMe  ▼  └──────────────────┘ │
│ │ Verified data NVMe  │◀━━━━━━━━━━━━━━━━━━━━━━━━━━━┘                     │
│ └─────────────────────┘                                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

```text
mooncake-loadtest.sh
scripts/system_profile.py
/tmp/.../pool-provision.py, recover-pool.py, read-speed-health.py,
         boot-read.py, short-selftests.py, sequencing shell scripts
```

## After

```text
┌─ Operator laptop ─────────────────────────────────────────────────────────┐
│ ┌─────────────────────┐ SSH:22 · pinned bundle + explicit node plan · mgmt │
│ │ [+] fleet.py        │┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐ │
│ │ bounded parallelism │◀┄┄ SSH:22 · evidence + hashes · management ┄┄┄┄┄┄┄┄┆ │
│ └─────────────────────┘                                                   ┆ │
└───────────────────────────────────────────────────────────────────────────┆─┘
┌─ Each physical storage server, independently locked ──────────────────────┆─┐
│ ┌──────────────────────┐   syscall:— · stage state · local CPU            ▼ │
│ │ [+] qualify_node.py  │┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐ │
│ │ persistent evidence  │                                                ┆ │
│ └──────────────────────┘                                                ┆ │
│ ┌──────────────────────┐  syscall:— · guarded jobs · local CPU          ▼ │
│ │ [~] canonical drive  │┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐ ┌─────────────────┐ │
│ │ checks + pool module │                           ┆ │ Mooncake runner │ │
│ └──────────────────────┘                           ┆ │ + host profile  │ │
│ ┌──────────────────────┐ PCIe:— · verified blocks ▼ └─────────────────┘ │
│ │ Explicit data NVMe   │◀━━━━━━━━━━━━━━━━━━━━━━━━━━━┘                    │
│ │ Boot: read-only      │                                                 │
│ └──────────────────────┘                                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

Dashed edges are control; solid edges are I/O. This offline workflow has no HTTP admission or 429 decision. SSH is control only; it does not qualify a KV data fabric.

```text
mooncake-loadtest.sh             # canonical emitted acceptance logic remains
scripts/
  system_profile.py             # existing reversible host tuning
  drive_checks.py               # isolated reads and short self-tests
  storage_pool.py               # homogeneous-sector pools, recovery, cleanup
  qualify_node.py               # persistent ordered per-host workflow
  fleet.py                      # parallel SSH stage/run/status/collection
tests/test_{drive_checks,storage_pool,qualify_node,fleet}.py
docs/fleet-qualification.md
```

## Gates and ownership

- Explicit current DMI, full physical inventory, data serial/capacity allowlist and boot serial. No inferred wipe authorization. `run --apply --erase-data` is required for destructive qualification.
- Full-capacity CRC and sampled initialized-region speed are separate evidence. A short read or clean SMART does not substitute for full media coverage.
- Persistent stage records and unique output paths. Resume verifies evidence and ownership; it never repeats a completed erase or adopts an unknown LV. In-flight host lock prevents overlapping workloads.
- Pool grouping uses NUMA node and logical sector size. A single-drive group uses a linear LV. LVM segment reports use `seg`; cleanup rechecks recorded UUIDs.
- Node failures remain visible while independent nodes continue. No host-key bypass, private-key transfer, embedded credentials, or automatic sudo policy changes.
- Explicit transport and tested RDMA gate. A TCP diagnostic remains unqualified for remote RDMA. Never turn off fast paths to obtain a pass.
- Retain reports, raw evidence, commands, bundle hashes, and scoped verdicts. Cleanup removes only owned temporary test resources; SSH remains available.

## Progress

- [x] Current 46 data-drive speed/CRC-region reports preserved; both live Mooncake runs left untouched.
- [x] Consolidate drive checks and pool ownership/recovery.
- [x] Implement and test node stages and parallel fleet commands.
- [x] Document one-shot usage and acceptance limits.
- [ ] Run Linux checks, review, commit and push main.
- [ ] Complete hardware qualification and verified vendor reporting; keep automation active until finished.

Owners: root integrates the node workflow and validates release; storage_pool agent owns pools; drive_checks agent owns drive checks; fleet agent owns SSH orchestration. Temporary helper deletion condition: after their live runs and evidence harvest are complete, replace them with the maintained modules and remove only task-owned copies.

Validation: 118 Linux fixtures pass, including exact CRC proof, boot protection, owned pool layout/cleanup, checkpoint recovery, parallel node failure isolation and artifact collection. The new fleet workflow has not yet completed an end-to-end physical full-capacity run; existing vendor runs remain separate evidence. Hardware acceptance is still false after the controlled one-second offload retry.
