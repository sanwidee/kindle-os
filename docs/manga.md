# Manga Playbook — kindle-os

Tiga jalur baca manga di Kindle, dipakai barengan sesuai situasi:

1. **rakuyomi** — plugin KOReader, baca langsung di Kindle, sekarang rusak.
2. **Server-side stack** — Suwayomi di VPS, Kindle narik lewat OPDS.
3. **MANGA Plus (Shueisha)** — resmi, gratis, legal.

Semua klaim di bawah ditandai statusnya. Yang belum gue verifikasi ditulis **UNVERIFIED** eksplisit — jangan diperlakukan sebagai fakta.

Baseline device (dari brief, **UNVERIFIED** karena Kindle-nya nggak ke-mount waktu dokumen ini ditulis — `/Volumes/` cuma berisi `Macintosh HD`, `San Drive 1`, `Sanwidi 2TB`):

- Kindle FW 5.18.5.0.1
- KOReader 2025.10 "Ghost"
- rakuyomi v1.22.2, build `kindlehf`
- Model Kindle spesifik: **UNVERIFIED** — belum dicek, dan ini penting buat profil KCC (lihat bagian preprocessing).

---

## 1. Diagnosis rakuyomi

### 1.1 Yang SUDAH pasti (verified)

**URL source list lu nggak mati.** Ini yang paling penting, karena tebakan default orang selalu "URL-nya dead".

```
https://raw.githubusercontent.com/Skittyblock/aidoku-community-sources/refs/heads/gh-pages/index.min.json
```

`curl -L` → HTTP 200, 10.831 byte, JSON array valid berisi **79 source**. File `.aix`-nya juga hidup (contoh: `ar.aasq-v1.aix` → HTTP 200, 54.222 byte). Jadi sisi server sehat, `sources/` kosong bukan karena URL busuk.

Catatan kecil: satu WebFetch summarizer bilang 96 source. Angka yang gue pakai adalah **79**, hasil `json.load` langsung. Yang 96 itu pembacaan model atas teks terkonversi, abaikan.

**Repo hulu rakuyomi sudah ARCHIVED.** `github.com/hanatsumi/rakuyomi` read-only sejak 2026-01-05. README-nya sendiri nunjuk ke fork `tachibana-shin/rakuyomi` sebagai penerus resmi. v1.22.2 (2025-06-22) adalah rilis terakhir hanatsumi. Fork aktif: **v1.39.6, 2026-07-31**, masih nyediain `rakuyomi-kindlehf.zip`.

**Ada bug terbuka yang persis kombinasi lu.** Issue hanatsumi #209: "Rakuyomi encountered an issue while starting up", Kindle Paperwhite 4, KOReader 2025.10, rakuyomi v1.22.2 kindlehf, dilaporkan 2025-11-05. Masih open, dan repo-nya archived jadi nggak akan pernah di-fix di sana.

**Legacy list nggak punya MANGA Plus sama sekali.** Filter 79 entri buat string `plus` cuma nemu `en.manhuaplus` (ManhuaPlus, aggregator, nggak ada hubungannya). Nol `mangaplus`, nol `shueisha`.

**v1.22.2 nggak bisa install dari list format baru.** Ini fakta kode, bukan tebakan. Di tag v1.22.2, `backend/shared/src/usecases/install_source.rs`:

```rust
.json::<Vec<SourceListItem>>()
...
let aix_url = source_list.join(&format!("sources/{}", &source_list_item.file))

#[derive(Deserialize)]
struct SourceListItem { id: SourceId, file: String }
```

List SDK baru nggak punya key `file` — mereka pakai `downloadURL` / `iconURL`. Deserialisasi gagal. Contoh entri baru:

```json
{"id":"multi.mangaplus","name":"MANGA Plus","version":4,
 "iconURL":"...","downloadURL":"sources/multi.mangaplus-v4.aix"}
```

**Arsitektur runtime.** `rakuyomi.koplugin` membundel binary Rust `server` (~20 MB, ELF 32-bit ARM EABI5, statically linked) yang dijalankan KOReader; frontend Lua ngobrol ke situ lewat HTTP. Kalau binary itu nggak start, UI-nya kosong melompong. (Di Android doang server itu dipisah jadi companion app di `127.0.0.1:8787` — nggak relevan buat Kindle.)

**`kindlehf` memang build yang benar.** Dokumen fork: Kindle 4 atau lebih baru dengan firmware ≥ 5.16.3 → build "Kindle (hard floats)". FW 5.18.5.0.1 ≥ 5.16.3. Jangan ganti ke build `kindle` biasa.

### 1.2 Hipotesis (belum dibuktikan)

**UNVERIFIED — kenapa `sources/` di device LU kosong.** Gue nggak nyentuh Kindle-nya. Dua penjelasan sama-sama muat:

- **H1**: backend `server` crash pas startup (pola issue #209), jadi Manage Sources nggak pernah bisa nampilin apa-apa.
- **H2**: nggak ada yang crash, lu cuma belum pernah tap satu source pun di Manage Sources. `sources/` memang kosong sampai source pertama di-install.

Pembeda: `koreader/crash.log` di device.

**UNVERIFIED** lain yang jujur harus disebut:

- Apakah v1.22.2 bahkan sampai ke tahap fetch source list di KOReader 2025.10 — binary-nya nggak gue jalanin.
- Apakah fork v1.39.6 beneran memperbaiki crash KOReader 2025.10. CHANGELOG fork nggak nyebut 2025.10 maupun issue #209; entri terdekat cuma "revert fix fork because koreader fixed (#221)". Nggak ada commit fix eksplisit yang gue temukan. Jadi upgrade = langkah paling masuk akal, **bukan** perbaikan yang dijamin.
- Apakah v1.22.2 akan gagal *listing* atau gagal *install* dari list format baru. `list_available_sources` cuma deserialize `{id,name,version}`, yang dipenuhi entri format baru — jadi source mungkin **muncul** di UI lalu gagal pas di-tap. Gue baca kodenya, nggak eksekusi.

### 1.3 Langkah perbaikan

Kindle mount di Mac lu sebagai `/Volumes/Kindle`. Semua path di bawah relatif ke situ.

**Step 0 — baca crash log dulu, sebelum ngapa-ngapain.**

```bash
ls -la /Volumes/Kindle/koreader/rakuyomi/
tail -200 /Volumes/Kindle/koreader/crash.log
```

- Kalau `crash.log` punya baris soal rakuyomi / `server` / spawn gagal → H1. Upgrade plugin wajib.
- Kalau bersih dan cuma ada `settings.json` tanpa `sources/` berisi → kemungkinan H2. Coba dulu install satu source lewat UI (Step 3) sebelum bongkar plugin.

**Step 1 — backup.**

```bash
cp -R /Volumes/Kindle/koreader/plugins/rakuyomi.koplugin ~/kindle-backup/rakuyomi.koplugin.v1.22.2
cp /Volumes/Kindle/koreader/rakuyomi/settings.json ~/kindle-backup/rakuyomi-settings.json.bak
```

**Step 2 — ganti plugin dengan fork yang dirawat.**

Ambil `rakuyomi-kindlehf.zip` dari rilis `tachibana-shin/rakuyomi` v1.39.6 (2026-07-31). Lalu:

```bash
rm -rf /Volumes/Kindle/koreader/plugins/rakuyomi.koplugin
unzip rakuyomi-kindlehf.zip -d /Volumes/Kindle/koreader/plugins/
```

Hasil akhir harus ada `/Volumes/Kindle/koreader/plugins/rakuyomi.koplugin/server` (binary ~20 MB).

**Step 3 — tulis ulang source list.**

File: `/Volumes/Kindle/koreader/rakuyomi/settings.json`. Ganti nilai `source_lists` jadi default fork:

```json
{
  "source_lists": [
    "https://tachibana-shin.github.io/aidoku-sources-next/index.min.json",
    "https://aidoku-community.github.io/sources/index.min.json"
  ]
}
```

Step 2 dan Step 3 harus barengan. Plugin lama nggak bisa parse list baru; list lama nggak punya MANGA Plus. Setengah-setengah = tetap mentok.

Kedua URL sudah gue verifikasi hidup: `aidoku-sources-next` → 200, 25.016 byte, 115 source. `aidoku-community` → 200, 26.940 byte, 122 source.

**Step 4 — eject, reboot KOReader, install source lewat UI.**

Di device: File Manager → tap bagian atas layar → menu search → **Rakuyomi** → hamburger menu → **Manage Sources** → tombol **+** → tap source di daftar Available Sources buat install. Source list URL cuma dibaca dari `settings.json`, nggak ada UI-nya.

**Step 5 — verifikasi, urut:**

1. Rakuyomi kebuka tanpa dialog "encountered an issue while starting up".
2. Manage Sources → **+** nampilin daftar panjang (ratusan), bukan kosong.
3. Setelah tap install satu source, `/Volumes/Kindle/koreader/rakuyomi/sources/` berisi direktori/file baru. Ini bukti keras bahwa jalur install jalan.
4. Search di source itu ngasih hasil dan satu chapter bisa dibuka.
5. Kalau masih gagal: `tail -200 /Volumes/Kindle/koreader/crash.log` lagi, bandingkan dengan log sebelum upgrade.

Kalau setelah Step 5 masih mati juga, jangan ngotot. Pindah ke jalur 2 (server-side) yang nggak bergantung pada runtime WASM di Kindle sama sekali.

---

## 2. Tiga jalur

### Jalur 1 — rakuyomi (on-device)

**Cara kerja**: plugin KOReader jalanin backend Rust lokal yang eksekusi source Aidoku (WASM). Browse, search, dan baca langsung dari Kindle, tanpa server.

**Reliability**: paling rapuh dari tiga. Titik gagalnya numpuk: build ARM 32-bit harus cocok firmware, KOReader upgrade bisa mecahin plugin (persis yang kejadian), source WASM harus cocok ABI runtime, dan Wi-Fi Kindle harus nyala tiap kali browse. Repo hulu archived; lu sekarang bergantung pada satu fork perorangan.

**Maintenance**: menengah-tinggi. Tiap upgrade KOReader adalah judi. Tiap kali plugin di-update lu colok Kindle dan copy manual — nggak ada auto-update.

**Legal**: plugin dan runtime-nya FOSS, netral. Status hukum ditentukan sepenuhnya oleh source apa yang lu install. Source resmi (mis. MANGA Plus) beda kelas dengan aggregator. Fakta itu aja; keputusan di lu.

**Kapan dipakai**: baca spontan tanpa siapin apa-apa, dan lu nggak keberatan sesekali benerin plugin.

### Jalur 2 — Suwayomi + OPDS (server-side)

**Rekomendasi arsitektur: Suwayomi-Server saja.** Bukan Suwayomi + Komga/Kavita.

**Cara kerja**: Suwayomi jalan di VPS, ngurus library + download ke CBZ, dan menyajikan feed OPDS 1.2 sendiri di `/api/opds/v1.2`. KOReader nambahin katalog OPDS ke situ, download CBZ, baca offline.

**Kenapa Suwayomi doang** (tiga alasan, semuanya terverifikasi):

1. **Kompatibilitas KOReader.** KOReader v2025.10 cuma bisa parse **OPDS 1.2 Atom XML**. Nol dukungan OPDS 2.0 JSON — itu baru masuk di KOReader v2026.07 "Sailing Walrus" (26 Jul 2026). Suwayomi, Komga, Kavita semuanya ngomong Atom 1.2, jadi ini seri. Suwayomi malah punya mode "direct stream/download links" (v2.3.2223) yang eksplisit ditujukan buat downloader macam KOReader. Kavita satu-satunya yang punya riwayat pecah sama KOReader (issue #1199: "Failed to parse catalog", 500 di bawah root feed) — sudah closed, tapi cuma dia yang punya bekas luka di sini.

2. **Resilience.** Argumen "tambah Komga biar tetap kebaca kalau source mati" itu benar, tapi Suwayomi sendiri sudah nutup celah itu: set `server.autoDownloadNewChapters = true` + `server.downloadAsCbz = true`, CBZ-nya nulis ke disk dan OPDS Suwayomi nyajikan dari local storage duluan. Asuransi ekstra dari server kedua cuma nutupi skenario "Suwayomi sendiri yang mati" — risiko lebih kecil daripada source mati.

3. **RAM, dan ini yang menentukan.** VPS2 (`<vps2-ip>`) hasil recon read-only: **AlmaLinux 8.10** (bukan 8.9 seperti di CLAUDE.md), **1 vCPU** Xeon kelas server, **2246 MB RAM** (431 MB terpakai, 1594 MB available), **40 GB disk, 26 GB free**, Docker 26.1.3 + Compose v2.27.0, nginx 1.14.1 pegang 80/443, certbot 1.22.0 dengan 7 lineage LE, firewalld aktif (cuma 22/80/443 publik), SELinux **Disabled**. Sudah ada 9 vhost nginx dan ~6 service app jalan di situ. Ini bukan box kosong.

   Aritmetiknya: Suwayomi (JVM, **UNVERIFIED** ~350 MB kalau `-Xmx` di-cap) + Kavita (~250 MB) + service Python (~100 MB) ≈ 700 MB dari 1594 MB → lega. Ganti Kavita dengan Komga (~500-900 MB, default max heap JVM ~1 GB) → ~1150 MB, dua JVM rebutan satu core. Itu yang bakal gigit. Kalau memang butuh Komga, jalankan tanpa Suwayomi atau pindah ke box lain — ini overcommit lurus, bukan soal tuning.

**Setup ringkas:**

1. Jalanin Suwayomi di Docker, port localhost-only, reverse-proxy lewat nginx yang sudah ada. **Cap heap eksplisit** (`-Xmx512m` atau `--memory=512m`) — jangan percaya default.
2. `server.downloadAsCbz = true`, `server.autoDownloadNewChapters = true`, `server.autoDownloadNewChaptersLimit = 3..5` (biar series dengan backlog panjang nggak ngabisin disk), `server.excludeEntryWithUnreadChapters = true` (sudah default).
3. `server.downloadsPath` ke path khusus, dan **pasang alert disk**. 26 GB free itu ceiling yang lebih ketat daripada RAM buat library manga.
4. `server.downloadConversions` buat normalisasi WebP → JPEG (`{ target: "image/jpeg", compressionLevel: 0.8 }`). Murah, sudah didukung, motong disk.
5. Di KOReader: tambah katalog OPDS ke `https://<host>/api/opds/v1.2`, isi username/password (Basic auth). Suwayomi ngaktifin Basic auth lewat query param `opds` buat klien yang nggak bisa pegang cookie.

**Aturan keras dari sisi KOReader** (semua hasil baca source koreader v2025.10 + luasocket commit yang di-pin + luasec v1.3.2):

- **Cuma HTTP Basic.** Nggak ada UI buat custom header atau bearer token. Kredensial dikirim preemptif di request pertama, jadi nggak butuh challenge 401.
- **Jangan taruh login form HTML / session cookie di depan feed.** KOReader ikut redirect, dapat HTML, kasih ke parser Atom, hasilnya nol entri, dan UI **diam saja** — katalog kosong tanpa pesan error. Ini failure mode terburuk karena kelihatan seperti "feed rusak".
- **Hindari 30x di link download.** LuaSocket ikut redirect tapi **membuang** user/password di hop berikutnya (`tredirect` cuma nyalin url/source/sink/headers/proxy/maxredirects/nredirects/create). Redirect ke CDN pre-signed tanpa auth → aman. Redirect ke path lain yang masih ber-auth → 401.
- **TLS tidak diverifikasi sama sekali.** LuaSec default `verify = "none"`, dan KOReader nggak pernah nge-set param TLS per-request; `cfg` di `ssl/https.lua` itu file-local, nggak bisa dioverride dari Lua. KOReader juga nggak ngirim CA bundle apa pun, dan dia bundle LibreSSL 4.2.0 sendiri — jadi umur firmware Kindle nggak relevan buat TLS. Konsekuensinya: Let's Encrypt aman dipakai, tapi yang lu dapat cuma enkripsi tanpa autentikasi server. Kredensial Basic bisa di-MITM attacker aktif. Mitigasinya: pakai token per-device yang bisa dicabut, bukan password akun. Bukan ngoprek config TLS — lu nggak bisa nyentuhnya dari plugin OPDS.
- **Download satu file itu blocking di UI thread**, cuma ada toast "Downloading…" 1 detik, nggak ada progress bar. Reader beku selama transfer. Timeout total 60 detik praktis nggak pernah kepicu karena `downloadFile` pakai `ltn12.sink.file`, bukan `socketutil.file_sink` yang enforce total_timeout. Konsekuensi praktis: **lebih baik banyak CBZ per-chapter daripada CBZ per-volume**.
- Mimetype yang dikenali: `application/vnd.comicbook+zip`, `application/x-cbz`, `application/epub+zip`, `application/pdf`, `application/x-mobipocket-ebook`. Ekstensi file dicek duluan sebelum mimetype; link yang nggak match dua-duanya **dihilangkan diam-diam** dari dialog download.
- rel="next" pagination dan OpenSearch didukung penuh. KOReader agresif prefetch halaman berikutnya, jadi jangan set page size gede.

**Troubleshooting order kalau download gagal:** flip `server.opdsCbzMimetype` (`MODERN` ↔ legacy) duluan — setting itu ada persis buat kompatibilitas klien, dan **UNVERIFIED** nilai mana yang benar buat KOReader. Kedua, aktifin direct download/stream link mode (v2.3.2223).

**Reliability**: paling tinggi dari tiga. CBZ yang sudah turun ke disk tetap kebaca meski source, Suwayomi, atau internet mati. Kindle nggak perlu Wi-Fi buat baca yang sudah diunduh.

**Maintenance**: paling tinggi juga. Lu jadi punya JVM service, cert renewal, disk monitoring, dan satu container lagi di box yang sudah nampung 9 vhost. Kalau OOM, yang ikut mati bukan cuma manga — ada sebuah app Next.js internal, <internal-service>, <internal-service> di situ. Set `--memory` limit eksplisit di tiap container baru.

**Legal**: Suwayomi FOSS, netral. Yang menentukan sekali lagi source extension yang lu pasang.

**Blocker sebelum deploy**: `<hub-host>` **NXDOMAIN** — belum ada. Nggak ada wildcard di zone `<apex-domain>`, dan apex-nya nunjuk ke VPS1 (<vps1-ip>), jadi nggak ada pewarisan. Zone-nya di nameserver Hostinger (`<dns-provider nameservers>`), artinya bikin A record `kindle → <vps2-ip>` itu **perubahan DNS Hostinger**, bukan rumahweb. Tanpa record itu, certbot HTTP-01 gagal. `<vps2-host>` sudah benar resolve ke <vps2-ip>, ACME webroot juga sudah terpasang di `/var/www/acme` + `/etc/nginx/conf.d/00-acme.conf`, jadi issuance tinggal `certbot certonly --webroot`.

**UNVERIFIED** di jalur ini:
- RSS asli Suwayomi/Kavita/Komga di box ini. Nggak ada yang diinstall, semua angka itu estimasi engineering.
- Default `-Xmx` Suwayomi. Nggak disebut di README maupun wiki config.
- Konfirmasi end-to-end KOReader **v2025.10 spesifik** ↔ Suwayomi OPDS. Upstream bilang sudah dites sama KOReader, tapi nggak ada pairing versi yang didokumentasikan dan gue nggak tes.
- Apakah OPDS-PSE (page streaming) jalan di browser OPDS KOReader sama sekali. KOReader nggak terdaftar sebagai klien PSE di mana pun yang gue temukan. **Rencanakan download CBZ utuh, jangan streaming.**
- Apakah egress dari VPS2 difilter. Cuma inbound firewalld yang gue baca.
- Apakah MuPDF/koptinterface punya batas memori praktis waktu **membuka** CBZ raksasa di Kindle low-RAM. Itu pertanyaan document engine, bukan OPDS client, dan nggak gue selidiki.

### Jalur 3 — MANGA Plus by Shueisha

Ini bagian yang harus paling jujur, karena hasil verifikasinya nggak sesederhana "ada source-nya, pakai".

**Yang terverifikasi ada:**

Source Aidoku `multi.mangaplus` **beneran ada dan aktif dirawat**. Name "MANGA Plus", version 4, baseURL `https://mangaplus.shueisha.co.jp`, bahasa en/es/fr/**id**/pt-BR/ru/th/vi/de, `contentRating: 0`, `minAppVersion: 0.7.2`. Ada di dua list: `aidoku-community.github.io/sources/index.min.json` dan `tachibana-shin.github.io/aidoku-sources-next/index.min.json`. Binary-nya hidup (`multi.mangaplus-v4.aix` → HTTP 200, 129.590 byte). Ekuivalen Mihon/Suwayomi ada di `keiyoushi/extensions-source` path `src/all/mangaplus`.

**Yang juga terverifikasi, dan mengubah kesimpulan:**

Source itu **bukan** klien API resmi. Dia manggil backend privat aplikasi MANGA Plus: `https://jumpg-webapi.tokyo-cdn.com/api` dan `https://jumpg-api.tokyo-cdn.com/api` (konstanta di `src/lib.rs` baris 20-22). Riwayat commit-nya sendiri yang membuka kartu: "mobile api option, with auth query params" (2026-02-18) dan "use random session token" (2026-05-18) — itu meniru klien resmi.

Terms of Use MANGA Plus yang gue baca langsung:
- Web Article 7(1): melarang "duplication (including screen shot)", distribusi, alterasi; dan translasi/duplikasi di luar private use.
- Web Article 7(4) / App 9(1)(4): melarang reverse engineering, decompile, disassemble.
- App Article 9(1)(5): melarang "Unauthorized access to and/or manipulation or elimination of the Application/the website".

Shueisha tidak menerbitkan API publik atau program developer apa pun yang gue temukan. (**UNVERIFIED**: gue nggak bisa membuktikan negatif — mungkin ada program partner yang nggak terindeks. Tapi nggak ada developer portal, nggak ada dokumen API, nggak disinggung di ToS.)

**Kesimpulan: tidak ada jalur otomatis yang legal buat MANGA Plus ke Kindle.** Source-nya ada dan bisa dipasang; itu bukan hal yang sama dengan diizinkan. Gratis bukan berarti bebas syarat. Fakta sudah disampaikan; sisanya keputusan lu.

**Rekomendasi: baca MANGA Plus di app resmi iOS/Android atau browser desktop.**

Browser Kindle bukan workaround. `mangaplus.shueisha.co.jp` mengembalikan shell HTML identik **2350 byte** untuk `/terms_of_service`, `/about`, `/faq`, `/privacy_policy` — SPA client-rendered murni, nol konten server-rendered. Browser eksperimental Kindle sangat kecil kemungkinannya menjalankan itu (**UNVERIFIED**, nggak ada Kindle buat dites, tapi jangan bikin rencana di atasnya).

Kalau syaratnya harus e-ink dan harus legal: beli volume-nya lewat toko Kindle native, atau arahkan KOReader ke konten yang memang dilisensikan untuk redistribusi.

**UNVERIFIED** tambahan di jalur ini:
- Apakah app resmi MANGA Plus punya fitur offline download. Satu snippet pencarian bilang tidak, tapi nggak bisa gue konfirmasi dari halaman milik Shueisha (SPA, nggak kebaca).
- Teks lengkap pasal ToS. WebFetch cuma ngasih fragmen terkutip + nomor artikel. Gue **tidak** menemukan kata harfiah "bot", "crawler", "scraper", atau "automated" di ToS. Larangan yang terkonfirmasi adalah duplikasi, distribusi, reverse engineering, dan unauthorized access. Perlakukan "ToS melarang scraping" sebagai **inferensi** dari pasal-pasal itu, bukan kutipan verbatim.
- Apakah `multi.mangaplus` (`minAppVersion 0.7.2`, ABI Rust/WASM Aidoku baru) benar-benar **load** di build rakuyomi mana pun. Klaim fork bahwa dia dukung "legacy SDK maupun next SDK" itu klaim penulis fork, bukan hasil tes gue.
- Apakah perilaku geo/licensing MANGA Plus (jendela ketersediaan chapter, region lock) sama lewat klien pihak ketiga vs app resmi.

---

## 3. Preprocessing CBZ untuk e-ink

Kalau lu naruh CBZ mentah hasil scan di Kindle, page turn bakal terasa berat. Ini alasan teknisnya, bukan mitos.

**Kenapa lambat.** Halaman manga scan mentah biasanya 1600-3000 px sisi panjang, RGB 24-bit, JPEG/PNG/WebP. Tiap page turn, KOReader harus: decode gambar full-res ke memori (2000×3000 RGB ≈ 18 MB uncompressed per halaman), downscale ke panel, konversi ke grayscale, dither, baru refresh e-ink. Kindle punya CPU lemah dan RAM sedikit. Yang mahal itu decode + rescale, dan itu diulang tiap halaman karena cache-nya kecil. Hasilnya jeda satu-dua detik tiap turn — yang bikin baca manga panjang terasa nyiksa.

Kalau gambarnya sudah tepat seukuran panel dan sudah 8-bit grayscale, decode-nya jauh lebih murah dan rescale hampir nol kerja. Ukuran file juga turun banyak, yang penting buat disk 26 GB dan buat transfer.

**Requirement:**

1. **Resolusi** — downscale ke resolusi panel device persis. Jangan lebih besar (kebuang percuma, mahal), jangan lebih kecil (blur permanen).
2. **Grayscale** — konversi ke 8-bit grayscale. Panel e-ink cuma 16 level abu. Menyimpan RGB = 3× data yang di-decode lalu dibuang.
3. **Gamma correction** — linearisasi. Tanpa ini, area gelap manga cenderung mampet jadi hitam solid di e-ink.
4. **Crop border** — buang margin putih scan. Nambah area baca efektif, sering setara satu step zoom.
5. **Panel/strip split** untuk halaman spread (`--maximizestrips`) kalau lu baca di layar kecil.

**Tool: KCC (`ciromattia/kcc`).** Punya CLI headless beneran — `kcc-c2e` (comic to ebook) dan `kcc-c2p` (panel splitter); dependency Qt6/PySide6 itu cuma buat GUI. Ada container multi-arch resmi `ghcr.io/ciromattia/kcc:latest` (v11.0.0, amd64/arm64/armv7). Profil Kindle tersedia: K1, K2, K11, K34, K57, K810, KDX, KPW*, KV, KO, KCS, Scribe. Output bisa CBZ.

**Profil mana buat device lu: UNVERIFIED** — model Kindle spesifik belum dikonfirmasi. Cek dulu, jangan asal tebak, karena salah profil = salah resolusi = manfaatnya hilang. Referensi umum: PW3/PW4 1072×1448, PW5/Oasis 1236×1648. Konfirmasi model dulu baru pilih profil.

**Posisi KCC di pipeline: jangan dijadikan service.**

Di VPS2 dengan 1 vCPU, image processing per-halaman untuk tiap chapter yang auto-download bakal rebutan CPU dengan nginx, Next.js sebuah app Next.js internal, dan <internal-service>. Suwayomi juga nggak butuh KCC buat nyajikan CBZ yang kebaca — KOReader render CBZ mentah baik-baik saja dan punya contrast/dithering sendiri di device.

Postur yang disarankan:
- Default: skip KCC di jalur otomatis. Pakai `server.downloadConversions` Suwayomi buat WebP → JPEG (murah, built-in).
- Kalau kualitas atau kecepatan on-device beneran mengecewakan: jalankan KCC sebagai batch `nice`-ed manual atas series tertentu, di Mac lu (bukan di VPS — Mac lu jauh lebih kencang dan nggak nabrak production).

**UNVERIFIED** soal KCC:
- Apakah image ghcr resmi ngasih CLI sebagai default entrypoint atau nunggu GUI. Dockerfile/entrypoint-nya nggak gue baca.
- Apakah output CBZ hasil KCC tetap mulus di-import ulang ke Suwayomi/KOReader (handling ComicInfo). Nggak dicek.
- Ide "KCC sebagai remote endpoint `downloadConversions`": setting-nya ada dan konversinya per-image, tapi nggak ada implementasi yang pernah gue lihat jalan. Ini ide bikin-sendiri, bukan pola terdokumentasi.

---

## 4. Tabel keputusan

| Situasi baca | Jalur | Alasan |
|---|---|---|
| One Piece / Jump series, mau legal, di rumah | MANGA Plus, app resmi di HP/desktop | Satu-satunya rute resmi. Nggak ada jalur otomatis legal ke Kindle. |
| Baca offline lama (pesawat, perjalanan, sinyal jelek) | Suwayomi → OPDS → CBZ turun duluan | CBZ di disk Kindle nggak butuh apa-apa. Paling tahan gangguan. |
| Iseng, di kasur, pengen langsung browse & baca di Kindle | rakuyomi (setelah diperbaiki) | Nol setup per-sesi. Butuh Wi-Fi. Paling rapuh. |
| Ngejar chapter mingguan otomatis tanpa disentuh | Suwayomi `autoDownloadNewChapters` + OPDS | Server yang kerja, Kindle tinggal narik. |
| Series panjang, arsip, mau tetap ada meski source mati | Suwayomi dengan `downloadAsCbz` | CBZ jadi milik lu, independen dari source. |
| Sekali baca, nggak mau nambah beban VPS | rakuyomi | Nggak nyentuh server sama sekali. |
| Halaman berat / page turn lemot | CBZ pre-processed KCC, sideload manual | Satu-satunya yang benar-benar mecahin masalah decode. |
| KOReader baru di-upgrade dan rakuyomi mati lagi | Suwayomi/OPDS sebagai fallback | OPDS itu fitur inti KOReader, nggak akan pecah karena plugin. |

**Postur akhir yang disarankan**: Suwayomi + OPDS sebagai tulang punggung (paling andal, offline-first), rakuyomi sebagai kenyamanan sehari-hari setelah dimigrasi ke fork, MANGA Plus dibaca di app resminya. Ketiganya nggak saling bertentangan.

---

## 5. Ringkasan koreksi terhadap catatan lama

- VPS2 itu **AlmaLinux 8.10**, bukan 8.9.
- VPS2 **bukan** kapasitas cadangan. Sudah ada 9 vhost nginx (termasuk <internal-site>, <internal-service>, <internal-service>, dua situs internal lain) dan 4 systemd service custom. Perlakukan lebih dekat ke box produksi kedua daripada sandbox. Project memory soal box ini basi.
- Rilis Suwayomi terkini v2.3.2243 (13 Jul 2026). Kode OPDS-nya sudah setahun lewat dari introduksi v2.0 — bukan fitur baru yang lu pertaruhkan.
- URL source rakuyomi lu **tidak** mati. Jangan buang waktu ngejar itu.
