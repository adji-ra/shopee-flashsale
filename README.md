# flashbuy

Alat Python untuk mencoba checkout **1 produk** flash sale Shopee secepat mungkin saat slot
dibuka, memakai **1 akun milik sendiri**, lewat UI seperti manusia. Ada dua jalur, Web
(Playwright) dan Aplikasi Android (adb + uiautomator2), yang akan berjalan paralel.

> Default **DRY-RUN**: berhenti sebelum "Buat Pesanan". Checkout sungguhan wajib `--live`.

## Status pengerjaan

| Tahap | Modul | Status |
|---|---|---|
| 1 | `timesync`, `config`, CLI `timesync` | ✅ |
| 2 | `web_runner` (dry-run & live), `guards`, `notifier`, `calibrate`/`login`/`precheck`/`run` web, mock Shopee | ✅ |
| 3 | `android_runner`, kalibrasi Android | ⏳ |
| 4 | `orchestrator` (paralel + lock pemenang) | ⏳ |

## Batasan keras (tertanam di kode, bukan opsi)

- 1 akun, login manual. Tidak ada multi-akun.
- Tidak ada captcha solver, bypass/evasion anti-bot, spoof fingerprint/device, stealth plugin,
  patch `navigator.webdriver`, atau pemanggilan API privat Shopee. Semua interaksi lewat UI.
- Captcha / verifikasi / OTP / slider / "aktivitas tidak biasa": **semua runner STOP**,
  alarm berbunyi, tidak ada retry otomatis. Browser dibiarkan terbuka.
- PIN ShopeePay tidak disimpan dan tidak diketik alat. Config menolak kunci tak dikenal (mis. `pin:`).
- Rate limit **polling & retry** (klik ulang Beli, reload): maks 1 aksi / 400 ms (dipakai 425 ms
  untuk menyerap jitter), hanya di jendela **T-1 s s.d. T+8 s**.
- Langkah maju (Checkout, pilih ShopeePay, Buat Pesanan) masing-masing dijalankan sekali, tanpa
  throttle, dengan batas total 30 s setelah klik Beli berhasil.
- Kuantitas 1: dicek di halaman produk (diset 1) dan di keranjang (bukan 1 → berhenti).

## Instalasi (Windows 11, Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python -m pytest -q
```

Jalur web memakai **Google Chrome yang sudah terpasang** (`web.channel: "chrome"`), jadi
tidak perlu unduh browser. Kalau ingin memakai Chromium bawaan Playwright, set
`web.channel: null` lalu jalankan `python -m playwright install chromium`.

## Konfigurasi

Salin `target.example.yaml` ke `target.yaml` (di-gitignore) lalu sesuaikan. `start_time` wajib
memakai zona waktu (`+07:00`). `lead_ms` default 150 (0–1000), bisa di-override `--lead-ms`.
`web.profile_dir` adalah profil Chrome **khusus flashbuy**, terpisah dari profil Chrome harian.

## Alur pemakaian web

### 1. Login manual (sekali)

```powershell
python -m flashbuy login --platform web --config target.yaml
```

Browser terbuka di halaman login dengan profil `web.profile_dir`. Login seperti biasa
(termasuk OTP), lalu **tutup jendela browser**. Sesi tersimpan di profil. Jangan membuka profil
yang sama di Chrome lain secara bersamaan.

### 2. Kalibrasi selector (di produk biasa yang murah)

```powershell
python -m flashbuy calibrate --platform web --config target.yaml --url "https://shopee.co.id/<produk-murah>"
```

Kotak panduan muncul di pojok kanan atas. Untuk tiap langkah:

- **Alt+klik** berarti rekam elemen tanpa menjalankannya.
- **Klik biasa** berarti lanjut navigasi (pilih variasi, masuk keranjang, checkout).

Urutan langkahnya: variasi (opsional), **Beli Sekarang**, **Checkout** di keranjang, tombol
pembuka daftar metode bayar (opsional), **ShopeePay**, **Buat Pesanan**, lalu indikator stok
habis (opsional). Selama kalibrasi, klik apa pun pada "Buat Pesanan" **diblokir**, termasuk
Enter dan Space.

Selector yang terverifikasi unik disimpan ke `selectors.json`, dan versi lama di-backup ke
`selectors.json.bak-<waktu>`. Runner memakai hasil kalibrasi lebih dulu, lalu selector teks
Bahasa Indonesia bawaan sebagai cadangan. Tanpa `selectors.json`, runner hanya memakai
selector teks bawaan.

`selectors.json` juga menyimpan `urls` untuk pola cart/checkout dan halaman precheck. Halaman
saldo ShopeePay (`wallet_page`, default `/user/shopeepay`) **perlu dicek manual**; ubah bila
berbeda. Aturan guard tambahan bisa dimasukkan di `web.guards`.

### 3. Pre-check

```powershell
python -m flashbuy precheck --config target.yaml --only web
```

Mengecek sesi login, alamat default ("Utama"), saldo ShopeePay ≥ harga + ongkir, dan apakah
tombol Beli ditemukan. Tidak menambah barang ke keranjang. Saldo yang tidak terbaca hanya
menjadi peringatan.

### 4. Dry-run lalu live

```powershell
python -m flashbuy run --config target.yaml --only web            # DRY-RUN
python -m flashbuy run --config target.yaml --only web --live     # pesanan sungguhan
```

Jadwal (waktu server terkoreksi):

| Waktu | Aksi |
|---|---|
| mulai | timesync awal (tabel offset NTP & Shopee) |
| T-10 mnt | pre-check. Gagal → alarm. Sesi tidak login → berhenti |
| T-2 mnt | timesync ulang. Selisih > 50 ms → peringatan di log, offset baru dipakai |
| T-60 s | buka halaman produk, pilih variasi, set qty 1, scroll ke tombol |
| T-lead | tunggu tombol Beli aktif (MutationObserver, tanpa request), lalu klik |
| … | "Flash sale belum dimulai" **bukan** kegagalan: klik ulang tiap ≥ 425 ms sampai T+8 s. Tombol masih nonaktif di T+2 s → reload (juga terhitung aksi polling) |
| sukses | Checkout → pastikan ShopeePay → **DRY-RUN berhenti** (screenshot) / LIVE klik "Buat Pesanan" → layar PIN → alarm |

Kalau run dimulai setelah T-2 menit, resync dilewati karena timesync awal masih segar.
Browser dibiarkan terbuka untuk PIN, captcha, atau verifikasi. Selesaikan manual lalu tutup
browser.

Status akhir yang mungkin:
- `DRYRUN_OK`
- `ORDER_PLACED_AWAIT_PIN`
- `NOT_STARTED_TIMEOUT`
- `SOLD_OUT`
- `CAPTCHA`
- `VERIFICATION`
- `LOGIN_REQUIRED`
- `TIMEOUT`
- `ABORTED`
- `ERROR`

Jika "Buat Pesanan" sudah diklik tetapi layar PIN tidak muncul, alarm tetap berbunyi dengan
pesan "cek status pesanan manual".

Log tersimpan di `logs/<run_id>/`:
- `web.log`: timeline tiap langkah dengan timestamp ms waktu server.
- `web-result.json`
- screenshot status akhir (hot path tanpa screenshot/tracing).

Alarm memakai beep `winsound` berulang, dan POST JSON ke `notify_webhook` bila diisi (timeout
2 s, non-blocking, error diabaikan).

### Uji coba lokal dengan mock (tanpa Shopee)

```powershell
python -m tests.mock_shopee --port 8765 --open-in 90 --scenario normal
```

Buat `mock.yaml` dengan `product_url` hasil cetakan di atas dan `start_time` sesuai jam
"Buka", lalu jalankan:

```powershell
python -m flashbuy run --config mock.yaml --only web --allow-local
```

`--allow-local` adalah opsi tersembunyi khusus mock: mengizinkan URL `127.0.0.1`/`localhost`.
Skenario yang tersedia:
- `normal`
- `sold_out`
- `captcha`
- `verification`
- `login_expired`
- `payment_not_shopeepay`
- `variant_required`

## Sinkronisasi waktu

```powershell
python -m flashbuy timesync [--samples 5] [--ntp-host id.pool.ntp.org] [--url https://shopee.co.id/]
```

- **NTP** (`id.pool.ntp.org`): median offset RFC 5905.
- **Header HTTP `Date` shopee.co.id** menjadi acuan utama. Resolusinya hanya 1 detik, jadi
  sampel dijadwalkan dengan bisection pergantian detik (1 kasar + N bisection + 2 verifikasi)
  lewat satu koneksi keep-alive. Presisinya mendekati RTT. Di mock dengan offset +730 ms,
  terukur 730,26 ms ±0,84 ms (RTT 0,75 ms).
- Header `Date` berasal dari CDN, bukan dari server flash sale. Karena itu klik yang sedikit
  terlalu awal ditangani sebagai `NOT_STARTED` lalu dicoba ulang.

## Tes

`python -m pytest -q` menjalankan 137 tes: unit, mock end-to-end dengan Chromium headless,
dan kalibrasi dengan Alt+klik yang disimulasikan. `FLASHBUY_HEADED=1` menjalankan browser
headed (di Linux tanpa layar: `xvfb-run -a python -m pytest`). Lint: `ruff check flashbuy tests`.

## Risiko

- **Melanggar ToS Shopee.** Otomasi pembelian dapat dianggap penyalahgunaan. Akun bisa
  dibatasi atau dibekukan, dan pesanan bisa dibatalkan sepihak. Gunakan dengan risiko sendiri.
- Tidak ada jaminan berhasil. Stok flash sale sangat sedikit dan keputusan akhir ada di server.
- **Harga:** di Shopee asli tombol "Beli Sekarang" bisa sudah aktif sebelum flash sale dengan
  harga normal. Alat hanya mengklik mulai T-lead. **Periksa total di layar PIN sebelum
  memasukkan PIN.**
- Perubahan UI Shopee dapat mematahkan selector. Jalankan `calibrate` ulang dan dry-run
  sebelum hari H. Tombol nonaktif yang hanya ditandai lewat class ter-obfuscate mungkin tidak
  terdeteksi.
- Deteksi captcha/verifikasi berbasis teks/URL/selector. Teks lemah hanya dicari di
  dialog/toast agar deskripsi penjual tidak memicu stop palsu, sehingga captcha yang tampil
  tanpa dialog bisa terlewat. Karena itu tetap awasi layar.
- Checkout `--live` memotong saldo ShopeePay sungguhan setelah Anda memasukkan PIN.
