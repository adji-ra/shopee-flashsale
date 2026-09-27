from __future__ import annotations

from flashbuy import selector_store
from flashbuy.cli import precheck_web
from flashbuy.runner_base import RunStatus
from tests.conftest import HEADLESS, make_cfg


def _pre(mock, admin, tmp_path, run, **scenario):
    admin.scenario(**scenario)
    cfg = make_cfg(mock, tmp_path, mock.now() + 3600)
    return run(precheck_web(cfg, selector_store.defaults(), headless=HEADLESS))


def _item(pre, name):
    return next(i for i in pre.items if i.name == name)


def test_precheck_ok(mock, admin, tmp_path, run, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pre = _pre(mock, admin, tmp_path, run)
    assert pre.ok and pre.status is None
    assert _item(pre, "saldo ShopeePay").ok is True
    assert "Rp111,000" in _item(pre, "saldo ShopeePay").detail  # 99.000 + ongkir 12.000
    assert admin.log("buy") == [] and admin.log("cart") == []  # tidak menambah ke keranjang


def test_precheck_login_expired(mock, admin, tmp_path, run, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pre = _pre(mock, admin, tmp_path, run, name="login_expired")
    assert pre.status == RunStatus.LOGIN_REQUIRED and not pre.ok


def test_precheck_no_address_low_balance(mock, admin, tmp_path, run, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pre = _pre(mock, admin, tmp_path, run, address=None, balance=50_000)
    assert not pre.ok and pre.status is None
    assert _item(pre, "alamat default").ok is False
    assert _item(pre, "saldo ShopeePay").ok is False


def test_precheck_unreadable_balance_is_warning(mock, admin, tmp_path, run, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pre = _pre(mock, admin, tmp_path, run, balance=None)
    assert pre.ok  # peringatan saja
    assert _item(pre, "saldo ShopeePay").ok is None
    assert pre.warnings
