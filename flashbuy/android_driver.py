"""Driver Android tipis di atas uiautomator2, plus FakeDriver untuk tes.

Hot path memakai query selector di device (1 RPC per query: exists / info / click koordinat),
BUKAN dump_hierarchy. `dump()` hanya untuk kalibrasi, diagnosa UNKNOWN, dan status akhir.

Semantik selector mengikuti UiSelector uiautomator:
  text / description          sama persis (case-sensitive)
  textContains / ...Contains  substring (case-sensitive)
  textStartsWith              awalan (case-sensitive)
  textMatches / ...Matches    regex Java, harus cocok SELURUH teks (Pattern.matches)
  resourceId / className      sama persis
"""

from __future__ import annotations

import re
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SEL_KINDS = ("resourceId", "text", "textContains", "textStartsWith", "textMatches",
             "description", "descriptionContains", "descriptionMatches", "className")


class DriverError(RuntimeError):
    """Kegagalan komunikasi dengan device / agent uiautomator2."""


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

    def find_all(self, sel: Sel) -> list[Node]: ...  # semua elemen yang cocok (1 RPC)

    def click(self, target: Sel | Node) -> bool: ...  # Node -> tap tengah bounds (1 RPC)

    def get_text(self, sel: Sel) -> str | None: ...

    def current_app(self) -> AppInfo: ...

    def start_url(self, url: str, package: str) -> None: ...  # intent VIEW ke package tertentu

    def swipe_refresh(self) -> None: ...  # tarik-untuk-muat-ulang

    def press_back(self) -> None: ...

    def webview_present(self) -> bool: ...

    def screenshot(self, path: Path) -> bool: ...

    def dump(self) -> str: ...

    def shell(self, cmd: list[str]) -> str: ...

    def agent_alive(self) -> bool | None: ...  # None = tidak bisa dicek

    def restart_agent(self) -> None: ...

    def window_size(self) -> tuple[int, int]: ...


# --------------------------------------------------------------------------- uiautomator2


class U2Driver:
    """Implementasi nyata di atas uiautomator2 3.x (tanpa atx-agent)."""

    def __init__(self, serial: str = "", device: Any = None):
        if device is None:
            import uiautomator2 as u2

            try:
                device = u2.connect(serial or None)
            except Exception as e:  # noqa: BLE001 - pesan apa pun dari adb/u2
                raise DriverError(f"gagal konek ke device {serial or '(pertama)'}: {e}") from e
        self.d = device
        self.serial = serial or getattr(device, "serial", "") or ""
        self._size: tuple[int, int] | None = None

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DriverError:
            raise
        except Exception as e:  # noqa: BLE001 - semua error u2/adb dibungkus
            if type(e).__name__ == "UiObjectNotFoundError":
                raise
            raise DriverError(f"{what}: {type(e).__name__}: {e}") from e

    def exists(self, sel: Sel) -> bool:
        return bool(self._call(f"exists {sel}", lambda: bool(self.d(**sel.kwargs()).exists)))

    def info(self, sel: Sel) -> Node | None:
        def get() -> Node | None:
            try:
                return Node.from_u2(self.d(**sel.kwargs()).info)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "UiObjectNotFoundError":
                    return None
                raise

        return self._call(f"info {sel}", get)

    def find_all(self, sel: Sel) -> list[Node]:
        def get() -> list[Node]:
            try:
                return [Node.from_u2(i) for i in self.d(**sel.kwargs()).info_list()]
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "UiObjectNotFoundError":
                    return []
                raise

        return self._call(f"find_all {sel}", get)

    def click(self, target: Sel | Node) -> bool:
        node = self.info(target) if isinstance(target, Sel) else target
        if node is None:
            return False
        x, y = node.center
        self._call(f"click {x},{y}", lambda: self.d.click(x, y))
        return True

    def get_text(self, sel: Sel) -> str | None:
        node = self.info(sel)
        return None if node is None else node.label

    def current_app(self) -> AppInfo:
        info = self._call("app_current", self.d.app_current)
        return AppInfo(info.get("package", ""), info.get("activity", ""))

    def start_url(self, url: str, package: str) -> None:
        cmd = ["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url, "-p", package]
        out = self.shell(cmd)
        if re.search(r"^Error|unable to resolve|Exception", out, re.M | re.I):
            raise DriverError(f"am start gagal: {out.strip()[:200]}")

    def window_size(self) -> tuple[int, int]:
        if self._size is None:
            self._size = tuple(self._call("window_size", self.d.window_size))
        return self._size

    def swipe_refresh(self) -> None:
        w, h = self.window_size()
        self._call("swipe_refresh", lambda: self.d.swipe(w * 0.5, h * 0.30, w * 0.5, h * 0.75, duration=0.12))

    def press_back(self) -> None:
        self._call("press back", lambda: self.d.press("back"))

    def webview_present(self) -> bool:
        return self.exists(Sel("className", "android.webkit.WebView"))

    def screenshot(self, path: Path) -> bool:
        self._call("screenshot", lambda: self.d.screenshot(str(path)))
        return Path(path).exists()

    def dump(self) -> str:
        return self._call("dump_hierarchy", self.d.dump_hierarchy)

    def shell(self, cmd: list[str]) -> str:
        res = self._call(f"shell {' '.join(cmd[:3])}", lambda: self.d.shell(cmd, timeout=30))
        return getattr(res, "output", res if isinstance(res, str) else "")

    def agent_alive(self) -> bool | None:
        check = getattr(self.d, "_check_alive", None)
        if check is None:
            return None
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            return False

    def restart_agent(self) -> None:
        def restart() -> None:
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

    def find_all(self, sel: Sel) -> list[Node]:
        return self._timed("find_all", sel, lambda: self.inner.find_all(sel))

    def click(self, target: Sel | Node) -> bool:
        label = target if isinstance(target, Sel) else f"node {target.label[:30]!r}"
        return self._timed("click", label, lambda: self.inner.click(target))

    def get_text(self, sel: Sel) -> str | None:
        return self._timed("get_text", sel, lambda: self.inner.get_text(sel))

    def current_app(self) -> AppInfo:
        return self._timed("current_app", "", self.inner.current_app)

    def start_url(self, url: str, package: str) -> None:
        return self._timed("start_url", url, lambda: self.inner.start_url(url, package))

    def swipe_refresh(self) -> None:
        return self._timed("swipe_refresh", "", self.inner.swipe_refresh)

    def press_back(self) -> None:
        return self._timed("press_back", "", self.inner.press_back)

    def webview_present(self) -> bool:
        return self._timed("webview", "", self.inner.webview_present)

    def screenshot(self, path: Path) -> bool:
        return self._timed("screenshot", path, lambda: self.inner.screenshot(path))

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
        return re.fullmatch(v, s) is not None
    raise ValueError(by)


class FakeApp(Protocol):
    """Model aplikasi untuk FakeDriver (state machine skenario, setara mock web)."""

    package: str

    def nodes(self) -> list[Node]: ...  # node layar saat ini, urutan dokumen

    def activity(self) -> str: ...

    def on_tap(self, x: int, y: int) -> None: ...

    def on_intent(self, url: str, package: str) -> None: ...

    def on_refresh(self) -> None: ...

    def on_back(self) -> None: ...

    def webview(self) -> bool: ...


class FakeDriver:
    """Driver palsu: mencocokkan selector ke node FakeApp dan mencatat setiap aksi.

    Setiap query memajukan jam (`clock.sleep(latency_s)`) supaya latensi & jadwal teruji
    deterministik dengan FakeClock.
    """

    def __init__(self, app: FakeApp, clock: Any, latency_s: float = 0.02, serial: str = "FAKE123",
                 props: dict[str, str] | None = None, wm_size: str = "Physical size: 720x1612",
                 alive: bool = True):
        self.app = app
        self.clock = clock
        self.latency_s = latency_s
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

    def find_all(self, sel: Sel) -> list[Node]:
        self._rpc("find_all", sel)
        return self._match(sel)

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

    def start_url(self, url: str, package: str) -> None:
        self._rpc("start_url", url, latency=0.3)
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
