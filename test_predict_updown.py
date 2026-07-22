# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import predict_updown


class TencentKlineFallbackTests(unittest.TestCase):
    def test_success_is_cached_and_network_failure_uses_cache(self):
        bars = [["2026-07-17", "100", "101", "102", "99", "12345"]]
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"sz300308": {"qfqday": bars}}}

        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.object(predict_updown, "CACHE", cache_dir), \
                patch.object(predict_updown.time, "sleep"):
            with patch.object(predict_updown.S, "get", return_value=response):
                self.assertEqual(predict_updown.tencent_kline("300308", 90),
                                 [("2026-07-17", 101.0)])

            with patch.object(predict_updown.S, "get",
                              side_effect=requests.ConnectionError("offline")):
                self.assertEqual(predict_updown.tencent_kline("300308", 90),
                                 [("2026-07-17", 101.0)])

    def test_no_cache_raises_readable_remote_error(self):
        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.object(predict_updown, "CACHE", cache_dir), \
                patch.object(predict_updown.time, "sleep"), \
                patch.object(predict_updown.S, "get",
                             side_effect=requests.ConnectionError("offline")):
            with self.assertRaisesRegex(predict_updown.RemoteDataError,
                                        "腾讯日K接口暂不可用"):
                predict_updown.tencent_kline("300308", 90)


if __name__ == "__main__":
    unittest.main()
