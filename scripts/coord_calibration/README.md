# Coordinate Calibration Scripts

This folder contains coordinate calibration and validation scripts.

- `calc_ai_coord_scale.py`:
  End-to-end AI closed-loop calibration. Generates tap error report and overlays.
- `save_coord_profile.py`:
  Writes per-device/per-model coordinate scale profile from calibration report.
- `test_coordinate_accuracy.py`:
  Runtime tap-path accuracy validation (non-AI baseline).

Default output directory for generated artifacts:

- `artifacts/coord_calibration/`

This keeps calibration outputs out of project root.
