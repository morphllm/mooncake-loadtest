# Mooncake load test

Install Mooncake, exercise real store puts and gets, and print the table report. No GitHub login, SSH key, repository credential, or private source is required on the machine. Reuses Morph's standalone hardware audit and store acceptance runner.

Linux, root and internet access are required. If no system Python 3.10–3.13 exists, the runner installs managed Python 3.13 under `/opt/mooncake-loadtest/python` through [uv](https://docs.astral.sh/uv/guides/install-python/), leaving the OS interpreter unchanged. Run on a storage test server with spare resources. The runner installs system packages and an isolated Python environment under `/opt/mooncake-loadtest`. It starts its own masters and store legs, uses unique scratch directories, and stops only processes recorded by that run. Drive formatting is excluded.

```sh
curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash
```

For repeatable runs, replace `main` in the URL with a reviewed full commit SHA and pin the Mooncake wheel:

```sh
curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/COMMIT_SHA/mooncake-loadtest.sh | sudo bash -s -- single --mooncake-version VERSION --protocol rdma --disk-dirs /mnt/scratch --segment-gb 8 --disk-cap-gb 32
```

No credentials are embedded in the launcher or transmitted to the server. Do not configure your GitHub account on test machines. Reports contain hardware inventory, IPs and logs; review those before sharing them publicly.

## One-shot server qualification

A compatible reviewed wheel must be supplied when the released CPU wheel lacks the registered-buffer fix; the launcher does not rebuild Mooncake or silently claim that the public wheel contains it. The default CPU wheel is `--wheel non-cuda`; `--mooncake-version` pins its version. For a reviewed custom CPU build, pass `--wheel-url https://HOST/PACKAGE.whl --wheel-sha256 SHA256`, or download it first and use `--wheel-file /path/build.whl --wheel-sha256 SHA256`; both normal installs and `--no-install` validate the requested package flavor/version, import, and master executable.

Pin the launcher to a reviewed commit and run it separately on **each physical server**. Keep the DMI service tag, drive count, and intended L3 backing count in the command so a Windows jump host, wrong SSH tab, or root-only scratch filesystem cannot qualify the storage node:

```sh
sudo bash mooncake-loadtest.sh single --expected-tag SERVICE_TAG --expected-drives 24 \
  --l3-drives 23 --disk-dirs /mnt/l3 \
  --protocol rdma --mooncake-version VERSION --segment-gb 4 --disk-cap-gb 32 \
  --out /opt/mooncake-loadtest/runs/UNIQUE_RUN
```

The example assumes `/mnt/l3` is an already-mounted aggregate backed by exactly 23 data drives. For individual mounts, list **all intended mounted data directories** in `--disk-dirs`, separated by commas. Every listed directory gets a store leg even if the node has only one NIC. Legs share NICs round-robin with separate ports; fabric comparisons count each physical NIC port once. `--l3-drives` counts unique underlying NVMe namespaces, so repeated paths on the boot drive cannot pass a 23-drive check. A single mounted array spanning all data drives can also satisfy that check. Provisioning and mounting the array is a separate operator action. The runner does not format drives. More legs divide the DRAM budget; explicit segment and disk sizes are per leg. The L3 capacity must exceed the DRAM segment. Existing output directories are rejected to keep samples and phase windows from different runs apart.

TCP diagnostic lanes without an RDMA device bind the store and same-host client to the backing NVMe NUMA node when every backing drive belongs to that node. Separate NUMA-local mounted groups can be listed in `--disk-dirs /mnt/l3-numa0,/mnt/l3-numa1`; `--l3-drives 23` still checks the combined backing count. Mixed or unknown backing nodes remain unbound with a warning. RDMA lanes retain their NIC locality. Check `server/lanes.json`, `client/lanes.json` and the setup log for the selected nodes.

A performance run is not full drive acceptance. The same downloaded launcher includes a guarded, full-capacity test:

```sh
sudo bash mooncake-loadtest.sh drives inventory --out /var/tmp/drive-inventory-UNIQUE
# Review inventory.json. Write an explicit JSON array containing ONLY data drives:
# [{"device":"/dev/nvme1n1","serial":"EXACT_SERIAL","bytes":15363000000000}]
sudo bash mooncake-loadtest.sh drives plan --expected-tag SERVICE_TAG \
  --allowlist /root/approved-data-drives.json --out /var/tmp/drive-plan-UNIQUE
# DESTRUCTIVE: erases every allowlisted data drive, then writes and reads its full capacity.
sudo systemd-run --unit=drive-acceptance-UNIQUE --service-type=exec \
  bash /root/mooncake-loadtest.sh drives run --expected-tag SERVICE_TAG \
  --allowlist /root/approved-data-drives.json --parallel 23 --apply \
  --out /var/tmp/drive-result-UNIQUE
```

The allowlist requires the live path, serial and exact byte capacity. The test checks them again before each erase, rejects boot/mounted/swap/held/open/read-only devices, and writes CRC32c across every byte including the tail. It saves per-drive fio results, before/after SMART and error logs, sampled health, and a result only passing with complete write/read byte counts and healthy evidence. Full-capacity testing can take hours. The boot disk remains protected: `drives os-read` reads it fully, and `drives scratch` verifies only an explicitly identified scratch LV; neither certifies full boot-disk writes. Use `drives --help` for these modes. Collect evidence before deleting only the exact test-owned scratch paths.

After acceptance, provision the intended L3 filesystems and run the guarded Mooncake command above. A failed or unreachable second server still needs its own completed run. Registered-buffer and restore-path fallbacks fail qualification instead of silently accepting slower results.

## Verified host tuning

See [storage provisioning](docs/storage-provisioning.md) for NUMA placement, registered I/O, hugepages and fabric checks. On a dedicated two-NUMA-node test server, wrap the reviewed launcher with `scripts/system_profile.py` to reserve 32 GiB of 2 MiB HugeTLB pages per node, set performance governors, disable automatic NUMA balancing, and verify process limits. It rejects active fio jobs and records/restores the prior settings; allocation failure prevents the benchmark from starting.

```sh
sudo python3 scripts/system_profile.py --expected-tag SERVICE_TAG \
  --evidence /var/tmp/system-profile-UNIQUE -- \
  bash mooncake-loadtest.sh single --expected-tag SERVICE_TAG --expected-drives 24 \
  --l3-drives 23 --disk-dirs /mnt/l3-numa0,/mnt/l3-numa1 \
  --protocol tcp --mooncake-version VERSION --segment-gb 16 --disk-cap-gb 64 \
  --out /opt/mooncake-loadtest/runs/UNIQUE_RUN
```

This example is a same-host TCP diagnostic. For remote RDMA, select active HCAs and size the page reservation for their actual NUMA placement. The profile is a bounded benchmark configuration, not production memory sizing. Check its runtime samples for actual HugeTLB backing and NUMA/CPU placement. It neither provisions drives nor activates a disconnected RDMA fabric.

## Separate store and load generator

Use a second server on the intended RDMA fabric to measure end-to-end serving traffic. Allow only your test client to reach the store/control ports; the temporary control endpoint is unauthenticated. It exposes inventory and can finish this test. Defaults: control TCP 50090; lane `i` uses ports starting at `50100 + 20*i` (master, metrics, store, transfer engine and fabric benchmarks).

```sh
# Storage server: waits for the client to finish.
curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash -s -- server --protocol rdma --disk-dirs /mnt/scratch --segment-gb 8 --disk-cap-gb 32

# Load generator: the same object size/profile, active RDMA NICs on the intended fabric.
curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash -s -- client --server SERVER_IP --protocol rdma --workers 16 --batch 4
```

Repeat with larger `--workers`, different `--batch` and the real KV object size to find the highest sustained successful bandwidth for your workload. The default object size is 21,233,664 bytes, a 1536-token MLA KV block (576 bytes × 24 layers). The sweep tests 1, one quarter of the worker pool, and the entire pool. Both pure puts and hot gets sweep concurrency; mixed traffic uses half writers and half readers of newly accepted puts and reports each direction plus their sum over the same measurement window.

| Table row | Measures |
| --- | --- |
| sustained put | Best observed successful unique inserts at tested concurrency after capacity pressure |
| hot get | Best observed successful reads of a recent numeric key cohort |
| L3 candidate get | One pass over a mid-fill cohort intended to require restores |
| mixed put / mixed get | Successful traffic in each direction while inserts compete with reads from a shared bounded ring of recently successful writer keys |
| mixed aggregate | Sum of directions over their common interval |

The table includes successful and offered GB/s, worker count, elapsed time, misses, API errors, sampled integrity mismatches, and final deadline refusals. Normal deadline refusals count as uncompleted offered work, separately from API faults. Offered bandwidth counts requested payload once per logical batch; retries are recorded separately. GB/s is decimal; latency is per batch. Integrity checks sample four 4 KiB spans per object.

**These are observed rates, not certified hardware peaks.** Same-host traffic is a diagnostic ceiling and may use local memory copies. An automatic TCP run does not qualify production RDMA. Recent numeric IDs do not prove insertion order or DRAM residency; mixed reads follow recently accepted puts through a ring capped at 10% of the segment (at most 4096 keys); misses remain visible. Misses are excluded from successful bytes and do not by themselves prove lost writes. Mid-fill keys may be restored or evicted: qualify disk-backed reads with the report's per-window disk attribution and store restore telemetry. Raw NVMe inventory does not mean those drives back the selected L3 filesystem. Root-backed L3 does not qualify an unmounted storage array.

The detailed report compares actual fabric, memory and filesystem ceilings, samples CPU/disk/NIC usage, identifies likely bottlenecks, and checks error-counter deltas. A rate still increasing at the highest tested concurrency is a lower bound. Use multiple clients if the load generator saturates first; this runner currently orchestrates one client host per server run.

## Budgets and reports

`quick` is the default. `standard` runs longer; `soak` can take hours. `--segment-gb` and `--disk-cap-gb` apply **per NIC lane**. L3 is clamped to 80% of available scratch space divided among lanes, less 4 GiB per lane for headroom. Quick caps the default DRAM segment at 16 GiB per lane. Existing filesystem data is retained. Test-owned L3 directories remain after the report: delete the exact paths listed in `server/info.json` after collecting the report. Never remove an entire mount.

```sh
# Smaller report-oriented run; skips separate hardware ceiling tests.
curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash -s -- single --segment-gb 4 --disk-cap-gb 16 --workers 8 --skip fabric,dram,nvme,fs

# Re-render captured evidence without rerunning the benchmark; preserve prior report files first.
sudo bash mooncake-loadtest.sh report /opt/mooncake-loadtest/runs/RUN
```

Drive health appears prominently beside throughput and in a final health table: serial, SMART critical warnings, media/error counts and deltas, wear, spare threshold, temperature, hours, unsafe shutdowns, thermal events, and the last recorded self-test result. Missing SMART evidence is UNKNOWN; a green “no SMART fault detected” means captured counters showed no fault, not ready to ship. Historical error-log entries require status-code inspection and may be administrative. Snapshot collection reads `nvme self-test-log`; it never starts a self-test, formats a drive, or resets counters. The public one-liner always downloads the latest report implementation.

Every run writes an offline `report.html`, plain `report.txt`, structured `report.json`, raw samples, configuration, logs and an adjacent `.tar.gz`. The HTML report shows summary metrics, every PUT/GET concurrency, mixed directions and their shared-window aggregate, batch latency, outcomes, sortable drives/NICs, and collapsible checks. It needs no network, CDN, or assets. Capacity refusals are amber; invalid samples remain visible with their measured rates. Check failures do not automatically diagnose defective hardware. Terminal tables use colors on a TTY unless `NO_COLOR` is set; saved text always stays plain. Missing evidence appears as a dash, not a measured zero. Completed phases also save `client/bench.json.partial.jsonl`; worker exits and phase timeouts produce a failed report rather than waiting forever. Exit status: 0 no failed checks; 1 failed checks; 2 setup failure. A skipped store phase cannot certify performance.

## Development

One canonical self-contained executable keeps the public raw-HTTPS launcher independent of GitHub tooling:

```text
Before: private scripts + mutable standalone gist -> manually install -> table
After:  reviewed public commit -> install + bounded puts/gets -> table + evidence

mooncake-loadtest.sh       # shell orchestrator and emitted Python/C payloads
README.md
tests/test_runner.py      # runs payload functions with a fake I/O boundary
.github/workflows/test.yml # fresh-checkout tests
```

```sh
sudo apt-get install -y fio # Linux runtime job validation
python -m pip install numpy
bash -n mooncake-loadtest.sh
shellcheck -S error mooncake-loadtest.sh
python -m unittest discover -s tests -v
```

Unit checks cover actual put/get memory transfers, successful-byte accounting, misses, deadline-bounded refusals, incomplete responses, shared mixed windows, dead workers, scoped process cleanup, rolling accepted-key reads, managed runtime reuse, real fio job parsing/read-only enforcement, HTML escaping, numeric report units, shared mixed windows, and report rendering with `NO_COLOR`. Real RDMA/L3 hardware qualification is a separate server run; unit tests cannot establish its throughput.
