PYTHON ?= python3

.PHONY: test compile install doctor

test:
	$(PYTHON) -m unittest -v

compile:
	$(PYTHON) -m py_compile mps_pressure_dashboard.py pc008_control.py pc008_experiment.py plot_pc008_waterblock.py

install:
	./install.sh

doctor:
	./doctor.sh
