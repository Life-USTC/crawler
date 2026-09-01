import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ustc_crawler.discover import discover_units, unit_sources


class DiscoverTests(unittest.TestCase):
    def test_directory_is_filtered_and_hosts_deduplicated(self) -> None:
        class Response:
            text = """
            <a href='https://math.ustc.edu.cn/main.htm'>数学科学学院</a>
            <a href='https://math.ustc.edu.cn/'>数学系</a>
            <a href='https://example.com'>化学学院</a>
            """

            def raise_for_status(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "units.json"
            with patch("ustc_crawler.discover.httpx.get", return_value=Response()):
                result = discover_units(output=output)
            self.assertEqual(len(result["units"]), 2)
            sources = unit_sources(output)
            self.assertEqual(len(sources), 1)
            self.assertIn("数学系", sources[0].aliases)
            json.loads(output.read_text(encoding="utf-8"))
