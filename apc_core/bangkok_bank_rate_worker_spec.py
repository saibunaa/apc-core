"""Disabled-by-default deployment contract for the separate Bank-rate worker.

It declares no process, timer, network, credential, or service activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class BangkokBankRateWorkerSpecError(ValueError):
    """The proposed worker/Core storage topology is unsafe."""


@dataclass(frozen=True)
class BangkokBankRateWorkerSpec:
    worker_state_dir: Path
    core_data_dir: Path
    enabled_by_default: bool = False
    opens_listener: bool = False
    starts_timer: bool = False
    core_may_fetch_bank: bool = False


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def separate_worker_spec(*, worker_state_dir: Path, core_data_dir: Path) -> BangkokBankRateWorkerSpec:
    """Describe a non-activating storage boundary for a future separate worker."""
    if not isinstance(worker_state_dir, Path) or not isinstance(core_data_dir, Path):
        raise BangkokBankRateWorkerSpecError("worker paths are invalid")
    if not worker_state_dir.is_absolute() or not core_data_dir.is_absolute():
        raise BangkokBankRateWorkerSpecError("worker paths must be absolute")
    worker_state_dir = worker_state_dir.resolve(strict=False)
    core_data_dir = core_data_dir.resolve(strict=False)
    if _contains(worker_state_dir, core_data_dir) or _contains(core_data_dir, worker_state_dir):
        raise BangkokBankRateWorkerSpecError("worker and Core storage must be separate")
    return BangkokBankRateWorkerSpec(worker_state_dir=worker_state_dir, core_data_dir=core_data_dir)
