# Empty on purpose: its only job is to sit at the repo root so pytest's
# rootdir-insertion adds this directory to sys.path, which lets
# tests/test_hackathon_*.py resolve `from hackathon... import ...` without
# an installed package or a src/ layout.
