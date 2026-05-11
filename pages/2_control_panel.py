import os
import sys
import subprocess
import signal
import time
import streamlit as st

# Tambahkan root folder ke path agar preflight_check bisa diimport dari /pages/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import preflight_check

st.set_page_config(page_title="⚙️ Bot Control", page_icon="⚙️", layout="wide")

st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .check-ok  { color: #00c853; font-weight: 600; }
    .check-err { color: #ff5252; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

BOT_PID_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bot_pid.txt"
)

# =============================================================================
# Helpers — process management
# =============================================================================
def _get_saved_pid() -> int | None:
    try:
        if os.path.exists(BOT_PID_FILE):
            with open(BOT_PID_FILE) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None

def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        # Windows: os.kill(pid, 0) kirim CTRL_C_EVENT (signal 0) yang mematikan proses!
        # Gunakan tasklist untuk cek apakah PID masih hidup tanpa menyentuhnya.
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode(errors="ignore")
            return str(pid) in output
        except Exception:
            return False
    else:
        # Unix/Linux: signal 0 aman — hanya cek keberadaan proses
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

def bot_process_running() -> bool:
    pid = _get_saved_pid()
    return pid is not None and _pid_is_running(pid)

def start_bot():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    with open(BOT_PID_FILE, "w") as f:
        f.write(str(proc.pid))

def stop_bot():
    pid = _get_saved_pid()
    if pid and _pid_is_running(pid):
        try:
            if os.name == "nt":
                subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)])
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if os.path.exists(BOT_PID_FILE):
        os.remove(BOT_PID_FILE)

# =============================================================================
# UI
# =============================================================================
st.markdown("## ⚙️ Bot Control Panel")
st.caption("Start / Stop bot `main.py` langsung dari browser.")
st.divider()

# ── Status Bot ───────────────────────────────────────────────────────────────
running = bot_process_running()

if running:
    st.success("🟢 **Bot sedang BERJALAN**")
    pid = _get_saved_pid()
    st.caption(f"PID: {pid}")
else:
    st.error("🔴 **Bot tidak aktif**")

st.divider()

# ── Tombol Start / Stop ───────────────────────────────────────────────────────
col_start, col_stop, _ = st.columns([1, 1, 3])

with col_start:
    start_clicked = st.button(
        "▶️ Start Bot",
        type="primary",
        disabled=running,
        use_container_width=True,
    )

with col_stop:
    if st.button("⏹️ Stop Bot", type="secondary",
                 disabled=not running, use_container_width=True):
        stop_bot()
        st.warning("Bot dihentikan.")
        time.sleep(1)
        st.rerun()

# ── Validasi saat Start diklik ────────────────────────────────────────────────
if start_clicked:
    with st.spinner("🔍 Memvalidasi `strategy.py` sebelum bot dijalankan..."):
        errors, warnings = preflight_check.validate_strategy()

    if warnings:
        for i, w in enumerate(warnings, 1):
            with st.expander(f"⚠️ Saran #{i} (bot tetap bisa jalan)", expanded=False):
                st.code(w, language="text")

    if errors:
        # Ada error → tampilkan semua, JANGAN jalankan bot
        st.error(
            f"⛔ **Bot TIDAK dijalankan** — ditemukan **{len(errors)} masalah fatal** "
            "pada `strategy.py`. Perbaiki sebelum melanjutkan:"
        )
        for i, err in enumerate(errors, 1):
            with st.expander(f"❌ Masalah #{i}", expanded=True):
                st.code(err, language="text")
    else:
        # Semua OK → jalankan bot
        st.success("✅ **Validasi berhasil** — semua pengecekan `strategy.py` lolos!")
        with st.spinner("Menyalakan bot..."):
            start_bot()
            time.sleep(1.5)
        st.success("🚀 Bot berhasil dinyalakan!")
        time.sleep(1)
        st.rerun()

st.divider()

# ── Panel Preflight Manual ────────────────────────────────────────────────────
with st.expander("🔍 Jalankan validasi manual (tanpa Start Bot)", expanded=False):
    if st.button("Cek strategy.py sekarang", key="manual_check"):
        with st.spinner("Memvalidasi..."):
            errors, warnings = preflight_check.validate_strategy()

        if warnings:
            st.warning(f"⚠️ {len(warnings)} saran perbaikan (bot tetap bisa jalan):")
            for i, w in enumerate(warnings, 1):
                with st.expander(f"Saran #{i}", expanded=False):
                    st.code(w, language="text")

        if not errors:
            st.success("✅ strategy.py **tidak memiliki error fatal**. Siap dijalankan.")
        else:
            st.error(f"❌ Ditemukan **{len(errors)} masalah fatal**:")
            for i, err in enumerate(errors, 1):
                with st.expander(f"Masalah #{i}", expanded=True):
                    st.code(err, language="text")

st.divider()
st.info("""
**Catatan:**
- Bot dijalankan sebagai *background process* terpisah dari Streamlit.
- Log bot tetap muncul di terminal/file log `main.py`, bukan di sini.
- Jika bot dikembalikan ke kondisi mati dari luar (Ctrl+C di terminal), tekan **Stop Bot** agar status di-reset.
- Tombol **Start Bot** akan otomatis memvalidasi `strategy.py` terlebih dahulu sebelum bot dinyalakan.
""")

