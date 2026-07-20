# -*- coding: utf-8 -*-
import unittest

from continuation_analysis import (_trading_add, future_outcome,
                                   snapshot_features)


class ContinuationAnalysisTests(unittest.TestCase):
    def test_trading_clock_crosses_lunch_both_directions(self):
        self.assertEqual(_trading_add("11:30", 30), "13:30")
        self.assertEqual(_trading_add("13:00", -15), "11:15")

    def test_snapshot_uses_only_data_before_cutoff(self):
        ticks = [
            {"time": "09:25", "price": 100.0, "vol": 10, "buyorsell": 2},
            {"time": "09:30", "price": 101.0, "vol": 10, "buyorsell": 0},
            {"time": "09:45", "price": 102.0, "vol": 10, "buyorsell": 0},
            {"time": "10:00", "price": 103.0, "vol": 10, "buyorsell": 0},
            {"time": "10:01", "price": 80.0, "vol": 9999, "buyorsell": 1},
        ]
        f = snapshot_features(ticks, "10:00", 99.0)
        self.assertEqual(f["price"], 103.0)
        self.assertEqual(f["high"], 103.0)
        self.assertGreater(f["vwap_dist"], 0)

    def test_first_touch_respects_tick_order(self):
        ticks = [
            {"time": "10:00", "price": 100.0, "vol": 1, "buyorsell": 2},
            {"time": "10:05", "price": 101.6, "vol": 1, "buyorsell": 0},
            {"time": "10:10", "price": 98.8, "vol": 1, "buyorsell": 1},
            {"time": "10:30", "price": 101.0, "vol": 1, "buyorsell": 0},
            {"time": "15:00", "price": 102.0, "vol": 1, "buyorsell": 0},
        ]
        out = future_outcome(ticks, "10:00", 100.0)
        self.assertEqual(out["first_hit"], "up")
        self.assertEqual(out["first_hit_time"], "10:05")
        self.assertAlmostEqual(out["ret_close"], 2.0)


if __name__ == "__main__":
    unittest.main()

