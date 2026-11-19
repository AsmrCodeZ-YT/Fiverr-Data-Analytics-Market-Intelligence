# Fiverr Data Analytics Market Intelligence — All Dashboard SQL Queries

**Source dashboard:** Fiverr Data Analytics Market Intelligence (7 pages, 14 datasets, 28 widgets)
**Source tables:** `demo.market_intelligence.fiverr_gig_market_features` (294 rows, 1 row per gig) and `demo.market_intelligence.fiverr_technology_metrics` (42 rows)
**SQL warehouse:** Serverless Starter Warehouse (`ad83d98611848da5`) — connect Power BI Desktop via Get Data → Databricks, paste each query below as an import/DirectQuery statement.

Notes:
* All buyer/price metrics derived from reviews are SAMPLED (1,241 reviews vs total counts) — keep the "sampled" labels.
* `PERCENTILE_APPROX` runs on the Databricks warehouse, so it works when Power BI sends the SQL to Databricks (do not rewrite for a local engine).
* No joins needed — every dataset reads a single table.

---

## Page 1 — Market Overview

### Dataset: `market_overview`
Feeds counters: Gigs, Unique sellers, Avg gig rating, Median reviews per gig, Median starting price (USD), Business buyers % (sampled)

```sql
SELECT COUNT(*) AS gigs,
       COUNT(DISTINCT seller_username) AS unique_sellers,
       ROUND(AVG(gig_rating), 2) AS avg_gig_rating,
       PERCENTILE_APPROX(gig_review_count, 0.5) AS median_gig_review_count,
       PERCENTILE_APPROX(starting_price, 0.5) AS median_starting_price_usd,
       ROUND(AVG(business_buyer_share) * 100, 1) AS business_buyer_share_pct_sampled,
       ROUND(AVG(repeat_buyer_share) * 100, 1) AS repeat_buyer_share_pct_sampled
FROM demo.market_intelligence.fiverr_gig_market_features
```

---

## Page 2 — Demand Analysis

### Dataset: `demand_by_family`
Feeds bar chart: Total reviews by service family

```sql
SELECT service_family,
       COUNT(*) AS gigs,
       SUM(gig_review_count) AS total_reviews,
       PERCENTILE_APPROX(gig_review_count, 0.5) AS median_reviews,
       PERCENTILE_APPROX(gig_review_count, 0.9) AS p90_reviews,
       MAX(latest_sampled_review_date) AS most_recent_sampled_review
FROM demo.market_intelligence.fiverr_gig_market_features
GROUP BY service_family
ORDER BY total_reviews DESC
```

### Dataset: `review_bands`
Feeds bar chart: Gigs by review band

```sql
SELECT gig_review_band,
       COUNT(*) AS gigs,
       SUM(gig_review_count) AS total_reviews_in_band
FROM demo.market_intelligence.fiverr_gig_market_features
GROUP BY gig_review_band
ORDER BY MIN(gig_review_count)
```

### Dataset: `top20_gigs`
Feeds table: Top 20 gigs by review count

```sql
SELECT title, gig_url, service_family, seller_level, gig_review_count, gig_rating, starting_price
FROM demo.market_intelligence.fiverr_gig_market_features
ORDER BY gig_review_count DESC
LIMIT 20
```

---

## Page 3 — Competition Analysis

### Dataset: `category_competition`
Feeds table: Category competition overview + bar chart: Total reviews by category

```sql
SELECT nested_sub_category AS category,
       MAX(category_gig_count) AS gig_count,
       MAX(category_seller_count) AS unique_sellers,
       SUM(gig_review_count) AS total_reviews,
       MAX(category_review_median) AS median_reviews_per_gig,
       MAX(category_review_p90) AS p90_reviews_per_gig,
       ROUND(MAX(category_review_median) / MAX(category_gig_count), 1) AS demand_per_gig_ratio,
       ROUND(MAX(category_promoted_share) * 100, 1) AS promoted_share_pct,
       ROUND(MAX(category_top10_review_share) * 100, 1) AS top10_review_concentration_pct
FROM demo.market_intelligence.fiverr_gig_market_features
GROUP BY nested_sub_category
ORDER BY total_reviews DESC
```

### Dataset: `promoted_vs_not`
Feeds table: Promoted vs non-promoted gigs

```sql
SELECT is_promoted,
       COUNT(*) AS gigs,
       COUNT(DISTINCT seller_username) AS unique_sellers,
       PERCENTILE_APPROX(gig_review_count, 0.5) AS median_reviews,
       PERCENTILE_APPROX(starting_price, 0.5) AS median_starting_price_usd
FROM demo.market_intelligence.fiverr_gig_market_features
GROUP BY is_promoted
```

---

## Page 4 — Pricing Analysis

### Dataset: `package_prices_by_family`
Feeds table: Package prices by service family (medians, USD)

```sql
SELECT service_family,
       PERCENTILE_APPROX(basic_price, 0.5) AS median_basic_price,
       PERCENTILE_APPROX(standard_price, 0.5) AS median_standard_price,
       PERCENTILE_APPROX(premium_price, 0.5) AS median_premium_price,
       ROUND(AVG(package_price_ratio), 2) AS avg_premium_to_basic_ratio,
       PERCENTILE_APPROX(starting_price, 0.25) AS starting_price_p25,
       PERCENTILE_APPROX(starting_price, 0.5) AS starting_price_median,
       PERCENTILE_APPROX(starting_price, 0.75) AS starting_price_p75
FROM demo.market_intelligence.fiverr_gig_market_features
GROUP BY service_family
ORDER BY starting_price_median DESC
```

### Dataset: `order_price_bands`
Feeds bar chart: Gigs by reported order price band (sampled reviews)

```sql
WITH priced AS (
  SELECT review_order_price_midpoint_median AS mid
  FROM demo.market_intelligence.fiverr_gig_market_features
  WHERE review_order_price_midpoint_median IS NOT NULL
)
SELECT CASE WHEN mid < 50 THEN 'a. < $50'
            WHEN mid < 100 THEN 'b. $50 - $100'
            WHEN mid < 200 THEN 'c. $100 - $200'
            WHEN mid < 400 THEN 'd. $200 - $400'
            ELSE 'e. $400+' END AS sampled_order_price_band,
       COUNT(*) AS gigs,
       ROUND(AVG(mid), 0) AS avg_reported_midpoint_usd
FROM priced
GROUP BY 1
ORDER BY 1
```

---

## Page 5 — Buyer Intelligence

### Dataset: `buyer_metrics_by_family`
Feeds table: Sampled buyer metrics by service family

```sql
SELECT service_family,
       COUNT(*) AS gigs_with_sampled_reviews,
       ROUND(AVG(business_buyer_share) * 100, 1) AS business_buyer_share_pct,
       ROUND(AVG(repeat_buyer_share) * 100, 1) AS repeat_buyer_share_pct,
       PERCENTILE_APPROX(median_order_duration_days, 0.5) AS median_order_duration_days
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE sampled_review_count > 0
GROUP BY service_family
ORDER BY business_buyer_share_pct DESC
```

### Dataset: `buyer_countries`
Feeds table: Top buyer countries (per gig sample)

```sql
SELECT top_buyer_country,
       COUNT(*) AS gigs,
       SUM(gig_review_count) AS total_reviews
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE top_buyer_country IS NOT NULL
GROUP BY top_buyer_country
ORDER BY gigs DESC
```

### Dataset: `buyer_industries`
Feeds table: Top buyer industries

```sql
SELECT top_buyer_industry,
       COUNT(*) AS gigs,
       SUM(sampled_review_count) AS sampled_reviews
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE top_buyer_industry IS NOT NULL
GROUP BY top_buyer_industry
ORDER BY gigs DESC
```

---

## Page 6 — Technology Demand

### Dataset: `technology_demand`
Feeds table: Top 25 technologies

```sql
SELECT technology,
       gig_count,
       median_gig_review_count,
       median_starting_price,
       ROUND(promoted_gig_share * 100, 1) AS promoted_share_pct
FROM demo.market_intelligence.fiverr_technology_metrics
ORDER BY gig_count DESC
LIMIT 25
```

### Dataset: `tech_combinations`
Feeds table: Technology combinations

```sql
SELECT 'Python + SQL' AS technology_combination,
       COUNT(*) AS gigs,
       PERCENTILE_APPROX(gig_review_count, 0.5) AS median_reviews,
       PERCENTILE_APPROX(starting_price, 0.5) AS median_price_usd
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE python_flag AND sql_flag
UNION ALL
SELECT 'Power BI + SQL', COUNT(*),
       PERCENTILE_APPROX(gig_review_count, 0.5),
       PERCENTILE_APPROX(starting_price, 0.5)
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE powerbi_flag AND sql_flag
UNION ALL
SELECT 'Python + Machine Learning', COUNT(*),
       PERCENTILE_APPROX(gig_review_count, 0.5),
       PERCENTILE_APPROX(starting_price, 0.5)
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE python_flag AND machine_learning_flag
UNION ALL
SELECT 'Excel + Power BI', COUNT(*),
       PERCENTILE_APPROX(gig_review_count, 0.5),
       PERCENTILE_APPROX(starting_price, 0.5)
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE excel_flag AND powerbi_flag
UNION ALL
SELECT 'Python + Tableau', COUNT(*),
       PERCENTILE_APPROX(gig_review_count, 0.5),
       PERCENTILE_APPROX(starting_price, 0.5)
FROM demo.market_intelligence.fiverr_gig_market_features
WHERE python_flag AND tableau_flag
ORDER BY median_reviews DESC
```

---

## Page 7 — Gig Explorer

### Dataset: `gig_explorer`
Feeds table: Top 100 gigs by review count

```sql
SELECT title, gig_url, service_family, seller_level, gig_review_count, gig_rating,
       starting_price, basic_price, standard_price, premium_price, technology_list, is_promoted
FROM demo.market_intelligence.fiverr_gig_market_features
ORDER BY gig_review_count DESC
LIMIT 100
```
