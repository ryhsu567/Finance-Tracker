-- SPEC.md §4. Never store a subtotal or a percentage here — always derive
-- those at query time from these tables plus config/budgets.yml.

CREATE TABLE IF NOT EXISTS transactions (
  id            TEXT PRIMARY KEY,   -- hash(source_file, sheet, row)
  date          DATE NOT NULL,
  description   TEXT NOT NULL,
  amount        DECIMAL NOT NULL,   -- always positive; direction lives in kind
  kind          TEXT NOT NULL,      -- expense | income | transfer | investment
  category      TEXT NOT NULL,      -- canonical slug, see config/categories.yml
  note          TEXT,
  source_file   TEXT NOT NULL,
  source_sheet  TEXT NOT NULL,
  source_row    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_tags (
  transaction_id TEXT NOT NULL REFERENCES transactions(id),
  tag            TEXT NOT NULL,
  PRIMARY KEY (transaction_id, tag)
);

CREATE TABLE IF NOT EXISTS envelopes (
  tag            TEXT PRIMARY KEY,
  label          TEXT NOT NULL,
  budget         DECIMAL,
  start_date     DATE,
  end_date       DATE
);

CREATE TABLE IF NOT EXISTS budgets (
  scenario       TEXT NOT NULL,
  category       TEXT NOT NULL,
  monthly_target DECIMAL NOT NULL,
  effective_from DATE NOT NULL,
  PRIMARY KEY (scenario, category, effective_from)
);
