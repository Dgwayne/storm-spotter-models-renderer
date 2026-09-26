"""Byte ranges for a subset of GRIB messages, from a NOAA-style .idx.

Usage: idx_ranges.py <full.idx> <selected.idx-lines>

Prints, on line 1, how many lines of the full idx belong to the selected
messages (the count `wgrib2 -s` must report for the assembled file), then
one curl `-r` range per line. A message ends where the next GREATER
offset starts (subfield lines like 624.1/624.2 share their parent's
offset); the last message runs to EOF ("off-"). Strictly adjacent
messages merge into one range: same bytes, fewer requests.
"""
import sys


def offsets(path):
    out = []
    for line in open(path):
        parts = line.split(":")
        if len(parts) >= 3:
            out.append(int(parts[1]))
    return out


def main():
    full = offsets(sys.argv[1])
    uniq = sorted(set(full))
    want = sorted(set(offsets(sys.argv[2])))
    end_of = {o: (uniq[i + 1] - 1 if i + 1 < len(uniq) else None)
              for i, o in enumerate(uniq)}
    runs = []
    for o in want:
        e = end_of[o]
        if runs and runs[-1][1] is not None and runs[-1][1] + 1 == o:
            runs[-1][1] = e
        else:
            runs.append([o, e])
    wset = set(want)
    print(sum(1 for o in full if o in wset))
    for s, e in runs:
        print(f"{s}-{'' if e is None else e}")


if __name__ == "__main__":
    main()
