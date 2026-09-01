import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.store import Store


class AssetStorageTests(unittest.TestCase):
    def test_empty_asset_body_is_an_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            url = "https://example.ustc.edu.cn/empty.docx"

            path = store.save_asset(
                url=url,
                source_url="https://example.ustc.edu.cn/article.htm",
                body=b"",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            row = store.db.execute(
                "SELECT status,error,size,local_path FROM assets WHERE url=?", (url,)
            ).fetchone()
            store.close()

        self.assertIsNone(path)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "empty response body")
        self.assertEqual(row["size"], 0)
        self.assertFalse(row["local_path"])


if __name__ == "__main__":
    unittest.main()
