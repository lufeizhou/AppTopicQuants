"""App Store Connect: daily SALES summary reports for your own apps. These are the
only *true* revenue numbers in the system and anchor the revenue estimator.

Needs in .env: ASC_ISSUER_ID, ASC_KEY_ID, ASC_PRIVATE_KEY_PATH (.p8), ASC_VENDOR_NUMBER.
The key needs the Sales and Reports role (Finance for financial reports)."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import os
import time
from dataclasses import dataclass, field

import httpx

from ..registry import sources

API = "https://api.appstoreconnect.apple.com/v1/salesReports"
# Product Type Identifier -> class. First-time downloads (free or paid) vs. IAP/subscriptions vs. updates/other.
DOWNLOAD_TYPES = {"1", "1F", "1T", "1E", "1EP", "1EU", "F1"}
UPDATE_TYPES = {"3", "3F", "3T", "F7", "7", "7F", "7T"}
# Rough FX to USD for proceeds currencies (anchors tolerate a few % error; refresh occasionally).
FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66, "JPY": 0.0066, "CNY": 0.14, "CHF": 1.12,
      "SEK": 0.095, "NOK": 0.093, "DKK": 0.145, "HKD": 0.128, "SGD": 0.75, "NZD": 0.61, "MXN": 0.055, "BRL": 0.18,
      "INR": 0.012, "KRW": 0.00073, "TWD": 0.031, "PLN": 0.25, "TRY": 0.03, "ZAR": 0.055, "AED": 0.27, "SAR": 0.27}


@sources.register("asc")
@dataclass
class ASCClient:
    issuer_id: str = field(default_factory=lambda: os.environ.get("ASC_ISSUER_ID", ""))
    key_id: str = field(default_factory=lambda: os.environ.get("ASC_KEY_ID", ""))
    private_key_path: str = field(default_factory=lambda: os.environ.get("ASC_PRIVATE_KEY_PATH", ""))
    vendor_number: str = field(default_factory=lambda: os.environ.get("ASC_VENDOR_NUMBER", ""))
    _token: tuple[str, float] | None = None

    @property
    def available(self) -> bool:
        return all([self.issuer_id, self.key_id, self.private_key_path, self.vendor_number]) and os.path.exists(self.private_key_path)

    def token(self) -> str:
        import jwt
        now = time.time()
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        with open(self.private_key_path) as f:
            key = f.read()
        exp = now + 15 * 60
        tok = jwt.encode({"iss": self.issuer_id, "iat": int(now), "exp": int(exp), "aud": "appstoreconnect-v1"},
                         key, algorithm="ES256", headers={"kid": self.key_id, "typ": "JWT"})
        self._token = (tok, exp)
        return tok

    def daily_sales(self, day: dt.date) -> list[dict] | None:
        """Rows of the DAILY SALES SUMMARY report, or None if not (yet) available."""
        params = {"filter[frequency]": "DAILY", "filter[reportDate]": day.isoformat(), "filter[reportSubType]": "SUMMARY",
                  "filter[reportType]": "SALES", "filter[vendorNumber]": self.vendor_number}
        r = httpx.get(API, params=params, headers={"Authorization": f"Bearer {self.token()}"}, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse_sales_tsv(r.content)


def parse_sales_tsv(raw: bytes) -> list[dict]:
    try:
        text = gzip.decompress(raw).decode("utf-8")
    except (OSError, EOFError):
        text = raw.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def sales_rows_to_db(rows: list[dict], report_date: dt.date) -> list[dict]:
    out = []
    for r in rows:
        ptype = (r.get("Product Type Identifier") or "").strip()
        units = int(float(r.get("Units") or 0))
        proceeds_unit = float(r.get("Developer Proceeds") or 0)
        cur = (r.get("Currency of Proceeds") or "USD").strip()
        kind = "download" if ptype in DOWNLOAD_TYPES else "update" if ptype in UPDATE_TYPES else "iap" if ptype.startswith("IA") else "other"
        out.append({"report_date": report_date, "app_id": (r.get("Apple Identifier") or "").strip(),
                    "parent_id": (r.get("Parent Identifier") or "").strip() or None, "sku": r.get("SKU"), "title": r.get("Title"),
                    "product_type": ptype, "kind": kind, "units": units, "proceeds_unit": proceeds_unit, "currency": cur,
                    "proceeds_usd": units * proceeds_unit * FX.get(cur, 1.0), "country": r.get("Country Code"),
                    "subscription": r.get("Subscription"), "period": r.get("Period")})
    return out
