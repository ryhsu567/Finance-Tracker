# Budget Dashboard

Consolidating several personal budget spreadsheets into one local dashboard.
Source data is private financial data — never commit `data/raw/`.

## Read first
`docs/SPEC.md` contains the target schema, the three-axis transaction model
(kind / category / tags), the category taxonomy with cadences, six documented bugs
in the source sheets, and open questions. Read it before writing any code.

## Where things are
- `data/raw/` — exported spreadsheets, read-only, gitignored
- `docs/SPEC.md` — the spec
- `config/` — categories.yml, budgets.yml, envelopes.yml (hand-edited, not generated)

## Hard rules
- The `Expenses` and `Income` tabs are the only source of truth. Every other sheet
  and tracker card is derived output — regenerate it, never import it.
- The loader must FAIL LOUDLY on any category not in `config/categories.yml`.
  A silently dropped transaction is the bug this project exists to fix.
- Never store a computed subtotal. Always derive it.
- Budgets are time-versioned. Correcting a target must not rewrite history.
- Ask before inventing a category mapping — open questions are in SPEC.md §7.

## Conventions
- Dates parse as `M/D/YY` with an explicit format string. Never let a library guess.
- Amounts are always positive; direction lives in `kind`.
- Annualize as `yearly / 12` everywhere.
