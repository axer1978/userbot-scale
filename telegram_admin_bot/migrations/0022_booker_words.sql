-- The booker's own words (booking_flow.py, bookings.py).
--
-- The booker (booking.provider: the girl, or whoever books for her) answers
-- a request in her own words: "yes, tell him to bring cash", "no anal for
-- him", "ask him if he wants 2 hours". What she wants him told waits on the
-- booking until his next reply carries it, and is cleared item by item (by
-- its id) once that reply is sent or saved for approval, so words she adds
-- meanwhile stay. The extras she takes or refuses are kept per booking; one
-- she refused is never offered again in that chat. When she asks him
-- something, his answers go on to her for a while.

ALTER TABLE bookings
  -- Her words waiting for his next reply, oldest first:
  -- [{"id", "at", "tell", "ask", "extras_ok": [...], "extras_no": [...]}].
  ADD COLUMN booker_news JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- The extras she said yes to / refused for this customer, comma lists.
  ADD COLUMN extras_ok TEXT NOT NULL DEFAULT '',
  ADD COLUMN extras_no TEXT NOT NULL DEFAULT '',
  -- She asked him something: what he writes goes on to her until she next
  -- acts on the booking, it is over, or three hours pass.
  ADD COLUMN booker_asked_at TIMESTAMPTZ;

-- A standing instruction of hers for every customer ("from now on cash
-- only") is saved as a proposed operator rule (0017) for the admin to accept.
ALTER TABLE prompt_rules DROP CONSTRAINT prompt_rules_source_check;
ALTER TABLE prompt_rules ADD CONSTRAINT prompt_rules_source_check
  CHECK (source IN ('draft_edit', 'correction', 'manual', 'booker'));
