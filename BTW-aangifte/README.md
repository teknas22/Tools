# Btw-aangifte

Calculates the Dutch VAT return (btw-aangifte, rubrics 1a–5g) from the sales invoices, purchase invoices and receipts you type in, for a month or a quarter.

**Open it:** https://teknas22.github.io/tools/BTW-aangifte/

Everything runs in your browser; nothing is sent anywhere.

- **Several companies:** add as many as you need. Each has its own VAT number, period, entries and filed returns. **Your companies** gives an overview of all of them.

- Rows show where they land in the return (1a, 3b, 4b, 5b…) and flag anything to check: a VAT amount that doesn't match the rate, a missing VAT ID on an EU sale, a date outside the period.
- **Finalise return** saves a filed return with its entries, filing date and Belastingdienst reference, and gives a PDF for your records.
- **Filed returns** lists every finalised return. You can download its PDF again, load its entries back, or export the whole archive as a backup.
- **Import** takes a CSV (Dutch or English headers, `;` or `,`). An example: [voorbeeld-invoer-2026-Q3.csv](voorbeeld-invoer-2026-Q3.csv).

Entries and filed returns are kept in this browser only. Clearing site data removes them, so keep the PDFs and use **Export all records** now and then. The PDF and Excel exports load their library from cdnjs the first time they're used.

Check the result before you file it in Mijn Belastingdienst Zakelijk. Not covered: margin schemes, OSS, private use (1d), and pro-rata deduction beyond a fixed percentage.

Source and tests: `teknas22/allseas`, folder `22-NL-VAT-declaration-tool`. That repo has the Python engine, and a test that keeps this page's calculation identical to the Python one.
