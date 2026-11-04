PYTHON ?= python3

.PHONY: test compile doctor status dry-run

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

compile:
	$(PYTHON) -m compileall -q core adapters storage tests trinity.py

doctor:
	$(PYTHON) trinity.py doctor

status:
	$(PYTHON) trinity.py status

dry-run:
	$(PYTHON) trinity.py run --dry-run "sửa lỗi pytest trong module python"
