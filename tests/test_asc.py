import datetime as dt
import gzip

from apptopics import db
from apptopics.sources.asc import parse_sales_tsv, sales_rows_to_db

TSV = ("Provider\tProvider Country\tSKU\tDeveloper\tTitle\tVersion\tProduct Type Identifier\tUnits\tDeveloper Proceeds\tBegin Date\tEnd Date\t"
       "Customer Currency\tCountry Code\tCurrency of Proceeds\tApple Identifier\tCustomer Price\tPromo Code\tParent Identifier\tSubscription\tPeriod\n"
       "APPLE\tUS\tplatecall\tSanvvy\tPlateCall\t1.2\t1F\t12\t0\t10/30/2026\t10/30/2026\tUSD\tUS\tUSD\t6816400351\t0\t\t\t\t\n"
       "APPLE\tUS\tpc.monthly\tSanvvy\tPlateCall Pro\t1.2\tIAY\t3\t2.79\t10/30/2026\t10/30/2026\tUSD\tUS\tUSD\t9999\t3.99\t\tplatecall\tNew\tMonthly\n"
       "APPLE\tUS\tpc.monthly\tSanvvy\tPlateCall Pro\t1.2\tIAY\t1\t2.5\t10/30/2026\t10/30/2026\tEUR\tDE\tEUR\t9999\t3.99\t\tplatecall\tRenewal\tMonthly\n")


def test_parse_and_classify(con):
    rows = parse_sales_tsv(gzip.compress(TSV.encode()))
    out = sales_rows_to_db(rows, dt.date(2026, 10, 30))
    assert [r["kind"] for r in out] == ["download", "iap", "iap"]
    assert out[0]["units"] == 12 and out[1]["proceeds_usd"] == 3 * 2.79
    assert abs(out[2]["proceeds_usd"] - 2.5 * 1.08) < 1e-9
    db.upsert(con, "own_sales", [{**r, "row_no": i} for i, r in enumerate(out)])
    assert con.execute("SELECT count(*) FROM own_sales").fetchone()[0] == 3
