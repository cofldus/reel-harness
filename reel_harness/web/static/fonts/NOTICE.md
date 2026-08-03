# Bundled webfonts

These files are vendored rather than loaded from a CDN on purpose. Fable
Studio is a local-first tool that has to render correctly with no network,
and a stylesheet that reaches out to a third-party host on every page load
would also leak when and how often the tool is used.

Both families are by SUNN and released under the SIL Open Font License
1.1; the full licence text for each sits beside the font.

## SUITE — display

- File: `SUITE-Variable.woff2` (variable, weight 300–800)
- Upstream: https://github.com/sun-typeface/SUITE
- Licence: `LICENSE-SUITE.txt`

Used only for the wordmark and page/step headings — roughly a fifth of
the text on screen. It has more character than a neutral UI grotesque,
which is what keeps the product from reading as a generic dashboard,
but it is still a sans: the earlier serif experiment made a production
tool look like a literary magazine.

## SUIT — text

- File: `SUIT-Variable.woff2` (variable, weight 300–800)
- Upstream: https://github.com/sun-typeface/SUIT
- Licence: `LICENSE-SUIT.txt`

Everything else: body copy, cards, forms, buttons, badges, shot metadata,
and all numerals. Numbers that are compared or stacked — costs, budgets,
durations — are set with `tabular-nums` so they align in a column.
