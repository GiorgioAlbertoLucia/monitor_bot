# stress_cpu.py
# Saturate the CPU for a bounded time, to check that the bot's CPU alert fires
# and that the dashboard chart / top-users card pick it up.
#
# Workers run at nice 19 by default: psutil still counts niced time as busy
# (so the alert triggers), but any real job on this shared machine takes
# priority over the burn loops.
#
#   python tests/stress_cpu.py                      # all cores, 100%, 60 s
#   python tests/stress_cpu.py -d 120 -l 95         # all cores at ~95% for 2 min
#   python tests/stress_cpu.py -c 8 -d 10           # 8 cores, 10 s (smoke test)
import argparse
import multiprocessing as mp
import os
import signal
import time

SLICE = 0.1  # seconds per busy/idle cycle when --load < 100


def burn(deadline, load, niceness):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # parent handles Ctrl-C
    if niceness:
        os.nice(niceness)
    busy = SLICE * load / 100
    while time.time() < deadline:
        start = time.time()
        while time.time() - start < busy:
            pass
        if load < 100:
            time.sleep(SLICE - busy)


def main():
    ncpu = os.cpu_count() or 1
    parser = argparse.ArgumentParser(description="Saturate the CPU for a limited time.")
    parser.add_argument("-c", "--cores", type=int, default=ncpu,
                        help=f"number of worker processes (default: all {ncpu})")
    parser.add_argument("-d", "--duration", type=float, default=60,
                        help="seconds to run (default: 60, max: 600)")
    parser.add_argument("-l", "--load", type=float, default=100,
                        help="target load per core in %% (default: 100)")
    parser.add_argument("-n", "--nice", type=int, default=19,
                        help="niceness of the workers, 0 = normal priority (default: 19)")
    args = parser.parse_args()

    if not 1 <= args.cores <= ncpu:
        parser.error(f"--cores must be between 1 and {ncpu}")
    if not 0 < args.duration <= 600:
        parser.error("--duration must be in (0, 600] seconds")
    if not 0 < args.load <= 100:
        parser.error("--load must be in (0, 100]")

    deadline = time.time() + args.duration
    print(f"Burning {args.cores}/{ncpu} cores at {args.load:.0f}% for {args.duration:.0f} s "
          f"(nice {args.nice}). Ctrl-C to stop early.")

    workers = [mp.Process(target=burn, args=(deadline, args.load, args.nice), daemon=True)
               for _ in range(args.cores)]
    for w in workers:
        w.start()
    try:
        for w in workers:
            w.join()
    except KeyboardInterrupt:
        print("\nStopping workers...")
    finally:
        for w in workers:
            if w.is_alive():
                w.terminate()
        for w in workers:
            w.join()
    print("Done.")


if __name__ == "__main__":
    main()
