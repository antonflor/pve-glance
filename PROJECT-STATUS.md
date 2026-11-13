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

## PVG-006 / PVG-007 — Appearance settings (2026-11-12)

Added a native Settings dialog with Light, Dark, Monty, Stars, and Tricolor
radio choices. Stars uses navy, white and red; Tricolor uses green, white and
red without country labels. Existing `pgtheme` preferences carry over. Reset
restores Dark and leaves unrelated browser storage intact. Storage failures
allow changes for the current visit and show an explanatory status.

Validation includes existing Python/backend and rendering checks plus Node
settings checks for migration, selection, invalid preferences, reset and blocked
storage. Browser/mobile verification and live installation evidence are recorded
in ProjectDock PVG-006 and PVG-007. VM/disk visibility and cluster grouping remain
separate backlog items PVG-008 through PVG-010.
