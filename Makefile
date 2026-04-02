SERVICE     := human_detect_led
UNIT_FILE   := $(SERVICE).service
INSTALL_DIR := $(shell pwd)
PYTHON      := python3

.PHONY: install start stop restart status logs test uninstall

install:
	@echo "Installing dependencies..."
	$(PYTHON) -m pip install -r requirements.txt
	@echo "Installing optional dependencies..."
	$(PYTHON) -m pip install -r requirements-optional.txt || true
	@echo "Installing systemd service..."
	sudo cp $(UNIT_FILE) /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable $(SERVICE)
	@echo ""
	@echo "Done. Run 'make start' to start the service."

start:
	sudo systemctl start $(SERVICE)
	@echo "Service started. Open http://$$(hostname -I | awk '{print $$1}'):5000"

stop:
	sudo systemctl stop $(SERVICE)

restart:
	sudo systemctl restart $(SERVICE)

status:
	sudo systemctl status $(SERVICE)

logs:
	sudo journalctl -u $(SERVICE) -f --no-pager

test:
	$(PYTHON) qa_test.py

test-full:
	$(PYTHON) qa_full.py

uninstall:
	sudo systemctl stop $(SERVICE) || true
	sudo systemctl disable $(SERVICE) || true
	sudo rm -f /etc/systemd/system/$(UNIT_FILE)
	sudo systemctl daemon-reload
	@echo "Service uninstalled."
