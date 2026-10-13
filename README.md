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

# Render an existing report again.
sudo bash mooncake-loadtest.sh report /opt/mooncake-loadtest/runs/RUN
```

Every run writes `report.txt`, `report.json`, raw samples, configuration, logs and an adjacent `.tar.gz`. The compact traffic table appears first. Completed phases also save `client/bench.json.partial.jsonl`; worker exits and phase timeouts produce a failed report rather than waiting forever. Exit status: 0 no failed checks; 1 failed checks; 2 setup failure. A skipped store phase cannot certify performance.

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

Unit checks cover actual put/get memory transfers, successful-byte accounting, misses, deadline-bounded refusals, incomplete responses, shared mixed windows, dead workers, scoped process cleanup, rolling accepted-key reads, managed runtime reuse, real fio job parsing and read-only enforcement, and report rendering. Real RDMA/L3 hardware qualification is a separate server run; unit tests cannot establish its throughput.
