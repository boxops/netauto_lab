# Nautobot Data Loader

## Overview

The Nautobot Data Loader is a declarative reconciliation feature for loading and maintaining Source-of-Truth objects from YAML.

It is no longer only an "initialize once" workflow. It now supports recurring reconciliation, drift correction, dry-run planning, and scoped pruning.

## Current Implementation

Data model source:

- `nautobot/data_loader/data.yml`

Loader script:

- `nautobot/data_loader/load_data.py`

Primary capabilities implemented so far:

- Create missing objects from desired state.
- Update existing objects when fields drift.
- No-op detection when current state already matches desired state.
- Plan mode (`--mode plan`) for non-mutating action previews.
- Prune mode (`--mode prune`) for managed-scope deletion.
- Action accounting (`create`, `update`, `noop`, `delete`) in a summary.
- Validation for required device/interface/cable fields before load.

Object coverage includes:

- Location types and locations.
- Roles, manufacturers, device types, platforms.
- Namespace, prefixes, VLANs, config contexts.
- Custom fields.
- Secrets, secret groups, associations.
- Devices, interfaces, interface IP assignments.
- Cables.

## Modes

The loader supports three modes:

- `apply`: Reconcile desired state into Nautobot (create/update).
- `plan`: Simulate `apply` and prune decisions without mutating Nautobot.
- `prune`: Remove managed objects that are no longer in desired state.

## Make Targets

Use these targets for day-to-day operations:

```bash
make load-data             # apply
make plan-data             # dry-run
make prune-data            # prune managed scope

make lint-data             # validate YAML syntax
make test-data-unit        # unit tests
make test-data-integration # integration tests
make test-data-crud        # focused CRUD + plan integration tests
```

## Direct Script Usage

Inside the Nautobot container:

```bash
python /opt/nautobot/data_loader/load_data.py \
  --data-file /opt/nautobot/data_loader/data.yml \
  --mode apply
```

Switch `--mode` to `plan` or `prune` as needed.

## Expected Workflow

1. Update `nautobot/data_loader/data.yml` with desired state changes.
2. Run `make plan-data` and review action summary.
3. Run `make load-data` to apply updates.
4. Run `make prune-data` when removing managed objects from desired state.
5. Validate with `make test-data-crud` in CI or before merge.

## Notes and Guardrails

- Plan mode is intended to be non-mutating.
- Prune mode is scoped to managed objects represented by the loader logic and data model.
- Integration tests invoke the same containerized loader path used in operations.
- If Docker permissions vary by environment, command wrappers use a sudo fallback path.
