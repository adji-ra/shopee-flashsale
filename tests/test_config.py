from __future__ import annotations

from pathlib import Path

import pytest

from flashbuy.config import ConfigError, TargetConfig, load_config

ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "product_url": "https://shopee.co.id/Produk-i.123.456",
    "start_time": "2026-10-10T00:00:00+07:00",
}


def test_example_config_loads():
    cfg = load_config(ROOT / "target.example.yaml")
    assert cfg.payment == "ShopeePay"
    assert cfg.lead_ms == 150
    assert cfg.variant == "128GB Hitam"
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
    ({"lead_ms": -1}, "lead_ms"),
    ({"lead_ms": 5000}, "lead_ms"),
    ({"pin": "123456"}, "pin"),
    ({"quantity": 2}, "quantity"),
    ({"notify_webhook": "ftp://x"}, "notify_webhook"),
    ({"web": {"enabled": False}, "android": {"enabled": False}}, "(root)"),
])
def test_invalid(tmp_path, patch, loc):
    import yaml

    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump({**BASE, **patch}), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert loc in str(ei.value)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="tidak ditemukan"):
        load_config(tmp_path / "nope.yaml")


def test_not_mapping(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)
