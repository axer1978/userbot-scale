-- Finetune runs from the chats a person typed by hand (finetune.py,
-- finetune_api.py hand-runs).
--
-- Nothing new is stored: the run picks the chats where the business's
-- replies were typed on the phone or by hand in the panel, reads them from
-- messages when it starts, and records only their names, like any run.

ALTER TABLE finetune_runs DROP CONSTRAINT finetune_runs_source_check;
ALTER TABLE finetune_runs ADD CONSTRAINT finetune_runs_source_check
  CHECK (source IN ('screenshots', 'text', 'examples', 'hand'));
