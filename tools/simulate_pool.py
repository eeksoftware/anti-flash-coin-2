"""Network-free coarse API pool load simulator."""
import argparse
import asyncio
import csv
from pathlib import Path

from infra.key_pool import Outcome, RateLimitedPool


async def simulate(keys: int, wallets: int, interval: float, duration: float):
    pool = RateLimitedPool([f"fake-{n}" for n in range(keys)], {"rate": 4, "burst": 2}, sleeper=asyncio.sleep)
    counts = [0] * keys
    arrivals = []
    async def worker(index):
        end = asyncio.get_running_loop().time() + duration
        while asyncio.get_running_loop().time() < end:
            lease = await pool.acquire(timeout=max(1, interval))
            counts[int(lease.key_label.split("-")[1])-1] += 1
            arrivals.append(asyncio.get_running_loop().time())
            pool.report(lease, Outcome.OK)
            await asyncio.sleep(interval)
    await asyncio.gather(*(worker(i) for i in range(wallets)))
    arrivals.sort()
    max_window = max((sum(1 for value in arrivals if start <= value < start + 1) for start in arrivals), default=0)
    return counts, max_window


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", type=int, default=1)
    parser.add_argument("--wallets", type=int, default=10)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--duration", type=float, default=60)
    args = parser.parse_args()
    counts, max_window = await simulate(args.keys, args.wallets, args.interval, args.duration)
    total = sum(counts)
    print("key calls:", ", ".join(f"key-{i+1}={value}" for i, value in enumerate(counts)))
    per_day_per_wallet = 86400 / max(args.interval, 0.1)
    sustainable = int(95000 / max(1, per_day_per_wallet))
    print(f"total={total}; output_rate={total/max(args.duration, 1):.2f}/s; daily_usage={total}/100000; sustainable_wallets≈{sustainable}")
    print(f"maximum requests in any simulated 1s window: {max_window} (per-key bound: 6)")
    report_dir = Path("reports"); report_dir.mkdir(exist_ok=True)
    rows = []
    for k in (1, 2, 3, 5):
        for w in (10, 20, 50, 100):
            result, max_1s = await simulate(k, w, args.interval, min(args.duration, 10))
            rows.append({"keys": k, "wallets": w, "calls": sum(result), "calls_per_second": sum(result)/min(args.duration, 10), "max_1s": max_1s})
    with (report_dir / "pool-simulation.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    with (report_dir / "pool-simulation.md").open("w", encoding="utf-8") as file:
        file.write("| Keys | Wallets | Calls | Calls/s | Max / 1s |\n|---:|---:|---:|---:|---:|\n")
        for row in rows: file.write(f"| {row['keys']} | {row['wallets']} | {row['calls']} | {row['calls_per_second']:.2f} | {row['max_1s']} |\n")


if __name__ == "__main__": asyncio.run(main())
