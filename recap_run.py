# =============================================================================
# recap_run.py — Demo Run Recorder & Recap (cek modal awal → rekap setelah N menit)
# =============================================================================
# Tujuan: jadi OBSERVER INDEPENDEN saat bot jalan di mode demo. Skrip ini:
#   1. Cek "modal awal" (balance) + posisi di Bybit demo saat start (T=0).
#   2. Snapshot balance + posisi tiap INTERVAL detik selama durasi yang diminta.
#   3. Setelah durasi habis → rekap: delta modal, posisi, deteksi DUPLICATE FIRE
#      (via ground-truth `contracts` Bybit, bukan klaim bot), + scan log bot
#      untuk error / pola order kembar.
#
# Data sumber kebenaran = Bybit demo (fetch_balance + fetch_positions langsung),
# JADI tidak bergantung pada state internal bot. Reuse mesin snapshot dari
# verify_collector.py.
#
# CATATAN: di mode demo/live bot TIDAK menulis trade_history.csv (log_trade cuma
# dipanggil di paper mode). Jadi rekap mengandalkan snapshot Bybit + log stdout.
#
# Jalankan di TERMINAL TERPISAH dari bot:
#   python recap_run.py --minutes 60                 # rekap 1 jam
#   python recap_run.py --minutes 60 --interval 30   # snapshot tiap 30s
#   python recap_run.py --once                       # 1x snapshot (tes koneksi)
# =============================================================================
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# Paksa output UTF-8 di terminal Windows (cegah UnicodeEncodeError dari emoji/box char
# di report rekap). Sama pattern dengan main.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# engine/ ke sys.path (sama pattern main.py / verify_collector)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))

# Reuse mesin exchange + snapshot dari verify_collector (DRY).
import verify_collector as vc
from config import ORDER_SIZE_USDT, LEVERAGE, SYMBOL

LOGS_DIR = "logs"
SNAP_FILE = os.path.join(LOGS_DIR, "recap_snapshots.jsonl")
EQUITY_CSV = os.path.join(LOGS_DIR, "equity_curve.csv")
# Log stdout bot — default ke bot_run.log (di-redirect saat launch). Fallback bot.log.
BOT_LOG_CANDIDATES = [
    os.path.join(LOGS_DIR, "bot_run.log"),
    os.path.join(LOGS_DIR, "bot.log"),
]

# Ambang deteksi: posisi dianggap "stacking/duplicate" kalau contracts > faktor ini
# dikali ukuran 1 order normal. 1.5 = aman dari noise rounding, tegas ke 2x stack.
STACK_RATIO_THRESHOLD = 1.5

os.makedirs(LOGS_DIR, exist_ok=True)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x, default=0.0) -> float:
    """Coerce ke float dengan aman (Bybit kadang return string/None)."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _expected_single_qty(price: float) -> float:
    """Ukuran 1 order normal (XRP) = (ORDER_SIZE_USDT * LEVERAGE) / harga."""
    return (ORDER_SIZE_USDT * LEVERAGE) / price if price > 0 else 0.0


# =============================================================================
# SNAPSHOT
# =============================================================================
async def _take(ex, sym: str) -> dict:
    snap = await vc._snapshot(ex, sym)
    return snap


def _append_snap(snap: dict) -> None:
    with open(SNAP_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap) + "\n")
        f.flush()


def _summarize_pos(snap: dict) -> str:
    pos = snap.get("positions", [])
    if not pos:
        return "FLAT (tidak ada posisi)"
    parts = []
    for p in pos:
        parts.append(
            f"{p.get('side')} {p.get('contracts')} @ {p.get('entryPrice')} "
            f"(uPnL={p.get('unrealizedPnl')})"
        )
    return " | ".join(parts)


# =============================================================================
# ANALISIS LOG BOT
# =============================================================================
def _find_bot_log() -> str | None:
    for c in BOT_LOG_CANDIDATES:
        if os.path.exists(c) and os.path.getsize(c) > 0:
            return c
    return None


def _analyze_bot_log(path: str | None) -> dict:
    """
    Scan log stdout bot untuk pola kunci. Mengembalikan dict statistik +
    deteksi duplicate-fire dari urutan [EXECUTE] event.
    """
    stats = {
        "log_file": path,
        "execute_buy": 0,
        "execute_sell": 0,
        "execute_close": 0,
        "order_placed": 0,          # [LIVE] Order placed (dispatch sukses)
        "inflight_skips": 0,        # guard mencegah dispatch kembar
        "optimistic_set": 0,
        "optimistic_clear": 0,
        "reject_110017": 0,
        "silent_reject": 0,
        "partial_fill": 0,
        "errors": 0,
        "critical": 0,
        "supervisor_crash": 0,
        "consecutive_entry_dupes": 0,  # 2+ entry berturut tanpa close di antaranya
        "error_samples": [],
    }
    if not path:
        return stats

    execute_seq: list[str] = []  # urutan 'buy'/'sell'/'close'
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[EXECUTE] BUY" in line:
                    stats["execute_buy"] += 1
                    execute_seq.append("buy")
                elif "[EXECUTE] SELL" in line:
                    stats["execute_sell"] += 1
                    execute_seq.append("sell")
                elif "[EXECUTE] CLOSE" in line:
                    stats["execute_close"] += 1
                    execute_seq.append("close")

                if "[LIVE] Order placed:" in line:
                    stats["order_placed"] += 1
                if "di-skip" in line and "in-flight" in line:
                    stats["inflight_skips"] += 1
                if "[OPTIMISTIC] Position set" in line:
                    stats["optimistic_set"] += 1
                if "[OPTIMISTIC] Position cleared" in line:
                    stats["optimistic_clear"] += 1
                if "110017" in line:
                    stats["reject_110017"] += 1
                if "SILENT REJECT" in line:
                    stats["silent_reject"] += 1
                if "PARTIAL FILL" in line:
                    stats["partial_fill"] += 1
                if "CRASHED" in line:
                    stats["supervisor_crash"] += 1

                # Level error/critical (format basicConfig: "... LEVEL: ...")
                if re.search(r"\bCRITICAL\b", line):
                    stats["critical"] += 1
                    if len(stats["error_samples"]) < 10:
                        stats["error_samples"].append(line.strip()[:200])
                elif re.search(r"\bERROR\b", line):
                    stats["errors"] += 1
                    if len(stats["error_samples"]) < 10:
                        stats["error_samples"].append(line.strip()[:200])
    except Exception as e:
        stats["error_samples"].append(f"[recap] gagal baca log: {e}")
        return stats

    # Deteksi duplicate entry: dua entry (buy/sell) berturut TANPA close di antara.
    prev_entry = False
    for ev in execute_seq:
        if ev in ("buy", "sell"):
            if prev_entry:
                stats["consecutive_entry_dupes"] += 1
            prev_entry = True
        elif ev == "close":
            prev_entry = False
    return stats


# =============================================================================
# ANALISIS SNAPSHOT (ground truth Bybit)
# =============================================================================
def _analyze_snaps(snaps: list[dict]) -> dict:
    """
    Segmentasi snapshot jadi 'episode' (run kontigua saat ada posisi, dipisah flat).
    Deteksi stacking dilakukan PER EPISODE dan HANYA untuk episode yang dibuka
    SELAMA observasi (started_flat=True). Episode pertama yang sudah ada posisi sejak
    snapshot[0] diperlakukan sebagai LEFTOVER pra-eksisting (di luar scope tes).
    """
    valid = [s for s in snaps if s.get("balance_usdt") is not None]
    balances = [_f(s["balance_usdt"]) for s in valid]
    fetch_errors = sum(1 for s in snaps if s.get("errors"))

    # Time series total contracts + harga + side dominan
    series = []
    for s in snaps:
        pos = s.get("positions", [])
        total_c = sum(_f(p.get("contracts")) for p in pos)
        price = 0.0
        side = None
        if pos:
            price = _f(pos[0].get("entryPrice")) or _f(pos[0].get("markPrice"))
            side = pos[0].get("side")
        series.append({"contracts": total_c, "price": price, "side": side})

    # Harga acuan global untuk hitung "1 order normal" — pakai harga non-zero TERAKHIR.
    # Robust terhadap transient entryPrice=None di satu snapshot (yang sebelumnya bikin
    # "1 order normal ~0.0000"). Selama bot pernah punya posisi dgn harga valid, acuan ada.
    nonzero_prices = [pt["price"] for pt in series if pt["price"] > 1e-9]
    ref_price = nonzero_prices[-1] if nonzero_prices else 0.0
    expected_single = _expected_single_qty(ref_price)

    # Bangun episode
    episodes: list[dict] = []
    cur: dict | None = None
    seen_flat = False
    for pt in series:
        if pt["contracts"] > 1e-9:
            if cur is None:
                cur = {"max_contracts": pt["contracts"], "price": pt["price"],
                       "side": pt["side"], "started_flat": seen_flat}
            else:
                if pt["contracts"] > cur["max_contracts"]:
                    cur["max_contracts"] = pt["contracts"]
            # Tangkap harga valid kapan pun muncul (jangan nyangkut di transient null
            # yg kebetulan ada di snapshot pertama episode).
            if cur["price"] <= 1e-9 and pt["price"] > 1e-9:
                cur["price"] = pt["price"]
        else:
            seen_flat = True
            if cur is not None:
                episodes.append(cur)
                cur = None
    if cur is not None:
        episodes.append(cur)

    new_episodes = [e for e in episodes if e["started_flat"]]      # dibuka saat observasi
    preexisting = [e for e in episodes if not e["started_flat"]]   # leftover run lama

    # Stacking hanya dinilai untuk episode baru. Pakai harga episode; kalau transient
    # null (0), fallback ke harga acuan global agar ratio tetap terhitung.
    worst_ratio = 0.0
    stack_new = 0
    worst_ep = None
    for e in new_episodes:
        exp = _expected_single_qty(e["price"]) or expected_single
        r = (e["max_contracts"] / exp) if exp > 0 else 0.0
        if r > worst_ratio:
            worst_ratio = r
            worst_ep = e
        if r > STACK_RATIO_THRESHOLD:
            stack_new += 1

    overall_max = max((e["max_contracts"] for e in episodes), default=0.0)
    leftover_contracts = preexisting[0]["max_contracts"] if preexisting else 0.0

    # "1 order normal" untuk worst episode — pakai harga episode bila valid, jika tidak
    # pakai acuan global (tidak pernah lagi tampil 0.0000 selama ada posisi).
    worst_single = (_expected_single_qty(worst_ep["price"]) if worst_ep else 0.0) or expected_single

    return {
        "snapshots_total": len(snaps),
        "snapshots_valid": len(valid),
        "fetch_errors": fetch_errors,
        "balance_start": balances[0] if balances else None,
        "balance_end": balances[-1] if balances else None,
        "balance_min": min(balances) if balances else None,
        "balance_max": max(balances) if balances else None,
        "overall_max_contracts": overall_max,
        "leftover_contracts": leftover_contracts,
        "new_episodes": len(new_episodes),
        "preexisting_episodes": len(preexisting),
        "worst_new_ratio": worst_ratio,
        "worst_new_max_contracts": worst_ep["max_contracts"] if worst_ep else 0.0,
        "worst_new_expected_single": worst_single,
        "stack_new_episodes": stack_new,
        "reference_price": ref_price,
        "expected_single": expected_single,
    }


# =============================================================================
# BUILD RECAP
# =============================================================================
def _build_recap(start_snap, end_snap, snaps, started_at_epoch, minutes, interval=30.0) -> str:
    snap_stats = _analyze_snaps(snaps)
    log_stats = _analyze_bot_log(_find_bot_log())

    bstart = snap_stats["balance_start"]
    bend = snap_stats["balance_end"]
    delta = (bend - bstart) if (bstart is not None and bend is not None) else None

    # ---- Verdict duplicate-fire (HANYA nilai episode yang dibuka saat observasi) ----
    new_eps = snap_stats["new_episodes"]
    wratio = snap_stats["worst_new_ratio"]
    stacking_new = snap_stats["stack_new_episodes"] > 0 or wratio > STACK_RATIO_THRESHOLD
    stacking_log = log_stats["consecutive_entry_dupes"] > 0
    # Entry dari LOG = ground-truth fine-grained (tahan flip cepat yg luput dari snapshot).
    log_entries = log_stats["execute_buy"] + log_stats["execute_sell"]

    verdict_lines = []
    if snap_stats["preexisting_episodes"] > 0:
        verdict_lines.append(
            f"⚪ Leftover pra-eksisting: posisi {snap_stats['leftover_contracts']:.4f} contracts "
            f"sudah terbuka sejak awal observasi (sisa run sebelumnya — DI LUAR scope tes fix)."
        )

    if new_eps == 0 and log_entries > 0:
        # Snapshot buta terhadap flip cepat: posisi flip < interval, jadi tak pernah
        # tertangkap momen flat→posisi. Bukan berarti bot diam — LOG yg jadi acuan.
        dup_log = "🔴 ADA" if stacking_log else "🟢 TIDAK ADA"
        verdict_lines.append(
            f"⚪ Snapshot (interval {interval:.0f}s) tidak menangkap episode flat→posisi: "
            f"posisi flip lebih cepat dari interval. Ini BUKAN tanda bot diam — "
            f"LOG mencatat {log_entries} entry ({log_stats['execute_buy']} buy / "
            f"{log_stats['execute_sell']} sell), {log_stats['optimistic_set']} optimistic-set. "
            f"Duplicate-fire dinilai dari urutan entry log → entry beruntun tanpa close: "
            f"{log_stats['consecutive_entry_dupes']} ({dup_log})."
        )
    elif new_eps == 0:
        verdict_lines.append(
            "⚪ Bot TIDAK membuka posisi baru selama observasi (MA mungkin tidak cross / "
            "masih mengelola leftover) DAN log tidak mencatat entry. Tidak ada episode "
            "baru untuk uji duplicate-fire."
        )
    elif stacking_new:
        verdict_lines.append(
            f"🔴 DUPLICATE-FIRE TERDETEKSI di {snap_stats['stack_new_episodes']} episode baru: "
            f"max {snap_stats['worst_new_max_contracts']:.4f} contracts = {wratio:.2f}× ukuran "
            f"1 order normal ({snap_stats['worst_new_expected_single']:.4f}). Fix BELUM efektif."
        )
    else:
        verdict_lines.append(
            f"🟢 TIDAK ADA STACKING di {new_eps} episode baru: worst-case "
            f"{snap_stats['worst_new_max_contracts']:.4f} contracts ≈ {wratio:.2f}× ukuran "
            f"1 order normal ({snap_stats['worst_new_expected_single']:.4f}). Posisi tunggal — "
            f"duplicate-fire tidak terjadi di ground-truth Bybit."
        )

    if log_stats["inflight_skips"] > 0:
        verdict_lines.append(
            f"🟢 In-flight guard AKTIF: mencegah {log_stats['inflight_skips']}× dispatch order kembar."
        )
    if stacking_log:
        verdict_lines.append(
            f"🟠 Log menunjukkan {log_stats['consecutive_entry_dupes']}× entry beruntun tanpa close "
            f"di antaranya — cek apakah ini ter-mitigasi guard (lihat in-flight skips)."
        )
    if log_stats["critical"] > 0 or log_stats["supervisor_crash"] > 0:
        verdict_lines.append(
            f"🔴 STABILITAS: {log_stats['critical']} CRITICAL, "
            f"{log_stats['supervisor_crash']} task crash terdeteksi di log."
        )
    elif log_stats["errors"] > 0:
        verdict_lines.append(
            f"🟠 {log_stats['errors']} baris ERROR di log (cek sample di bawah — "
            f"sebagian mungkin transient/expected)."
        )
    else:
        verdict_lines.append("🟢 Tidak ada ERROR/CRITICAL di log bot.")

    dur_actual = (time.time() - started_at_epoch) / 60.0

    lines = []
    lines.append(f"# REKAP RUN DEMO — {SYMBOL}")
    lines.append("")
    lines.append(f"- Mulai (UTC)      : {start_snap.get('timestamp')}")
    lines.append(f"- Selesai (UTC)    : {end_snap.get('timestamp')}")
    lines.append(f"- Durasi diminta   : {minutes:.0f} menit")
    lines.append(f"- Durasi aktual    : {dur_actual:.1f} menit")
    lines.append("")
    lines.append("## MODAL (balance USDT — ground truth Bybit demo)")
    lines.append(f"- Awal   : {bstart}")
    lines.append(f"- Akhir  : {bend}")
    lines.append(f"- Delta  : {('%+.4f' % delta) if delta is not None else 'n/a'}")
    lines.append(f"- Min/Max: {snap_stats['balance_min']} / {snap_stats['balance_max']}")
    lines.append("")
    lines.append("## POSISI")
    lines.append(f"- Posisi awal          : {_summarize_pos(start_snap)}")
    lines.append(f"- Posisi akhir         : {_summarize_pos(end_snap)}")
    lines.append(f"- Leftover pra-eksisting: {snap_stats['leftover_contracts']:.4f} contracts "
                 f"({snap_stats['preexisting_episodes']} episode, di luar scope)")
    lines.append(f"- Episode baru (snapshot): {snap_stats['new_episodes']} "
                 f"(flat→posisi tertangkap snapshot — bisa 0 jika flip < interval)")
    lines.append(f"- Entry dari log (tes)  : {log_entries} "
                 f"({log_stats['execute_buy']} buy / {log_stats['execute_sell']} sell) "
                 f"— ground-truth fine-grained, tahan flip cepat")
    lines.append(f"- 1 order normal       : ~{snap_stats['expected_single']:.4f} XRP "
                 f"@ ref {snap_stats['reference_price']:.4f} "
                 f"(ORDER_SIZE_USDT={ORDER_SIZE_USDT} × LEV={LEVERAGE})")
    lines.append(f"- Worst episode baru   : {snap_stats['worst_new_max_contracts']:.4f} contracts "
                 f"= {wratio:.2f}× normal (ambang stacking > {STACK_RATIO_THRESHOLD}×)")
    lines.append(f"- Overall max contracts: {snap_stats['overall_max_contracts']:.4f} "
                 f"(termasuk leftover)")
    lines.append("")
    lines.append("## LOG BOT (stdout)")
    lines.append(f"- File             : {log_stats['log_file']}")
    lines.append(f"- [EXECUTE] BUY/SELL/CLOSE : "
                 f"{log_stats['execute_buy']}/{log_stats['execute_sell']}/{log_stats['execute_close']}")
    lines.append(f"- Order placed (OK dispatch): {log_stats['order_placed']}")
    lines.append(f"- In-flight skips (guard)   : {log_stats['inflight_skips']}")
    lines.append(f"- Optimistic set/clear      : {log_stats['optimistic_set']}/{log_stats['optimistic_clear']}")
    lines.append(f"- Reject 110017             : {log_stats['reject_110017']}")
    lines.append(f"- Silent reject / partial   : {log_stats['silent_reject']}/{log_stats['partial_fill']}")
    lines.append(f"- ERROR / CRITICAL / crash  : "
                 f"{log_stats['errors']}/{log_stats['critical']}/{log_stats['supervisor_crash']}")
    lines.append(f"- Entry beruntun (log)      : {log_stats['consecutive_entry_dupes']}")
    lines.append("")
    lines.append("## SNAPSHOT")
    lines.append(f"- Total / valid    : {snap_stats['snapshots_total']} / {snap_stats['snapshots_valid']}")
    lines.append(f"- Fetch errors     : {snap_stats['fetch_errors']}")
    lines.append("")
    lines.append("## VERDICT")
    for v in verdict_lines:
        lines.append(f"- {v}")
    lines.append("")
    if log_stats["error_samples"]:
        lines.append("## SAMPLE ERROR/CRITICAL (maks 10)")
        for s in log_stats["error_samples"]:
            lines.append(f"- `{s}`")
        lines.append("")
    return "\n".join(lines)


# =============================================================================
# RUN
# =============================================================================
async def _run(minutes: float, interval: float) -> None:
    ex = vc._build_exchange()
    try:
        sym = await vc._resolve_symbol(ex)
        started = time.time()

        # ---- T=0 ----
        start_snap = await _take(ex, sym)
        _append_snap({"_recap_session_start": _iso_now(), "symbol": sym,
                      "minutes": minutes, "interval": interval})
        _append_snap(start_snap)

        print("=" * 64)
        print(f"[RECAP] START — {sym}")
        print(f"[RECAP] MODAL AWAL : {start_snap.get('balance_usdt')} USDT")
        print(f"[RECAP] POSISI AWAL: {_summarize_pos(start_snap)}")
        print(f"[RECAP] Durasi {minutes:.0f} menit, snapshot tiap {interval:.0f}s")
        if start_snap.get("errors"):
            print(f"[RECAP] ⚠ Error fetch awal: {start_snap['errors']}")
        print("=" * 64)

        snaps = [start_snap]
        end_t = started + minutes * 60.0
        while time.time() < end_t:
            remaining = end_t - time.time()
            await asyncio.sleep(min(interval, max(1.0, remaining)))
            snap = await _take(ex, sym)
            snaps.append(snap)
            _append_snap(snap)
            elapsed_min = (time.time() - started) / 60.0
            err = f" | ERR:{snap['errors'][0][:50]}" if snap.get("errors") else ""
            print(f"[{elapsed_min:5.1f}m] balance={snap.get('balance_usdt')} "
                  f"pos={_summarize_pos(snap)}{err}")

        end_snap = snaps[-1]
        recap = _build_recap(start_snap, end_snap, snaps, started, minutes, interval)

        out_path = os.path.join(LOGS_DIR, f"recap_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(recap)

        print("\n" + recap)
        print(f"\n[RECAP] Tersimpan: {out_path}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def _run_once() -> None:
    """Tes koneksi: 1 snapshot lalu exit."""
    ex = vc._build_exchange()
    try:
        sym = await vc._resolve_symbol(ex)
        snap = await _take(ex, sym)
        print(f"[RECAP --once] {sym}")
        print(f"  balance : {snap.get('balance_usdt')} USDT")
        print(f"  posisi  : {_summarize_pos(snap)}")
        if snap.get("errors"):
            print(f"  ERRORS  : {snap['errors']}")
        else:
            print("  ✓ koneksi & fetch OK")
    finally:
        try:
            await ex.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Recap run demo bot.")
    ap.add_argument("--minutes", type=float, default=60.0, help="durasi observasi (menit)")
    ap.add_argument("--interval", type=float, default=30.0, help="interval snapshot (detik)")
    ap.add_argument("--once", action="store_true", help="1x snapshot lalu exit (tes koneksi)")
    args = ap.parse_args()

    try:
        if args.once:
            asyncio.run(_run_once())
        else:
            asyncio.run(_run(args.minutes, args.interval))
    except KeyboardInterrupt:
        print("\n[RECAP] Dihentikan user.")


if __name__ == "__main__":
    main()
