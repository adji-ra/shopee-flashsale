"""Load & validasi target.yaml.

Batasan keras (rate limit, jendela polling, qty) sengaja berupa konstanta, bukan opsi config.
Kunci yang tidak dikenal ditolak (mis. `pin:`), jadi PIN tidak bisa diselipkan ke config.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

# ---- batasan keras (jangan dijadikan opsi)
MIN_ACTION_INTERVAL_MS = 400  # maks 1 aksi refresh/polling per 400 ms
POLL_WINDOW_BEFORE_S = 1.0  # polling hanya mulai T-1 s ...
POLL_WINDOW_AFTER_S = 8.0  # ... sampai T+8 s
MAX_QUANTITY = 1
FLOW_TIMEOUT_S = 30.0  # setelah klik Beli berhasil: batas total sampai checkout/PIN
PRECHECK_BEFORE_S = 600  # pre-check T-10 menit
RESYNC_BEFORE_S = 120  # timesync ulang T-2 menit
OPEN_PAGE_BEFORE_S = 60  # buka halaman produk T-60 s
RESYNC_WARN_MS = 50  # offset baru beda > 50 ms -> peringatan
DEFAULT_LEAD_MS = 150

SHOPEE_HOSTS = ("shopee.co.id",)
LOCAL_HOSTS = ("127.0.0.1", "localhost")  # hanya untuk tes (mock), lewat validation context


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WebConfig(_Strict):
    enabled: bool = True
    profile_dir: Path = Path("chrome-profile")
    channel: str | None = "chrome"  # "chrome" | "msedge" | null (Chromium bawaan Playwright)
    block_media: bool = False  # blokir gambar/video/font untuk mempercepat


class AndroidConfig(_Strict):
    enabled: bool = True
    serial: str = ""  # kosong = device pertama
    package: str = "com.shopee.id"


class TargetConfig(_Strict):
    product_url: str
    variant: str | None = None
    start_time: datetime
    payment: Literal["ShopeePay"] = "ShopeePay"
    # Pengaman harga (wajib, fail-closed). Rupiah bulat.
    max_item_price: int = Field(gt=0)  # harga satuan maksimum (harga flash)
    max_total: int = Field(gt=0)  # total pembayaran maks, termasuk ongkir & biaya layanan
    expected_name: str | None = None  # substring nama produk (case-insensitive)
    lead_ms: int = Field(DEFAULT_LEAD_MS, ge=0, le=1000)
    web: WebConfig = WebConfig()
    android: AndroidConfig = AndroidConfig()
    notify_webhook: str = ""

    @field_validator("product_url")
    @classmethod
    def _shopee_url(cls, v: str, info: ValidationInfo) -> str:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if (info.context or {}).get("allow_local") and host in LOCAL_HOSTS:
            return v
        if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in SHOPEE_HOSTS):
            raise ValueError("harus URL https produk di shopee.co.id")
        return v

    @field_validator("variant", "expected_name")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v or None

    @field_validator("start_time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("wajib pakai zona waktu, mis. 2026-10-10T00:00:00+07:00")
        return v

    @field_validator("notify_webhook")
    @classmethod
    def _webhook(cls, v: str) -> str:
        if v and urlsplit(v).scheme not in ("http", "https"):
            raise ValueError("notify_webhook harus URL http(s) atau kosong")
        return v

    @model_validator(mode="after")
    def _one_platform(self) -> TargetConfig:
        if not (self.web.enabled or self.android.enabled):
            raise ValueError("minimal satu jalur (web/android) harus enabled")
        if self.max_total < self.max_item_price:
            raise ValueError("max_total harus >= max_item_price")
        return self

    @property
    def limits(self):
        from flashbuy.pricing import Limits

        return Limits(self.max_item_price, self.max_total, self.expected_name)

    @property
    def start_epoch(self) -> float:
        return self.start_time.timestamp()

    @property
    def origin(self) -> str:
        parts = urlsplit(self.product_url)
        return f"{parts.scheme}://{parts.netloc}"


def require_live_ready(cfg: TargetConfig) -> None:
    """Syarat tambahan mode --live: `expected_name` wajib (pengaman nama produk di keranjang & checkout)."""
    if not cfg.expected_name:
        raise ConfigError("mode --live ditolak: `expected_name` wajib diisi di target.yaml "
                          "(potongan nama produk, mis. \"Ponsel X 128GB\")")


def load_config(path: str | Path, *, allow_local: bool = False) -> TargetConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"file config tidak ditemukan: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML tidak valid di {path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: isi harus mapping YAML")
    try:
        return TargetConfig.model_validate(raw, context={"allow_local": allow_local})
    except ValidationError as e:
        lines = [f"  - {'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"config {path} tidak valid:\n" + "\n".join(lines)) from None
