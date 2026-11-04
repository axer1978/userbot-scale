-- More than one booking at the same time (booking.max_at_once).
--
-- Many customers never come, so an account may take several bookings for
-- one time: every time he asks goes to the owner, who sees which bookings
-- it overlaps and decides. 0003's exclusion constraint refused any overlap
-- of two live bookings of a tenant; it now leaves out bookings written with
-- overlap_ok on (booking_store.BookingStore.overlap_ok: their account allows
-- more than one at a time; the limit is checked in code). For an account
-- that keeps "never two at once" the database still guarantees it.

ALTER TABLE bookings
  -- Written with each new booking and each new time: whether the account
  -- allowed bookings at the same time then.
  ADD COLUMN overlap_ok BOOLEAN NOT NULL DEFAULT FALSE;

-- 0003 left the constraint unnamed: drop it by what it is.
DO $$
DECLARE
  found TEXT;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint WHERE conrelid = 'bookings'::regclass AND contype = 'x'
  LOOP
    EXECUTE format('ALTER TABLE bookings DROP CONSTRAINT %I', found);
  END LOOP;
END $$;

ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap
  EXCLUDE USING gist (tenant_id WITH =, tstzrange(starts_at, blocked_until) WITH &&)
  WHERE (state IN ('requested', 'pending', 'confirmed') AND NOT legacy AND NOT overlap_ok);
