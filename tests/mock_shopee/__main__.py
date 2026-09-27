"""Jalankan mock mandiri: python -m tests.mock_shopee [--port 8765] [--open-in 30]"""

import argparse
import time

from tests.mock_shopee.server import MockShopee

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=8765)
p.add_argument("--open-in", type=float, default=60.0, help="slot dibuka N detik lagi")
p.add_argument("--scenario", default="normal")
a = p.parse_args()

mock = MockShopee(port=a.port).start()
mock.apply({"name": a.scenario, "open_in_ms": int(a.open_in * 1000)})
print(f"Mock jalan di {mock.base_url}")
print(f"Produk : {mock.product_url}")
print(f"Buka   : {time.strftime('%H:%M:%S', time.localtime(mock.scenario.open_at))}")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    mock.stop()
