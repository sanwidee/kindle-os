# Device: Kindle jailbroken

Referensi durable. Isinya hasil inspeksi langsung device yang di-mount lewat USB,
bukan ingatan atau tebakan. Format tiap bagian sama: **apa yang ada**, **apa yang
dibuka olehnya**, **batasan apa yang dia paksakan**.

Penanda:

- **[V]** = kelihatan langsung waktu inspeksi device, atau dibaca dari source code
- **[U]** = belum diverifikasi

Dokumen ini yang harus dibaca duluan sebelum `architecture.md`. Hampir semua
keputusan desain hub itu turunan dari batasan di halaman ini, dan bakal kelihatan
sewenang-wenang kalau dibaca terbalik.

---

## 1. Hasil audit device

Inspeksi dilakukan dengan device ter-mount sebagai USB mass storage. Read-only,
nggak ada yang diubah.

### Firmware dan identitas

| Item | Nilai |
|---|---|
| Firmware | 5.18.5.0.1 **[V]** |
| Build | 455681 **[V]** |
| Codename | juno / bellatrix **[V]** |

**[U] Model dan resolusi panelnya belum dikonfirmasi.** Codename-nya kebaca, tapi
pemetaan codename → model komersial belum dicek dari sumber yang bisa dipegang.
Ini penting karena `EINK_MAX_WIDTH` di converter gambar diturunkan dari lebar
panel; default 1072 px (lebar kelas Paperwhite) itu **asumsi**.

Cara nutup: KOReader → Help → About, di situ ada nama device dan ukuran layar.
Lakuin sebelum nyetel default converter.

### Jailbreak

| Item | Nilai |
|---|---|
| Metode | AdBreak 1.0.0 + hotfix **[V]** |
| Developer keys | terpasang **[V]** |
| `mntus` exec flag | aktif **[V]** |

**Yang dibuka:** exec flag di partisi userstore itu prasyarat buat semua yang lain
— tanpa itu binary di `/mnt/us` nggak bisa dijalanin, dan KUAL plus KOReader nggak
jalan. Developer keys bikin paket yang nggak ditandatangani Amazon bisa dipasang.

**Batasannya:** jailbreak itu state yang bisa hilang. Update firmware OTA berpotensi
nutup lubangnya dan/atau ngehapus hooks. Praktis: **matiin update otomatis dan
jangan colok ke jaringan yang maksa OTA** kalau nggak siap kehilangan setup ini.
`renameotabin` yang terpasang (lihat di bawah) memang buat itu.

---

## 2. Yang terpasang, dan gunanya

### KUAL extensions **[V]**

| Ekstensi | Fungsi | Relevansi ke project |
|---|---|---|
| `koreader` | Launcher KOReader | **Inti.** Semua jalur baca lewat sini |
| `kindlefetch` | Fetch/manage konten | Sekunder |
| `kterm` | Terminal di device | Debugging on-device tanpa SSH |
| `MRInstaller` | Installer paket | Cara masang/ganti komponen |
| `renameotabin` | Blokir update OTA | **Penting.** Ini yang jaga jailbreak nggak ketimpa update |

### KOReader

- Versi **v2025.10** **[V]**, 36 plugin terpasang **[V]**.
- Plugin yang relevan: `opds`, `SSH`, `terminal`, `texteditor`, `httpinspector`,
  `newsdownloader`, `wallabag`, `kosync`, `keepalive`, `exporter`, `rakuyomi`
  (v1.22.2, build `kindlehf`) **[V]**.

Versi ini yang mendefinisikan seluruh kanal OPDS di `architecture.md`. Lihat §3.

Plugin yang perlu dicatat efeknya:

- **`opds`** — klien katalog. Ini pintu Kindle ke hub.
- **`kosync`** — sinkronisasi posisi baca. Seam buat fase belakangan; bisa
  self-hosted.
- **`keepalive`** — nahan device tetap online. Berguna waktu download panjang,
  karena download OPDS single-file nge-freeze UI tanpa progress bar (§3).
- **`wallabag`** — sudah ada, dan sebagai bonus source code-nya adalah template
  kerja buat plugin yang pakai bearer token **[V]**. Kalau suatu hari kepaksa
  nulis `.koplugin` custom, mulai dari sini.
- **`SSH` + `terminal` + `httpinspector`** — jalur debug di device. Waktu feed OPDS
  keliatan aneh, ini yang dipakai buat lihat apa yang beneran diterima device.
- **`newsdownloader`** — jalur alternatif buat konten feed-based, bukan bagian
  desain Fase 1.

### Tooling lain **[V]**

- `kpm` (Kindle Package Manager)
- `libkh`

### rakuyomi

- v1.22.2, build `kindlehf` **[V]**.
- **Direktori `sources/` kosong — nol source terpasang** **[V]**.

`kindlehf` itu build yang benar buat firmware ini: dokumen fork bilang Kindle 4 ke
atas dengan firmware ≥ 5.16.3 harus pakai varian "hard floats", dan 5.18.5.0.1
lewat ambang itu **[V]**.

Arsitektur runtime-nya: plugin membundel backend HTTP Rust ARM 32-bit
statically-linked (~20 MB) bernama `server` di dalam `rakuyomi.koplugin`, yang
diluncurkan KOReader; frontend Lua ngomong ke situ **[V]**.

Detail soal kenapa `sources/` kosong, apa yang bisa dipasang, dan apa yang legal
ada di `docs/manga.md`. Ringkas dua kalimat: repo asal rakuyomi **sudah diarsipkan
sejak Januari 2026** dan v1.22.2 punya issue terbuka yang persis cocok sama kombo
"v1.22.2 kindlehf + KOReader 2025.10 gagal start" **[V]**; fork yang dirawat ada
dan masih ngirim build `kindlehf` **[V]**. **[U]** apakah fork-nya beneran
memperbaiki crash itu.

**Langkah diagnosis pertama, sebelum ngapa-ngapain:** baca `koreader/crash.log` di
device. Itu yang mbedain "backend crash waktu start" dari "nggak pernah ada source
yang dipasang". Dua-duanya konsisten sama `sources/` kosong.

---

## 3. Batasan yang dipaksakan KOReader v2025.10

Ini bagian paling penting di dokumen ini. Semuanya **[V]** dari pembacaan source
tag v2025.10 (plus commit luasocket yang di-pin dan LuaSec v1.3.2), kecuali yang
ditandai lain. **[U] Nggak ada yang dites runtime di device.**

### Auth: cuma HTTP Basic

Dialog "Add catalog" cuma punya empat field teks — nama, URL, username, password —
plus dua checkbox ("Use server filenames", "Sync catalog"). Nggak ada field header,
nggak ada pemilih tipe auth, nggak ada token.

LuaSocket ngubah `user`/`password` jadi header `Authorization: Basic` **preemptive
di request pertama** — nggak nunggu challenge 401. Jadi server yang 401 tanpa
`WWW-Authenticate` tetap kelayan.

**Yang dipaksakan ke hub:** pintu Kindle **wajib** HTTP Basic. Bukan pilihan.

### Nggak ada cookie, nggak bisa form login

Nggak ada cookie jar di seluruh stack. Nggak ada form POST. Nggak ada persistensi
session.

**Failure mode terburuk, dan ini yang wajib diingat:** kalau `/opds` yang
unauthenticated di-redirect ke halaman login HTML, KOReader ngikutin redirect,
dapet 200 + `text/html`, nyuapin HTML itu ke parser Atom, dapet nol entry, terus
**nggak nampilin error apa pun**. Cuma katalog kosong. Kelihatan kayak server rusak.

**Yang dipaksakan:** `/opds` unauthenticated balikin **401 polos**. Selamanya.

KOReader sendiri sebenernya punya pesan yang bener kalau dikasih status yang bener:
401 → "Authentication required for catalog. Please add a username and password.";
403 → "Failed to authenticate. Please check your username and password."

### Kredensial hilang waktu redirect

LuaSocket ngikutin 301/302/303/307 (maks 5 hop, GET/HEAD) tapi **nggak nyalin
`user`/`password`** ke request hasil redirect.

**Yang dipaksakan:** URL feed dan URL akuisisi **same origin, zero 30x**. Redirect
ke path ber-auth lain = 401 senyap.

### TLS: nggak ada validasi sertifikat sama sekali

LuaSec dipakai dengan default modulnya, `verify = "none"`. Variabel `cfg`-nya
file-local jadi nggak bisa di-override dari Lua, dan nggak ada kode KOReader yang
ngirim parameter TLS per-request. Nggak ada CA bundle yang dikirim bareng KOReader.
Jalur HTTP satunya (turbo) juga eksplisit `verify_ca = false`.

Dua konsekuensi, dan dua-duanya harus dipegang bareng:

1. **Umur firmware Kindle nggak relevan** buat Let's Encrypt. Nggak ada masalah CA
   bundle basi karena nggak ada CA bundle-nya. KOReader membundel TLS-nya sendiri
   (LibreSSL 4.2.0 menurut resep build).
2. **TLS di sini = enkripsi tanpa autentikasi server.** Penyerang aktif di jalur
   bisa MITM device dan nyolong kredensial Basic-nya. **Nggak bisa dibetulin dari
   device.** Mitigasinya cuma satu: bikin kredensialnya murah dan gampang dicabut.

Downgrade HTTPS→HTTP lewat redirect diblokir di dua lapis, dan KOReader nampilin
dialog peringatan eksplisit kalau kejadian. Itu satu-satunya bagian TLS yang dia
tegas.

**[U]** Apakah binary Kindle yang dikirim beneran nge-link LibreSSL yang dibundel
(buktinya resep build, bukan artefak yang dites) dan versi TLS mana yang akhirnya
bisa dinegosiasi. Praktisnya: **biarkan TLS 1.2 tetap aktif di server**, jangan
1.3-only. Kalau handshake gagal di device, itu hal pertama yang dites.

### Kredensial disimpan plaintext di device

KOReader nyimpen username/password OPDS **plaintext** di settings file-nya, di
device yang kalau dicolok mount sebagai USB mass storage **tanpa enkripsi**.

Siapa pun yang minjem, nyuri, atau nyolok Kindle bisa baca kredensial itu. Backup
folder settings juga bawa isinya.

**Nggak bisa diperbaiki di device.** Yang bisa cuma membatasi akibatnya:

1. Yang diketik di Kindle **harus** token device, bukan password manusia.
2. Kehilangan kendali fisik = anggap token kompromi, rotasi.
3. Token itu read-only dan single-purpose.
4. Log akses per-token di server biar pemakaian aneh kelihatan.

### Parser: Atom XML doang, dan rapuh

Nol dukungan OPDS 2.0 / JSON. Tipe link katalog yang dicocokkan di-hardcode ke
`application/atom+xml`. (OPDS 2.0 baru masuk KOReader di v2026.07 — sembilan bulan
setelah build yang terpasang di device ini.)

Parser-nya luxl, lexer XML streaming, dan sebelum di-handoff XML-nya **di-mangle
pakai regex** dulu: stylesheet dibuang, komentar dibuang, tag self-closing
di-rewrite, CDATA di-un-CDATA, dan seluruh body `<content>` di-escape supaya luxl
nggak keselek XHTML di dalamnya.

**Yang dipaksakan ke feed:** nggak ada komentar XML, nggak ada CDATA, nggak ada
`<?xml-stylesheet?>`, `<summary type="text">` dan bukan XHTML di `<content>`.

### Ekstensi file dicek sebelum mimetype

Link akuisisi cuma ditawarin buat di-download kalau **ekstensi file-nya ATAU
mimetype-nya** kepetakan ke provider terdaftar. Yang nggak cocok dua-duanya
**di-drop diam-diam** dari dialog download.

**Yang dipaksakan:** href harus berakhiran ekstensi asli (`.epub`, `.cbz`) **dan**
punya mimetype yang dikenal. Query string ngerusak cek ekstensi — jadi jangan
`?token=` di URL download.

Format yang terdaftar mencakup EPUB, PDF, CBZ, CBR, CBT, MOBI, AZW, FB2, HTMLZ,
DOCX, TXT, termasuk mimetype alternatif khusus OPDS (`application/x-cbz`,
`application/epub`).

### Download nge-freeze UI

Download per-buku (tap normal) jalan **sinkron di UI thread**, cuma dikasih toast
"Downloading…" satu detik, tanpa progress bar. Reader-nya beku selama transfer.
(Jalur "Download all" / sync memang fork subprocess yang bisa dibatalin; tap normal
nggak.)

Ada `FILE_BLOCK_TIMEOUT` 15 detik dan `FILE_TOTAL_TIMEOUT` 60 detik, tapi **[U]**
yang 60 detik kemungkinan ompong karena `downloadFile` pakai `ltn12.sink.file`
biasa, bukan sink KOReader sendiri yang satu-satunya beneran menegakkan total
timeout. Ini dibaca dari source plus komentar KOReader sendiri, belum dites.

Nggak ada batas ukuran hardcoded dan nggak ada buffering seluruh file — body
di-stream langsung ke disk.

**Yang dipaksakan:** bikin file **seukuran bab/artikel, bukan volume**. Selalu
kirim `Content-Length`. Jangan stall >15 detik di tengah stream.

### Cache feed: `HEAD` + `Last-Modified`

KOReader **nggak polling di background**. Update muncul waktu katalognya dibuka.
Tiap load feed nembak `HEAD` dulu, terus nge-cache di memori (20 slot) dengan key
`"opds|catalog|<url>|<last-modified>"`.

**Yang dipaksakan:** setiap route feed wajib jawab `HEAD` dengan `Last-Modified`
yang akurat. Ini detail server-side paling berdaya ungkit di seluruh kanal OPDS.

**[U]** Kalau `Last-Modified` nggak ada, key-nya jadi konstan per URL, yang dari
konstruksi hash-nya kelihatan bakal ngunci rak ke kondisi pertama selama sesi
KOReader hidup. Inferensi, belum dieksekusi — jadikan negative test.

Cache-nya in-memory, jadi restart KOReader ngebersihin. Rak basi yang bikin bingung
selalu tinggal satu restart.

### Yang justru didukung dengan baik

Nggak semuanya jebakan. Yang aman dipakai:

- **Pagination `rel="next"`** — jalan, dan KOReader **agresif prefetch** halaman
  berikutnya sampai layar penuh. Jadi ukuran halaman dibikin sedang (25 entry);
  halaman kegedean cuma buang round trip yang nge-block.
- **OpenSearch** — descriptor `application/opensearchdescription+xml` di-fetch dan
  di-parse, `Url` yang tipenya `application/atom+xml` dipilih, `{searchTerms}`
  jadi slot substitusi. Ada juga fallback gaya Calibre.
- **CBZ/CBR native** — v2025.08 nambah dukungan CBR plus OPDS browser syncing dan
  facets, jadi build yang ada di device ini udah lumayan lengkap buat manga.
- **API plugin** — `main.lua` + `_meta.lua`, `WidgetContainer:extend`,
  `registerToMainMenu`, `socket.http`. Murah kalau memang perlu. Tapi tiap plugin
  custom itu kode device yang harus di-ship dan di-update selamanya — persis yang
  mau dihindari dengan pakai OPDS.

---

## 4. Ringkasan: apa yang dipaksakan device ini ke hub

Tabel kompak. Kolom kanan itu yang harus dipatuhi implementasi.

| Batasan device | Konsekuensi di server |
|---|---|
| Cuma HTTP Basic | Pintu Kindle Basic; nggak ada opsi lain |
| Nggak ada cookie / form | `/opds` unauthenticated = 401 polos, **jangan** redirect ke login |
| Kredensial hilang saat redirect | Feed + download same-origin, zero 30x |
| Sertifikat nggak divalidasi | Kredensial Kindle = token device revocable, bukan password manusia; TLS 1.2 tetap aktif |
| Kredensial plaintext di device | Rotasi kalau kendali fisik hilang; token read-only single-purpose |
| Atom XML doang, parser rapuh | OPDS 1.2; nggak ada komentar/CDATA/stylesheet; `<summary type="text">` |
| Ekstensi dicek sebelum mimetype | href berakhiran `.epub`; mimetype benar; tanpa query string |
| Download nge-freeze UI | File seukuran bab; `Content-Length` selalu ada; jangan stall >15 detik |
| Cache pakai `HEAD` + `Last-Modified` | Semua route feed jawab `HEAD` dengan `Last-Modified` akurat |
| Prefetch `rel="next"` agresif | Halaman sedang (25 entry) |
| Panel grayscale 16 level | Gambar grayscale + kuantisasi; **tanpa** highlighting sintaks; **tanpa** background fill; teks hitam murni |
| Kolom monospace sempit (~40-55 char) | Reflow code saat build ke 64 kolom, hanging indent |
| Font device itu preferensi user | Jangan embed font; ukuran pakai `em`/`%`; jangan override justifikasi/hyphenation |
| Font device nggak punya emoji | Strip emoji (jadinya tofu) |

---

## 5. Yang harus dicek di device sebelum ngoding jauh

Urut nilai per menit yang dikeluarin.

1. **KOReader → Help → About**: konfirmasi model, resolusi layar, dan versi
   KOReader. Ini nutup `EINK_MAX_WIDTH` **[U]** dan mastiin asumsi v2025.10.
2. **Sideload satu EPUB hasil generate lewat USB dan baca beneran.** Ini cek paling
   berharga di seluruh rencana — dia memvalidasi seluruh lapisan tipografi selagi
   biaya salahnya masih sebatas edit stylesheet. Yang dilihat: code block kebaca,
   nggak ada tofu, background nggak dither jadi kotor, tabel kebaca.
3. **Cek glyph `☐`/`☑`** di fixture task list **[U]**. Kalau tofu, fallback ke
   `[ ]`/`[x]`.
4. **Baca `koreader/crash.log`** buat mendiagnosis rakuyomi sebelum nyentuh apa pun
   di sana.
5. Setelah hub hidup: tambah katalog dengan kredensial Basic, cek rak muncul, tap
   satu buku, buka. Lalu tambah dokumen kedua dan buka ulang katalog **tanpa**
   restart KOReader — itu tes sesungguhnya buat desain `Last-Modified`.
6. Tes checkbox "Sync catalog" dan catat apa yang beneran dia lakuin **[U]**.
7. Download EPUB besar lewat wifi lambat: lihat apakah timeout 15 detik nyala dan
   gimana UI beku itu kelihatan **[U]**. Ini yang ngalibrasi default ukuran file.

---

## 6. Catatan pemeliharaan

- **Jangan update firmware** tanpa niat sadar. `renameotabin` terpasang buat
  ngeblok OTA; itu yang jaga seluruh setup ini hidup.
- **Update KOReader** ngubah semua yang ada di §3. Kalau naik ke v2026.07+, dukungan
  OPDS 2.0 JSON masuk — nggak ngerusak desain Atom yang ada, tapi verifikasi di §3
  jadi kedaluwarsa dan harus diperiksa ulang.
- **Kehilangan kendali fisik atas device = token OPDS-nya kompromi.** Rotasi di
  server, ketik ulang yang baru. Satu baris di sisi server.
- Semua di dokumen ini hasil inspeksi satu kali. Kalau device diutak-atik lagi
  (plugin ditambah, rakuyomi diganti fork), **update file ini**, jangan andelin
  ingatan.
