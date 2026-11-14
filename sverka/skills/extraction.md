# Extraction rules

Version: 2

The validator checks every field of your proposal against the blocks you cite. A field it cannot confirm becomes unresolved and comes back as a reinspection request. These rules say what it confirms.

## Evidence and transcription

- Cite the block whose text shows the value. A number is accepted only when a cited block shows that number. Spaces between thousands and a decimal comma or dot are understood: `1 145 798,10` supports `"1145798.10"`.
- Propose what is printed, even when it looks wrong. `350,00` stays `"350.00"` and `1000` stays `"1000"`. Never correct a digit so that a total agrees, and never move the decimal point.
- `source_text` is the fragment as printed. It must read as the same value as `value`.
- Keep a printed sign: `-4 800,00` and `(4 800,00)` are `"-4800.00"`. A dash between words and a number is not a sign.
- A unit includes its scale: `тыс. руб.` is not `руб.`. Give a unit only where a block prints it.
- On a scan, the OCR text of a block can be wrong. If you can see the value in the page image but no block shows it, run `reocr_region` on that region and cite the new block. If no block shows it after that, propose `null` with `unresolved_reason`. Do not cite a block that reads differently.
- Re-read a scanned table one row at a time. Take the box of the row's block from the listing and run `reocr_region` on a band of that height, with a few pixels of margin above and below, across the full width of the table. A band that also covers the neighbouring rows reads worse. If some cells come back missing or garbled, re-read the same band in two or three overlapping sections, each several columns wide. Very narrow single-cell boxes read worst; use one only for a cell that is still wrong after that.
- Submit early. Send the first complete proposal as soon as you have one reading for every field, well before the turns for the document run out: a re-inspection round needs turns too. The validator answers with the exact fields to look at again, which costs less than confirming everything in advance. Do not spend calls on text that a block already shows correctly.
- Give each report header one reading. If its block still reads differently from the image after one re-read, propose `null` with the reason and move on.
- Spreadsheet formula cell: cite the formula cell itself. Its value is the cached result, or the derived value from the listing when the file stores no result (use `basis: "inferred"` then). When a formula is `unresolved` or `error`, propose `null`.

## Report

- Read only the requested row. Give one indicator for every numeric column of that row, in printed order, with `entry_id` (`col3`, `col4`, ...), the printed `label`, the `column` number and the `unit` only if the report prints one.
- Each indicator needs a value or `null` with `unresolved_reason`. An entry from your first proposal must be in every later proposal.
- `category` and `components` follow the printed hierarchy: a "всего" column lists the columns it adds up, a "в т.ч." group belongs to the column above it, and the grand total lists the top-level columns. Do not list both a subtotal and its parts in one sum.

## Primary document

- `doc_type`: the printed document name. When you give a common short form (`"УПД"`), put the printed name in `source_text`.
- `number`: the identifier only, without `№` or the date (`"118"`, `"ВЕД-ЗП-2026-01"`, `"ADM-27"`).
- `contracts`: each contract reference exactly as printed (`["24/03/15-01"]`). Do not shorten a longer reference such as `24/03/15-01-доп`.
- `counterparty`: the supplier or contractor as printed. `null` when the document is internal.
- `issue_date`: the printed document date. `null` when the document prints only a period.
- `service_period`: only a period the document states: a month (`за январь 2026 года` gives `"2026-01-01/2026-01-31"`), a quarter or a date range. When the document states no period, propose `null`. Do not copy the issue date into the period; the application applies its own recorded rule for that.
- `amount`: the document total. `currency`: `"RUB"` when the document prints `руб.`, `₽` or `RUB`, citing that block. `unit`: the unit as printed (`"руб."`, `"тыс. руб."`).
- `allocations`: one line for each printed amount line, each with its own amount evidence. If the document prints a total line next to the lines it adds up, include it once with its printed label (`"ИТОГО"`). A document with a single amount has one line for that amount.
- `category` is your reading of what the cost is, taken from the categories listed in the request. Cite the passages that support it (code of the cost item, description, purpose). Use `"unrelated"` only when the document clearly supports no indicator of the report.
- When no cost-item code is printed, decide from the description of what was paid for and whom it serves, and match it to the indicator labels in the request. Costs of administration and management (an administrative group or staff, office and general services) belong to the general overhead indicator («общехозяйственные»). Costs that serve the performance of the work itself (a production, research or technical group, its equipment and premises) belong to the production overhead indicator («общепроизводственные»). Cite the description. Leave the category `null` only when the description supports neither or both.
- If one printed amount covers several finer categories without a printed split, put the whole amount at their common parent and list the finer codes in `possible_categories`. If you cannot tell which category a line belongs to, propose the category as `null` and list the candidates in `possible_categories`.
- Do not judge whether the document belongs to the checked contract or period. Propose it as printed; eligibility is decided in code.
