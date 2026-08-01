# -*- coding: utf-8 -*-
import unittest

from resistance_levels import (chip_profile, crash_guard, env_stance,
                               limit_up_price, post_cutoff_extremes,
                               profile_peaks, round_number_levels,
                               snap_to_level, structural_levels, surge_guard,
                               swing_points, _cluster_levels, _nearest_by,
                               _time_stats)


def _bar(date, o, c, h, l, v=1000):
    return {"date": date, "open": o, "close": c, "high": h, "low": l, "vol": v}


class ResistanceLevelsTests(unittest.TestCase):
    def test_limit_up_by_board(self):
        self.assertAlmostEqual(limit_up_price("300308", 100.0), 120.0)
        self.assertAlmostEqual(limit_up_price("688256", 100.0), 120.0)
        self.assertAlmostEqual(limit_up_price("600183", 100.0), 110.0)
        self.assertAlmostEqual(limit_up_price("830001", 100.0), 130.0)

    def test_chip_profile_peak_at_volume_cluster(self):
        # 大量成交集中在 100~102, 其余分散 → 峰应落在该区间
        bars = [_bar(f"2026-01-{i+1:02d}", 100, 101, 102, 100, 50000)
                for i in range(10)]
        bars += [_bar(f"2026-02-{i+1:02d}", 90 + i, 91 + i, 92 + i, 89 + i, 100)
                 for i in range(10)]
        peaks = profile_peaks(chip_profile(bars, bins=40, decay=1.0))
        self.assertTrue(peaks)
        self.assertTrue(100 <= peaks[0][0] <= 102.5)

    def test_swing_points_detects_local_extremes(self):
        highs_seq = [10, 11, 15, 11, 10, 9, 8, 9, 10]
        bars = [_bar(f"2026-01-{i+1:02d}", h - 1, h - 0.5, h, h - 2)
                for i, h in enumerate(highs_seq)]
        highs, lows = swing_points(bars)
        self.assertTrue(any(abs(s["price"] - 15) < 1e-9 for s in highs))
        self.assertTrue(any(abs(s["price"] - 6) < 1e-9 for s in lows))

    def test_cluster_merges_nearby_levels(self):
        merged = _cluster_levels([
            {"price": 100.0, "strength": 2, "tags": ["前高"]},
            {"price": 100.3, "strength": 3, "tags": ["筹码密集"]},
            {"price": 105.0, "strength": 1, "tags": ["整数关口"]},
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["tags"], ["前高", "筹码密集"])
        self.assertEqual(merged[0]["strength"], 5)

    def test_round_levels_excludes_current_price(self):
        levels = round_number_levels(100.0, 115.0, 90.0)
        self.assertIn(110.0, levels)
        self.assertNotIn(100.0, levels)

    def test_structural_levels_sides_and_cap(self):
        bars = [_bar(f"2026-01-{i+1:02d}", 99, 100, 101, 98) for i in range(70)]
        res, sup, limit_up = structural_levels(bars, 100.0, 100.0, "600183")
        self.assertAlmostEqual(limit_up, 110.0)
        self.assertTrue(all(lv["price"] > 100.0 for lv in res))
        self.assertTrue(all(lv["price"] < 100.0 for lv in sup))
        self.assertTrue(any("涨停价" in lv["tags"] for lv in res))

    def test_snap_within_tolerance_only(self):
        levels = [{"price": 101.0, "tags": ["前高"]}]
        self.assertIsNotNone(snap_to_level(100.8, levels, tol=0.006))
        self.assertIsNone(snap_to_level(98.0, levels, tol=0.006))

    def test_env_stance_shifts_quantile_band(self):
        step_strong, _, _ = env_stance(1.2, {"上证指数": 0.5})
        step_weak, _, _ = env_stance(-1.0, {"上证指数": -1.5})
        step_neutral, _, _ = env_stance(0.1, {"上证指数": 0.0})
        self.assertEqual(step_strong, 1)
        self.assertEqual(step_weak, -1)
        self.assertEqual(step_neutral, 0)

    def test_post_cutoff_extremes_ignores_earlier_ticks(self):
        ticks = [
            {"time": "09:30", "price": 999.0, "vol": 1, "buyorsell": 0},
            {"time": "10:05", "price": 103.0, "vol": 1, "buyorsell": 0},
            {"time": "10:30", "price": 98.0, "vol": 1, "buyorsell": 1},
        ]
        ex = post_cutoff_extremes(ticks, "10:00", 100.0)
        self.assertAlmostEqual(ex["high_px"], 103.0)
        self.assertEqual(ex["high_time"], "10:05")
        self.assertAlmostEqual(ex["mae"], -2.0)

    def test_nearest_by_orders_by_similarity(self):
        rows = [{"features": {"a": v, "b": v}} for v in (0.0, 1.0, 5.0)]
        out = _nearest_by(rows, {"a": 0.9, "b": 1.1}, ("a", "b"), limit=2)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0]["features"]["a"], 1.0)

    def test_time_stats_share_before_lunch(self):
        t = _time_stats(["09:45", "10:20", "11:00", "14:10"])
        self.assertEqual(t["before_1130"], 0.75)
        self.assertEqual(t["median"], "11:00")

    @staticmethod
    def _surge_rows():
        # 20个普通日(龙头平淡, 上冲~1%) + 10个龙头联动日(上冲~5%)
        rows = [{"features": {"leader_pct": 0.2, "volume_ratio": 1.0,
                              "flow_strength": 0.5, "range_pos": 60,
                              "self_pct": 0.5},
                 "outcome": {"mfe": 1.0}} for _ in range(20)]
        rows += [{"features": {"leader_pct": 4.0, "volume_ratio": 2.5,
                               "flow_strength": 4.0, "range_pos": 98,
                               "self_pct": 3.5},
                  "outcome": {"mfe": 5.0}} for _ in range(10)]
        return rows

    def test_surge_guard_calibrates_and_activates(self):
        target = {"leader_pct": 4.5, "volume_ratio": 2.6, "flow_strength": 4.0,
                  "range_pos": 99, "self_pct": 4.0}
        s = surge_guard(self._surge_rows(), target, surge_thr=3.0)
        self.assertTrue(s["active"])
        self.assertAlmostEqual(s["base_rate"], 10 / 30, places=2)
        leader_cond = next(c for c in s["conditions"] if c["key"] == "leader")
        self.assertEqual(leader_cond["hist_rate"], 1.0)
        self.assertIsNotNone(s["cond_quantiles"])
        self.assertAlmostEqual(s["cond_quantiles"]["q50"], 5.0)

    def test_surge_guard_stays_quiet_on_normal_day(self):
        target = {"leader_pct": 0.5, "volume_ratio": 1.1, "flow_strength": 1.0,
                  "range_pos": 70, "self_pct": 1.0}
        s = surge_guard(self._surge_rows(), target, surge_thr=3.0)
        self.assertFalse(s["active"])

    def test_surge_guard_board_plus_leader_activates(self):
        # 仅龙头一个校准条件+板块强 → 也应触发
        target = {"leader_pct": 3.5, "volume_ratio": 1.0, "flow_strength": 0.5,
                  "range_pos": 60, "self_pct": 1.0}
        s = surge_guard(self._surge_rows(), target, surge_thr=3.0, board_pct=2.5)
        self.assertTrue(s["active"])
        s2 = surge_guard(self._surge_rows(), target, surge_thr=3.0, board_pct=0.5)
        self.assertFalse(s2["active"])

    @staticmethod
    def _crash_rows():
        # 20个普通日(回撤~-1%) + 10个联动杀跌日(下杀~-5%)
        rows = [{"features": {"leader_pct": -0.2, "volume_ratio": 1.0,
                              "flow_strength": -0.5, "range_pos": 40,
                              "self_pct": -0.5},
                 "outcome": {"mae": -1.0}} for _ in range(20)]
        rows += [{"features": {"leader_pct": -4.0, "volume_ratio": 2.5,
                               "flow_strength": -4.0, "range_pos": 2,
                               "self_pct": -3.5},
                  "outcome": {"mae": -5.0}} for _ in range(10)]
        return rows

    def test_crash_guard_calibrates_and_activates(self):
        target = {"leader_pct": -4.5, "volume_ratio": 2.6, "flow_strength": -4.0,
                  "range_pos": 1, "self_pct": -4.0}
        s = crash_guard(self._crash_rows(), target, crash_thr=-3.0)
        self.assertTrue(s["active"])
        leader_cond = next(c for c in s["conditions"] if c["key"] == "leader")
        self.assertEqual(leader_cond["hist_rate"], 1.0)
        self.assertIsNotNone(s["cond_quantiles"])
        self.assertAlmostEqual(s["cond_quantiles"]["q50"], -5.0)

    def test_crash_guard_quiet_on_normal_day(self):
        target = {"leader_pct": 0.5, "volume_ratio": 1.1, "flow_strength": 1.0,
                  "range_pos": 70, "self_pct": 1.0}
        s = crash_guard(self._crash_rows(), target, crash_thr=-3.0)
        self.assertFalse(s["active"])


if __name__ == "__main__":
    unittest.main()
