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
```

## sample_hold — currently FAILING, by design

`sample_hold.va` is VAMS-2023 §5.10.3.1's own sample-and-hold, written verbatim.
Per the LRM `state` updates only when `V(smpl)` crosses the threshold upward, so
`out` is a staircase holding each sampled value until the next crossing.

Baseline measured on `local-all` (2026-09-11), which is the behaviour issue #37
describes:

```
max |v(out) - v(in)| over the run : 0.0050 V      <- out follows the input
worst drift within a hold interval: 0.9430 V      <- should be ~0
VERDICT: out TRACKS the input -- the event never fires, body runs every step
```

The 5 mV is just the 10 ns `transition` lag on a 0.5 V/us ramp: the output is
tracking, not holding.

**This is the acceptance criterion for event scheduling.** When `cross()` takes
part in scheduling, the same run must instead show a drift near zero within each
hold interval, and `v(out)` sitting at the sampled values 0.554, 1.554, 2.554,
3.554 and 4.554 V. Until then the test failing is the correct result, and the
`unscheduled_event` lint warns about the same thing at compile time.
