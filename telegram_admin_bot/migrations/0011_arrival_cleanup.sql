-- Arrival instructions that delete themselves (booking_flow.py).
--
-- With booking.arrival_cleanup_minutes set, the arrival instructions and the
-- photos sent with them are deleted from the chat, for both sides, that long
-- after they were sent: door codes and the flat number don't stay in the
-- customer's phone. The rows in `messages` keep only a placeholder after.

ALTER TABLE bookings
  -- messages.id of what the arrival step sent (text and photos).
  ADD COLUMN instructions_message_ids BIGINT[]    NOT NULL DEFAULT '{}',
  ADD COLUMN instructions_cleanup_at  TIMESTAMPTZ,
  ADD COLUMN instructions_cleaned_at  TIMESTAMPTZ;

CREATE INDEX idx_bookings_instructions_cleanup ON bookings (tenant_id, instructions_cleanup_at)
  WHERE instructions_cleanup_at IS NOT NULL AND instructions_cleaned_at IS NULL;
