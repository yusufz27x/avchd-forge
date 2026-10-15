"""Unit tests for :mod:`avchdforge.progress` (tool output → a 0..1 fraction).

The fixtures are real captures: ``tsmuxer/mux_success.txt`` is the complete stdout
of a successful mux, ``tools/ffmpeg_progress_subtitle.txt`` is a whole
``-progress pipe:1`` stream.
"""

from __future__ import annotations

import pytest

from avchdforge.process import ProgressScanner
from avchdforge.progress import (
    MUX_SUCCESS_SENTINEL,
    FfmpegProgress,
    FfmpegProgressScanner,
    TsmuxerScanner,
    is_mux_success,
    last_fraction,
    parse_percent,
    scan_lines,
)

from .conftest import load_text


@pytest.fixture()
def mux_output() -> str:
    return load_text("mux_success.txt")


@pytest.fixture()
def ffmpeg_progress() -> str:
    return load_text("ffmpeg_progress_subtitle.txt")


# --------------------------------------------------------------------------
# tsMuxeR percentage lines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("7.0% complete", 7.0),
        ("100.0% complete", 100.0),
        ("50% complete", 50.0),
        ("7,5% complete", 7.5),
        ("  12.3 % complete", 12.3),
        ("0.0% complete", 0.0),
    ],
)
def test_parse_percent_accepts_the_variants_tsmuxer_prints(line, expected):
    assert parse_percent(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Processed 96 video frames",
        "Mux successful complete",
        "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer",
        "Decoding AC3 stream (track 2): Bitrate: 448Kbps",
        "complete",
        "%% complete",
    ],
)
def test_parse_percent_ignores_non_progress_lines(line):
    assert parse_percent(line) is None


def test_percent_is_clamped_to_the_valid_range():
    assert parse_percent("150% complete") == 100.0


def test_scanner_walks_a_real_mux_to_completion(mux_output):
    scanner = TsmuxerScanner()

    fractions = scan_lines(mux_output.splitlines(), scanner)

    assert fractions[0] == pytest.approx(0.07)
    assert fractions[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in fractions)
    assert scanner.frames == 96
    assert scanner.success is True
    assert scanner.done is True


def test_scanner_is_a_progress_scanner():
    assert isinstance(TsmuxerScanner(), ProgressScanner)
    assert isinstance(FfmpegProgressScanner(), ProgressScanner)


def test_scanner_stays_put_on_noise(mux_output):
    scanner = TsmuxerScanner()
    scanner.feed("7.0% complete")

    assert scanner.feed("H264 bitstream changed: insert nal unit delimiters") is None
    assert scanner.feed("") is None
    assert scanner.percent == 7.0, "a line that is not progress must not reset it"


def test_success_sentinel_is_recognised(mux_output):
    assert is_mux_success(mux_output) is True
    assert is_mux_success("7.0% complete\nFlushing write buffer\n") is False
    assert is_mux_success("") is False
    assert MUX_SUCCESS_SENTINEL == "Mux successful complete"


def test_100_percent_without_the_sentinel_still_counts_as_done():
    scanner = TsmuxerScanner()
    scanner.feed("100.0% complete")

    assert scanner.done is True
    assert scanner.success is False, "the sentinel is the only proof of success"


def test_scanner_reports_100_once_the_sentinel_arrives(mux_output):
    scanner = TsmuxerScanner()
    for line in mux_output.splitlines():
        if "Mux successful" in line:
            assert scanner.feed(line) == 1.0
    assert scanner.success


# --------------------------------------------------------------------------
# ffmpeg -progress
# --------------------------------------------------------------------------


def test_ffmpeg_scanner_reads_a_real_progress_stream(ffmpeg_progress):
    scanner = FfmpegProgressScanner(duration=4.004)

    fractions = scan_lines(ffmpeg_progress.splitlines(), scanner)

    assert fractions == [1.0], "progress=end is the only line that reports a value"
    assert scanner.done
    assert len(scanner.blocks) == 1
    assert scanner.last.bitrate == "0.2kbits/s"
    assert scanner.last.total_size == 100
    assert scanner.last.speed == "3.43e+03x"
    assert scanner.last.out_time_label == "0:00:03"


def test_ffmpeg_scanner_accumulates_fields_until_the_block_ends():
    scanner = FfmpegProgressScanner(duration=100.0)

    assert scanner.feed("out_time_us=50000000") is None
    assert scanner.feed("bitrate=  64.0kbits/s") is None
    assert scanner.feed("total_size=1048576") is None
    assert scanner.feed("out_time_ms=50000000") is None

    assert scanner.feed("progress=continue") == pytest.approx(0.5)
    assert scanner.last.out_time_us == 50_000_000
    assert scanner.last.bitrate == "64.0kbits/s"


def test_ffmpeg_scanner_reaches_one_at_the_end_block():
    scanner = FfmpegProgressScanner(duration=100.0)
    scanner.feed("out_time_us=99000000")

    assert scanner.feed("progress=end") == 1.0
    assert scanner.done


def test_ffmpeg_scanner_ignores_stderr_noise():
    scanner = FfmpegProgressScanner(duration=10.0)

    assert scanner.feed("[srt @ 0x55] Some warning") is None
    assert scanner.feed("frame=  10") is None
    assert scanner.last == FfmpegProgress()
    assert scanner.blocks == []


def test_ffmpeg_scanner_without_a_duration_reports_nothing_until_the_end():
    scanner = FfmpegProgressScanner(duration=None)
    scanner.feed("out_time_us=12345678")

    assert scanner.feed("progress=continue") is None
    assert scanner.fraction is None
    assert scanner.feed("progress=end") == 1.0


def test_ffmpeg_scanner_clamps_overruns():
    scanner = FfmpegProgressScanner(duration=1.0)
    scanner.feed("out_time_us=99999999")

    assert scanner.feed("progress=continue") == 1.0


def test_ffmpeg_progress_labels_unknown_times():
    assert FfmpegProgress().out_time_label == "?"
    assert FfmpegProgress(out_time_us=3_725_000).out_time_label == "0:00:03"
    assert FfmpegProgress(out_time_us=3_725_000_000).out_time_label == "1:02:05"


def test_ffmpeg_progress_fraction_needs_both_numbers():
    assert FfmpegProgress(out_time_us=None).fraction(10.0) is None
    assert FfmpegProgress(out_time_us=1_000_000).fraction(None) is None
    assert FfmpegProgress(out_time_us=1_000_000).fraction(0.0) is None
    assert FfmpegProgress(out_time_us=5_000_000).fraction(10.0) == pytest.approx(0.5)
    assert FfmpegProgress(done=True).fraction(None) == 1.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_scan_lines_and_last_fraction_compose(mux_output):
    scanner = TsmuxerScanner()

    values = scan_lines(mux_output.splitlines(), scanner)

    assert last_fraction(values) == 1.0
    assert last_fraction([]) is None
    assert last_fraction([None, None]) is None


def test_the_runner_feeds_the_scanner(mux_output, runner):
    runner.add_stdout(mux_output)
    scanner = TsmuxerScanner()

    result = runner.run(
        ["tsMuxeR", "meta.txt", "out"], scanner=scanner, on_progress=lambda value: None
    )

    assert result.ok
    assert runner.progress[0] == pytest.approx(0.07)
    assert runner.progress[-1] == 1.0
    assert scanner.success
    assert len(runner.lines) == len(mux_output.splitlines(keepends=True))
