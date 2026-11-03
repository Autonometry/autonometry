"""run_planning_check.py -- how often the 'rule of three' zero-event claim is wrong when the
true rate sits exactly at the threshold and sessions are over-dispersed (NB2 gamma frailty).

Setup: lambda_max = 0.1 interventions per active hour, true rate = lambda_max.
Exposure T is split equally over R 'frailty units' (robots / deployments) that each carry an
independent Gamma(1/a, a) multiplier (mean 1, variance a). R = n_sessions reproduces iid
per-session frailty (1 h sessions); R = 1 is a single shared frailty for the whole test.
A zero count after T hours leads the tester to claim 'rate < lambda_max at 95 %'. At the
threshold this claim is wrong, so the probability of a zero count is the false-claim rate;
the Poisson design (T = -ln(0.05)/lambda_max = 29.96 h) targets 5 %.
Compared designs: T_poisson = hours_for_zero_events(0.1) and T_nb = hours_for_zero_events_nb(0.1, a).
Monte Carlo: 20,000 runs per cell, seed 20261030; the exact value prod_r (1 + a*lambda*T/R)^(-1/a)
is reported alongside.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("AUTONOMY_METER_REF", os.path.join(os.path.dirname(HERE), "work", "reference")))
from autonomy_meter import planning

SEED = 20261030
N_RUNS = 20000
LAM = 0.1


def p_zero_exact(lam, T, a, R):
    if a == 0:
        return float(np.exp(-lam * T))
    return float((1 + a * lam * T / R) ** (-R / a))


def simulate(lam, T, a, R, rng):
    per = T / R
    if a == 0:
        counts = rng.poisson(lam * T, N_RUNS)
    else:
        G = rng.gamma(1 / a, a, size=(N_RUNS, R))
        counts = rng.poisson(lam * per * G).sum(axis=1)
    pz = float((counts == 0).mean())
    return pz, float(np.sqrt(pz * (1 - pz) / N_RUNS))


def main():
    rng = np.random.default_rng(SEED)
    rows = []
    T_pois = planning.hours_for_zero_events(LAM)
    for a in (0.0, 0.3, 0.5, 1.0):
        T_nb = planning.hours_for_zero_events_nb(LAM, a) if a > 0 else T_pois
        for design, T in (("poisson_rule_of_three", T_pois), ("nb_rule", T_nb)):
            for R_label, R in (("shared_1", 1), ("robots_3", 3), ("robots_10", 10), ("iid_sessions_1h", max(int(round(T)), 1))):
                pz, se = simulate(LAM, T, a, R, rng)
                rows.append(dict(alpha_nb=a, design=design, hours=T, frailty_structure=R_label, n_frailty_units=R,
                                 false_claim_rate_mc=pz, mc_se=se, false_claim_rate_exact=p_zero_exact(LAM, T, a, R),
                                 nominal=0.05, n_runs=N_RUNS))
    df = pd.DataFrame(rows)
    out = os.path.join(HERE, "results", "planning_zero_event_check.csv")
    df.to_csv(out, index=False)
    print(df.round(4).to_string())
    return df


if __name__ == "__main__":
    main()
