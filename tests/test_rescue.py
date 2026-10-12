import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yd_rescue as yd  # noqa: E402


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scans = os.path.join(self.tmp.name, "scans")
        os.makedirs(self.scans)
        self.cloud = os.path.join(self.tmp.name, "cloud.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_file_in_other_folder_counts_as_present(self):
        write_csv(os.path.join(self.scans, "d1.csv"), ["path", "size"],
                  [["E:\\renamed folder\\IMG_1.JPG", 100]])
        write_csv(self.cloud, ["path", "size", "md5", "media_type"],
                  [["/Trip/IMG_1.jpg", 100, "", "image"],      # same name+size -> present
                   ["/Trip/IMG_2.jpg", 100, "", "image"],      # not on drive
                   ["/Trip/IMG_1.jpg.bak", 100, "", ""]])      # different name
        missing, st = yd.compare(self.cloud, self.scans)
        self.assertEqual([r["path"] for r in missing],
                         ["/Trip/IMG_2.jpg", "/Trip/IMG_1.jpg.bak"])
        self.assertEqual(st["present_n"], 1)

    def test_same_name_different_size_is_missing(self):
        write_csv(os.path.join(self.scans, "d1.csv"), ["path", "size"], [["E:\\a\\x.mp4", 5]])
        write_csv(self.cloud, ["path", "size", "md5", "media_type"], [["/a/x.mp4", 6, "", ""]])
        missing, _ = yd.compare(self.cloud, self.scans)
        self.assertEqual(len(missing), 1)

    def test_exclude_top_folder(self):
        write_csv(os.path.join(self.scans, "d1.csv"), ["path", "size"], [])
        write_csv(self.cloud, ["path", "size", "md5", "media_type"],
                  [["/Junk/a.bin", 10, "", ""], ["/Keep/b.bin", 20, "", ""]])
        missing, st = yd.compare(self.cloud, self.scans, exclude_top=["junk"])
        self.assertEqual([r["path"] for r in missing], ["/Keep/b.bin"])
        self.assertEqual(st["excluded_n"], 1)


class SafetyTests(unittest.TestCase):
    def test_safe_rel_blocks_traversal(self):
        rel = yd.safe_rel("/../../etc/passwd")
        self.assertNotIn("..", rel.split(os.sep))
        self.assertEqual(rel, os.path.join("etc", "passwd"))

    def test_safe_rel_replaces_reserved_chars(self):
        self.assertEqual(yd.safe_rel("/a/b?c:d.jpg"), os.path.join("a", "b_c_d.jpg"))

    def test_only_yandex_hosts_get_the_token(self):
        self.assertTrue(yd.is_yandex_host("https://downloader.disk.yandex.ru/preview/abc"))
        self.assertFalse(yd.is_yandex_host("https://evil.example.com/yandex.ru"))
        self.assertFalse(yd.is_yandex_host("https://yandex.ru.evil.com/x"))


class RedirectTests(unittest.TestCase):
    def redirect(self, url):
        h = yd._YandexOnlyRedirect()
        req = yd.urllib.request.Request("https://downloader.disk.yandex.ru/p",
                                        headers={"Authorization": "OAuth secret"})
        return h.redirect_request(req, None, 302, "Found", {}, url)

    def test_redirect_to_foreign_host_is_refused(self):
        for bad in ("https://evil.example.com/x", "https://yandex.ru.evil.com/x",
                    "http://downloader.disk.yandex.ru/x"):
            with self.assertRaises(yd.urllib.error.URLError):
                self.redirect(bad)

    def test_redirect_to_yandex_is_followed(self):
        new = self.redirect("https://downloader.disk.yandex.net/x")
        self.assertEqual(new.full_url, "https://downloader.disk.yandex.net/x")


class PassesTests(unittest.TestCase):
    def test_retries_failed_items_and_stops(self):
        calls = {}

        def work(row):
            calls[row["path"]] = calls.get(row["path"], 0) + 1
            return row["path"] != "dead" and (row["path"] != "flaky" or calls["flaky"] >= 2)

        with tempfile.TemporaryDirectory() as t:
            left = yd.run_passes([{"path": "ok"}, {"path": "flaky"}, {"path": "dead"}], work,
                                 workers=2, pause=0, stop_after=2,
                                 pending_out=os.path.join(t, "p.csv"))
        self.assertEqual([r["path"] for r in left], ["dead"])
        self.assertGreaterEqual(calls["flaky"], 2)


class WatchdogTests(unittest.TestCase):
    def test_stuck_item_does_not_block_the_run(self):
        import time

        def work(row):
            if row["path"] == "hang":
                time.sleep(60)          # simulates a socket call that never returns
            return True

        started = time.time()
        with tempfile.TemporaryDirectory() as t:
            left = yd.run_passes([{"path": "ok1"}, {"path": "hang"}, {"path": "ok2"}], work,
                                 workers=2, pause=0, stop_after=1,
                                 pending_out=os.path.join(t, "p.csv"), stall=1)
        self.assertLess(time.time() - started, 15)
        self.assertEqual([r["path"] for r in left], ["hang"])


class WatchTests(unittest.TestCase):
    def test_count_files_ignores_part_and_pending(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "a", "b"))
            open(os.path.join(t, "a", "b", "x.jpg"), "wb").write(b"123")
            open(os.path.join(t, "a", "y.jpg"), "wb").write(b"12")
            open(os.path.join(t, "a", "z.jpg.part"), "wb").write(b"1")
            open(os.path.join(t, "_pending_previews.csv"), "w").write("path\n")
            self.assertEqual(yd.count_files(t), (2, 5))
        self.assertEqual(yd.count_files(os.path.join(t, "missing")), (0, 0))

    def test_watch_once_runs(self):
        with tempfile.TemporaryDirectory() as t:
            lst = os.path.join(t, "l.csv")
            write_csv(lst, ["path", "size", "md5", "media_type"],
                      [["/a/1.jpg", 10, "", ""], ["/a/2.mp4", 20, "", ""]])
            yd.main(["watch", "--once", "--previews", t, lst, "--originals", t, lst])


class ScanTests(unittest.TestCase):
    def test_scan_skips_recycle_bin(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(t, "$RECYCLE.BIN"))
            os.makedirs(os.path.join(t, "photos"))
            open(os.path.join(t, "$RECYCLE.BIN", "x.jpg"), "wb").write(b"1")
            open(os.path.join(t, "photos", "a.jpg"), "wb").write(b"12")
            yd.main(["scan", t, "--label", "t", "--out-dir", out])
            rows = list(csv.DictReader(open(os.path.join(out, "t.csv"), encoding="utf-8-sig")))
            self.assertEqual([os.path.basename(r["path"]) for r in rows], ["a.jpg"])


if __name__ == "__main__":
    unittest.main()
