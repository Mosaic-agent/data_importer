# Market Data & Pricing Rules

Strict domain mandates governing market data authenticity and calculations.

## 1. Zero LLM Calculations
- NEVER estimate, average, or calculate financial metrics (PE, CAGR, sums, weights) within reasoning.
- All numbers must originate from tool output or SQL results and be reported verbatim.

## 2. Indian Equity Price Authority
- Secondary market Indian stock and ETF prices must use Shoonya (Finvasia) or NSE.
- NEVER rely on Yahoo Finance for Indian ETF secondary prices.
- NEVER fall back to declared AMC NAV as secondary market LTP (severely distorts premium/discount, especially for international ETFs subject to RBI limits).

## 3. ETF Premium / Discount Calculation
- Always pair live real-time indicative NAV (`iNAV`) published by the AMC with real-time secondary market LTP.
- NEVER measure premium/discount against static NSE EOD declared NAV (creates phantom arbitrage signals).

## 4. Promoter Dilution vs Promoter Sale
A reduction in promoter percentage is NOT necessarily a promoter sale:
- **Dilution**: Percentage decreases while absolute share count remains unchanged (QIP, rights issue, preferential allotment, ESOP exercises, M&A share issuance).
- **Sale**: Percentage decreases AND absolute share count decreases (red flag).
- Before concluding a promoter sale, verify total shares outstanding in the filing.

## 5. DSP Active Fund Holdings Conviction
- Active DSP funds (`DSP_SMALL_CAP`, `DSP_MID_CAP`, `DSP_FLEXI_CAP`, etc.) represent the primary institutional signal.
- Cross-ownership across 2+ active DSP funds for 24+ months indicates highest conviction.
- Ignore passive index-tracking funds (`DSP_NIFTY_*_INDEX`, `DSP_BSE_*_ETF`).
- Filter: `market_data.mf_holdings FINAL WHERE fund_name LIKE 'DSP%'`.

## 6. Regulatory Source Hierarchy
- **Tier 1 (Authoritative)**: SEBI monthly portfolio filings (`.xlsx`/`.xml`), AMFI disclosures, NSE official corporate feeds.
- **Tier 2 (Collateral)**: Marketing factsheets (PDFs), third-party web scrapes. Cross-verify Tier 2 against Tier 1.
