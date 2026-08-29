"""Sampling `nvidia-smi`: the only place the controller learns what a card is doing.

Two queries, kept separate because they fail differently:

    nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits
    nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory
               --format=csv,noheader,nounits

Parsing is pure functions over the raw stdout string; only `LocalSampler` runs a
subprocess. No host in this fleet is reachable from the machine this code is
written on, so a seam that lets the logic be exercised without a GPU is not a
convenience -- it is the only way any of this gets tested before it runs on
somebody's workstation.

Units
-----
`nvidia-smi` reports MiB. `models.py` says GB, and docs/08 section 8.1 pairs
`HEADROOM_FREE_GB = 1.0` with `headroom_free_mb = 1024`, so the codebase's "GB"
is a GiB: **MiB / 1024**. Decimal GB would read a 24564 MiB 4090 as 25.8 "GB" and
put every band edge in docs/03 section 3 out by 7% in the direction that takes
more of the user's card than promised. One conversion constant, used everywhere.

Failure
-------
Every problem -- missing binary, timeout, non-zero exit, `[N/A]`, a short row, an
unexpected unit -- raises `NvidiaSmiError`. Nothing is defaulted, coerced or
skipped. docs/08 section 13: "never coerce a bad parse to 0 used", because a bad
parse that reads as 0 GB used is indistinguishable from a free card, and the
controller would answer that by loading a 17 GB model on top of someone's job.
The caller's contract is: catch `NvidiaSmiError`, count it, and after three in a
row put the host in `HostState.UNKNOWN`, which never promotes.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fleet_controller.models import GpuSample

MIB_PER_GB = 1024.0
"""What the rest of the service calls a GB. See the module docstring."""

QUERY_GPU_ARGS: tuple[str, ...] = (
    "--query-gpu=memory.total,memory.used",
    "--format=csv,noheader,nounits",
)

QUERY_APPS_ARGS: tuple[str, ...] = (
    "--query-compute-apps=pid,process_name,used_gpu_memory",
    "--format=csv,noheader,nounits",
)

DEFAULT_TIMEOUT_S = 2.0
"""docs/08 section 13: a wedged nvidia-smi must cost one sample, not the loop --
which comes round every 2 s anyway."""

DEFAULT_PLATFORM_NAME_PATTERNS: tuple[str, ...] = ("vllm", "infinity", "comfyui")
"""Fallback only -- see `ProcessOwnership`. Matched case-insensitively as
substrings of the process name nvidia-smi reports."""

_NOT_A_NUMBER = re.compile(r"^\[.*\]$")
_MEMORY_FIELD = re.compile(r"^(?P<value>\d+)\s*(?P<unit>[A-Za-z]*)$")
_ACCEPTED_UNITS = frozenset({"", "mib", "mb"})
_NO_PROCESSES = "no running processes found"


class NvidiaSmiError(RuntimeError):
    """Base for every way a sample can fail to be trustworthy.

    Catch this one. The subclasses exist for logging and for the dashboard's
    "why is this host Unknown" line, not to be handled differently: the response
    to all of them is identical, which is to hold state and never promote.
    """


class NvidiaSmiUnavailable(NvidiaSmiError):
    """The binary is missing, unrunnable, or exited non-zero."""


class NvidiaSmiTimeout(NvidiaSmiError):
    """The call exceeded its deadline.

    A wedged `nvidia-smi` is a real failure mode on a card under load, and it is
    the one failure that could otherwise stall the sampling loop itself.
    """


class NvidiaSmiParseError(NvidiaSmiError):
    """Output arrived but could not be read as numbers we would bet VRAM on."""


@dataclass(frozen=True)
class MemoryReading:
    """One GPU's memory totals, still in the units nvidia-smi used."""

    total_mib: int
    used_mib: int

    @property
    def free_mib(self) -> int:
        return max(0, self.total_mib - self.used_mib)


@dataclass(frozen=True)
class ComputeApp:
    """One CUDA process, as `--query-compute-apps` sees it."""

    pid: int
    process_name: str | None = None
    used_mib: int | None = None
    """None when the driver reported `[N/A]`, which WSL2 and some consumer
    drivers do routinely. The pid is what the yield decision needs; per-process
    memory is dashboard detail, so its absence must not fail the sample."""


def mib_to_gb(mib: float) -> float:
    """MiB -> the GB (really GiB) that models.py and the ladder speak."""
    return mib / MIB_PER_GB


def gb_to_mib(gb: float) -> float:
    """Inverse of `mib_to_gb`, for building fixtures and fakes."""
    return gb * MIB_PER_GB


def _rows(stdout: str) -> list[list[str]]:
    """CSV rows, blank lines dropped.

    `csv` rather than `str.split(",")` so a Windows process path containing a
    comma does not silently become two fields. Cells keep their surrounding
    whitespace -- nvidia-smi separates with ", " and the callers below strip what
    they parse, but a process name gets rejoined from its cells and would
    otherwise lose the space inside its own path.
    """
    reader = csv.reader(io.StringIO(stdout.replace("\r\n", "\n")))
    return [row for row in reader if any(cell.strip() for cell in row)]


def _parse_mib(text: str, *, field_name: str) -> int:
    """A memory field, or raise. Never returns a guess."""
    value = text.strip()
    if not value:
        raise NvidiaSmiParseError(f"{field_name}: empty field")
    if _NOT_A_NUMBER.match(value):
        # [N/A], [Not Supported], [Insufficient Permissions] -- the driver saying
        # it does not know. Neither do we, then.
        raise NvidiaSmiParseError(f"{field_name}: driver reported {value}, not a number")
    match = _MEMORY_FIELD.match(value)
    if match is None:
        raise NvidiaSmiParseError(f"{field_name}: cannot read {value!r} as a memory value")
    unit = match["unit"].lower()
    if unit not in _ACCEPTED_UNITS:
        # A labelled GiB could be converted, but a unit nvidia-smi is not
        # documented to emit means our assumptions about this driver are already
        # wrong. Refusing costs one sample; guessing costs someone's job.
        raise NvidiaSmiParseError(f"{field_name}: unexpected unit {match['unit']!r} in {value!r}")
    return int(match["value"])


def _parse_optional_mib(text: str) -> int | None:
    value = text.strip()
    if not value or _NOT_A_NUMBER.match(value):
        return None
    return _parse_mib(value, field_name="used_gpu_memory")


def parse_gpu_memory(stdout: str) -> tuple[MemoryReading, ...]:
    """Parse `--query-gpu=memory.total,memory.used`, one entry per GPU.

    Tolerates both the `nounits` form (`24564, 1187`) and the unit-suffixed form
    (`24564 MiB, 1187 MiB`); drivers differ, and the flag has been left off
    before.
    """
    rows = _rows(stdout)
    if not rows:
        raise NvidiaSmiParseError("query-gpu returned no rows")
    readings: list[MemoryReading] = []
    for index, row in enumerate(rows):
        if len(row) < 2:
            raise NvidiaSmiParseError(f"query-gpu row {index}: short row {row!r}")
        total = _parse_mib(row[0], field_name=f"memory.total[{index}]")
        used = _parse_mib(row[1], field_name=f"memory.used[{index}]")
        if total <= 0:
            raise NvidiaSmiParseError(f"query-gpu row {index}: total of {total} MiB is not a card")
        readings.append(MemoryReading(total_mib=total, used_mib=used))
    return tuple(readings)


def single_gpu(readings: Sequence[MemoryReading]) -> MemoryReading:
    """The one card on a host, or raise.

    Every host in this fleet has exactly one GPU (docs/02). If a second appears,
    "free VRAM" no longer describes any physical card, so stop rather than let
    two of them be averaged into a number the ladder will act on.
    """
    if len(readings) != 1:
        raise NvidiaSmiParseError(f"expected exactly one GPU, got {len(readings)}")
    return readings[0]


def parse_compute_apps(stdout: str) -> tuple[ComputeApp, ...]:
    """Parse `--query-compute-apps`. Empty output means an idle card, not an error.

    Accepts both the two-field form (`pid,used_gpu_memory`) and the three-field
    form (`pid,process_name,used_gpu_memory`) from docs/08 section 7.2: the name
    is wanted for the dashboard and for name-based ownership, but is not always
    asked for. Fields beyond the third are rejoined as a process name that
    contained a comma.
    """
    apps: list[ComputeApp] = []
    for index, row in enumerate(_rows(stdout)):
        head = row[0].strip()
        if head.lower().startswith(_NO_PROCESSES):
            continue
        if not head.isdigit():
            # An unreadable pid means we cannot say whether a foreign process is
            # on the card. That is the entire yield decision, so fail the sample
            # rather than report an empty process list.
            raise NvidiaSmiParseError(f"compute-apps row {index}: bad pid {head!r}")
        pid = int(head)
        if len(row) == 1:
            apps.append(ComputeApp(pid=pid))
        elif len(row) == 2:
            apps.append(ComputeApp(pid=pid, used_mib=_parse_optional_mib(row[1])))
        else:
            name = ",".join(row[1:-1]).strip()  # a path that contained a comma
            apps.append(
                ComputeApp(
                    pid=pid,
                    process_name=name or None,
                    used_mib=_parse_optional_mib(row[-1]),
                )
            )
    return tuple(apps)


@dataclass(frozen=True)
class ProcessOwnership:
    """Which CUDA processes are the platform's own.

    This is the highest-stakes classification in the service, and its two failure
    modes are not symmetric. `GpuSample.foreign_pids` is what makes a host yield:

    * Claim too much -- a name pattern like `python`, which also matches the
      user's training script -- and their job never registers as foreign. The
      platform keeps a 17 GB model resident on top of them: precisely the "the
      platform made my job crash" outcome docs/03 section 1 exists to prevent.
    * Claim too little -- our own vLLM not recognised -- and the host reads as
      permanently occupied by a stranger, pins itself to its bottom rung, and the
      platform is useless while looking healthy.

    The first is much worse than the second, so anything unrecognised is foreign:
    unknown means theirs.

    Prefer `pids`. The agent starts the platform's processes and therefore knows
    their pids exactly (docs/08 section 7.2), and an exact pid set cannot
    over-claim. `name_patterns` is the fallback for an agent that restarted and
    lost track, and should be as specific as that host's process names allow.
    """

    pids: frozenset[int] = frozenset()
    name_patterns: tuple[str, ...] = ()
    predicate: Callable[[ComputeApp], bool] | None = None
    """Escape hatch for a host needing judgement neither of the above captures --
    matching a container's pid namespace, say."""

    def owns(self, app: ComputeApp) -> bool:
        if app.pid in self.pids:
            return True
        name = (app.process_name or "").lower()
        if name and any(pattern.lower() in name for pattern in self.name_patterns):
            return True
        if self.predicate is not None:
            return self.predicate(app)
        return False

    def foreign(self, apps: Iterable[ComputeApp]) -> tuple[int, ...]:
        """Pids on the card that are not ours, in the order reported."""
        return tuple(app.pid for app in apps if not self.owns(app))


NOTHING_IS_OURS = ProcessOwnership()
"""The right default for a freshly started agent: it has launched nothing yet, so
every process it can see belongs to somebody else."""


def build_sample(
    *,
    host: str,
    memory_stdout: str,
    apps_stdout: str,
    ownership: ProcessOwnership = NOTHING_IS_OURS,
    sampled_at: datetime | None = None,
) -> GpuSample:
    """Both raw outputs -> one `GpuSample`. The seam `LocalSampler` is built on."""
    reading = single_gpu(parse_gpu_memory(memory_stdout))
    apps = parse_compute_apps(apps_stdout)
    return GpuSample(
        host=host,
        total_gb=mib_to_gb(reading.total_mib),
        used_gb=mib_to_gb(reading.used_mib),
        foreign_pids=ownership.foreign(apps),
        sampled_at=sampled_at if sampled_at is not None else _utcnow(),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Sampler(Protocol):
    """One reading of one host's GPU, on demand.

    Structural rather than inherited, so the fake owes the real implementation
    nothing beyond this method.
    """

    host: str

    def sample(self) -> GpuSample:
        """Raise `NvidiaSmiError` rather than return anything doubtful."""
        ...


@dataclass
class LocalSampler:
    """Runs `nvidia-smi` on the host this process is on.

    Per docs/08 section 7 the controller never shells into anyone's machine: this
    lives inside the per-host agent, reading its own card.
    """

    host: str
    ownership: ProcessOwnership = NOTHING_IS_OURS
    binary: str = "nvidia-smi"
    timeout_s: float = DEFAULT_TIMEOUT_S
    clock: Callable[[], datetime] = _utcnow

    def sample(self) -> GpuSample:
        return build_sample(
            host=self.host,
            memory_stdout=self.run(QUERY_GPU_ARGS),
            apps_stdout=self.run(QUERY_APPS_ARGS),
            ownership=self.ownership,
            sampled_at=self.clock(),
        )

    def run(self, args: Sequence[str]) -> str:
        """One invocation, with every failure turned into an `NvidiaSmiError`.

        Nothing here may return a plausible-looking empty string on failure: an
        empty `--query-gpu` output is caught by the parser, but only because this
        never hands one back silently.
        """
        argv = [self.binary, *args]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise NvidiaSmiUnavailable(f"{self.binary} not found on {self.host}") from exc
        except subprocess.TimeoutExpired as exc:
            raise NvidiaSmiTimeout(
                f"{self.binary} exceeded {self.timeout_s}s on {self.host}"
            ) from exc
        except OSError as exc:
            raise NvidiaSmiUnavailable(
                f"{self.binary} could not be run on {self.host}: {exc}"
            ) from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip().splitlines()
            raise NvidiaSmiUnavailable(
                f"{self.binary} exited {completed.returncode} on {self.host}: "
                f"{stderr[0] if stderr else 'no stderr'}"
            )
        return completed.stdout


ScriptEntry = GpuSample | NvidiaSmiError


@dataclass
class FakeSampler:
    """A `Sampler` driven by a scripted sequence.

    Not a nicety. Nothing in this fleet is reachable from the machine the
    controller is written on, so behaviour over time -- a job ramping, a card
    going quiet, three parse failures in a row -- can only be tested by writing
    the sequence down. Entries that are exceptions get raised, which is how the
    UNKNOWN path gets exercised at all.

    Once the script runs out the last entry repeats, so a loop under test can run
    as long as it likes without the fake being the thing that ends it.
    """

    host: str
    script: list[ScriptEntry] = field(default_factory=list)
    calls: int = 0

    def sample(self) -> GpuSample:
        if not self.script:
            raise NvidiaSmiUnavailable(f"FakeSampler for {self.host} has an empty script")
        entry = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(entry, NvidiaSmiError):
            raise entry
        return entry

    @property
    def exhausted(self) -> bool:
        """True once the fake has started repeating its last entry."""
        return self.calls >= len(self.script)


def make_sample(
    *,
    host: str,
    total_gb: float,
    used_gb: float,
    foreign_pids: Sequence[int] = (),
    sampled_at: datetime | None = None,
) -> GpuSample:
    """A `GpuSample` straight from GB, for scripting fakes."""
    return GpuSample(
        host=host,
        total_gb=total_gb,
        used_gb=used_gb,
        foreign_pids=tuple(foreign_pids),
        sampled_at=sampled_at if sampled_at is not None else _utcnow(),
    )


def ramping_samples(
    *,
    host: str,
    total_gb: float,
    start_used_gb: float,
    end_used_gb: float,
    count: int,
    interval_s: float = 2.0,
    foreign_pids: Sequence[int] = (),
    start_at: datetime | None = None,
) -> list[GpuSample]:
    """A job whose VRAM grows linearly between two readings.

    The shape docs/03 section 2 warns about: PyTorch's caching allocator keeps
    growing after launch, so a job reading 4 GB can be 11 GB a minute later, and
    sizing against the first reading is how the platform ends up reclaiming
    memory the user's job was about to need. That is what the 60 s settle is for,
    and anything that picks rungs over time should be tested against one of
    these.
    """
    if count < 2:
        raise ValueError("a ramp needs at least two samples")
    first = start_at if start_at is not None else _utcnow()
    step = (end_used_gb - start_used_gb) / (count - 1)
    return [
        make_sample(
            host=host,
            total_gb=total_gb,
            used_gb=start_used_gb + step * index,
            foreign_pids=foreign_pids,
            sampled_at=first + timedelta(seconds=interval_s * index),
        )
        for index in range(count)
    ]
