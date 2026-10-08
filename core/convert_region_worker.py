"""Internal worker for PS3ToPC. Not intended for direct user interaction."""
import sys
from ps3_converter import run_region_worker

if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: convert_region_worker.py GAMEDATA REGION_NAME OUTPUT_DIR")
    run_region_worker(sys.argv[1], sys.argv[2], sys.argv[3])
