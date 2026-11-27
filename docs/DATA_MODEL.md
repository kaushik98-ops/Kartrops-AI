# Data model (what your systems must provide)

KartOps reads operational facts from one DynamoDB table (`DATA_TABLE`, single-table design). Each item is
`{pk, sk, json}` where `json` is the entity below, serialised. `samples/seed.json` is a complete example and
`kartops seed` loads it. In production, feed these from your OMS / ERP / CRM via an ETL or DynamoDB Streams.

| Entity | pk | sk | Key fields |
|---|---|---|---|
| Order | `ORDER#<order_id>` | `META` | `order_id, customer_id, seller_id?, status, tracking_status, awb` |
| Tracking | `AWB#<awb>` | `META` | `status, last_scan` |
| Customer | `CUSTOMER#<id>` | `META` | `tier, contact_count_30d` (maintained by your CRM) |
| Vendor master | `VENDOR#<id>` | `MASTER` | `name, gstin, bank_account, ifsc, payment_terms_days, emails[], phones[]` |
| Vendor e-mail index | `VENDOREMAIL#<lower-case email>` | `META` | `vendor_id` (written by `seed` from `emails[]`) |
| Purchase order | `PO#<po_id>` **and** `VENDOR#<vendor_id>` / `PO#<po_id>` | `META` | `po_id, vendor_id, status(open/closed), promised_ship, need_by, criticality, buffer_days, transit_days, lines[{sku,qty,unit_price}]` |
| GRN | `GRN#<po_id>` | `META` | `lines[{sku,qty_received}]` |
| Payment history | `VENDOR#<id>` | `PAY#<invoice_no>` | `invoice_no, total, invoice_date, status` |
| Scorecard | `VENDOR#<id>` | `SCORECARD` | `on_time_pct` |
| Inventory | `SKU#<sku>` | `INVENTORY` | `on_hand, daily_sales` |
| Sale calendar | `CALENDAR#sales` | `META` | `events[{name,start,end}]` |

Bank account numbers live only here and in code. The model only ever sees the last four digits.

`OPS_TABLE` (written by the system) holds `ITEM#<id>` rows: `STATE` (queue + status; GSI `gsi1` powers the review
queues), `AUDIT#<ts>#<event>`, `DRAFT#<id>`, `FLAG#...`, `HOLD`, `HOLD_WORKFLOW`.
`CHECKPOINT_TABLE` is owned by `langgraph-checkpoint-aws` (keys `PK`/`SK`, TTL attribute `ttl`).
