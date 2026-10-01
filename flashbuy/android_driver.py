"""Driver Android tipis di atas uiautomator2, plus FakeDriver untuk tes.

Hot path memakai query selector di device (1 RPC per query: exists / info / click koordinat),
BUKAN dump_hierarchy. `dump()` hanya untuk kalibrasi, diagnosa UNKNOWN, dan status akhir.

Semantik selector mengikuti UiSelector uiautomator:
  text / description          sama persis (case-sensitive)
  textContains / ...Contains  substring (case-sensitive)
  textStartsWith              awalan (case-sensitive)
  textMatches / ...Matches    regex Java, harus cocok SELURUH teks (Pattern.matches)
  resourceId / className      sama persis; resourceIdMatches = regex seluruh resource-id

Dua jalur pencarian di server uiautomator2 (dari source u2.jar):
  exists / info (jalur A)     semua jendela interaktif (urutan antarjendela tidak tentu)
  find_all = info_list (B)    hanya jendela AKTIF, cache aksesibilitas dibersihkan (SDK >= 34)
Karena itu elemen di dalam bottom sheet/dialog diambil dari find_all, bukan info.
"""

from __future__ import annotations

import re
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SEL_KINDS = ("resourceId", "resourceIdMatches", "text", "textContains", "textStartsWith", "textMatches",
             "description", "descriptionContains", "descriptionMatches", "className")


class DriverError(RuntimeError):
    """Kegagalan komunikasi dengan device / agent uiautomator2."""


class AgentDead(DriverError):
    """Agent uiautomator2 / transport adb tidak menjawab (mis. dibunuh HiOS); bisa dipulihkan dengan restart."""


# Nama exception u2/adbutils/socket yang berarti agent atau transport mati (bukan "elemen tidak ada").
_DEAD_ERRORS = {"HTTPError", "HTTPTimeoutError", "UiAutomationNotConnectedError", "ConnectionError",
                "ConnectionResetError", "ConnectionRefusedError", "TimeoutError", "timeout", "AdbError",
                "LaunchUiAutomationError", "BrokenPipeError"}


@dataclass(frozen=True)
class Sel:
    by: str
    value: str

    def __post_init__(self) -> None:
        if self.by not in SEL_KINDS:
            raise ValueError(f"jenis selector tidak dikenal: {self.by}")

    def kwargs(self) -> dict[str, str]:
        return {self.by: self.value}

    def __str__(self) -> str:
        return f"{self.by}={self.value!r}"


@dataclass(frozen=True)
class Node:
    text: str = ""
    desc: str = ""
    rid: str = ""
    cls: str = ""
    enabled: bool = True
    clickable: bool = False
    selected: bool = False
    checked: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom

    @property
    def label(self) -> str:
        """Teks yang terlihat: text, atau content-desc bila text kosong."""
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @classmethod
    def from_u2(cls, info: dict[str, Any]) -> Node:
        b = info.get("visibleBounds") or info.get("bounds") or {}
        return cls(
            text=info.get("text") or "",
            desc=info.get("contentDescription") or "",
            rid=info.get("resourceName") or "",
            cls=info.get("className") or "",
            enabled=bool(info.get("enabled", True)),
            clickable=bool(info.get("clickable", False)),
            selected=bool(info.get("selected", False)),
            checked=bool(info.get("checked", False)),
            bounds=(int(b.get("left", 0)), int(b.get("top", 0)), int(b.get("right", 0)), int(b.get("bottom", 0))),
        )


@dataclass(frozen=True)
class AppInfo:
    package: str
    activity: str = ""


class AndroidDriver(Protocol):
    """Kontrak minimal yang dipakai android_runner. Semua metode sinkron (dipanggil dari thread runner)."""

    serial: str

    def exists(self, sel: Sel) -> bool: ...

    def info(self, sel: Sel) -> Node | None: ...  # elemen pertama yang cocok, None bila tidak ada

    # Seperti info, tetapi TANPA batas package aplikasi (dialog sistem crash/ANR, aplikasi lain di atas Shopee).
    def info_any(self, sel: Sel) -> Node | None: ...

    def find_all(self, sel: Sel) -> list[Node]: ...  # semua elemen yang cocok (1 RPC)

    def click(self, target: Sel | Node) -> bool: ...  # Node -> tap tengah bounds (1 RPC)

    def get_text(self, sel: Sel) -> str | None: ...

    def current_app(self) -> AppInfo: ...

    def start_url(self, url: str, package: str, wait: bool = True) -> None: ...  # intent VIEW ke package

    def swipe_refresh(self) -> None: ...  # tarik-untuk-muat-ulang

    def press_back(self) -> None: ...

    def webview_present(self) -> bool: ...

    def screenshot(self, path: Path) -> bool: ...

    def last_toast(self) -> str | None: ...  # Toast Android = jendela terpisah, tidak ada di pohon node

    def clear_toast(self) -> None: ...

    def dump(self) -> str: ...

    def shell(self, cmd: list[str]) -> str: ...

    def agent_alive(self) -> bool | None: ...  # None = tidak bisa dicek

    def restart_agent(self) -> None: ...

    def window_size(self) -> tuple[int, int]: ...


# --------------------------------------------------------------------------- uiautomator2


RPC_TIMEOUT_S = 5.0  # batas satu query/klik ke agent (default u2: 300 s, dan timeout socket tidak dipasang)
SWIPE_STEPS = 60  # swipe_refresh 0,3 s (u2: 1 langkah = 5 ms)
SLOW_RPC_TIMEOUT_S = 30.0  # dump/screenshot/shell


def _patch_u2_transport() -> None:
    """Pasang timeout socket per request + TCP_NODELAY pada koneksi HTTP-over-adb uiautomator2.

    u2 3.x menyetel `conn.timeout` tetapi `AdbHTTPConnection.connect` tidak pernah menerapkannya
    ke socket (default adbutils 600 s), jadi satu RPC yang macet bisa menggantung run.
    """
    import socket

    from uiautomator2 import core

    cls = core.AdbHTTPConnection
    if getattr(cls, "_flashbuy_patched", False):
        return
    original = cls.connect

    def connect(self) -> None:
        original(self)
        try:
            if isinstance(self.timeout, (int, float)):
                self.sock.settimeout(self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass

    cls.connect = connect
    cls._flashbuy_patched = True


def _disable_implicit_restart(d: Any) -> bool:
    """u2 me-restart server diam-diam (berdetik-detik) bila RPC gagal; di hot path itu menyembunyikan
    agent yang dibunuh HiOS. Ganti dengan panggilan langsung: error naik ke runner yang me-restart
    secara eksplisit dan mencatatnya."""
    try:
        from uiautomator2 import core

        dev, port, debug = d._dev, d._device_server_port, d._debug
    except (ImportError, AttributeError):
        return False

    def jsonrpc_call(method: str, params: Any = None, timeout: float = RPC_TIMEOUT_S) -> Any:
        return core._jsonrpc_call(dev, port, method, params, timeout, debug)

    d.jsonrpc_call = jsonrpc_call
    return True


class U2Driver:
    """Implementasi nyata di atas uiautomator2 3.x (tanpa atx-agent)."""

    def __init__(self, serial: str = "", device: Any = None, rpc_timeout_s: float = RPC_TIMEOUT_S,
                 package: str | None = None):
        if device is None:
            import uiautomator2 as u2

            try:
                _patch_u2_transport()
                device = u2.connect(serial or None)
            except Exception as e:  # noqa: BLE001 - pesan apa pun dari adb/u2
                raise DriverError(f"gagal konek ke device {serial or '(pertama)'}: {e}") from e
            self.no_implicit_restart = _disable_implicit_restart(device)
        else:
            self.no_implicit_restart = False
        self.d = device
        self.serial = serial or getattr(device, "serial", "") or ""
        self.rpc_timeout_s = rpc_timeout_s
        self.package = package  # query dibatasi ke aplikasi ini (notifikasi/jendela sistem tidak ikut cocok)
        self._size: tuple[int, int] | None = None

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DriverError:
            raise
        except Exception as e:  # noqa: BLE001 - semua error u2/adb dibungkus
            name = type(e).__name__
            if name == "UiObjectNotFoundError":
                raise
            cls = AgentDead if name in _DEAD_ERRORS or isinstance(e, (OSError, TimeoutError)) else DriverError
            raise cls(f"{what}: {name}: {e}") from e

    def _rpc(self, method: str, *params: Any) -> Any:
        """Panggil jsonrpc agent dengan timeout per panggilan (bukan 300 s bawaan u2)."""
        return getattr(self.d.jsonrpc, method)(*params, http_timeout=self.rpc_timeout_s)

    def _selector(self, sel: Sel, any_package: bool = False) -> Any:
        kw = sel.kwargs()
        if self.package and not any_package:
            kw["packageName"] = self.package
        return self.d(**kw).selector

    def exists(self, sel: Sel) -> bool:
        return bool(self._call(f"exists {sel}", lambda: self._rpc("exist", self._selector(sel))))

    def info(self, sel: Sel, any_package: bool = False) -> Node | None:
        def get() -> Node | None:
            for attempt in (1, 2):
                try:
                    node = Node.from_u2(self._rpc("objInfo", self._selector(sel, any_package)))
                    break
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ == "UiObjectNotFoundError":
                        return None
                    # elemen dibangun ulang di antara find & getter (mis. tepat saat T): coba sekali lagi
                    if "StaleObject" in str(e):
                        if attempt == 1:
                            continue
                        return None
                    raise
            # objInfo bisa jatuh ke jalur B (contains/startsWith tidak peka huruf): cek ulang di klien
            return node if node_matches(node, sel) else None

        return self._call(f"info {sel}", get)

    def info_any(self, sel: Sel) -> Node | None:
        return self.info(sel, any_package=True)

    def find_all(self, sel: Sel) -> list[Node]:
        def get() -> list[Node]:
            try:
                infos = self._rpc("objInfoOfAllInstances", self._selector(sel)) or []
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "UiObjectNotFoundError":
                    return []
                raise
            # elemen yang hilang di tengah iterasi server menjadi null di array
            return [n for n in (Node.from_u2(i) for i in infos if i) if node_matches(n, sel)]

        return self._call(f"find_all {sel}", get)

    def click(self, target: Sel | Node) -> bool:
        node = self.info(target) if isinstance(target, Sel) else target
        if node is None:
            return False
        x, y = node.center
        self._call(f"click {x},{y}", lambda: self._rpc("click", x, y))
        return True

    def get_text(self, sel: Sel) -> str | None:
        node = self.info(sel)
        return None if node is None else node.label

    def current_app(self) -> AppInfo:
        info = self._call("app_current", self.d.app_current)
        return AppInfo(info.get("package", ""), info.get("activity", ""))

    def start_url(self, url: str, package: str, wait: bool = True) -> None:
        # -W menunggu activity selesai diluncurkan (boleh saat T-60 s; jangan saat reload polling).
        # "Warning: Activity not started, ... delivered to currently running top-most instance" bukan error.
        cmd = ["am", "start", *(["-W"] if wait else []), "-a", "android.intent.action.VIEW", "-d", url,
               "-p", package]
        out = self.shell(cmd)
        if re.search(r"^Error|unable to resolve|Exception", out, re.M | re.I):
            raise DriverError(f"am start gagal: {out.strip()[:200]}")

    def window_size(self) -> tuple[int, int]:
        if self._size is None:
            self._size = tuple(self._call("window_size", self.d.window_size))
        return self._size

    def swipe_refresh(self) -> None:
        w, h = self.window_size()
        # tarik pelan (bukan fling) dari 30% ke 75% tinggi layar, 0,3 s (= 60 langkah u2 @5 ms); koordinat bulat.
        # Lewat jsonrpc langsung: d.swipe()/d.press() u2 memakai batas 300 s, bukan rpc_timeout_s.
        self._call("swipe_refresh", lambda: self._rpc("swipe", int(w * 0.5), int(h * 0.30), int(w * 0.5),
                                                      int(h * 0.75), SWIPE_STEPS))

    def press_back(self) -> None:
        self._call("press back", lambda: self._rpc("pressKey", "back"))

    def webview_present(self) -> bool:
        return self.exists(Sel("className", "android.webkit.WebView"))

    def screenshot(self, path: Path) -> bool:
        self._call("screenshot", lambda: self.d.screenshot(str(path)))
        return Path(path).exists()

    def last_toast(self) -> str | None:
        return self._call("getLastToast", lambda: self._rpc("getLastToast")) or None

    def clear_toast(self) -> None:
        self._call("clearLastToast", lambda: self._rpc("clearLastToast"))

    def dump(self) -> str:
        return self._call("dump_hierarchy", self.d.dump_hierarchy)  # hanya kalibrasi/diagnosa/status akhir

    def shell(self, cmd: list[str]) -> str:
        res = self._call(f"shell {' '.join(cmd[:3])}", lambda: self.d.shell(cmd, timeout=30))
        return getattr(res, "output", res if isinstance(res, str) else "")

    def agent_alive(self) -> bool | None:
        """/ping hanya membuktikan transport; UiAutomation harus dibuktikan dengan RPC sungguhan."""
        check = getattr(self.d, "_check_alive", None)
        if check is None:
            return None
        try:
            if not check():
                return False
            self._rpc("deviceInfo")
            return True
        except Exception:  # noqa: BLE001
            return False

    def restart_agent(self) -> None:
        def restart() -> None:
            try:  # server yang bukan milik sesi ini tidak dimatikan stop_uiautomator: minta berhenti via HTTP
                from uiautomator2 import core

                core._http_request(self.d._dev, self.d._device_server_port, "GET", "/stop", timeout=3)
            except Exception:  # noqa: BLE001
                pass
            self.d.stop_uiautomator()
            self.d.start_uiautomator()

        self._call("restart agent", restart)


# --------------------------------------------------------------------------- latensi


@dataclass
class QuerySample:
    op: str
    target: str
    ms: float
    t_server_ms: int


@dataclass
class QueryStats:
    samples: list[QuerySample] = field(default_factory=list)

    def by_op(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for s in self.samples:
            out.setdefault(s.op, []).append(s.ms)
        return out

    def summary(self) -> list[str]:
        lines = []
        for op, ms in sorted(self.by_op().items()):
            ms_sorted = sorted(ms)
            p95 = ms_sorted[min(len(ms_sorted) - 1, int(round(0.95 * (len(ms_sorted) - 1))))]
            lines.append(f"{op}: n={len(ms)} median={statistics.median(ms):.1f} ms p95={p95:.1f} ms "
                         f"max={max(ms):.1f} ms")
        return lines

    def slow(self, limit_ms: float) -> list[QuerySample]:
        return [s for s in self.samples if s.ms > limit_ms]

    def write_csv(self, path: Path) -> None:
        with Path(path).open("w", encoding="utf-8") as f:
            f.write("t_server_ms,op,target,ms\n")
            for s in self.samples:
                target = s.target.replace('"', "'")
                f.write(f'{s.t_server_ms},{s.op},"{target}",{s.ms:.2f}\n')


class TimedDriver:
    """Bungkus driver: ukur latensi setiap query (ke memori; ditulis ke log di luar hot path)."""

    def __init__(self, inner: AndroidDriver, monotonic: Callable[[], float], server_ms: Callable[[], int]):
        self.inner = inner
        self.serial = inner.serial
        self.stats = QueryStats()
        self._mono = monotonic
        self._server_ms = server_ms
        self._lock = threading.Lock()  # satu query pada satu waktu (keepalive vs runner)

    def _timed(self, op: str, target: object, fn: Callable[[], Any]) -> Any:
        with self._lock:
            t0 = self._mono()
            try:
                return fn()
            finally:
                ms = (self._mono() - t0) * 1000
                self.stats.samples.append(QuerySample(op, str(target), ms, self._server_ms()))

    def exists(self, sel: Sel) -> bool:
        return self._timed("exists", sel, lambda: self.inner.exists(sel))

    def info(self, sel: Sel) -> Node | None:
        return self._timed("info", sel, lambda: self.inner.info(sel))

    def info_any(self, sel: Sel) -> Node | None:
        return self._timed("info_any", sel, lambda: self.inner.info_any(sel))

    def find_all(self, sel: Sel) -> list[Node]:
        return self._timed("find_all", sel, lambda: self.inner.find_all(sel))

    def click(self, target: Sel | Node) -> bool:
        label = target if isinstance(target, Sel) else f"node {target.label[:30]!r}"
        return self._timed("click", label, lambda: self.inner.click(target))

    def get_text(self, sel: Sel) -> str | None:
        return self._timed("get_text", sel, lambda: self.inner.get_text(sel))

    def current_app(self) -> AppInfo:
        return self._timed("current_app", "", self.inner.current_app)

    def start_url(self, url: str, package: str, wait: bool = True) -> None:
        return self._timed("start_url", url, lambda: self.inner.start_url(url, package, wait))

    def swipe_refresh(self) -> None:
        return self._timed("swipe_refresh", "", self.inner.swipe_refresh)

    def press_back(self) -> None:
        return self._timed("press_back", "", self.inner.press_back)

    def webview_present(self) -> bool:
        return self._timed("webview", "", self.inner.webview_present)

    def screenshot(self, path: Path) -> bool:
        return self._timed("screenshot", path, lambda: self.inner.screenshot(path))

    def last_toast(self) -> str | None:
        return self._timed("last_toast", "", self.inner.last_toast)

    def clear_toast(self) -> None:
        return self._timed("clear_toast", "", self.inner.clear_toast)

    def dump(self) -> str:
        return self._timed("dump", "", self.inner.dump)

    def shell(self, cmd: list[str]) -> str:
        return self._timed("shell", " ".join(cmd[:4]), lambda: self.inner.shell(cmd))

    def agent_alive(self) -> bool | None:
        return self._timed("agent_alive", "", self.inner.agent_alive)

    def restart_agent(self) -> None:
        return self._timed("restart_agent", "", self.inner.restart_agent)

    def window_size(self) -> tuple[int, int]:
        return self._timed("window_size", "", self.inner.window_size)


# --------------------------------------------------------------------------- fake (tes)


def node_matches(node: Node, sel: Sel) -> bool:
    """Semantik UiSelector (lihat docstring modul)."""
    by, v = sel.by, sel.value
    if by == "resourceId":
        return node.rid == v
    if by == "resourceIdMatches":
        return _jmatch(v, node.rid)
    if by == "className":
        return node.cls == v
    if by.startswith("text"):
        s = node.text
    else:
        s = node.desc
    kind = by.removeprefix("text").removeprefix("description")
    if kind == "":
        return s == v
    if kind == "Contains":
        return v in s
    if kind == "StartsWith":
        return s.startswith(v)
    if kind == "Matches":
        return _jmatch(v, s)
    raise ValueError(by)


def _jmatch(pattern: str, s: str) -> bool:
    """Pattern.matches Java: cocok seluruh teks; \\s/\\w/\\b hanya ASCII (NBSP BUKAN \\s di Java)."""
    return re.fullmatch(pattern, s, re.ASCII) is not None


class FakeApp(Protocol):
    """Model aplikasi untuk FakeDriver (state machine skenario, setara mock web)."""

    package: str

    def nodes(self) -> list[Node]: ...  # node semua jendela (jalur A: exists/info), urutan dokumen

    def active_nodes(self) -> list[Node]: ...  # node jendela aktif saja (jalur B: find_all)

    def activity(self) -> str: ...

    def on_tap(self, x: int, y: int) -> None: ...

    def on_intent(self, url: str, package: str) -> None: ...

    def on_refresh(self) -> None: ...

    def on_back(self) -> None: ...

    def webview(self) -> bool: ...

    # opsional: node jendela milik package lain (dialog crash/ANR, overlay aplikasi lain); hanya info_any
    # def system_nodes(self) -> list[Node]: ...


class FakeDriver:
    """Driver palsu: mencocokkan selector ke node FakeApp dan mencatat setiap aksi.

    Setiap query memajukan jam (`clock.sleep(latency_s)`) supaya latensi & jadwal teruji
    deterministik dengan FakeClock.
    """

    def __init__(self, app: FakeApp, clock: Any, latency_s: float = 0.02, serial: str = "FAKE123",
                 props: dict[str, str] | None = None, wm_size: str = "Physical size: 720x1612",
                 alive: bool = True, per_match_s: float | None = None):
        self.app = app
        self.clock = clock
        self.latency_s = latency_s
        # info_list di server = ~16 pencarian pohon per elemen cocok: find_all jauh lebih mahal dari info
        self.per_match_s = latency_s / 2 if per_match_s is None else per_match_s
        self.serial = serial
        self.props = props or {}
        self.wm_size = wm_size
        self.alive = alive
        self.calls: list[tuple[str, str]] = []
        self.restarts = 0
        self.fail_next: list[Exception] = []  # dilempar pada query berikutnya (simulasi agent mati)

    def _rpc(self, op: str, target: object = "", latency: float | None = None) -> None:
        self.calls.append((op, str(target)))
        self.clock.sleep(self.latency_s if latency is None else latency)
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _match(self, sel: Sel) -> list[Node]:
        return [n for n in self.app.nodes() if node_matches(n, sel)]

    def exists(self, sel: Sel) -> bool:
        self._rpc("exists", sel)
        return bool(self._match(sel))

    def info(self, sel: Sel) -> Node | None:
        self._rpc("info", sel)
        found = self._match(sel)
        return found[0] if found else None

    def info_any(self, sel: Sel) -> Node | None:
        """Tanpa batas package: node aplikasi + jendela sistem/aplikasi lain di atasnya (app.system_nodes)."""
        self._rpc("info_any", sel)
        system = getattr(self.app, "system_nodes", lambda: [])()
        found = [n for n in [*system, *self.app.nodes()] if node_matches(n, sel)]
        return found[0] if found else None

    def find_all(self, sel: Sel) -> list[Node]:
        active = getattr(self.app, "active_nodes", self.app.nodes)
        found = [n for n in active() if node_matches(n, sel)]
        self._rpc("find_all", sel, latency=self.latency_s + self.per_match_s * len(found))
        return found

    def click(self, target: Sel | Node) -> bool:
        node = self.info(target) if isinstance(target, Sel) else target
        if node is None:
            return False
        self._rpc("click", node.label)
        self.app.on_tap(*node.center)
        return True

    def get_text(self, sel: Sel) -> str | None:
        node = self.info(sel)
        return None if node is None else node.label

    def current_app(self) -> AppInfo:
        self._rpc("current_app")
        return AppInfo(self.app.package, self.app.activity())

    def start_url(self, url: str, package: str, wait: bool = True) -> None:
        self._rpc("start_url", url, latency=0.3 if wait else 0.05)
        self.app.on_intent(url, package)

    def swipe_refresh(self) -> None:
        self._rpc("swipe_refresh", latency=0.15)
        self.app.on_refresh()

    def press_back(self) -> None:
        self._rpc("press_back")
        self.app.on_back()

    def webview_present(self) -> bool:
        self._rpc("webview")
        return self.app.webview()

    def screenshot(self, path: Path) -> bool:
        self._rpc("screenshot", path, latency=0.2)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return True

    def last_toast(self) -> str | None:
        self._rpc("last_toast", latency=self.latency_s / 2)
        return getattr(self.app, "last_toast", None)

    def clear_toast(self) -> None:
        self._rpc("clear_toast", latency=self.latency_s / 2)
        if hasattr(self.app, "last_toast"):
            self.app.last_toast = None

    def dump(self) -> str:
        self._rpc("dump", latency=0.4)
        from xml.sax.saxutils import quoteattr

        rows = [f'  <node text={quoteattr(n.text)} content-desc={quoteattr(n.desc)} resource-id={quoteattr(n.rid)} '
                f'class={quoteattr(n.cls)} enabled="{str(n.enabled).lower()}" '
                f'clickable="{str(n.clickable).lower()}" selected="{str(n.selected).lower()}" '
                f'checked="{str(n.checked).lower()}" bounds="[{n.bounds[0]},{n.bounds[1]}]'
                f'[{n.bounds[2]},{n.bounds[3]}]"/>' for n in self.app.nodes()]
        return "<hierarchy>\n" + "\n".join(rows) + "\n</hierarchy>\n"

    def shell(self, cmd: list[str]) -> str:
        self._rpc("shell", " ".join(cmd))
        if cmd[:1] == ["getprop"]:
            if len(cmd) == 1:
                return "\n".join(f"[{k}]: [{v}]" for k, v in self.props.items())
            return self.props.get(cmd[1], "") + "\n"
        if cmd[:2] == ["wm", "size"]:
            return self.wm_size + "\n"
        if cmd[:2] == ["wm", "density"]:
            return "Physical density: 320\n"
        return self.app.shell(cmd) if hasattr(self.app, "shell") else ""

    def agent_alive(self) -> bool | None:
        self._rpc("agent_alive", latency=0.005)
        return self.alive

    def restart_agent(self) -> None:
        self._rpc("restart_agent", latency=2.0)
        self.restarts += 1
        self.alive = True

    def window_size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self.wm_size)
        return (int(m.group(1)), int(m.group(2))) if m else (720, 1600)
