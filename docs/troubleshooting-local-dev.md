# Local-dev troubleshooting (Apple Silicon / Docker Desktop)

## SIGILL (exit 132) on import of `cryptography` / `pypdf` / `simmer-sdk`

### Symptom
On an Apple-Silicon Mac running the local stack under Docker Desktop (or Colima), a
container process dies with **exit code 132 (SIGILL, illegal instruction)** the moment it
imports a module that (directly or transitively) loads OpenSSL. Common triggers:

- the **worker** exits 132 immediately after `Picked up job … (simmer_domain)` — `simmer_domain`
  imports `simmer_sdk`, whose `__init__` pulls the agentic stack → `cryptography` → OpenSSL;
- `import cryptography.hazmat.bindings._rust` → exit 132;
- `import pypdf` → exit 132 (pypdf loads `cryptography` for encrypted-PDF support).

The extract/classify path does **not** crash because it never imports OpenSSL.

### Cause
It is **not** a Mac/package incompatibility (these packages run fine on Macs everywhere) and
**not** an arch mismatch (everything is native `aarch64`, no emulation). It's a **virtual-CPU
feature mismatch**: Docker Desktop's lightweight ARM VM *advertises* crypto CPU features
(`sme` / `sve2`, visible in `/proc/cpuinfo` Features) that its guest kernel **cannot actually
execute**. OpenSSL (bundled inside `cryptography` ≥ 47) auto-probes those ARM capabilities and
emits accelerated crypto instructions that then **fault** on the virtualized CPU → SIGILL.

This is a known, documented class of issue:
- [docker/for-mac #7397 — SIGILL on Apple Silicon under the VM](https://github.com/docker/for-mac/issues/7397)
- [LocalStack docs — SIGILL on Apple Silicon + `OPENSSL_armcap=0` workaround](https://github.com/localstack/localstack-docs/pull/930)
- [openssl `crypto/armcap.c` — the `OPENSSL_armcap` override](https://github.com/openssl/openssl/blob/master/crypto/armcap.c)

### Fix
Set the environment variable **`OPENSSL_armcap=0`**, which disables OpenSSL's ARM
crypto-capability probe so it never reaches for the unsupported instructions. It only turns off
a crypto **acceleration** path — correctness is unchanged, and it's verified end-to-end
(cryptography, pypdf, and a full `simmer_domain` job all run under it).

It is applied for you in **`docker-compose.ollama.yml`** (the Mac-local ollama override), on both
the `orchestrator` and `worker` services. Rebuild/recreate to pick it up:

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d orchestrator worker
docker exec <worker> sh -c 'echo $OPENSSL_armcap'   # -> 0
```

### When to set it — and when NOT to
| Context | Set `OPENSSL_armcap=0`? | Why |
|---|---|---|
| Local dev, Apple-Silicon Docker Desktop / Colima, hitting the SIGILL | **Yes** (already in `docker-compose.ollama.yml`) | The VM can't execute the ARM crypto instructions OpenSSL selects. |
| Real Linux host on real hardware (e.g. the **Spark** deploy) | **No** | The CPU executes SME/SVE2 correctly and should keep **hardware crypto**. That's why this lives only in the Mac-local override, never in the base `docker-compose.yml` or the Spark override. |
| Non-Apple-Silicon Docker, or a Docker Desktop new enough that its VM kernel supports the instructions | Optional | Harmless to leave set (it's a no-op where the CPU works), so the override keeps it unconditionally for robustness. |

**Robustness:** because `OPENSSL_armcap=0` is a no-op where the CPU is capable, keeping it set in
the Mac-local override is safe even after a Docker Desktop update fixes the VM. The **permanent**
fix is a newer VM guest kernel that supports the advertised instruction set.

### If you're bitten outside the compose stack
Running a one-off command (e.g. `pytest`, a script) directly in a container without the override?
Prefix it: `OPENSSL_armcap=0 python -m pytest …` — or `docker exec -e OPENSSL_armcap=0 …`.
