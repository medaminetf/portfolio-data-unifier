from __future__ import annotations

import pandas as pd
import streamlit as st

from portfolio_builder import build_workbook, parse_market_file, with_ticker


st.set_page_config(page_title="Historical Portfolio Builder", page_icon="📈", layout="wide")

st.title("Historical Portfolio Builder")
st.caption(
    "Upload one CSV/Excel file per asset. The app cleans, aligns, audits, and exports "
    "a workbook compatible with the Portefeuille/Cours layout."
)

with st.expander("How missing dates are handled", expanded=False):
    st.markdown(
        """
        - The default calendar is the **union of observed trading dates** across uploaded assets.
          Weekends and market-wide holidays are therefore not created.
        - If one asset is missing on a date when another traded, the default treatment is to carry
          forward the asset's previous close. This represents a zero-return/stale-price day for
          portfolio valuation.
        - No value is backfilled before an asset starts or extrapolated after its last observation.
        - Every repair, flag, and imputed point is written to the `Qualite` sheet.
        """
    )

with st.sidebar:
    st.header("Processing options")

    dayfirst = st.checkbox(
        "Dates are day-first (DD/MM/YYYY)",
        value=True,
    )

    calendar_label = st.selectbox(
        "Alignment calendar",
        [
            "Union of observed dates (recommended)",
            "Only dates shared by every asset",
            "All Monday-Friday dates",
        ],
        index=0,
    )

    missing_label = st.selectbox(
        "Missing/non-trading values",
        [
            "Carry forward previous close (recommended)",
            "Log-price interpolation",
            "Leave missing",
        ],
        index=0,
    )

    normalize_weights = st.checkbox(
        "Normalize weights to 100%",
        value=True,
    )


calendar_mode = {
    "Union of observed dates (recommended)": "union",
    "Only dates shared by every asset": "intersection",
    "All Monday-Friday dates": "business_days",
}[calendar_label]
uploads = st.file_uploader(
    "Historical files",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
    help="Upload one file per asset.",
)

if not uploads:
    st.info("Upload at least one historical file to begin.")
    st.stop()


assets = []
errors = []

for uploaded in uploads:
    try:
        asset = parse_market_file(
            uploaded.name,
            uploaded.getvalue(),
            dayfirst=dayfirst,
            outlier_threshold=10.0,
            outlier_policy="flag",
        )
        assets.append(asset)

    except Exception as exc:
        errors.append((uploaded.name, str(exc)))


for file_name, message in errors:
    st.error(f"{file_name}: {message}")

if not assets:
    st.stop()
missing_method = {
    "Carry forward previous close (recommended)": "previous_close",
    "Log-price interpolation": "log_interpolation",
    "Leave missing": "leave_missing",
}[missing_label]
assets = []
errors = []

for uploaded in uploads:
    try:
        assets.append(
            parse_market_file(
                uploaded.name,
                uploaded.getvalue(),
                dayfirst=dayfirst,
                outlier_threshold=10.0,
                outlier_policy="flag",
            )
        )
    except Exception as exc:
        errors.append((uploaded.name, str(exc)))
outlier_policy = {
    "Flag only (recommended)": "flag",
    "Treat as missing before imputation": "replace",
}[outlier_label]

uploads = st.file_uploader(
    "Historical files",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
    help="Upload one file per asset. Date and closing-price columns are required.",
)

if not uploads:
    st.info("Upload at least one historical file to begin.")
    st.stop()

assets = []
errors = []
for uploaded in uploads:
    try:
        assets.append(
            parse_market_file(
                uploaded.name,
                uploaded.getvalue(),
                dayfirst=dayfirst,
                outlier_threshold=outlier_threshold,
                outlier_policy=outlier_policy,
            )
        )
    except Exception as exc:  # Streamlit should report a file-specific error and continue.
        errors.append((uploaded.name, str(exc)))

for file_name, message in errors:
    st.error(f"{file_name}: {message}")
if not assets:
    st.stop()

st.subheader("Portfolio configuration")
equal_weight = 100.0 / len(assets)
configuration = pd.DataFrame(
    {
        "File": [asset.file_name for asset in assets],
        "Ticker": [asset.ticker for asset in assets],
        "Weight %": [equal_weight] * len(assets),
        "Valid observations": [int(asset.data["Close"].notna().sum()) for asset in assets],
        "First date": [asset.data.index.min().date() for asset in assets],
        "Last date": [asset.data.index.max().date() for asset in assets],
    }
)
edited = st.data_editor(
    configuration,
    hide_index=True,
    use_container_width=True,
    disabled=["File", "Valid observations", "First date", "Last date"],
    column_config={
        "Weight %": st.column_config.NumberColumn(min_value=0.0, format="%.4f"),
    },
    key="portfolio_configuration",
)

with st.expander("Detected source columns", expanded=False):
    detected_rows = []
    for asset in assets:
        detected_rows.append(
            {
                "File": asset.file_name,
                **{key: value or "Not found" for key, value in asset.detected_columns.items()},
            }
        )
    st.dataframe(pd.DataFrame(detected_rows), hide_index=True, use_container_width=True)

if st.button("Build portfolio workbook", type="primary", use_container_width=True):
    try:
        configured_assets = [
            with_ticker(asset, edited.loc[index, "Ticker"])
            for index, asset in enumerate(assets)
        ]
        weights = {
            configured_assets[index].ticker: float(edited.loc[index, "Weight %"])
            for index in range(len(configured_assets))
        }
        result = build_workbook(
            configured_assets,
            weights,
            calendar_mode=calendar_mode,
            missing_method=missing_method,
            normalize_weights=normalize_weights,
        )
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    st.success("Workbook built successfully.")
    metric_1, metric_2, metric_3, metric_4 = st.columns(4)
    metric_1.metric("Assets", len(configured_assets))
    metric_2.metric("Aligned dates", len(result.cours))
    metric_3.metric("Quality-log entries", len(result.quality))
    metric_4.metric("Weight total", f"{result.portfolio['Poids'].sum():.2f}%")

    tab_preview, tab_quality, tab_portfolio = st.tabs(
        ["Cours preview", "Quality summary", "Portfolio"]
    )
    with tab_preview:
        st.dataframe(result.preview.tail(100), hide_index=True, use_container_width=True)
    with tab_quality:
        if result.quality_summary.empty:
            st.info("No repairs, imputations, or statistical flags were recorded.")
        else:
            st.dataframe(result.quality_summary, hide_index=True, use_container_width=True)
    with tab_portfolio:
        st.dataframe(result.portfolio, hide_index=True, use_container_width=True)

    st.download_button(
        "Download portefeuille_historique.xlsx",
        data=result.workbook_bytes,
        file_name="portefeuille_historique.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

