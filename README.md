# Historical Portfolio Builder

This Streamlit app combines one historical-price file per asset into an Excel workbook compatible with the supplied `portefeuille_test.xlsx` structure.

## Output

- `Portefeuille`: ticker and portfolio weight.
- `Cours`: no header row, matching the reference pattern: date, then repeating blocks of ticker, closing price, and opening price.
- `Qualite`: every excluded row, deterministic OHLC repair, statistical outlier flag, missing date, and imputed price.
- `Synthese qualite`: counts by asset and treatment.
- `Methodologie`: the selected processing parameters.

The first two sheets remain suitable for a downstream stress-test process that expects the reference layout. The extra sheets are audit information.

## Missing and non-trading dates

The recommended calendar is the union of dates observed in all uploaded files. This does not create weekends or market-wide holidays. If one asset is absent on a date when another asset traded, the recommended treatment carries forward the previous close and sets the imputed day's open/high/low to that price and volume to zero. This is a common mark-to-market convention and creates a zero return for the non-trading day.

No value is backfilled before the first observation or extrapolated after the final observation. Log-price interpolation and leaving values blank are available as alternatives.

Statistical return outliers are detected with a rolling median/MAD robust z-score. They are flagged by default, not silently overwritten. The interface can instead treat them as missing before applying the selected imputation rule.

## Expected inputs

CSV, XLSX, and XLS files are accepted. Each file must contain:

- a date column, such as `Date`;
- a closing/last-price column, such as `Dernier`, `Close`, or `Clôture`.

Opening, high, low, volume, and percentage-change columns are detected when present. French decimal commas and abbreviated volumes such as `12,16K` are supported.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Then open the local address shown by Streamlit, upload the historical files, review tickers and weights, and download the workbook.

