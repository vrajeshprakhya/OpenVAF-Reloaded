import sys

SIM = "."
rows = []
for line in open(f"{SIM}/sh_out.txt"):
    p = line.split()
    if len(p) < 6:
        continue
    try:
        t, vin, vout, vsmpl = float(p[0]), float(p[1]), float(p[3]), float(p[5])
    except ValueError:
        continue
    rows.append((t, vin, vout, vsmpl))

print(f"rows: {len(rows)}   t: {rows[0][0]:.3g} .. {rows[-1][0]:.3g} s")

# rising crossings of the 2.5 V threshold on smpl
crossings = [
    rows[i][0]
    for i in range(1, len(rows))
    if rows[i - 1][3] < 2.5 <= rows[i][3]
]
print(f"rising sample edges at: {', '.join(f'{t*1e6:.2f}us' for t in crossings)}")

# 1) does out track in?
track_err = max(abs(vout - vin) for _, vin, vout, _ in rows)
print(f"\nmax |v(out) - v(in)| over the run : {track_err:.4f} V")

# 2) within each hold interval, how much does out move?
print("\nper hold interval (settling excluded):")
worst = 0.0
for k, start in enumerate(crossings):
    end = crossings[k + 1] if k + 1 < len(crossings) else rows[-1][0]
    seg = [r for r in rows if start + 100e-9 <= r[0] <= end - 1e-9]
    if len(seg) < 3:
        continue
    outs = [r[2] for r in seg]
    ins = [r[1] for r in seg]
    drift = max(outs) - min(outs)
    worst = max(worst, drift)
    print(
        f"  {start*1e6:5.2f}us -> {end*1e6:5.2f}us : "
        f"v(out) {min(outs):.3f}..{max(outs):.3f} (drift {drift:.3f} V), "
        f"v(in) {min(ins):.3f}..{max(ins):.3f}, "
        f"sampled value should be {seg[0][1]:.3f}"
    )

print(f"\nworst drift within a hold interval: {worst:.4f} V")
print()
if worst < 0.05:
    print("VERDICT: out HOLDS between sample edges -- LRM behaviour")
else:
    print("VERDICT: out TRACKS the input -- the event never fires, body runs every step")
