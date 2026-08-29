"""Tests for nvidia-smi sampling.

Every fixture below is shaped like real captured output, because the failures
this module exists to prevent are all failures of *reading* -- a `[N/A]` where a
number was expected, a driver that forgot `nounits`, a short row. The rule these
tests enforce is the one from docs/08 section 13: a bad parse must raise, never
resolve to "0 GB used", because 0 GB used and a free card are the same reading
and the controller answers a free card by loading a 17 GB model onto it.

There is no GPU on the machine this runs on, and no host in the fleet is
reachable from it. `FakeSampler` is therefore not a shortcut around a real test;
it is the test.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fleet_controller.ladder import breaches_headroom, select_rung
from fleet_controller.models import GpuSample, HostState, Rung
from fleet_controller.nvidia import (
    DEFAULT_PLATFORM_NAME_PATTERNS,
    NOTHING_IS_OURS,
    ComputeApp,
    FakeSampler,
    LocalSampler,
    MemoryReading,
    NvidiaSmiError,
    NvidiaSmiParseError,
    NvidiaSmiTimeout,
    NvidiaSmiUnavailable,
    ProcessOwnership,
    Sampler,
    build_sample,
    gb_to_mib,
    make_sample,
    mib_to_gb,
    parse_compute_apps,
    parse_gpu_memory,
    ramping_samples,
    single_gpu,
)

# --------------------------------------------------------------------------
# Fixtures, shaped like captured output.
#
# .226 is an RTX 4090 (24564 MiB), .87 and .210 are RTX 4070s (12282 MiB).
# --------------------------------------------------------------------------

GPU_4090_IDLE = "24564, 1187\n"
GPU_4090_CODER_LOADED = "24564, 18402\n"
GPU_4070_IDLE = "12282, 622\n"
GPU_WITH_UNITS = "24564 MiB, 1187 MiB\n"
GPU_CRLF = "24564, 1187\r\n"

APPS_EMPTY = ""
APPS_NO_PROCESSES = "No running processes found\n"

# vLLM plus the user's own training script, the case the whole policy is about.
APPS_MIXED = "3251, /usr/bin/python3.11, 17420\n40122, python.exe, 6180\n"

# Two-field form, from an agent that queried without process_name.
APPS_TWO_FIELD = "3251, 17420\n40122, 6180\n"

# WSL2 and some consumer drivers report per-process memory as [N/A].
APPS_NA_MEMORY = "40122, python.exe, [N/A]\n"

# Windows paths, one with a comma in it.
APPS_WINDOWS = (
    "7364, C:\\Windows\\System32\\dwm.exe, 412\n"
    "40122, C:\\Users\\alex\\models, v2\\python.exe, 6180\n"
)


class TestMemoryParsingHappyPath:
    def test_nounits_form(self) -> None:
        assert parse_gpu_memory(GPU_4090_IDLE) == (MemoryReading(total_mib=24564, used_mib=1187),)

    def test_unit_suffixed_form(self) -> None:
        """A driver that ignores `nounits` must not silently fail to parse."""
        assert parse_gpu_memory(GPU_WITH_UNITS) == parse_gpu_memory(GPU_4090_IDLE)

    def test_crlf_from_a_windows_agent(self) -> None:
        assert parse_gpu_memory(GPU_CRLF) == parse_gpu_memory(GPU_4090_IDLE)

    def test_free_mib_never_negative(self) -> None:
        assert MemoryReading(total_mib=12282, used_mib=99999).free_mib == 0


class TestMemoryParsingFailures:
    """Each of these must raise. None may return a number."""

    @pytest.mark.parametrize(
        ("stdout", "why"),
        [
            ("", "empty output"),
            ("\n\n", "blank lines only"),
            ("24564\n", "short row -- used column missing"),
            ("24564, \n", "empty used field"),
            ("24564, [N/A]\n", "driver does not know how much is used"),
            ("24564, [Not Supported]\n", "field unsupported on this card"),
            ("[N/A], [N/A]\n", "no numbers at all"),
            ("24564, [Insufficient Permissions]\n", "agent lacks rights to read it"),
            ("banana, 1187\n", "non-numeric total"),
            ("24564, 11.87\n", "fractional MiB is not a thing nvidia-smi emits"),
            ("24564, -100\n", "negative used"),
            ("24, 1 GiB\n", "unit we do not accept -- refuse rather than guess"),
            ("0, 0\n", "a card with no memory is not a card"),
        ],
    )
    def test_raises_rather_than_guessing(self, stdout: str, why: str) -> None:
        with pytest.raises(NvidiaSmiParseError):
            parse_gpu_memory(stdout)

    def test_a_parse_failure_never_looks_like_a_free_card(self) -> None:
        """The failure mode this module exists for.

        `used = 0` on a 24 GB card is what a genuinely idle 4090 reads as. If any
        malformed row could produce it, the controller would promote onto
        somebody's job.
        """
        for stdout in ("", "24564\n", "24564, [N/A]\n", "24564, banana\n"):
            with pytest.raises(NvidiaSmiError):
                build_sample(host=".226", memory_stdout=stdout, apps_stdout=APPS_EMPTY)

    def test_multi_gpu_output_is_refused(self) -> None:
        """Averaging two cards produces a free-VRAM figure describing neither."""
        readings = parse_gpu_memory("24564, 1187\n12282, 622\n")
        assert len(readings) == 2
        with pytest.raises(NvidiaSmiParseError):
            single_gpu(readings)

    def test_no_gpu_at_all_is_refused(self) -> None:
        with pytest.raises(NvidiaSmiParseError):
            single_gpu([])


class TestComputeAppParsing:
    def test_three_field_form(self) -> None:
        assert parse_compute_apps(APPS_MIXED) == (
            ComputeApp(pid=3251, process_name="/usr/bin/python3.11", used_mib=17420),
            ComputeApp(pid=40122, process_name="python.exe", used_mib=6180),
        )

    def test_two_field_form(self) -> None:
        """`--query-compute-apps=pid,used_gpu_memory`: no name, still a pid."""
        assert parse_compute_apps(APPS_TWO_FIELD) == (
            ComputeApp(pid=3251, used_mib=17420),
            ComputeApp(pid=40122, used_mib=6180),
        )

    @pytest.mark.parametrize("stdout", [APPS_EMPTY, "\n", APPS_NO_PROCESSES])
    def test_idle_card_is_not_an_error(self, stdout: str) -> None:
        """An idle card is the common case; it must not raise."""
        assert parse_compute_apps(stdout) == ()

    def test_na_memory_keeps_the_pid(self) -> None:
        """WSL2 hides per-process memory routinely. The pid is the load-bearing
        part -- dropping the row would hide a foreign process entirely."""
        apps = parse_compute_apps(APPS_NA_MEMORY)
        assert apps == (ComputeApp(pid=40122, process_name="python.exe", used_mib=None),)

    @pytest.mark.parametrize("stdout", ["[N/A], python.exe, 6180\n", "pid, python.exe, 6180\n"])
    def test_unreadable_pid_fails_the_sample(self, stdout: str) -> None:
        """Not knowing the pid means not knowing whether to yield."""
        with pytest.raises(NvidiaSmiParseError):
            parse_compute_apps(stdout)

    def test_process_path_containing_a_comma(self) -> None:
        apps = parse_compute_apps(APPS_WINDOWS)
        assert [a.pid for a in apps] == [7364, 40122]
        assert apps[1].process_name == "C:\\Users\\alex\\models, v2\\python.exe"
        assert apps[1].used_mib == 6180


class TestForeignClassification:
    """`GpuSample.foreign_pids` is the yield trigger; these are its edges."""

    APPS = parse_compute_apps(APPS_MIXED)

    def test_nothing_known_means_everything_is_foreign(self) -> None:
        """A restarted agent has launched nothing, so nothing on the card is ours."""
        assert NOTHING_IS_OURS.foreign(self.APPS) == (3251, 40122)

    def test_known_pid_is_ours(self) -> None:
        ownership = ProcessOwnership(pids=frozenset({3251}))
        assert ownership.foreign(self.APPS) == (40122,)

    def test_name_pattern_is_ours(self) -> None:
        apps = parse_compute_apps("3251, vllm serve qwen3-coder, 17420\n40122, python.exe, 6180\n")
        ownership = ProcessOwnership(name_patterns=DEFAULT_PLATFORM_NAME_PATTERNS)
        assert ownership.foreign(apps) == (40122,)

    def test_name_matching_is_case_insensitive(self) -> None:
        apps = parse_compute_apps("900, C:\\ComfyUI\\python.exe, 9000\n")
        assert ProcessOwnership(name_patterns=("comfyui",)).foreign(apps) == ()

    def test_over_broad_pattern_would_pin_the_platform_on_top_of_them(self) -> None:
        """The dangerous direction, stated as a test so nobody adds `python`.

        Both processes here are `python`; matching on it claims the user's
        training run as ours and the host never yields.
        """
        assert ProcessOwnership(name_patterns=("python",)).foreign(self.APPS) == ()

    def test_over_narrow_ownership_only_costs_capability(self) -> None:
        """The safe direction: our own vLLM read as foreign pins us to the bottom
        rung, which is wasteful but never takes VRAM from anybody."""
        assert ProcessOwnership(name_patterns=("not-a-real-name",)).foreign(self.APPS) == (
            3251,
            40122,
        )

    def test_predicate_escape_hatch(self) -> None:
        ownership = ProcessOwnership(predicate=lambda app: (app.used_mib or 0) > 10_000)
        assert ownership.foreign(self.APPS) == (40122,)

    def test_pids_win_over_the_predicate(self) -> None:
        ownership = ProcessOwnership(pids=frozenset({40122}), predicate=lambda app: False)
        assert ownership.foreign(self.APPS) == (3251,)

    def test_empty_card_has_no_foreign_pids(self) -> None:
        assert NOTHING_IS_OURS.foreign(()) == ()

    def test_pid_with_no_name_cannot_match_a_name_pattern(self) -> None:
        """Two-field output plus name-only ownership must fail toward foreign."""
        apps = parse_compute_apps(APPS_TWO_FIELD)
        assert ProcessOwnership(name_patterns=("vllm",)).foreign(apps) == (3251, 40122)


class TestUnits:
    """MiB / 1024, matching docs/08 section 8.1's headroom_free_mb = 1024."""

    def test_a_gb_here_is_1024_mib(self) -> None:
        assert mib_to_gb(1024) == 1.0
        assert gb_to_mib(3.0) == 3072.0

    @pytest.mark.parametrize(
        ("mib", "gb"),
        [(24564, 23.99), (12282, 11.99), (17420, 17.01), (0, 0.0)],
    )
    def test_card_sizes_land_where_the_ladder_expects(self, mib: int, gb: float) -> None:
        assert mib_to_gb(mib) == pytest.approx(gb, abs=0.01)

    def test_decimal_gb_would_have_overstated_the_card(self) -> None:
        """Regression guard on the choice itself: decimal GB reads a 4090 as
        ~25.8, which would take ~1.8 GB more of the user's card than promised."""
        decimal_gb = 24564 * 1024 * 1024 / 1_000_000_000
        assert decimal_gb > 25.0
        assert mib_to_gb(24564) < 24.0


class TestBuildSample:
    def test_idle_4090_is_free_and_unclaimed(self) -> None:
        sample = build_sample(
            host=".226",
            memory_stdout=GPU_4090_IDLE,
            apps_stdout=APPS_EMPTY,
            sampled_at=datetime(2026, 8, 29, 11, 4, 11, tzinfo=UTC),
        )
        assert sample.host == ".226"
        assert sample.total_gb == pytest.approx(23.99, abs=0.01)
        assert sample.used_gb == pytest.approx(1.16, abs=0.01)
        assert sample.free_gb == pytest.approx(22.83, abs=0.01)
        assert sample.has_foreign_process is False

    def test_our_coder_alone_on_the_card_is_still_free(self) -> None:
        """17 GB used by our own vLLM must not read as somebody else's job."""
        sample = build_sample(
            host=".226",
            memory_stdout=GPU_4090_CODER_LOADED,
            apps_stdout=APPS_MIXED.splitlines(keepends=True)[0],
            ownership=ProcessOwnership(pids=frozenset({3251})),
        )
        assert sample.foreign_pids == ()
        assert sample.used_gb == pytest.approx(17.97, abs=0.01)

    def test_foreign_job_appears(self) -> None:
        sample = build_sample(
            host=".210",
            memory_stdout=GPU_4070_IDLE,
            apps_stdout=APPS_MIXED,
            ownership=ProcessOwnership(pids=frozenset({3251})),
        )
        assert sample.foreign_pids == (40122,)
        assert sample.has_foreign_process is True

    def test_sampled_at_defaults_to_now(self) -> None:
        sample = build_sample(host=".87", memory_stdout=GPU_4070_IDLE, apps_stdout=APPS_EMPTY)
        assert sample.sampled_at.tzinfo is not None


class TestLocalSampler:
    """The subprocess edges. Every one of these must surface as NvidiaSmiError so
    the caller can turn it into HostState.UNKNOWN (docs/08 section 13)."""

    def test_missing_binary(self) -> None:
        sampler = LocalSampler(host=".226", binary="nvidia-smi-does-not-exist")
        with pytest.raises(NvidiaSmiUnavailable):
            sampler.sample()

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=2.0)

        monkeypatch.setattr(subprocess, "run", explode)
        with pytest.raises(NvidiaSmiTimeout):
            LocalSampler(host=".226").sample()

    def test_non_zero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=9,
                stdout="",
                stderr="Unable to determine the device handle for GPU 0000:01:00.0\n",
            )

        monkeypatch.setattr(subprocess, "run", failed)
        with pytest.raises(NvidiaSmiUnavailable, match="exited 9"):
            LocalSampler(host=".226").sample()

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise PermissionError("permission denied")

        monkeypatch.setattr(subprocess, "run", explode)
        with pytest.raises(NvidiaSmiUnavailable):
            LocalSampler(host=".226").sample()

    def test_happy_path_calls_both_queries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            stdout = GPU_4090_IDLE if "--query-gpu" in argv[1] else APPS_MIXED
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        sampler = LocalSampler(
            host=".226",
            ownership=ProcessOwnership(pids=frozenset({3251})),
            clock=lambda: datetime(2026, 8, 29, 11, 4, 11, tzinfo=UTC),
        )
        sample = sampler.sample()
        assert [argv[1].split("=")[0] for argv in seen] == ["--query-gpu", "--query-compute-apps"]
        assert sample.foreign_pids == (40122,)
        assert sample.sampled_at == datetime(2026, 8, 29, 11, 4, 11, tzinfo=UTC)


class TestFakeSampler:
    def test_plays_the_script_in_order(self) -> None:
        first = make_sample(host=".87", total_gb=11.99, used_gb=0.6)
        second = make_sample(host=".87", total_gb=11.99, used_gb=6.2, foreign_pids=[40122])
        sampler = FakeSampler(host=".87", script=[first, second])
        assert sampler.sample() is first
        assert sampler.sample() is second

    def test_repeats_the_last_entry_once_exhausted(self) -> None:
        """A loop under test should not end because the fixture ran out."""
        last = make_sample(host=".87", total_gb=11.99, used_gb=6.2)
        sampler = FakeSampler(host=".87", script=[last])
        assert sampler.exhausted is False
        assert [sampler.sample() for _ in range(5)] == [last] * 5
        assert sampler.exhausted is True

    def test_scripted_failures_are_raised(self) -> None:
        """Three in a row is what puts a host in UNKNOWN; this is how that path
        gets exercised without breaking a GPU."""
        good = make_sample(host=".226", total_gb=23.99, used_gb=1.2)
        sampler = FakeSampler(
            host=".226",
            script=[good, NvidiaSmiTimeout("wedged"), NvidiaSmiParseError("short row")],
        )
        assert sampler.sample() is good
        with pytest.raises(NvidiaSmiTimeout):
            sampler.sample()
        with pytest.raises(NvidiaSmiParseError):
            sampler.sample()

    def test_empty_script_is_a_failure_not_a_free_card(self) -> None:
        with pytest.raises(NvidiaSmiUnavailable):
            FakeSampler(host=".226").sample()

    def test_both_implementations_satisfy_the_protocol(self) -> None:
        samplers: list[Sampler] = [LocalSampler(host=".226"), FakeSampler(host=".226")]
        assert [s.host for s in samplers] == [".226", ".226"]


class TestRampingJob:
    """docs/03 section 2 and section 7 test 3: the caching allocator grows, and
    the platform must drop a rung rather than let the user's job OOM."""

    CODER = Rung(name="coder", served_model="qwen3-coder-30b-a3b-int4", footprint_gb=17.0)
    CHAT14 = Rung(name="chat", served_model="qwen3-14b-int4", footprint_gb=9.0)
    CHAT8 = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.5)
    CHAT4 = Rung(name="chat-tiny", served_model="qwen3-4b-int4", footprint_gb=3.0)
    H226 = (CODER, CHAT14, CHAT8, CHAT4)

    TOTAL_GB = mib_to_gb(24564)
    IDLE_GB = mib_to_gb(1187)
    """Desktop and compositor, taken from the idle fixture at the top of this
    file rather than made up -- docs/08 section 7.2 calls it `baseline_mb` and
    says to measure it per host."""

    OURS = CHAT14

    def ramp(self) -> list[GpuSample]:
        """Their job going 4 GB -> 11 GB over 60 s, our 9 GB rung resident.

        `used_gb` is what nvidia-smi reports, which includes the desktop and our
        own model: that is the whole difficulty, and a test that ramped a bare 4
        to 11 on an otherwise empty card would not exercise it.
        """
        floor = self.IDLE_GB + self.OURS.footprint_gb
        return ramping_samples(
            host=".226",
            total_gb=self.TOTAL_GB,
            start_used_gb=floor + 4.0,
            end_used_gb=floor + 11.0,
            count=31,
            interval_s=2.0,
            foreign_pids=[40122],
            start_at=datetime(2026, 8, 29, 11, 4, 11, tzinfo=UTC),
        )

    def theirs_gb(self, sample: GpuSample) -> float:
        """What is left when our own resident model is discounted."""
        return sample.used_gb - self.OURS.footprint_gb

    def test_ramp_shape(self) -> None:
        samples = self.ramp()
        assert self.theirs_gb(samples[0]) - self.IDLE_GB == pytest.approx(4.0)
        assert self.theirs_gb(samples[-1]) - self.IDLE_GB == pytest.approx(11.0)
        assert samples[-1].sampled_at - samples[0].sampled_at == timedelta(seconds=60)
        assert all(
            later.used_gb >= earlier.used_gb
            for earlier, later in zip(samples, samples[1:], strict=False)
        )

    def test_every_sample_stays_foreign(self) -> None:
        """The job never stops being theirs part-way up the ramp."""
        assert all(s.has_foreign_process for s in self.ramp())

    def test_headroom_survives_the_launch_reading_but_not_the_settled_one(self) -> None:
        """Why the 60 s settle exists, as an assertion.

        At launch their job looks like 4 GB and everything is comfortable. A
        minute later the same job holds 11 GB, the promised 3 GB is gone, and
        `breaches_headroom` fires -- the emergency demotion that deliberately
        bypasses hysteresis (docs/03 section 4.6). Sizing against the first
        reading and waiting is how the platform becomes the reason their run
        died.
        """
        samples = self.ramp()
        assert breaches_headroom(self.OURS, samples[0].free_gb, HostState.SHARING) is False
        assert breaches_headroom(self.OURS, samples[-1].free_gb, HostState.SHARING) is True

    def test_platform_drops_a_rung_as_the_job_grows(self) -> None:
        """What the demotion resolves to: the same ladder, over what they hold."""
        samples = self.ramp()
        at_launch = self.TOTAL_GB - self.theirs_gb(samples[0])
        settled = self.TOTAL_GB - self.theirs_gb(samples[-1])
        first = select_rung(self.H226, at_launch, HostState.SHARING)
        last = select_rung(self.H226, settled, HostState.SHARING)
        assert first is not None and first.name == "chat"
        assert last is not None and last.name == "chat-small"

    def test_a_ramp_needs_two_points(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            ramping_samples(
                host=".226", total_gb=23.99, start_used_gb=4.0, end_used_gb=11.0, count=1
            )
