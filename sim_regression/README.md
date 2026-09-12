# Transient regression harness

Local-only. These tests run a compiled model in **ngspice** and measure the
waveform, which is the one thing the in-tree harnesses cannot do:

| harness | lowers equations | steps time | judges |
| --- | --- | --- | --- |
| `test_data/mir` | no | no | MIR shape |
| `test_data/ui` | n/a | no | diagnostics |
| `openvaf/tests/integration.rs` (mock sim) | yes | state swap only, `abstime = 0` | residuals and Jacobians |
| this | yes | yes, a real integrator | behaviour over time |

`slew`'s rate limit, a filter's corner frequency and — above all — whether an
event actually fires are claims about waveforms, so they belong here.

## Running

```sh
export PATH=$HOME/ngspice-install/bin:$PATH

cd sim_regression/sample_hold
../../target/release/openvaf-r sample_hold.va -o sample_hold.osdi
ngspice -b sample_hold.cir
python3 ../analyze_sample_hold.py

cd ../above_init
../../target/release/openvaf-r above_init.va -o above_init.osdi
ngspice -b above_init.cir
python3 ../analyze_above_init.py
```

Each analyzer exits non-zero on failure.

## sample_hold — passing

`sample_hold.va` is VAMS-2023 §5.10.3.1's own sample-and-hold, written verbatim.
Per the LRM `state` updates only when `V(smpl)` crosses the threshold upward, so
`out` is a staircase holding each sampled value until the next crossing.

The analyzer checks two independent claims and exits non-zero if either fails:

| check | what it catches |
| --- | --- |
| hold | `out` must not move between sample edges |
| accuracy | the held value must be `v(in)` at the interpolated crossing instant |

Current result:

```
worst drift within a hold interval : 7.49e-06 V  (tol 5e-03)
worst held-vs-sampled error        : 7.50e-05 V  (tol 1e-03)
PASS: out holds each sampled value between edges -- LRM behaviour
```

The 75 uV is not noise, it is the remaining half of issue #37: `cross` detects
the crossing but does not yet *place* a timestep on it, so the event lands on the
first accepted step after the crossing and the input has moved on by then (0.5
V/us times one step). Closing that means driving `bound_step` from the pending
crossing so the step lands inside the `time_tol` / `expr_tol` box, at which point
this number should drop by orders of magnitude. It is the natural next
acceptance criterion — tighten `SAMPLE_TOL` when it does.

For the record, the two earlier states of this test, both of which it now
distinguishes by name:

```
local-all, before any crossing detection:
  worst drift 0.9430 V   -> out TRACKS the input, the body runs every step

with crossing detection but retained state in the OSDI state array:
  v(out) == 0 throughout -> the crossing is never detected at all
```

## above_init — passing

`above_init.va` is VAMS-2023 §5.10.3.2's own sample-and-hold: 5.10.3.1's module
with `above` in place of `cross`. The netlist drives the scenario the clause was
written for — the clock sits at 5 V for the entire run and never crosses the
2.5 V threshold in either direction, so `cross` has nothing to trigger on and
would leave `out` at 0 forever. The LRM's words: `cross` "would never trigger,
even if the voltage on the smpl port is always above 2.5V".

```
v(smpl)                 : 5.000 .. 5.000 V  (never crosses 2.5 V)
v(in) over the run      : 1.056 .. 6.000 V
v(out) after settling   : 1.0000 .. 1.0000 V
PASS: above() sampled at initialization and held -- LRM behaviour
```

This one earns its place twice over. `above`'s initialization event was first
written gated on the `static` analysis flag, which the mock simulator was happy
with because the test handed it that flag — but ngspice sets ANALYSIS_STATIC only
on the *first Newton iteration* of the initial step and ANALYSIS_TRAN on the
rest, so the event fired on one iteration and was overwritten by the others, and
`v(out)` stayed at 0 for the whole run. The flags are not stable within a
timestep; `$abstime` is. Nothing but a real integrator would have caught that.

## Why retained state does not live in the OSDI state array

Worth writing down, because it is the reason the middle state above looked like a
compiler bug and was not one.

`@(cross)` needs the value its expression had at the previous *accepted* timestep.
The obvious home is OSDI's `prev_state` / `next_state`, and that is where it
started. It cannot work there:

* `OSDIload` passes `ckt->CKTstates[0]` as **both** `prev_state` and `next_state`
  (ngspice-46 `src/osdi/osdiload.c:141`), so the two alias.
* `dctran` rotates that ring once per accepted step
  (`src/spicelib/analysis/dctran.c:659`) over `MAX(2,maxord)+2` buffers
  (`cktsetup.c:192`), so with the default `maxord = 2` a model reads back the
  slot it wrote **four steps** ago.

Neither is a conformance violation: the OSDI header documents no semantics for
those pointers, and the array's real purpose is `$limit`, where aliasing gives
exactly the previous-Newton-iteration value that limiting wants. It is also why
nobody noticed — a limit state only steers the Newton path and never the
converged answer. A retained value *is* the answer.

So retained slots live in the instance data instead, which no simulator rotates,
committed on `$abstime` movement. See the retained-state note at the top of
`openvaf/osdi/stdlib.c` for the mechanism and its rejected-timestep handling.

Two things are still worth fixing upstream, neither of them here:

1. **ngspice**: even read as "previous iteration", the ring rotation is wrong.
   The first Newton iteration of every step reads a four-step-old buffer rather
   than the last accepted value, because nothing copies `CKTstates[1]` into
   `[0]` for OSDI devices (classic SPICE devices rewrite all their state each
   load, so they never needed it). That is a convergence-quality bug in `$limit`
   that stands on its own.
2. **OSDI**: there is no facility with accepted-timestep semantics, and one
   pointer cannot serve both meanings. The precedent for how it should look is
   `absdelay`, the other genuinely time-dependent operator: it is a
   descriptor-level protocol (`OsdiAbsDelayInfo`) where the simulator owns the
   history, not something a model fakes through the state array.
