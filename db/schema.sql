CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  stock INTEGER NOT NULL,
  price_cents INTEGER NOT NULL
);

INSERT INTO products (sku, name, stock, price_cents) VALUES
  ('VL-1001', 'Voltra Wireless Earbuds', 42, 7999),
  ('VL-1002', 'Voltra USB-C Hub', 18, 4599),
  ('VL-1003', 'Voltra Laptop Sleeve', 67, 2499),
  ('VL-1004', 'Voltra Desk Lamp', 11, 5499)
ON CONFLICT (sku) DO NOTHING;

-- Grant schema access
GRANT USAGE ON SCHEMA public TO PUBLIC;

-- CRITICAL: Grant table permissions so dynamic users can access existing tables
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO PUBLIC;