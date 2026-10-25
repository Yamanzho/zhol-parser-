# Changelog

## 0.2.0

- Company lookup uses indexed name and phone keys instead of reading every company in a city.
- One exact candidate is attached only when no other candidate is ambiguous. Confirmed platform ids stay on their company.
- `zhol rebuild-match-keys` rebuilds keys after a direct SQL load. It does not change companies.
- `zhol identity-report` prints unconfirmed snapshot pairs and does not write.
- CSV and JSON exports no longer stop at 5 000 rows. Excel stops at the worksheet limit of 1 048 575 data rows.
- Migration `b6d2f4a81c30` adds the key table and the indexes used by lookup, the newest snapshot, and the job queue. Downgrade drops those objects only.

## 0.1.0

- Versioned collection API, simplified panel, and localhost demo.
