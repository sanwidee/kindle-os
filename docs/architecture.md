# Arsitektur kindle-os

Catatan kerja. Bahasa santai, isinya serius. Semua klaim di sini ditandai:

- **[V]** = terverifikasi (baca source code, cek device langsung, atau SSH read-only)
- **[U]** = belum diverifikasi, dibawa terus sebagai langkah tes, jangan diperlakukan sebagai fakta

Repo ini publik. Jadi di dokumen ini **tidak ada IP, tidak ada hostname asli, tidak
ada kredensial**. Host-nya ditulis `<hub-host>`. Nilai aslinya ada di catatan ops
privat sama password manager.

Status: **belum ada yang di-deploy**. Ini desain, bukan laporan hasil.

---

## 1. Gambaran besar

Satu content store, dua pintu, dua-duanya berakhir di nginx yang sudah ada di box.

```
 ── SISI DEV (laptop) ───────────────────────────────────
   Sesi Claude Code
        │  nulis ./out/*.md (YAML front matter + markdown)
        ▼
   tools/send_to_inbox.sh   →  rsync over SSH, key terbatas, forced command
        │
 ══ port 22 ═════════════════════════════════════════════
        ▼
 ── VPS <hub-host> ──────────────────────────────────────

   inbox/<collection>/<name>.md        ← target rsync, di-mount :ro
   library/<doc_id>/<slug>.<hash>.epub ← hasil build, :rw
   state/hub.db                        ← SQLite (WAL)
        │
        ▼
  ┌──────────────────────────────────────────────────┐
  │ container kindle-hub  (127.0.0.1:8090 SAJA)      │
  │ python:3.12-slim, gunicorn 1×gthread(4), 256m    │
  │                                                   │
  │  builder thread (flock, sweep 60 detik)          │
  │    scanner → frontmatter → render → epub → store │
  │                                                   │
  │  WSGI app (Flask)                                │
  │    /opds/**  OPDS 1.2 Atom   [Basic ONLY]        │
  │    /d/**     web reader      [session cookie]    │
  │    /admin/** rebuild, health                     │
  │    byte EPUB → X-Accel-Redirect → nginx          │
  └──────────────────────────────────────────────────┘
        ▲                          │
        │ proxy_pass               │ internal: /_epub/
        ▼                          ▼
  ┌──────────────────────────────────────────────────┐
  │ nginx (SHARED — vhost lain hidup di sini juga)   │
  │  :80  → ACME + 301                                │
  │  :443 → proxy + location internal + limit_req    │
  └──────────────────────────────────────────────────┘
        ▲
   ┌────┴────────────────┬─────────────────┐
   │                     │                 │
 Kindle/KOReader     Browser        (Fase 2: HTTP ingest)
 Basic + Atom XML    cookie
```

Prinsip yang nyetir semuanya: **auth ada di depan setiap byte, secara struktural**,
bukan karena URL-nya susah ditebak.

---

## 2. Kenapa dua pintu, bukan satu

Ini bukan pilihan estetika. Ini dipaksa sama klien.

KOReader v2025.10 OPDS client cuma ngerti **satu** dialek auth: HTTP Basic,
dikirim preemptive di request pertama, field username/password diisi di dialog
"Add catalog" **[V]**. Nggak ada cookie jar **[V]**, nggak ada UI buat custom
header atau bearer token **[V]**, nggak bisa isi form login **[V]**.

Konsekuensi paling penting, dan ini failure mode terburuknya: **kalau `/opds`
di-redirect ke halaman login HTML, KOReader ngikutin redirect-nya, dapet HTML,
nyuapin HTML itu ke parser Atom, dapet nol entry, terus nggak nampilin error apa
pun — cuma katalog kosong** **[V]**. Kelihatannya kayak server rusak, padahal cuma
belum login. Jadi `/opds` yang unauthenticated **wajib** balikin 401 polos.
Selamanya. Ini invariant, bukan preferensi.

Sisi web sebaliknya: form login → hash → session cookie. Masalah bosan yang udah
selesai 20 tahun lalu.

Karena dua klien ini nggak bisa share mekanisme, mereka juga **nggak boleh share
kredensial**. Alasannya di §4.

---

## 3. Pintu 1: Web (browser)

- App Python kecil di `127.0.0.1:8xxx`, di-proxy nginx. Nggak buka port firewall baru.
- **Satu password bersama**, form HTML di `/login`. Nggak ada username.
- Hash: **argon2id** via argon2-cffi, `time_cost=3`, `memory_cost=65536` (64 MiB),
  `parallelism=1`. Box cuma 1 vCPU **[V]**; 64 MiB per verify aman karena `/login`
  di-rate-limit ke angka satu digit per menit.
- Session **server-side**: ID random 128-bit (`secrets.token_urlsafe(16)+`),
  disimpan hash-nya di tabel SQLite (id-hash, created_at, last_seen).
- Cookie: nama `__Host-session`; `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`,
  tanpa atribut `Domain`.
- Umur: sliding idle 30 hari, hard cap 90 hari. "Logout everywhere" =
  `DELETE FROM sessions`.
- `SameSite=Lax`, bukan Strict, biar nge-tap link artifact dari WhatsApp/Slack
  mendarat dalam keadaan authed, bukan di halaman login.

### Kenapa nggak pakai akun per-user

Yang pakai satu orang. Akun per-user cuma nambah permukaan: enumerasi username,
flow reset password, admin akun. Nol proteksi tambahan karena cuma ada satu
manusia dan satu tingkat kepercayaan. Satu passphrase kuat (25+ karakter dari
password manager) plus rate limit agresif lebih sederhana dan sama kuatnya.

Kalau nanti ada pembaca kedua dengan hak berbeda, **itu** momennya nambah akun.
Bukan sekarang.

### CSRF

Hub ini read-only lewat HTTP. GET nge-render/nyajiin konten; endpoint yang ngubah
state cuma `POST /login` dan `POST /logout`. `SameSite=Lax` udah blokir POST
cross-site. Tetep pasang CSRF token per-form di `/login` karena murah dan matiin
login-CSRF. Kalau nanti ada endpoint upload/delete, dia ikut aturan token yang
sama. Nggak usah bangun machinery CSRF lebih dari itu.

### Brute force

- nginx: `limit_req_zone $binary_remote_addr zone=login:10m rate=5r/m;` dengan
  `burst=5 nodelay` di `location = /login`.
- App: delay konstan ~1 detik di setiap verify yang gagal.
- fail2ban udah jalan di box **[V]**, tapi **jail-nya cuma sshd** **[V]** — nggak
  ada proteksi HTTP sama sekali sekarang. Jadi `limit_req` di vhost itu beneran
  kerja, bukan hiasan. Tambahin jail buat 401/403 berulang di vhost ini:
  10 gagal / 10 menit → ban 1 jam.
- **Nggak ada hard lockout.** Dengan satu password bersama, lockout itu tombol DoS
  yang diarahin ke diri sendiri. Throttle + fail2ban ngasih biaya yang sama ke
  penyerang tanpa efek samping itu.

Failed auth (web maupun OPDS) balikin 401 dengan body generik. Nggak bocorin
"password salah" vs "user nggak ada" — lagian nggak ada user.

---

## 4. Pintu 2: Kindle (OPDS)

- Endpoint: `https://<hub-host>/opds/`, **OPDS 1.2 Atom XML doang**. KOReader
  v2025.10 nol dukungan OPDS 2.0/JSON **[V]** (baru masuk di v2026.07).
- Auth: **HTTP Basic**, satu-satunya yang didukung **[V]**. Cek di app (baca header
  `Authorization`) supaya bisa nge-log device mana yang konek; `auth_basic` nginx
  juga sah, cuma kehilangan log itu.
- Kredensial: username `kindle-pw4`, password = token random 32 karakter
  (`openssl rand -base64 24`), **BUKAN** password web manusia.
- Server nyimpen `sha256(token)` doang, compare constant-time. Hash cepat itu
  benar di sini: token punya ~144 bit entropi, argon2 nggak nambah apa-apa dan
  cuma ngebakar satu-satunya vCPU.
- Satu token per device. Kalau nambah reader, mint token kedua (`kindle-2`, dst).

### Kenapa Basic tiap request masih oke di sini

1. Selalu di dalam TLS.
2. Kredensialnya token mesin yang cuma buka satu hal: baca feed + download file.
   Nggak bisa bikin session web, nggak bisa nulis, nggak nyentuh apa pun lain.
3. Rotasi = satu baris di DB. Hapus hash, mint token baru, ketik ulang sekali di
   Kindle.

### Kenapa bukan token di URL (pola Kavita)

`/opds/<token>/` jalan di KOReader, tapi URL nyangkut di access log nginx, history
browser, dan header Referer. Header `Authorization` nggak di-log secara default.
Dukungan klien sama, profil kebocoran jelas lebih jelek.

### Higiene protokol — semua dari source KOReader

Ini bagian yang paling gampang bikin rusak diam-diam. Semua **[V]**:

- **Feed dan URL akuisisi harus SAME ORIGIN, ZERO 30x.** luasocket ngikutin
  redirect tapi **ngedrop `user`/`password`** di hop-nya, jadi redirect ke path
  ber-auth lain bakal 401 tanpa penjelasan. Sajikan byte langsung.
- **Selalu kirim `Content-Length`.** Jangan stall >15 detik di tengah stream
  (block timeout).
- **Link akuisisi wajib berakhiran ekstensi asli** (`.epub`) **DAN** deklarasi
  mimetype yang dikenal (`application/epub+zip`). KOReader ngecek ekstensi file
  **sebelum** mimetype; link yang nggak cocok dua-duanya di-drop diam-diam dari
  dialog download. Query string ngerusak cek ekstensi — jangan pakai `?token=`.
- **Download file tunggal nge-freeze UI KOReader selama transfer**, tanpa progress
  bar. Jadi bikin EPUB-nya seukuran artikel/bab, jangan seukuran volume.
- **JANGAN PERNAH redirect `/opds` yang unauthenticated ke halaman login.**
  (Lihat §2.)

### Yang harus diakui jujur soal TLS

KOReader **nggak memvalidasi sertifikat sama sekali**. LuaSec dipakai dengan
`verify = "none"`, `cfg` itu file-local jadi nggak bisa di-override dari Lua, dan
nggak ada CA bundle yang dibundel **[V]**. Artinya:

- Umur firmware Kindle **tidak relevan** buat kompatibilitas Let's Encrypt. Isu
  CA-bundle basi itu non-isu.
- Tapi TLS di jalur Kindle = **enkripsi tanpa autentikasi server**. Penyerang aktif
  di jalur bisa MITM Kindle dan nyolong token-nya.
- Ini **nggak bisa diperbaiki dari sisi klien**. Plugin OPDS nggak ngasih knob apa
  pun. Mitigasinya desain kredensial (token low-value, read-only, revocable), bukan
  ngoprek TLS.
- **[U]** Binary KOReader yang dikirim ke Kindle benar-benar nge-link LibreSSL
  4.2.0 yang dibundel — buktinya cuma resep build, belum dites di device. Dan
  versi TLS mana yang dinegosiasi juga **[U]**. Makanya: **biarkan TLS 1.2 tetap
  aktif** di vhost, jangan 1.3-only. Kalau handshake gagal di device, itu hal
  pertama yang dicek. **Jangan pernah** fallback ke HTTP polos.

### Yang harus diakui jujur soal penyimpanan di device

KOReader nyimpen kredensial OPDS **plaintext** di settings file, di device yang
kalau dicolok USB mount sebagai mass storage nggak terenkripsi. Siapa pun yang
minjem, nyuri, atau nyolokin Kindle bisa baca token itu. **Nggak bisa diperbaiki
di device.** Yang bisa dilakukan cuma membatasi:

1. Yang disimpan token device, bukan password manusia. Blast radius = akses baca
   sampai dirotasi.
2. Kehilangan kendali fisik atas Kindle = anggap token kompromi, rotasi langsung.
3. Token itu nggak bisa bikin session web dan nggak nyentuh apa pun lain di box.
4. Log akses per-token di server, biar pemakaian aneh kelihatan.

Risiko residual ini diterima. Alternatifnya cuma satu: nggak usah bikin fitur
Kindle-nya sama sekali.

---

## 5. Isolasi konten

Ini bagian yang paling gampang dianggap remeh dan paling mahal kalau salah.

- Semua artifact hidup **di luar docroot mana pun**, mode 750, dimiliki user unix
  khusus.
- Default-deny di nginx: vhost cuma punya tiga keluarga location — `/login` (plus
  aset statis form), `/` di-proxy ke app (dicek session), `/opds/` + `/dl/` (dicek
  Basic). Sisanya 404/401. `autoindex off` di mana-mana, dotfile ditolak.
- `default_server` balikin 444 biar traffic scan berbasis IP nggak pernah nyampe
  vhost app.
- Byte file di-stream lewat **X-Accel-Redirect**: app ngecek auth (cookie ATAU
  Basic), terus balikin `X-Accel-Redirect` ke
  `location /_epub/ { internal; alias <library-dir>; }`. Kata kunci `internal`
  bikin nginx nolak request eksternal ke `/_epub/...` mentah-mentah.

Efeknya: **nggak ada URL yang bisa ditebak klien unauthenticated buat nyampe ke
file**. Cek auth ada di depan setiap byte secara struktural, dan Python nggak
pernah sibuk nge-stream file di box 1 vCPU.

### Soal slug random

Slug nggak ketebak (`/a/8f3k2.../title`) tetap dipakai sebagai ID artifact — dia
nyegah enumerasi dan bikin entri log yang bocor jadi kurang informatif. Tapi dia
**suplemen, bukan access control**.

URL itu bearer secret tanpa expiry, tanpa revocation, dan punya belasan kanal
kebocoran: history browser, share sheet HP, header Referer, backup settings
KOReader, access log nginx, chat app yang prefetch link. Auth dicek di setiap
request tanpa peduli entropi slug-nya.

### Secrets

- Nggak ada apa-apa di git. Repo cuma bawa `.env.example`.
- File secret di `/etc/<app>/secrets.env`, `root:<app>` 640, dimuat lewat
  `EnvironmentFile=` systemd (atau `LoadCredential=`).
- Isinya: `ARGON2_PASSWORD_HASH`, `SESSION_SIGNING_KEY` (defense-in-depth
  walaupun session-nya server-side). Digest token device ada di tabel SQLite app.
- Plaintext token device cuma eksis di dua tempat: Kindle, dan password manager.

---

## 6. Kanal OPDS: bentuk feed-nya

### Rak

| Path | Jenis | Isi |
|---|---|---|
| `/opds` | navigation | Root, link ke rak-rak di bawah |
| `/opds/new` | acquisition | Semua, `updated` desc, paginated. **Ini yang dijadiin catalog URL di Kindle** |
| `/opds/collections` | navigation | Satu entry per subdirektori inbox |
| `/opds/collections/<name>` | acquisition | Isi koleksi itu |
| `/opds/tags` | navigation | Satu entry per tag front-matter, plus jumlah |
| `/opds/tags/<tag>` | acquisition | Isi tag itu |
| `/opds/all` | acquisition | Semua, judul asc |
| `/opds/search?q=` | acquisition | Hasil pencarian |
| `/opds/opensearch.xml` | descriptor | `application/opensearchdescription+xml` |

25 entry per halaman. KOReader **agresif prefetch** halaman `rel="next"` sampai
layar penuh **[V]**, jadi halaman kegedean cuma buang round trip di device yang
nggak punya progress indicator.

### Aturan XML yang nggak boleh dilanggar

Parser OPDS KOReader itu luxl (lexer XML streaming) yang rapuh, dan sebelum
di-handoff, XML-nya **di-mangle pakai regex** dulu **[V]**. Jadi:

- **Nggak ada** komentar XML (`<!--`).
- **Nggak ada** CDATA.
- **Nggak ada** `<?xml-stylesheet?>`.
- `<summary type="text">`, jangan XHTML di dalam `<content>`.
- `<id>` = `uuid5(namespace, relpath)` — stabil lintas rebuild, jadi identitas
  entry selamat waktu dokumennya direvisi.
- `length` diisi, biar kelihatan berapa mahal satu tap di download yang nge-block
  dan nggak punya progress bar **[V]**.

Semua aturan ini di-encode jadi tes: `tests/test_koreader_constraints.py`. Itu file
yang nyegah refactor di masa depan diam-diam matiin jalur Kindle.

### Cara KOReader lihat update

KOReader **nggak polling di background**. Update muncul waktu katalognya dibuka,
dan mekanismenya spesifik **[V]**:

1. Tiap load feed nembak `HEAD` dulu, terus nge-cache di memori (20 slot) dengan
   key `"opds|catalog|<url>|<last-modified>"`.
2. Jadi **setiap route feed wajib jawab `HEAD` dengan `Last-Modified` yang akurat**,
   dihitung `MAX(updated)` di rak itu. Ini detail server-side paling berdaya ungkit
   di seluruh desain OPDS.
3. **[U]** Kalau `Last-Modified` nggak ada, key cache-nya jadi konstan per URL, yang
   dari pembacaan source kelihatannya bakal ngunci rak itu ke kondisi pertama
   selama sesi KOReader hidup. Itu inferensi dari konstruksi hash, belum dieksekusi.
   Jadikan negative test, jangan fakta.
4. Cache-nya in-memory, jadi restart KOReader ngebersihin. Rak basi yang bikin
   bingung selalu tinggal satu restart.
5. **Revisi.** Nama file akuisisi bawa build hash 8 karakter, jadi dokumen yang
   diedit turun sebagai file berbeda, bukan nabrak salinan lama. Harganya: KOReader
   nyimpen progress baca per-file, jadi revisi mulai dari halaman satu. Alternatif
   nama file stabil bikin KOReader nanya soal file existing dan berisiko baca
   salinan basi tanpa sadar. Hash-in-filename jadi default; `STABLE_FILENAMES=1`
   buat yang milih trade-off satunya.
6. **Penghapusan.** Dokumen yang dihapus dari inbox langsung hilang dari feed, tapi
   EPUB-nya nongkrong 7 hari sebelum di-GC, biar download yang lagi jalan nggak 404.
7. **[U]** Checkbox "Sync catalog" ada di dialog add-catalog **[V]**, tapi
   semantiknya belum jelas. Tes dulu, jangan didesain di atasnya.

---

## 7. Konversi ke EPUB e-ink

Detail lengkapnya nyusul di dokumen converter; ini prinsipnya, dan semua
prinsipnya soal panel, bukan soal selera.

- **Nggak embed font sama sekali.** KOReader punya manajemen font sendiri dan itu
  preferensi pembaca; font yang di-embed malah ngelawan. Stylesheet nggak nyetel
  `font-family` di `body`, cuma `monospace` di `pre`/`code`. Ukuran pakai `em`/`%`,
  jangan `pt`/`px`.
- **Code block: reflow saat build, hanging indent, nggak ada highlighting.**
  Kindle di ukuran font nyaman muat ~40-55 karakter monospace; baris code asli
  80-120. crengine nggak punya scroll horizontal. Jadi reflow ke `CODE_COLUMNS`
  (default 64), baris lanjutan diindent +2 dan diprefix `↳`. Highlighting dibuang
  karena di panel 16 level abu-abu, tema warna kolaps jadi beberapa abu yang mirip
  semua — nambah noise, nol informasi. Struktur dibawa border kiri 3 px, indent,
  monospace, dan label bahasa kecil di atas blok.
- **Tabel: ukur dulu, transpose kalau nggak muat.** Muat → `<table>` beneran.
  Nggak muat → tiap baris jadi blok dengan kolom pertama sebagai heading dan `<dl>`
  `kolom: nilai` di bawahnya. Grid yang kepotong itu sama dengan nggak ada.
- **Gambar: grayscale, kuantisasi 16 level, PNG-8, downscale ke lebar panel.**
  **Jangan pernah fetch gambar remote** — itu vektor SSRF dari box semi-produksi,
  dan gambar remote nggak guna di device yang baca offline. Referensi `http(s)`
  di-render sebagai placeholder berlabel.
- **Nggak ada background fill.** `background-color` abu di code block bakal
  di-dither jadi noise dan memperparah ghosting antar page turn. Ini cara paling
  umum stylesheet yang didesain di LCD kelihatan kotor di Kindle. Pakai border.
- **Teks hitam murni.** Abu-abu tengah kehilangan kontras parah di layar reflektif.
- **Emoji di-strip.** Font Kindle nggak punya coverage-nya, jadinya kotak tofu.
- Justifikasi dan hyphenation dibiarin — itu setting user KOReader, nggak usah
  sok tau.
- **Web reader dapet versi asli**: code nggak di-wrap, tabel tetap grid dalam
  `overflow-x: auto`. Browser bisa scroll horizontal, jadi biarin dia scroll.
  Asimetri ini justru alasan besar kenapa dua pintu itu worth it, bukan duplikasi.

Satu parse markdown, dua profil renderer dari token stream yang sama. Bukan parse
dua kali, bukan post-process HTML.

---

## 8. Ingest: rsync over SSH

Fase 1 pakai rsync, bukan HTTP upload, bukan git, bukan Syncthing.

Alasannya, urut bobot:

1. **Nggak butuh apa pun yang belum ada.** SSH key auth udah jalan **[V]**, port 22
   udah kebuka **[V]**, dan itu satu-satunya yang beneran dijaga fail2ban **[V]**.
   Opsi lain semuanya minta service baru / port baru / secret baru / daemon baru di
   box yang udah rame.
2. **Nggak nunggu desain auth.** Dua pintu yang butuh auth itu OPDS dan web reader.
   Ingest sengaja bukan salah satunya, jadi kerja converter dan feed bisa jalan
   duluan.
3. **Gagalnya aman.** Ingest rusak = file nggak nyampe. Endpoint upload HTTP rusak
   di box tanpa rate limiting HTTP = sesuatu yang lebih buruk.
4. **Masalah partial write udah beres gratis.** rsync nulis ke nama temp lalu
   rename; scanner skip apa pun yang basename-nya diawali titik.

Key-nya dikunci, bukan cuma dipercaya:

```
command="/usr/local/bin/rrsync -wo <inbox-dir>",restrict ssh-ed25519 AAAA... kindle-os-ingest
```

`restrict` matiin port forwarding, agent forwarding, PTY, X11, dan `~/.ssh/rc`
sekaligus. Forced command bikin key itu nggak bisa dapet shell — cuma rsync,
write-only, terkurung di inbox. Sesi yang megang key ini nggak bisa baca library,
nggak bisa baca project lain, nggak bisa eskalasi.

**Kelemahannya, terang-terangan:** cuma jalan dari mesin yang megang key. Sesi
Claude Code di cloud atau dari HP nggak bisa. Itu yang dijawab Fase 2.

---

## 9. Seam buat fase berikutnya

Desain ini sengaja ninggalin beberapa sambungan yang jelas. Fase berikutnya nyolok
ke situ, bukan bongkar ulang.

**Seam ingest — inbox itu kontrak direktori polos.** File markdown di subdirektori,
dotfile diabaikan, nggak ada coupling ke database, nggak ada protokol lock. Scanner
nggak tau dan nggak peduli siapa yang nulis. Nambah `POST /ingest` di Fase 2 —
setelah auth ada dan bisa dipakai ulang — itu writer baru terhadap kontrak yang
sama, dan nggak ngubah apa pun di hilir. Dua jalur ingest bisa hidup bareng
selamanya. Catatan: `client_max_body_size` nginx default 1 MB, jadi HTTP ingest
butuh perubahan vhost juga.

**Seam auth — `Principal` + protocol `Authenticator` + registry.** Decorator
`require_opds` / `require_web` / `require_admin` dipasang di route; implementasinya
bisa diganti tanpa nyentuh route. Fase 1 bisa jalan pakai placeholder token statis
dari env supaya converter dan feed bisa dites duluan.

**Seam writer EPUB — `write_epub(document, path) -> None`.** Sekarang ditulis
tangan (~200 baris, stdlib `zipfile`) karena tiap keputusan e-ink di §7 hidup
persis di layer yang biasanya di-abstract library, dan karena kita butuh
determinisme byte (hash konten masuk nama file). Kalau file itu jadi time sink,
tukar implementasinya di balik signature yang sama.

**Seam manga (Fase 3).** Suwayomi punya OPDS-nya sendiri di `/api/opds/v1.2`
(Atom 1.2, cocok sama KOReader v2025.10). Dia jalan sebagai service terpisah di
belakang nginx yang sama, bukan di-merge ke hub. Alasannya: siklus rilisnya beda,
runtime-nya beda (JVM), dan profil risikonya beda. Kindle nyimpen dua katalog OPDS,
selesai. Detail dan pertimbangan legalnya di `docs/manga.md`.

**Seam sync progress (Fase 4).** kosync udah terpasang di device **[V]**. Kalau
mau self-hosted, itu service tambahan lagi di belakang nginx yang sama, dan sekali
lagi bukan urusan hub.

---

## 10. Yang sengaja ditolak

Ditulis biar nggak dibahas ulang tiap tiga bulan.

| Ditolak | Kenapa |
|---|---|
| Akun per-user | Satu manusia. Nambah enumerasi + reset flow + admin, nol proteksi. |
| oauth2-proxy / OIDC / SSO di depan semuanya | KOReader nggak bisa nyelesaiin flow berbasis redirect **[V]**. Buat sisi web, itu nyerahin keputusan satu-password ke pihak ketiga plus satu hop proxy. |
| Form login di depan path OPDS | Katalog kosong senyap **[V]**. Failure mode terburuk. |
| Token di URL path (pola Kavita) | Jalan, tapi bocor lewat log/history/Referer. Sama dukungannya, lebih jelek. |
| Plugin `.koplugin` custom buat bearer auth | Bisa (wallabag jadi template kerja **[V]**), tapi itu kode device yang harus di-ship dan di-update selamanya, buat gantiin mekanisme yang udah cukup. |
| mTLS / client cert | Klien OPDS KOReader nggak ekspos konfigurasi TLS sama sekali **[V]**. Mustahil di sisi Kindle. |
| VPN / WireGuard-only | Matiin syarat "buka link dari browser mana pun". Dan WireGuard di bawah networking KOReader di Kindle jailbroken itu wilayah nggak didukung. |
| IP allowlist | Baca dari jaringan mobile dengan IP rotasi. Ini generator lockout, bukan kontrol. |
| Hard lockout setelah N gagal | Tombol DoS yang diarahin ke pemiliknya sendiri. |
| Enkripsi at-rest / EPUB terenkripsi | KOReader nggak bisa dekripsi (matiin pintu Kindle), dan di box bersama kuncinya bakal nongkrong sebelah datanya. Jujur aja: paparan at-rest ke kompromi root itu **diterima**, bukan direkayasa. |
| TOTP/2FA di pintu web | Defensible, tapi ngelindungin password yang cuma hidup di password manager dan nggak pernah diketik di mesin orang. Nambah failure mode (HP hilang = kekunci dari catatan sendiri). Gampang ditambah nanti. |
| Feed OPDS 2.0 JSON | Nol dukungan di v2025.10 **[V]**. |
| WAF / CAPTCHA / cloud proxy | Nggak ada yang terekspos unauthenticated. fail2ban + rate limit cukup buat noise bot. |
| FastAPI | Nggak ada cerita async: satu Kindle, satu browser, kerja beratnya CPU-bound di thread background pada satu-satunya vCPU. Output-nya XML + HTML template, jadi Jinja2 wajib apa pun frameworknya. Nggak ada JSON body buat divalidasi. Werkzeug juga nanganin conditional request / `HEAD` / `Last-Modified` dengan benar — dan itu persis yang jadi tumpuan cache OPDS KOReader **[V]**. |
| EbookLib | Narik `lxml` + `six` **[V]**, buat nyerialisasi OPF dan NCX yang tiap elemennya udah kita kontrol. Dan kita butuh determinisme byte. |
| pygments, watchdog, beautifulsoup4, lxml | Highlighting dibuang by design (§7); sweep scandir 60 detik lebih murah dari dependensi inotify di skala ini; sisanya nggak kepakai. |
| Komga di box ini (Fase 3) | JVM kedua di sebelah Suwayomi, di 1 vCPU. Bukan marginal — overcommit lurus. Kavita kalau butuh library server kedua. |
| KCC di jalur otomatis | Pemrosesan per-halaman di setiap chapter bakal rebutan CPU sama semua yang lain. Simpan sebagai batch sesekali. |

---

## 11. Risiko residual — diterima, bukan diselesaikan

1. **MITM aktif di jalur Kindle bisa nyolong token device.** KOReader nggak
   validasi sertifikat **[V]** dan itu nggak bisa dikonfigurasi. Diterima karena
   token-nya read-only, single-purpose, rotasi satu baris. Paparan praktisnya =
   jaringan Wi-Fi yang dimasukin Kindle.
2. **Akses fisik ke Kindle (atau USB-nya, atau backup settings KOReader) = token
   plaintext.** Nggak bisa diperbaiki di device. Prosedurnya: rotasi begitu kendali
   fisik hilang. Nggak pernah ngasih password web.
3. **Blast radius box bersama.** Box ini nge-host beberapa vhost dan service lain
   **[V]**. Kompromi level root di salah satunya bisa baca artifact store dalam
   plaintext. Desain ini **nggak** ngelindungin dari host yang dikompromi. Kalau
   sensitivitas konten lewat dari itu, jawabannya box khusus atau enkripsi at-rest
   dengan kunci di tempat lain — bukan nambah layer auth di sini.
4. **Satu password web = satu titik kegagalan.** Siapa pun yang dapet, dapet
   semuanya sampai dirotasi. Diterima buat sistem satu user.
5. **Session 30 hari.** HP kecuri dalam keadaan unlock di jendela itu bisa baca hub.
   Kill switch: hapus tabel sessions.
6. **Rate limit nge-key ke IP klien.** Tebakan terdistribusi low-and-slow tetap di
   bawah limit per-IP. Dengan passphrase 25+ karakter dan argon2id 64 MiB ini
   irelevan secara komputasi — tapi itu justru artinya **kekuatan password**, bukan
   limiter, yang jadi kontrol sesungguhnya.
7. **Attacker level negara / insider hosting provider.** Nggak didefensi, dan itu
   dikatakan terang-terangan. Konten duduk plaintext at-rest di VM sewaan.
8. **[U] nginx di box itu build lama.** Dialek config dan perilaku HTTP/2-nya
   ketinggalan dari nginx modern. `listen 443 ssl http2;` (direktif `http2 on;`
   baru ada di 1.25.1 dan bakal bikin `nginx -t` gagal). Kalau X-Accel-Redirect
   atau `limit_req` aneh, cek build flag duluan.

---

## 12. Bahaya deployment (RHEL family)

Diurut dari yang paling mungkin bikin masalah nyata. Ini semua hasil recon
read-only; nggak ada yang di-deploy, di-start, di-stop, atau diubah.

1. **`/usr/bin/python3` itu 3.6.8** **[V]**. Semua dependensi runtime butuh ≥3.10.
   `dnf` sendiri jalan di platform-python 3.6, jadi ngotorin site-packages-nya itu
   cara nyata buat ngerusak package management. Ini argumen terkuat buat container.
   Kalau jalur bare-metal diambil, venv wajib dibikin pakai `python3.12 -m venv`
   eksplisit, jangan `python3`.
2. **Docker publish nembus firewalld.** Nggak ada `/etc/docker/daemon.json` **[V]**
   dan firewalld pakai backend nftables **[V]**, jadi `ports: ["8090:8090"]` bisa
   dijangkau dari internet walaupun `firewall-cmd --list-all` kelihatan
   menenangkan. **Selalu bind eksplisit: `127.0.0.1:8090:8090`.** Jangan percaya
   output firewalld — tes dari luar box.
3. **Webroot ACME itu per-vhost, bukan global.** Config ACME yang ada di-scope ke
   satu server_name doang **[V]**. Subdomain baru bakal 404 di challenge HTTP-01
   sampai punya blok port-80 sendiri. Urutannya ketat: DNS → blok :80 + reload →
   `certbot certonly` → blok :443. Nembak certbot duluan itu cara paling gampang
   ngabisin jatah percobaan Let's Encrypt.
4. **Sertifikat renew tapi nginx nggak pernah reload.** Nggak ada lineage yang
   punya `renew_hook` dan direktori deploy-hook kosong **[V]**, padahal timer
   renew aktif. Ini bug laten yang **udah ada** dan kena ke semua sertifikat di box,
   bukan cuma yang baru. Buat kindle spesifiknya: pintu browser mulai error ~60
   hari setelah issue sementara KOReader jalan terus karena dia nggak verifikasi
   sertifikat **[V]** — gagal dengan cara paling membingungkan. Fixnya satu baris
   script, tapi dia ngubah perilaku bersama buat sertifikat produksi orang lain.
   **Angkat sebagai keputusan, jangan benerin diam-diam.**
5. **`dnf`, bukan `apt`.** certbot udah ada dari EPEL dengan timer aktif **[V]**.
   Jangan `pip install certbot`, jangan snap — itu bikin state machine renewal
   kedua yang berantem sama yang udah ada.
6. **fail2ban cuma jaga SSH** **[V]**. Bagusnya: KOReader yang retry Basic auth
   nggak bakal bikin IP rumah kena ban. Jeleknya: pintu HTTP baru nggak punya
   proteksi brute-force sama sekali sampai kita bikin.
7. **SELinux Disabled** **[V]** — dan itu bahaya, bukan kelegaan. Sekarang nggak
   perlu label `:z`/`:Z`, nggak perlu `httpd_can_network_connect`. Tapi kalau
   SELinux dibalikin ke enforcing (update kernel, hardening pass, ada yang
   beres-beres), tiga hal rusak barengan: `proxy_pass` nginx ke 127.0.0.1, bind
   mount, dan pembacaan file X-Accel-Redirect. Command `doctor` harus nge-print
   `getenforce` dan nolak lapor healthy kalau berubah.
8. **cgroup v1 + driver cgroupfs** **[V]**. `mem_limit` jalan, tapi akuntansi swap
   gaya v1 dan box punya swap gede. Set `memswap_limit` barengan `mem_limit`, atau
   build yang ngamuk bakal paging berat dan nyiksa satu-satunya vCPU sementara
   semua yang lain nunggu.
9. **Logging json-file nggak dibatasi.** Nggak ada `daemon.json` = nggak ada rotasi
   global **[V]**. Set `logging.options.max-size` dan `max-file` per service di
   compose.
10. **File `.conf.bak-*` di `conf.d` itu inert** — nginx cuma include `*.conf`
    **[V]**. Ikutin konvensi yang ada.
11. **Ini box semi-produksi, bukan sandbox.** Ada vhost dan service lain yang
    hidup di sini, termasuk yang nyentuh pelanggan bayar **[V]**. Aturan praktis:
    selalu `nginx -t` sebelum reload, selalu `reload` jangan `restart`, dan inget
    nggak ada MCP buat provider box ini — box yang nggak mau boot berarti recovery
    manual lewat panel web.
12. **Jam.** Kebenaran `Last-Modified` yang nyetir cache KOReader. Pastiin
    `timedatectl` nunjukin NTP synchronized. Sepele, tapi jam yang skew
    menghasilkan gejala rak-basi yang mirip banget sama bug aplikasi.

---

## 13. Daftar UNVERIFIED yang dibawa terus

Nggak ada satu pun di bawah ini yang boleh dianggap beres.

1. Model Kindle dan resolusi panel — default `EINK_MAX_WIDTH` 1072 itu asumsi.
   (Audit device bilang PW-class; konfirmasi di KOReader → Help → About.)
2. Apakah menghilangkan `Last-Modified` beneran ngunci cache feed KOReader selama
   sesi. Diinferensi dari konstruksi cache key, belum dieksekusi.
3. Apa yang sebenarnya dilakukan checkbox "Sync catalog".
4. Apakah `FILE_TOTAL_TIMEOUT` 60 detik beneran ompong di download besar, dan
   gimana UI beku itu kelihatan.
5. Coverage glyph `☐`/`☑` di font bawaan KOReader.
6. Dukungan SVG crengine. Fase 1 ngehindar pakai placeholder.
7. RSS container beneran di bawah beban. Perkiraan 45-75 MB itu ekstrapolasi dari
   service Flask sebanding, bukan pengukuran.
8. Konfirmasi end-to-end bahwa KOReader v2025.10 nge-download EPUB dari feed ini
   tanpa masalah. Semua analisis di atas statik, dari source.
9. Apakah binary Kindle nge-link LibreSSL yang dibundel, dan versi TLS mana yang
   dinegosiasi.
