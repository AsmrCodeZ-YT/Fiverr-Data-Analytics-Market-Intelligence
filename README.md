# Fiverr Market Intelligence (Databricks)

End-to-end Databricks project that transforms scraped Fiverr data-analytics gig data into curated Delta tables and dashboard-ready SQL.

## Components

- **`Fiverr Market Intelligence Feature Engineering.ipynb`** (PySpark): ingests raw data from Unity Catalog Volumes, validates/normalizes/flattens it, classifies service families, and engineers demand, pricing/package, sampled buyer/review, seller/competition, and technology features; writes curated Delta tables.
- **`fiverr-total-gigs-info.ipynb`**: quick inspection of the raw gig snapshot.
- **`fiverr_dashboard_sql_queries.md`**: Databricks SQL queries for a 7-page dashboard (Market Overview, Demand, Competition, Pricing, Buyer Intelligence, Technology Demand, Gig Explorer).
- **`pdf/`**: exported dashboard views (`Dark.pdf`, `light.pdf`).

## Data

- **Inputs:** `/Volumes/demo/bronze/fiverr/summary.csv`, `/Volumes/demo/bronze/fiverr/all_gigs.json`
- **Outputs (Delta):**
  - `demo.market_intelligence.fiverr_gig_market_features` — one row per gig
  - `demo.market_intelligence.fiverr_technology_metrics` — technology-level aggregates

## Notes

- Runs on Databricks (Spark, `%sql`, Unity Catalog Volumes/tables, Databricks SQL warehouse).
- Review/buyer/order-price metrics are derived from **sampled** reviews, not full transaction history.
