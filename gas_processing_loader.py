"""Shim for /api/jobs/gas-refresh — delegates to gas_infra_loader."""
from gas_infra_loader import load_gas_processings  # noqa: F401
