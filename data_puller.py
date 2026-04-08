"""
Houston MSA — Comprehensive County Data Pull
=============================================
Pulls financial, demographic, housing, and migration data from 13 free
federal/public sources for the 9-county Houston-Woodlands-Sugar Land MSA.

Each source is written to its own Excel sheet in houston_county_data.xlsx.
Sources that fail (network, URL changes) are skipped gracefully.

Requirements
------------
    pip install pandas openpyxl xlrd requests

API Keys (free — optional but recommended)
------------------------------------------
    CENSUS_API_KEY  :  https://api.census.gov/data/key_signup.html
    BEA_API_KEY     :  https://apps.bea.gov/API/signup/
    BLS_API_KEY     :  https://data.bls.gov/registrationEngine/

Set them as environment variables before running, or paste them into the
CONFIG section below.  The script will still attempt to pull data without
keys, but some APIs may throttle or reject unauthenticated requests.
"""

import os
import io
import re
import time
import json
import requests
import pandas as pd

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

# Houston-Woodlands-Sugar Land MSA counties  (full 5-digit FIPS → name)
HOUSTON_COUNTIES = {
    "48015": "Austin",
    "48039": "Brazoria",
    "48071": "Chambers",
    "48157": "Fort Bend",
    "48167": "Galveston",
    "48201": "Harris",
    "48291": "Liberty",
    "48339": "Montgomery",
    "48473": "Waller",
}

STATE_FIPS = "48"  # Texas
COUNTY_FIPS_3 = {k[-3:]: v for k, v in HOUSTON_COUNTIES.items()}  # 3-digit codes

# API keys — set via env vars or hardcode here
CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "dbabc21e4868577738ca77ceb61c477b5387cd36")
BEA_API_KEY = os.environ.get("BEA_API_KEY", "13CDED12-D911-4B7D-A154-8DA20A6CCD00")
BLS_API_KEY = os.environ.get("BLS_API_KEY", "c0e02be07451439fa940455e0db4fe97")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HoustonMSA-DataPull/1.0"})


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _get(url: str, **kwargs) -> requests.Response | None:
    """GET with retries and timeout.  Does NOT retry on 404/410 (file gone)."""
    kwargs.setdefault("timeout", 120)
    quiet = kwargs.pop("quiet", False)       # suppress per-attempt warnings
    for attempt in range(3):
        try:
            r = SESSION.get(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            # 404 / 410 are definitive — no point retrying
            if r.status_code in (404, 410):
                if not quiet:
                    print(f"    ⚠  {r.status_code} {url.split('/')[-1]}")
                return None
            if attempt == 2:
                print(f"    ⚠  Failed after 3 attempts: {e}")
                return None
            time.sleep(2 * (attempt + 1))
        except requests.RequestException as e:
            if attempt == 2:
                print(f"    ⚠  Failed after 3 attempts: {e}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


def _fips_filter(df: pd.DataFrame, fips_col: str) -> pd.DataFrame:
    """Filter a dataframe to only Houston MSA counties by a FIPS column."""
    df[fips_col] = df[fips_col].astype(str).str.zfill(5)
    return df[df[fips_col].isin(HOUSTON_COUNTIES.keys())].copy()


# ═══════════════════════════════════════════════════════════════════════════
# 1. CENSUS PEP — COMPONENTS OF CHANGE
# ═══════════════════════════════════════════════════════════════════════════

PEP_SOURCES = {
    "2000-2010": {
        "url": "https://www2.census.gov/programs-surveys/popest/datasets/2000-2010/intercensal/county/co-est00int-alldata-48.csv",
        "encoding": "latin-1",
    },
    "2010-2019": {
        "url": "https://www2.census.gov/programs-surveys/popest/datasets/2010-2019/counties/totals/co-est2019-alldata.csv",
        "encoding": "latin-1",
    },
    "2020-2024": {
        "url": "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv",
        "encoding": "utf-8",
    },
}


def pull_census_pep() -> pd.DataFrame:
    """Download Census PEP files → tidy dataframe."""
    frames = []
    for label, meta in PEP_SOURCES.items():
        print(f"  Downloading Census PEP {label} …")
        r = _get(meta["url"])
        if r is None:
            # try fallback with prior vintage year
            alt = re.sub(r"(\d{4})-(\d{4})", lambda m: f"{m.group(1)}-{int(m.group(2))-1}", meta["url"])
            alt = re.sub(r"co-est(\d{4})", lambda m: f"co-est{int(m.group(1))-1}", alt)
            r = _get(alt)
        if r is None and "-48" not in meta["url"]:
            # 2000-2010 intercensal files are per-state; try Texas (FIPS 48)
            alt = meta["url"].replace(".csv", f"-{STATE_FIPS}.csv")
            r = _get(alt)
        if r is None:
            continue

        df = pd.read_csv(io.StringIO(r.text), encoding=meta.get("encoding", "utf-8"),
                         dtype={"STATE": str, "COUNTY": str}, low_memory=False)
        df["FIPS"] = df["STATE"] + df["COUNTY"]
        df = df[df["FIPS"].isin(HOUSTON_COUNTIES.keys())].copy()
        if df.empty:
            continue
        df["COUNTY_NAME"] = df["FIPS"].map(HOUSTON_COUNTIES)

        prefixes = ["DOMESTICMIG", "INTERNATIONALMIG", "NETMIG", "NPOPCHG",
                     "BIRTHS", "DEATHS", "RESIDUAL", "POPESTIMATE"]
        mig_cols = [c for c in df.columns if any(c.startswith(p) for p in prefixes)]

        records = []
        for _, row in df.iterrows():
            for col in mig_cols:
                yr = col[-4:]
                if not yr.isdigit():
                    continue
                records.append({"fips": row["FIPS"], "county": row["COUNTY_NAME"],
                                "year": int(yr), "measure": col[:-4], "value": row[col]})
        if records:
            frames.append(pd.DataFrame(records))

    if not frames:
        return pd.DataFrame()

    tidy = pd.concat(frames, ignore_index=True)
    piv = tidy.pivot_table(index=["fips", "county", "year"], columns="measure",
                           values="value", aggfunc="first").reset_index()
    piv.columns.name = None
    rename = {"POPESTIMATE": "population_estimate", "NPOPCHG": "net_pop_change",
              "BIRTHS": "births", "DEATHS": "deaths", "NETMIG": "net_migration",
              "DOMESTICMIG": "net_domestic_migration",
              "INTERNATIONALMIG": "net_international_migration", "RESIDUAL": "residual"}
    return piv.rename(columns=rename).sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 2. IRS SOI — COUNTY MIGRATION FLOWS
# ═══════════════════════════════════════════════════════════════════════════

IRS_YEAR_PAIRS = [
    (1112, "2011-2012"), (1213, "2012-2013"), (1314, "2013-2014"),
    (1415, "2014-2015"), (1516, "2015-2016"), (1617, "2016-2017"),
    (1718, "2017-2018"), (1819, "2018-2019"), (1920, "2019-2020"),
    (2021, "2020-2021"), (2122, "2021-2022"), (2223, "2022-2023"),
]
IRS_BASE = "https://www.irs.gov/pub/irs-soi"


def _download_irs(direction: str, code: int):
    for fmt in [f"{code:04d}", str(code)]:
        r = _get(f"{IRS_BASE}/county{direction}{fmt}.csv")
        if r is not None:
            try:
                return pd.read_csv(io.StringIO(r.text), dtype=str, on_bad_lines="skip")
            except Exception:
                pass
    return None


def _std_irs_cols(df):
    df.columns = df.columns.str.strip().str.upper()
    rn = {}
    for c in df.columns:
        if c in ("Y1_STATEFIPS", "Y1_STATE", "Y1_STATE_FIPS"): rn[c] = "ORIGIN_STATE"
        elif c in ("Y2_STATEFIPS", "Y2_STATE", "Y2_STATE_FIPS"): rn[c] = "DEST_STATE"
        elif c in ("Y1_COUNTYFIPS", "Y1_COUNTY", "Y1_COUNTY_FIPS"): rn[c] = "ORIGIN_COUNTY"
        elif c in ("Y2_COUNTYFIPS", "Y2_COUNTY", "Y2_COUNTY_FIPS"): rn[c] = "DEST_COUNTY"
        elif "RETURN" in c or c == "N1": rn[c] = "RETURNS"
        elif "EXEMPTION" in c or c == "N2": rn[c] = "EXEMPTIONS"
        elif "AGI" in c: rn[c] = "AGI_THOUSANDS"
    return df.rename(columns=rn)


def pull_irs_soi() -> pd.DataFrame:
    county3 = {f[-3:] for f in HOUSTON_COUNTIES}
    records = []
    for code, label in IRS_YEAR_PAIRS:
        print(f"  Downloading IRS SOI {label} …")
        for direction in ["inflow", "outflow"]:
            raw = _download_irs(direction, code)
            if raw is None:
                continue
            df = _std_irs_cols(raw)
            sc = "DEST_STATE" if direction == "inflow" else "ORIGIN_STATE"
            cc = "DEST_COUNTY" if direction == "inflow" else "ORIGIN_COUNTY"
            if sc not in df.columns or cc not in df.columns:
                continue
            df[sc] = df[sc].astype(str).str.strip().str.zfill(2)
            df[cc] = df[cc].astype(str).str.strip().str.zfill(3)
            filt = df[(df[sc] == STATE_FIPS) & (df[cc].isin(county3))].copy()
            if filt.empty:
                continue
            for nc in ["RETURNS", "EXEMPTIONS", "AGI_THOUSANDS"]:
                if nc in filt.columns:
                    filt[nc] = pd.to_numeric(filt[nc].astype(str).str.replace(",", ""), errors="coerce")
            filt["FIPS"] = STATE_FIPS + filt[cc]
            filt["COUNTY_NAME"] = filt["FIPS"].map(HOUSTON_COUNTIES)
            agg = filt.groupby(["FIPS", "COUNTY_NAME"]).agg(
                returns=("RETURNS", "sum"), exemptions=("EXEMPTIONS", "sum"),
                agi_thousands=("AGI_THOUSANDS", "sum")).reset_index()
            agg["year_pair"] = label
            agg["direction"] = direction
            records.append(agg)

    if not records:
        return pd.DataFrame()
    af = pd.concat(records, ignore_index=True)
    piv = af.pivot_table(index=["FIPS", "COUNTY_NAME", "year_pair"], columns="direction",
                         values=["returns", "exemptions", "agi_thousands"], aggfunc="sum")
    piv.columns = [f"{v}_{d}" for v, d in piv.columns]
    piv = piv.reset_index()
    for m in ["returns", "exemptions", "agi_thousands"]:
        ic, oc = f"{m}_inflow", f"{m}_outflow"
        if ic in piv.columns and oc in piv.columns:
            piv[f"net_{m}"] = piv[ic].fillna(0) - piv[oc].fillna(0)
    return piv.rename(columns={"FIPS": "fips", "COUNTY_NAME": "county"}).sort_values(
        ["county", "year_pair"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 3. BEA — PERSONAL INCOME BY COUNTY
# ═══════════════════════════════════════════════════════════════════════════

def pull_bea_personal_income() -> pd.DataFrame:
    """BEA CAINC1 — personal income, per-capita income, population."""
    if not BEA_API_KEY:
        print("    ⚠  No BEA_API_KEY set — trying bulk CSV fallback")
        return _pull_bea_csv_fallback("personal_income")

    frames = []
    for fips in HOUSTON_COUNTIES:
        url = (f"https://apps.bea.gov/api/data/?UserID={BEA_API_KEY}"
               f"&method=GetData&datasetname=Regional"
               f"&TableName=CAINC1&LineCode=1&GeoFips={fips}&Year=ALL"
               f"&ResultFormat=JSON")
        r = _get(url)
        if r is None:
            continue
        try:
            data = r.json()["BEAAPI"]["Results"]["Data"]
        except (KeyError, json.JSONDecodeError):
            continue
        frames.append(pd.DataFrame(data))

    if not frames:
        return _pull_bea_csv_fallback("personal_income")

    df = pd.concat(frames, ignore_index=True)
    df["county"] = df["GeoFips"].map(HOUSTON_COUNTIES)
    df = df.rename(columns={"GeoFips": "fips", "TimePeriod": "year", "DataValue": "value",
                            "Description": "measure"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"].astype(str).str.replace(",", ""), errors="coerce")
    return df[["fips", "county", "year", "measure", "value"]].sort_values(
        ["county", "year"]).reset_index(drop=True)


def _pull_bea_csv_fallback(kind: str) -> pd.DataFrame:
    """Attempt to pull BEA data from their bulk CSV downloads."""
    url = "https://apps.bea.gov/regional/zip/CAINC1.zip"
    r = _get(url)
    if r is None:
        return pd.DataFrame()
    import zipfile
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_names = [n for n in z.namelist() if n.endswith(".csv")]
        if not csv_names:
            return pd.DataFrame()
        df = pd.read_csv(z.open(csv_names[0]), dtype={"GeoFIPS": str}, encoding="latin-1", low_memory=False)
    df["GeoFIPS"] = df["GeoFIPS"].str.strip().str.replace('"', '')
    df = df[df["GeoFIPS"].isin(HOUSTON_COUNTIES.keys())]
    if df.empty:
        return pd.DataFrame()
    # Melt year columns
    year_cols = [c for c in df.columns if re.match(r"^\d{4}$", str(c))]
    melted = df.melt(id_vars=["GeoFIPS", "GeoName", "Description"],
                     value_vars=year_cols, var_name="year", value_name="value")
    melted["county"] = melted["GeoFIPS"].map(HOUSTON_COUNTIES)
    melted["year"] = pd.to_numeric(melted["year"], errors="coerce")
    melted["value"] = pd.to_numeric(melted["value"].astype(str).str.replace(",", "").str.replace("(NA)", ""),
                                    errors="coerce")
    return melted.rename(columns={"GeoFIPS": "fips", "Description": "measure"})[
        ["fips", "county", "year", "measure", "value"]].sort_values(
        ["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4. BEA — GDP BY COUNTY
# ═══════════════════════════════════════════════════════════════════════════

def pull_bea_gdp() -> pd.DataFrame:
    """BEA CAGDP2 — GDP by county (all industries)."""
    if not BEA_API_KEY:
        print("    ⚠  No BEA_API_KEY set — trying bulk CSV fallback")
        return _pull_bea_gdp_csv()

    frames = []
    for fips in HOUSTON_COUNTIES:
        url = (f"https://apps.bea.gov/api/data/?UserID={BEA_API_KEY}"
               f"&method=GetData&datasetname=Regional"
               f"&TableName=CAGDP2&LineCode=1&GeoFips={fips}&Year=ALL"
               f"&ResultFormat=JSON")
        r = _get(url)
        if r is None:
            continue
        try:
            data = r.json()["BEAAPI"]["Results"]["Data"]
        except (KeyError, json.JSONDecodeError):
            continue
        frames.append(pd.DataFrame(data))

    if not frames:
        return _pull_bea_gdp_csv()

    df = pd.concat(frames, ignore_index=True)
    df["county"] = df["GeoFips"].map(HOUSTON_COUNTIES)
    df = df.rename(columns={"GeoFips": "fips", "TimePeriod": "year",
                            "DataValue": "gdp_thousands", "Description": "industry"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["gdp_thousands"] = pd.to_numeric(df["gdp_thousands"].astype(str).str.replace(",", ""),
                                        errors="coerce")
    return df[["fips", "county", "year", "industry", "gdp_thousands"]].sort_values(
        ["county", "year"]).reset_index(drop=True)


def _pull_bea_gdp_csv() -> pd.DataFrame:
    url = "https://apps.bea.gov/regional/zip/CAGDP2.zip"
    r = _get(url)
    if r is None:
        return pd.DataFrame()
    import zipfile
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_names = [n for n in z.namelist() if n.endswith(".csv")]
        if not csv_names:
            return pd.DataFrame()
        df = pd.read_csv(z.open(csv_names[0]), dtype={"GeoFIPS": str}, encoding="latin-1", low_memory=False)
    df["GeoFIPS"] = df["GeoFIPS"].str.strip().str.replace('"', '')
    df = df[df["GeoFIPS"].isin(HOUSTON_COUNTIES.keys())]
    if df.empty:
        return pd.DataFrame()
    year_cols = [c for c in df.columns if re.match(r"^\d{4}$", str(c))]
    melted = df.melt(id_vars=["GeoFIPS", "GeoName", "Description"],
                     value_vars=year_cols, var_name="year", value_name="gdp_thousands")
    melted["county"] = melted["GeoFIPS"].map(HOUSTON_COUNTIES)
    melted["year"] = pd.to_numeric(melted["year"], errors="coerce")
    melted["gdp_thousands"] = pd.to_numeric(
        melted["gdp_thousands"].astype(str).str.replace(",", "").str.replace("(NA)", ""),
        errors="coerce")
    return melted.rename(columns={"GeoFIPS": "fips", "Description": "industry"})[
        ["fips", "county", "year", "industry", "gdp_thousands"]].sort_values(
        ["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 5. BLS QCEW — EMPLOYMENT & WAGES
# ═══════════════════════════════════════════════════════════════════════════

def pull_bls_qcew() -> pd.DataFrame:
    """BLS QCEW annual averages — total all-industries by county.
    Uses the QCEW Open Data API which only covers the most recent 5 years.
    For older data, bulk CSV downloads would be needed from:
    https://www.bls.gov/cew/downloadable-data-files.htm"""
    frames = []

    # QCEW Open Data API only provides the most recent 5 years of data.
    # Older years return 404. Adjust start year accordingly.
    for year in range(2020, 2025):
        print(f"    QCEW {year} …", end=" ")
        got_data_this_year = False
        for fips in HOUSTON_COUNTIES:
            # QCEW Data API: pull annual averages for one county
            url = (f"https://data.bls.gov/cew/data/api/{year}/a/"
                   f"area/{fips}.csv")
            r = _get(url)
            if r is None:
                continue
            try:
                df = pd.read_csv(io.StringIO(r.text), dtype={"area_fips": str})
            except Exception:
                continue

            # Filter to total all-industries, all ownerships
            mask = pd.Series([False] * len(df))
            if "industry_code" in df.columns and "own_code" in df.columns:
                mask = (df["industry_code"].astype(str) == "10") & (df["own_code"].astype(str) == "0")
            filt = df[mask].copy()
            if filt.empty:
                continue

            filt = filt.assign(county=HOUSTON_COUNTIES[fips], year=year)
            cols_keep = {"area_fips": "fips", "county": "county", "year": "year",
                         "annual_avg_estabs": "establishments",
                         "annual_avg_emplvl": "avg_employment",
                         "total_annual_wages": "total_wages",
                         "annual_avg_wkly_wage": "avg_weekly_wage"}
            available = {k: v for k, v in cols_keep.items() if k in filt.columns}
            frames.append(filt[list(available.keys())].rename(columns=available))
            got_data_this_year = True
        print("ok" if got_data_this_year else "skip")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 6. BLS LAUS — UNEMPLOYMENT
# ═══════════════════════════════════════════════════════════════════════════

def pull_bls_laus() -> pd.DataFrame:
    """BLS Local Area Unemployment Statistics — annual averages by county.
    Uses BLS Public Data API v2.  With a valid registration key the API
    allows 20-year spans and 50 series/request.  Without a key (or if the
    key is rejected) we fall back to 10-year spans and 25 series/request.
    """
    measures = {
        "03": "unemployment_rate",
        "04": "unemployment_count",
        "05": "employment_count",
        "06": "labor_force",
    }

    all_records = []

    # Validate the API key with a quick test request before using it
    use_key = False
    if BLS_API_KEY:
        try:
            test_payload = {
                "seriesid": ["LAUCN482010000000003"],
                "startyear": "2023", "endyear": "2024",
                "registrationkey": BLS_API_KEY,
            }
            tr = SESSION.post("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                              json=test_payload, timeout=30)
            tj = tr.json()
            if tj.get("status") == "REQUEST_SUCCEEDED":
                use_key = True
                print("    BLS API key validated ✓")
            else:
                print(f"    ⚠  BLS API key rejected — falling back to no-key mode "
                      f"(10-year spans, may be slower)")
        except Exception:
            print("    ⚠  BLS API key test failed — using no-key mode")

    # With key: 20-year chunks.  Without: 10-year chunks (BLS limit).
    if use_key:
        year_chunks = [(1990, 2009), (2010, 2024)]
    else:
        year_chunks = [(1990, 1999), (2000, 2009), (2010, 2019), (2020, 2024)]

    for fips, county_name in HOUSTON_COUNTIES.items():
        series_ids = [f"LAUCN{fips}00000000{mc}" for mc in measures]

        for start_yr, end_yr in year_chunks:
            payload = {
                "seriesid": series_ids,
                "startyear": str(start_yr),
                "endyear": str(end_yr),
            }
            if use_key:
                payload["registrationkey"] = BLS_API_KEY

            try:
                resp = SESSION.post("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                                    json=payload, timeout=60)
                resp.raise_for_status()
                result = resp.json()
            except Exception as e:
                print(f"    ⚠  BLS API error for {county_name} ({start_yr}-{end_yr}): {e}")
                continue

            if result.get("status") != "REQUEST_SUCCEEDED":
                msg = result.get("message", [""])[0] if result.get("message") else ""
                print(f"    ⚠  BLS API: {result.get('status')} for {county_name} "
                      f"({start_yr}-{end_yr}){': ' + msg[:80] if msg else ''}")
                continue

            for series in result.get("Results", {}).get("series", []):
                sid = series["seriesID"]
                measure_code = sid[-2:]
                measure_name = measures.get(measure_code, measure_code)
                for item in series.get("data", []):
                    if item.get("period") == "M13":  # annual average
                        all_records.append({
                            "fips": fips,
                            "county": county_name,
                            "year": int(item["year"]),
                            "measure": measure_name,
                            "value": float(item["value"].replace(",", "")),
                        })

    if not all_records:
        return pd.DataFrame()

    tidy = pd.DataFrame(all_records)
    piv = tidy.pivot_table(index=["fips", "county", "year"], columns="measure",
                           values="value", aggfunc="first").reset_index()
    piv.columns.name = None
    return piv.sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 7. FDIC — SUMMARY OF DEPOSITS
# ═══════════════════════════════════════════════════════════════════════════

def pull_fdic_sod() -> pd.DataFrame:
    """FDIC Summary of Deposits — aggregate deposits by county by year.
    Uses the FDIC BankFind API (api.fdic.gov).
    Filter syntax uses colons (FIELD:value), not EQ.
    Branch-level fields: STCNTYBR (branch county FIPS), DEPSUMBR (branch deposits)."""
    frames = []
    for year in range(1994, 2025):
        print(f"    FDIC SOD {year} …", end=" ")
        got_data_this_year = False
        for fips in HOUSTON_COUNTIES:
            # FDIC API uses colon filter syntax and branch-level county field
            url = (f"https://api.fdic.gov/banks/sod?"
                   f"filters=YEAR:{year}%20AND%20STCNTYBR:{fips}"
                   f"&fields=YEAR,STCNTYBR,NAMEFULL,DEPSUMBR"
                   f"&limit=10000&offset=0&output=json")
            r = _get(url)
            if r is None:
                continue
            try:
                data = r.json().get("data", [])
            except (json.JSONDecodeError, AttributeError):
                continue
            if data:
                got_data_this_year = True
                for row in data:
                    d = row.get("data", row)
                    frames.append({
                        "fips": str(d.get("STCNTYBR", fips)),
                        "year": year,
                        "institution": d.get("NAMEFULL", ""),
                        "deposits_thousands": d.get("DEPSUMBR", None),
                    })
        print("ok" if got_data_this_year else "skip")

    if not frames:
        return pd.DataFrame()

    df = pd.DataFrame(frames)
    df["fips"] = df["fips"].astype(str).str.zfill(5)
    df["county"] = df["fips"].map(HOUSTON_COUNTIES)
    df["deposits_thousands"] = pd.to_numeric(df["deposits_thousands"], errors="coerce")

    # Aggregate to county-year level
    agg = df.groupby(["fips", "county", "year"]).agg(
        total_deposits_thousands=("deposits_thousands", "sum"),
        num_institutions=("institution", "nunique"),
    ).reset_index()
    return agg.sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 8. FHFA — HOUSE PRICE INDEX
# ═══════════════════════════════════════════════════════════════════════════

def pull_fhfa_hpi() -> pd.DataFrame:
    """FHFA All-Transactions House Price Index by county.
    Uses the FHFA master CSV which contains all geographies and frequencies.
    We filter for county-level, non-seasonally-adjusted, annual data.
    Columns: hpi_type, hpi_flavor, frequency, level, place_name, place_id,
             yr, period, index_nsa, index_sa
    """
    # Primary: master CSV (all levels including county)
    url = "https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv"
    r = _get(url)
    if r is None:
        # Fallback: annual county-level XLSX
        r = _get("https://www.fhfa.gov/hpi/download/annual/hpi_at_county.xlsx")
        if r is not None:
            try:
                df = pd.read_excel(io.BytesIO(r.content), dtype={"FIPS code": str})
                fips_col = [c for c in df.columns if "fips" in c.lower()]
                if not fips_col:
                    return pd.DataFrame()
                fips_col = fips_col[0]
                df[fips_col] = df[fips_col].astype(str).str.zfill(5)
                df = df[df[fips_col].isin(HOUSTON_COUNTIES.keys())].copy()
                if df.empty:
                    return pd.DataFrame()
                df["county"] = df[fips_col].map(HOUSTON_COUNTIES)
                hpi_col = [c for c in df.columns if "hpi" in c.lower() or "index" in c.lower()]
                year_col = [c for c in df.columns if "year" in c.lower()]
                if not hpi_col or not year_col:
                    return pd.DataFrame()
                df["year"] = pd.to_numeric(df[year_col[0]], errors="coerce")
                df["hpi"] = pd.to_numeric(df[hpi_col[0]], errors="coerce")
                annual = df.groupby([fips_col, "county", "year"]).agg(
                    hpi_annual_avg=("hpi", "mean")).reset_index()
                return annual.rename(columns={fips_col: "fips"}).sort_values(
                    ["county", "year"]).reset_index(drop=True)
            except Exception as e:
                print(f"    ⚠ FHFA XLSX fallback failed: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(r.text), dtype={"place_id": str})

    # Filter for county-level data
    # level == "County or County Equivalent" (or similar)
    level_mask = df["level"].str.contains("(?i)county", na=False)
    df = df[level_mask].copy()

    # Filter for our Houston MSA counties by place_id (5-digit FIPS)
    df["place_id"] = df["place_id"].astype(str).str.zfill(5)
    df = df[df["place_id"].isin(HOUSTON_COUNTIES.keys())].copy()
    if df.empty:
        return pd.DataFrame()

    df["county"] = df["place_id"].map(HOUSTON_COUNTIES)
    df["year"] = pd.to_numeric(df["yr"], errors="coerce")

    # Prefer the non-seasonally adjusted index
    hpi_col = "index_nsa" if "index_nsa" in df.columns else "index_sa"
    df["hpi"] = pd.to_numeric(df[hpi_col], errors="coerce")

    # Average across all periods in each year to get annual figure
    annual = df.groupby(["place_id", "county", "year"]).agg(
        hpi_annual_avg=("hpi", "mean")).reset_index()
    return annual.rename(columns={"place_id": "fips"}).sort_values(
        ["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 9. ZILLOW — HOME VALUE INDEX (ZHVI)
# ═══════════════════════════════════════════════════════════════════════════

def pull_zillow_zhvi() -> pd.DataFrame:
    """Zillow ZHVI — typical home value by county (monthly → annual avg)."""
    url = ("https://files.zillowstatic.com/research/public_csvs/zhvi/"
           "County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
    r = _get(url)
    if r is None:
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(r.text), dtype={"StateCodeFIPS": str, "MunicipalCodeFIPS": str})
    df["fips"] = df["StateCodeFIPS"].str.zfill(2) + df["MunicipalCodeFIPS"].str.zfill(3)
    df = df[df["fips"].isin(HOUSTON_COUNTIES.keys())].copy()
    if df.empty:
        return pd.DataFrame()

    df["county"] = df["fips"].map(HOUSTON_COUNTIES)

    # Date columns are like "2000-01-31", "2000-02-29", etc.
    date_cols = [c for c in df.columns if re.match(r"\d{4}-\d{2}-\d{2}", c)]

    # Melt to long, extract year, average to annual
    melted = df.melt(id_vars=["fips", "county"], value_vars=date_cols,
                     var_name="date", value_name="zhvi")
    melted["year"] = pd.to_datetime(melted["date"]).dt.year
    melted["zhvi"] = pd.to_numeric(melted["zhvi"], errors="coerce")

    annual = melted.groupby(["fips", "county", "year"]).agg(
        zhvi_annual_avg=("zhvi", "mean")).reset_index()
    annual["zhvi_annual_avg"] = annual["zhvi_annual_avg"].round(0)
    return annual.sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 10. HUD — FAIR MARKET RENTS
# ═══════════════════════════════════════════════════════════════════════════

def pull_hud_fmr() -> pd.DataFrame:
    """HUD Fair Market Rents by county.
    Tries multiple URL patterns since HUD changes formats across years:
      1. XLSX county-level file (newer years)
      2. CSV with various naming conventions
      3. Revised CSV variants
    HUD FMR data is organized by fiscal year (FY). The FIPS columns and
    FMR column names vary across years.
    """
    frames = []
    # HUD doesn't provide downloadable county-level FMR files before FY2005.
    for fy in range(2005, 2027):
        print(f"    FMR FY{fy} …", end=" ")
        r = None
        file_type = "csv"

        # Try multiple URL patterns — HUD changes them frequently.
        # Key findings: recent years use 2-digit FY abbreviation and XLSX;
        # older years use /fmr{year}f/ path (with 'f' suffix) and XLS format.
        fy2 = str(fy)[-2:]  # 2-digit year (e.g. "26" for 2026)
        url_patterns = [
            # Recent years (2020+): XLSX with 2-digit FY abbreviation
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy2}_FMRs.xlsx", "xlsx"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy2}_FMRs_revised.xlsx", "xlsx"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy2}_4050_FMRs_rev.xlsx", "xlsx"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FMR{fy}_final_revised.xlsx", "xlsx"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy2}_4050_FMRs.xlsx", "xlsx"),
            # Full 4-digit year XLSX patterns
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy}_4050_FMR.xlsx", "xlsx"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy}_FMR.xlsx", "xlsx"),
            # CSV patterns (various capitalization and revision variants)
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy}_4050_FMR.csv", "csv"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/fy{fy}_4050_fmr.csv", "csv"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}/FY{fy}_4050_FMR_rev.csv", "csv"),
            # Older years: /fmr{year}f/ path with XLS format
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}f/FY{fy}_4050_Final.xls", "xls"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}f/FY{fy}_4050_FMR.xls", "xls"),
            (f"https://www.huduser.gov/datasets/fmr/fmr{fy}f/FMR_Area_{fy}_FMRs_CountyLevel.xls", "xls"),
            (f"https://www.huduser.gov/portal/datasets/fmr/fmr{fy}f/FY{fy}_Final.xls", "xls"),
        ]

        for url, ftype in url_patterns:
            r = _get(url, quiet=True)
            if r is not None:
                file_type = ftype
                break

        if r is None:
            print("skip")
            continue

        try:
            if file_type in ("xlsx", "xls"):
                # Some HUD files have multiple sheets; try first sheet
                xls_data = io.BytesIO(r.content)
                try:
                    df = pd.read_excel(xls_data, dtype=str, sheet_name=0)
                except Exception:
                    # Try with engine specified explicitly
                    xls_data.seek(0)
                    engine = "openpyxl" if file_type == "xlsx" else "xlrd"
                    df = pd.read_excel(xls_data, dtype=str, sheet_name=0, engine=engine)
            else:
                df = pd.read_csv(io.StringIO(r.text), dtype=str, on_bad_lines="skip")
        except Exception as exc:
            print(f"parse-error ({type(exc).__name__}: {str(exc)[:60]})")
            continue

        # Look for FIPS column — HUD uses varying names across years
        fips_col = None
        for c in df.columns:
            cl = c.lower().strip()
            if cl in ("fips", "fips2010", "fips2000", "county_code", "fips_code",
                       "countycode", "cntycode", "stcofips"):
                fips_col = c
                break
            if "fips" in cl:
                fips_col = c
                break

        if fips_col is None:
            # Some files embed FIPS in a combined area code column
            for c in df.columns:
                if "area" in c.lower() and "code" in c.lower():
                    fips_col = c
                    break

        if fips_col is None:
            print("no-fips-col")
            continue

        df[fips_col] = df[fips_col].astype(str).str.strip()
        # Some HUD files use 10-digit FIPS (state+county+subcounty) or longer codes
        if df[fips_col].str.len().max() > 5:
            df["county_fips"] = df[fips_col].str[:5]
        else:
            df["county_fips"] = df[fips_col].str.zfill(5)

        filt = df[df["county_fips"].isin(HOUSTON_COUNTIES.keys())].copy()
        if filt.empty:
            print("no-houston-rows")
            continue

        filt["county"] = filt["county_fips"].map(HOUSTON_COUNTIES)
        filt["fiscal_year"] = fy

        # Find FMR columns (typically fmr_0, fmr_1, fmr_2, fmr_3, fmr_4
        # or fmr0, fmr1, etc., or Efficiency, One-Bedroom, etc.)
        fmr_cols = [c for c in filt.columns if re.match(r"(?i)fmr_?\d", c)]
        if not fmr_cols:
            # Try bedroom-based column names
            fmr_cols = [c for c in filt.columns if re.match(
                r"(?i)(efficiency|one.?bed|two.?bed|three.?bed|four.?bed)", c)]

        if not fmr_cols:
            print("no-fmr-cols")
            continue

        for col in fmr_cols:
            filt[col] = pd.to_numeric(
                filt[col].astype(str).str.replace(",", "").str.replace("$", ""),
                errors="coerce")

        # Average across sub-areas to county level
        keep = ["county_fips", "county", "fiscal_year"] + fmr_cols
        agg = filt[keep].groupby(["county_fips", "county", "fiscal_year"]).mean(
            numeric_only=True).reset_index()
        agg = agg.rename(columns={"county_fips": "fips"})
        frames.append(agg)
        print("ok")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(
        ["county", "fiscal_year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 11. CENSUS — BUILDING PERMITS
# ═══════════════════════════════════════════════════════════════════════════

def pull_census_building_permits() -> pd.DataFrame:
    """Census Building Permits Survey — annual new residential units by county.
    Uses per-year CSV files from Census instead of the massive bulk file,
    which can hang on download. Each year's file is small and fast.
    Falls back to the Census BPS API if the CSV approach fails.
    """
    frames = []

    for year in range(2000, 2025):
        print(f"    Permits {year} …", end=" ")
        r = None

        # Try per-year county file (pipe-delimited or comma-delimited)
        for url in [
            f"https://www2.census.gov/econ/bps/County/co{year}a.txt",
            f"https://www2.census.gov/econ/bps/County/co{year}a.csv",
        ]:
            r = _get(url)
            if r is not None:
                break

        if r is None:
            print("skip")
            continue

        try:
            # Census BPS files use various delimiters
            text = r.text
            if "|" in text[:500]:
                df = pd.read_csv(io.StringIO(text), sep="|", dtype=str, on_bad_lines="skip")
            elif "\t" in text[:500]:
                df = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, on_bad_lines="skip")
            else:
                df = pd.read_csv(io.StringIO(text), dtype=str, on_bad_lines="skip")
        except Exception:
            print("parse-error")
            continue

        # Build FIPS from state + county codes
        state_col = [c for c in df.columns if "state" in c.lower() and "name" not in c.lower()
                     and "fips" not in c.lower()]
        county_col = [c for c in df.columns if "county" in c.lower() and "name" not in c.lower()
                      and "fips" not in c.lower()]

        # Also check for a combined FIPS column
        fips_col = [c for c in df.columns if "fips" in c.lower()]

        if fips_col:
            df["fips"] = df[fips_col[0]].astype(str).str.zfill(5)
        elif state_col and county_col:
            df["fips"] = (df[state_col[0]].astype(str).str.zfill(2) +
                          df[county_col[0]].astype(str).str.zfill(3))
        else:
            print("no-fips-cols")
            continue

        df = df[df["fips"].isin(HOUSTON_COUNTIES.keys())].copy()
        if df.empty:
            print("no-houston-rows")
            continue

        df["county"] = df["fips"].map(HOUSTON_COUNTIES)
        df["year"] = year

        # Look for units/buildings/value columns
        units_cols = [c for c in df.columns if any(term in c.lower() for term in
                      ["units", "bldgs", "buildings", "value"])]

        keep = ["fips", "county", "year"] + units_cols
        keep = [c for c in keep if c in df.columns]
        result = df[keep].copy()

        for c in units_cols:
            if c in result.columns:
                result[c] = pd.to_numeric(
                    result[c].astype(str).str.replace(",", ""), errors="coerce")

        frames.append(result)
        print("ok")

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 12. CENSUS ACS 5-YEAR — KEY DEMOGRAPHICS
# ═══════════════════════════════════════════════════════════════════════════

def pull_census_acs() -> pd.DataFrame:
    """Census ACS 5-year estimates — median income, poverty, education, housing."""
    if not CENSUS_API_KEY:
        print("    ⚠  No CENSUS_API_KEY — ACS pull requires an API key.")
        print("    Get one free at: https://api.census.gov/data/key_signup.html")
        return pd.DataFrame()

    # Key variables:
    # B19013_001E = median household income
    # B17001_001E = total pop for poverty status
    # B17001_002E = below poverty level
    # B15003_001E = total pop 25+ (education)
    # B15003_022E = bachelor's degree
    # B15003_023E = master's
    # B15003_024E = professional
    # B15003_025E = doctorate
    # B25077_001E = median home value
    # B25064_001E = median gross rent
    # B01002_001E = median age

    variables = "B19013_001E,B17001_001E,B17001_002E,B15003_001E,B15003_022E,B15003_023E,B15003_024E,B15003_025E,B25077_001E,B25064_001E,B01002_001E"

    county_codes = ",".join(f[-3:] for f in HOUSTON_COUNTIES)
    frames = []

    for year in range(2009, 2024):  # ACS 5-year available from ~2009
        url = (f"https://api.census.gov/data/{year}/acs/acs5"
               f"?get=NAME,{variables}"
               f"&for=county:{county_codes}"
               f"&in=state:{STATE_FIPS}"
               f"&key={CENSUS_API_KEY}")
        r = _get(url)
        if r is None:
            continue
        try:
            data = r.json()
        except json.JSONDecodeError:
            continue
        if not data or len(data) < 2:
            continue

        header = data[0]
        rows = data[1:]
        df = pd.DataFrame(rows, columns=header)
        df["year"] = year
        df["fips"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
        df["county_name"] = df["fips"].map(HOUSTON_COUNTIES)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Rename and compute derived metrics
    rename_map = {
        "B19013_001E": "median_household_income",
        "B17001_001E": "poverty_universe",
        "B17001_002E": "below_poverty",
        "B15003_001E": "pop_25plus",
        "B15003_022E": "bachelors",
        "B15003_023E": "masters",
        "B15003_024E": "professional_degree",
        "B15003_025E": "doctorate",
        "B25077_001E": "median_home_value",
        "B25064_001E": "median_gross_rent",
        "B01002_001E": "median_age",
    }
    combined = combined.rename(columns=rename_map)

    for col in rename_map.values():
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")

    # Compute poverty rate and bachelor's+ rate
    combined["poverty_rate_pct"] = (combined["below_poverty"] / combined["poverty_universe"] * 100).round(1)
    bachelors_plus = (combined[["bachelors", "masters", "professional_degree", "doctorate"]]
                      .fillna(0).sum(axis=1))
    combined["bachelors_plus_rate_pct"] = (bachelors_plus / combined["pop_25plus"] * 100).round(1)

    keep = ["fips", "county_name", "year", "median_household_income", "poverty_rate_pct",
            "bachelors_plus_rate_pct", "median_home_value", "median_gross_rent", "median_age"]
    return combined[keep].rename(columns={"county_name": "county"}).sort_values(
        ["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 13. CENSUS SAIPE — INCOME & POVERTY ESTIMATES
# ═══════════════════════════════════════════════════════════════════════════

def pull_census_saipe() -> pd.DataFrame:
    """Census Small Area Income and Poverty Estimates by county."""
    if not CENSUS_API_KEY:
        print("    ⚠  No CENSUS_API_KEY — SAIPE requires an API key.")
        return pd.DataFrame()

    # SAIPE variables:
    # SAEPOVALL_PT = all ages in poverty (estimate)
    # SAEPOVRTALL_PT = poverty rate (all ages)
    # SAEMHI_PT = median household income
    county_codes = ",".join(f[-3:] for f in HOUSTON_COUNTIES)
    frames = []

    for year in range(1989, 2024):
        url = (f"https://api.census.gov/data/timeseries/poverty/saipe"
               f"?get=NAME,SAEPOVALL_PT,SAEPOVRTALL_PT,SAEMHI_PT"
               f"&for=county:{county_codes}"
               f"&in=state:{STATE_FIPS}"
               f"&time={year}"
               f"&key={CENSUS_API_KEY}")
        r = _get(url)
        if r is None:
            continue
        try:
            data = r.json()
        except json.JSONDecodeError:
            continue
        if not data or len(data) < 2:
            continue
        df = pd.DataFrame(data[1:], columns=data[0])
        df["year"] = year
        df["fips"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
        df["county_name"] = df["fips"].map(HOUSTON_COUNTIES)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.rename(columns={
        "SAEPOVALL_PT": "poverty_count_all_ages",
        "SAEPOVRTALL_PT": "poverty_rate_all_ages",
        "SAEMHI_PT": "median_household_income",
    })
    for c in ["poverty_count_all_ages", "poverty_rate_all_ages", "median_household_income"]:
        if c in combined.columns:
            combined[c] = pd.to_numeric(combined[c], errors="coerce")

    return combined[["fips", "county_name", "year", "median_household_income",
                     "poverty_count_all_ages", "poverty_rate_all_ages"]].rename(
        columns={"county_name": "county"}).sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 14. CENSUS SAHIE — HEALTH INSURANCE ESTIMATES
# ═══════════════════════════════════════════════════════════════════════════

def pull_census_sahie() -> pd.DataFrame:
    """Census Small Area Health Insurance Estimates by county."""
    if not CENSUS_API_KEY:
        print("    ⚠  No CENSUS_API_KEY — SAHIE requires an API key.")
        return pd.DataFrame()

    # SAHIE variables:
    # NIC_PT = number insured
    # NUI_PT = number uninsured
    # PCTIC_PT = % insured
    # PCTUI_PT = % uninsured
    county_codes = ",".join(f[-3:] for f in HOUSTON_COUNTIES)
    frames = []

    for year in range(2008, 2024):
        url = (f"https://api.census.gov/data/timeseries/healthins/sahie"
               f"?get=NAME,NIC_PT,NUI_PT,PCTIC_PT,PCTUI_PT"
               f"&for=county:{county_codes}"
               f"&in=state:{STATE_FIPS}"
               f"&time={year}"
               f"&AGECAT=0&RACECAT=0&SEXCAT=0&IPRCAT=0"
               f"&key={CENSUS_API_KEY}")
        r = _get(url)
        if r is None:
            continue
        try:
            data = r.json()
        except json.JSONDecodeError:
            continue
        if not data or len(data) < 2:
            continue
        df = pd.DataFrame(data[1:], columns=data[0])
        df["year"] = year
        df["fips"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
        df["county_name"] = df["fips"].map(HOUSTON_COUNTIES)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.rename(columns={
        "NIC_PT": "insured_count", "NUI_PT": "uninsured_count",
        "PCTIC_PT": "pct_insured", "PCTUI_PT": "pct_uninsured",
    })
    for c in ["insured_count", "uninsured_count", "pct_insured", "pct_uninsured"]:
        if c in combined.columns:
            combined[c] = pd.to_numeric(combined[c], errors="coerce")

    return combined[["fips", "county_name", "year", "insured_count", "uninsured_count",
                     "pct_insured", "pct_uninsured"]].rename(
        columns={"county_name": "county"}).sort_values(["county", "year"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 15. CDC WONDER — BIRTH & DEATH RATES  (download requires manual query)
# ═══════════════════════════════════════════════════════════════════════════

def pull_cdc_wonder() -> pd.DataFrame:
    """
    CDC WONDER does not have a public bulk-download API.
    This function returns an empty DataFrame with instructions.
    The user can query interactively at https://wonder.cdc.gov/
    """
    print("    ℹ  CDC WONDER requires interactive queries — see Sources sheet for URL.")
    return pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════════════
# PULLS LIST
# ═══════════════════════════════════════════════════════════════════════════

PULLS = [
    ("Census PEP",           pull_census_pep,             "Annual population components of change (births, deaths, net migration)",
     "https://www.census.gov/programs-surveys/popest.html"),
    ("IRS SOI Flows",        pull_irs_soi,                "County-level tax return migration flows (inflow, outflow, net)",
     "https://www.irs.gov/statistics/soi-tax-stats-migration-data"),
    ("BEA Personal Income",  pull_bea_personal_income,    "Per-capita and total personal income by county",
     "https://www.bea.gov/data/income-spending/personal-income-county"),
    ("BEA GDP",              pull_bea_gdp,                "Gross domestic product by county and industry",
     "https://www.bea.gov/data/regional/gdp-county-metro"),
    ("BLS QCEW",             pull_bls_qcew,               "Employment, wages, and establishments (all industries)",
     "https://www.bls.gov/cew/"),
    ("BLS Unemployment",     pull_bls_laus,               "Unemployment rate, labor force, employment count",
     "https://www.bls.gov/lau/"),
    ("FDIC Deposits",        pull_fdic_sod,               "Total bank deposits and number of institutions by county",
     "https://www5.fdic.gov/sod/"),
    ("FHFA HPI",             pull_fhfa_hpi,               "All-transactions house price index (annual avg)",
     "https://www.fhfa.gov/DataTools/Downloads"),
    ("Zillow ZHVI",          pull_zillow_zhvi,            "Typical home value index (annual avg of monthly)",
     "https://www.zillow.com/research/data/"),
    ("HUD Fair Mkt Rents",   pull_hud_fmr,               "Fair Market Rents by bedroom size",
     "https://www.huduser.gov/portal/datasets/fmr.html"),
    ("Census Bldg Permits",  pull_census_building_permits, "New residential building permits and units authorized",
     "https://www.census.gov/construction/bps/"),
    ("Census ACS 5-Yr",      pull_census_acs,             "Median income, poverty, education, home value, rent, median age",
     "https://data.census.gov/"),
    ("Census SAIPE",         pull_census_saipe,           "Small Area Income and Poverty Estimates",
     "https://www.census.gov/programs-surveys/saipe.html"),
    ("Census SAHIE",         pull_census_sahie,           "Small Area Health Insurance Estimates",
     "https://www.census.gov/programs-surveys/sahie.html"),
    ("CDC WONDER",           pull_cdc_wonder,             "Birth/death rates by county (requires manual query)",
     "https://wonder.cdc.gov/"),
]


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def run_pull() -> bytes:
    """Run all data pulls and return an in-memory Excel workbook as bytes.

    The Excel sheet names match what macro_parser.py expects:
      - "Census PEP"          → parsed by _ws(["census", "pep"])
      - "IRS SOI Flows"       → parsed by _ws(["irs"])
      - "BEA Personal Income" → parsed by _ws(["bea", "income"])
      - "BEA GDP"             → parsed by _ws(["bea", "gdp"])
      - "BLS QCEW"            → parsed by _ws(["bls"])
      - "FDIC Deposits"       → parsed by _ws(["fdic"])
      - "Zillow ZHVI"         → parsed by _ws(["zillow"])
      - "HUD Fair Mkt Rents"  → parsed by _ws(["hud"])
    """
    import io as _io
    results = {}
    for sheet_name, func, description, url in PULLS:
        print(f"[data_puller] Running: {sheet_name}", flush=True)
        try:
            df = func()
        except Exception as e:
            print(f"[data_puller] ERROR in {sheet_name}: {e}", flush=True)
            df = pd.DataFrame()
        results[sheet_name] = df
        print(f"[data_puller] {sheet_name}: {len(df)} rows", flush=True)

    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in results.items():
            if not df.empty:
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    buf.seek(0)
    return buf.read()
