#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yd_rescue.py - rescue files from Yandex.Disk when the website / downloads are down.

Subcommands:
  list      snapshot the full file list of your cloud via the REST API
  scan      describe a local drive or folder (read-only)
  compare   find cloud files that are not present on any scanned drive
  previews  download preview (reduced-size) copies of images
  download  download original files (fast-fail, several passes)
  watch     print a progress report every N minutes while the jobs above run

Only the Python standard library is used. The OAuth token is read from the
YADISK_TOKEN environment variable or from a file (default: token.txt) and is
never accepted on the command line and never written anywhere.
"""
import argparse
import csv
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff",
             ".webp", ".cr2", ".nef", ".dng", ".arw", ".orf", ".rw2")
SKIP_DIRS = {"$recycle.bin", "system volume information", ".trash", ".trashes",
             ".spotlight-v100", ".fseventsd"}
YANDEX_SUFFIXES = (".yandex.ru", ".yandex.net", ".yandex.com", ".yandex.by", ".yandex.kz")

_log_lock = threading.Lock()
_log_file = None


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------- helpers

def load_token():
    tok = os.environ.get("YADISK_TOKEN", "")
    if not tok:
        path = os.environ.get("YADISK_TOKEN_FILE", "token.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                tok = f.read()
    tok = tok.strip()
    if not tok:
        sys.exit("Token not found. Put it in the YADISK_TOKEN environment variable "
                 "or in a file token.txt (see README).")
    return tok


def api_get(path_and_query, token, timeout=20):
    req = urllib.request.Request(API + path_and_query,
                                 headers={"Authorization": "OAuth " + token})
    with _opener.open(req, timeout=timeout) as r:
        return json.load(r)


def is_yandex_host(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host.endswith(YANDEX_SUFFIXES)


class _YandexOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https Yandex hosts: urllib would otherwise carry the
    Authorization header to whatever host a redirect points at."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https" or not is_yandex_host(newurl):
            raise urllib.error.URLError("redirect to a non-Yandex host refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_YandexOnlyRedirect)


def safe_rel(cloud_path):
    """Cloud path -> safe relative local path (no '..', no reserved characters)."""
    parts = []
    for p in cloud_path.replace("\\", "/").split("/"):
        if p in ("", ".", ".."):
            continue
        p = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in p)
        p = p.rstrip(" .") or "_"
        parts.append(p)
    return os.path.join(*parts) if parts else "_"


def lp(path):
    """Long-path prefix on Windows."""
    path = os.path.abspath(path)
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        return "\\\\?\\" + path
    return path


def atomic_write(dst, data):
    os.makedirs(lp(os.path.dirname(dst)), exist_ok=True)
    part = dst + ".part"
    with open(lp(part), "wb") as f:
        f.write(data)
    os.replace(lp(part), lp(dst))


def fmt_gb(n):
    return "%.1f GB" % (n / 1024 ** 3)


def run_passes(rows, work_fn, workers, pause, stop_after, pending_out, stall=120):
    """Try every row; remember the ones that failed; try them again.

    Yandex serves files erratically during an outage: some are available, some
    time out. So we do not hang on a dead file - we skip it and come back later.
    Stops when `stop_after` passes in a row bring nothing new.

    Watchdog: a socket call can hang for good (a timeout covers one read, not a
    DNS lookup or a trickling connection). If nothing finishes for `stall`
    seconds, the pass is abandoned (the stuck daemon threads are left behind)
    and the unfinished rows go to the next pass.
    """
    # Legacy generic helper retained for callers; actual transfers use bounded,
    # joined workers in rescue.cli/engine and never abandon a live writer.
    pending = rows
    empty = 0
    n = 0
    while pending:
        n += 1
        batch = pending
        todo = queue.Queue()
        for i in range(len(batch)):
            todo.put(i)
        done = {}
        stop = threading.Event()
        state = {"last": time.time()}
        lock = threading.Lock()

        def worker():
            while not stop.is_set():
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    ok = bool(work_fn(batch[i]))
                except Exception:
                    ok = False
                with lock:
                    done[i] = ok
                    state["last"] = time.time()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
            if time.time() - state["last"] > stall:
                log("pass %d: nothing finished for %ds, abandoning the stuck rest" % (n, stall))
                stop.set()
                break
        with lock:
            remaining = [r for i, r in enumerate(batch) if not done.get(i)]
        got = len(batch) - len(remaining)
        log("pass %d: done %d, still unavailable %d" % (n, got, len(remaining)))
        with open(pending_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["path", "size", "md5", "media_type"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(remaining)
        pending = remaining
        if not pending:
            break
        empty = empty + 1 if got == 0 else 0
        if empty >= stop_after:
            log("stop: %d empty passes in a row; %d files left in %s"
                % (stop_after, len(pending), pending_out))
            break
        time.sleep(pause)
    return pending


# ---------------------------------------------------------------- list

def cmd_list(args):
    token = load_token()
    offset = 0
    n = total = 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "size", "md5", "media_type"])
        while True:
            page = min(1000, args.limit - n) if args.limit else 1000
            q = ("/resources/files?limit=%d&offset=%d"
                 "&fields=items.path,items.size,items.md5,items.media_type" % (page, offset))
            for attempt in range(6):
                try:
                    data = api_get(q, token, timeout=60)
                    break
                except Exception as e:
                    wait = min(60, 5 * (attempt + 1))
                    log("retry offset=%d (%s) in %ds" % (offset, type(e).__name__, wait))
                    time.sleep(wait)
            else:
                sys.exit("API does not answer; try again later.")
            items = data.get("items", [])
            if not items:
                break
            for it in items:
                p = it.get("path", "")
                p = p[len("disk:"):] if p.startswith("disk:") else p
                size = it.get("size", 0) or 0
                w.writerow([p, size, it.get("md5", ""), it.get("media_type", "")])
                n += 1
                total += size
            log("%d files, %s" % (n, fmt_gb(total)))
            offset += len(items)
            if args.limit and n >= args.limit:
                break
    log("done: %d files, %s -> %s" % (n, fmt_gb(total), args.out))


# ---------------------------------------------------------------- scan

def cmd_scan(args):
    root = args.path
    if not os.path.isdir(root):
        sys.exit("Not a folder: %s" % root)
    label = args.label or os.path.basename(os.path.abspath(root).rstrip("\\/")) or "drive"
    label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, label + ".csv")
    n = total = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "size"])
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                w.writerow([full, size])
                n += 1
                total += size
    log("scanned %s: %d files, %s -> %s" % (root, n, fmt_gb(total), out))


# ---------------------------------------------------------------- compare

def _name_key(path, size):
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (name, int(size))


def load_scan_keys(scans_dir):
    keys = set()
    for fn in os.listdir(scans_dir):
        if not fn.lower().endswith(".csv"):
            continue
        with open(os.path.join(scans_dir, fn), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    keys.add(_name_key(r["path"], r["size"]))
                except (KeyError, ValueError):
                    continue
    return keys


def compare(cloud_csv, scans_dir, exclude_top=()):
    """Return (missing_rows, stats). A file counts as present if some scanned
    drive has a file with the same name and exact size (in any folder)."""
    keys = load_scan_keys(scans_dir)
    exclude = {e.lower() for e in exclude_top}
    missing = []
    stats = {"cloud_n": 0, "cloud_b": 0, "present_n": 0, "present_b": 0,
             "missing_n": 0, "missing_b": 0, "excluded_n": 0, "excluded_b": 0}
    with open(cloud_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                size = int(r["size"])
            except (KeyError, ValueError):
                continue
            stats["cloud_n"] += 1
            stats["cloud_b"] += size
            top = r["path"].strip("/").split("/")[0].lower()
            if _name_key(r["path"], size) in keys:
                stats["present_n"] += 1
                stats["present_b"] += size
            elif top in exclude:
                stats["excluded_n"] += 1
                stats["excluded_b"] += size
            else:
                stats["missing_n"] += 1
                stats["missing_b"] += size
                missing.append(r)
    return missing, stats


def cmd_compare(args):
    missing, st = compare(args.cloud, args.scans, args.exclude or ())
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["path", "size", "md5", "media_type"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(missing)
    log("cloud:        %d files, %s" % (st["cloud_n"], fmt_gb(st["cloud_b"])))
    log("already have: %d files, %s" % (st["present_n"], fmt_gb(st["present_b"])))
    if st["excluded_n"]:
        log("excluded:     %d files, %s" % (st["excluded_n"], fmt_gb(st["excluded_b"])))
    log("ONLY IN CLOUD: %d files, %s -> %s" % (st["missing_n"], fmt_gb(st["missing_b"]), args.out))
    by_top = {}
    for r in missing:
        top = r["path"].strip("/").split("/")[0]
        by_top[top] = by_top.get(top, 0) + int(r["size"])
    for top, b in sorted(by_top.items(), key=lambda x: -x[1])[:25]:
        log("  %9s  %s" % (fmt_gb(b), top))
    log("NOTE: the match is by name + exact size. A file that was renamed or "
        "re-encoded counts as missing, so this is an upper bound.")


# ---------------------------------------------------------------- previews / download

def read_rows(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cmd_previews(args):
    from rescue.cli import run_transfer_command
    run_transfer_command(args, load_token(), preview=True)


def cmd_download(args):
    from rescue.cli import run_transfer_command
    run_transfer_command(args, load_token(), preview=False)


# ---------------------------------------------------------------- watch

def count_files(path):
    """Finished files under `path` (ignores .part and _pending* bookkeeping files)."""
    n = size = 0
    if not os.path.isdir(path):
        return n, size
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif not e.name.endswith(".part") and not e.name.startswith("_pending"):
                        n += 1
                        try:
                            size += e.stat().st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return n, size


def _eta(remaining, rate):
    if rate <= 0 or remaining <= 0:
        return "unknown"
    s = remaining / rate
    h, m = int(s // 3600), int(s % 3600 // 60)
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def cmd_watch(args):
    jobs = []   # (name, dest, total_files, total_bytes or None)
    for dest, lst in args.previews or []:
        rows = [r for r in read_rows(lst) if r["path"].lower().endswith(IMAGE_EXT)]
        jobs.append(("previews -> " + dest, dest, len(rows), None))
    for dest, lst in args.originals or []:
        rows = read_rows(lst)
        jobs.append(("originals -> " + dest, dest, len(rows), sum(int(r["size"]) for r in rows)))
    if not jobs:
        sys.exit("Give at least one --previews DEST LIST or --originals DEST LIST")
    prev = {}
    last = time.time()
    first = True
    while True:
        now = time.time()
        dt = max(now - last, 1)
        mins = max(int(dt // 60), 1)
        log("--- progress report%s ---" % ("" if first else " (last %d min)" % mins))
        for name, dest, tot_n, tot_b in jobs:
            n, b = count_files(dest)
            pn, pb = prev.get(name, (n, b))
            dn, db = n - pn, b - pb
            warn = "" if first or dn > 0 else "  !! no progress in this interval"
            if tot_b:
                left = max(tot_b - b, 0)
                log("%s: %d/%d files, %s of %s (%.1f%%) | +%d files, +%s | left %s, ETA %s%s"
                    % (name, n, tot_n, fmt_gb(b), fmt_gb(tot_b), b * 100.0 / tot_b, dn,
                       fmt_gb(db), fmt_gb(left), _eta(left, db / dt), warn))
            else:
                left = max(tot_n - n, 0)
                log("%s: %d/%d images (%.1f%%) | +%d | left %d, ETA %s%s"
                    % (name, n, tot_n, n * 100.0 / max(tot_n, 1), dn, left,
                       _eta(left, dn / dt), warn))
            prev[name] = (n, b)
        seen = set()
        for _, dest, _, _ in jobs:
            try:
                root = os.path.splitdrive(os.path.abspath(dest))[0] or os.path.abspath(dest)
                if root not in seen:
                    seen.add(root)
                    log("free space on %s: %s" % (root, fmt_gb(shutil.disk_usage(dest).free)))
            except OSError:
                log("destination not reachable: %s  !! is the drive connected?" % dest)
        if args.once:
            return
        first = False
        last = now
        time.sleep(args.interval)


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(prog="yd_rescue", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", help="also append the log to this file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="snapshot the cloud file list")
    s.add_argument("--out", default="cloud_files.csv")
    s.add_argument("--limit", type=int, default=0, help="stop after N files (for testing)")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("scan", help="describe a local drive/folder (read-only)")
    s.add_argument("path")
    s.add_argument("--label", help="name for this drive (default: folder name)")
    s.add_argument("--out-dir", default="scans")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("compare", help="find files that exist only in the cloud")
    s.add_argument("--cloud", default="cloud_files.csv")
    s.add_argument("--scans", default="scans")
    s.add_argument("--out", default="only_in_cloud.csv")
    s.add_argument("--exclude", nargs="*", help="top-level cloud folders to ignore")
    s.set_defaults(fn=cmd_compare)

    for name, fn, help_ in (("previews", cmd_previews, "download image previews"),
                            ("download", cmd_download, "download original files")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--list", default="only_in_cloud.csv")
        s.add_argument("--dest", required=True)
        s.add_argument("--workers", type=int, default=8)
        s.add_argument("--timeout", type=int, default=12,
                       help="seconds before giving up on one file in a pass")
        s.add_argument("--pause", type=int, default=30, help="seconds between passes")
        s.add_argument("--stop-after", type=int, default=3,
                       help="stop after N passes in a row that bring nothing")
        if name == "previews":
            s.add_argument("--size", default="XXXL",
                           help="S, M, L, XL, XXL, XXXL or WxH (default XXXL)")
        else:
            s.add_argument("--only-ext", nargs="*", help="e.g. .mp4 .mov")
        s.set_defaults(fn=fn)

    s = sub.add_parser("watch", help="print a progress report every N minutes")
    s.add_argument("--previews", nargs=2, action="append", metavar=("DEST", "LIST"),
                   help="a running 'previews' job (repeat for several)")
    s.add_argument("--originals", nargs=2, action="append", metavar=("DEST", "LIST"),
                   help="a running 'download' job (repeat for several)")
    s.add_argument("--interval", type=int, default=600, help="seconds between reports")
    s.add_argument("--once", action="store_true", help="print one report and exit")
    s.set_defaults(fn=cmd_watch)
    return p


def main(argv=None):
    global _log_file
    args = build_parser().parse_args(argv)
    _log_file = args.log
    args.fn(args)


if __name__ == "__main__":
    main()
