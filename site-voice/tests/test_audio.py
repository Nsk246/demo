"""Codec correctness against the reference implementation.

audioop is gone in 3.13, so this pins the numpy tables to the standard
library's output while it still exists to compare against.
"""
import numpy as np
import pytest

from app import audio as A


def test_encode_matches_audioop_across_the_full_range():
    audioop = pytest.importorskip("audioop")
    samples = np.arange(-32768, 32768, dtype=np.int16)
    assert A.pcm16_to_ulaw(samples) == audioop.lin2ulaw(samples.tobytes(), 2)


def test_decode_matches_audioop_for_every_byte():
    audioop = pytest.importorskip("audioop")
    payload = bytes(range(256))
    expected = np.frombuffer(audioop.ulaw2lin(payload, 2), dtype=np.int16)
    assert np.array_equal(A.ulaw_to_pcm16(payload), expected)


def test_negative_samples_round_away_from_zero():
    """The `abs(s >> 2) << 2` case. A naive abs() disagrees on 381 values."""
    audioop = pytest.importorskip("audioop")
    tricky = np.array([-1, -2, -3, -4, -5, -33, -129], dtype=np.int16)
    assert A.pcm16_to_ulaw(tricky) == audioop.lin2ulaw(tricky.tobytes(), 2)


def test_twilio_frame_expands_to_model_rate():
    payload = b"\xff" * 160  # 20 ms
    assert len(A.phone_to_model(payload, 16000)) == 640


def test_model_output_collapses_to_whole_frames():
    pcm = b"\x00\x00" * 2400  # 100 ms at 24 kHz
    ulaw = A.model_to_phone(pcm, 24000)
    chunks, remainder = A.frames(ulaw)
    assert len(chunks) == 5
    assert remainder == b""
    assert all(len(c) == 160 for c in chunks)


def test_frames_returns_the_remainder_rather_than_padding():
    chunks, remainder = A.frames(b"\x00" * 170)
    assert len(chunks) == 1 and len(remainder) == 10
