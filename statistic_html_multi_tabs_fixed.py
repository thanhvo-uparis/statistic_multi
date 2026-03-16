from __future__ import annotations

import re
from io import BytesIO

import pandas as pd
import plotly.express as px
import streamlit as st
from bs4 import BeautifulSoup

# ============================================================
# CONFIG
# ============================================================
REF_COL = "Référence Administrative de la demande"

TITLE_CLEAN_RE = re.compile(r"[\u00bb\u2022]")
SPACE_RE = re.compile(r"\s+")
REF_RE = re.compile(r"\(([^)]+)\)")
RECNO_RE = re.compile(r"Enregistrement n°\s*(\d+)")

IGNORE_MSG = "A relancer après correction"


# ============================================================
# UTILS
# ============================================================
def clean_title(raw: str) -> str:
    raw = TITLE_CLEAN_RE.sub("", raw)
    raw = SPACE_RE.sub(" ", raw).strip()
    return raw


def is_result_csv_badge(span_text: str) -> bool:
    t = (span_text or "").strip()
    return t.startswith("Résultat") and t.endswith(".csv")


def _soup_from_bytes(html_bytes: bytes) -> BeautifulSoup:
    return BeautifulSoup(html_bytes, "lxml")


# ============================================================
# LOADERS
# ============================================================
@st.cache_data(show_spinner=False)
def load_excel_from_bytes(excel_bytes: bytes) -> pd.DataFrame:
    df = pd.read_excel(BytesIO(excel_bytes))

    if REF_COL not in df.columns:
        raise ValueError(
            f"Excel missing expected column: '{REF_COL}'. Found: {list(df.columns)}"
        )

    df[REF_COL] = df[REF_COL].astype(str).str.strip()
    return df


@st.cache_data(show_spinner=False)
def extract_header_info(html_bytes: bytes) -> dict:
    soup = _soup_from_bytes(html_bytes)

    verification_type = None
    for tr in soup.select("tr"):
        td_label = tr.find("td", class_="normal")
        td_val = tr.find("td", class_="normalcode")
        if not (td_label and td_val):
            continue
        if td_label.get_text(" ", strip=True) == "Type de vérification":
            verification_type = td_val.get_text(" ", strip=True)
            break

    chargement_value = None
    h2s = soup.find_all(
        "h2",
        class_=lambda c: c and "titresection1" in c and "trigger" in c.split(),
    )

    target_h2 = None
    for h2 in h2s:
        title = clean_title(h2.get_text(" ", strip=True))
        if "Chargement des fichiers" in title:
            target_h2 = h2
            break

    if target_h2:
        sib = target_h2.find_next_sibling()
        while sib and getattr(sib, "name", None) is None:
            sib = sib.next_sibling

        if sib:
            first_val = sib.find("td", class_="normalcode")
            if first_val:
                chargement_value = first_val.get_text(" ", strip=True)

    return {
        "verification_type": verification_type,
        "chargement_value": chargement_value,
    }


@st.cache_data(show_spinner=False)
def parse_audit_html(html_bytes: bytes) -> pd.DataFrame:
    soup = _soup_from_bytes(html_bytes)
    rows: list[dict] = []

    h2s = soup.find_all(
        "h2",
        class_=lambda c: c and "titresection1" in c and "trigger" in c.split(),
    )

    for h2 in h2s:
        span = h2.find("span", class_="echec")
        if not span:
            continue

        if is_result_csv_badge(span.get_text(" ", strip=True)):
            continue

        section = clean_title(h2.get_text(" ", strip=True))

        sib = h2.find_next_sibling()
        while sib and getattr(sib, "name", None) is None:
            sib = sib.next_sibling

        table = sib.find("table", class_="datas") if sib else None
        if not table:
            continue

        for tr in table.select("tbody tr"):
            td_echec = tr.find("td", class_="echec")
            td_msg = tr.find("td", class_="echecode")

            if not (td_echec and td_msg):
                continue

            msg = td_msg.get_text(" ", strip=True)
            if msg.strip() == IGNORE_MSG:
                continue

            txt = td_echec.get_text(" ", strip=True)

            mref = REF_RE.search(txt)
            reference = mref.group(1).strip() if mref else None

            mrec = RECNO_RE.search(txt)
            record_no = int(mrec.group(1)) if mrec else None

            rows.append(
                {
                    "section": section,
                    "echecode": msg,
                    "reference": reference,
                    "record_no": record_no,
                }
            )

    return pd.DataFrame(rows, columns=["section", "echecode", "reference", "record_no"])


# ============================================================
# CHART
# ============================================================
def build_pie(section_df: pd.DataFrame, section_name: str):
    counts = (
        section_df.groupby("echecode", as_index=False)
        .size()
        .rename(columns={"size": "count"})
        .sort_values("count", ascending=False)
    )

    fig = px.pie(
        counts,
        names="echecode",
        values="count",
        title=section_name,
        hole=0.0,
    )
    fig.update_traces(
        hovertemplate="<b>%{label}</b><br>count=%{value}<extra></extra>",
        textinfo="percent",
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=50, b=10),
        legend_title_text="Message erreur",
    )
    return fig, counts


# ============================================================
# RENDER ONE HTML RESULT INSIDE ONE TAB
# ============================================================
def render_one_audit_tab(tab_idx: int, html_name: str, audit_df: pd.DataFrame, excel_df: pd.DataFrame, header_info: dict):
    if audit_df.empty:
        st.error(f"Không parse được lỗi nào từ file HTML: {html_name}")
        return

    sections = sorted(audit_df["section"].dropna().unique().tolist())
    nb_sections_en_erreur = len(sections)

    st.markdown(f"## {html_name}")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Type de vérification", header_info.get("verification_type") or "—")
    with c2:
        st.metric("Chargement des fichiers", header_info.get("chargement_value") or "—")
    with c3:
        st.metric("Sections en erreur", nb_sections_en_erreur)

    state_prefix = f"tab_{tab_idx}"

    if f"{state_prefix}_selected_section" not in st.session_state:
        st.session_state[f"{state_prefix}_selected_section"] = sections[0] if sections else None
    if f"{state_prefix}_selected_echecode" not in st.session_state:
        st.session_state[f"{state_prefix}_selected_echecode"] = None
    if f"{state_prefix}_selected_references" not in st.session_state:
        st.session_state[f"{state_prefix}_selected_references"] = []
    if f"{state_prefix}_picked_section_prev" not in st.session_state:
        st.session_state[f"{state_prefix}_picked_section_prev"] = "(TOUS)"

    picked_section = st.selectbox(
        f"Filtrer en fonction de la section - {html_name}",
        options=["(TOUS)"] + sections,
        index=0,
        key=f"{state_prefix}_section_selectbox",
    )

    if picked_section != st.session_state[f"{state_prefix}_picked_section_prev"]:
        st.session_state[f"{state_prefix}_selected_echecode"] = None
        st.session_state[f"{state_prefix}_selected_references"] = []
        st.session_state[f"{state_prefix}_picked_section_prev"] = picked_section

    sections_to_render = sections if picked_section == "(TOUS)" else [picked_section]

    st.divider()
    st.subheader("Diagramme")

    for sec in sections_to_render:
        sec_df = audit_df[audit_df["section"] == sec]
        fig, counts = build_pie(sec_df, sec)

        selection = st.plotly_chart(
            fig,
            use_container_width=True,
            on_select="rerun",
            selection_mode=("points",),
            key=f"{state_prefix}_pie_{sec}",
        )

        if selection and selection.get("points"):
            point_idx = selection["points"][0].get("point_index")
            if point_idx is not None and 0 <= point_idx < len(counts):
                chosen_msg = str(counts.iloc[point_idx]["echecode"])

                st.session_state[f"{state_prefix}_selected_section"] = sec
                st.session_state[f"{state_prefix}_selected_echecode"] = chosen_msg

                refs = (
                    audit_df[
                        (audit_df["section"] == sec)
                        & (audit_df["echecode"] == chosen_msg)
                    ]["reference"]
                    .dropna()
                    .astype(str)
                    .unique()
                    .tolist()
                )
                st.session_state[f"{state_prefix}_selected_references"] = refs

    st.divider()

    if picked_section == "(TOUS)":
        st.info("Chọn 1 section cụ thể để hiển thị bảng Excel.")
        return

    active_section_for_table = picked_section

    base_refs = (
        audit_df[audit_df["section"] == active_section_for_table]["reference"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    base_filtered = excel_df[excel_df[REF_COL].isin(base_refs)]

    ctrl_l, ctrl_r = st.columns([4, 1], vertical_alignment="center")
    with ctrl_l:
        st.caption(
            f"Section: {active_section_for_table} | lignes Excel: {len(base_filtered)}"
        )
        if (
            st.session_state[f"{state_prefix}_selected_echecode"]
            and st.session_state[f"{state_prefix}_selected_section"] == active_section_for_table
        ):
            st.caption(
                f"Message sélectionné: {st.session_state[f'{state_prefix}_selected_echecode']}"
            )

    with ctrl_r:
        if st.button("Reset message", use_container_width=True, key=f"{state_prefix}_reset_btn"):
            st.session_state[f"{state_prefix}_selected_echecode"] = None
            st.session_state[f"{state_prefix}_selected_references"] = []
            st.rerun()

    if (
        st.session_state[f"{state_prefix}_selected_echecode"]
        and st.session_state[f"{state_prefix}_selected_references"]
        and st.session_state[f"{state_prefix}_selected_section"] == active_section_for_table
    ):
        filtered = base_filtered[
            base_filtered[REF_COL].isin(st.session_state[f"{state_prefix}_selected_references"])
        ]
    else:
        filtered = base_filtered

    st.dataframe(filtered, use_container_width=True, height=560)


# ============================================================
# MAIN UI
# ============================================================
st.set_page_config(page_title="Statistiques multi HTML", layout="wide")
st.title("📊 Statistiques après audit EasyAudit - Multi HTML")

with st.sidebar:
    st.header("Upload sources")
    html_files = st.file_uploader(
        "Upload 3 fichiers HTML audit",
        type=["html"],
        accept_multiple_files=True,
    )
    excel_file = st.file_uploader("Upload Excel input (commun aux 3 HTML)", type=["xlsx"])

if not html_files or not excel_file:
    st.info("Veuillez télécharger 3 fichiers HTML et 1 fichier Excel pour commencer.")
    st.stop()

try:
    excel_bytes = excel_file.getvalue()
    excel_df = load_excel_from_bytes(excel_bytes)

    audits_data = []
    for html_file in html_files:
        html_bytes = html_file.getvalue()
        audit_df = parse_audit_html(html_bytes)
        header_info = extract_header_info(html_bytes)

        audits_data.append(
            {
                "html_name": html_file.name,
                "audit_df": audit_df,
                "header_info": header_info,
            }
        )
except Exception as e:
    st.error(f"Không thể đọc file: {e}")
    st.stop()

tab_labels = [item["html_name"] for item in audits_data]
tabs = st.tabs(tab_labels)

for idx, tab in enumerate(tabs):
    with tab:
        render_one_audit_tab(
            tab_idx=idx,
            html_name=audits_data[idx]["html_name"],
            audit_df=audits_data[idx]["audit_df"],
            excel_df=excel_df,
            header_info=audits_data[idx]["header_info"],
        )