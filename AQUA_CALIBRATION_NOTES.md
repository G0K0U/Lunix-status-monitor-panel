# Aqua calibration notes

The supplied `calibration_curve_raw_to_mbar.xml` is the exported Aqua
Computer aquasuite RAW→mbar curve. The default curve is linear with no offset;
changing it in aquasuite overwrites the curve stored in the device. The
automatic zero-point calibration applies to MPS flow variants, not this
pressure sensor.

The dashboard loads the XML at startup, applies piecewise-linear interpolation
to `pressure_normalized_raw`, clamps outside the exported range, and retains
all raw report fields for verification against a known reference pressure.
