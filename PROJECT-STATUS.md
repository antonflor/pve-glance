# Project status

## PVG-005 — KVM guest filesystem reporting (2026-11-12)

Implemented automatic QEMU Guest Agent filesystem collection in local pvesh and
remote API modes. Each usable local filesystem has a meter. Missing agents,
timeouts, permission failures and missing counters have explicit statuses.
Queries use four workers per cluster, five-second timeouts and a 30-second cache.

Validation: seven Python regression tests and Node guest-card rendering checks
pass. A staged candidate on the owner's Proxmox VE 9.2 host collected real
filesystem counters from all four running KVM VMs, including secondary data
disks, in 1.54 seconds. This is actual local pvesh validation; remote transport,
Windows, failure cases and LXC preservation have automated fixture coverage.
No live remote-token or Windows guest validation has been performed.

GitHub CI/merge and installed service verification are recorded in PVG-005.
