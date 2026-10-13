"""Local read-only dashboard. It does not start the bot.

`python dashboard.py` serves the HTML panel on this machine, port 1337.
render() remains the previous Streamlit view. The matching Streamlit
config is .streamlit/config.toml. Do not start both servers at once.
"""

import sys
from datetime import datetime, timedelta, timezone

from reporting.dashboard_data import default_paths, feedback_view, load_overview, report_view, risk_view, transfer_rows


BIND_HOST = "127.0.0.1"
BIND_PORT = 1337
_PAGES = ("Genel bakış", "Transferler", "Risk ve tespit", "Geri bildirim", "İstatistikler")


def render() -> None:
    import streamlit as st

    st.set_page_config(page_title="Anti-Flash Token", layout="wide")
    st.title("Anti-Flash Token")
    st.caption(f"Salt okunur yerel panel · http://{BIND_HOST}:{BIND_PORT} · veriler UTC")
    paths = default_paths()
    page = st.sidebar.radio("Sayfa", _PAGES)
    if page == "Genel bakış":
        _overview(st, paths)
    elif page == "Transferler":
        _transfers(st, paths)
    elif page == "Risk ve tespit":
        _risk(st, paths)
    elif page == "Geri bildirim":
        _feedback(st, paths)
    else:
        _reports(st, paths)


def _overview(st, paths) -> None:
    data = load_overview(paths)
    for note in data.notes:
        st.info(note)
    columns = st.columns(4)
    columns[0].metric("İzlenen cüzdan", _number(data.wallet_count))
    columns[1].metric("Kayıtlı transfer", _number(data.transfer_count))
    columns[2].metric("Son 24 saat", _number(data.transfers_24h))
    columns[3].metric("Son 7 gün", _number(data.transfers_7d))
    left, right = st.columns(2)
    with left:
        st.subheader("Sınıflandırma")
        _pairs(st, data.by_classification, "Sınıflandırma dağılımı bu şemada yok.")
    with right:
        st.subheader("Risk seviyesi")
        _pairs(st, data.by_risk_level, "Risk dağılımı bu şemada yok.")
    st.subheader("Son transferler")
    _table(st, data.recent_transfers, "Kayıtlı transfer yok.")
    st.subheader("Son tespitler")
    _table(st, data.recent_detections, "Kayıtlı tespit yok.")


def _transfers(st, paths) -> None:
    start, end = _range(st)
    network = st.selectbox("Ağ", ("Tümü", "ethereum", "tron"))
    classification = st.selectbox("Sınıflandırma", ("Tümü", "CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR"))
    detection = st.selectbox("Tespit türü", ("Tümü", "transfer", "poisoning", "approval"))
    risk = st.selectbox("Risk seviyesi", ("Tümü", "DÜŞÜK", "ORTA", "YÜKSEK", "KRİTİK"))
    if start >= end:
        st.warning("Başlangıç, bitişten önce olmalı. Bitiş anı dahil değildir.")
        return
    rows, truncated = transfer_rows(
        paths,
        start=start,
        end=end,
        network=None if network == "Tümü" else network,
        classification=None if classification == "Tümü" else classification,
        detection=None if detection == "Tümü" else detection,
        risk_level=None if risk == "Tümü" else risk,
    )
    if truncated:
        st.info("Sonuçlar sınırlandı. Aralığı daraltırsanız kalan kayıtlar da sorgulanır.")
    _table(st, rows, "Bu süzgeçte kayıt yok.")


def _risk(st, paths) -> None:
    start, end = _range(st)
    if start >= end:
        st.warning("Başlangıç, bitişten önce olmalı. Bitiş anı dahil değildir.")
        return
    view = risk_view(paths, start=start, end=end)
    if not view["available"]:
        st.info("Tespit tablosu yok. Address poisoning ve approval verisi gösterilmiyor.")
        return
    st.subheader("Risk seviyeleri")
    _pairs(st, view["by_level"], "Bu aralıkta risk seviyesi yok.")
    st.subheader("Güne göre risk seviyesi")
    _table(st, view["by_day"], "Bu aralıkta risk kaydı yok.")
    st.subheader("Address poisoning")
    _table(st, view["poisoning"], "Bu aralıkta address poisoning kaydı yok.")
    st.subheader("Approval")
    _table(st, view["approvals"], "Bu aralıkta approval kaydı yok.")
    st.subheader("Sinyaller")
    _table(st, view["signals"], "Bu aralıkta sinyal kaydı yok.")


def _feedback(st, paths) -> None:
    start, end = _range(st)
    if start >= end:
        st.warning("Başlangıç, bitişten önce olmalı. Bitiş anı dahil değildir.")
        return
    view = feedback_view(paths, start=start, end=end)
    if not view["available"]:
        st.info("Geri bildirim tablosu yok.")
        return
    counts = view["counts"]
    columns = st.columns(3)
    columns[0].metric("CORRECT", counts.get("CORRECT", 0))
    columns[1].metric("FALSE_POSITIVE", counts.get("FALSE_POSITIVE", 0))
    columns[2].metric("UNSURE", counts.get("UNSURE", 0))
    st.metric("Etiketli örnek", view["labeled"])
    rate = view["false_alarm"]
    if rate in {None, "N/A", "Yeterli etiketli veri yok"}:
        st.info(f"Yanlış alarm oranı: {rate or 'N/A'}. UNSURE ve etiketsiz tespitler paydaya girmez.")
    else:
        st.metric("Yanlış alarm oranı", rate)
    st.subheader("Sınıflandırma ve geri bildirim")
    _table(st, view["by_classification"], "Bu aralıkta geri bildirim yok.")


def _reports(st, paths) -> None:
    start, end = _range(st)
    if start >= end:
        st.warning("Başlangıç, bitişten önce olmalı. Bitiş anı dahil değildir.")
        return
    view = report_view(paths, start=start, end=end)
    for note in view["notes"]:
        st.info(note)
    st.subheader("Günlük transfer")
    _table(st, view["daily_transfers"], "Bu aralıkta transfer yok.")
    st.subheader("Günlük tespit")
    _table(st, view["daily_detections"], "Bu aralıkta tespit yok.")
    st.subheader("Sınıflandırma")
    _pairs(st, view["by_classification"], "Bu aralıkta sınıflandırma yok.")
    st.subheader("Geri bildirim")
    if view["feedback"]:
        _table(st, [{"feedback": name, "count": count} for name, count in sorted(view["feedback"].items())], "")
    else:
        st.caption("Bu aralıkta geri bildirim yok.")
    st.subheader("Haftalık gönderimler")
    st.caption("Kullanıcı kimliği gösterilmez. Sayı, gönderim kaydıdır.")
    _table(st, view["weekly"], "Haftalık gönderim kaydı yok.")
    st.subheader("Genel sayaçlar")
    st.caption("stats_daily tüm kurulum içindir. Bu sayılar cüzdan bazında ayrılmaz; hata ve /check sayaçları transfer sayısı değildir.")
    _table(st, view["global_metrics"], "Bu aralıkta stats_daily kaydı yok.")


def _range(st) -> tuple[datetime, datetime]:
    today = datetime.now(timezone.utc).date()
    picked = st.date_input(
        "Tarih aralığı (UTC, bitiş hariç)",
        value=(today - timedelta(days=7), today),
    )
    if isinstance(picked, tuple) and len(picked) == 2 and picked[0] and picked[1]:
        start_day, end_day = picked
    else:
        start_day = end_day = today
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=timezone.utc)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)
    return start, end


def _number(value) -> str:
    return "yok" if value is None else str(value)


def _pairs(st, rows, empty: str) -> None:
    if rows is None:
        st.info(empty)
        return
    if not rows:
        st.caption("Kayıt yok.")
        return
    _table(st, [{"ad": name, "sayı": count} for name, count in rows], empty)


def _table(st, rows, empty: str) -> None:
    if not rows:
        if empty:
            st.caption(empty)
        return
    st.dataframe(list(rows), hide_index=True, width="stretch")


def main() -> None:
    """Serve the HTML dashboard. This does not start the bot."""
    from web_server import serve

    serve(BIND_HOST, BIND_PORT)


if __name__ == "__main__":
    if "streamlit.runtime.scriptrunner" in sys.modules:
        render()
    else:
        main()
