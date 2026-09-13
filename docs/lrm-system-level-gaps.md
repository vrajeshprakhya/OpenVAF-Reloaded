# VAMS-2023 conformance gaps for system-level modeling

What stands between OpenVAF-Reloaded today and writing system-level behavioral
models (PLLs, data converters, samplers, DSP chains, jitter and noise budgets) in
Verilog-A, measured against the Accellera VAMS-2023 LRM.

Every status below was established by compiling a probe model with
`target/release/openvaf-r`, not by reading a support list. Clause numbers are
VAMS-2023.

The LRM has no clause called "system-level modeling". The features that flow
needs are spread across 4.5 (analog operators), 5.10 (analog events), 9.5, 9.13,
9.17 and 9.21 (file I/O, random distributions, kernel control, table lookup) and
10 (compiler directives). Chapter 7 and the `$driver_*` functions of 9.22/9.23
are mixed-signal: they need a digital engine and are out of scope for an
analog-only OSDI flow.

## The one structural blocker

Most of what follows is not independent work. OSDI 0.4 gives a model no way to
say anything to the *integrator*. The descriptor
(`openvaf/osdi/header/osdi_0_4.h:204-236`) has `eval`, the loads, setup, given
flags and noise, and nothing else. It specifically lacks:

- **an "accepted timestep" callback.** ngspice calls one per device
  (`DEVaccept`, e.g. `src/spicelib/devices/vsrc/vsrcacct.c`); OSDI exposes none.
- **a way to request a breakpoint.** ngspice has `CKTsetBreak(ckt, t)` and its
  own sources use it; OSDI cannot reach it.
- **a way to announce a discontinuity.** The only return channel is
  `EVAL_RET_FLAG_LIM`, which ngspice turns into `CKTnoncon++`
  (`src/osdi/osdiload.c:257`) — one more Newton iteration, not an integrator
  history reset plus a breakpoint.

Three separate LRM features reduce to that single missing facility: retained
state commit (worked around with `$abstime` — see `sim_regression/README.md`),
`timer` breakpoints, and `$discontinuity`.

`bound_step` is the one channel that does exist, and ngspice honours it
(`src/osdi/osditrunc.c`), so it can *approximate* breakpoints by capping the
step. That is the basis for most of the work below, but it cannot place a point
at an exact time.

An OSDI proposal for an accept callback plus a breakpoint/discontinuity request
is therefore the highest-leverage item here. The precedent for how it should look
is `absdelay`: a descriptor-level protocol where the simulator owns the
time-dependent part.

## Tier 1 — accepted but not honoured

The worst category is a model that compiles, behaves differently from what it
describes, and says nothing about it. One item was in it; it now warns.

| Feature | Clause | Status |
| --- | --- | --- |
| `$discontinuity(n)`, n >= 0 | 9.17.1 | Still dropped, but **no longer silent**: warns as `ignored_discontinuity` (L019). |
| Analog variable persistence | 4.5.10, 5.10.2 | Fixed. Retention is granted wherever a read can precede its write. |

`hir_lower/src/expr.rs` handles only `$discontinuity(-1)`, the form that belongs
with `$limit` (9.17.3); every other degree lowers to nothing, because OSDI has no
channel to announce a discontinuity. It now says so, points at `$bound_step` as
the thing that does reach the simulator, and notes that the `-1` form is
supported — so a model can no longer claim a discontinuity that never arrives.

Actually announcing it still needs the OSDI facility below. Driving `bound_step`
down across the jump would be a partial answer, but the degree argument carries
no time, so the cap would have to be invented; that is a policy decision worth
making deliberately rather than silently.

### Analog variable persistence

Found while implementing `last_crossing`, and fixed with it. A variable assigned
inside an event handler already retained across timesteps — that is the machinery
behind `@(cross)` latches — but a variable **read before it was assigned** within
one evaluation read its initialization value instead of what it held last time.
That is the shape of 4.5.10's own period example, which copies `previous = latest`
inside the `@(cross)` handler before `latest = last_crossing(...)` runs further
down the block.

Retention is now decided by a conservative definite-assignment walk over the
analog block: a write settles a variable for what follows only if it is certain to
have happened, so anything that may not run — one arm of an `if`, a loop body, an
event handler — leaves it unsettled, and any read that can come first grants
retention. That single rule subsumes the old "assigned inside `@(cross)`" rule,
since a handler may not fire.

Fixing it required making `@(initial_step)` actually conditional. It had been
lowered unconditionally, with assignments to retained variables skipped so they
were not re-applied every evaluation — which silently meant an initializer only
ever worked if the value it wrote was zero. It is now guarded on a retained
"first evaluation" flag, so an initializer applies once and retention carries it.

The blast radius is real and worth knowing. Compact models wrap their parameter
precomputation in `@(initial_step)`: BSIM4 has ~4000 lines in one, and now
retains the 319 variables it defines rather than recomputing them on every Newton
iteration. Verified byte-identical DC sweep and transient output against the
previous compiler, so the numbers do not move — but instance data grows (~5 kB for
BSIM4) and the work moves from every evaluation to once per setup. `OSDItemp`
re-invokes `setup_instance`, so a temperature change re-runs the precomputation.

Of ten real compact models compiled before and after, seven were byte-identical;
BSIM4 grew 5.1%, PSP103 3.1%, HICUML2 1.9%.

## Tier 2 — monitored events

| Feature | Clause | Status |
| --- | --- | --- |
| `cross` | 5.10.3.1 | Works. Detects the crossing and guards the body. |
| `above` | 5.10.3.2 | Works. Rising-only, plus the initialization event. |
| `timer` | 5.10.3.3 | Parses and type-checks, **body runs every evaluation** (warns `L018`). |
| `absdelta` | 5.10.3.4 | Same — but see the note below. |

`above` shares `cross`'s crossing detection, with two differences the LRM is
explicit about: no `dir` argument, so it triggers only from below, and it also
fires when the expression is already positive before time has moved — "if the
expression is positive at the conclusion of the initial condition analysis that
precedes a transient analysis, the above() function shall generate an event".
Note the argument layout differs too: `enable` is the fourth argument, not the
fifth.

That initialization event is keyed on `$abstime`, **not** on the analysis flags.
ngspice reports `ANALYSIS_STATIC` only on the first Newton iteration of the
initial step and `ANALYSIS_TRAN` on the rest, so a flag-gated event fires on one
iteration and is then overwritten by the others — which is exactly how the first
attempt failed. The mock simulator cannot catch that, because the test supplies
the flags itself; `sim_regression/above_init` does.

One known imprecision: in a dc sweep `above` fires at every point where the
expression is positive rather than only where it crosses, because nothing commits
retained state while time stands still. 5.10.3.2 asks for crossings there and
does not control the sweep step to resolve them, so over-firing samples the same
value a crossing would.

`timer` needs a next-event time in retained state and a `bound_step` capped at
`next_event - now`. It is the first feature to really want the missing breakpoint
facility, since `bound_step` lands the point at or just before the event rather
than within `time_tol` of it — acceptable under 5.10.3.3's "at, or just beyond,
the time of the event" only if the cap is tight.

`absdelta` is worth a scoping decision rather than an implementation. 5.10.3.4
says it "is only allowed in an initial or always block of a Verilog-AMS module":
it is a digital-side sampler for converting analog signals into real-typed
digital variables. Accepting it in an analog block, as today, is not something
the LRM sanctions. Consider rejecting it in Verilog-A instead of implementing it.

### Accuracy, not presence

`cross` fires on the first accepted step after the crossing, not inside the
`time_tol`/`expr_tol` box that 5.10.3.1 Figure 5-6 requires ("the event shall
occur after the threshold crossing, and while the signal remains in the box").
Measured residual in `sim_regression`: 7.5e-05 V on a 0.5 V/us ramp. Closing it
means driving `bound_step` from the pending crossing.

## Tier 3 — hard errors that block whole model classes

All of these produce `function 'x' is currently not supported by OpenVAF`, from
the `UNSUPPORTED` list at `sourcegen/src/hir_builtins.rs:29`.

| Feature | Clause | What it blocks |
| --- | --- | --- |
| `zi_nd`, `zi_np`, `zi_zd`, `zi_zp` | 4.5.12 | Linear discrete-time filters. Sampled-data systems, digital filter models, sigma-delta modulators, any DSP chain. A unity Z-filter is a sample-and-hold with period T. |
| `$random`, `$arandom`, `$dist_*`, `$rdist_*` | 9.13 | Jitter, noise injection, mismatch, Monte-Carlo. |
| `$table_model` | 9.21 | Data-driven behavioral models from swept or measured data. Not merely unsupported: the name is commented out of the sysfun list (`hir_builtins.rs:207`), so it does not resolve at all. |
| `$fopen`, `$fclose`, `$fdisplay`, `$fwrite`, `$fstrobe`, `$fmonitor`, `$fscanf`, `$fgets`, `$sformat`, `$swrite`, `$sscanf`, `$fseek`, `$ftell`, `$feof`, … | 9.5 | File-driven stimulus and result logging — the normal way a system-level testbench gets vectors in and measurements out. |
| `$simprobe` | 9.16 | Probing another instance's signals. |
| `$analog_node_alias`, `$analog_port_alias` | 9.20 | Node aliasing. |
| `$test$plusargs`, `$value$plusargs` | 9.12 | Command-line configuration of a model. |

Two of these interact in a way worth calling out. 5.10.3.3's own PRBS generator
example is:

```verilog
analog begin
    @(timer(0, period))
        x = $random + 0.5;
    V(out) <+ transition( x, 0.0, period/100.0 );
end
```

The LRM's canonical `timer` example cannot be written today for two independent
reasons — `timer` does not schedule, and `$random` does not exist.

`$random` also deserves a note on ordering. Its seed is an inout integer that the
function mutates, so calling it in straight-line analog code re-randomizes on
every Newton iteration and will not converge. It is only meaningful inside a
scheduled event body, which makes it dependent on Tier 2. It also needs
per-instance seed storage, and the instance-data pattern added for retained state
is the right home for that.

## Tier 4 — language and grammar

| Feature | Clause | Status |
| --- | --- | --- |
| `` `default_discipline `` | 10.2 | Not implemented; fails as ``macro '`default_discipline' has not been declared``. |
| `` `default_transition `` | 10.3 | Same. Directly relevant here: it sets the default rise/fall for every bare `transition()` in a file, which is exactly how system-level models get written. |
| Multi-dimensional arrays | 3.x, 4.2.14 | Parse error on the second subscript (`real g[0:1][0:1]`). 1-D arrays work. |
| `paramset` | 6.x | Parse error: `expected 'discipline', 'nature' or 'module'`. Matters more for device libraries than for system-level work. |

## Confirmed working

Recorded so it is not re-litigated. All probed: `ddt`, `idt`, `idtmod`,
`absdelay`, `transition`, `slew`, `ddx`, `limexp`, `last_crossing`, all four
`laplace_*` forms
(4.5.11), `white_noise` / `flicker_noise` / `ac_stim` / `analysis` (4.6),
`$limit` (9.17.3), `$bound_step` (9.17.2), named events and `->` (5.10.4),
`initial_step` / `final_step` (5.10.2), `analog initial` (5.2.1), indirect
contributions (5.6.7), analog user-defined functions (4.7), 1-D arrays, strings
and string parameters, `aliasparam`, `$param_given`, `$port_connected`,
`$simparam`, `$temperature`, `$vt`, `$abstime`, `$finish` / `$stop` / `$error` /
`$info`, bus ports with `genvar` loops, and the display tasks (9.4).

## Suggested order

1. ~~**`$discontinuity`**~~ — done: warns as `ignored_discontinuity` (L019).
   Announcing it for real needs step 5.
2. ~~**`above`**~~ — done. Rising-only crossing plus the initialization event,
   verified against ngspice in `sim_regression/above_init`.
3. ~~**`last_crossing`**~~ — done. Retained state plus linear interpolation;
   4.5.10 does not control the timestep, so it needed no new infrastructure.
   Measures crossings to ~0.2 ps in `sim_regression/last_crossing`.
4. ~~**Analog variable persistence**~~ — done. Retention wherever a read can
   precede its write, plus a conditional `@(initial_step)`. 4.5.10's period
   example now measures the period.
5. **`timer`** — retained next-event time plus `bound_step`. First real want of
   the missing breakpoint facility.
6. **OSDI proposal** — accept callback plus breakpoint/discontinuity request.
   Unblocks 1 and 5, closes the `cross` tolerance box, and lets the `$abstime`
   retained-state workaround retire.
7. **`$random` / `$dist_*`** — per-instance seed in instance data; only useful
   after 5.
8. **Z-transform filters** — retained state plus T-periodic sampling plus
   `transition`, so largely a composition of 3, 5 and what already exists.
9. **`` `default_transition `` / `` `default_discipline ``** — independent,
   small, and immediately visible to model writers.
10. **`$table_model`, then file I/O** — the two largest self-contained items.
