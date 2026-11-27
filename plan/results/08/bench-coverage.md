## coverage_locate on Python bug-fix tasks (the hidden failing check; the project's own passing tests when present)

Fault function (changed by the reference fix) in the top 5: 67% of 3 tasks.

- fix-py-catalog-extra-page: rank 2 (0.22 s, 1 passing run(s)); fault [('catalog/paging.py', 'page_count')]; top ['catalog/listing.py:paginate', 'catalog/paging.py:page_count', 'catalog/listing.py:Page.has_next']
- fix-py-stocksync-batch-leak: rank 6 (0.24 s, 1 passing run(s)); fault [('stocksync/util.py', 'deep_merge')]; top ['stocksync/jobs.py:run_batch', 'stocksync/filters.py:sort_items', 'stocksync/inventory.py:StockItem.value']
- py-fix-test: rank 1 (0.22 s, 0 passing run(s)); fault [('temps.py', 'to_fahrenheit')]; top ['temps.py:to_fahrenheit', 'temps.py:clamp_setpoint']

