# ProjectHub

Personal project manager + code explorer dengan integrasi Git/GitHub, AI review, dan viewer 3D — dibangun untuk mengelola banyak project pribadi (terutama elektronik/PCB) di satu tempat.

Single-file FastAPI backend (`app.py`, stdlib `sqlite3`, tanpa ORM) + single-file SPA (`static/index.html`, Alpine.js + Tailwind, semua vendor library di-host lokal untuk jalan offline).

## Fitur

### Task management (ClickUp/Trello-style)
- Hierarki **Space > List > Task > Subtask**, status custom per-list, prioritas, due date, tag, estimasi, recurrence.
- Tampilan **List**, **Board (kanban drag-drop)**, **Calendar**, **My Work** (project-first), **Dashboard**, **Aktivitas**.
- Command palette (Ctrl/⌘K), quick-add natural language, undo delete, light/dark theme, responsif mobile.

### Project Explorer (browse 60+ repo)
- Telusuri folder kode read-only: tree lazy, viewer untuk **code (highlight), Markdown, CSV, XLSX, DOCX, PDF, gambar**.
- Grid kartu project dengan thumbnail (`DOC/thumbnail.jpeg`) + auto-refresh.
- **Search isi file** (grep), filter cepat Docs/Foto/Code.
- **Viewer 3D** STEP/STL/OBJ/3MF (online-3d-viewer + occt, offline).

### Git / GitHub (branch: main)
- **Buat project baru** → folder + `git init` + `gh repo create` + push, pilih public/private.
- Status git (dirty/ahead/behind/conflict), **Push**, **Pull**, **diff viewer**, **commit history**, **Release** (zip repo / pilih file) + versioning.
- **GitHub Issues ↔ task** (import/buat), badge visibility per repo.
- **Board task per repo** — 1 repo = 1 kanban, kelola task tepat di samping code-nya.

### AI (OpenClaw)
- Generate pesan commit, release notes, README; review diff sebelum push; AI review file; daily standup.

### Lainnya
- Auth (pbkdf2 + signed cookie), backup DB harian + export, reminder & auto-review WhatsApp via cron.

## Jalankan

```bash
pip install fastapi uvicorn python-multipart mammoth openpyxl
uvicorn app:app --host 0.0.0.0 --port 5055
```

Akses `http://localhost:5055`. Tanpa `data/auth.json`, app terbuka (no auth). Override folder kode yang dibrowse via env `PH_CODE_ROOT`.

## Konfigurasi

| Env | Default | Keterangan |
|-----|---------|------------|
| `PH_CODE_ROOT` | `/home/rendra/shared/Pribadi/Github` | Root folder project yang dibrowse Explorer |
| `PH_GH_OWNER` | `juarendra` | Owner GitHub untuk create repo |

## Lisensi

MIT
