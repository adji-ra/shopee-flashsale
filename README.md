# flashbuy

Alat Python untuk mencoba checkout **1 produk** flash sale Shopee secepat mungkin saat slot
dibuka, memakai **1 akun milik sendiri**, lewat UI seperti manusia — jalur Web (Playwright)
dan Aplikasi Android (adb + uiautomator2) berjalan paralel.

> Default **DRY-RUN**: berhenti sebelum "Buat Pesanan". Checkout sungguhan wajib `--live`.

## Status pengerjaan

| Tahap | Modul | Status |
|---|---|---|
| 1 | `timesync`, `config`, CLI `timesync` | ✅ selesai + tes |
| 2 | `web_runner` (dry-run), `calibrate --platform web` | ⏳ |
| 3 | `android_runner` (dry-run), `calibrate --platform android` | ⏳ |
| 4 | `orchestrator` (paralel + lock pemenang) | ⏳ |
| 5 | `guards`, `notifier`, `precheck` | ⏳ |

## Batasan keras (tertanam di kode, bukan opsi)

- 1 akun, login manual. Tidak ada multi-akun.
- Tidak ada captcha solver, bypass/evasion anti-bot, spoof fingerprint/device, stealth plugin,
  atau pemanggilan API privat Shopee. Semua interaksi lewat UI.
- Captcha / verifikasi / OTP / slider / "aktivitas tidak biasa" → **semua runner STOP**,
  alarm berbunyi, tidak ada retry otomatis.
- PIN ShopeePay tidak disimpan dan tidak diketik alat. Config menolak kunci tak dikenal (mis. `pin:`).
- Refresh/polling maks 1 aksi per 400 ms, hanya di jendela T-1 s s.d. T+8 s.
- Kuantitas total 1 unit; lock pemenang mencegah double order antar jalur.

## Instalasi (Windows 11, Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python -m pytest -q
```

Setup Playwright, login profil manual, USB debugging, dan `python -m uiautomator2 init`
ditambahkan di tahap 2–3.

## Konfigurasi

Salin `target.example.yaml` → `target.yaml` (di-gitignore) lalu sesuaikan. `start_time` wajib
memakai zona waktu (`+07:00`). `lead_ms` default 150 (0–1000), bisa di-override `--lead-ms`.

## Sinkronisasi waktu

```powershell
python -m flashbuy timesync [--samples 5] [--ntp-host id.pool.ntp.org] [--url https://shopee.co.id/]
```

Mengukur offset `waktu_server - waktu_lokal` dari dua sumber dan melaporkan keduanya:

- **NTP** (`id.pool.ntp.org`): 5 sampel, median offset RFC 5905.
- **Header HTTP `Date` shopee.co.id** — **acuan utama**. Header ini hanya beresolusi 1 detik,
  jadi median biasa + koreksi RTT/2 masih bergalat hingga ±500 ms. Karena itu tiap sampel
  dijadwalkan agar pergantian detik server jatuh di tengah request (bisection), sehingga
  presisinya mendekati RTT (tipikal ±20–40 ms). Urutan request: 1 kasar + 5 bisection +
  2 verifikasi, lewat satu koneksi keep-alive (request `HEAD` ke halaman publik,
  User-Agent `flashbuy-timesync/0.1`).

Kolom `±` adalah ketidakpastian. Jika Shopee gagal, NTP dipakai sebagai cadangan (ada
peringatan); jika keduanya gagal, exit code 1. Selisih Shopee vs NTP di luar ketidakpastian
juga diperingatkan.

`ServerClock.wait_until(t, lead_ms)` menunggu sampai waktu server `t - lead_ms`: sleep kasar
(dipecah ≤ 1 s) lalu busy-wait ~15 ms terakhir (presisi ~1 ms). Jam server dijangkar ke
`perf_counter`, jadi tidak ikut melompat bila Windows menyetel ulang jam di tengah run.

## Risiko

- **Melanggar ToS Shopee.** Otomasi pembelian dapat dianggap penyalahgunaan; akun bisa
  dibatasi, dibekukan, atau pesanan dibatalkan sepihak. Gunakan dengan risiko sendiri.
- Tidak ada jaminan berhasil: stok flash sale sangat sedikit dan keputusan akhir ada di server.
- Perubahan UI Shopee dapat mematahkan selector → jalankan `calibrate` ulang sebelum hari H.
- Checkout `--live` memotong saldo ShopeePay sungguhan setelah Anda memasukkan PIN.
