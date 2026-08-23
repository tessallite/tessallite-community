---
title: "Query Tessallite from a Jupyter Notebook"
audience: end user
area: analyst-guides
updated: 2026-08-04
---

## What this covers

For when the destination is not a chart but an analysis: pulling governed data from Tessallite into a Jupyter notebook with Python, shaping it with pandas, plotting it, and training a small machine-learning model on it — using the same sign-in and the same numbers as every BI tool in the building.

---

## Before you start

- Python 3 with Jupyter (or JupyterLab), and permission to `pip install` packages.
- Your Tessallite sign-in, the workspace slug, and the gateway address. On the local demo: `admin@acme-demo.com` / `acme-demo`, slug `acme-demo`, gateway `localhost`, port `5433`.
- The demo `modelx` model deployed, as with the rest of this section.

Install the two packages this page needs (plus two for the later steps):

```
pip install psycopg2-binary pandas matplotlib scikit-learn
```

---

## Why not connect straight to the source database?

You could — the credentials might even work. But three things would quietly change:

1. **Definitions.** Tessallite knows that `net_sales` means a specific governed calculation. A hand-written `SUM(...)` against raw tables is your private interpretation of it, and next quarter it will disagree with the official dashboard.
2. **Security.** Row security, column restrictions, and personas are applied by Tessallite before any data leaves. A direct connection bypasses all of it.
3. **Speed.** Queries through Tessallite are answered from pre-computed summaries when they can be. Your notebook gets the same acceleration as the executive dashboard.

Going through Tessallite means your experiment reconciles with the business's numbers — which is the difference between an analysis that changes a decision and one that gets debated.

---

## Step 1 — Connect

Tessallite speaks the PostgreSQL wire protocol on port `5433`, so the standard `psycopg2` driver connects directly:

```python
import psycopg2
import pandas as pd

conn = psycopg2.connect(
    host="localhost",        # or your gateway hostname
    port=5433,
    dbname="acme-demo",      # the workspace slug — case-sensitive
    user="admin@acme-demo.com",
    password="acme-demo",
)
```

To scope the connection to one model, extend the `dbname`: `"acme-demo/modelx"`.

---

## Step 2 — Your first governed dataframe

Query the model the way you would query any SQL table. The model appears as a table whose columns are its dimensions and measures:

```python
df = pd.read_sql(
    """
    SELECT product_name, SUM(net_sales) AS net_sales
    FROM modelx
    GROUP BY product_name
    ORDER BY net_sales DESC
    LIMIT 10
    """,
    conn,
)
df
```

Ten rows: the product leaderboard, computed by Tessallite (from a summary if one covers it), identical to the number Excel and Power BI would show. Note what you did **not** write: no joins, no table names from the source system, no metric definitions. The model supplies all three.

> **Tip: ask for grouped data, not raw rows.** Let Tessallite aggregate and bring back the summary (`GROUP BY` in the query), rather than pulling raw rows into pandas and summing them yourself. It is faster, kinder to the network, and it keeps the arithmetic where the definitions live.

---

## Step 3 — Plot it

```python
import matplotlib.pyplot as plt

df.plot(kind="barh", x="product_name", y="net_sales", legend=False)
plt.xlabel("Net sales")
plt.tight_layout()
plt.show()
```

A horizontal bar chart of the top ten products — the same leaderboard the Tableau walkthrough built, now reproducible in code and re-runnable every morning.

---

## Step 4 — A small machine-learning example

The notebook pattern that matters most for ML: **use governed measures as your features.** Then the training data agrees with the reports the business reads, and the model's results can be explained in the same vocabulary.

Here is a complete, honest little example — predicting a country's `net_sales` from its `transaction_count` and average transaction size, using ordinary least squares:

```python
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

features = pd.read_sql(
    """
    SELECT country_code,
           SUM(transaction_count) AS transactions,
           AVG(transaction_amount) AS avg_transaction,
           SUM(net_sales)         AS net_sales
    FROM modelx
    GROUP BY country_code
    """,
    conn,
).dropna()

X = features[["transactions", "avg_transaction"]]
y = features["net_sales"]

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
model = LinearRegression().fit(X_train, y_train)
print("R^2 on held-out countries:", model.score(X_test, y_test))
```

A few interpretations to keep you honest:

- The target and both features are **governed measures** — so "transactions" here means exactly what the sales dashboard means by it.
- This is deliberately tiny: a teaching example with a handful of countries, not a production forecast. Real work would add time features, more dimensions, and proper validation — but the data-sourcing pattern stays exactly this one.
- When the model eventually matters, the features can be recomputed on fresh data at any time by re-running the same query. No extract to babysit.

---

## Step 5 — Tidy up

```python
conn.close()
```

And a habit worth forming: keep the connection string in one cell at the top, never paste password cells into shared notebooks, and prefer environment variables or a secrets file for real credentials. The demo password above is public knowledge; yours is not.

---

## When the notebook becomes a product

If the end result of your experiment is something an application should call — a score, a forecast, a governed metric inside your own product — the notebook stops being the right delivery vehicle. That is what the [Headless API](../integrations/headless-api.md) is for: the same governed questions, answered as JSON, callable from any backend. The query you prototyped here transfers directly.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `connection refused` on port 5433 | Gateway not running, or wrong host | Confirm the host with your administrator; on a local install check the stack is up |
| `password authentication failed` | Wrong slug, email, or password | `dbname` is the workspace slug and is case-sensitive |
| `relation "modelx" does not exist` | The model is not deployed, or the connection is scoped elsewhere | Check the deployed models in the web app; adjust the `dbname` scoping |
| A column name is not found | Dimensions and measures use their model names | List them in the web app or query `SELECT * FROM modelx LIMIT 1` and inspect the columns |
| Results differ from a colleague's notebook | Different filters, or different personas | Personas legitimately change which rows you see — compare like for like |

---

## Related

- [Connect a BI Tool via JDBC](../getting-started/connect-a-bi-tool.md)
- [Headless API](../integrations/headless-api.md)
- [API Authentication](../integrations/api-authentication.md)
- [Why Your Numbers Match](why-your-numbers-match.md)

---

← [Build a Tableau Dashboard](build-a-tableau-dashboard.md) | [Home](../index.md) | [Why Your Numbers Match →](why-your-numbers-match.md)
