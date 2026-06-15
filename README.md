# ProjectHub

**Satu tempat untuk semua project-mu — kelola task, jelajah kode, dan kendalikan Git/GitHub tanpa pindah aplikasi.**

Bayangkan ClickUp + file explorer + GitHub Desktop + viewer 3D/PCB + asisten AI, jadi satu, ringan, jalan offline, dan kamu yang punya datanya. ProjectHub dibuat untuk kamu yang punya banyak project (terutama elektronik/PCB & coding) dan lelah lompat-lompat antar tool.

> Backend **satu file** (`app.py`, FastAPI + sqlite3, tanpa ORM) dan frontend **satu file** (`static/index.html`, Alpine.js + Tailwind). Semua library di-host lokal — **tidak butuh internet untuk jalan**. Pasang sekali, pakai dari mana saja di jaringanmu.

## Kenapa kamu mau pasang ini?

- 🗂️ **Task management seperti ClickUp/Trello** — tanpa langganan, tanpa batas, tanpa data keluar dari mesinmu.
- 📁 **Jelajahi puluhan repo dalam satu layar** — baca code, Markdown, PDF, DOCX, Excel, gambar, sampai **model 3D STEP/STL** dan **board PCB (KiCad & Gerber)** langsung di browser.
- 🔀 **Git/GitHub tanpa terminal** — buat repo, push/pull, lihat diff & history, bikin Release, kelola Issues, semuanya lewat tombol.
- 🤖 **Asisten AI bawaan** — generate pesan commit, release notes, README, dan review perubahan sebelum push.
- 📱 **Pengingat & ringkasan ke WhatsApp** — deadline harian dan rekap mingguan otomatis.
- 🔒 **Sepenuhnya milikmu** — self-hosted, offline-first, login pribadi, backup otomatis.

## Fitur lengkap

### Task management
- Hierarki **Space › List › Task › Subtask**, status custom per-list, prioritas, deadline, tag, estimasi, recurrence.
- Tampilan **List**, **Board kanban (drag-drop)**, **Calendar**, **My Work** (fokus "yang sedang dikerjakan"), **Dashboard**, **Aktivitas**.
- Quick-add bahasa natural (`lapor PCB besok #hardware !urgent`), command palette (Ctrl/⌘K), undo, tema terang/gelap, responsif HP.

### Project Explorer
- Telusuri folder kode **read-only** dengan grid bertumbnail + auto-refresh.
- Viewer: kode (syntax highlight), Markdown, CSV, XLSX, DOCX, PDF, gambar.
- **Viewer 3D** STEP/STL/OBJ/3MF, **viewer KiCad** (.kicad_pcb & skematik), **viewer Gerber** (render board top/bottom) — semua offline.
- Cari **isi file** (grep) dalam satu project atau **semua project sekaligus**.

### Git / GitHub (branch: main)
- Buat project baru → folder + repo + push otomatis (pilih public/private), atau **clone** repo yang sudah ada.
- Status git (dirty/ahead/behind/conflict), Push, Pull, **diff viewer**, **commit history**, **Release** (zip repo / pilih file) + versioning.
- **GitHub Issues ↔ task**, **board kanban per repo** (kelola task tepat di samping code-nya), editor **.gitignore** dengan pilih file.

### AI & otomasi
- Generate commit message, release notes, README; review diff sebelum push; daily standup — via agen AI (OpenClaw).
- Update task dari WhatsApp lewat API aman.

### Lainnya
- Login (pbkdf2 + signed cookie), **backup database harian** + export, pengingat & auto-review WhatsApp, logging terpusat.

## Pasang (cepat)

```bash
git clone https://github.com/<owner>/projecthub.git
cd projecthub
pip install fastapi uvicorn python-multipart mammoth openpyxl
uvicorn app:app --host 0.0.0.0 --port 5055
```

Buka **http://localhost:5055** (atau IP mesinmu dari perangkat lain). Selesai.

> Tanpa file `data/auth.json`, app terbuka tanpa login (cocok untuk coba cepat). Untuk mengaktifkan login, buat akun lewat halaman setup/login.

### Opsional (fitur penuh)
- **Viewer KiCad/Gerber/3D**: sudah termasuk (library di `static/vendor/`, offline).
- **Git/GitHub**: butuh `git` dan [`gh` CLI](https://cli.github.com/) ter-login di mesin host.
- **AI & WhatsApp**: butuh agen AI (OpenClaw) terpasang di host.

## Konfigurasi

Atur lewat environment variable:

| Env | Default | Keterangan |
|-----|---------|------------|
| `PH_CODE_ROOT` | `~/Github` | Folder berisi repo project yang ditelusuri Explorer |
| `PH_GH_OWNER` | — | Username GitHub untuk membuat/mengelola repo |

## Jalan otomatis saat boot (opsional)

Tersedia contoh unit **systemd** (`projecthub.service`) untuk menjalankan ProjectHub sebagai service di Linux (mis. mini-PC / SBC di rumah).

## Stack

FastAPI + sqlite3 (stdlib, tanpa ORM) · Alpine.js + Tailwind · SortableJS, marked, DOMPurify, highlight.js, online-3d-viewer, KiCanvas, pcb-stackup — semua di-vendor lokal (offline-first).

## Lisensi

MIT — bebas dipakai, ubah, dan distribusikan.
