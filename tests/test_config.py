from __future__ import annotations

from pathlib import Path

import pytest

from flashbuy.config import ConfigError, TargetConfig, load_config

ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "product_url": "https://shopee.co.id/Produk-i.123.456",
    "start_time": "2026-10-10T00:00:00+07:00",
    "max_item_price": 100_000,
    "max_total": 120_000,
}


def test_example_config_loads():
    cfg = load_config(ROOT / "target.example.yaml")
    assert cfg.payment == "ShopeePay"
    assert cfg.web.lead_ms == 150 and cfg.android.lead_ms == 300
    assert cfg.variant == "128GB Hitam"
    assert cfg.max_item_price == 1_500_000 and cfg.max_total == 1_550_000
    assert cfg.expected_name == "Ponsel"
    assert cfg.start_time.utcoffset().total_seconds() == 7 * 3600
    assert cfg.android.package == "com.shopee.id"


def test_defaults():
    cfg = TargetConfig.model_validate(BASE)
    assert cfg.web.enabled and cfg.android.enabled
    assert cfg.variant is None and cfg.notify_webhook == ""
    assert cfg.start_epoch == pytest.approx(1791565200.0)


@pytest.mark.parametrize("patch, loc", [
    ({"start_time": "2026-10-10T00:00:00"}, "start_time"),
    ({"product_url": "https://tokopedia.com/x"}, "product_url"),
    ({"product_url": "http://shopee.co.id/x"}, "product_url"),
    ({"payment": "COD"}, "payment"),
    ({"lead_ms": 150}, "per platform"),  # kunci lama tingkat atas: pesan migrasi ke web.lead_ms / android.lead_ms
    ({"web": {"lead_ms": -1}}, "lead_ms"),
    ({"android": {"lead_ms": 5000}}, "lead_ms"),
    ({"pin": "123456"}, "pin"),
    ({"quantity": 2}, "quantity"),
    ({"notify_webhook": "ftp://x"}, "notify_webhook"),
    ({"web": {"enabled": False}, "android": {"enabled": False}}, "(root)"),
    ({"max_item_price": 0}, "max_item_price"),
    ({"max_item_price": -5}, "max_item_price"),
    ({"max_total": 90_000}, "max_total harus"),
    ({"max_item_price": "murah"}, "max_item_price"),
])
def test_invalid(tmp_path, patch, loc):
    import yaml

    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump({**BASE, **patch}), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert loc in str(ei.value)


@pytest.mark.parametrize("missing", ["max_item_price", "max_total"])
def test_price_limits_required(tmp_path, missing):
    import yaml

    data = {k: v for k, v in BASE.items() if k != missing}
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=missing):
        load_config(p)


def test_limits_property():
    cfg = TargetConfig.model_validate({**BASE, "expected_name": "  "})
    assert cfg.expected_name is None
    assert cfg.limits.max_item_price == 100_000 and cfg.limits.max_total == 120_000


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="tidak ditemukan"):
        load_config(tmp_path / "nope.yaml")


def test_not_mapping(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)
