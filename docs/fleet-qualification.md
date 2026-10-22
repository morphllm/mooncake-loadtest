# Qualify storage servers in parallel

Use `scripts/fleet.py` from a clean, committed checkout. It stages that exact source over existing SSH aliases and runs one persistent systemd service per physical server. The current tuning profile requires a dedicated two-NUMA-node Linux host. Each server proceeds independently; the coordinator limits concurrent SSH operations. Disconnecting the laptop does not stop a launched test.

This workflow **erases every explicitly listed data drive**. It never writes the boot drive. Inventory and review are mandatory: pin current DMI serial, boot serial, and every data namespace's path, serial, and exact bytes. A drive move or replacement requires a new reviewed configuration and run ID.

## Configuration

A fleet JSON contains `schema: 1`, a unique `run_id`, and `nodes`. Start with identity-only nodes for inventory:

```json
{
  "schema": 1,
  "run_id": "acceptance-20261022",
  "nodes": [
    {"name": "storage1", "ssh": "storage1", "sudo": true, "expected_tag": "REPLACE_WITH_DMI_SERIAL"},
    {"name": "storage2", "ssh": "storage2", "sudo": true, "expected_tag": "REPLACE_WITH_DMI_SERIAL"}
  ]
}
```

SSH must already authenticate, verify the host key, and reach Linux with root or separately configured noninteractive sudo. The tool does not change SSH, firewall, or sudo policy. Do not put passwords or keys in the JSON.

For each node, add the reviewed fields below. The two-drive example illustrates the schema; a 24-drive server requires exactly 23 allowlist entries. Do not infer a writable allowlist automatically from an inventory.

```json
{
  "expected_drives": 2,
  "boot_serial": "REPLACE_WITH_BOOT_SERIAL",
  "allowlist": [
    {"device": "/dev/nvme1n1", "serial": "REPLACE_WITH_DATA_SERIAL", "bytes": 15360950534144}
  ],
  "protocol": "tcp",
  "require_rdma": true,
  "nics": [],
  "profile": "standard",
  "pool_gib": 128,
  "segment_gib": 8,
  "disk_cap_gib": 64,
  "workers": 16,
  "batch": 4,
  "offload_heartbeat_seconds": 1,
  "minimum_read_gbps": 5.5,
  "wheel": {
    "flavor": "non-cuda",
    "version": "REPLACE_WITH_EXACT_VERSION",
    "file": "/absolute/remote/path/to/approved.whl",
    "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"
  }
}
```

`wheel.file` is an existing **remote** file, not a laptop upload. Alternatively use a pinned HTTPS `wheel.url` without embedded credentials. Keep private wheels outside the public repository and collected artifact directory. The workflow creates a separate runtime directory for dependencies.

## Run

```sh
python3 scripts/fleet.py inventory --config fleet.json --out ./evidence
python3 scripts/fleet.py plan --config fleet.json --out ./evidence
python3 scripts/fleet.py plan --config fleet.json --out ./evidence --remote
python3 scripts/fleet.py run --config fleet.json --out ./evidence --apply --erase-data
python3 scripts/fleet.py status --config fleet.json --out ./evidence
python3 scripts/fleet.py collect --config fleet.json --out ./evidence
python3 scripts/fleet.py cleanup --config fleet.json --out ./evidence --apply --resume
```

Consult `python3 scripts/fleet.py --help` for output-directory and parallelism options. Review the local plan before staging; the remote plan performs identity and occupancy checks without writing media. A run uses a separate artifact directory from planning, so the destructive phase cannot silently reuse a preview.

The ordered node workflow is:

1. Verify live identity, exact full inventory, boot ancestry, and reviewed data targets.
2. Discard supported data namespaces; write and CRC-verify their entire reported capacity concurrently. Unsupported discard is recorded; full writes still replace the entire data namespace.
3. Measure each data drive in isolation: three direct `io_uring` reads, 128 KiB, queue depth 32, 30 seconds plus 5 seconds warmup. Use a NUMA-local CPU and restore the original governor.
4. Provision temporary LVM/XFS groups split by NUMA node and logical sector size. Singleton groups are linear; larger groups striped. Record exact PV/VG/LV/filesystem UUIDs.
5. Run the canonical Mooncake suite on every group with reversible hugepage/NUMA/limit tuning and explicit transport. Keep cold registration, steady-state, SSD restore and physical-read evidence.
6. Read the boot drive without erasing it, including full-capacity read coverage; run supported short controller self-tests on all drives.
7. Emit a per-serial speed/health table and an acceptance JSON with separate evidence gates.

Full writes plus verification across tens of terabytes take hours. Progress and raw outputs remain on each host; a launched service is not a pass.

## Resume, verdicts and cleanup

Use the same configuration and run ID with `run --apply --erase-data --resume`. Completed stages are reused only when their saved evidence still matches. A crash after a verifiably completed stage can be recovered. An interrupted erase or uncertain pool mutation is preserved for review instead of replayed or adopted. Do not delete its state to bypass a refusal.

`acceptance_ready` requires full data CRC, all repeated data and boot reads above the configured threshold, clean boot-read evidence, supported successful controller self-tests and passing Mooncake gates. A same-host run **never qualifies remote RDMA**, even when it uses an RDMA device. `require_rdma` defaults true and holds overall acceptance false until a separately verified cross-host result exists. Explicitly setting false means local storage acceptance only; report that scope. Use the canonical server/client workflow in the README for cross-host testing over the intended high-speed fabric.

The node profile sets `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=1` by default; verify each leg logs the requested interval. Builds supporting SSD offload use this interval to discover persistence work. The pinned version must support the setting; retain its startup logs.

A full memory segment causing capacity refusals is a workload/capacity result, not proof of a slow SSD. Do not relax the gate or disable fast paths to manufacture a pass. Review the measured phase and telemetry, adjust the declared workload only with an explained new run, and retain the earlier result.

Collect reports before cleanup. Cleanup verifies ownership UUIDs and removes only the test pool; it preserves evidence, SSH and unrelated storage. Never remove a runtime needed by an active service. Fleet collection includes only artifacts, not dependency runtimes or the wheel file. Review collected logs for secrets before publishing them.
