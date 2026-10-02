# flashbuy

Alat Python untuk mencoba checkout **1 produk** flash sale Shopee secepat mungkin saat slot
dibuka, memakai **1 akun milik sendiri**, lewat UI seperti manusia. Ada dua jalur, Web
(Playwright) dan Aplikasi Android (adb + uiautomator2), yang berjalan paralel lewat orchestrator:
pesanan paling banyak **satu**, siapa pun jalur yang lebih cepat.

> Default **DRY-RUN**: berhenti sebelum "Buat Pesanan". Checkout sungguhan wajib `--live`.

## Status pengerjaan

| Tahap | Modul | Status |
|---|---|---|
| 1 | `timesync`, `config`, CLI `timesync` | ✅ |
| 2 | `web_runner` (dry-run & live), `guards`, `notifier`, `calibrate`/`login`/`precheck`/`run` web, mock Shopee | ✅ |
| 2.1 | `pricing` (pengaman harga 3 lapis), deteksi captcha non-dialog, `UNKNOWN_STATE`, reload T+0,5 s | ✅ |
| 3 | `android_runner` (dry-run & live) di atas device palsu, kalibrasi/precheck/run Android, koreksi live web | ✅ (perlu kalibrasi di HP asli) |
| 4 | `orchestrator` (paralel, lock pemenang, stop global, satu alarm), `doctor`, `setup.ps1`, lock antar-proses | ✅ |
| 4.1 | mode ketuk Android `selector` (cari+ketuk di HP, 1 RPC), rate limit per jalur, `rehearse` | ✅ (perlu dicoba di HP asli) |

## Batasan keras (tertanam di kode, bukan opsi)

- Android: **hanya interaksi UI lewat uiautomator2** (query elemen, klik selector / tap koordinat elemen, swipe, back,
  intent VIEW `am start`). Tidak ada modifikasi/patch/dekompilasi APK Shopee (jadx/apktool), Frida/hook,
  root, bypass captcha, atau API privat. Perintah shell adb yang dipakai hanya membaca status device
  (`getprop`, `wm`, `dumpsys`, `settings get`, `pm path`, `ps`, `cmd package resolve-activity`), membuka
  intent VIEW, dan `svc power stayon` / `settings put global stay_on_while_plugged_in` (layar tetap menyala
  selama run, dikembalikan setelahnya). Alat tidak pernah mengetik apa pun (tidak ada `input text`).

- 1 akun, login manual. Tidak ada multi-akun.
- Tidak ada captcha solver, bypass/evasion anti-bot, spoof fingerprint/device, stealth plugin,
  patch `navigator.webdriver`, atau pemanggilan API privat Shopee. Semua interaksi lewat UI.
- Captcha / verifikasi / OTP / slider / "aktivitas tidak biasa": **semua runner STOP**,
  alarm berbunyi, tidak ada retry otomatis. Halaman/layar captcha dibiarkan apa adanya untuk
  diselesaikan manual.
- Mode **live**: browser **tidak pernah ditutup otomatis**, apa pun hasilnya (termasuk error);
  Anda yang menutupnya. Aplikasi Android **tidak pernah** ditutup/di-force-stop alat. Dry-run boleh
  menutup browser (kecuali captcha/verifikasi/login).
- `--live` **wajib** `expected_name` (ditolak CLI & runner bila kosong).
- Setelah "Buat Pesanan" diklik tetapi layar PIN tidak muncul: `UNKNOWN_STATE` (semua runner
  stop, alarm) dengan pesan **"Pesanan MUNGKIN sudah terbuat — cek status pesanan manual"**.
- PIN ShopeePay tidak disimpan dan tidak diketik alat. Config menolak kunci tak dikenal (mis. `pin:`).
- Rate limit **polling & retry** (klik ulang Beli, reload, pilih ulang variasi): maks 1 aksi / 400 ms (dipakai
  425 ms untuk menyerap jitter) **per jalur** (web dan Android masing-masing punya `RateLimiter`), hanya di jendela
  **T-1 s s.d. T+8 s** (juga per jalur).
- Langkah maju (Checkout, pilih ShopeePay, Buat Pesanan) masing-masing dijalankan sekali, tanpa
  throttle, dengan batas total 30 s setelah klik Beli berhasil.
- Kuantitas 1: diset di halaman produk, dicek di keranjang dan checkout.
- **Pengaman harga fail-closed** (lihat di bawah): harga/total di atas batas atau tidak terbaca
  → `PRICE_GUARD`, tanpa request pesanan.

## Instalasi (Windows 11, Python 3.12)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1      # atau: pwsh -File .\setup.ps1 [-SkipDevice]
```

`setup.ps1` (PowerShell 5.1 & 7, tanpa hak admin, aman dijalankan ulang): cek Python 3.12, Chrome, dan `adb`
di PATH (pesan jelas bila tidak ada), buat `.venv` di folder proyek (dipakai ulang bila sudah ada),
`pip install -e .`, salin `target.example.yaml` → `target.yaml` bila belum ada, lalu
`python -m uiautomator2 init` untuk setiap HP yang terhubung & diizinkan. Tidak ada skrip luar yang diunduh
lalu dijalankan selain pip dan `uiautomator2 init`. Di akhir ditampilkan langkah berikutnya.

Manual (setara, plus dependensi dev untuk tes):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python -m pytest -q
```

Jalur web memakai **Google Chrome yang sudah terpasang** (`web.channel: "chrome"`), jadi
tidak perlu unduh browser. Kalau ingin memakai Chromium bawaan Playwright, set
`web.channel: null` lalu jalankan `python -m playwright install chromium`.

## Alur H-1 dan hari H

H-1 (sehari sebelumnya):

| # | Langkah | Perintah |
|---|---|---|
| 1 | setup laptop (sekali) | `.\setup.ps1` |
| 2 | login manual web (termasuk OTP) dan aplikasi | `python -m flashbuy login --platform web --config target.yaml` / `--platform android` |
| 3 | kalibrasi di produk biasa yang murah | `python -m flashbuy calibrate --platform web --config target.yaml --url <URL>` / `--platform android` |
| 4 | **rehearse di produk target** (harga masih normal; sampai checkout, tanpa "Buat Pesanan"); hapus sisa keranjang manual | `python -m flashbuy rehearse --config target.yaml` |
| 5 | dry-run kedua jalur (berhenti sebelum "Buat Pesanan") | `python -m flashbuy run --config target.yaml` |
| 6 | cek akhir + uji alarm | `python -m flashbuy doctor --config target.yaml --beep` |

Hari H:

| # | Langkah | Perintah |
|---|---|---|
| 1 | cek akhir (semua baris PASS; WARN dibaca dan diputuskan) | `python -m flashbuy doctor --config target.yaml` |
| 2 | run live, paling lambat T-70 s (idealnya sebelum T-10 mnt agar precheck terjadwal ikut jalan) | `python -m flashbuy run --config target.yaml --live` |
| 3 | alarm mendesak → periksa total di layar PIN, masukkan PIN **manual** | — |

`run`, `precheck`, dan `rehearse` tanpa `--only` menjalankan **semua jalur yang `enabled`** di config;
`--only web` / `--only android` tetap ada untuk satu jalur saja.

## Rehearsal (`flashbuy rehearse`)

Menguji jalur dari produk target **asli** sampai halaman checkout, beberapa hari sebelum event, saat harga masih
normal:

```powershell
python -m flashbuy rehearse --config target.yaml [--only web|android]
```

- Langsung jalan (tidak menunggu T; `start_time` boleh sudah lewat), memakai file lock yang sama. `--live` di CLI
  atau kunci `live`/`mode` di config → ditolak (exit 2).
- Alur sekali jalan per platform (berurutan): buka URL produk → pilih variasi → klik Beli sekali (lewat rate
  limit; lapis 1 harga **tidak** ditegakkan, harga tetap dibaca) → keranjang (web; Android bila aplikasi membuka
  keranjang) → checkout: pilih ShopeePay bila belum, baca harga/qty/nama/ongkir/total → cari tombol "Buat Pesanan"
  **tanpa** klik → STOP.
- Lapis 2 & 3 tetap **dievaluasi dan dilaporkan** ("akan lolos" / "akan gagal: …"); dengan harga normal lapis harga
  memang "akan gagal" — itu wajar dan tidak memengaruhi exit code.
- Pengaman berlapis: fungsi klik/ketuk "Buat Pesanan" kedua runner melempar error di mode rehearsal (juga di
  dry-run); `before_place_order()` diganti fungsi yang melempar, jadi tidak pernah dipanggil.
- CAPTCHA, VERIFICATION, UNKNOWN_STATE, layar PIN tak terduga → stop + alarm (platform berikutnya tidak
  dijalankan), sama seperti run biasa. Browser/layar dibiarkan untuk diselesaikan manual.
- Laporan per platform: tabel di terminal + `logs/<run_id>/<platform>-rehearsal.json`. Tiap langkah: nama,
  OK/GAGAL, kandidat selector yang cocok, latensi, nilai yang terbaca, path screenshot. Di akhir:
  **"keranjang berisi: …"** — hapus manual; alat tidak menghapusnya.

| Exit (`rehearse`) | Arti |
|---|---|
| 0 | semua langkah OK di semua platform |
| 1 | ada langkah GAGAL (nama langkahnya di tabel & JSON) / error |
| 2 | config/argumen salah, termasuk `--live` atau kunci live di config |
| 4 | CAPTCHA / VERIFICATION / LOGIN_REQUIRED |
| 5 | UNKNOWN_STATE (mis. layar PIN muncul setelah Beli: pesanan MUNGKIN terbuat — cek manual) |
| 6 | proses flashbuy lain memegang lock |

## Konfigurasi

Salin `target.example.yaml` ke `target.yaml` (di-gitignore) lalu sesuaikan. `start_time` wajib
memakai zona waktu (`+07:00`). `lead_ms` per jalur (0–1000): `web.lead_ms` (default 150) dan
`android.lead_ms` (default 300; saran nilainya dari `doctor`). `--lead-ms` meng-override semua jalur. Kunci
lama `lead_ms` di tingkat atas ditolak dengan pesan pindah.
`web.profile_dir` adalah profil Chrome **khusus flashbuy**, terpisah dari profil Chrome harian.

Batas harga wajib diisi (Rupiah bulat):

| Kunci | Arti |
|---|---|
| `max_item_price` | harga satuan maksimum yang boleh dibeli (harga flash), > 0 |
| `max_total` | "Total Pembayaran" maksimum termasuk ongkir & biaya layanan, ≥ `max_item_price` |
| `expected_name` | potongan nama produk (tidak peka huruf besar/kecil); opsional untuk dry-run, **wajib untuk `--live`** |

Bagian `android`: `serial` (kosong = device pertama), `package` (default `com.shopee.id`), dan
`reload` = `swipe` (tarik-untuk-muat-ulang, default) atau `intent` (buka ulang URL produk lewat
intent VIEW) untuk reload halaman produk saat polling. Pilih yang terbukti memuat ulang harga
saat dry-run di HP Anda.

## Pengaman harga (3 lapis, fail-closed)

Saat stok flash habis, atau saat klik terjadi sebelum slot dibuka, Shopee menampilkan harga
normal dengan tombol "Beli Sekarang" tetap aktif. Alat tidak boleh lanjut dengan harga itu.
Keputusan ada di `flashbuy/pricing.py`, modul bersama jalur web dan Android.
Nilai yang tidak terbaca dengan yakin dianggap **gagal**, bukan 0. Harga coret selalu diabaikan.

1. **Halaman produk** (di loop polling). Klik Beli hanya jika tombol aktif **dan** harga tampil
   (varian terpilih) ≤ `max_item_price`. Status tombol dan teks harga dibaca dalam satu
   `evaluate` yang sama.
   - Harga di atas batas (`price_not_yet`) atau tidak terbaca (`price_unreadable`), termasuk
     rentang "Rp89.000 - Rp129.000" sebelum variasi dipilih: tidak diklik, dicatat, polling
     lanjut.
   - Jendela T+8 s habis tanpa harga valid: `PRICE_GUARD`.
2. **Keranjang**. Item lain yang ikut tercentang di-uncheck sekali. Jika masih ada lebih dari 1
   item tercentang, atau target tidak bisa dipastikan: `PRICE_GUARD`.
3. **Checkout** (penentu akhir), tepat sebelum `before_place_order()` di dry-run maupun live.
   - Tunggu total stabil: ongkir sudah bernilai dan total sama pada 2 pembacaan berjarak 100 ms
     (batas 1,5 s).
   - Syarat lolos: tepat 1 baris produk, qty 1, nama cocok `expected_name` (jika diisi), harga
     satuan ≤ `max_item_price`, dan "Total Pembayaran" ≤ `max_total`.
   - Gagal: `PRICE_GUARD` tanpa retry. Semua nilai yang terbaca masuk pesan + log,
     screenshot diambil, alarm berbunyi.

Pembacaan memakai heuristik:
- harga produk: nominal non-coret dengan font terbesar;
- keranjang: baris di atas tiap checkbox;
- checkout: baris dengan penanda "x1", "(N Produk)", dan label "Total Pembayaran" / "Ongkos Kirim".

Semua heuristik bisa diganti CSS lewat `selectors.json`: `web.steps.product_price` (hasil
kalibrasi) dan `web.layout.{cart_row, checkout_row, checkout_total, checkout_shipping}` (diisi
manual). **Jalankan dry-run di produk biasa sebelum hari H.** Bila heuristik tidak cocok dengan
tampilan Shopee asli, hasilnya `PRICE_GUARD` (aman), lalu isi CSS di atas.

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

Urutan langkahnya: variasi (opsional), **harga produk** (opsional, hanya CSS), **Beli
Sekarang**, **Checkout** di keranjang, tombol pembuka daftar metode bayar (opsional),
**ShopeePay**, **Buat Pesanan**, lalu indikator stok habis (opsional). Selama kalibrasi, klik apa pun pada "Buat Pesanan" **diblokir**, termasuk
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
| T-lead | tunggu tombol Beli aktif **dan** harga ≤ `max_item_price` (MutationObserver, tanpa request), lalu klik |
| … | "Flash sale belum dimulai" **bukan** kegagalan: klik ulang tiap ≥ 425 ms sampai T+8 s |
| T+0,5 s | tombol belum aktif atau harga belum valid → reload, lalu tiap 2 s. Setelah reload variasi dipilih ulang. Reload terhitung aksi polling |
| sukses | keranjang (lapis 2) → Checkout → pastikan ShopeePay → cek harga (lapis 3) → **DRY-RUN berhenti** (screenshot) / LIVE klik "Buat Pesanan" → layar PIN → alarm |

Kalau run dimulai setelah T-2 menit, resync dilewati karena timesync awal masih segar. Run yang
dimulai setelah T-70 s ditolak (precheck & buka halaman tidak boleh jatuh di jendela polling).
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
- `PRICE_GUARD`
- `UNKNOWN_STATE`
- `TIMEOUT`
- `ABORTED`
- `ERROR`

Captcha/verifikasi terdeteksi dari:
- URL main frame, atau iframe terlihat berukuran ≥ 60 px, yang cocok pola `captcha`,
  `verify`, `traffic`. Pola bisa ditambah di `selectors.json` → `web.guards`.
- Event `framenavigated`/`frameattached`, jadi tidak hanya dicek setelah tiap langkah.
- Teks/dialog.

Jaring pengaman: halaman yang tidak dikenali selama lebih dari 1,5 s berturut-turut berujung
`UNKNOWN_STATE`. URL, judul, dan screenshot dicatat, alarm berbunyi, semua runner berhenti.

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
- `captcha_redirect`
- `captcha_iframe`
- `verification`
- `login_expired`
- `payment_not_shopeepay`
- `variant_required`
- `normal_price_before_open`
- `flash_sold_out_normal_price`
- `static_ui`
- `cart_other_checked`
- `unknown_page`

## Alur pemakaian Android (aplikasi Shopee, uiautomator2)

### 0. Siapkan HP (Tecno Spark / HiOS)

1. **Aktifkan Opsi Pengembang**: Setelan → Tentang ponsel → ketuk *Nomor build* 7×.
2. Di Opsi Pengembang aktifkan **USB debugging** dan **Tetap aktif** (layar tidak mati saat diisi
   daya). Bila ada, aktifkan juga *USB debugging (Setelan keamanan)* / *Nonaktifkan pemantauan izin*.
3. Hubungkan USB ke laptop, terima dialog sidik jari RSA di HP, lalu cek:
   ```powershell
   adb devices                      # serial harus berstatus "device"
   python -m uiautomator2 init      # pasang agent u2.jar (+ IME, tidak dipakai alat)
   python -m uiautomator2 doctor    # "uiautomator2 is OK"
   ```
4. **Checklist HiOS** (HiOS agresif mematikan proses latar):
   - [ ] **Optimasi baterai OFF** untuk aplikasi uiautomator2 (*ATX* / `com.github.uiautomator`) dan
         Shopee: Setelan → Baterai → Optimasi baterai / Manajemen aplikasi → "Tidak dibatasi".
   - [ ] **Auto-start ON** untuk ATX/uiautomator2 dan Shopee (Phone Master → Manajemen auto-start).
   - [ ] **Kunci di recent apps**: buka daftar aplikasi terbaru, tahan/ketuk ikon gembok di kartu ATX
         dan Shopee agar tidak ikut dibersihkan.
   - [ ] **Pembersihan otomatis Phone Master OFF** (pembersih memori/akselerasi terjadwal, "Bersihkan
         saat layar terkunci").
   - [ ] **Auto-update Shopee di Play Store OFF sampai 10.10** (Play Store → Shopee → ⋮ → matikan
         *Aktifkan pembaruan otomatis*): versi aplikasi yang berubah membuat hasil kalibrasi bisa tidak
         cocok; precheck memberi **PERINGATAN KERAS** + alarm bila versi berbeda dari saat kalibrasi.
   - [ ] Layar tidak dikunci selama run. *Tetap aktif* tidak wajib: saat `run` alat memasang
         `svc power stayon usb` dan mengembalikan nilai lamanya setelah selesai.

   Agent uiautomator2 yang mati atau lambat (3 query berturut-turut harus masing-masing < 1 s) dideteksi
   saat precheck dan dihidupkan ulang; saat arm dan tiap 2 s sampai T-3 s juga dicek. Saat polling, query
   yang gagal karena agent mati memicu restart (maks 2×). Semua restart dicatat di log.

### 1. Login manual di aplikasi

```powershell
python -m flashbuy login --platform android --config target.yaml
```

Aplikasi Shopee dibuka di HP. Login sendiri (termasuk OTP) dan pastikan ShopeePay aktif. Alat
tidak mengetik dan tidak menyimpan apa pun.

### 2. Kalibrasi (produk biasa yang murah)

```powershell
python -m flashbuy calibrate --platform android --config target.yaml
```

**Anda yang men-tap HP; alat hanya membaca layar** (dump hierarki + query baca). Selama kalibrasi alat
tidak pernah mengklik, menekan tombol, atau membuka intent, jadi "Buat Pesanan" tidak mungkin ditekan alat.

Tiap langkah:
1. Buka layar yang diminta di HP, tekan Enter di terminal.
2. Alat mengambil dump dan menampilkan **kandidat bernomor** yang cocok dengan kata kunci langkah
   (teks / content-desc / resourceId / bounds), elemen di jendela aktif lebih dulu, mis.
   `1. teks='Beli Sekarang' id=com.shopee.id:id/buy bounds=[360,1500][720,1612] klik`.
3. Ketik nomornya (Enter = 1; teks lain, termasuk teks pendek seperti variasi `S`, = cari elemen
   bertulisan itu; `u` = baca ulang layar; `lewati` untuk langkah opsional). Kata kunci variasi dicocokkan
   per kata dan tidak ke awalan resourceId `com.shopee.id:id/`.
4. Alat menyusun selector (`resourceId → text → textContains → description`) dan **hanya menyimpan
   yang unik**: tepat satu elemen cocok di dump (semua jendela) dan di device, dan itu elemen yang
   dipilih. "Buat Pesanan" diverifikasi dari dump saja.

Urutan: Beli Sekarang → harga (opsional, hanya resourceId/desc) → bottom sheet (penanda "Jumlah",
variasi, tombol konfirmasi) → checkout ("Buat Pesanan", JANGAN ditekan; baris "Metode Pembayaran") →
daftar metode (ShopeePay, Konfirmasi). Hasil disimpan ke bagian `android` di `selectors.json` bersama
**versi aplikasi Shopee dan resolusi layar** (`calibrated.app_version`, `calibrated.wm_size`); bagian
`web` tidak disentuh, versi lama di-backup. Versi/resolusi yang gagal dibaca (adb putus sesaat) disimpan
kosong dengan peringatan; hasil kalibrasi tetap tersimpan.

Default tanpa kalibrasi ada di `flashbuy/android_selectors.py` dan **hanya berisi teks yang terlihat**
(Bahasa Indonesia); resourceId hanya berasal dari kalibrasi di HP Anda. Termasuk
penanda status (`markers`: captcha, verifikasi, login, PIN, habis, belum mulai) yang bisa ditambah
lewat `selectors.json` → `android.markers` (regex, cocok seluruh teks satu elemen).

### 3. Pre-check

```powershell
python -m flashbuy precheck --config target.yaml --only android
```

- Membaca dan mencatat info device: `getprop` (merek, model, versi Android, SDK, build, versi
  HiOS) dan `wm size`/`wm density`. Tidak ada asumsi versi atau resolusi.
- Agent uiautomator2 hidup **dan responsif**: 3 query berturut-turut masing-masing < 1 s. Mati atau
  lambat → dihidupkan ulang sekali (peringatan); masih gagal → `ERROR`, run berhenti.
- Layar menyala dan tidak terkunci. Saat `run`: `svc power stayon usb` selama run, nilai
  `stay_on_while_plugged_in` lama dikembalikan setelah run selesai (apa pun hasilnya, juga saat Ctrl+C di
  tengah precheck). Gagal dikembalikan → alarm + petunjuk mengembalikan manual. Nilai semula tidak
  terbaca → `svc` tidak dikirim (peringatan), supaya pengaturan pengguna tidak tertimpa.
- Aplikasi Shopee terpasang, `versionName` dicatat. Berbeda dari versi saat kalibrasi, tidak terbaca, atau
  kalibrasi lama tanpa versi tercatat, sehingga tidak bisa dibandingkan → **PERINGATAN KERAS** + alarm (run
  tidak dihentikan; kalibrasi ulang + dry-run dulu).
- Halaman produk terbuka lewat intent tanpa diminta login, tombol Beli ditemukan, harga terbaca.
- Latensi query: 10× `exists` + 3× `info`; peringatan bila p95 > 100 ms. Semua sampel ada di
  `logs/<run>/android-queries-precheck.csv`.
- Alamat default ("Utama") dan saldo ShopeePay dibaca dari UI. Tidak terbaca / tidak ada / saldo
  kurang → **alarm saja** (PERINGATAN), run tidak dihentikan. Captcha/verifikasi di halaman itu (teks,
  content-desc, activity verifikasi, aplikasi asing di depan) → stop.

### 4. Dry-run lalu live

Run harus dimulai paling lambat **T-70 s** (precheck & pembukaan halaman tidak boleh jatuh di jendela
polling); lebih lambat ditolak.

```powershell
python -m flashbuy run --config target.yaml --only android            # DRY-RUN
python -m flashbuy run --config target.yaml --only android --live     # pesanan sungguhan
```

Jadwal sama dengan web: pre-check T-10 mnt, resync T-2 mnt, arm T-60 s, polling dari T-lead.

| Waktu | Aksi |
|---|---|
| T-60 s | buka produk lewat intent VIEW (`am start -a VIEW -d <url> -p com.shopee.id`), tunggu halaman produk, pilih variasi lebih awal bila opsinya tampil di halaman |
| T-lead | polling: tombol Beli aktif **dan** harga ≤ `max_item_price` (lapis 1) → klik, lewat RateLimiter (≥ 425 ms) di jendela T-1 s..T+8 s |
| T+0,5 s | belum siap (termasuk halaman galat "Gagal memuat" tanpa tombol Beli) → reload (`android.reload`: swipe-down atau buka ulang intent), lalu tiap 2 s (dihitung dari awal reload); aksi polling, tidak dikirim selama dialog ANR tampil. Variasi yang dipilih di halaman produk diperiksa lagi dan dipilih ulang lewat gate bila terlepas |
| setelah Beli | bottom sheet variasi/jumlah: pilih variasi, konfirmasi (sekali) → checkout, atau keranjang (lapis 2 hanya bila layar keranjang muncul) |
| checkout | pastikan ShopeePay (ganti lewat "Metode Pembayaran" bila perlu) → lapis 3 → DRY-RUN berhenti / LIVE klik "Buat Pesanan" → layar PIN → alarm |

Setiap iterasi **mengklasifikasi layar dulu, lalu bertindak**. Status: `PRODUCT_WAITING`,
`PRODUCT_ACTIVE`, `VARIANT_SHEET`, `CART`, `CHECKOUT`, `PIN_SCREEN`, `NOT_STARTED`, `SOLD_OUT`, `CAPTCHA`,
`VERIFICATION`, `LOGIN_REQUIRED`, `UNKNOWN` (+ sub-status sementara: pesan "pilih variasi"/error di
halaman produk, dan `LOADING`). Ditentukan dari elemen; bila tidak ada elemen yang dikenali, dari
package/activity di depan:
- **CAPTCHA/VERIFICATION** (stop semua runner, alarm, tanpa retry): teks verifikasi pendek di
  dialog/bottom sheet/toast (teks panjang = deskripsi produk diabaikan), juga di content-desc; WebView
  Shopee tanpa elemen yang dikenali; activity Shopee bernama captcha/verifikasi; aplikasi/activity asing
  selain `com.shopee.id` dan dialog sistem yang dikenal. Setelah klik, halaman Shopee yang tidak berubah
  atau spinner > 0,5 s, dan setiap reload, didahului cek package di depan (adb dumpsys; tidak dipakai
  sebelum klik pertama atau setelah reaksi toast Shopee): jendela asing berbentuk dialog → VERIFICATION,
  tanpa klik ulang/reload di atasnya.
- **UNKNOWN**: Shopee keluar dari foreground (launcher), crash, dialog ANR/crash di atas Shopee (dicari di
  luar package Shopee dan SystemUI, jadi judul produk/ulasan "tidak merespons" dan notifikasi bukan dialog;
  berkala tiap ≤ 0,5 s dan **tepat sebelum setiap tap**: Beli, reload, pilih/pilih ulang variasi, konfirmasi
  sheet, centang keranjang, Checkout keranjang, metode bayar, "Buat Pesanan"; selama dialog tampil tidak ada
  tap, juga saat menunggu layar PIN), telepon/keyboard/Phone Master di depan, atau layar tak dikenal.
  UNKNOWN > 1,5 s → `UNKNOWN_STATE`: dump + screenshot + activity disimpan, alarm, tanpa retry. Bacaan
  langkah maju yang mengenali layarnya mereset jaring ini (dialog yang sudah hilang tidak ikut dihitung).
  Indikator loading ditunggu, tetapi > 10 s (juga bila berselang-seling dengan layar kosong/tombol Beli)
  → `UNKNOWN_STATE`. Setelah "Buat Pesanan" pesannya "Pesanan MUNGKIN sudah terbuat — cek status pesanan
  manual"; layar PIN yang judulnya hanya content-desc tetap dikenali sebagai layar PIN.

Kecepatan:
- Hot path hanya memakai query satu-elemen di device (`info`/`exists`, satu pencarian pohon):
  per iterasi polling = tombol Beli + satu query bahaya (captcha/verifikasi/PIN/"Flash Sale telah
  berakhir") + harga. Cek yang jarang perlu (penanda bahaya di content-desc, bottom sheet yang sudah
  terbuka sebelum klik pertama) paling sering tiap 0,5 s, dan segera setelah setiap klik. Klasifikasi
  layar = rantai `exists`/`info` berprioritas dengan regex gabungan. Query penanda hanya mencocokkan
  teks pendek (≤ 120 karakter, di regex device) dan bukan "Habis" polos (lencana chip variasi lain;
  tombol "Habis" dicari terpisah), supaya elemen cocok pertama tidak menutupi penanda sesungguhnya. **Bukan** `dump_hierarchy`
  (dump hanya untuk kalibrasi, diagnosa, dan status akhir `android-<ts>-<status>.xml`), dan bukan
  `info_list`, yang di u2.jar melakukan ~16 pencarian pohon per elemen cocok. `find_all` hanya dipakai
  untuk bacaan sempit di langkah maju (sheet, checkout, keranjang) dan untuk menilai tombol "Habis" polos
  setelah `exists`-nya kena (tidak di iterasi polling).
- Toast Android (jendela terpisah, tidak ada di pohon node) dibaca lewat `getLastToast`.
- Latensi tiap query diukur dan ditulis setelah run ke log (ringkasan median/p95/maks) dan
  `android-queries-run.csv`. Target info/exists < 100 ms, find_all < 300 ms; lebih = peringatan.
- Koneksi u2: timeout per RPC 5 s, termasuk swipe reload & back (bawaan u2 300 s), TCP_NODELAY, dan restart
  agent implisit u2 dimatikan: agent yang dibunuh HiOS terlihat sebagai error, lalu di-restart
  eksplisit (tercatat, maks 2× saat polling). Query dibatasi ke package `com.shopee.id`.
- Tidak ada sleep tetap; semua penantian berbasis kondisi + timeout. Indikator loading
  (ProgressBar) setelah klik ditunggu sampai batas 10 s; layar tak dikenal tidak pernah memicu
  klik ulang/reload, hanya jaring `UNKNOWN_STATE` (1,5 s).

Keamanan klik:
- Klik ulang Beli, konfirmasi ulang di bottom sheet, dan reload = aksi polling (≥ 425 ms, jendela
  T-1..T+8 s, dicek lagi tepat sebelum tap). Jarak dihitung dari saat tap/gestur/intent **selesai**, jadi
  RPC lambat sebelum tap tidak memperpendeknya. Konfirmasi ulang yang ditahan dialog tidak dihitung klik.
- **Mode ketuk** `android.tap_mode`:
  - `selector` (default): SEMUA ketukan maju (Beli, chip variasi, konfirmasi sheet, centang keranjang, Checkout,
    metode bayar, "Buat Pesanan") memakai klik selector yang dijalankan di HP (jsonrpc `click(selector)`): elemen
    dicari dan diketuk dalam **satu RPC**, tanpa tunggu implisit (`waitForSelectorTimeout` agent di-nol-kan saat
    precheck, tanpa ketukan). Elemen tidak ada → **tidak** mengetuk, layar diklasifikasi ulang. Selector tombol
    Beli (dan langkah maju lain) wajib tidak mungkin cocok dengan "Buat Pesanan" (diuji terhadap teks & resource-id
    "Buat Pesanan"; className polos ditolak) — gagal → precheck GAGAL, run tidak dimulai.
  - `coord`: tap koordinat hasil bacaan (perilaku lama). Bila sempat menunggu slot / tap ulang, tombol dibaca
    ulang tepat sebelum diklik; sisa celah satu RPC baca (lihat Risiko).
  - Cek stop global/batal dan dialog ANR sebelum ketukan berlaku di kedua mode. `doctor` melaporkan estimasi
    latensi satu ketukan kedua mode dari query `exists`/`info` dengan selector Beli yang sama (tanpa ketukan).
- Toast lama dibersihkan (`clearLastToast`) **sebelum** klik Beli/konfirmasi/"Buat Pesanan", jadi toast
  reaksi klik itu sendiri tetap terbaca.
- Bottom sheet yang sudah terbuka sebelum klik Beli pertama ditutup dengan back (sekali; tidak tertutup →
  `ERROR`), tidak pernah dikonfirmasi tanpa pemilihan variasi.
- Variasi yang dipilih di halaman produk saat arm diperiksa lagi setelah setiap reload: chip belum tampil →
  ditunggu (harga belum dibaca, Beli belum diklik); terlepas, atau status terpilihnya tidak terbaca → dipilih
  ulang sekali lewat gate (aksi polling; chip dibaca ulang setelah slot, bottom sheet yang ikut terbuka
  ditutup) sebelum harga lapis 1 dibaca. Lapis 3 tetap memverifikasi variasi di checkout.
- Sebelum konfirmasi bottom sheet: cek captcha/verifikasi (content-desc selalu, teks bila sempat memilih
  variasi atau konfirmasi ulang setelah menunggu slot).
- Layar PIN yang muncul tanpa klik "Buat Pesanan" dari alat (saat polling, setelah Beli, atau di langkah
  keranjang/checkout/metode bayar) dianggap "pesanan mungkin terbuat" (`UNKNOWN_STATE` + pesan wajib +
  alarm + stop semua).
- Saat arm, dialog crash/ANR di atas chip variasi ditunggu hilang (≤ 2 s) sebelum chip di-tap; masih ada →
  dipilih saat polling lewat gate. Back untuk menutup sheet tidak dikirim selama dialog tampil.

Pembacaan harga di aplikasi (aksesibilitas Android tidak memberi tahu teks yang dicoret):
- harga produk (lapis 1): teks Rp pendek **pertama** yang terlihat (harga utama berada di atas harga
  coret); format aneh ("Rp99rb", rentang) = tidak terbaca → tidak diklik. Kalibrasi `product_price`
  (resourceId) bila heuristik salah memilih;
- keranjang (lapis 2): teks ditempelkan ke checkbox terdekat; dengan `expected_name`, satu-satunya item
  tercentang pun harus target; centang ditunggu terbarui (≤ 2 s) setelah uncheck;
- checkout (lapis 3): harga pada baris yang sama dengan penanda "x1" (harga coret sebaris yang lebih
  kecil diabaikan; seri → diambil yang terbesar); nama dicocokkan di kartu produk tanpa header toko;
  `variant` dari config harus terlihat di baris "Variasi" (bukan di nama produk; tanda baca/spasi
  bebas). Bacaan checkout sempit (hanya teks yang cocok pola); bila nama/variasi tidak terlihat, semua teks
  layar dibaca ulang SEKALI sebelum memutuskan. "Total Pembayaran"/ongkir dari selector
  hasil kalibrasi (resourceId, bila dikalibrasi) **dan** label baris (harus diawali label, jadi badge
  "Gratis Ongkir" diabaikan), semua harus sama; stabil ≥ 100 ms, atau ≥ 1 s setelah ganti metode
  bayar/uncheck keranjang (server menghitung ulang total & promo);
- metode bayar: dari radio yang tercentang atau baris yang diawali "Metode Pembayaran"; hanya
  "ShopeePay", "Saldo ShopeePay", "ShopeePay (Rp…)" yang diterima (bukan SPayLater/"saldo tidak cukup").
Bila tampilan asli berbeda, hasilnya `PRICE_GUARD` (aman).

Aplikasi tidak pernah ditutup alat. Captcha/verifikasi/PIN dibiarkan di layar untuk Anda.

## Orchestrator (web + Android paralel)

```powershell
python -m flashbuy run --config target.yaml            # DRY-RUN semua jalur enabled
python -m flashbuy run --config target.yaml --live     # pesanan sungguhan (maks 1)
```

- Satu `ServerClock` untuk semua jalur: timesync di awal, resync di T-2 mnt. Eksekusi asyncio; runner
  Android berjalan di thread executor.
- Precheck kedua jalur paralel (T-10 mnt). Satu gagal → lanjut dengan jalur yang lolos + alarm "jalan dengan
  satu platform". Keduanya gagal → batal (exit 3). Captcha/verifikasi saat precheck → stop semua.
- Arm (T-60 s) paralel; tiap jalur mulai polling di `T - <jalur>.lead_ms`.
- **Rate limit polling per jalur**: setiap jalur punya `RateLimiter` sendiri (1 aksi / 425 ms) dan jendela
  T-1 s..T+8 s sendiri; klik Beli pertama web tidak tertunda oleh klik Android.
- **Lock pemenang** (`threading.Lock`, hook `before_place_order()` tepat sebelum klik "Buat Pesanan"):
  pemanggil pertama mendapat izin, berikutnya ditolak → `ABORTED` tanpa klik. Lock **tidak pernah dilepas**
  dalam run itu, walaupun pemenang gagal setelahnya. Begitu diambil, jalur lain menerima event batal dan
  berhenti polling (tidak ada klik Beli/reload lagi).
- **Stop global**: `CAPTCHA`, `VERIFICATION`, atau `UNKNOWN_STATE` di satu jalur → semua jalur berhenti.
  Captcha yang tampil di browser saat jalur web sedang diam (menunggu arm / T-lead) langsung memicu stop lewat
  event halaman; jalur yang menunggu ikut bangun, jadi alarm tidak menunggu T. Layar PIN yang muncul tanpa klik
  "Buat Pesanan" dari alat (pesanan mungkin ada) juga langsung menghentikan jalur lain.
  Setiap runner memeriksanya sebelum **setiap** tap/klik/reload (web: tiap klik & navigasi; Android:
  `TimedDriver.before_action` sebelum klik, intent, swipe, back). Pengecualian: jalur yang **sudah**
  mengklik "Buat Pesanan" tetap menunggu layar PIN.
- `PRICE_GUARD`, `SOLD_OUT`, `NOT_STARTED_TIMEOUT` di satu jalur **tidak** menghentikan jalur lain.
- Akhir run: satu tabel ringkasan (jalur, status, waktu langkah kunci relatif T: Beli, Checkout, Lapis 3,
  Buat Pesanan, PIN; pemenang lock), status gabungan, dan **satu alarm**: pola mendesak (panjang, nada
  naik-turun) bila pesanan (mungkin) terbuat, pola pendek untuk hasil lain. Alarm runner ditahan; paling
  banyak satu alarm precheck tambahan.
- Mode live: browser dan aplikasi tetap terbuka apa pun hasilnya. Ctrl+C pertama saat menunggu Anda menutup
  browser **tidak** menutupnya (pesan muncul); Ctrl+C kedua = paksa keluar (browser ikut tertutup karena proses
  berakhir).
- **Lock antar-proses** `~/.flashbuy/run.lock` (`FLASHBUY_HOME` mengganti folder): hanya satu
  `run`/`precheck`/`doctor`/`login`/`calibrate` yang memegang browser & HP. Proses kedua langsung ditolak
  (exit 6) dengan PID, perintah, dan waktu mulai pemegang lock. Lock dilepas OS saat proses keluar (juga crash).

Status gabungan (prioritas dari atas):

| # | Status gabungan | Arti | Alarm |
|---|---|---|---|
| 1 | `ORDER_PLACED_AWAIT_PIN` | satu jalur sampai layar PIN: periksa total, masukkan PIN manual | mendesak |
| 2 | `UNKNOWN_STATE (setelah order)` | "Buat Pesanan" diklik (atau PIN muncul tanpa klik alat) tapi layar PIN tidak tercapai — termasuk captcha/verifikasi/login **setelah** klik: **pesanan MUNGKIN terbuat**, cek manual | mendesak |
| 3 | `CAPTCHA` / `VERIFICATION` | selesaikan manual, jangan diulang otomatis | pendek |
| 4 | `UNKNOWN_STATE` | layar tak dikenal sebelum order | pendek |
| 5 | `DRYRUN_OK` | dry-run sampai checkout & lolos pengaman harga | pendek |
| 6 | `LOGIN_REQUIRED` | login manual dulu | pendek |
| 7 | `PRICE_GUARD`, `SOLD_OUT`, `TIMEOUT`, `ERROR`, `NOT_STARTED_TIMEOUT`, `ABORTED` | tidak ada pesanan (urutan ini) | pendek |

Exit code (`run`; `precheck`/`doctor` memakai 0/1/2/6):

| Exit | Arti |
|---|---|
| 0 | `ORDER_PLACED_AWAIT_PIN` (live) / `DRYRUN_OK` (dry-run) |
| 1 | tidak ada pesanan: `PRICE_GUARD`, `SOLD_OUT`, `NOT_STARTED_TIMEOUT`, `TIMEOUT`, `ERROR`, `ABORTED` (`doctor`: ada FAIL) |
| 2 | config/argumen salah, start terlambat (< T-70 s), atau HP tidak terhubung — sebelum run dimulai |
| 3 | precheck gagal di semua jalur: run dibatalkan |
| 4 | `CAPTCHA` / `VERIFICATION` / `LOGIN_REQUIRED`: selesaikan manual |
| 5 | `UNKNOWN_STATE` (setelah order: pesanan mungkin sudah terbuat — cek manual) |
| 6 | proses flashbuy lain sedang memegang lock |
| 130 | dihentikan Ctrl+C |

## doctor (cek akhir H-1 / hari H)

```powershell
python -m flashbuy doctor --config target.yaml [--beep]
```

Tabel PASS / WARN / FAIL; exit 1 bila ada FAIL. Precheck di sini **tidak** menambah ke keranjang dan tidak
checkout (hanya membuka halaman alamat, saldo, produk, lalu membaca layar); alarm precheck tidak dibunyikan.

| Baris | PASS | WARN | FAIL |
|---|---|---|---|
| `config: expected_name`, `max_item_price`, `max_total` | terisi & valid | — | kosong / tidak valid |
| `config: start_time` | ber-zona waktu, ≥ 10 mnt lagi | < 10 mnt lagi | lewat, < T-70 s, atau tanpa zona waktu |
| `timesync` | ketidakpastian ≤ 100 ms | Shopee gagal (pakai NTP) / Shopee vs NTP beda > 100 ms | ketidakpastian > 100 ms / semua sumber gagal |
| `precheck web` / `precheck android` | semua cek OK | ada cek tak terbaca/peringatan | precheck gagal / HP tidak terhubung |
| `versi Shopee vs kalibrasi` | sama | belum dikalibrasi / versi tak tercatat atau tak terbaca | versi berubah |
| `umur selector web` / `android` | ≤ 3 hari | > 3 hari / belum dikalibrasi | — |
| `latensi query Android` | p95 ≤ 100 ms dan `android.lead_ms` ≥ saran | p95 > 100 ms / lead < saran / tak terukur | p95 > 500 ms |
| `mode ketuk Android` | `selector` + estimasi latensi kedua mode | `coord` (celah ketukan basi) / tak terukur | — |
| `ruang disk log` | ≥ 1 GB | < 1 GB | < 200 MB |
| `alarm (--beep)` | pola pendek lalu mendesak dibunyikan (+ webhook) | webhook gagal | — |

Saran `android.lead_ms` = 3 × p95 + 50 ms (satu iterasi polling ≈ 3 query), dibulatkan ke atas kelipatan 50,
batas 150–1000 ms.

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

`python -m pytest -q` menjalankan 1387 tes (tanpa xfail): unit, mock end-to-end web dengan Chromium
headless, kalibrasi dengan Alt+klik yang disimulasikan, jalur Android di atas device palsu
(`FakeDriver` + `tests/fake_android.py`, jam virtual) termasuk CLI, kalibrasi, pengaman harga, dan
skenario keselamatan (captcha, PIN, habis, agent mati, toast, sheet, dialog ANR, aplikasi asing), serta
orchestrator dengan **kedua jalur berjalan bersamaan** (mock web + Playwright dan FakeDriver di jam nyata):
lock pemenang (tepat 1 "Buat Pesanan"), stop global (0 tap setelah stop), pemenang yang sudah mengklik tetap
sampai PIN, PRICE_GUARD satu jalur, precheck satu/dua gagal, rate limit per jalur, lock antar-proses (proses
kedua ditolak), dan setiap baris `doctor` (PASS/WARN/FAIL, termasuk versi Shopee berubah & selector lama).
Jumlahnya besar karena parametrisasi: 586 fungsi tes, 217 di antaranya diparametrisasi (misalnya setiap
skenario dijalankan dry-run dan live, serta untuk kedua mode refresh). Setiap run Android juga memeriksa
invarian otomatis (0 klik "Buat Pesanan" saat dry-run, ≤ 1 pesanan saat live, polling di dalam jendela dan
berjarak ≥ 400 ms).
`FLASHBUY_HEADED=1` menjalankan browser headed (di Linux tanpa layar: `xvfb-run -a python -m pytest`).
Lint: `ruff check flashbuy tests`.

## Risiko

- **Melanggar ToS Shopee.** Otomasi pembelian dapat dianggap penyalahgunaan. Akun bisa
  dibatasi atau dibekukan, dan pesanan bisa dibatalkan sepihak. Gunakan dengan risiko sendiri.
- Tidak ada jaminan berhasil. Stok flash sale sangat sedikit dan keputusan akhir ada di server.
- **Harga:** pengaman 3 lapis mencegah checkout dengan harga normal, tetapi bergantung pada
  pembacaan UI. **Tetap periksa total di layar PIN sebelum memasukkan PIN.**
- Perubahan UI Shopee dapat mematahkan selector. Jalankan `calibrate` ulang dan dry-run
  sebelum hari H. Tombol nonaktif yang hanya ditandai lewat class ter-obfuscate mungkin tidak
  terdeteksi.
- Deteksi captcha/verifikasi berbasis teks/URL/selector. Teks lemah hanya dicari di
  dialog/toast agar deskripsi penjual tidak memicu stop palsu, sehingga captcha yang tampil
  tanpa dialog bisa terlewat. Karena itu tetap awasi layar.
- Android: captcha yang hanya punya content-desc (tanpa teks) dicek paling sering tiap 0,5 s demi
  kecepatan klik di T, jadi bisa ada satu klik Beli sebelum terdeteksi; dicek lagi segera setelah klik dan
  tepat sebelum konfirmasi bottom sheet.
- Checkout `--live` memotong saldo ShopeePay sungguhan setelah Anda memasukkan PIN.
- Android, **hanya `tap_mode: coord`**: tap memakai koordinat elemen yang dibaca sebelumnya. Bila klik Beli
  diterima server terlambat (tanpa reaksi 1,5 s) lalu checkout muncul tepat saat tap ulang, tap bisa mendarat di
  "Buat Pesanan" (posisinya sama). Sebelum setiap tap ulang (Beli & konfirmasi sheet) alat membaca ulang layar
  sebagai RPC terakhir: tombol harus sama di posisi yang sama dan "Buat Pesanan" tidak tampil. Sisa jendela = satu
  RPC baca (± 10–60 ms). Bila tetap terjadi, layar PIN terdeteksi → `UNKNOWN_STATE` "Pesanan MUNGKIN sudah
  terbuat", jalur lain langsung berhenti, alarm; PIN tidak pernah diketik alat, jadi pembayaran tidak terjadi
  tanpa Anda. Mode `selector` (default) tidak punya celah ini.
- Android `tap_mode: selector` memakai jsonrpc `click(selector)` & `setConfigurator` agent uiautomator2; baru
  diverifikasi di HP palsu. Jalankan `rehearse` di HP sungguhan dulu: langkah "klik Beli" membuktikan klik selector
  bekerja. Bila agent menolak, precheck GAGAL dengan saran `tap_mode: coord`.
