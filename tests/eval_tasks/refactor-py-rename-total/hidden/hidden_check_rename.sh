#!/bin/sh
# 1) the old name is gone from every text file (pattern written so this script does not match itself)
old='calc''_tot'
if grep -rnI --exclude-dir=.git --exclude-dir=__pycache__ "$old" . ; then
    echo "old name still present"
    exit 1
fi
# 2) the new name is defined and used by the callers
grep -q "def compute_total(" billing/totals.py || { echo "compute_total not defined in billing/totals.py"; exit 1; }
for f in billing/invoice.py billing/quotes.py billing/reports.py billing/cli.py billing/__init__.py; do
    grep -q "compute_total" "$f" || { echo "$f does not use compute_total"; exit 1; }
done
# 3) behaviour: visible and hidden tests
python3 -m unittest discover -s tests -q || exit 1
python3 -m unittest discover -s hidden_tests -q || exit 1
