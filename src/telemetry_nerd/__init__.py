import os

# The daemon only solves small dense systems (ARL tables, fits). OpenBLAS sizes its thread pool to
# the host's cores, not the container's CPU quota; spinning workers then made the first SPC panel
# take seconds under CI load. Set before numpy is imported anywhere in the process.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
