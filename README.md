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
| 2.1 | `pricing` (pengaman harga 3 lapis), deteksi captcha non-dialog, `UNKNOWN_STATE`, reload T+0,5 s | ✅ |
| 3 | `android_runner` (dry-run & live) di atas device palsu, kalibrasi/precheck/run Android, koreksi live web | ✅ (perlu kalibrasi di HP asli) |
| 4 | `orchestrator` (paralel + lock pemenang) | ⏳ |

## Batasan keras (tertanam di kode, bukan opsi)

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
- Rate limit **polling & retry** (klik ulang Beli, reload): maks 1 aksi / 400 ms (dipakai 425 ms
  untuk menyerap jitter), hanya di jendela **T-1 s s.d. T+8 s**.
- Langkah maju (Checkout, pilih ShopeePay, Buat Pesanan) masing-masing dijalankan sekali, tanpa
  throttle, dengan batas total 30 s setelah klik Beli berhasil.
- Kuantitas 1: diset di halaman produk, dicek di keranjang dan checkout.
- **Pengaman harga fail-closed** (lihat di bawah): harga/total di atas batas atau tidak terbaca
  → `PRICE_GUARD`, tanpa request pesanan.

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
4. HiOS agresif mematikan proses latar. Matikan optimasi baterai/aktifkan *Auto-start* untuk
   Shopee, kunci aplikasi Shopee di daftar aplikasi terbaru, dan jangan biarkan layar terkunci.
   Agent uiautomator2 yang mati dideteksi saat precheck, saat arm, dan tiap 2 s sampai T-3 s,
   lalu dihidupkan ulang otomatis (dicatat di log). Saat polling, query yang gagal karena agent
   mati memicu restart (maks 2×).

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

**Anda yang men-tap HP; alat hanya membaca layar** (dump hierarki, khusus kalibrasi) sehingga
"Buat Pesanan" tidak mungkin ditekan alat. Tiap langkah: buka layar yang diminta, tekan Enter.
Alat mengenali elemen lewat teks default; bila tidak dikenali, ketik teks yang terlihat. Urutan:
Beli Sekarang → harga (opsional, hanya resourceId/desc) → bottom sheet (penanda "Jumlah",
variasi, tombol konfirmasi) → checkout ("Buat Pesanan", JANGAN ditekan; baris "Metode
Pembayaran") → daftar metode (ShopeePay, Konfirmasi). Kandidat disimpan terurut
`resourceId → text → textContains → description` setelah diverifikasi di device, ke bagian
`android` di `selectors.json` (bagian `web` tidak disentuh, versi lama di-backup).

Default tanpa kalibrasi (teks Bahasa Indonesia) ada di `flashbuy/android_selectors.py`, termasuk
penanda status (`markers`: captcha, verifikasi, login, PIN, habis, belum mulai) yang bisa ditambah
lewat `selectors.json` → `android.markers` (regex, cocok seluruh teks satu elemen).

### 3. Pre-check

```powershell
python -m flashbuy precheck --config target.yaml --only android
```

- Membaca dan mencatat info device: `getprop` (merek, model, versi Android, SDK, build, versi
  HiOS) dan `wm size`/`wm density`. Tidak ada asumsi versi atau resolusi.
- Agent uiautomator2 hidup (mati → dihidupkan ulang + peringatan).
- Layar menyala dan tidak terkunci; peringatan bila *Tetap aktif* mati.
- Aplikasi Shopee terpasang (versi dicatat).
- Halaman produk terbuka lewat intent tanpa diminta login, tombol Beli ditemukan, harga terbaca.
- Latensi query: 10× `exists` + 3× `info`; peringatan bila p95 > 100 ms. Semua sampel ada di
  `logs/<run>/android-queries-precheck.csv`.
- Alamat default ("Utama") dan saldo ShopeePay dibaca dari UI. Tidak terbaca = peringatan saja.

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
| T+0,5 s | belum siap → reload (`android.reload`), lalu tiap 2 s; terhitung aksi polling |
| setelah Beli | bottom sheet variasi/jumlah: pilih variasi, konfirmasi (sekali) → checkout, atau keranjang (lapis 2 hanya bila layar keranjang muncul) |
| checkout | pastikan ShopeePay (ganti lewat "Metode Pembayaran" bila perlu) → lapis 3 → DRY-RUN berhenti / LIVE klik "Buat Pesanan" → layar PIN → alarm |

Setiap iterasi **mengklasifikasi layar dulu, lalu bertindak** (produk, bottom sheet,
keranjang, checkout, PIN, habis, captcha/verifikasi, login, loading, aplikasi lain, tak dikenal).

Kecepatan:
- Hot path hanya memakai query satu-elemen di device (`info`/`exists`, satu pencarian pohon):
  per iterasi polling = tombol Beli + satu query bahaya (captcha/verifikasi/PIN) + harga. Klasifikasi
  layar = rantai `exists`/`info` berprioritas dengan regex gabungan. **Bukan** `dump_hierarchy`
  (dump hanya untuk kalibrasi, diagnosa, dan status akhir `android-<ts>-<status>.xml`), dan bukan
  `info_list`, yang di u2.jar melakukan ~16 pencarian pohon per elemen cocok. `find_all` hanya dipakai
  untuk bacaan sempit di langkah maju (sheet, checkout, keranjang).
- Toast Android (jendela terpisah, tidak ada di pohon node) dibaca lewat `getLastToast`.
- Latensi tiap query diukur dan ditulis setelah run ke log (ringkasan median/p95/maks) dan
  `android-queries-run.csv`. Target info/exists < 100 ms, find_all < 300 ms; lebih = peringatan.
- Koneksi u2: timeout per RPC 5 s (bawaan u2 tidak menerapkan timeout socket), TCP_NODELAY, dan restart
  agent implisit u2 dimatikan: agent yang dibunuh HiOS terlihat sebagai error, lalu di-restart
  eksplisit (tercatat, maks 2× saat polling). Query dibatasi ke package `com.shopee.id`.
- Tidak ada sleep tetap; semua penantian berbasis kondisi + timeout. Indikator loading
  (ProgressBar) setelah klik ditunggu sampai batas 30 s; layar tak dikenal tidak pernah memicu
  klik ulang/reload, hanya jaring `UNKNOWN_STATE` (1,5 s).

Keamanan klik:
- Klik ulang Beli, konfirmasi ulang di bottom sheet, dan reload = aksi polling (≥ 425 ms, jendela
  T-1..T+8 s). Reload dihitung dari saat gestur/intent **selesai**.
- Bila sempat menunggu slot, tombol Beli dibaca ulang tepat sebelum diklik (layar bisa sudah
  berganti ke checkout; koordinat lama tidak pernah dipakai).
- Layar PIN yang muncul tanpa klik "Buat Pesanan" dari alat dianggap "pesanan mungkin terbuat"
  (`UNKNOWN_STATE` + pesan wajib + alarm).

Pembacaan harga di aplikasi (aksesibilitas Android tidak memberi tahu teks yang dicoret):
- harga produk (lapis 1): teks Rp pendek **pertama** yang terlihat (harga utama berada di atas harga
  coret); format aneh ("Rp99rb", rentang) = tidak terbaca → tidak diklik. Kalibrasi `product_price`
  (resourceId) bila heuristik salah memilih;
- keranjang (lapis 2): teks ditempelkan ke checkbox terdekat; dengan `expected_name`, satu-satunya item
  tercentang pun harus target; centang ditunggu terbarui (≤ 2 s) setelah uncheck;
- checkout (lapis 3): harga pada baris yang sama dengan penanda "x1" (harga coret sebaris yang lebih
  kecil diabaikan; seri → diambil yang terbesar); nama dicocokkan hanya di kolom produk (bukan nama
  toko); `variant` dari config harus terlihat di baris produk; "Total Pembayaran"/ongkir dari testID
  (`labelTotalPayment`, `labelShippingFinalPrice`) **dan** label baris (harus diawali label, jadi badge
  "Gratis Ongkir" diabaikan), semua harus sama; stabil ≥ 100 ms, atau ≥ 1 s setelah ganti metode
  bayar/uncheck keranjang (server menghitung ulang total & promo);
- metode bayar: dari radio yang tercentang atau baris yang diawali "Metode Pembayaran"; hanya
  "ShopeePay", "Saldo ShopeePay", "ShopeePay (Rp…)" yang diterima (bukan SPayLater/"saldo tidak cukup").
Bila tampilan asli berbeda, hasilnya `PRICE_GUARD` (aman).

Aplikasi tidak pernah ditutup alat. Captcha/verifikasi/PIN dibiarkan di layar untuk Anda.

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

`python -m pytest -q` menjalankan 222 tes: unit, mock end-to-end dengan Chromium headless,
dan kalibrasi dengan Alt+klik yang disimulasikan. `FLASHBUY_HEADED=1` menjalankan browser
headed (di Linux tanpa layar: `xvfb-run -a python -m pytest`). Lint: `ruff check flashbuy tests`.

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
- Checkout `--live` memotong saldo ShopeePay sungguhan setelah Anda memasukkan PIN.
