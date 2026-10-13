# ADR-0001: Judge alert policies in a closed loop, train on observable labels, score on latent truth

## Status
Accepted

## Context
The deliverable is a list of jobs for a crew that can do about six planned jobs a week. A
model can look good on held-out rows and still be the wrong thing to send that crew, for
three reasons:

1. **The labels are censored by maintenance.** A pump that got a preventive job never shows
   whether it would have failed. Labelling those days "no failure" teaches the model that
   whatever the planners chose to service was healthy.
2. **Alerts change the future.** Once an asset is serviced, its next failure moves. Offline
   metrics computed on a history produced by a different policy do not say what this policy
   would do to uptime.
3. **The crew is the constraint.** A model that orders the whole fleet well over months
   (high concordance) is not necessarily good at choosing this week's six jobs.

## Decision
- **Training labels are observable only.** A day is positive if a corrective job starts in
  the next 14 days, negative if the next event of any kind is more than 14 days away, and
  dropped otherwise (a preventive job or the end of the period hides the outcome). Rows
  whose horizon crosses the training cutoff are also dropped.
- **Evaluation uses the latent life.** The plant model records each asset's latent failure
  day even when a job pre-empted it. Offline scores and the closed-loop precision use this
  truth by construction; no model judges another model.
- **Every policy runs a year in the closed loop.** The plant is replayed to the end of the
  two-year training history, forked, and each policy runs 365 days from the same fork with
  the same random stream (daily noise drawn in fixed-size blocks, per-asset streams for new
  lives), so a difference between policies is caused by the policy. Five forks give a spread.
- **One feature function for both paths.** `window_features` builds the training table and
  the scheduler's live features. A test asserts the two agree on the same plant.
- **Alert rule is cost break-even, ranked by expected saving.** Propose a job when
  `p * failure_cost > planned_job_cost`; the scheduler takes the highest expected savings up
  to the day's and week's crew capacity.

## Alternatives rejected
- **Offline precision@k on the historical policy's data as the headline.** It ignores that
  the alert changes the outcome, and it rewards a model for agreeing with past planners.
- **Labelling censored days as negatives.** Simple, and wrong in a way that grows with how
  good the existing preventive programme is.
- **A fixed probability threshold (e.g. 0.5).** It ignores that a compressor failure costs
  about twice a pump failure; the break-even rule makes the trade explicit.
- **Time-varying Cox as the only model.** Kept as a comparator (landmark Cox on the same
  features) and for the remaining-life view; a single linear hazard cannot represent the
  different fault signatures, and it shows in the closed loop.

## Consequences
- Results are about the method on a plant whose physics are an assumption; the README
  says so and names it the biggest gap.
- The same harness can test a new policy, a different crew size or a different cost ratio
  by editing `config.py` and re-running one command.
