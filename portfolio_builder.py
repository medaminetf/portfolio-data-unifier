from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

import numpy as np
import pandas as pd


CalendarMode = Literal["union", "intersection", "business_days"]
MissingMethod = Literal["previous_close", "log_interpolation", "leave_missing"]
OutlierPolicy = Literal["flag", "replace"]


@dataclass
class ParsedAsset:
    file_name: str
    ticker: str
    data: pd.DataFrame
    events: pd.DataFrame
    detected_columns: dict[str, str | None]
    source_rows: int


@dataclass
class BuildResult:
    portfolio: pd.DataFrame
    cours: pd.DataFrame
    preview: pd.DataFrame
    quality: pd.DataFrame
    quality_summary: pd.DataFrame
    workbook_bytes: bytes
    calendar_start: pd.Timestamp
    calendar_end: pd.Timestamp


_COLUMN_ALIASES = {
    "date": ("date", "jour", "trading date", "seance", "séance"),
    "close": (
        "dernier",
        "close",
        "closing price",
        "cloture",
        "clôture",
        "cours de cloture",
        "last",
        "price",
        "prix",
    ),
    "open": ("ouv", "ouverture", "open", "opening price"),
    "high": ("plus haut", "haut", "high", "highest"),
    "low": ("plus bas", "bas", "low", "lowest"),
    "volume": ("vol", "volume", "quantite", "quantité"),
    "change_pct": (
        "variation",
        "variation pct",
        "variation %",
        "change",
        "change pct",
        "change %",
    ),
}


def _normalize_label(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("%", " pct ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def infer_ticker(file_name: str) -> str:
    stem = Path(file_name).stem
    stem = re.split(r"\s+-\s+", stem, maxsplit=1)[0]
    stem = re.sub(
        r"(?i)\b(donnees?|données?|historique[s]?|historical|history|prices?|cours)\b",
        " ",
        stem,
    )
    ticker = re.sub(r"[^A-Za-z0-9._-]+", "", stem).upper()
    return ticker[:20] or "ASSET"


def clean_ticker(value: object) -> str:
    ticker = re.sub(r"[^A-Za-z0-9._-]+", "", str(value)).upper()
    if not ticker:
        raise ValueError("Every uploaded file needs a non-empty ticker.")
    return ticker[:20]


def _read_tabular_bytes(file_name: str, payload: bytes) -> pd.DataFrame:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(io.BytesIO(payload), dtype=str)
    if suffix != ".csv":
        raise ValueError(f"Unsupported file type: {suffix or 'unknown'}")

    decode_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = payload.decode(encoding)
            frame = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str)
            if frame.shape[1] == 1:
                for separator in (",", ";", "\t"):
                    candidate = pd.read_csv(
                        io.StringIO(text), sep=separator, engine="python", dtype=str
                    )
                    if candidate.shape[1] > frame.shape[1]:
                        frame = candidate
            return frame
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            decode_error = exc
    raise ValueError(f"Could not read CSV encoding or delimiter: {decode_error}")


def _detect_column(columns: Iterable[object], semantic: str) -> str | None:
    normalized = {str(column): _normalize_label(column) for column in columns}
    aliases = {_normalize_label(alias) for alias in _COLUMN_ALIASES[semantic]}

    for original, label in normalized.items():
        if label in aliases:
            return original
    for original, label in normalized.items():
        if any(alias and (label.startswith(alias) or alias in label) for alias in aliases):
            return original
    return None


def _parse_number(value: object, *, percent: bool = False) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return np.nan
    if isinstance(value, (int, float, np.number)):
        result = float(value)
        return result / 100.0 if percent else result

    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text or text.lower() in {"nan", "none", "null", "-", "--", "n/a"}:
        return np.nan

    negative_parentheses = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    suffix_multiplier = 1.0
    suffix_match = re.search(r"(?i)([kmb])$", text)
    if suffix_match:
        suffix_multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[
            suffix_match.group(1).lower()
        ]
        text = text[:-1]

    had_percent = "%" in text
    text = text.replace("%", "")
    text = re.sub(r"[^0-9,\.\-+]", "", text)
    if not text:
        return np.nan

    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        text = text.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 4:
            text = ".".join(parts)
        else:
            text = "".join(parts)
    elif text.count(".") > 1:
        parts = text.split(".")
        if len(parts[-1]) <= 4:
            text = "".join(parts[:-1]) + "." + parts[-1]
        else:
            text = "".join(parts)

    try:
        result = float(text) * suffix_multiplier
    except ValueError:
        return np.nan
    if negative_parentheses:
        result = -result
    if percent or had_percent:
        result /= 100.0
    return result


def _last_valid(series: pd.Series):
    valid = series.dropna()
    return valid.iloc[-1] if not valid.empty else np.nan


def _event(
    issue: str,
    detail: str,
    date: pd.Timestamp | pd.NaT = pd.NaT,
    original_close: float = np.nan,
    output_close: float = np.nan,
) -> dict[str, object]:
    return {
        "Date": date,
        "Issue": issue,
        "Detail": detail,
        "Original Close": original_close,
        "Output Close": output_close,
    }


def parse_market_file(
    file_name: str,
    payload: bytes,
    *,
    dayfirst: bool = True,
    outlier_threshold: float = 10.0,
    outlier_policy: OutlierPolicy = "flag",
) -> ParsedAsset:
    raw = _read_tabular_bytes(file_name, payload)
    raw.columns = [str(column).strip() for column in raw.columns]
    detected = {
        semantic: _detect_column(raw.columns, semantic) for semantic in _COLUMN_ALIASES
    }
    if detected["date"] is None or detected["close"] is None:
        raise ValueError(
            "Required columns were not found. Each file needs a date column and a "
            "closing/last-price column (for example Date and Dernier)."
        )

    frame = pd.DataFrame(index=raw.index)
    frame["Date"] = pd.to_datetime(
        raw[detected["date"]], dayfirst=dayfirst, errors="coerce"
    ).dt.normalize()
    for target, semantic in (
        ("Close", "close"),
        ("Open", "open"),
        ("High", "high"),
        ("Low", "low"),
        ("Volume", "volume"),
        ("ChangePct", "change_pct"),
    ):
        source_column = detected[semantic]
        if source_column is None:
            frame[target] = np.nan
        else:
            is_percent = semantic == "change_pct"
            frame[target] = raw[source_column].map(
                lambda value: _parse_number(value, percent=is_percent)
            )

    events: list[dict[str, object]] = []
    invalid_dates = frame["Date"].isna()
    if invalid_dates.any():
        events.append(
            _event(
                "Invalid date excluded",
                f"{int(invalid_dates.sum())} row(s) could not be parsed as dates.",
            )
        )
    frame = frame.loc[~invalid_dates].copy()

    invalid_close = frame["Close"].notna() & (frame["Close"] <= 0)
    for date, close in frame.loc[invalid_close, ["Date", "Close"]].itertuples(
        index=False, name=None
    ):
        events.append(
            _event(
                "Invalid close removed",
                "A non-positive closing price was treated as missing before alignment.",
                date,
                close,
            )
        )
    frame.loc[invalid_close, "Close"] = np.nan

    duplicates = frame[frame.duplicated("Date", keep=False)]
    if not duplicates.empty:
        for date, count in duplicates.groupby("Date").size().items():
            events.append(
                _event(
                    "Duplicate date consolidated",
                    f"{int(count)} rows were consolidated using the last non-empty value.",
                    date,
                )
            )
        value_columns = ["Close", "Open", "High", "Low", "Volume", "ChangePct"]
        frame = frame.groupby("Date", as_index=False)[value_columns].agg(_last_valid)

    frame = frame.sort_values("Date").drop_duplicates("Date", keep="last")
    frame = frame.set_index("Date")

    missing_open = frame["Open"].isna() & frame["Close"].notna()
    for date, close in frame.loc[missing_open, ["Close"]].itertuples(
        index=True, name=None
    ):
        events.append(
            _event(
                "Opening price missing",
                "Opening price was set equal to the observed close.",
                date,
                close,
                close,
            )
        )
    frame.loc[missing_open, "Open"] = frame.loc[missing_open, "Close"]

    reference_max = frame[["Open", "Close", "High", "Low"]].max(axis=1, skipna=True)
    reference_min = frame[["Open", "Close", "High", "Low"]].min(axis=1, skipna=True)
    high_bad = frame["High"].notna() & (frame["High"] < reference_max)
    low_bad = frame["Low"].notna() & (frame["Low"] > reference_min)
    for date in frame.index[high_bad | low_bad]:
        details: list[str] = []
        if high_bad.loc[date]:
            details.append("high raised to the row maximum")
            frame.loc[date, "High"] = reference_max.loc[date]
        if low_bad.loc[date]:
            details.append("low lowered to the row minimum")
            frame.loc[date, "Low"] = reference_min.loc[date]
        events.append(
            _event(
                "OHLC range repaired",
                "; ".join(details).capitalize() + ".",
                date,
                frame.loc[date, "Close"],
                frame.loc[date, "Close"],
            )
        )

    positive_close = frame["Close"].where(frame["Close"] > 0)
    log_returns = np.log(positive_close).diff()
    rolling_median = log_returns.rolling(41, center=True, min_periods=10).median()
    rolling_mad = (log_returns - rolling_median).abs().rolling(
        41, center=True, min_periods=10
    ).median()
    global_median = log_returns.median()
    global_mad = (log_returns - global_median).abs().median()
    center = rolling_median.fillna(global_median)
    scale = (1.4826 * rolling_mad).replace(0, np.nan)
    if pd.notna(global_mad) and global_mad > 0:
        scale = scale.fillna(1.4826 * global_mad)
    robust_z = (log_returns - center).abs() / scale
    outliers = robust_z > float(outlier_threshold)
    for date in frame.index[outliers.fillna(False)]:
        close = frame.loc[date, "Close"]
        action = (
            "The observation was set to missing and will follow the selected imputation rule."
            if outlier_policy == "replace"
            else "The observation was retained and only flagged for review."
        )
        events.append(
            _event(
                "Statistical return outlier",
                f"Robust z-score {robust_z.loc[date]:.2f}. {action}",
                date,
                close,
                close if outlier_policy == "flag" else np.nan,
            )
        )
    if outlier_policy == "replace":
        frame.loc[outliers.fillna(False), ["Close", "Open", "High", "Low"]] = np.nan

    frame["WasObserved"] = frame["Close"].notna()
    frame["RobustReturnZ"] = robust_z
    return ParsedAsset(
        file_name=file_name,
        ticker=infer_ticker(file_name),
        data=frame,
        events=pd.DataFrame(events),
        detected_columns=detected,
        source_rows=len(raw),
    )


def with_ticker(asset: ParsedAsset, ticker: str) -> ParsedAsset:
    return replace(asset, ticker=clean_ticker(ticker))


def _master_calendar(assets: list[ParsedAsset], mode: CalendarMode) -> pd.DatetimeIndex:
    indexes = [asset.data.index for asset in assets if not asset.data.empty]
    if not indexes:
        raise ValueError("No valid dated observations were found.")
    if mode == "intersection":
        calendar = indexes[0]
        for index in indexes[1:]:
            calendar = calendar.intersection(index)
    elif mode == "business_days":
        calendar = pd.bdate_range(
            min(index.min() for index in indexes), max(index.max() for index in indexes)
        )
    else:
        calendar = indexes[0]
        for index in indexes[1:]:
            calendar = calendar.union(index)
    calendar = pd.DatetimeIndex(calendar).sort_values().unique()
    if calendar.empty:
        raise ValueError("The selected calendar has no dates shared by the uploaded files.")
    return calendar


def _impute_asset(
    asset: ParsedAsset, calendar: pd.DatetimeIndex, method: MissingMethod
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    aligned = asset.data.reindex(calendar).copy()
    observed_close = aligned["Close"].copy()
    first_date = asset.data.index.min()
    last_date = asset.data.index.max()
    inside_coverage = (calendar >= first_date) & (calendar <= last_date)
    missing_inside = aligned["Close"].isna() & inside_coverage
    extra_events: list[dict[str, object]] = []

    if method == "previous_close":
        filled_close = aligned["Close"].ffill().where(inside_coverage)
        issue_name = "Missing/non-trading date: previous close carried forward"
        method_detail = (
            "The last observed close was carried forward; open/high/low were set to that "
            "price and volume to zero."
        )
    elif method == "log_interpolation":
        logged = np.log(aligned["Close"].where(aligned["Close"] > 0))
        filled_close = np.exp(logged.interpolate(method="time", limit_area="inside"))
        filled_close = filled_close.where(inside_coverage)
        issue_name = "Missing date: log-price interpolation"
        method_detail = (
            "The internal gap was filled by linear interpolation in log-price space; "
            "open/high/low were set to the estimate and volume to zero."
        )
    else:
        filled_close = aligned["Close"]
        issue_name = "Missing date left blank"
        method_detail = "No price was imputed."

    imputed = missing_inside & filled_close.notna()
    aligned["Close"] = filled_close
    aligned.loc[imputed, "Open"] = aligned.loc[imputed, "Close"]
    aligned.loc[imputed, "High"] = aligned.loc[imputed, "Close"]
    aligned.loc[imputed, "Low"] = aligned.loc[imputed, "Close"]
    aligned.loc[imputed, "Volume"] = 0.0

    for date in calendar[missing_inside]:
        if imputed.loc[date]:
            extra_events.append(
                _event(
                    issue_name,
                    method_detail,
                    date,
                    np.nan,
                    aligned.loc[date, "Close"],
                )
            )
        else:
            extra_events.append(
                _event("Missing date left blank", method_detail, date, np.nan, np.nan)
            )

    outside = aligned["Close"].isna() & ~inside_coverage
    for date in calendar[outside]:
        extra_events.append(
            _event(
                "Outside asset coverage",
                "No backfill or extrapolation was applied outside the asset's observed range.",
                date,
            )
        )

    aligned["WasObserved"] = observed_close.notna()
    aligned["WasImputed"] = imputed
    return aligned, extra_events


def _to_quality_frame(
    asset: ParsedAsset, extra_events: list[dict[str, object]]
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if not asset.events.empty:
        frames.append(asset.events.copy())
    if extra_events:
        frames.append(pd.DataFrame(extra_events))
    if not frames:
        return pd.DataFrame(
            columns=[
                "Date",
                "Valeur",
                "Fichier source",
                "Anomalie / traitement",
                "Détail",
                "Cours original",
                "Cours de sortie",
            ]
        )
    quality = pd.concat(frames, ignore_index=True)
    quality["Valeur"] = asset.ticker
    quality["Fichier source"] = asset.file_name
    quality = quality.rename(
        columns={
            "Issue": "Anomalie / traitement",
            "Detail": "Détail",
            "Original Close": "Cours original",
            "Output Close": "Cours de sortie",
        }
    )
    return quality[
        [
            "Date",
            "Valeur",
            "Fichier source",
            "Anomalie / traitement",
            "Détail",
            "Cours original",
            "Cours de sortie",
        ]
    ]


def build_workbook(
    assets: list[ParsedAsset],
    weights: dict[str, float],
    *,
    calendar_mode: CalendarMode = "union",
    missing_method: MissingMethod = "previous_close",
    normalize_weights: bool = True,
) -> BuildResult:
    if not assets:
        raise ValueError("Upload at least one valid historical data file.")
    tickers = [clean_ticker(asset.ticker) for asset in assets]
    if len(set(tickers)) != len(tickers):
        raise ValueError("Ticker names must be unique across uploaded files.")

    raw_weights = pd.Series(
        [float(weights.get(asset.ticker, 0.0)) for asset in assets], index=tickers
    )
    if (raw_weights < 0).any() or raw_weights.sum() <= 0:
        raise ValueError("Weights must be non-negative and their total must be greater than zero.")
    if normalize_weights:
        output_weights = raw_weights / raw_weights.sum() * 100.0
    else:
        output_weights = raw_weights

    calendar = _master_calendar(assets, calendar_mode)
    aligned_assets: list[tuple[ParsedAsset, pd.DataFrame]] = []
    quality_frames: list[pd.DataFrame] = []
    for asset in assets:
        aligned, events = _impute_asset(asset, calendar, missing_method)
        aligned_assets.append((asset, aligned))
        quality_frames.append(_to_quality_frame(asset, events))

    portfolio = pd.DataFrame(
        {"Valeur": tickers, "Poids": [output_weights.loc[ticker] for ticker in tickers]}
    )
    cours = pd.DataFrame({"Date": calendar})
    preview = pd.DataFrame({"Date": calendar})
    for asset, aligned in aligned_assets:
        block_start = len(cours.columns)
        cours.insert(block_start, f"Ticker_{asset.ticker}", asset.ticker)
        cours.insert(block_start + 1, f"Close_{asset.ticker}", aligned["Close"].to_numpy())
        cours.insert(block_start + 2, f"Open_{asset.ticker}", aligned["Open"].to_numpy())
        preview[f"{asset.ticker} | Dernier"] = aligned["Close"].to_numpy()
        preview[f"{asset.ticker} | Ouv."] = aligned["Open"].to_numpy()

    quality = pd.concat(quality_frames, ignore_index=True) if quality_frames else pd.DataFrame()
    if not quality.empty:
        quality["Date"] = pd.to_datetime(quality["Date"], errors="coerce")
        quality = quality.sort_values(["Date", "Valeur"], na_position="last").reset_index(
            drop=True
        )
        quality_summary = (
            quality.groupby(["Valeur", "Anomalie / traitement"], dropna=False)
            .size()
            .rename("Nombre")
            .reset_index()
        )
    else:
        quality_summary = pd.DataFrame(
            columns=["Valeur", "Anomalie / traitement", "Nombre"]
        )

    params = [
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        ("Calendar", calendar_mode),
        ("Missing-data method", missing_method),
        ("Weight normalization", "Yes" if normalize_weights else "No"),
        ("Calendar start", calendar.min().strftime("%Y-%m-%d")),
        ("Calendar end", calendar.max().strftime("%Y-%m-%d")),
        ("Calendar rows", len(calendar)),
    ]
    methodology = pd.DataFrame(params, columns=["Paramètre", "Valeur"])

    output = io.BytesIO()
    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
        datetime_format="yyyy-mm-dd",
        engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}},
    ) as writer:
        portfolio.to_excel(writer, sheet_name="Portefeuille", index=False)
        cours.to_excel(writer, sheet_name="Cours", index=False, header=False)
        quality.to_excel(writer, sheet_name="Qualite", index=False)
        quality_summary.to_excel(writer, sheet_name="Synthese qualite", index=False)
        methodology.to_excel(writer, sheet_name="Methodologie", index=False)

        workbook = writer.book
        header = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#163A5F",
                "border": 0,
            }
        )
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
        price_format = workbook.add_format({"num_format": "0.0000"})
        weight_format = workbook.add_format({"num_format": "0.00"})
        integer_format = workbook.add_format({"num_format": "0"})
        note_format = workbook.add_format({"text_wrap": True, "valign": "top"})

        portfolio_sheet = writer.sheets["Portefeuille"]
        portfolio_sheet.set_column("A:A", 14)
        portfolio_sheet.set_column("B:B", 12, weight_format)
        portfolio_sheet.freeze_panes(1, 0)
        portfolio_sheet.autofilter(0, 0, len(portfolio), 1)
        portfolio_sheet.set_row(0, 20, header)

        cours_sheet = writer.sheets["Cours"]
        cours_sheet.set_column(0, 0, 12, date_format)
        for position in range(len(assets)):
            first = 1 + position * 3
            cours_sheet.set_column(first, first, 11)
            cours_sheet.set_column(first + 1, first + 2, 14, price_format)
        cours_sheet.freeze_panes(0, 1)

        for sheet_name, frame in (
            ("Qualite", quality),
            ("Synthese qualite", quality_summary),
            ("Methodologie", methodology),
        ):
            sheet = writer.sheets[sheet_name]
            sheet.set_row(0, 22, header)
            sheet.freeze_panes(1, 0)
            if len(frame.columns):
                sheet.autofilter(0, 0, max(len(frame), 1), len(frame.columns) - 1)

        quality_sheet = writer.sheets["Qualite"]
        quality_sheet.set_column("A:A", 12, date_format)
        quality_sheet.set_column("B:B", 12)
        quality_sheet.set_column("C:C", 34)
        quality_sheet.set_column("D:D", 46)
        quality_sheet.set_column("E:E", 68, note_format)
        quality_sheet.set_column("F:G", 16, price_format)

        summary_sheet = writer.sheets["Synthese qualite"]
        summary_sheet.set_column("A:A", 12)
        summary_sheet.set_column("B:B", 58)
        summary_sheet.set_column("C:C", 12, integer_format)

        method_sheet = writer.sheets["Methodologie"]
        method_sheet.set_column("A:A", 28)
        method_sheet.set_column("B:B", 34)

    return BuildResult(
        portfolio=portfolio,
        cours=cours,
        preview=preview,
        quality=quality,
        quality_summary=quality_summary,
        workbook_bytes=output.getvalue(),
        calendar_start=calendar.min(),
        calendar_end=calendar.max(),
    )
