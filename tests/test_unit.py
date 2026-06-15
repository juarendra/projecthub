"""Unit test fungsi murni ProjectHub. Jalankan: python3 tests/test_unit.py (atau pytest)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _raises(fn, *a):
    try:
        fn(*a)
        return False
    except Exception:
        return True


def test_ext_of():
    assert app._ext_of("SpeedTester.kicad_pcb") == "kicad_pcb"
    assert app._ext_of("Makefile") == "makefile"
    assert app._ext_of(".gitignore") == "gitignore"
    assert app._ext_of("real1.JPEG") == "jpeg"


def test_safe_path_confined():
    p = app._safe_path("sub/file.txt")
    assert p.startswith(app.CODE_ROOT)
    # leading slash dinetralkan, tetap confined
    assert app._safe_path("/etc/passwd").startswith(app.CODE_ROOT)


def test_safe_path_rejects_escape():
    for bad in ["..", "../../etc", "a/../../..", "../x"]:
        assert _raises(app._safe_path, bad), "harus tolak: " + bad


def test_decode_utf16_and_utf8():
    raw = b"\xff\xfe" + "# Judul\nhalo dunia".encode("utf-16-le")
    out = app._decode_text(raw)
    assert "halo dunia" in out and "\x00" not in out
    assert app._decode_text("héllo".encode("utf-8")) == "héllo"


def test_suggest_version():
    assert app._suggest_version("v1.2.3") == "v1.2.4"
    assert app._suggest_version("2.0.0") == "v2.0.1"
    assert app._suggest_version(None) == "v0.1.0"


def test_done_status_id():
    assert app._done_status_id([{"id": "todo", "name": "To Do"}, {"id": "done", "name": "Complete"}]) == "done"
    assert app._done_status_id([{"id": "a", "name": "Mulai"}, {"id": "b", "name": "Selesai"}]) == "b"
    assert app._done_status_id([{"id": "x", "name": "X"}]) == "x"  # fallback last


def test_name_re():
    assert app.NAME_RE.match("my-repo_1.2")
    assert not app.NAME_RE.match("bad name")
    assert not app.NAME_RE.match("-bad")
    assert not app.NAME_RE.match(".hidden")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            fail += 1
            print("FAIL", fn.__name__, "->", e)
    print(f"\n{len(fns)-fail}/{len(fns)} passed")
    sys.exit(1 if fail else 0)
