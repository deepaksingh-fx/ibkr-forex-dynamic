"""Tests for pivots (9 SR levels)."""
from __future__ import annotations

import math

import pytest

from pivots import Pivots, compute_pivots


class TestComputePivots:
    def test_formulas(self):
        H, L, C = 1.2000, 1.1000, 1.1500
        p = compute_pivots(H, L, C)
        rng = H - L
        pp = (H + L + C) / 3.0
        assert math.isclose(p.pp, pp, rel_tol=1e-12)
        assert math.isclose(p.r1, 2 * pp - L, rel_tol=1e-12)
        assert math.isclose(p.r2, pp + rng, rel_tol=1e-12)
        assert math.isclose(p.r3, p.r1 + rng, rel_tol=1e-12)
        assert math.isclose(p.r4, p.r2 + rng, rel_tol=1e-12)
        assert math.isclose(p.s1, 2 * pp - H, rel_tol=1e-12)
        assert math.isclose(p.s2, pp - rng, rel_tol=1e-12)
        assert math.isclose(p.s3, p.s1 - rng, rel_tol=1e-12)
        assert math.isclose(p.s4, p.s2 - rng, rel_tol=1e-12)

    def test_r4_s4_convention(self):
        # R4 = P + 2*range, S4 = P - 2*range
        H, L, C = 1.2000, 1.1000, 1.1500
        p = compute_pivots(H, L, C)
        rng = H - L
        assert math.isclose(p.r4, p.pp + 2 * rng, rel_tol=1e-12)
        assert math.isclose(p.s4, p.pp - 2 * rng, rel_tol=1e-12)

    def test_ordering_top_to_bottom(self):
        p = compute_pivots(1.2000, 1.1000, 1.1500)
        names = [n for n, _ in p.as_levels()]
        vals = [v for _, v in p.as_levels()]
        assert names == ["R4", "R3", "R2", "R1", "P", "S1", "S2", "S3", "S4"]
        # Strictly descending for a non-degenerate range.
        assert vals == sorted(vals, reverse=True)

    def test_as_levels_count(self):
        p = compute_pivots(150.0, 145.0, 148.0)
        assert len(p.as_levels()) == 9

    def test_high_below_low_raises(self):
        with pytest.raises(ValueError):
            compute_pivots(1.0, 2.0, 1.5)

    def test_nonpositive_raises(self):
        with pytest.raises(ValueError):
            compute_pivots(0.0, 0.0, 0.0)

    def test_frozen(self):
        p = compute_pivots(1.20, 1.10, 1.15)
        with pytest.raises(Exception):
            p.pp = 999  # type: ignore[misc]
